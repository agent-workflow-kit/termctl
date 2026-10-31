"""Gateway: the single boundary for subprocesses, the filesystem and the clock.

Every call to an external program goes through a ``Gateway`` so that tests can substitute
fakes (see design guide §14); time comes from an injectable ``Clock``. Commands are always
run with an argv list; the gateway never adds a shell (a hook's ``sh -c`` is in its argv).
"""

from __future__ import annotations

import os
import pwd
import select
import selectors
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import FrameType
from typing import Any, NamedTuple, Protocol

from . import hookwatch
from .hookwatch import kill_unreaped

# How long connecting to a Unix socket may block (a listener with a full backlog).
SOCKET_TIMEOUT = 10.0

_CHUNK = 65536
# How long run_group keeps reading after the program exited while something it left behind
# holds its output open (interface.md "Hooks").
LEADER_EXIT_GRACE_S = 1.0
# How often run_group looks for the program's exit while its output is still open.
_EXIT_POLL_S = 0.05
# How long a killed program may take to be reaped before GroupNotStopped.
STOP_WAIT_S = 5.0
# The watchdog runs by file path, so it works where the interpreter cannot import termctl (a
# user-site install, PYTHONPATH); it imports only the standard library.
WATCHDOG_PATH = os.path.abspath(hookwatch.__file__)


class Terminated(BaseException):
    """termctl got SIGTERM or SIGHUP while a hook ran; the hook's group was killed first.
    termctl exits ``128 + signum``."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"terminated by signal {signum}")
        self.signum = signum


class GroupNotStopped(Exception):
    """A program to be killed (timed out or interrupted) was still not reaped after SIGKILL
    to its group and to itself, for example a set-user-ID program termctl may not signal."""

    def __init__(self, pgid: int) -> None:
        super().__init__(f"process group {pgid} could not be stopped")
        self.pgid = pgid


class GroupRun(NamedTuple):
    """The result of ``Gateway.run_group``.

    ``pgid`` is the process group the gateway created for the program (its PID).
    ``returncode`` is None when the program timed out (its group was killed), else its exit
    status, or the negative signal number that ended it. ``stdout`` and ``stderr`` hold at most
    the requested limits; ``*_truncated`` says the program wrote more (the rest was read and
    discarded, so a verbose program is never blocked or killed for it).
    """

    pgid: int
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool

    @property
    def timed_out(self) -> bool:
        return self.returncode is None


@dataclass(frozen=True)
class FileInfo:
    """What termctl reads from ``lstat``: the file type and permission bits, and the owner."""

    mode: int
    uid: int


class BoundedOutput(NamedTuple):
    """The result of ``Gateway.run_bounded``: at most ``limit`` bytes of each stream, and
    whether either stream had more. When output was truncated the child is killed if still
    running, so ``returncode`` is either its own exit status or the negative kill signal;
    callers must not rely on it when ``truncated`` is true."""

    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool


class Gateway(Protocol):
    """External effects termctl needs. Later stages add tty writes here."""

    def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...

    def run_group(
        self,
        argv: list[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> GroupRun:
        """Run ``argv`` in a new process group with exactly ``env``, write ``stdin`` to it, and
        keep at most ``stdout_limit`` and ``stderr_limit`` bytes of its output.

        The run ends when the program has exited and closed both output streams, or
        ``LEADER_EXIT_GRACE_S`` after the program (the group's leader) exited while something
        it left behind still holds a stream open; then the leader's exit status counts and the
        rest of its group is left alone. If the leader has not exited within ``timeout``
        seconds, the gateway sends SIGKILL to the group it created and to the leader (and to
        nothing else) and returns ``returncode=None``.

        While the program runs, SIGTERM and SIGHUP (unless ignored) raise ``Terminated`` and
        SIGINT raises ``KeyboardInterrupt``; the group is killed the same way first. Raises
        ``OSError`` or ``ValueError`` if the program cannot be started and ``GroupNotStopped``
        if it was to be killed but could not be.
        """
        ...

    def spawn_watched(
        self, argv: list[str], *, stdin: bytes, env: Mapping[str, str], timeout: float
    ) -> int:
        """Start ``argv`` detached under a watchdog (``hookwatch.py``, run by its file path) in a
        new session,
        with exactly ``env``, ``stdin`` as its input and no output, and return the watchdog's
        PID (also its process group and session id) without waiting. The watchdog runs
        ``argv`` in a process group of its own and kills that group after ``timeout`` seconds.
        Raises ``OSError`` or ``ValueError`` if the watchdog cannot be started."""
        ...

    def run_bounded(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        limit: int,
    ) -> BoundedOutput:
        """Run ``argv`` with no stdin and keep at most ``limit`` bytes of stdout and of stderr.

        Output beyond ``limit`` kills the program and sets ``truncated``. Raises
        ``subprocess.TimeoutExpired`` (after killing it) if it has not finished, and closed
        both streams, within ``timeout`` seconds, and ``OSError`` if it cannot be started.
        """
        ...

    def current_dir(self) -> str:
        """The caller's current directory; raises ``OSError`` if it cannot be read."""
        ...

    def is_dir(self, path: str) -> bool:
        """Whether ``path`` exists and is a directory (symlinks followed)."""
        ...

    def is_executable_file(self, path: str) -> bool:
        """Whether ``path`` is a regular file (symlinks followed) the real uid may execute."""
        ...

    def real_uid(self) -> int:
        """The real uid of the termctl process."""
        ...

    def lstat(self, path: str) -> FileInfo:
        """``lstat`` of ``path`` (a symlink is not followed); raises ``OSError``."""
        ...

    def real_path(self, path: str) -> str:
        """``path`` with every symlink resolved; raises ``OSError`` if any part of it is
        missing or cannot be resolved."""
        ...

    def read_link(self, path: str) -> str:
        """The target of the symlink ``path``, as stored; raises ``OSError``."""
        ...

    def make_dir(self, path: str, mode: int) -> None:
        """``mkdir`` of ``path`` with ``mode`` (less the umask); never creates parents. Raises
        ``FileExistsError`` if ``path`` exists (a symlink included) and ``OSError`` otherwise."""
        ...

    def platform(self) -> str:
        """``sys.platform``: ``darwin`` on macOS, ``linux`` on Linux and WSL."""
        ...

    def read_proc_stat(self, pid: int) -> str | None:
        """The contents of ``/proc/<pid>/stat`` (Linux and WSL), decoded as UTF-8 with
        surrogateescape, or None when no such process exists (the file is missing, or the
        process went away while reading it). Raises ``OSError`` for any other failure."""
        ...

    def socket_peer_pid(self, path: str) -> int:
        """Linux: connect to the Unix socket ``path`` and return the listener's PID from
        ``SO_PEERCRED``, as the kernel translates it into termctl's PID namespace (0 when the
        listener is not visible there). Sends nothing. Raises ``OSError`` if it cannot."""
        ...

    def stdin_isatty(self) -> bool:
        """Whether stdin (fd 0) is a terminal."""
        ...

    def user_name(self) -> str | None:
        """The real uid's user name from the password database, or None if it has none."""
        ...


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware (UTC)."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards, for timeouts and durations."""
        ...


class SystemGateway:
    """The real gateway."""

    def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv`` without a shell and capture its output as text.

        ``env`` replaces the whole environment when given; ``None`` inherits termctl's own.
        Output is read as bytes and decoded with ``.decode("utf-8", "surrogateescape")``
        whatever the locale (tmux runs with ``-u`` and writes UTF-8; each undecodable byte is
        kept as a lone surrogate), with no newline translation, so a ``\r`` in tmux's output
        reads back as ``\r`` and no byte makes the call fail. ``stdin`` is encoded with
        ``.encode("utf-8", "surrogateescape")``.
        """
        completed = subprocess.run(
            argv,
            input=None if stdin is None else stdin.encode("utf-8", "surrogateescape"),
            capture_output=True,
            timeout=timeout,
            env=None if env is None else dict(env),
            check=False,
        )
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            completed.stdout.decode("utf-8", "surrogateescape"),
            completed.stderr.decode("utf-8", "surrogateescape"),
        )

    def run_group(
        self,
        argv: list[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> GroupRun:
        with _hook_signals() as stops:
            return _run_group(argv, stdin, env, timeout, stdout_limit, stderr_limit, stops)

    def spawn_watched(
        self, argv: list[str], *, stdin: bytes, env: Mapping[str, str], timeout: float
    ) -> int:
        _default_sigchld()
        # A file, not a pipe, holds the input, so writing it never blocks on a program that
        # reads slowly or not at all. It is unlinked; only the watchdog's descriptor (which it
        # hands to the hook unread) keeps it.
        with tempfile.TemporaryFile() as data:
            data.write(stdin)
            data.seek(0)
            proc = subprocess.Popen(
                [sys.executable, "-I", WATCHDOG_PATH, str(timeout), *argv],
                stdin=data,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(env),
                start_new_session=True,
            )
        return proc.pid

    def run_bounded(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        limit: int,
    ) -> BoundedOutput:
        deadline = time.monotonic() + timeout
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
        )
        try:
            stdout, stderr, truncated = _read_bounded(proc, deadline, limit)
            if truncated:
                _kill(proc)
            else:
                proc.wait(timeout=max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            _kill(proc)
            raise subprocess.TimeoutExpired(argv, timeout) from None
        except BaseException:
            _kill(proc)
            raise
        finally:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        return BoundedOutput(proc.returncode, stdout, stderr, truncated)

    def current_dir(self) -> str:
        return os.getcwd()

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def is_executable_file(self, path: str) -> bool:
        try:
            return os.path.isfile(path) and os.access(path, os.X_OK)
        except ValueError:  # an embedded NUL
            return False

    def real_uid(self) -> int:
        return os.getuid()

    def lstat(self, path: str) -> FileInfo:
        st = os.lstat(path)
        return FileInfo(mode=st.st_mode, uid=st.st_uid)

    def real_path(self, path: str) -> str:
        return os.path.realpath(path, strict=True)

    def read_link(self, path: str) -> str:
        return os.readlink(path)

    def make_dir(self, path: str, mode: int) -> None:
        os.mkdir(path, mode)

    def platform(self) -> str:
        return sys.platform

    def read_proc_stat(self, pid: int) -> str | None:
        try:
            with open(f"/proc/{int(pid)}/stat", "rb") as stat_file:
                return stat_file.read().decode("utf-8", "surrogateescape")
        except (FileNotFoundError, ProcessLookupError):
            return None

    def socket_peer_pid(self, path: str) -> int:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(SOCKET_TIMEOUT)
            sock.connect(path)
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, _uid, _gid = struct.unpack("3i", creds)
        return pid

    def stdin_isatty(self) -> bool:
        return os.isatty(0)

    def user_name(self) -> str | None:
        # The password database, not $USER or $LOGNAME, which the caller's environment can set.
        try:
            return pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            return None


class _Capture:
    """At most ``limit`` bytes of one output stream; whether there were more."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def add(self, chunk: bytes) -> None:
        room = self.limit - len(self.data)
        if len(chunk) > room:
            self.truncated = True
        self.data += chunk[: max(room, 0)]


def _default_sigchld() -> None:
    """Undo an inherited ``SIGCHLD = SIG_IGN``: with it the kernel reaps children at once, so
    an exit status reads as 0 and a PID could be reused before its group is signaled."""
    if threading.current_thread() is not threading.main_thread():
        return  # termctl is single-threaded; only the main thread may change handlers
    if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)


class _StopSignals:
    """The handler ``_hook_signals`` installs for SIGTERM and SIGHUP: the first signal raises
    ``Terminated``, unless it is held, in which case ``release`` raises it.

    Held while the hook starts, so the signal cannot come between the fork and the moment the
    caller has the process to kill. Not blocked with ``pthread_sigmask``: the hook would inherit
    the blocked mask.
    """

    def __init__(self) -> None:
        self._raised = False
        self._held = False
        self._pending: int | None = None

    def handle(self, signum: int, frame: FrameType | None) -> None:
        # Only the first signal raises, so a second one cannot interrupt the cleanup.
        if self._raised:
            return
        self._raised = True
        if self._held:
            self._pending = signum
            return
        raise Terminated(signum)

    def hold(self) -> None:
        self._held = True

    def release(self) -> None:
        """Stop holding; raise ``Terminated`` for a signal that came while held."""
        self._held = False
        if self._pending is not None:
            signum, self._pending = self._pending, None
            raise Terminated(signum)


@contextmanager
def _hook_signals() -> Iterator[_StopSignals]:
    """While a hook runs: ``SIGCHLD`` at its default, and ``SIGTERM`` and ``SIGHUP`` (unless
    ignored or handled already) raise ``Terminated`` (see ``_StopSignals``), so the hook's group
    is killed before termctl exits. The previous handlers are restored afterwards."""
    stops = _StopSignals()
    if threading.current_thread() is not threading.main_thread():
        yield stops
        return
    _default_sigchld()
    previous: dict[int, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(signum) == signal.SIG_DFL:
            previous[signum] = signal.signal(signum, stops.handle)
    try:
        yield stops
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _run_group(
    argv: list[str],
    stdin: bytes,
    env: Mapping[str, str],
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
    stops: _StopSignals,
) -> GroupRun:
    deadline = time.monotonic() + timeout
    # A stop signal before proc is assigned would leave the group with nobody to kill it.
    stops.hold()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            process_group=0,  # setpgid(0, 0) in the child: the group id is proc.pid
        )
    except BaseException:
        stops.release()  # a stop signal that came meanwhile is what the caller must see
        raise
    out = _Capture(stdout_limit)
    err = _Capture(stderr_limit)
    leader = _LeaderExit(proc.pid)
    try:
        stops.release()
        finished = _exchange(proc, stdin, out, err, deadline, leader)
        if finished:
            try:
                proc.wait(timeout=max(deadline - time.monotonic(), 0))
            except subprocess.TimeoutExpired:
                finished = False
    except BaseException:
        try:
            _stop(proc)
        except GroupNotStopped:
            pass  # the interruption is what the caller must see
        raise
    finally:
        leader.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    if not finished:
        _stop(proc)
    return GroupRun(
        pgid=proc.pid,
        returncode=proc.returncode if finished else None,
        stdout=bytes(out.data),
        stderr=bytes(err.data),
        stdout_truncated=out.truncated,
        stderr_truncated=err.truncated,
    )


class _LeaderExit:
    """Whether a child has exited, found out without reaping it, so its PID (the id of the
    group it leads) stays reserved until the caller reaps it.

    ``os.waitid`` with ``WNOWAIT`` where Python has it (Linux, and macOS from Python 3.13);
    else a kqueue ``NOTE_EXIT`` filter (macOS before Python 3.13). Where neither works, or
    setting it up fails, ``available`` is False and the exit is only seen when the streams
    close. Never raises.
    """

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._exited = False
        self._kqueue: Any = None
        self.available = False
        try:
            self._watch()
        except OSError:
            self.close()
            self.available = False

    def _watch(self) -> None:
        self.available = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")
        if not self.available and sys.platform == "darwin":  # pragma: no cover - macOS only
            self._kqueue = select.kqueue()
            event = select.kevent(
                self._pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                self._kqueue.control([event], 0, 0)
                self.available = True
            except ProcessLookupError:  # already a zombie
                self._exited = self.available = True

    def exited(self) -> bool:
        if self._exited or not self.available:
            return self._exited
        if self._kqueue is not None:  # pragma: no cover - macOS only
            self._exited = bool(self._kqueue.control(None, 1, 0))
            return self._exited
        try:
            result = os.waitid(os.P_PID, self._pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return False  # reaped by someone else: the exit cannot be seen; wait for timeout
        self._exited = result is not None
        return self._exited

    def close(self) -> None:
        if self._kqueue is not None:  # pragma: no cover - macOS only
            self._kqueue.close()
            self._kqueue = None


def _exchange(
    proc: subprocess.Popen[bytes],
    stdin: bytes,
    out: _Capture,
    err: _Capture,
    deadline: float,
    leader: _LeaderExit,
) -> bool:
    """Write ``stdin`` to ``proc`` and read its output until both output streams close, or
    until ``LEADER_EXIT_GRACE_S`` (but not past ``deadline``) after ``proc`` exited.

    Returns False if ``deadline`` passed while ``proc`` was still running. A program that exits
    (or closes its input) without reading all of ``stdin`` is not an error. Never reaps
    ``proc``, so its PID stays its group's id until the caller waits for it.
    """
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    captures = {proc.stdout.fileno(): out, proc.stderr.fileno(): err}
    view = memoryview(stdin)
    end = deadline
    exited = False
    with selectors.DefaultSelector() as selector:
        for fd in captures:
            selector.register(fd, selectors.EVENT_READ)
        stdin_fd = proc.stdin.fileno()
        if view:
            os.set_blocking(stdin_fd, False)
            selector.register(stdin_fd, selectors.EVENT_WRITE)
        else:
            proc.stdin.close()
        while selector.get_map():
            now = time.monotonic()
            if not exited and leader.exited():
                exited = True
                end = min(deadline, now + LEADER_EXIT_GRACE_S)
            remaining = end - now
            if remaining <= 0:
                return exited
            if not exited and leader.available:
                remaining = min(remaining, _EXIT_POLL_S)
            for key, _ in selector.select(remaining):
                if key.fd == stdin_fd:
                    try:
                        view = view[os.write(stdin_fd, view[:_CHUNK]) :]
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        view = view[:0]
                    if not view:
                        selector.unregister(stdin_fd)
                        proc.stdin.close()
                    continue
                chunk = os.read(key.fd, _CHUNK)
                if chunk:
                    captures[key.fd].add(chunk)
                else:
                    selector.unregister(key.fd)
    return True


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the process group ``run_group`` created for ``proc`` and ``proc`` itself
    (``hookwatch.kill_unreaped``), then reap ``proc``; raise ``GroupNotStopped`` if it is
    still not reaped after ``STOP_WAIT_S``."""
    kill_unreaped(proc)
    try:
        proc.wait(timeout=STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        raise GroupNotStopped(proc.pid) from None


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill and reap the one child ``run_bounded`` started (never anything else)."""
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _read_bounded(
    proc: subprocess.Popen[bytes], deadline: float, limit: int
) -> tuple[bytes, bytes, bool]:
    """Read ``proc``'s stdout and stderr until both close, keeping at most ``limit`` bytes of
    each. Stops early, with ``truncated`` set, once either has more than ``limit`` bytes;
    raises ``subprocess.TimeoutExpired`` at ``deadline``."""
    assert proc.stdout is not None and proc.stderr is not None
    buffers = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
    with selectors.DefaultSelector() as selector:
        for fd in buffers:
            selector.register(fd, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, 0)
            for key, _ in selector.select(remaining):
                buffer = buffers[key.fd]
                # One byte past the limit is enough to know the limit was exceeded.
                chunk = os.read(key.fd, limit + 1 - len(buffer))
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                buffer += chunk
                if len(buffer) > limit:
                    out, err = (bytes(b[:limit]) for b in buffers.values())
                    return out, err, True
    out, err = (bytes(b) for b in buffers.values())
    return out, err, False


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()
