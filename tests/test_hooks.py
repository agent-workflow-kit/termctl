from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from termctl import config, hooks, lock
from termctl.errors import TermctlError, UsageError
from termctl.gateway import GroupRun, SystemGateway
from termctl.hooks import Hook

from .fakes import FakeClock, FakeGateway, group_run
from .schemas import validate

ID = "tc-abcdefgh"
SOCKET = "/tmp/tmux-1000/default"
CALLER_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "SECRETISH": "kept", "TERMCTL_ID": "x"}

PANE: dict[str, Any] = {
    "id": ID,
    "name": "build",
    "display_name": "build",
    "marker": None,
    "marker_text": None,
    "labels": ["kind=review", "repo=x"],
    "placement": "default",
    "session": "termctl",
    "pane_id": "%3",
    "window_id": "@2",
    "socket_path": SOCKET,
    "created": "2026-01-01T00:00:00.000Z",
    "pid": 1234,
    "state": "running",
    "exit_status": None,
    "exit_signal": None,
    "keep_on_exit": "never",
}


def pre_open(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "object_id": ID,
        "labels": ["kind=review"],
        "name": "build",
        "cwd": "/home/u",
        "argv": ["make", "test"],
        "env_names": ["FOO"],
        "placement": "default",
        "session": "termctl",
        "target": "window",
        "socket_path": SOCKET,
        "keep_on_exit": "never",
        "focus": False,
        "actor": "auto:policy",
        "decision_ids": [],
        "override_escalation": False,
    }
    fields.update(overrides)
    return hooks.pre_open_payload(**fields)


def sh(cmd: str) -> tuple[str, ...]:
    return ("/bin/sh", "-c", cmd)


def pre(*cmds: str, timeout: int = 30) -> list[Hook]:
    return [Hook("pre-open", cmd, timeout) for cmd in cmds]


def gateway_for(responses: dict[str, Any]) -> FakeGateway:
    gateway = FakeGateway()
    gateway.group_responses = {sh(cmd): response for cmd, response in responses.items()}
    return gateway


def run_pre(
    gateway: FakeGateway,
    hook_list: list[Hook],
    *,
    payload: dict[str, Any] | None = None,
    decision_ids: list[str] | None = None,
    override: bool = False,
) -> hooks.PreHookOutcome:
    if payload is None:
        payload = pre_open(decision_ids=decision_ids or [], override_escalation=override)
    return hooks.run_pre_hooks(
        hook_list,
        payload,
        decision_ids=decision_ids or [],
        override_escalation=override,
        gateway=gateway,
        environ=CALLER_ENV,
    )


def ran(gateway: FakeGateway) -> list[str]:
    return [call.argv[-1] for call in gateway.group_calls]


# --- --hook flags -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "value", "expected"),
    [
        ("open", "pre-open=gate check", Hook("pre-open", "gate check", 30)),
        ("open", "post-open=a=b", Hook("post-open", "a=b", 30)),
        ("close", "pre-close=x", Hook("pre-close", "x", 30)),
        ("close", "post-close= x ", Hook("post-close", " x ", 30)),
    ],
)
def test_flag_hooks_parse_with_fixed_timeout_and_never_async(
    command: str, value: str, expected: Hook
) -> None:
    assert hooks.parse_flag_hooks(command, [value]) == [expected]
    assert expected.async_ is False


@pytest.mark.parametrize(
    ("command", "value"),
    [
        ("open", "pre-close=x"),
        ("open", "post-close=x"),
        ("close", "pre-open=x"),
        ("open", "on-open=x"),
        ("open", "PRE-OPEN=x"),
        ("open", " pre-open=x"),
        ("open", "pre-open"),
        ("open", "=x"),
        ("open", "pre-open="),
        ("open", "pre-open=bad\udcff"),
        ("open", "pre-open\x1b[31m=x"),
    ],
)
def test_flag_hooks_reject_bad_values(command: str, value: str) -> None:
    with pytest.raises(UsageError) as info:
        hooks.parse_flag_hooks(command, [value])
    assert "\x1b" not in info.value.message


def test_flag_hooks_keep_their_order() -> None:
    parsed = hooks.parse_flag_hooks("open", ["post-open=a", "pre-open=b", "pre-open=c"])
    assert [(h.name, h.cmd) for h in parsed] == [
        ("post-open", "a"),
        ("pre-open", "b"),
        ("pre-open", "c"),
    ]


def test_hooks_for_runs_configured_hooks_then_flag_hooks(tmp_path: Path) -> None:
    (tmp_path / "termctl").mkdir()
    (tmp_path / "termctl" / "config.yaml").write_text(
        "hooks:\n"
        "  pre-open: [{cmd: c1, timeout: 600}, {cmd: c2}]\n"
        "  post-open: [{cmd: p1, async: true}]\n"
    )
    cfg = config.load({"XDG_CONFIG_HOME": str(tmp_path)})
    flags = hooks.parse_flag_hooks("open", ["pre-open=f1", "post-open=f2"])
    assert hooks.hooks_for(cfg, "pre-open", flags) == [
        Hook("pre-open", "c1", 600),
        Hook("pre-open", "c2", 30),
        Hook("pre-open", "f1", 30),
    ]
    assert hooks.hooks_for(cfg, "post-open", flags) == [
        Hook("post-open", "p1", 30, async_=True),
        Hook("post-open", "f2", 30),
    ]
    assert hooks.hooks_for(cfg, "pre-close", []) == []


# --- the --override-escalation gate -------------------------------------------------------------


@pytest.mark.parametrize(
    ("actor", "tty"),
    [(None, True), ("human:alice", False), ("human:alice", True), ("auto:policy", True)],
)
def test_override_gate_accepts_a_tty_or_a_human_actor(actor: str | None, tty: bool) -> None:
    hooks.check_override_gate(True, actor=actor, stdin_is_tty=tty)


@pytest.mark.parametrize("actor", [None, "auto:policy", "auto:human"])
def test_override_gate_refuses_automation(actor: str | None) -> None:
    with pytest.raises(UsageError, match="override-escalation"):
        hooks.check_override_gate(True, actor=actor, stdin_is_tty=False)


def test_override_gate_does_nothing_without_the_flag() -> None:
    hooks.check_override_gate(False, actor=None, stdin_is_tty=False)


# --- payloads -----------------------------------------------------------------------------------


def test_pre_open_payload_matches_its_schema() -> None:
    payload = pre_open(decision_ids=["d-1"], override_escalation=True)
    validate(payload, "termctl/hooks/pre-open.json")
    assert payload["tool"] == "termctl"
    assert payload["hook"] == "pre-open"
    assert payload["object_type"] == "pane"
    assert payload["object_id"] == ID
    assert payload["override_escalation"] is True


def test_pre_close_payload_matches_its_schema() -> None:
    payload = hooks.pre_close_payload(
        pane=PANE,
        force=True,
        grace_ms=10000,
        actor="human:alice",
        decision_ids=["d-1"],
        override_escalation=False,
    )
    validate(payload, "termctl/hooks/pre-close.json")
    assert (payload["object_id"], payload["labels"]) == (ID, PANE["labels"])


def test_post_open_payload_matches_its_schema() -> None:
    output = {**PANE, "focused": False, "decision_ids": ["d-1"]}
    payload = hooks.post_open_payload(pane=output)
    validate(payload, "termctl/hooks/post-open.json")
    assert payload["pane"] == output


def test_post_close_payload_matches_its_schema() -> None:
    result = {
        "id": ID,
        "pane_closed": True,
        "was_running": False,
        "forced": False,
        "signals_sent": [],
        "exited": True,
        "pid_mismatch": False,
        "decision_ids": [],
        "duration_ms": 5,
        "socket_path": SOCKET,
    }
    payload = hooks.post_close_payload(result=result, labels=["kind=review"])
    validate(payload, "termctl/hooks/post-close.json")
    assert payload["object_id"] == ID


# --- pre-hooks: order and verdicts --------------------------------------------------------------


def test_pre_hooks_run_in_order_and_allow() -> None:
    gateway = gateway_for({"a": group_run(0), "b": group_run(0), "c": group_run(0)})
    outcome = run_pre(gateway, pre("a", "b", "c"))
    assert ran(gateway) == ["a", "b", "c"]
    assert outcome.verdict == "allow"
    outcome.check()


def test_a_veto_stops_later_hooks_and_exits_11() -> None:
    gateway = gateway_for({"a": group_run(0), "b": group_run(1, stderr=b"no way\n")})
    outcome = run_pre(gateway, pre("a", "b", "c"))
    assert ran(gateway) == ["a", "b"]
    assert outcome.verdict == "vetoed"
    with pytest.raises(TermctlError) as info:
        outcome.check()
    err = info.value
    assert (err.code, err.exit_code) == ("vetoed", 11)
    assert (err.hook, err.hook_stderr) == ("b", "no way\n")
    assert "no way" in err.message
    validate(err.envelope(), "termctl/cli/error.json")


def test_an_escalation_lets_later_hooks_run_and_exits_13() -> None:
    gateway = gateway_for(
        {"a": group_run(13, stderr=b"ask a human"), "b": group_run(0), "c": group_run(13)}
    )
    outcome = run_pre(gateway, pre("a", "b", "c"))
    assert ran(gateway) == ["a", "b", "c"]
    assert outcome.verdict == "escalated"
    with pytest.raises(TermctlError) as info:
        outcome.check()
    err = info.value
    assert (err.code, err.exit_code) == ("escalated", 13)
    # The first hook that escalated is reported.
    assert (err.hook, err.hook_stderr) == ("a", "ask a human")
    validate(err.envelope(), "termctl/cli/error.json")


def test_a_veto_after_an_escalation_wins() -> None:
    gateway = gateway_for({"a": group_run(13), "b": group_run(2, stderr=b"denied")})
    outcome = run_pre(gateway, pre("a", "b", "c"))
    assert ran(gateway) == ["a", "b"]
    assert (outcome.verdict, outcome.hook, outcome.hook_stderr) == ("vetoed", "b", "denied")


@pytest.mark.parametrize("returncode", [1, 2, 12, 14, 126, 127, 255, -9, -15])
def test_any_other_non_zero_exit_is_a_veto(returncode: int) -> None:
    gateway = gateway_for({"a": group_run(returncode), "b": group_run(0)})
    outcome = run_pre(gateway, pre("a", "b"))
    assert (outcome.verdict, ran(gateway)) == ("vetoed", ["a"])


def test_a_timeout_counts_as_a_veto() -> None:
    gateway = gateway_for({"slow": group_run(None, stderr=b"partial"), "b": group_run(0)})
    outcome = run_pre(gateway, pre("slow", "b", timeout=7))
    assert ran(gateway) == ["slow"]
    assert gateway.group_calls[0].timeout == 7
    assert (outcome.verdict, outcome.hook, outcome.hook_stderr) == ("vetoed", "slow", "partial")
    assert outcome.message is not None and "timed out after 7 s" in outcome.message


def test_a_hook_that_cannot_start_counts_as_a_veto() -> None:
    gateway = gateway_for({"a": OSError(2, "No such file or directory"), "b": group_run(0)})
    outcome = run_pre(gateway, pre("a", "b"))
    assert (outcome.verdict, outcome.hook, outcome.hook_stderr) == ("vetoed", "a", "")
    assert ran(gateway) == ["a"]


def test_no_hooks_allow() -> None:
    outcome = run_pre(FakeGateway(), [])
    assert (outcome.verdict, outcome.decision_ids) == ("allow", [])


# --- the escalation override --------------------------------------------------------------------


def test_override_treats_an_escalation_as_allow() -> None:
    gateway = gateway_for({"a": group_run(13), "b": group_run(0)})
    outcome = run_pre(gateway, pre("a", "b"), override=True)
    assert outcome.verdict == "allow"
    assert outcome.overridden == ["a"]
    outcome.check()


def test_override_never_bypasses_a_veto() -> None:
    gateway = gateway_for({"a": group_run(13), "b": group_run(1)})
    outcome = run_pre(gateway, pre("a", "b"), override=True)
    assert (outcome.verdict, outcome.hook) == ("vetoed", "b")


def test_override_payload_must_say_so() -> None:
    gateway = gateway_for({"a": group_run(0)})
    with pytest.raises(ValueError, match="override_escalation"):
        run_pre(gateway, pre("a"), payload=pre_open(override_escalation=False), override=True)
    assert gateway.group_calls == []


# --- execution: argv, stdin and environment -----------------------------------------------------


def test_hooks_run_through_sh_with_the_payload_and_environment() -> None:
    gateway = gateway_for({"echo $HOME; rm -rf x": group_run(0)})
    payload = pre_open()
    run_pre(gateway, pre("echo $HOME; rm -rf x"), payload=payload)
    (call,) = gateway.group_calls
    assert call.argv == ["/bin/sh", "-c", "echo $HOME; rm -rf x"]
    assert json.loads(call.stdin) == payload
    assert call.env == {
        **CALLER_ENV,
        "TERMCTL_HOOK": "pre-open",
        "TERMCTL_ID": ID,
        "TERMCTL_OBJECT_TYPE": "pane",
    }
    assert (call.stdout_limit, call.stderr_limit) == (hooks.STDOUT_LIMIT, hooks.STDERR_LIMIT)


def test_payload_is_ascii_json_without_lone_surrogates() -> None:
    gateway = gateway_for({"a": group_run(0)})
    run_pre(gateway, pre("a"), payload=pre_open(cwd="/home/\udcffé", argv=["a\x00b"]))
    data = gateway.group_calls[0].stdin
    data.decode("ascii")
    assert json.loads(data)["cwd"] == "/home/\\xffé"


def test_payload_hook_must_match_the_hooks() -> None:
    gateway = gateway_for({"a": group_run(0)})
    with pytest.raises(ValueError):
        run_pre(gateway, [Hook("pre-close", "a", 30)])
    with pytest.raises(ValueError):
        hooks.run_post_hooks(pre("a"), pre_open(), gateway=gateway, environ={})
    assert gateway.group_calls == []


def test_hooks_never_run_while_the_server_lock_is_held(tmp_path: Path) -> None:
    gateway = gateway_for({"a": group_run(0)})
    environ = {"XDG_STATE_HOME": str(tmp_path / "state")}
    with lock.server_lock(SOCKET, environ=environ, clock=FakeClock()):
        with pytest.raises(RuntimeError, match="lock"):
            run_pre(gateway, pre("a"))
        with pytest.raises(RuntimeError, match="lock"):
            hooks.run_post_hooks(
                [Hook("post-open", "a", 30)],
                hooks.post_open_payload(pane={**PANE, "focused": False, "decision_ids": []}),
                gateway=gateway,
                environ={},
            )
    assert gateway.group_calls == []
    run_pre(gateway, pre("a"))
    assert ran(gateway) == ["a"]


# --- hook stderr --------------------------------------------------------------------------------


def test_hook_stderr_is_cut_at_a_utf8_boundary() -> None:
    # 4095 ASCII bytes and the first byte of a two-byte character fill the 4096-byte limit.
    stderr = b"x" * 4095 + "é".encode()[:1]
    gateway = gateway_for({"a": GroupRun(1, 1, b"", stderr, False, True)})
    outcome = run_pre(gateway, pre("a"))
    assert outcome.hook_stderr == "x" * 4095


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("é".encode(), "é"),
        ("€".encode(), "€"),
        ("€".encode()[:2], ""),
        ("😀".encode(), "😀"),
        ("😀".encode()[:3], ""),
        (b"ok" + "😀".encode()[:1], "ok"),
        (b"\xff\xfe", "��"),
        (b"a\x1b[31mb", "a\x1b[31mb"),
    ],
)
def test_cut_utf8(raw: bytes, expected: str) -> None:
    assert hooks.cut_stderr(raw) == expected


def test_hook_stderr_control_characters_are_escaped_in_human_output() -> None:
    from termctl.output import Output

    gateway = gateway_for({"a": group_run(1, stderr=b"\x1b]0;pwned\x07")})
    outcome = run_pre(gateway, pre("a"))
    with pytest.raises(TermctlError) as info:
        outcome.check()
    import io

    out = Output(stdout=io.StringIO(), stderr=io.StringIO())
    out.error(info.value)
    printed = out.stderr.getvalue()  # type: ignore[attr-defined]
    assert "\x1b" not in printed and "\x07" not in printed
    assert "\\x1b]0;pwned\\x07" in printed


# --- decision ids -------------------------------------------------------------------------------


def stdout_ids(*stdouts: bytes, given: list[str] | None = None) -> hooks.PreHookOutcome:
    cmds = [f"h{index}" for index in range(len(stdouts))]
    gateway = gateway_for(
        {cmd: group_run(0, stdout=out) for cmd, out in zip(cmds, stdouts, strict=True)}
    )
    return run_pre(gateway, pre(*cmds), decision_ids=given)


def test_decision_ids_are_collected_after_the_given_ones() -> None:
    outcome = stdout_ids(
        b'{"decision_id": "gate:1"}',
        b'{"decision_id": ["a/b", "c@d"], "other": 1}\n',
        given=["given-1"],
    )
    assert outcome.decision_ids == ["given-1", "gate:1", "a/b", "c@d"]
    assert outcome.warnings == []


@pytest.mark.parametrize("stdout", [b"", b"  \n", b'{"other": 1}', b'{"decision_id": null}'])
def test_no_decision_ids_and_no_warning(stdout: bytes) -> None:
    outcome = stdout_ids(stdout)
    assert (outcome.decision_ids, outcome.warnings) == ([], [])


@pytest.mark.parametrize(
    "bad",
    ["", "has space", "x" * 129, "semi;colon", "new\nline", "é", "a\x00", "$(rm)"],
)
def test_invalid_decision_ids_are_dropped_with_a_warning(bad: str) -> None:
    outcome = stdout_ids(json.dumps({"decision_id": ["ok-1", bad, "ok-2"]}).encode())
    assert outcome.decision_ids == ["ok-1", "ok-2"]
    (warning,) = outcome.warnings
    assert warning.hook == "pre-open" and warning.cmd == "h0"
    assert "decision id" in warning.problem
    assert "\n" not in warning.problem and "\x00" not in warning.problem


def test_decision_ids_beyond_32_are_dropped_with_a_warning() -> None:
    given = [f"g{index}" for index in range(30)]
    outcome = stdout_ids(
        json.dumps({"decision_id": ["h1", "h2", "h3"]}).encode(),
        json.dumps({"decision_id": "h4"}).encode(),
        given=given,
    )
    assert outcome.decision_ids == [*given, "h1", "h2"]
    assert len(outcome.warnings) == 2
    assert all("32" in warning.problem for warning in outcome.warnings)


@pytest.mark.parametrize(
    "stdout",
    [
        b"not json",
        b"[1, 2]",
        b'"just a string"',
        b'{"decision_id": 5}',
        b'{"decision_id": {"a": 1}}',
        b'{"decision_id": true}',
        b'{"decision_id": "a", "decision_id": "b"}',
        b'{"decision_id": NaN}',
        b'{"decision_id": "a"} trailing',
        b"\xff\xfe{}",
        b"[" * 100000 + b"]" * 100000,
    ],
)
def test_unusable_stdout_is_ignored_with_a_warning(stdout: bytes) -> None:
    outcome = stdout_ids(stdout)
    assert outcome.decision_ids == []
    assert len(outcome.warnings) == 1
    assert outcome.verdict == "allow"


def test_non_string_items_are_dropped_with_a_warning() -> None:
    outcome = stdout_ids(b'{"decision_id": ["a", 1, null, ["b"], "c"]}')
    assert outcome.decision_ids == ["a", "c"]
    assert len(outcome.warnings) == 3


def test_truncated_stdout_is_ignored_with_a_warning() -> None:
    gateway = gateway_for({"a": GroupRun(1, 0, b'{"decision_id": "x"}', b"", True, False)})
    outcome = run_pre(gateway, pre("a"))
    assert outcome.decision_ids == []
    (warning,) = outcome.warnings
    assert str(hooks.STDOUT_LIMIT) in warning.problem


def test_vetoing_and_escalating_hooks_return_decision_ids() -> None:
    gateway = gateway_for(
        {
            "a": group_run(13, stdout=b'{"decision_id": "esc-1"}'),
            "b": group_run(1, stdout=b'{"decision_id": "deny-1"}'),
        }
    )
    outcome = run_pre(gateway, pre("a", "b"), decision_ids=["given"])
    assert outcome.decision_ids == ["given", "esc-1", "deny-1"]
    with pytest.raises(TermctlError) as info:
        outcome.check()
    assert info.value.decision_ids == ["given", "esc-1", "deny-1"]


def test_a_timed_out_hook_returns_no_decision_ids() -> None:
    gateway = gateway_for({"a": group_run(None, stdout=b'{"decision_id": "late"}')})
    outcome = run_pre(gateway, pre("a"))
    assert outcome.decision_ids == []


def test_hook_failed_record_matches_the_log_schema() -> None:
    outcome = stdout_ids(b'{"decision_id": "bad id"}')
    (warning,) = outcome.warnings
    record = warning.log_record(FakeClock().now(), id=ID, socket_path=SOCKET)
    validate(record, "termctl/log.json")
    assert (record["event"], record["result"], record["level"]) == (
        "hook_failed",
        "warning",
        "warn",
    )
    assert record["details"]["hook"] == "pre-open"
    assert record["details"]["cmd"] == "h0"


# --- post-hooks ---------------------------------------------------------------------------------


def post_payload() -> dict[str, Any]:
    return hooks.post_open_payload(pane={**PANE, "focused": False, "decision_ids": []})


def test_post_hooks_run_in_order_and_failures_only_warn() -> None:
    gateway = gateway_for(
        {
            "a": group_run(1, stderr=b"oops"),
            "b": group_run(None),
            "c": OSError(13, "Permission denied"),
            "d": group_run(0, stdout=b'{"decision_id": "ignored"}'),
        }
    )
    post = [Hook("post-open", cmd, 5) for cmd in "abcd"]
    warnings = hooks.run_post_hooks(post, post_payload(), gateway=gateway, environ=CALLER_ENV)
    assert ran(gateway) == ["a", "b", "c", "d"]
    assert [w.cmd for w in warnings] == ["a", "b", "c"]
    assert "exited 1" in warnings[0].problem and "oops" in warnings[0].problem
    assert "timed out after 5 s" in warnings[1].problem
    assert "Permission denied" in warnings[2].problem
    call = gateway.group_calls[3]
    assert call.env["TERMCTL_HOOK"] == "post-open"
    assert call.stdout_limit == 0


def test_async_post_hooks_are_spawned_detached() -> None:
    gateway = gateway_for({"bg": group_run(0), "bad": OSError(2, "missing")})
    post = [Hook("post-open", "bg", 30, async_=True), Hook("post-open", "bad", 30, async_=True)]
    warnings = hooks.run_post_hooks(post, post_payload(), gateway=gateway, environ=CALLER_ENV)
    assert [call.spawned for call in gateway.group_calls] == [True, True]
    assert json.loads(gateway.group_calls[0].stdin) == post_payload()
    assert gateway.group_calls[0].env["TERMCTL_ID"] == ID
    assert [w.cmd for w in warnings] == ["bad"]


def test_pre_hooks_are_never_async() -> None:
    gateway = gateway_for({"a": group_run(0)})
    run_pre(gateway, [Hook("pre-open", "a", 30, async_=True)])
    assert [call.spawned for call in gateway.group_calls] == [False]


# --- real processes -----------------------------------------------------------------------------


def real_pre(hook_list: list[Hook], payload: dict[str, Any], environ: dict[str, str]) -> Any:
    return hooks.run_pre_hooks(
        hook_list,
        payload,
        decision_ids=[],
        override_escalation=False,
        gateway=SystemGateway(),
        environ=environ,
    )


def test_real_hook_gets_payload_on_stdin_and_variables(tmp_path: Path) -> None:
    out = tmp_path / "payload.json"
    env_out = tmp_path / "env"
    cmd = (
        f"cat > '{out}'; "
        f'printf "%s|%s|%s|%s" "$TERMCTL_HOOK" "$TERMCTL_ID" "$TERMCTL_OBJECT_TYPE" "$KEEP" '
        f"> '{env_out}'"
    )
    payload = pre_open()
    environ = {**os.environ, "KEEP": "caller", "TERMCTL_HOOK": "spoofed"}
    outcome = real_pre(pre(cmd), payload, environ)
    assert outcome.verdict == "allow"
    received = json.loads(out.read_bytes())
    validate(received, "termctl/hooks/pre-open.json")
    assert received == payload
    assert env_out.read_text() == f"pre-open|{ID}|pane|caller"


def test_real_hooks_run_in_order_and_a_veto_stops_them(tmp_path: Path) -> None:
    log = tmp_path / "order"
    cmds = [f"echo {name} >> '{log}'; exit {code}" for name, code in [("a", 0), ("b", 13)]]
    cmds += [f"echo c >> '{log}'; echo 'c says no' >&2; exit 1", f"echo d >> '{log}'"]
    outcome = real_pre(pre(*cmds), pre_open(), dict(os.environ))
    assert log.read_text() == "a\nb\nc\n"
    assert (outcome.verdict, outcome.hook_stderr) == ("vetoed", "c says no\n")


def test_real_timeout_vetoes_and_kills_the_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "survived"
    cmd = f"(sleep 2; touch '{marker}') & sleep 30"
    outcome = real_pre(pre(cmd, timeout=1), pre_open(), dict(os.environ))
    assert outcome.verdict == "vetoed"
    assert outcome.message is not None and "timed out" in outcome.message
    import time

    time.sleep(2.5)
    assert not marker.exists()


def test_real_hook_does_not_inherit_unrelated_descriptors(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    try:
        out = tmp_path / "fds"
        outcome = real_pre(
            pre(f"ls /proc/$$/fd > '{out}' 2>/dev/null || echo none > '{out}'"),
            pre_open(),
            dict(os.environ),
        )
        assert outcome.verdict == "allow"
        listed = out.read_text().split()
        if listed != ["none"]:
            assert str(write_fd) not in listed
    finally:
        os.close(read_fd)
        os.close(write_fd)
