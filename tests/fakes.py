"""Test doubles for the gateway and the clock."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta


class FakeGateway:
    """Records every call and answers from a table of scripted results.

    An unscripted call fails the test, so a test states every external call it expects.
    """

    def __init__(
        self, responses: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None
    ) -> None:
        self.responses = dict(responses or {})
        self.calls: list[list[str]] = []

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
