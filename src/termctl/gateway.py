"""Gateway: the single boundary for subprocesses, the filesystem and the clock.

Every call to an external program goes through a ``Gateway`` so that tests can substitute
fakes (see design guide §14); time comes from an injectable ``Clock``. Commands are always
run with an argv list; the gateway never adds a shell (a hook's ``sh -c`` is in its argv).
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import pwd
import re
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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import FrameType
from typing import Any, NamedTuple, Protocol

from . import hookwatch
from .hookwatch import kill_unreaped

# How long connecting to a Unix socket may block (a listener with a full backlog).
SOCKET_TIMEOUT = 10.0


class BoundedOutput(NamedTuple):
    """The result of ``Gateway.run_bounded``: at most ``limit`` bytes of each stream, and
    whether either stream had more. When output was truncated the child is killed if still
    running, so ``returncode`` is either its own exit status or the negative kill signal;
    callers must not rely on it when ``truncated`` is true."""

    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool


@dataclass(frozen=True)
class FileInfo:
    """What termctl reads from ``lstat``: the file type and permission bits, and the owner."""

    mode: int
    uid: int


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
    """termctl got SIGTERM or SIGHUP (``_hook_signals``, ``stop_signals``); a hook that was
    running had its group killed first. termctl exits ``128 + signum``."""

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


class ProcessHandle(Protocol):
    """A handle on one process, from ``Gateway.open_process``: on Linux a pidfd, which keeps
    naming that process even if it exits and its PID is reused; on macOS only the PID, so the
    caller checks the start time immediately before each signal (``procs.signal_sequence``)."""

    def exited(self) -> bool:
        """Whether the process is known to have exited (a zombie included). False when the
        handle cannot tell (macOS); the caller also reads the process."""
        ...

    def send_signal(self, signum: int) -> bool:
        """Send ``signum`` to the process and nothing else, never a group; False when it is
        gone (``ESRCH``). Raises ``OSError`` for any other failure (``EPERM``)."""
        ...

    def close(self) -> None:
        """Release the handle; idempotent."""
        ...


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

    def run_group(
        self,
        argv: list[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
        record: Callable[[GroupRun], None] | None = None,
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
        SIGINT raises ``KeyboardInterrupt``; the group is killed the same way first, and the
        exception carries its group (``started_pgid``). ``record``, if given, is called with
        the result before it is returned, with stop signals held, so a stop that comes after
        the program ended is raised only once the caller has recorded the run (and then
        carries no group; without ``record`` it carries the group). Raises ``OSError`` or
        ``ValueError`` if the program cannot be started and ``GroupNotStopped`` if it was to
        be killed but could not be (a stop that came meanwhile is raised instead, carrying
        the group).
        """
        ...

    def spawn_watched(
        self,
        argv: list[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        timeout: float,
        record: Callable[[int], None] | None = None,
    ) -> int:
        """Start ``argv`` detached under a watchdog (``hookwatch.py``, run by its file path) in a
        new session,
        with exactly ``env``, ``stdin`` as its input and no output, and return the watchdog's
        PID (also its process group and session id) without waiting. The watchdog runs
        ``argv`` in a process group of its own and kills that group after ``timeout`` seconds.
        Stop signals are held while it starts and while ``record`` (if given) is called with
        the PID; a stop that came meanwhile is raised afterwards, carrying the PID
        (``started_pgid``) unless ``record`` took it. The watchdog is not stopped then.
        Raises ``OSError`` or ``ValueError`` if the watchdog cannot be started."""
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

    def stdin_isatty(self) -> bool:
        """Whether stdin (fd 0) is a terminal."""
        ...

    def user_name(self) -> str | None:
        """The real uid's user name from the password database, or None if it has none."""
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

    def open_process(self, pid: int) -> ProcessHandle | None:
        """A handle on process ``pid`` (``close --force``), or None when no such process
        exists. Linux: ``pidfd_open``; raises ``OSError`` when the kernel or Python has no
        pidfd (the caller then sends no signal). macOS: a handle on the PID alone."""
        ...

    def proc_is_own_namespace(self) -> bool:
        """Linux: whether ``/proc`` shows termctl's own PID namespace (``/proc/self`` names
        termctl's PID), so ``/proc/<pid>`` is the process a pidfd or ``SO_PEERCRED`` names.
        Raises ``OSError`` if ``/proc/self`` cannot be read."""
        ...


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware (UTC)."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards, for timeouts and durations."""
        ...

    def sleep(self, seconds: float) -> None:
        """Wait ``seconds`` (the waits of ``close --force``)."""
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

        The program runs in a process group of its own, so a signal sent to termctl's group
        or by its terminal (``timeout``, ``kill -TERM -<pgid>``, a hangup) reaches termctl
        only: a tmux client killed halfway through a command chain exits 0 with the rest of
        the chain dropped. Being in a background group, it must never use the terminal (that
        would stop it with SIGTTIN or SIGTTOU), so its stdin is ``/dev/null`` (or the
        ``stdin`` pipe) and its output is captured: the tmux commands and ``ps`` run here
        never attach a client or otherwise need a terminal.
        """
        if stdin is None:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                env=None if env is None else dict(env),
                check=False,
                process_group=0,
            )
        else:
            completed = subprocess.run(
                argv,
                input=stdin.encode("utf-8", "surrogateescape"),
                capture_output=True,
                timeout=timeout,
                env=None if env is None else dict(env),
                check=False,
                process_group=0,
            )
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            completed.stdout.decode("utf-8", "surrogateescape"),
            completed.stderr.decode("utf-8", "surrogateescape"),
        )

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

    def run_group(
        self,
        argv: list[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
        record: Callable[[GroupRun], None] | None = None,
    ) -> GroupRun:
        with _hook_signals() as stops:
            try:
                # Returns with stop signals held (``_run_group``).
                run = _run_group(argv, stdin, env, timeout, stdout_limit, stderr_limit, stops)
            except GroupNotStopped as exc:
                _release_with_pgid(stops, exc.pgid)
                raise
            try:
                if record is not None:
                    record(run)
            except BaseException:
                stops.release()
                raise
            _release_with_pgid(stops, None if record is not None else run.pgid)
            return run

    def spawn_watched(
        self,
        argv: list[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        timeout: float,
        record: Callable[[int], None] | None = None,
    ) -> int:
        with _hook_signals() as stops:
            # A stop signal before proc is assigned would leave a watchdog nobody lists.
            stops.hold()
            try:
                # A file, not a pipe, holds the input, so writing it never blocks on a program
                # that reads slowly or not at all. It is unlinked; only the watchdog's
                # descriptor (which it hands to the hook unread) keeps it.
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
            except BaseException:
                stops.release()  # a stop signal that came meanwhile is what the caller must see
                raise
            try:
                if record is not None:
                    record(proc.pid)
            except BaseException:
                stops.release()
                raise
            _release_with_pgid(stops, None if record is not None else proc.pid)
            return proc.pid

    def current_dir(self) -> str:
        return os.getcwd()

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def is_executable_file(self, path: str) -> bool:
        try:
            return os.path.isfile(path) and os.access(path, os.X_OK)
        except ValueError:  # an embedded NUL
            return False

    def stdin_isatty(self) -> bool:
        return os.isatty(0)

    def user_name(self) -> str | None:
        # The password database, not $USER or $LOGNAME, which the caller's environment can set.
        try:
            return pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            return None

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

    def open_process(self, pid: int) -> ProcessHandle | None:
        if sys.platform == "darwin":  # pragma: no cover - macOS only
            return _PidHandle(pid)
        try:
            fd = _pidfd_open(pid)
        except ProcessLookupError:
            return None
        return _PidfdHandle(fd)

    def proc_is_own_namespace(self) -> bool:
        return os.readlink("/proc/self") == str(os.getpid())


# The Linux system call numbers of pidfd_send_signal (5.1) and pidfd_open (5.3) on the
# architectures that use the unified numbering for calls added since 5.1. Others differ: alpha
# (534, 544), ia64 (1448, 1458) and MIPS (4424/4434 o32, 5424/5434 n64, 6424/6434 n32), and
# x32 needs __X32_SYSCALL_BIT; there the raw calls are refused (ENOSYS).
_SYS_PIDFD_SEND_SIGNAL = 424
_SYS_PIDFD_OPEN = 434
# platform.machine() of the architectures known to use those numbers.
_UNIFIED_SYSCALL_MACHINE = re.compile(r"x86_64|i[3-6]86|aarch64|arm.*|riscv64|ppc64(le)?|s390x")
# The largest PID (and pidfd_open's pid_t argument): a C int.
_PID_T_MAX = 2**31 - 1


def _syscall_numbers_known() -> bool:
    """Whether ``_SYS_PIDFD_*`` are this machine's numbers: Linux on an allowlisted
    architecture, and on x86_64 the 64-bit ABI (not x32, whose pointers are 4 bytes)."""
    if sys.platform != "linux":
        return False
    machine = platform.machine()
    if _UNIFIED_SYSCALL_MACHINE.fullmatch(machine) is None:
        return False
    return machine != "x86_64" or ctypes.sizeof(ctypes.c_void_p) == 8


def _syscall(number: int, *args: ctypes.c_int | ctypes.c_uint | None) -> int:
    """A raw Linux system call through libc's ``syscall``, for Python builds whose ``os`` lacks
    the pidfd functions (some standalone builds); raises ``OSError`` with its errno, and
    ``ENOSYS`` where the system call numbers are not known (``_syscall_numbers_known``)."""
    if not _syscall_numbers_known():
        raise OSError(errno.ENOSYS, "pidfd system calls are not known on this architecture")
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(ctypes.c_long(number), *args)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return int(result)


def _pidfd_open(pid: int) -> int:
    """``pidfd_open(pid, 0)``; ``ProcessLookupError`` when no such process exists (a PID
    outside 1 to 2**31-1 never does, and is never passed on), another ``OSError`` (``ENOSYS``
    before Linux 5.3) when no handle can be opened."""
    if not 1 <= pid <= _PID_T_MAX:
        raise ProcessLookupError(errno.ESRCH, f"no process can have PID {pid}")
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    try:
        fd = _syscall(_SYS_PIDFD_OPEN, ctypes.c_int(pid), ctypes.c_uint(0))
    except OSError as err:
        if err.errno == errno.ESRCH:
            raise ProcessLookupError(err.errno, err.strerror) from None
        raise
    os.set_inheritable(fd, False)  # pidfd_open sets O_CLOEXEC itself; made sure here
    return fd


def _pidfd_send_signal(fd: int, signum: int) -> None:
    """``pidfd_send_signal(fd, signum, NULL, 0)``; ``ProcessLookupError`` when its process
    has exited."""
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, signum)
        return
    try:
        _syscall(
            _SYS_PIDFD_SEND_SIGNAL, ctypes.c_int(fd), ctypes.c_int(signum), None, ctypes.c_uint(0)
        )
    except OSError as err:
        if err.errno == errno.ESRCH:
            raise ProcessLookupError(err.errno, err.strerror) from None
        raise


class _PidfdHandle:
    """A Linux pidfd: signals reach the process it was opened on, or nobody (``ESRCH``)."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def exited(self) -> bool:
        if self._fd is None:
            raise ValueError("the process handle is closed")
        # A pidfd polls readable once its process has exited (a zombie included).
        poller = select.poll()
        poller.register(self._fd, select.POLLIN)
        return bool(poller.poll(0))

    def send_signal(self, signum: int) -> bool:
        if self._fd is None:
            raise ValueError("the process handle is closed")
        try:
            _pidfd_send_signal(self._fd, signum)
        except ProcessLookupError:
            return False
        return True

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)


# TODO(#26): macOS signals a bare PID. The start time is checked through ps (one-second
# resolution) just before os.kill, so a PID reused between that check and the kill could be
# signaled (docs/design.md "Implementation notes", interface.md "Residual risks"); #26 owns the
# macOS signaling path and its verification.
class _PidHandle:  # pragma: no cover - macOS only
    """macOS: the PID alone. The caller checks the start time just before each signal."""

    def __init__(self, pid: int) -> None:
        self._pid = pid

    def exited(self) -> bool:
        return False

    def send_signal(self, signum: int) -> bool:
        try:
            os.kill(self._pid, signum)
        except ProcessLookupError:
            return False
        return True

    def close(self) -> None:
        pass


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


def _stop_exception(signum: int) -> BaseException:
    """What a stop signal raises: ``KeyboardInterrupt`` for SIGINT, else ``Terminated``."""
    if signum == signal.SIGINT:
        return KeyboardInterrupt()
    return Terminated(signum)


# The attribute run_group sets on an exception that interrupted a program it had started.
_PGID_ATTRIBUTE = "termctl_pgid"


def with_pgid[E: BaseException](exc: E, pgid: int) -> E:
    """``exc``, marked as having interrupted ``run_group`` after it created group ``pgid``."""
    setattr(exc, _PGID_ATTRIBUTE, pgid)
    return exc


def started_pgid(exc: BaseException) -> int | None:
    """The process group ``run_group`` had created (and killed) when ``exc`` interrupted it, so
    that a caller can list the interrupted program; None if it had started none."""
    value = getattr(exc, _PGID_ATTRIBUTE, None)
    return value if isinstance(value, int) else None


class _StopSignals:
    """A handler for stop signals: the first signal raises ``Terminated`` (``KeyboardInterrupt``
    for SIGINT), unless it is held, in which case ``release`` raises it.

    ``_hook_signals`` installs one for SIGTERM and SIGHUP while a hook runs, held while the hook
    starts and from its end until the caller recorded it, so the signal cannot come between
    the fork and the moment the caller has the process to kill, nor lose a hook that ended.
    ``stop_signals`` installs one for a whole run; a hook's handler has the run's as its
    ``parent``: holding it holds the parent too (SIGINT stays with the run's handler), and
    only the first signal of either raises. A signal in ``ignored`` (the run's, for a hook's
    handler) does nothing at all (``RunSignals.ignore_hangups``). Not blocked with
    ``pthread_sigmask``: the hook (or tmux and the pane) would inherit the blocked mask.
    """

    def __init__(self, parent: _StopSignals | None = None) -> None:
        self._parent = parent
        self.ignored: set[int] = set()
        self._raised = False
        self._held = False
        self._holding_parent = False
        self._pending: int | None = None

    def _done(self) -> bool:
        return self._raised or (self._parent is not None and self._parent._raised)

    def _ignores(self, signum: int) -> bool:
        return signum in self.ignored or (
            self._parent is not None and signum in self._parent.ignored
        )

    def handle(self, signum: int, frame: FrameType | None) -> None:
        # Only the first signal raises, so a second one cannot interrupt the cleanup; the run's
        # handler, restored after a hook, must not raise again either (review round 2, R2-3).
        if self._done() or self._ignores(signum):
            return
        self._raised = True
        if self._parent is not None:
            self._parent._raised = True
        if self._held:
            self._pending = signum
            return
        raise _stop_exception(signum)

    def hold(self) -> None:
        self._held = True
        parent = self._parent
        if parent is not None and not parent._held and not self._holding_parent:
            parent._held = self._holding_parent = True

    def release(self) -> None:
        """Stop holding (and the parent, if this held it); raise the stop of a signal that
        came while held. At most one of the two has one."""
        self._held = False
        if self._holding_parent:
            assert self._parent is not None
            self._holding_parent = False
            self._parent.release()
        if self._pending is not None:
            signum, self._pending = self._pending, None
            raise _stop_exception(signum)

    def ignore_later(self) -> None:
        """Stop holding, drop a signal that came while held and ignore any later one."""
        self._raised = True
        self._held, self._pending = False, None


def _is_stop_handler(handler: Any) -> bool:
    """Whether ``handler`` is a ``_StopSignals`` handler (``stop_signals``'), which a hook's
    own handler may replace for as long as the hook runs."""
    return isinstance(getattr(handler, "__self__", None), _StopSignals)


def _run_stops() -> _StopSignals | None:
    """The run's handler (``stop_signals``), if one handles a stop signal now."""
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        owner = getattr(signal.getsignal(signum), "__self__", None)
        if isinstance(owner, _StopSignals):
            return owner
    return None


class RunSignals:
    """The stop-signal handling of a whole run (``stop_signals``)."""

    def __init__(self, stops: _StopSignals) -> None:
        self._stops = stops

    @contextmanager
    def deferred(self) -> Iterator[None]:
        """Within the block, a stop signal is only recorded; when the block ends normally its
        stop (``KeyboardInterrupt`` or ``Terminated``) is raised. A block that ends with an
        exception ends the run anyway: that exception is what the caller sees, and later stop
        signals are ignored, as after ``finish``."""
        self._stops.hold()
        try:
            yield
        except BaseException:
            self._stops.ignore_later()
            raise
        self._stops.release()

    def finish(self) -> None:
        """The run is ending (its last record is being written): ignore later stop signals."""
        self._stops.ignore_later()

    def ignore_hangups(self) -> None:
        """From now on SIGHUP does nothing, a hook's handler included (``close`` from its
        re-check on: closing the pane termctl runs in hangs up its terminal, and the run must
        still complete and write its records). It stays handled, not ``SIG_IGN``, so the
        programs termctl runs get it at its default."""
        self._stops.ignored.add(signal.SIGHUP)


@contextmanager
def stop_signals() -> Iterator[RunSignals]:
    """For a whole run: SIGINT (when Python's own handler is installed), SIGTERM and SIGHUP
    (unless ignored or handled already) raise ``KeyboardInterrupt`` or ``Terminated`` through a
    handler that ``RunSignals.deferred`` can hold, and that a running hook's handler replaces
    for as long as the hook runs (``_hook_signals``). The previous handlers are restored
    afterwards."""
    stops = _StopSignals()
    if threading.current_thread() is not threading.main_thread():
        yield RunSignals(stops)
        return
    previous: dict[int, Any] = {}
    defaults = {
        signal.SIGINT: signal.default_int_handler,
        signal.SIGTERM: signal.SIG_DFL,
        signal.SIGHUP: signal.SIG_DFL,
    }
    try:
        for signum, default in defaults.items():
            if signal.getsignal(signum) == default:
                previous[signum] = signal.signal(signum, stops.handle)
        yield RunSignals(stops)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def _hook_signals() -> Iterator[_StopSignals]:
    """While a hook runs: ``SIGCHLD`` at its default, and ``SIGTERM`` and ``SIGHUP`` (unless
    ignored or handled already, other than by ``stop_signals``) raise ``Terminated`` (see
    ``_StopSignals``), so the hook's group is killed before termctl exits. The run's handler
    (``stop_signals``), if any, is its parent: holding holds both, and once either has raised
    neither raises again. The previous handlers are restored afterwards."""
    stops = _StopSignals()
    if threading.current_thread() is not threading.main_thread():
        yield stops
        return
    _default_sigchld()
    stops = _StopSignals(parent=_run_stops())
    previous: dict[int, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGHUP):
        current = signal.getsignal(signum)
        if current == signal.SIG_DFL or _is_stop_handler(current):
            previous[signum] = signal.signal(signum, stops.handle)
    try:
        yield stops
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _release_with_pgid(stops: _StopSignals, pgid: int | None) -> None:
    """``stops.release()``; the stop it raises carries ``pgid`` when given (``with_pgid``)."""
    try:
        stops.release()
    except BaseException as exc:
        if pgid is not None:
            with_pgid(exc, pgid)
        raise


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
        # Held from here until the caller recorded the run (``SystemGateway.run_group``).
        stops.hold()
    except BaseException as exc:
        try:
            _stop(proc)
        except GroupNotStopped:
            pass  # the interruption is what the caller must see
        with_pgid(exc, proc.pid)
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


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(seconds, 0.0))
