"""Test doubles for the gateway and the clock."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from termctl.gateway import GroupRun


@dataclass
class GroupCall:
    """One ``run_group`` or ``spawn_group`` call as the fake saw it."""

    argv: list[str]
    stdin: bytes
    env: dict[str, str]
    timeout: float | None
    stdout_limit: int | None
    stderr_limit: int | None
    spawned: bool


# A scripted group run: a fixed result, an exception to raise, or a function of the call.
GroupResponse = GroupRun | OSError | Callable[[GroupCall], GroupRun]


def group_run(
    returncode: int | None = 0, stdout: bytes = b"", stderr: bytes = b"", pgid: int = 4242
) -> GroupRun:
    return GroupRun(pgid, returncode, stdout, stderr, False, False)


class FakeGateway:
    """Records every call and answers from a table of scripted results.

    An unscripted call fails the test, so a test states every external call it expects.
    """

    def __init__(
        self,
        responses: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
        *,
        cwd: str | None = "/",
        dirs: set[str] | None = None,
        tty: bool = False,
        user: str | None = "user",
    ) -> None:
        self.responses = dict(responses or {})
        self.calls: list[list[str]] = []
        self.cwd = cwd
        # By default only the caller's current directory exists.
        if dirs is None:
            dirs = {cwd} if cwd is not None else set()
        self.dirs = set(dirs)
        self.tty = tty
        self.user = user
        self.dir_checks: list[str] = []
        # run_group and spawn_group answers, keyed by argv.
        self.group_responses: dict[tuple[str, ...], GroupResponse] = {}
        self.group_calls: list[GroupCall] = []

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
            return self.responses[tuple(argv)]
        except KeyError:
            raise AssertionError(f"unexpected gateway call: {argv!r}") from None

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
        call = GroupCall(list(argv), stdin, dict(env), timeout, stdout_limit, stderr_limit, False)
        self.group_calls.append(call)
        response = self._group_response(argv)
        if isinstance(response, OSError):
            raise response
        if isinstance(response, GroupRun):
            return response
        return response(call)

    def spawn_group(self, argv: list[str], *, stdin: bytes, env: Mapping[str, str]) -> int:
        self.group_calls.append(GroupCall(list(argv), stdin, dict(env), None, None, None, True))
        response = self._group_response(argv)
        if isinstance(response, OSError):
            raise response
        return 4343

    def _group_response(self, argv: list[str]) -> GroupResponse:
        try:
            return self.group_responses[tuple(argv)]
        except KeyError:
            raise AssertionError(f"unexpected gateway group call: {argv!r}") from None

    def current_dir(self) -> str:
        if self.cwd is None:
            raise FileNotFoundError("current directory removed")
        return self.cwd

    def is_dir(self, path: str) -> bool:
        self.dir_checks.append(path)
        return path in self.dirs

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
