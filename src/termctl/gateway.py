"""Gateway: the single boundary for subprocesses, the filesystem and the clock.

Every call to an external program goes through a ``Gateway`` so that tests can substitute
fakes (see design guide §14); time comes from an injectable ``Clock``. Commands are always
run with an argv list, never through a shell.
"""

from __future__ import annotations

import os
import pwd
import selectors
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NamedTuple, Protocol


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
