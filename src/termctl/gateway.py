"""Gateway: the single boundary for subprocesses, the filesystem and the clock.

Every call to an external program goes through a ``Gateway`` so that tests can substitute
fakes (see design guide §14); time comes from an injectable ``Clock``. Commands are always
run with an argv list; the gateway never adds a shell (a hook's ``sh -c`` is in its argv).
"""

from __future__ import annotations

import os
import pwd
import selectors
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import NamedTuple, Protocol

_CHUNK = 65536


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


class Gateway(Protocol):
    """External effects termctl needs. Later stages add `/proc`, `stat` and tty writes here."""

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

        The run ends when the program has exited and closed both output streams. If that has
        not happened within ``timeout`` seconds, the gateway sends SIGKILL to the group it
        created (and to nothing else) and returns ``returncode=None``. Raises ``OSError`` if
        the program cannot be started.
        """
        ...

    def spawn_group(self, argv: list[str], *, stdin: bytes, env: Mapping[str, str]) -> int:
        """Start ``argv`` detached in a new process group with exactly ``env``, ``stdin`` as
        its input and no output; return its process group id without waiting for it. Raises
        ``OSError`` if it cannot be started."""
        ...

    def current_dir(self) -> str:
        """The caller's current directory; raises ``OSError`` if it cannot be read."""
        ...

    def is_dir(self, path: str) -> bool:
        """Whether ``path`` exists and is a directory (symlinks followed)."""
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
        """Run ``argv`` without a shell and capture its text output.

        ``env`` replaces the whole environment when given; ``None`` inherits termctl's own.
        """
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=None if env is None else dict(env),
            check=False,
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
        deadline = time.monotonic() + timeout
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            process_group=0,  # setpgid(0, 0) in the child: the group id is proc.pid
        )
        out = _Capture(stdout_limit)
        err = _Capture(stderr_limit)
        try:
            finished = _exchange(proc, stdin, out, err, deadline)
            if finished:
                try:
                    proc.wait(timeout=max(deadline - time.monotonic(), 0))
                except subprocess.TimeoutExpired:
                    finished = False
            if not finished:
                _kill_group(proc)
        except BaseException:
            _kill_group(proc)
            raise
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        return GroupRun(
            pgid=proc.pid,
            returncode=proc.returncode if finished else None,
            stdout=bytes(out.data),
            stderr=bytes(err.data),
            stdout_truncated=out.truncated,
            stderr_truncated=err.truncated,
        )

    def spawn_group(self, argv: list[str], *, stdin: bytes, env: Mapping[str, str]) -> int:
        # A file, not a pipe, holds the input, so writing it never blocks on a program that
        # reads slowly or not at all. It is unlinked; only the program's descriptor keeps it.
        with tempfile.TemporaryFile() as data:
            data.write(stdin)
            data.seek(0)
            proc = subprocess.Popen(
                argv,
                stdin=data,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(env),
                process_group=0,
            )
        return proc.pid

    def current_dir(self) -> str:
        return os.getcwd()

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

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


def _exchange(
    proc: subprocess.Popen[bytes], stdin: bytes, out: _Capture, err: _Capture, deadline: float
) -> bool:
    """Write ``stdin`` to ``proc`` and read its output until both output streams close.

    Returns False if ``deadline`` passed first. A program that exits (or closes its input)
    without reading all of ``stdin`` is not an error. Never reaps ``proc``, so its PID stays
    its group's id until the caller waits for it.
    """
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    captures = {proc.stdout.fileno(): out, proc.stderr.fileno(): err}
    view = memoryview(stdin)
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
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


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the process group ``run_group`` created for ``proc``, then reap ``proc``.

    The group is signaled only while ``proc`` is unreaped: until then its PID, which is the
    group's id, cannot be reused, so the signal reaches only that group.
    """
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            proc.kill()
    proc.wait()


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()
