"""Gateway: the single boundary for subprocesses, the filesystem and the clock.

Every call to an external program goes through a ``Gateway`` so that tests can substitute
fakes (see design guide §14); time comes from an injectable ``Clock``. Commands are always
run with an argv list, never through a shell.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


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

    def real_uid(self) -> int:
        return os.getuid()

    def lstat(self, path: str) -> FileInfo:
        st = os.lstat(path)
        return FileInfo(mode=st.st_mode, uid=st.st_uid)

    def real_path(self, path: str) -> str:
        return os.path.realpath(path, strict=True)

    def read_link(self, path: str) -> str:
        return os.readlink(path)


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()
