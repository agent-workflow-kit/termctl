"""Test doubles for the gateway and the clock."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

# The tmux binary the fakes know about by default, found on ``PATH=/usr/bin``.
TMUX = "/usr/bin/tmux"

Response = subprocess.CompletedProcess[str] | BaseException


def completed(
    stdout: str = "", *, stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    """A scripted result of one ``run`` call."""
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeGateway:
    """Records every call and answers from a table of scripted results.

    An unscripted call fails the test, so a test states every external call it expects. A
    scripted exception is raised instead of returned. ``tmux -V`` of ``TMUX`` answers
    ``tmux 3.5a`` unless the test scripts it, and ``TMUX`` is the only executable file
    unless the test gives ``executables``.
    """

    def __init__(
        self,
        responses: Mapping[tuple[str, ...], Response] | None = None,
        *,
        cwd: str | None = "/",
        dirs: set[str] | None = None,
        tty: bool = False,
        user: str | None = "user",
        executables: set[str] | None = None,
    ) -> None:
        self.responses: dict[tuple[str, ...], Response] = {
            (TMUX, "-V"): completed("tmux 3.5a\n"),
            **(responses or {}),
        }
        self.executables = {TMUX} if executables is None else set(executables)
        self.executable_checks: list[str] = []
        self.calls: list[list[str]] = []
        self.cwd = cwd
        # By default only the caller's current directory exists.
        if dirs is None:
            dirs = {cwd} if cwd is not None else set()
        self.dirs = set(dirs)
        self.tty = tty
        self.user = user
        self.dir_checks: list[str] = []

    def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        try:
            response = self.responses[tuple(argv)]
        except KeyError:
            raise AssertionError(f"unexpected gateway call: {argv!r}") from None
        if isinstance(response, BaseException):
            raise response
        return response

    def current_dir(self) -> str:
        if self.cwd is None:
            raise FileNotFoundError("current directory removed")
        return self.cwd

    def is_dir(self, path: str) -> bool:
        self.dir_checks.append(path)
        return path in self.dirs

    def is_executable_file(self, path: str) -> bool:
        self.executable_checks.append(path)
        return path in self.executables

    def stdin_isatty(self) -> bool:
        return self.tty

    def user_name(self) -> str | None:
        return self.user


class FakeClock:
    """A clock that only moves when the test advances it."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._monotonic = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds
