"""``close`` through ``main`` against a fake tmux, fake processes and a fake clock
(interface.md `close`, "Liveness", "Hooks", "Audit and log"; plan stage 4). Real tmux runs
are in test_close_integration.py."""

from __future__ import annotations

import io
import json
import os
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from termctl import tmux_backend as tb
from termctl.cli import main
from termctl.gateway import GroupRun, Terminated, with_pgid

from .conftest import private_environ
from .fakes import (
    TMUX,
    FakeClock,
    FakeGateway,
    FakeProcess,
    GroupCall,
    GroupResponse,
    completed,
    group_run,
    is_list_panes,
    pane_row,
)
from .schemas import validate
from .test_cli import ENVIRON, GOLDEN

ID = "tc-abcdefgh"
SOCK = "/tmp/tmux-1000/default"
SERVER = 77
DEAD = {"pane_dead": "1", "pane_dead_status": "0"}
HUMAN = ["--actor", "human:me"]


def key(invocation: tb.Invocation) -> tuple[str, ...]:
    return tuple(invocation.argv(TMUX, SOCK))


QUERY = key(tb.query_process("%3", ID))
KILL = key(tb.kill_pane("%3", ID, query=False))
KILL_QUERY = key(tb.kill_pane("%3", ID, query=True))
NOMATCH = completed(tb.NOMATCH + "\n")
ANSWER = completed(f"4242 0 {SERVER}\n")


@dataclass
class Closed:
    code: int
    stdout: str
    stderr: str
    gateway: FakeGateway
    clock: FakeClock

    def json(self) -> Any:
        return json.loads(self.stdout)

    def output(self) -> Any:
        assert self.code == 0, self.stdout + self.stderr
        data = self.json()
        validate(data, "termctl/cli/close.json")
        return data

    def error(self, code: str, exit_code: int) -> Any:
        assert self.code == exit_code, self.stdout + self.stderr
        envelope = self.json()
        validate(envelope, "termctl/cli/error.json")
        assert envelope["error"]["code"] == code
        return envelope["error"]

    def tmux(self) -> list[str]:
        """Each tmux call by name: ``list-panes``, ``query``, ``kill`` or ``kill+query``."""
        names = {QUERY: "query", KILL: "kill", KILL_QUERY: "kill+query"}
        found = []
        for call in self.gateway.calls:
            if is_list_panes(call):
                found.append("list-panes")
            elif call[1:] == ["-V"]:
                continue
            else:
                found.append(names.get(tuple(call), " ".join(call)))
        return found


def gateway_for(
    rows: list[dict[str, str]] | None = None,
    *,
    process: FakeProcess | None = None,
    responses: Mapping[tuple[str, ...], Any] | None = None,
    peer: int | BaseException = SERVER,
    tty: bool = False,
) -> tuple[FakeGateway, FakeClock]:
    clock = FakeClock()
    gateway = FakeGateway(
        {QUERY: ANSWER, KILL: completed(), KILL_QUERY: ANSWER, **(responses or {})},
        panes=[pane_row(tc_id=ID)] if rows is None else rows,
        peer_pids={SOCK: peer},
        processes=[process or FakeProcess()],
        clock=clock,
        tty=tty,
    )
    return gateway, clock


def close(
    argv: list[str],
    gateway: FakeGateway,
    clock: FakeClock,
    environ: Mapping[str, str] | None = None,
    *,
    json_output: bool = True,
) -> Closed:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(
        [*(["--json"] if json_output else []), "close", *argv],
        gateway=gateway,
        clock=clock,
        environ=private_environ({**ENVIRON, **(environ or {})}),
        stdout=stdout,
        stderr=stderr,
    )
    return Closed(code, stdout.getvalue(), stderr.getvalue(), gateway, clock)


def forced(
    *,
    process: FakeProcess | None = None,
    rows: list[dict[str, str]] | None = None,
    responses: Mapping[tuple[str, ...], Any] | None = None,
    peer: int | BaseException = SERVER,
    extra: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Closed:
    gateway, clock = gateway_for(rows, process=process, responses=responses, peer=peer)
    return close([ID, "--force", *HUMAN, *(extra or [])], gateway, clock, environ)


def records(state: Path, name: str = "log.jsonl") -> list[Any]:
    path = state / "termctl" / name
    if not path.exists():
        return []
    found = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for record in found:
        validate(record, "termctl/log.json" if name == "log.jsonl" else "termctl/audit.json")
    return found


def close_records(state: Path) -> list[Any]:
    return [r for r in records(state) if r["event"] == "close"]


def sh(cmd: str) -> tuple[str, ...]:
    return ("/bin/sh", "-c", cmd)


def hook(gateway: FakeGateway, cmd: str, response: GroupResponse) -> None:
    gateway.group_responses[sh(cmd)] = response


def changing(gateway: FakeGateway, rows: list[dict[str, str]]) -> Callable[[GroupCall], GroupRun]:
    """A pre-close hook during which the panes change to ``rows``."""

    def run(_: GroupCall) -> GroupRun:
        gateway.panes = rows
        return group_run(0)

    return run


# --- step 1: running panes, the --force gate ----------------------------------------------------


def test_a_running_pane_without_force_is_refused_and_nothing_runs(private_state_home: Path) -> None:
    gateway, clock = gateway_for()
    hook(gateway, "gate", group_run(0))
    result = close([ID, "--hook", "pre-close=gate"], gateway, clock)
    error = result.error("refused", 20)
    assert error["reason"] == "running"
    assert result.tmux() == ["list-panes"]
    assert gateway.group_calls == [] and gateway.signals == [] and gateway.handles == []
    [record] = close_records(private_state_home)
    assert (record["result"], record["level"], record["exit_code"]) == ("refused", "warn", 20)
    assert (record["force"], record["id"], record["hooks"]) == (False, ID, [])
    assert records(private_state_home, "audit.jsonl") == []


@pytest.mark.parametrize(
    "argv",
    [
        [ID, "--force"],
        [ID, "--force", "--actor", "auto:policy"],
        [ID, "--force", "--dry-run"],
        ["abcdefgh", "--force", "--grace", "1s"],
    ],
)
def test_force_needs_a_terminal_or_a_human_actor(argv: list[str], private_state_home: Path) -> None:
    gateway, clock = gateway_for()
    result = close(argv, gateway, clock)
    error = result.error("usage", 2)
    assert "--force" in error["message"]
    assert gateway.calls == []
    assert records(private_state_home) == []


def test_force_with_a_terminal_on_stdin_is_accepted() -> None:
    gateway, clock = gateway_for(process=FakeProcess(hup_on_kill=True), tty=True)
    result = close([ID, "--force"], gateway, clock)
    assert result.output()["forced"] is True


def test_force_takes_only_a_full_id() -> None:
    gateway, clock = gateway_for()
    result = close(["abcd", "--force", *HUMAN], gateway, clock)
    assert "full id" in result.error("usage", 2)["message"]


def test_force_also_accepts_the_id_without_its_prefix() -> None:
    gateway, clock = gateway_for(process=FakeProcess(hup_on_kill=True))
    result = close(["ABCDEFGH", "--force", *HUMAN], gateway, clock)
    assert result.output()["id"] == ID


# --- dead panes ---------------------------------------------------------------------------------


def test_a_dead_pane_is_closed_without_force_by_a_guarded_kill_pane(
    private_state_home: Path,
) -> None:
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)])
    result = close([ID], gateway, clock)
    assert result.output() == {
        "id": ID, "pane_closed": True, "was_running": False, "forced": False,
        "signals_sent": [], "exited": True, "pid_mismatch": False, "decision_ids": [],
        "duration_ms": 0, "socket_path": SOCK,
    }  # fmt: skip
    # Step 1, the re-check, and kill-pane (never kill-window: spike D5), nothing else.
    assert result.tmux() == ["list-panes", "list-panes", "kill"]
    assert not any("kill-window" in " ".join(call) for call in gateway.calls)
    assert gateway.handles == [] and gateway.peer_checks == []
    [record] = close_records(private_state_home)
    assert (record["result"], record["force"], record["actor"]) == ("ok", False, "auto:unknown")
    assert (record["escalation_override"], record["decision_ids"]) == (False, [])
    assert records(private_state_home, "audit.jsonl") == []


def test_the_guarded_kill_targets_only_the_managed_pane() -> None:
    argv = list(KILL)
    assert argv[3:6] == ["if-shell", "-F", "-t"] and argv[6] == "%3"
    assert "#{==:#{@termctl_pane},#{pane_id}}" in argv[8]
    assert f"#{{==:#{{@termctl_id}},{ID}}}" in argv[8]
    assert argv[9] == "'kill-pane' '-t' '%3'"


def test_human_output_is_closed_and_the_id() -> None:
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)])
    result = close([ID], gateway, clock, json_output=False)
    assert (result.code, result.stdout, result.stderr) == (0, f"closed {ID}\n", "")


def test_a_dead_pane_with_a_mismatched_pid_reports_pid_mismatch() -> None:
    row = pane_row(tc_id=ID, overrides={**DEAD, "pane_pid": "5555"})
    gateway, clock = gateway_for([row])
    data = close([ID], gateway, clock).output()
    assert (data["pid_mismatch"], data["exited"], data["signals_sent"]) == (True, None, [])


def test_a_dead_pane_gone_at_the_kill_counts_as_closed() -> None:
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)], responses={KILL: NOMATCH})
    assert close([ID], gateway, clock).output()["pane_closed"] is True


@pytest.mark.parametrize(
    "failure",
    [
        completed(stderr="can't find pane: %3\n", returncode=1),
        completed(stderr=f"no server running on {SOCK}\n", returncode=1),
    ],
)
def test_tmux_reporting_the_pane_or_server_gone_at_the_kill_counts_as_closed(
    failure: Any,
) -> None:
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)], responses={KILL: failure})
    assert close([ID], gateway, clock).output()["pane_closed"] is True


def test_another_tmux_failure_at_the_kill_exits_12(private_state_home: Path) -> None:
    failure = completed(stderr="server exited unexpectedly\n", returncode=1)
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)], responses={KILL: failure})
    close([ID], gateway, clock).error("server_unavailable", 12)
    [record] = close_records(private_state_home)
    assert (record["result"], record["exit_code"]) == ("error", 12)


# --- running panes with --force: the signal sequence (spike F3) --------------------------------


def test_f3_kernel_sighup_at_the_kill_is_enough_and_nothing_is_signaled(
    private_state_home: Path,
) -> None:
    result = forced(process=FakeProcess(hup_on_kill=True))
    data = result.output()
    assert (data["was_running"], data["forced"], data["signals_sent"]) == (True, True, [])
    assert (data["exited"], data["pid_mismatch"]) == (True, False)
    # Step 1 probes the process, the re-check reads again, step 5 probes again with a handle
    # open, then one guarded invocation reads #{pane_pid} and kills the pane.
    assert result.tmux() == ["list-panes", "query", "list-panes", "query", "kill+query"]
    assert result.gateway.signals == []
    assert all(handle.closed for handle in result.gateway.handles)
    [audit] = records(private_state_home, "audit.jsonl")
    assert (audit["action"], audit["target"], audit["force"]) == ("close", ID, True)
    assert audit["inputs"] == {"socket_path": SOCK, "pid": 4242, "grace_ms": 10000}
    assert audit["result"] == data
    [record] = close_records(private_state_home)
    assert (record["force"], record["actor"], record["result"]) == (True, "human:me", "ok")


def test_f3_term_after_the_grace_then_kill_5s_later_then_1s() -> None:
    result = forced(process=FakeProcess())
    data = result.output()
    assert (data["signals_sent"], data["exited"]) == (["SIGTERM", "SIGKILL"], False)
    assert result.gateway.signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert sum(result.clock.sleeps) == pytest.approx(16.0)
    assert data["duration_ms"] == 16000


def test_f3_a_process_that_exits_on_sigterm_is_exited() -> None:
    result = forced(process=FakeProcess(exits_on=frozenset({signal.SIGTERM})))
    data = result.output()
    assert (data["signals_sent"], data["exited"]) == (["SIGTERM"], True)


def test_grace_flag_sets_the_wait_before_sigterm() -> None:
    result = forced(
        process=FakeProcess(exits_on=frozenset({signal.SIGTERM})), extra=["--grace", "2s"]
    )
    assert result.output()["duration_ms"] == 2000


def test_close_grace_from_the_environment_sets_the_wait() -> None:
    result = forced(
        process=FakeProcess(exits_on=frozenset({signal.SIGTERM})),
        environ={"TERMCTL_CLOSE_GRACE": "500ms"},
    )
    assert result.output()["duration_ms"] == 500


def test_a_zombie_at_the_kill_counts_as_exited() -> None:
    result = forced(process=FakeProcess(zombie=True, exited=True))
    data = result.output()
    assert (data["signals_sent"], data["exited"], data["pid_mismatch"]) == ([], True, False)
    assert result.tmux()[-1] == "kill"


def test_a_running_pane_with_no_recorded_pid_is_killed_and_nothing_is_signaled() -> None:
    row = pane_row(tc_id=ID, overrides={"@termctl_pid": "", "@termctl_pid_start": ""})
    result = forced(rows=[row])
    data = result.output()
    assert (data["signals_sent"], data["exited"], data["pid_mismatch"]) == ([], True, False)
    assert result.tmux() == ["list-panes", "list-panes", "kill"]
    assert result.gateway.handles == []


# --- pid_mismatch: never signaled ---------------------------------------------------------------


def assert_unsignaled_mismatch(result: Closed) -> Any:
    data = result.output()
    assert (data["pid_mismatch"], data["exited"], data["signals_sent"]) == (True, None, [])
    assert result.gateway.signals == []
    # The pane is still closed.
    assert result.tmux()[-1] in ("kill", "kill+query")
    return data


def test_pid_not_equal_to_pane_pid_at_step_1_is_a_mismatch_and_no_process_is_read() -> None:
    result = forced(rows=[pane_row(tc_id=ID, overrides={"pane_pid": "5555"})])
    assert_unsignaled_mismatch(result)
    assert result.tmux() == ["list-panes", "list-panes", "kill"]
    assert result.gateway.proc_reads == [] and result.gateway.handles == []


def test_pid_not_equal_to_pane_pid_at_the_step_3_re_resolution_is_a_mismatch() -> None:
    gateway, clock = gateway_for()
    hook(gateway, "gate", changing(gateway, [pane_row(tc_id=ID, overrides={"pane_pid": "5555"})]))
    result = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock)
    assert_unsignaled_mismatch(result)
    assert result.tmux()[-1] == "kill"


def test_a_recorded_pid_that_changed_between_the_checks_is_a_mismatch() -> None:
    gateway, clock = gateway_for()
    changed = {"@termctl_pid": "5555", "pane_pid": "5555", "@termctl_pid_start": "1"}
    hook(gateway, "gate", changing(gateway, [pane_row(tc_id=ID, overrides=changed)]))
    result = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock)
    assert_unsignaled_mismatch(result)


def test_a_respawned_pane_at_kill_time_is_a_mismatch() -> None:
    result = forced(responses={KILL_QUERY: completed(f"5555 0 {SERVER}\n")})
    assert_unsignaled_mismatch(result)


def test_another_server_at_kill_time_is_a_mismatch() -> None:
    result = forced(responses={KILL_QUERY: completed("4242 0 99\n")})
    assert_unsignaled_mismatch(result)


def test_an_unreadable_kill_time_answer_is_a_mismatch() -> None:
    result = forced(responses={KILL_QUERY: completed("")})
    assert_unsignaled_mismatch(result)


def test_a_start_time_mismatch_is_a_mismatch() -> None:
    assert_unsignaled_mismatch(forced(process=FakeProcess(start="999")))


def test_a_process_whose_parent_is_not_the_server_is_a_mismatch() -> None:
    assert_unsignaled_mismatch(forced(process=FakeProcess(ppid=1)))


@pytest.mark.parametrize("peer", [0, 99])
def test_a_server_in_another_pid_namespace_is_never_signaled(peer: int) -> None:
    result = forced(peer=peer)
    assert_unsignaled_mismatch(result)
    # The namespace is checked before the process is read.
    assert result.gateway.proc_reads == []


def test_a_namespace_check_that_cannot_be_made_warns_and_signals_nothing() -> None:
    result = forced(peer=PermissionError(13, "Permission denied"))
    assert_unsignaled_mismatch(result)
    assert "cannot check the server: Permission denied" in result.stderr


@pytest.mark.parametrize(
    ("own", "problem"),
    [
        (False, "/proc belongs to another PID namespace than termctl"),
        (PermissionError(13, "Permission denied"), "cannot read /proc/self: Permission denied"),
    ],
)
def test_a_proc_of_another_pid_namespace_is_uncheckable_and_never_signaled(
    own: bool | OSError, problem: str
) -> None:
    gateway, clock = gateway_for()
    gateway.own_namespace = own
    result = close([ID, "--force", *HUMAN], gateway, clock)
    assert_unsignaled_mismatch(result)
    assert problem in result.stderr
    assert gateway.proc_reads == []


def test_pane_pid_changing_between_the_list_and_the_query_is_a_mismatch() -> None:
    result = forced(responses={QUERY: completed(f"5555 0 {SERVER}\n")})
    assert_unsignaled_mismatch(result)


def test_without_a_pidfd_nothing_is_signaled() -> None:
    gateway, clock = gateway_for()
    gateway.pidfd_error = OSError(38, "Function not implemented")
    result = close([ID, "--force", *HUMAN], gateway, clock)
    assert_unsignaled_mismatch(result)
    assert "cannot open a handle on process 4242" in result.stderr


def test_the_handle_is_open_before_the_process_is_verified() -> None:
    gateway, clock = gateway_for(process=FakeProcess(hup_on_kill=True))
    order: list[str] = []
    real_open, real_read = gateway.open_process, gateway.read_proc_stat

    def open_process(pid: int) -> Any:
        order.append("open")
        return real_open(pid)

    def read_proc_stat(pid: int) -> Any:
        order.append("read")
        return real_read(pid)

    gateway.open_process = open_process  # type: ignore[method-assign]
    gateway.read_proc_stat = read_proc_stat  # type: ignore[method-assign]
    close([ID, "--force", *HUMAN], gateway, clock).output()
    # Step 1's probe reads; step 5 opens the handle, then verifies through it.
    assert order[:3] == ["read", "open", "read"]


# --- steps 2 and 3: hooks and the re-check ------------------------------------------------------


def test_pre_close_payload_validates_and_post_close_gets_the_result(
    private_state_home: Path,
) -> None:
    gateway, clock = gateway_for(process=FakeProcess(hup_on_kill=True))
    hook(gateway, "gate", group_run(0, stdout=b'{"decision_id": "from-hook"}', pgid=900))
    hook(gateway, "notify", group_run(0, pgid=901))
    argv = [ID, "--force", *HUMAN, "--decision-id", "d-1", "--grace", "3s",
            "--hook", "pre-close=gate", "--hook", "post-close=notify"]  # fmt: skip
    data = close(argv, gateway, clock).output()
    pre = json.loads(gateway.group_calls[0].stdin)
    validate(pre, "termctl/hooks/pre-close.json")
    assert (pre["hook"], pre["object_id"], pre["labels"]) == ("pre-close", ID, ["kind=review"])
    assert (pre["force"], pre["grace_ms"], pre["actor"]) == (True, 3000, "human:me")
    assert (pre["decision_ids"], pre["override_escalation"]) == (["d-1"], False)
    assert pre["pane"]["id"] == ID and pre["pane"]["state"] == "running"
    env = gateway.group_calls[0].env
    assert (env["TERMCTL_HOOK"], env["TERMCTL_ID"], env["TERMCTL_OBJECT_TYPE"]) == (
        "pre-close",
        ID,
        "pane",
    )
    post = json.loads(gateway.group_calls[1].stdin)
    validate(post, "termctl/hooks/post-close.json")
    assert post["result"] == data and post["labels"] == ["kind=review"]
    assert data["decision_ids"] == ["d-1", "from-hook"]
    [record] = close_records(private_state_home)
    assert record["hooks"] == [
        {"hook": "pre-close", "cmd": "gate", "pgid": 900, "async": False},
        {"hook": "post-close", "cmd": "notify", "pgid": 901, "async": False},
    ]
    assert record["decision_ids"] == ["d-1", "from-hook"]
    [audit] = records(private_state_home, "audit.jsonl")
    assert audit["decision_id"] == ["d-1", "from-hook"]


def test_the_hooks_run_after_the_check_and_before_the_re_check() -> None:
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)])
    seen: list[int] = []

    def gate(_: GroupCall) -> GroupRun:
        seen.append(len(gateway.calls))
        return group_run(0)

    hook(gateway, "gate", gate)
    result = close([ID, "--hook", "pre-close=gate"], gateway, clock)
    assert result.output()["pane_closed"] is True
    # -V and step 1's read ran before the hook; the re-check and the kill after it.
    assert seen == [2]
    assert result.tmux() == ["list-panes", "list-panes", "kill"]


def test_a_veto_exits_11_and_closes_nothing(private_state_home: Path) -> None:
    gateway, clock = gateway_for()
    hook(gateway, "gate", group_run(1, stderr=b"no"))
    result = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock)
    error = result.error("vetoed", 11)
    assert (error["hook"], error["hook_stderr"]) == ("gate", "no")
    assert result.tmux() == ["list-panes", "query"]
    assert gateway.signals == []
    [record] = close_records(private_state_home)
    assert (record["result"], record["details"]) == ("vetoed", {"hook": "gate"})
    # A forced close that saw a running pane is audited whatever the outcome.
    [audit] = records(private_state_home, "audit.jsonl")
    assert audit["result"]["error"]["code"] == "vetoed"


def test_an_escalation_exits_13_and_an_override_run_closes(private_state_home: Path) -> None:
    rows = [pane_row(tc_id=ID, overrides=DEAD)]
    gateway, clock = gateway_for(rows)
    hook(gateway, "gate", group_run(13))
    close([ID, "--hook", "pre-close=gate"], gateway, clock).error("escalated", 13)
    assert records(private_state_home, "audit.jsonl") == []
    gateway, clock = gateway_for(rows)
    hook(gateway, "gate", group_run(13))
    result = close(
        [ID, "--hook", "pre-close=gate", "--override-escalation", *HUMAN], gateway, clock
    )
    data = result.output()
    assert json.loads(gateway.group_calls[0].stdin)["override_escalation"] is True
    [audit] = records(private_state_home, "audit.jsonl")
    assert (audit["force"], audit["escalation_override"]) == (False, True)
    assert audit["inputs"] == {"socket_path": SOCK, "pid": 4242, "grace_ms": None}
    assert audit["result"] == data
    record = close_records(private_state_home)[-1]
    assert (record["escalation_override"], record["result"]) == (True, "ok")


def test_a_pane_that_died_during_the_hooks_is_state_changed(private_state_home: Path) -> None:
    gateway, clock = gateway_for()
    hook(gateway, "gate", changing(gateway, [pane_row(tc_id=ID, overrides=DEAD)]))
    result = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock)
    result.error("state_changed", 12)
    assert result.tmux() == ["list-panes", "query", "list-panes"]
    assert gateway.signals == []
    [record] = close_records(private_state_home)
    assert (record["result"], record["exit_code"]) == ("error", 12)
    [audit] = records(private_state_home, "audit.jsonl")
    assert audit["result"]["error"]["code"] == "state_changed"


def test_a_dead_pane_respawned_during_the_hooks_is_state_changed(private_state_home: Path) -> None:
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)])
    hook(gateway, "gate", changing(gateway, [pane_row(tc_id=ID)]))
    result = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock)
    result.error("state_changed", 12)
    assert "kill" not in result.tmux()
    # Step 3 saw it running: the forced run is audited.
    [audit] = records(private_state_home, "audit.jsonl")
    assert audit["force"] is True


def test_the_id_on_another_tmux_pane_at_the_re_check_is_state_changed() -> None:
    # Step 1's tmux pane %3 is still a managed pane (now under another id).
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)])
    rows = [pane_row("%3", "tc-zzzzzzzz", overrides=DEAD), pane_row("%9", ID, overrides=DEAD)]
    hook(gateway, "gate", changing(gateway, rows))
    result = close([ID, "--hook", "pre-close=gate"], gateway, clock)
    result.error("state_changed", 12)
    assert "kill" not in result.tmux()


def test_the_id_moved_off_a_gone_tmux_pane_counts_as_gone_and_the_other_is_untouched(
    private_state_home: Path,
) -> None:
    # Step 1's tmux pane %3 is gone; a running pane %9 now carries the id (forged).
    gateway, clock = gateway_for(process=FakeProcess(exits_on=frozenset({signal.SIGTERM})))
    hook(gateway, "gate", changing(gateway, [pane_row("%9", ID)]))
    result = close(
        [ID, "--force", *HUMAN, "--hook", "pre-close=gate", "--grace", "0s"], gateway, clock
    )
    data = result.output()
    assert (data["pane_closed"], data["was_running"]) == (True, True)
    # Step 1's proof stands: the recorded process is signaled by its start time alone.
    assert data["signals_sent"] == ["SIGTERM"]
    assert result.tmux() == ["list-panes", "query", "list-panes"]  # %9 is never touched
    assert "its id now appears on tmux pane %9, which was not touched" in result.stderr
    assert [r["event"] for r in records(private_state_home)].count("warning") == 1


def test_two_panes_with_the_id_at_the_re_check_are_ambiguous() -> None:
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)])
    rows = [pane_row(tc_id=ID, overrides=DEAD), pane_row("%9", ID, overrides=DEAD)]
    hook(gateway, "gate", changing(gateway, rows))
    result = close([ID, "--hook", "pre-close=gate"], gateway, clock)
    assert [c["pane_id"] for c in result.error("ambiguous", 21)["candidates"]] == ["%3", "%9"]


def test_gone_at_the_re_check_exits_0_and_runs_the_post_close_hooks() -> None:
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)])
    hook(gateway, "gate", changing(gateway, []))
    hook(gateway, "notify", group_run(0))
    argv = [ID, "--hook", "pre-close=gate", "--hook", "post-close=notify"]
    data = close(argv, gateway, clock).output()
    assert (data["pane_closed"], data["was_running"], data["exited"]) == (True, False, True)
    assert "kill" not in Closed(0, "", "", gateway, clock).tmux()
    post = json.loads(gateway.group_calls[1].stdin)
    validate(post, "termctl/hooks/post-close.json")


def test_gone_at_the_re_check_signals_the_pid_that_passed_step_1_by_start_time() -> None:
    gateway, clock = gateway_for(process=FakeProcess(exits_on=frozenset({signal.SIGTERM})))
    hook(gateway, "gate", changing(gateway, []))
    result = close(
        [ID, "--force", *HUMAN, "--hook", "pre-close=gate", "--grace", "1s"], gateway, clock
    )
    data = result.output()
    assert (data["was_running"], data["signals_sent"], data["exited"]) == (True, ["SIGTERM"], True)
    assert result.tmux() == ["list-panes", "query", "list-panes"]
    # The namespace was checked once, at step 1; there is no #{pid} to read afterwards.
    assert gateway.peer_checks == [SOCK]


def test_gone_at_the_re_check_with_a_reused_pid_is_a_mismatch() -> None:
    gateway, clock = gateway_for()

    def gate(_: GroupCall) -> GroupRun:
        gateway.panes = []
        gateway.add_process(FakeProcess(start="999"))  # the PID now belongs to another process
        return group_run(0)

    hook(gateway, "gate", gate)
    result = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock)
    data = result.output()
    assert (data["pid_mismatch"], data["exited"], data["signals_sent"]) == (True, None, [])
    assert gateway.signals == []


def test_gone_at_the_re_check_never_signals_a_pid_that_failed_step_1() -> None:
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides={"pane_pid": "5555"})])
    hook(gateway, "gate", changing(gateway, []))
    result = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock)
    data = result.output()
    assert (data["pid_mismatch"], data["signals_sent"]) == (True, [])
    assert gateway.handles == [] and gateway.signals == []


def test_gone_at_the_re_check_never_signals_a_pid_in_another_namespace() -> None:
    gateway, clock = gateway_for(peer=0)
    hook(gateway, "gate", changing(gateway, []))
    result = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock)
    assert result.output()["pid_mismatch"] is True
    assert gateway.handles == [] and gateway.signals == []


def test_gone_at_the_re_check_with_the_process_exited_is_exited() -> None:
    gateway, clock = gateway_for()

    def gate(_: GroupCall) -> GroupRun:
        gateway.panes = []
        gateway.processes[4242].exited = True
        return group_run(0)

    hook(gateway, "gate", gate)
    data = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock).output()
    assert (data["exited"], data["pid_mismatch"], data["signals_sent"]) == (True, False, [])


def test_gone_with_its_server_at_the_re_check_counts_as_closed() -> None:
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)])
    gone = completed(stderr=f"no server running on {SOCK}\n", returncode=1)

    def gate(_: GroupCall) -> GroupRun:
        gateway.list_panes = gone
        return group_run(0)

    hook(gateway, "gate", gate)
    assert close([ID, "--hook", "pre-close=gate"], gateway, clock).output()["pane_closed"]


def test_gone_at_the_kill_still_runs_the_signal_sequence() -> None:
    process = FakeProcess(exits_on=frozenset({signal.SIGTERM}))
    result = forced(process=process, responses={KILL_QUERY: NOMATCH}, extra=["--grace", "0s"])
    data = result.output()
    assert (data["pane_closed"], data["signals_sent"], data["exited"]) == (True, ["SIGTERM"], True)


def test_gone_at_the_step_5_probe_uses_the_step_1_proof() -> None:
    process = FakeProcess(exits_on=frozenset({signal.SIGTERM}))
    gateway, clock = gateway_for(process=process)

    def gate(_: GroupCall) -> GroupRun:
        gateway.responses[QUERY] = NOMATCH  # gone between the re-check and the kill
        return group_run(0)

    hook(gateway, "gate", gate)
    result = close(
        [ID, "--force", *HUMAN, "--hook", "pre-close=gate", "--grace", "0s"], gateway, clock
    )
    data = result.output()
    assert data["signals_sent"] == ["SIGTERM"]
    assert "kill+query" not in result.tmux()


def test_gone_at_the_step_1_probe_with_its_process_exited_is_exited_not_a_mismatch() -> None:
    gateway, clock = gateway_for(responses={QUERY: NOMATCH})

    def gate(_: GroupCall) -> GroupRun:
        gateway.panes = []
        return group_run(0)

    hook(gateway, "gate", gate)
    gateway.processes[4242].exited = True
    result = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock)
    data = result.output()
    assert (data["pid_mismatch"], data["exited"], data["signals_sent"]) == (False, True, [])
    assert "warning" not in result.stderr
    assert gateway.signals == []


def test_gone_at_the_step_1_probe_with_its_process_alive_warns_and_never_signals() -> None:
    gateway, clock = gateway_for(responses={QUERY: NOMATCH})
    hook(gateway, "gate", changing(gateway, []))
    result = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock)
    data = result.output()
    assert (data["pid_mismatch"], data["exited"], data["signals_sent"]) == (True, None, [])
    assert "the pane was gone before process 4242 could be checked" in result.stderr
    assert gateway.signals == []
    assert all(handle.closed for handle in gateway.handles)


def test_gone_at_the_step_1_probe_but_listed_at_the_re_check_is_never_signaled() -> None:
    result = forced(responses={QUERY: NOMATCH})
    data = result.output()
    assert (data["pid_mismatch"], data["signals_sent"]) == (True, [])
    assert result.tmux() == ["list-panes", "query", "list-panes", "kill"]
    assert result.gateway.signals == []


# --- a run stopped by a signal ------------------------------------------------------------------


def send(signum: int) -> None:
    """Deliver ``signum`` to this process, only with termctl's handler installed (it must
    never reach the test runner)."""
    assert signal.getsignal(signum) not in (signal.SIG_DFL, signal.SIG_IGN)
    assert signal.getsignal(signum) is not signal.default_int_handler
    os.kill(os.getpid(), signum)  # this process: exact, and handled


def signaling_on_sleep(clock: FakeClock, signum: int, after: int) -> None:
    """The fake clock's ``after``-th sleep delivers ``signum`` (during the signal sequence)."""
    sleep = clock.sleep
    count = [0]

    def sleeping(seconds: float) -> None:
        count[0] += 1
        if count[0] == after:
            send(signum)
        sleep(seconds)

    clock.sleep = sleeping  # type: ignore[method-assign]


def signaling_on_call(gateway: FakeGateway, call: tuple[str, ...], signum: int) -> None:
    """The tmux ``call`` delivers ``signum`` once it has run, as a signal during it would."""
    run = gateway.run

    def running(argv: list[str], **kwargs: Any) -> Any:
        result = run(argv, **kwargs)
        if tuple(argv) == call:
            send(signum)
        return result

    gateway.run = running  # type: ignore[method-assign]


STOPS = [(signal.SIGINT, "interrupted"), (signal.SIGTERM, "terminated by signal 15")]
HANDLED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def assert_audited_output(state: Path) -> Any:
    [audit] = records(state, "audit.jsonl")
    validate(audit["result"], "termctl/cli/close.json")
    assert (audit["action"], audit["force"], audit["target"]) == ("close", True, ID)
    return audit["result"]


@pytest.mark.parametrize(("signum", "message"), STOPS, ids=["int", "term"])
def test_a_stop_signal_during_the_signal_sequence_is_deferred_until_the_audit(
    signum: int, message: str, private_state_home: Path
) -> None:
    gateway, clock = gateway_for()
    hook(gateway, "notify", group_run(0))
    # The grace is 0 s: SIGTERM goes out at once; the signal comes in the 5 s wait after it.
    signaling_on_sleep(clock, signum, after=1)
    before = {s: signal.getsignal(s) for s in HANDLED}
    argv = [ID, "--force", *HUMAN, "--grace", "0s", "--hook", "post-close=notify"]
    result = close(argv, gateway, clock)
    assert result.code == 128 + signum, result.stderr
    # The sequence ran to its end, the output was audited; no post-close hook ran.
    assert gateway.signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert assert_audited_output(private_state_home)["signals_sent"] == ["SIGTERM", "SIGKILL"]
    assert gateway.group_calls == []
    [record] = close_records(private_state_home)
    assert (record["result"], record["exit_code"], record["message"]) == (
        "error",
        128 + signum,
        message,
    )
    assert {s: signal.getsignal(s) for s in HANDLED} == before


@pytest.mark.parametrize(("signum", "message"), STOPS, ids=["int", "term"])
def test_a_stop_signal_during_the_guarded_kill_leaves_no_run_unrecorded(
    signum: int, message: str, private_state_home: Path
) -> None:
    # R1-11: the tmux client runs in its own process group (``SystemGateway.run``), so the
    # signal reaches termctl only; it is raised once the close and its audit record are done.
    gateway, clock = gateway_for(process=FakeProcess(hup_on_kill=True))
    signaling_on_call(gateway, KILL_QUERY, signum)
    result = close([ID, "--force", *HUMAN], gateway, clock)
    assert result.code == 128 + signum, result.stderr
    assert result.tmux()[-1] == "kill+query"
    output = assert_audited_output(private_state_home)
    assert (output["pane_closed"], output["exited"], output["pid_mismatch"]) == (True, True, False)
    [record] = close_records(private_state_home)
    assert (record["exit_code"], record["message"]) == (128 + signum, message)


@pytest.mark.parametrize("where", ["kill", "sequence", "post-close"])
def test_sighup_from_the_re_check_on_does_not_stop_close(
    where: str, private_state_home: Path
) -> None:
    # Closing the pane termctl runs in hangs up its terminal: the run completes.
    gateway, clock = gateway_for()
    hook(gateway, "notify", group_run(0))
    if where == "kill":
        signaling_on_call(gateway, KILL_QUERY, signal.SIGHUP)
    elif where == "sequence":
        signaling_on_sleep(clock, signal.SIGHUP, after=1)
    else:

        def hanging_up(_: GroupCall) -> GroupRun:
            send(signal.SIGHUP)
            return group_run(0)

        hook(gateway, "notify", hanging_up)
    argv = [ID, "--force", *HUMAN, "--grace", "0s", "--hook", "post-close=notify"]
    data = close(argv, gateway, clock).output()
    assert assert_audited_output(private_state_home) == data
    assert [r["exit_code"] for r in close_records(private_state_home)] == [0]


def test_sighup_in_a_pre_close_hook_still_stops_close_before_any_change(
    private_state_home: Path,
) -> None:
    gateway, clock = gateway_for()
    hook(gateway, "gate", with_pgid(Terminated(signal.SIGHUP), 9))
    result = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock)
    assert result.code == 129
    assert result.tmux() == ["list-panes", "query"]
    assert gateway.signals == []
    [record] = close_records(private_state_home)
    assert (record["exit_code"], record["hooks"]) == (
        129,
        [{"hook": "pre-close", "cmd": "gate", "pgid": 9, "async": False}],
    )
    # Nothing was changed, so there is nothing to audit (as for open before its window).
    assert records(private_state_home, "audit.jsonl") == []


def test_a_stop_in_a_later_pre_close_hook_keeps_the_earlier_hooks_and_ids(
    private_state_home: Path,
) -> None:
    # R1-10.
    gateway, clock = gateway_for()
    hook(gateway, "a", group_run(0, stdout=b'{"decision_id": "a-1"}', pgid=11))
    hook(gateway, "b", with_pgid(KeyboardInterrupt(), 12))
    argv = [ID, "--force", *HUMAN, "--decision-id", "d-1", "--hook", "pre-close=a",
            "--hook", "pre-close=b"]  # fmt: skip
    assert close(argv, gateway, clock).code == 130
    [record] = close_records(private_state_home)
    assert record["decision_ids"] == ["d-1", "a-1"]
    assert record["hooks"] == [
        {"hook": "pre-close", "cmd": "a", "pgid": 11, "async": False},
        {"hook": "pre-close", "cmd": "b", "pgid": 12, "async": False},
    ]


def test_a_stop_in_a_later_post_close_hook_keeps_the_earlier_hooks_and_the_audit(
    private_state_home: Path,
) -> None:
    # R1-10: the audit record was written with the output before the post-close hooks.
    gateway, clock = gateway_for(process=FakeProcess(hup_on_kill=True))
    hook(gateway, "a", group_run(0, pgid=21))
    hook(gateway, "b", with_pgid(Terminated(signal.SIGTERM), 22))
    argv = [ID, "--force", *HUMAN, "--hook", "post-close=a", "--hook", "post-close=b"]
    assert close(argv, gateway, clock).code == 143
    [record] = close_records(private_state_home)
    assert (record["exit_code"], record["message"]) == (143, "terminated by signal 15")
    assert record["hooks"] == [
        {"hook": "post-close", "cmd": "a", "pgid": 21, "async": False},
        {"hook": "post-close", "cmd": "b", "pgid": 22, "async": False},
    ]
    assert assert_audited_output(private_state_home)["pane_closed"] is True


def test_a_stopped_close_without_force_of_a_dead_pane_is_not_audited(
    private_state_home: Path,
) -> None:
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)])
    hook(gateway, "gate", KeyboardInterrupt())
    result = close([ID, "--hook", "pre-close=gate"], gateway, clock)
    assert result.code == 130
    assert close_records(private_state_home)[0]["exit_code"] == 130
    assert records(private_state_home, "audit.jsonl") == []


# --- logging and errors before the pane is known ------------------------------------------------


def test_no_server_is_logged_without_an_id(private_state_home: Path) -> None:
    gateway, clock = gateway_for()
    gateway.list_panes = completed(stderr=f"no server running on {SOCK}\n", returncode=1)
    close([ID, "--force", *HUMAN], gateway, clock).error("no_server", 10)
    [record] = close_records(private_state_home)
    assert (record["id"], record["exit_code"], record["force"]) == (None, 10, True)
    assert records(private_state_home, "audit.jsonl") == []


def test_not_found_exits_10(private_state_home: Path) -> None:
    gateway, clock = gateway_for([])
    close([ID], gateway, clock).error("not_found", 10)
    assert close_records(private_state_home)[0]["exit_code"] == 10


# --- --dry-run ----------------------------------------------------------------------------------


def dry(rows: list[dict[str, str]] | None = None, **kwargs: Any) -> Closed:
    peer = kwargs.pop("peer", SERVER)
    gateway, clock = gateway_for(rows, peer=peer, **kwargs)
    return close([ID, "--force", *HUMAN, "--dry-run"], gateway, clock)


def dry_output(result: Closed) -> Any:
    assert result.code == 0, result.stdout + result.stderr
    data = result.json()
    validate(data, "termctl/cli/close-dry-run.json")
    # Read-only: no kill, no handle, no signal, no hook.
    assert not any("kill" in name for name in result.tmux())
    assert result.gateway.handles == [] and result.gateway.signals == []
    assert result.gateway.group_calls == []
    return data


def test_dry_run_of_a_forced_running_pane_reports_the_possible_signals(
    private_state_home: Path,
) -> None:
    data = dry_output(dry())
    expected = json.loads((GOLDEN / "close-dry-run.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/close-dry-run.json")
    assert data == expected
    assert records(private_state_home) == [] and records(private_state_home, "audit.jsonl") == []


def test_dry_run_of_a_dead_pane_has_no_possible_signals() -> None:
    gateway, clock = gateway_for([pane_row(tc_id=ID, overrides=DEAD)])
    result = close([ID, "--dry-run"], gateway, clock)
    data = dry_output(result)
    assert (data["state"], data["forced"], data["signals_possible"]) == ("dead", False, [])
    assert data["grace_ms"] == 10000


@pytest.mark.parametrize(
    ("rows", "kwargs"),
    [
        ([pane_row(tc_id=ID, overrides={"pane_pid": "5555"})], {}),
        (None, {"process": FakeProcess(start="999")}),
        (None, {"peer": 0}),
    ],
)
def test_dry_run_reports_a_mismatch_and_no_signals(
    rows: list[dict[str, str]] | None, kwargs: dict[str, Any]
) -> None:
    data = dry_output(dry(rows, **kwargs))
    assert (data["pid_mismatch"], data["signals_possible"]) == (True, [])


def test_dry_run_of_a_pane_gone_at_the_probe_with_its_process_exited_is_no_mismatch() -> None:
    gateway, clock = gateway_for(responses={QUERY: NOMATCH})
    gateway.processes[4242].exited = True
    result = close([ID, "--force", *HUMAN, "--dry-run"], gateway, clock)
    data = dry_output(result)
    assert (data["pid_mismatch"], data["signals_possible"]) == (False, [])
    assert "warning" not in result.stderr


def test_dry_run_of_a_pane_gone_at_the_probe_with_its_process_alive_warns() -> None:
    gateway, clock = gateway_for(responses={QUERY: NOMATCH})
    result = close([ID, "--force", *HUMAN, "--dry-run"], gateway, clock)
    data = dry_output(result)
    assert (data["pid_mismatch"], data["signals_possible"]) == (True, [])
    assert "a real run would not signal the recorded process" in result.stderr


def test_dry_run_of_a_running_pane_without_force_is_refused() -> None:
    gateway, clock = gateway_for()
    result = close([ID, "--dry-run"], gateway, clock)
    assert result.error("refused", 20)["reason"] == "running"


def test_dry_run_human_output_is_one_line() -> None:
    gateway, clock = gateway_for()
    result = close([ID, "--force", *HUMAN, "--dry-run"], gateway, clock, json_output=False)
    assert result.code == 0
    assert result.stdout == "running pid=4242 pid_mismatch=false signals_possible=SIGTERM,SIGKILL\n"


def test_forced_close_golden(private_state_home: Path) -> None:
    data = forced(process=FakeProcess(exits_on=frozenset({signal.SIGTERM}))).output()
    expected = json.loads((GOLDEN / "close-forced.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/close.json")
    assert data == expected


def test_config_close_grace_is_used(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"close_grace": "250ms"}), encoding="utf-8")
    gateway, clock = gateway_for(process=FakeProcess(exits_on=frozenset({signal.SIGTERM})))
    result = close(["--config", str(config), ID, "--force", *HUMAN], gateway, clock)
    assert result.output()["duration_ms"] == 250
