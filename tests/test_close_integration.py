"""`close` end to end against real tmux on a private server (``tmux_server`` in conftest.py):
spike D5 (``kill-pane`` only) and F3 (the kernel's SIGHUP, then SIGTERM and SIGKILL)
regressions, and forged PID options that must never get a process signaled.

Every process a test signals or checks is one it started: through ``termctl open`` on its
private server, or with ``subprocess``. A process left running is stopped by its exact PID
(verified first) or its ``Popen`` handle; the server by ``kill-server`` on its socket.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from termctl import procs
from termctl.gateway import SystemGateway

from .conftest import TmuxServer, wait_for
from .procs import is_gone, wait_gone
from .schemas import validate
from .test_cli_open_integration import fmt, opened, pane_exists, termctl

pytestmark = [
    pytest.mark.tmux,
    pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="reads /proc (Linux)"),
]

HUMAN = ("--actor", "human:tester")


def closed(server: TmuxServer, *argv: str) -> dict[str, object]:
    result = termctl(server, "--json", "close", *argv)
    assert result.code == 0, result.stdout + result.stderr
    data = result.json()
    validate(data, "termctl/cli/close.json")
    return data


def recorded_pid(server: TmuxServer, pane: str) -> int:
    pid = fmt(server, pane, "#{@termctl_pid}")
    assert pid, "open recorded no PID"
    return int(pid)


@contextmanager
def stop_if_left(pid: int, start: str) -> Iterator[None]:
    """After the test, SIGKILL ``pid`` only if it is still the process this test started
    (same start time), so a failing test leaves nothing behind and never hits another."""
    try:
        yield
    finally:
        try:
            process = procs.read(SystemGateway(), pid)
        except procs.ProcessError:
            process = None
        if process is not None and not process.zombie and process.start == start:
            os.kill(pid, signal.SIGKILL)


def start_of(pid: int) -> str:
    process = procs.read(SystemGateway(), pid)
    assert process is not None
    return process.start


def test_d5_close_kills_only_the_managed_pane_of_a_split_window(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    out = opened(tmux_server, "--", "sleep", "600")
    split = tmux_server.raw(
        "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", out["pane_id"],
        "--", "/usr/bin/env", "--", "sleep", "600",
    )  # fmt: skip
    assert split.returncode == 0, split.stderr
    user_pane = split.stdout.strip()
    data = closed(tmux_server, out["id"], "--force", *HUMAN, "--grace", "0s")
    assert (data["pane_closed"], data["was_running"]) == (True, True)
    assert wait_for(lambda: not pane_exists(tmux_server, out["pane_id"]))
    # The user's pane and its window survive.
    assert pane_exists(tmux_server, user_pane)
    assert fmt(tmux_server, user_pane, "#{window_id}") == out["window_id"]


def test_a_running_pane_is_refused_without_force(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    out = opened(tmux_server, "--", "sleep", "600")
    result = termctl(tmux_server, "--json", "close", out["id"])
    assert result.code == 20
    assert result.json()["error"]["reason"] == "running"
    assert pane_exists(tmux_server, out["pane_id"])


def test_a_dead_pane_closes_without_force(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    out = opened(tmux_server, "--keep-on-exit", "always", "--", "true")
    assert wait_for(lambda: fmt(tmux_server, out["pane_id"], "#{pane_dead}") == "1")
    data = closed(tmux_server, out["id"])
    assert (data["was_running"], data["signals_sent"], data["exited"]) == (False, [], True)
    assert not pane_exists(tmux_server, out["pane_id"])
    assert termctl(tmux_server, "exists", out["id"]).code == 10


def test_f3_the_kernel_sighup_ends_an_ordinary_command(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    out = opened(tmux_server, "--", "sleep", "600")
    pid = recorded_pid(tmux_server, out["pane_id"])
    with stop_if_left(pid, start_of(pid)):
        data = closed(tmux_server, out["id"], "--force", *HUMAN)
        assert (data["signals_sent"], data["exited"], data["pid_mismatch"]) == ([], True, False)
        assert is_gone(pid)


def test_f3_sigterm_after_the_grace_for_a_command_that_ignores_sighup(
    tmux_server: TmuxServer,
) -> None:
    tmux_server.start()
    out = opened(tmux_server, "--", "sh", "-c", "trap '' HUP; exec sleep 600")
    pid = recorded_pid(tmux_server, out["pane_id"])
    with stop_if_left(pid, start_of(pid)):
        data = closed(tmux_server, out["id"], "--force", *HUMAN, "--grace", "300ms")
        assert (data["signals_sent"], data["exited"]) == (["SIGTERM"], True)
        assert data["duration_ms"] >= 300  # type: ignore[operator]
        assert wait_gone(pid)


def test_f3_sigkill_5s_after_sigterm_for_a_command_that_ignores_both(
    tmux_server: TmuxServer,
) -> None:
    tmux_server.start()
    out = opened(tmux_server, "--", "sh", "-c", "trap '' HUP TERM; exec sleep 600")
    pid = recorded_pid(tmux_server, out["pane_id"])
    with stop_if_left(pid, start_of(pid)):
        data = closed(tmux_server, out["id"], "--force", *HUMAN, "--grace", "0s")
        assert (data["signals_sent"], data["exited"]) == (["SIGTERM", "SIGKILL"], True)
        assert data["duration_ms"] >= 5000  # type: ignore[operator]
        assert wait_gone(pid)


def test_a_forged_pid_naming_another_process_is_never_signaled(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    out = opened(tmux_server, "--", "sleep", "600")
    pane = out["pane_id"]
    victim = subprocess.Popen(["/bin/sleep", "60"])
    try:
        # Forge the options so they name a process of ours, with its true start time.
        for option, value in (
            ("@termctl_pid", str(victim.pid)),
            ("@termctl_pid_start", start_of(victim.pid)),
        ):
            assert tmux_server.raw("set", "-p", "-t", pane, option, value).returncode == 0
        data = closed(tmux_server, out["id"], "--force", *HUMAN, "--grace", "0s")
        assert (data["pid_mismatch"], data["signals_sent"], data["exited"]) == (True, [], None)
        assert victim.poll() is None, "a process that is not the pane's was signaled"
    finally:
        victim.kill()
        victim.wait()


def test_a_forged_start_time_gets_the_pane_process_no_signal(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    out = opened(tmux_server, "--", "sh", "-c", "trap '' HUP; exec sleep 600")
    pane = out["pane_id"]
    pid = recorded_pid(tmux_server, pane)
    with stop_if_left(pid, start_of(pid)):
        assert tmux_server.raw("set", "-p", "-t", pane, "@termctl_pid_start", "1").returncode == 0
        data = closed(tmux_server, out["id"], "--force", *HUMAN, "--grace", "0s")
        assert (data["pid_mismatch"], data["signals_sent"]) == (True, [])
        # It ignores the kernel's SIGHUP, and termctl sent nothing.
        assert not is_gone(pid)
        assert not pane_exists(tmux_server, pane)


def test_dry_run_changes_nothing(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    out = opened(tmux_server, "--", "sleep", "600")
    result = termctl(tmux_server, "--json", "close", out["id"], "--force", *HUMAN, "--dry-run")
    assert result.code == 0, result.stderr
    data = result.json()
    validate(data, "termctl/cli/close-dry-run.json")
    assert (data["signals_possible"], data["pid_mismatch"]) == (["SIGTERM", "SIGKILL"], False)
    assert data["pid"] == recorded_pid(tmux_server, out["pane_id"])
    assert pane_exists(tmux_server, out["pane_id"])


@pytest.mark.parametrize("command", [("sleep", "600"), ("sh", "-c", "trap '' HUP; exec sleep 600")])
def test_closing_the_last_pane_of_the_server_ends_it_and_still_closes(
    tmux_server: TmuxServer, command: tuple[str, ...]
) -> None:
    out = opened(tmux_server, "--", *command)  # the server's only pane
    pid = recorded_pid(tmux_server, out["pane_id"])
    with stop_if_left(pid, start_of(pid)):
        data = closed(tmux_server, out["id"], "--force", *HUMAN, "--grace", "200ms")
        assert (data["pane_closed"], data["exited"], data["pid_mismatch"]) == (True, True, False)
        assert wait_for(lambda: not tmux_server.alive())
        assert wait_gone(pid)
