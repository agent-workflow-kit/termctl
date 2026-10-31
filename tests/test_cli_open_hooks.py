"""``open``'s hooks, override, decision ids, log and audit records and final output, against a
fake tmux and a fake hook runner (interface.md "Open order" steps 4 and 9, "Hooks", "Audit and
log"). Hooks that really run are in test_cli_open_integration.py."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest
import yaml

from termctl import tmux_backend as tb
from termctl.gateway import GroupNotStopped, Terminated, with_pgid

from .fakes import TMUX, FakeGateway, completed, group_run, pane_row
from .schemas import validate
from .test_cli import ENVIRON, GOLDEN, Run
from .test_cli_open import (
    CREATED_AT,
    ID,
    INVALID,
    RECORD,
    SOCK,
    TRUE,
    calls,
    error,
    existing_session_flow,
    fixed_id,  # noqa: F401  (a fixture)
    key,
    make_plan,
    new_row,
    no_tmux_creation,
    proc_stat,
    run,
    tag_key,
    tagged,
)

pytestmark = pytest.mark.usefixtures("fixed_id")

SH = "/bin/sh"


def sh(cmd: str) -> tuple[str, ...]:
    return (SH, "-c", cmd)


def records(state: Path, name: str = "log.jsonl") -> list[Any]:
    path = state / "termctl" / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def open_records(state: Path) -> list[Any]:
    found = [r for r in records(state) if r["event"] == "open"]
    for record in found:
        validate(record, "termctl/log.json")
    return found


def audit_records(state: Path) -> list[Any]:
    found = records(state, "audit.jsonl")
    for record in found:
        validate(record, "termctl/audit.json")
    return found


def payload(gateway: FakeGateway, index: int = 0) -> Any:
    return json.loads(gateway.group_calls[index].stdin)


def flow(**hooks: Any) -> Any:
    """The existing-session flow with ``hooks`` (cmd: response) scripted."""
    gateway = existing_session_flow(make_plan())
    gateway.group_responses.update({sh(cmd): response for cmd, response in hooks.items()})
    return gateway


def config_file(tmp_path: Path, hooks: dict[str, Any]) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"hooks": hooks}), encoding="utf-8")
    return str(path)


# --- step 4: the pre-open payload ---------------------------------------------------------------


def test_pre_open_payload_validates_and_carries_env_names_only() -> None:
    plan = make_plan(env=(("API_MODE", "s3cret-value"), ("ZED", "z")), labels=("k=v",))
    gateway = existing_session_flow(plan, after=[new_row(**{"@termctl_labels": "k=v"})])
    gateway.group_responses[sh("gate")] = group_run(0)
    argv = ["--hook", "pre-open=gate", "--env", "API_MODE=s3cret-value", "--env", "ZED=z",
            "--label", "k=v", "--actor", "auto:ci", "--decision-id", "d-1",
            "--", "true"]  # fmt: skip
    result = run(argv, gateway)
    assert result.code == 0, result.stderr
    data = payload(gateway)
    validate(data, "termctl/hooks/pre-open.json")
    assert "s3cret" not in gateway.group_calls[0].stdin.decode()
    assert data == {
        "tool": "termctl", "hook": "pre-open", "object_type": "pane", "object_id": ID,
        "labels": ["k=v"], "name": "true", "cwd": "/", "argv": ["true"],
        "env_names": ["API_MODE", "ZED"], "placement": "default", "session": "termctl",
        "target": "window", "socket_path": SOCK, "keep_on_exit": "never", "focus": False,
        "actor": "auto:ci", "decision_ids": ["d-1"], "override_escalation": False,
    }  # fmt: skip


def test_hooks_get_the_callers_full_environment_and_the_hook_variables() -> None:
    gateway = flow(gate=group_run(0))
    environ = {"AWS_SECRET_ACCESS_KEY": "k", "TERMCTL_ID": "tc-forged00"}
    assert run(["--hook", "pre-open=gate", "--", "true"], gateway, environ).code == 0
    env = gateway.group_calls[0].env
    assert env["AWS_SECRET_ACCESS_KEY"] == "k"
    assert (env["TERMCTL_HOOK"], env["TERMCTL_ID"], env["TERMCTL_OBJECT_TYPE"]) == (
        "pre-open",
        ID,
        "pane",
    )
    # tmux still gets only the filtered environment.
    assert all("AWS_SECRET_ACCESS_KEY" not in e for e in gateway.envs if e is not None)


def test_configured_pre_open_hooks_run_before_the_flag_ones(tmp_path: Path) -> None:
    config = config_file(tmp_path, {"pre-open": [{"cmd": "first", "timeout": 5}]})
    gateway = flow(first=group_run(0), second=group_run(0))
    argv = ["--json", "--config", config, "open", "--hook", "pre-open=second", "--", "true"]
    assert Run(argv, gateway, ENVIRON).code == 0
    assert [c.argv[2] for c in gateway.group_calls] == ["first", "second"]
    assert [c.timeout for c in gateway.group_calls] == [5, 30]


def test_pre_open_hooks_run_after_the_id_collision_check_and_before_creation() -> None:
    order: list[str] = []
    gateway = flow()

    def gate(call: Any) -> Any:
        order.append(" ".join(calls(gateway)))
        return group_run(0)

    gateway.group_responses[sh("gate")] = gate
    assert run(["--hook", "pre-open=gate", "--", "true"], gateway).code == 0
    assert order == ["-V list-panes"]


def test_a_payload_with_undecodable_bytes_is_valid_json() -> None:
    plan = make_plan(("true", "a\udcff"))
    gateway = existing_session_flow(plan)
    gateway.group_responses[sh("gate")] = group_run(0)
    result = run(["--hook", "pre-open=gate", "--", "true", "a\udcff"], gateway)
    assert result.code == 0, result.stderr
    data = json.loads(gateway.group_calls[0].stdin.decode("ascii"))
    assert data["argv"] == ["true", "a\\xff"]


# --- step 4: veto, escalation and override ------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "code", "exit_code"),
    [
        (group_run(1, stderr=b"policy says no\n"), "vetoed", 11),
        (group_run(-9), "vetoed", 11),
        (group_run(None, stderr=b"slow"), "vetoed", 11),
        (OSError(2, "No such file or directory"), "vetoed", 11),
        (GroupNotStopped(4242), "vetoed", 11),
        (group_run(13, stderr=b"ask a human\n"), "escalated", 13),
    ],
    ids=["exit-1", "signal", "timeout", "not-started", "not-stopped", "escalate"],
)
def test_a_veto_or_escalation_creates_nothing(
    response: Any, code: str, exit_code: int, private_state_home: Path
) -> None:
    gateway = flow(gate=response)
    err = error(run(["--hook", "pre-open=gate", "--", "true"], gateway), code, exit_code)
    assert err["hook"] == "gate"
    no_tmux_creation(gateway)
    [record] = open_records(private_state_home)
    assert (record["result"], record["level"], record["exit_code"]) == (code, "warn", exit_code)
    assert record["details"] == {"hook": "gate"}
    assert record["message"] == err["message"]
    assert audit_records(private_state_home) == []


def test_a_veto_stops_later_hooks_and_wins_over_an_earlier_escalation() -> None:
    gateway = flow(ask=group_run(13), no=group_run(1), never=group_run(0))
    argv = ["--hook", "pre-open=ask", "--hook", "pre-open=no", "--hook", "pre-open=never",
            "--", "true"]  # fmt: skip
    err = error(run(argv, gateway), "vetoed", 11)
    assert err["hook"] == "no"
    assert [c.argv[2] for c in gateway.group_calls] == ["ask", "no"]


def test_the_veto_message_holds_no_hook_output_and_the_stderr_follows_it() -> None:
    gateway = flow(gate=group_run(1, stderr=b"line one\ntermctl: fake\x1b[31m\n"))
    result = Run(["open", "--hook", "pre-open=gate", "--", "true"], gateway, ENVIRON)
    assert result.code == 11
    lines = result.stderr.splitlines()
    assert lines[0] == "termctl: pre-open hook vetoed (exit 1): gate"
    assert lines[1:] == [
        "termctl: hook stderr: line one",
        "termctl: hook stderr: termctl: fake\\x1b[31m",
    ]


def test_override_treats_an_escalation_as_allow_and_is_audited(private_state_home: Path) -> None:
    gateway = flow(ask=group_run(13, stdout=b'{"decision_id": "hook-1"}'))
    gateway.tty = True
    argv = ["--hook", "pre-open=ask", "--override-escalation", "--decision-id", "d-1",
            "--", "true"]  # fmt: skip
    result = run(argv, gateway)
    assert result.code == 0, result.stderr
    out = result.json()
    assert payload(gateway)["override_escalation"] is True
    assert out["decision_ids"] == ["d-1", "hook-1"]
    [record] = open_records(private_state_home)
    assert (record["result"], record["escalation_override"]) == ("ok", True)
    assert record["actor"] == "human:user"
    [audit] = audit_records(private_state_home)
    assert audit == {
        "ts": CREATED_AT, "actor": "human:user", "decision_id": ["d-1", "hook-1"], "target": ID,
        "action": "open", "force": False, "escalation_override": True,
        "inputs": {"socket_path": SOCK, "pid": None, "grace_ms": None, "name": "true",
                   "argv": ["true"], "cwd": "/", "placement": "default", "session": "termctl",
                   "env_names": [], "labels": []},
        "result": out,
    }  # fmt: skip


def test_override_does_not_override_a_veto_and_the_veto_is_audited(
    private_state_home: Path,
) -> None:
    gateway = flow(ask=group_run(13), no=group_run(3))
    argv = ["--actor", "human:pat", "--override-escalation", "--hook", "pre-open=ask",
            "--hook", "pre-open=no", "--", "true"]  # fmt: skip
    result = run(argv, gateway)
    err = error(result, "vetoed", 11)
    no_tmux_creation(gateway)
    [audit] = audit_records(private_state_home)
    assert audit["result"] == result.json()
    assert audit["actor"] == "human:pat"
    assert err["hook"] == "no"


def test_an_override_run_without_hooks_is_still_audited(private_state_home: Path) -> None:
    gateway = flow()
    result = run(["--actor", "human:pat", "--override-escalation", "--", "true"], gateway)
    assert result.code == 0, result.stderr
    [audit] = audit_records(private_state_home)
    assert audit["result"] == result.json()


def test_an_override_run_failing_before_the_id_exists_is_only_logged(
    private_state_home: Path,
) -> None:
    gateway = FakeGateway(panes=[], executables=TRUE)
    argv = ["--actor", "human:pat", "--override-escalation", "--placement", "nope", "--", "true"]
    error(run(argv, gateway), "usage", 2)
    [record] = open_records(private_state_home)
    assert (record["id"], record["escalation_override"]) == (None, True)
    assert audit_records(private_state_home) == []


def test_an_override_run_ending_in_command_not_found_is_logged_with_the_id_and_audited(
    private_state_home: Path,
) -> None:
    gateway = FakeGateway(panes=[])
    argv = ["--actor", "human:pat", "--override-escalation", "--", "no-such-tool"]
    result = run(argv, gateway)
    error(result, "command_not_found", 12)
    [record] = open_records(private_state_home)
    assert (record["id"], record["exit_code"]) == (ID, 12)
    [audit] = audit_records(private_state_home)
    assert (audit["target"], audit["result"]) == (ID, result.json())
    assert audit["inputs"]["argv"] == ["no-such-tool"]


def test_an_override_run_ending_in_tag_failed_is_audited(private_state_home: Path) -> None:
    gateway = existing_session_flow(make_plan(), completed("termctl-nomatch\n"))
    result = run(["--actor", "human:pat", "--override-escalation", "--", "true"], gateway)
    error(result, "tag_failed", 12)
    [audit] = audit_records(private_state_home)
    assert audit["result"] == result.json()
    assert audit["target"] == ID


def test_an_audit_that_cannot_be_written_warns_and_keeps_the_exit_code(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "termctl").mkdir(parents=True, mode=0o700)
    (state / "termctl" / "audit.jsonl").mkdir()
    gateway = flow()
    argv = ["--actor", "human:pat", "--override-escalation", "--", "true"]
    result = run(argv, gateway, {"XDG_STATE_HOME": str(state)})
    assert result.code == 0
    assert "warning: could not write the audit record" in result.stderr
    assert len(open_records(state)) == 1
    [warning] = [r for r in records(state) if r["event"] == "warning"]
    validate(warning, "termctl/log.json")
    assert "could not write the audit record" in warning["message"]
    assert warning["id"] == ID


# --- decision ids -------------------------------------------------------------------------------


def test_decision_ids_from_hooks_follow_the_flag_values_everywhere(
    private_state_home: Path,
) -> None:
    gateway = flow(
        a=group_run(0, stdout=b'{"decision_id": ["a-1", "a-2"]}'),
        b=group_run(0, stdout=b'{"decision_id": "b-1"}'),
        post=group_run(0),
    )
    argv = ["--decision-id", "d-1", "--hook", "pre-open=a", "--hook", "pre-open=b",
            "--hook", "post-open=post", "--", "true"]  # fmt: skip
    result = run(argv, gateway)
    assert result.code == 0, result.stderr
    expected = ["d-1", "a-1", "a-2", "b-1"]
    assert result.json()["decision_ids"] == expected
    # Every pre-open hook sees only the --decision-id values.
    assert payload(gateway, 0)["decision_ids"] == payload(gateway, 1)["decision_ids"] == ["d-1"]
    assert payload(gateway, 2)["pane"]["decision_ids"] == expected
    [record] = open_records(private_state_home)
    assert record["decision_ids"] == expected


def test_a_veto_reports_the_ids_of_every_hook_that_ran() -> None:
    gateway = flow(
        a=group_run(0, stdout=b'{"decision_id": "a-1"}'),
        b=group_run(1, stdout=b'{"decision_id": "b-1"}'),
    )
    argv = ["--decision-id", "d-1", "--hook", "pre-open=a", "--hook", "pre-open=b", "--", "true"]
    err = error(run(argv, gateway), "vetoed", 11)
    assert err["decision_ids"] == ["d-1", "a-1", "b-1"]


def test_every_later_error_carries_the_decision_ids(private_state_home: Path) -> None:
    gateway = existing_session_flow(make_plan(), completed("termctl-nomatch\n"))
    gateway.group_responses[sh("a")] = group_run(0, stdout=b'{"decision_id": "a-1"}')
    argv = ["--decision-id", "d-1", "--hook", "pre-open=a", "--", "true"]
    err = error(run(argv, gateway), "tag_failed", 12)
    assert err["decision_ids"] == ["d-1", "a-1"]
    [record] = open_records(private_state_home)
    assert record["decision_ids"] == ["d-1", "a-1"]


def test_dropped_decision_ids_warn_once_and_get_a_hook_failed_record(
    private_state_home: Path,
) -> None:
    stdout = b'{"decision_id": ["ok-1", 7, "bad id", "ok-2"]}'
    gateway = flow(a=group_run(0, stdout=stdout, pgid=31))
    result = run(["--hook", "pre-open=a", "--", "true"], gateway)
    assert result.code == 0, result.stderr
    assert result.json()["decision_ids"] == ["ok-1", "ok-2"]
    assert result.stderr.count("warning:") == 1
    assert "bad id" not in result.stderr
    [failed] = [r for r in records(private_state_home) if r["event"] == "hook_failed"]
    validate(failed, "termctl/log.json")
    assert (failed["id"], failed["socket_path"], failed["details"]["pgid"]) == (ID, SOCK, 31)


# --- step 9: post-open hooks --------------------------------------------------------------------


def test_post_open_payload_is_the_open_output() -> None:
    gateway = flow(notify=group_run(0))
    result = run(["--hook", "post-open=notify", "--", "true"], gateway)
    assert result.code == 0, result.stderr
    data = payload(gateway)
    validate(data, "termctl/hooks/post-open.json")
    assert data["pane"] == result.json()
    assert (data["object_id"], data["labels"]) == (ID, [])
    call = gateway.group_calls[0]
    assert (call.stdout_limit, call.env["TERMCTL_HOOK"]) == (0, "post-open")
    # After the pane exists: the read for the output came first.
    assert calls(gateway)[-1] == "list-panes"


def test_post_open_top_level_labels_are_the_given_ones_not_the_read_back_ones() -> None:
    plan = make_plan(labels=("k=v",))
    forged = new_row(**{"@termctl_labels": "forged=1 k=v"})
    gateway = existing_session_flow(plan, after=[forged])
    gateway.group_responses[sh("notify")] = group_run(0)
    argv = ["--label", "k=v", "--hook", "post-open=notify", "--", "true"]
    assert run(argv, gateway).code == 0
    data = payload(gateway)
    validate(data, "termctl/hooks/post-open.json")
    assert data["labels"] == ["k=v"]
    assert data["pane"]["labels"] == ["forged=1", "k=v"]


def test_a_pane_gone_before_the_post_open_hooks_is_reported_to_them() -> None:
    gateway = existing_session_flow(make_plan(), after=[])
    gateway.group_responses[sh("notify")] = group_run(0)
    assert run(["--hook", "post-open=notify", "--", "true"], gateway).code == 0
    assert payload(gateway)["pane"]["state"] == "gone"


@pytest.mark.parametrize(
    ("response", "problem"),
    [
        (group_run(3, stderr=b"boom\n"), "exited 3"),
        (group_run(-15), "killed by signal 15"),
        (group_run(None), "timed out after 30 s"),
        (OSError(2, "No such file or directory"), "could not be started"),
    ],
    ids=["exit", "signal", "timeout", "not-started"],
)
def test_a_failed_post_open_hook_warns_and_open_still_succeeds(
    response: Any, problem: str, private_state_home: Path
) -> None:
    gateway = flow(notify=response, later=group_run(0))
    argv = ["--hook", "post-open=notify", "--hook", "post-open=later", "--", "true"]
    result = run(argv, gateway)
    assert result.code == 0, result.stderr
    assert problem in result.stderr
    assert [c.argv[2] for c in gateway.group_calls] == ["notify", "later"]
    [failed] = [r for r in records(private_state_home) if r["event"] == "hook_failed"]
    validate(failed, "termctl/log.json")
    assert problem in failed["details"]["problem"]
    [record] = open_records(private_state_home)
    assert record["result"] == "ok"


def test_post_open_hook_stderr_follows_its_warning_in_human_mode_only() -> None:
    gateway = flow(notify=group_run(1, stderr=b"oops\x07\n"))
    human = Run(["open", "--hook", "post-open=notify", "--", "true"], gateway, ENVIRON)
    assert human.code == 0
    assert human.stderr.splitlines()[-2:] == [
        "termctl: warning: post-open hook 'notify': exited 1",
        "termctl: hook stderr: oops\\x07",
    ]
    gateway = flow(notify=group_run(1, stderr=b"oops\n"))
    machine = run(["--hook", "post-open=notify", "--", "true"], gateway)
    assert "hook stderr" not in machine.stderr and "oops" not in machine.stderr
    gateway = flow(notify=group_run(1, stderr=b"oops\n"))
    quiet = Run(["--quiet", "open", "--hook", "post-open=notify", "--", "true"], gateway, ENVIRON)
    assert quiet.stderr == ""


def test_an_async_post_open_hook_is_started_under_the_watchdog(
    tmp_path: Path, private_state_home: Path
) -> None:
    config = config_file(tmp_path, {"post-open": [{"cmd": "bg", "async": True, "timeout": 9}]})
    gateway = flow(bg=group_run(0))
    argv = ["--json", "--config", config, "open", "--", "true"]
    assert Run(argv, gateway, ENVIRON).code == 0
    [call] = gateway.group_calls
    assert (call.spawned, call.timeout) == (True, 9)
    [record] = open_records(private_state_home)
    assert record["hooks"] == [{"hook": "post-open", "cmd": "bg", "pgid": 4343, "async": True}]


def test_no_post_open_hook_runs_after_tag_failed() -> None:
    gateway = existing_session_flow(make_plan(), completed("termctl-nomatch\n"))
    argv = ["--hook", "post-open=notify", "--", "true"]
    error(run(argv, gateway), "tag_failed", 12)
    assert gateway.group_calls == []


# --- the log record of every outcome ------------------------------------------------------------


def test_a_successful_open_is_logged_with_its_hooks(private_state_home: Path) -> None:
    gateway = flow(gate=group_run(0, pgid=11), notify=group_run(0, pgid=12))
    argv = ["--actor", "auto:ci", "--decision-id", "d-1", "--hook", "pre-open=gate",
            "--hook", "post-open=notify", "--", "true"]  # fmt: skip
    assert run(argv, gateway).code == 0
    [record] = open_records(private_state_home)
    assert record == {
        "ts": CREATED_AT, "level": "info", "event": "open", "id": ID, "socket_path": SOCK,
        "result": "ok", "exit_code": 0, "actor": "auto:ci", "decision_ids": ["d-1"],
        "escalation_override": False,
        "hooks": [{"hook": "pre-open", "cmd": "gate", "pgid": 11, "async": False},
                  {"hook": "post-open", "cmd": "notify", "pgid": 12, "async": False}],
    }  # fmt: skip


def test_an_open_without_hooks_logs_an_empty_hook_list(private_state_home: Path) -> None:
    assert run(["--", "true"], flow()).code == 0
    [record] = open_records(private_state_home)
    assert (record["hooks"], record["actor"]) == ([], "auto:unknown")


def test_a_failure_in_step_2_before_the_id_is_logged_without_one(
    private_state_home: Path,
) -> None:
    gateway = FakeGateway(panes=[], executables=TRUE)
    err = error(run(["--placement", "nope", "--", "true"], gateway), "usage", 2)
    [record] = open_records(private_state_home)
    assert (record["id"], record["socket_path"], record["result"]) == (None, SOCK, "error")
    assert (record["exit_code"], record["message"]) == (2, err["message"])


def test_the_command_check_comes_after_the_id(private_state_home: Path) -> None:
    gateway = FakeGateway(panes=[])
    error(run(["--", "no-such-tool"], gateway), "command_not_found", 12)
    [record] = open_records(private_state_home)
    assert record["id"] == ID


def test_a_failure_in_step_3_is_logged_with_the_id(private_state_home: Path) -> None:
    gateway = FakeGateway({(TMUX, "-V"): completed("tmux 3.1\n")}, panes=[], executables=TRUE)
    error(run(["--", "true"], gateway), "tmux_too_old", 12)
    [record] = open_records(private_state_home)
    assert (record["id"], record["exit_code"]) == (ID, 12)
    assert record["details"]["tmux_version"] == "3.1"


def test_an_exit_2_after_step_1_is_logged(private_state_home: Path) -> None:
    gateway = FakeGateway(panes=[], executables=TRUE)
    error(run(["--", "true", "x" * 12500], gateway), "usage", 2)
    [record] = open_records(private_state_home)
    assert (record["result"], record["exit_code"]) == ("error", 2)


@pytest.mark.parametrize(
    "argv",
    [["--name", "a\nb", "--", "true"], ["--decision-id", "bad id", "--", "true"],
     ["--override-escalation", "--", "true"]],
    ids=["name", "decision-id", "override-gate"],
)  # fmt: skip
def test_usage_errors_are_not_logged(argv: list[str], private_state_home: Path) -> None:
    error(run(argv, FakeGateway(panes=[])), "usage", 2)
    assert records(private_state_home) == []


def test_a_config_error_is_not_logged(tmp_path: Path, private_state_home: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("keep_on_exit: sometimes\n", encoding="utf-8")
    result = Run(["--json", "--config", str(config), "open"], FakeGateway(), ENVIRON)
    error(result, "config_invalid", 2)
    assert records(private_state_home) == []


def test_a_dry_run_is_not_logged_and_runs_no_hooks(private_state_home: Path) -> None:
    gateway = FakeGateway(panes=[], executables=TRUE)
    error(run(["--dry-run", "--hook", "pre-open=gate", "--", "true"], gateway), "internal", 1)
    assert gateway.group_calls == []
    assert records(private_state_home) == []


def test_an_unexpected_exception_is_logged_as_internal(
    monkeypatch: pytest.MonkeyPatch, private_state_home: Path
) -> None:
    from termctl import open_cmd

    def broken(*_: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(open_cmd, "create_window", broken)
    err = error(run(["--", "true"], flow()), "internal", 1)
    [record] = open_records(private_state_home)
    assert (record["exit_code"], record["message"]) == (1, err["message"])


@pytest.mark.parametrize(
    ("stop", "exit_code", "message"),
    [(KeyboardInterrupt(), 130, "interrupted"), (Terminated(15), 143, "terminated by signal 15")],
    ids=["sigint", "sigterm"],
)
def test_an_open_stopped_by_a_signal_in_a_hook_is_logged(
    stop: BaseException, exit_code: int, message: str, private_state_home: Path
) -> None:
    gateway = flow(gate=stop)
    result = run(["--hook", "pre-open=gate", "--", "true"], gateway)
    assert result.code == exit_code
    no_tmux_creation(gateway)
    [record] = open_records(private_state_home)
    assert (record["result"], record["exit_code"], record["message"]) == (
        "error",
        exit_code,
        message,
    )


def test_a_stop_in_a_later_pre_open_hook_keeps_the_earlier_hooks_and_ids(
    private_state_home: Path,
) -> None:
    gateway = flow(
        a=group_run(0, stdout=b'{"decision_id": "a-1"}', pgid=11),
        b=with_pgid(KeyboardInterrupt(), 12),
    )
    argv = ["--decision-id", "d-1", "--hook", "pre-open=a", "--hook", "pre-open=b", "--", "true"]
    assert run(argv, gateway).code == 130
    [record] = open_records(private_state_home)
    assert record["decision_ids"] == ["d-1", "a-1"]
    assert record["hooks"] == [
        {"hook": "pre-open", "cmd": "a", "pgid": 11, "async": False},
        {"hook": "pre-open", "cmd": "b", "pgid": 12, "async": False},
    ]


def test_a_stop_in_a_post_open_hook_keeps_the_watchdog_and_the_interrupted_hook(
    tmp_path: Path, private_state_home: Path
) -> None:
    config = config_file(tmp_path, {"post-open": [{"cmd": "bg", "async": True}]})
    gateway = flow(bg=group_run(0), sync=with_pgid(Terminated(15), 21))
    argv = ["--json", "--config", config, "open", "--hook", "post-open=sync", "--", "true"]
    assert Run(argv, gateway, ENVIRON).code == 143
    [record] = open_records(private_state_home)
    assert (record["exit_code"], record["message"]) == (143, "terminated by signal 15")
    assert record["hooks"] == [
        {"hook": "post-open", "cmd": "bg", "pgid": 4343, "async": True},
        {"hook": "post-open", "cmd": "sync", "pgid": 21, "async": False},
    ]


def test_an_override_run_stopped_in_a_post_open_hook_has_its_audit_record(
    private_state_home: Path,
) -> None:
    gateway = flow(notify=with_pgid(KeyboardInterrupt(), 5))
    argv = ["--actor", "human:pat", "--override-escalation", "--hook", "post-open=notify",
            "--", "true"]  # fmt: skip
    assert run(argv, gateway).code == 130
    [audit] = audit_records(private_state_home)
    validate(audit["result"], "termctl/cli/open.json")
    assert (audit["target"], audit["result"]["id"], audit["result"]["pane_id"]) == (ID, ID, "%5")
    [record] = open_records(private_state_home)
    assert record["exit_code"] == 130


def send(signum: int) -> Any:
    """A fake call's effect: deliver ``signum`` to this process, only with termctl's handler
    installed (it must never reach the test runner)."""

    def effect(_: Any) -> None:
        assert signal.getsignal(signum) not in (signal.SIG_DFL, signal.SIG_IGN)
        assert signal.getsignal(signum) is not signal.default_int_handler
        os.kill(os.getpid(), signum)

    return effect


STOPS = [(signal.SIGINT, "interrupted"), (signal.SIGTERM, "terminated by signal 15"),
         (signal.SIGHUP, "terminated by signal 1")]  # fmt: skip


@pytest.mark.parametrize("step", ["create", "tag"])
@pytest.mark.parametrize(("signum", "message"), STOPS, ids=["int", "term", "hup"])
def test_a_stop_signal_from_window_creation_on_is_deferred_until_the_audit(
    step: str, signum: int, message: str, private_state_home: Path
) -> None:
    plan = make_plan()
    gateway = existing_session_flow(plan)
    gateway.group_responses[sh("notify")] = group_run(0)
    trigger = key(tb.new_window("termctl", plan.window)) if step == "create" else tag_key(plan)
    gateway.effects[trigger] = send(signum)
    before = {s: signal.getsignal(s) for s, _ in STOPS}
    argv = ["--actor", "human:pat", "--override-escalation", "--hook", "post-open=notify",
            "--", "true"]  # fmt: skip
    result = run(argv, gateway)
    assert result.code == 128 + signum, result.stderr
    # The pane was tagged, the output read and audited; no post-open hook ran.
    assert gateway.tag_key in [tuple(c) for c in gateway.calls]
    assert calls(gateway)[-1] == "list-panes"
    assert gateway.group_calls == []
    [audit] = audit_records(private_state_home)
    validate(audit["result"], "termctl/cli/open.json")
    assert audit["result"]["state"] == "running"
    [record] = open_records(private_state_home)
    assert (record["result"], record["exit_code"], record["message"]) == (
        "error",
        128 + signum,
        message,
    )
    assert {s: signal.getsignal(s) for s, _ in STOPS} == before


@pytest.mark.parametrize(("signum", "message"), STOPS[1:], ids=["term", "hup"])
def test_a_stop_signal_outside_a_hook_before_creation_is_logged_only(
    signum: int, message: str, private_state_home: Path
) -> None:
    gateway = flow()
    gateway.effects[(TMUX, "-V")] = send(signum)
    argv = ["--actor", "human:pat", "--override-escalation", "--", "true"]
    assert run(argv, gateway).code == 128 + signum
    no_tmux_creation(gateway)
    [record] = open_records(private_state_home)
    assert (record["exit_code"], record["message"], record["id"]) == (128 + signum, message, ID)
    assert audit_records(private_state_home) == []


def test_a_log_that_cannot_be_written_warns_and_keeps_success(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    result = run(["--", "true"], flow(), {"XDG_STATE_HOME": str(blocker)})
    assert result.code == 0
    assert "warning: could not write the log record" in result.stderr


# --- warnings: printed once and logged ----------------------------------------------------------


def warning_records(state: Path) -> list[Any]:
    found = [r for r in records(state) if r["event"] == "warning"]
    for record in found:
        validate(record, "termctl/log.json")
    return found


def test_the_unknown_human_actor_warns_once_and_is_logged(private_state_home: Path) -> None:
    gateway = flow(gate=group_run(0), notify=group_run(0))
    gateway.tty, gateway.user = True, "not a valid name"
    argv = ["--hook", "pre-open=gate", "--hook", "post-open=notify", "--", "true"]
    result = run(argv, gateway)
    assert result.code == 0, result.stderr
    assert result.stderr.count("human:unknown") == 1
    assert payload(gateway)["actor"] == "human:unknown"
    [warning] = warning_records(private_state_home)
    assert (warning["id"], warning["socket_path"]) == (ID, SOCK)
    assert "human:unknown" in warning["message"]
    assert open_records(private_state_home)[0]["actor"] == "human:unknown"


def test_reader_warnings_become_warning_records(private_state_home: Path) -> None:
    gateway = existing_session_flow(make_plan(), after=[new_row(), INVALID])
    gateway.panes = [INVALID]
    result = run(["--", "true"], gateway)
    assert result.code == 0, result.stderr
    [warning] = warning_records(private_state_home)
    assert "%9" in warning["message"]
    assert warning["level"] == "warn"


def test_pid_warnings_become_warning_records(private_state_home: Path) -> None:
    gateway = existing_session_flow(make_plan(), tagged("4242 0 77\n"))
    gateway.responses[RECORD] = completed(stderr="boom\n", returncode=1)
    gateway.proc_stats[4242] = proc_stat()
    gateway.peer_pids[SOCK] = 77
    assert run(["--", "true"], gateway).code == 0
    [warning] = warning_records(private_state_home)
    assert "could not record the PID" in warning["message"]


def test_a_tag_failed_cleanup_warning_becomes_a_warning_record(private_state_home: Path) -> None:
    gateway = existing_session_flow(make_plan(), completed("termctl-nomatch\n"))
    gateway.responses[key(tb.cleanup_kill("%5", "@3"))] = completed("termctl-nomatch\n")
    error(run(["--", "true"], gateway), "tag_failed", 12)
    [warning] = warning_records(private_state_home)
    assert "@3" in warning["message"]
    assert [r["event"] for r in records(private_state_home)] == ["warning", "open"]


def test_a_tmux_version_warning_becomes_a_warning_record(private_state_home: Path) -> None:
    gateway = flow()
    gateway.responses[(TMUX, "-V")] = completed("tmux next\n")
    result = run(["--", "true"], gateway)
    assert result.code == 0, result.stderr
    [warning] = warning_records(private_state_home)
    assert warning["message"] in result.stderr
    assert (warning["id"], warning["socket_path"]) == (ID, SOCK)


def test_warnings_are_logged_even_with_quiet(private_state_home: Path) -> None:
    gateway = existing_session_flow(make_plan(), after=[new_row(), INVALID])
    gateway.panes = [INVALID]
    result = Run(["--quiet", "open", "--", "true"], gateway, ENVIRON)
    assert (result.code, result.stderr) == (0, "")
    assert len(warning_records(private_state_home)) == 1


def test_reads_by_other_commands_write_no_warning_records(private_state_home: Path) -> None:
    gateway = FakeGateway(panes=[INVALID, pane_row(tc_id=ID)])
    result = Run(["--json", "list"], gateway, ENVIRON)
    assert result.code == 0 and "warning:" in result.stderr
    assert records(private_state_home) == []


# --- golden output ------------------------------------------------------------------------------


def test_golden_open_output_with_a_recorded_pid() -> None:
    recorded = {"@termctl_pid": "4242", "@termctl_pid_start": "823520334", "pane_pid": "4242",
                "@termctl_labels": "team=core"}  # fmt: skip
    plan = make_plan(labels=("team=core",))
    gateway = existing_session_flow(plan, tagged("4242 0 77\n"), after=[new_row(**recorded)])
    gateway.responses[RECORD] = completed()
    gateway.proc_stats[4242] = proc_stat()
    gateway.peer_pids[SOCK] = 77
    gateway.group_responses[sh("gate")] = group_run(0, stdout=b'{"decision_id": "gate-7"}')
    argv = ["--label", "team=core", "--decision-id", "d-1", "--hook", "pre-open=gate", "--",
            "true"]  # fmt: skip
    result = run(argv, gateway)
    assert result.code == 0, result.stderr
    expected = json.loads((GOLDEN / "open-running.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/open.json")
    assert result.json() == expected


def test_golden_open_output_of_a_pane_already_gone() -> None:
    gateway = existing_session_flow(make_plan(), after=[])
    result = run(["--", "true"], gateway)
    assert result.code == 0, result.stderr
    expected = json.loads((GOLDEN / "open-gone.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/open.json")
    assert result.json() == expected


def test_golden_open_vetoed_error() -> None:
    gateway = flow(gate=group_run(1, stdout=b'{"decision_id": "gate-7"}', stderr=b"no\n"))
    result = run(["--decision-id", "d-1", "--hook", "pre-open=gate", "--", "true"], gateway)
    assert result.code == 11
    expected = json.loads((GOLDEN / "error-open-vetoed.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/error.json")
    assert result.json() == expected
