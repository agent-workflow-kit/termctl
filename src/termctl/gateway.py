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
from datetime import UTC, datetime
from typing import Protocol


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


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()
