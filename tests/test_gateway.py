from __future__ import annotations

import sys
from datetime import UTC

from termctl.gateway import Clock, Gateway, SystemClock, SystemGateway

from .fakes import FakeClock, FakeGateway


def test_system_gateway_runs_argv_without_a_shell() -> None:
    result = SystemGateway().run([sys.executable, "-c", "import sys; print(sys.argv[1])", "$HOME;"])
    assert (result.returncode, result.stdout) == (0, "$HOME;\n")


def test_system_gateway_passes_stdin_and_env() -> None:
    script = "import os, sys; print(sys.stdin.read() + os.environ['X'])"
    result = SystemGateway().run([sys.executable, "-c", script], stdin="a", env={"X": "b"})
    assert result.stdout == "ab\n"


def test_system_clock_is_timezone_aware_and_monotonic() -> None:
    clock = SystemClock()
    assert clock.now().tzinfo is UTC
    first = clock.monotonic()
    assert clock.monotonic() >= first


def test_fakes_satisfy_the_protocols() -> None:
    gateway: Gateway = FakeGateway()
    clock: Clock = FakeClock()
    assert gateway is not None and clock is not None


def test_fake_clock_advances() -> None:
    clock = FakeClock()
    start, mono = clock.now(), clock.monotonic()
    clock.advance(2.5)
    assert (clock.now() - start).total_seconds() == 2.5
    assert clock.monotonic() - mono == 2.5
