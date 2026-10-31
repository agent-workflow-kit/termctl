"""`focus` through the check order, against a fake tmux (interface.md `focus`, "Audit and log").
Real-tmux end-to-end tests are in test_cli_focus_integration.py."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from termctl import cli, focus_cmd
from termctl import tmux_backend as tb

from .fakes import TMUX, UID, Execed, FakeGateway, completed, pane_row
from .schemas import validate
from .test_cli import ENVIRON, GOLDEN, Run

SOCK = f"/tmp/tmux-{UID}/default"
ID = "tc-abcdefgh"
# The pane: %3 in window @2 of session $1 ("termctl").
PANE = pane_row()


def key(invocation: tb.Invocation) -> tuple[str, ...]:
    return tuple(invocation.argv(TMUX, SOCK))


LIST_CLIENTS = key(tb.list_clients())


def clients(*lines: str) -> subprocess.CompletedProcess[str]:
    return completed("".join(line + "\n" for line in lines))


def switch(client: str, pane_id: str = "%3") -> tuple[str, ...]:
    return key(tb.switch_client(client, pane_id))


def gateway(
    client_lines: Sequence[str] = (),
    *,
    switched: Sequence[str] = (),
    rows: Sequence[Mapping[str, str]] | None = None,
    extra: Mapping[tuple[str, ...], Any] | None = None,
    **kwargs: Any,
) -> FakeGateway:
    """A server with ``PANE`` and the clients ``client_lines``; ``switched`` are the clients
    a switch to %3 may be scripted for (any other switch is an unexpected call)."""
    responses: dict[tuple[str, ...], Any] = {LIST_CLIENTS: clients(*client_lines)}
    responses.update({switch(name): completed() for name in switched})
    responses.update(extra or {})
    return FakeGateway(responses, panes=[PANE] if rows is None else rows, **kwargs)


def run(argv: list[str], gw: FakeGateway, environ: Mapping[str, str] | None = None) -> Run:
    return Run(["focus", ID, *argv], gw, environ={**ENVIRON, **(environ or {})})


def run_json(argv: list[str], gw: FakeGateway, environ: Mapping[str, str] | None = None) -> Run:
    return Run(["--json", "focus", ID, *argv], gw, environ={**ENVIRON, **(environ or {})})


def output(result: Run) -> Any:
    assert result.code == 0, result.stdout + result.stderr
    out = result.json()
    validate(out, "termctl/cli/focus.json")
    return out


def error(result: Run, code: str, exit_code: int) -> Any:
    assert result.code == exit_code, result.stdout + result.stderr
    envelope = result.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == code
    return envelope["error"]


def records(state: Path) -> list[Any]:
    path = state / "termctl" / "log.jsonl"
    if not path.exists():
        return []
    found = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for record in found:
        validate(record, "termctl/log.json")
    return found


def switches(gw: FakeGateway) -> list[list[str]]:
    return [call for call in gw.calls if "switch-client" in call]


# The step 1 query: every pane with its session (is the caller's pane in its session?).
CALLER = key(tb.pane_sessions())


def displays(gw: FakeGateway) -> list[list[str]]:
    """The step 1 queries made."""
    return [call for call in gw.calls if tuple(call) == CALLER]


# Inside tmux on the resolved server, in pane %9 of session $4.
INSIDE = {"TMUX": f"{SOCK},123,4", "TMUX_PANE": "%9"}


# --- step 3: any client, preferring the pane's session ------------------------------------------


def test_switches_the_most_recent_client_on_the_panes_session(private_state_home: Path) -> None:
    gw = gateway(
        [
            "/dev/pts/1 100 $1 @0",
            "/dev/pts/2 300 $1 @5",
            "/dev/pts/3 900 $2 @7",  # more recent, but on another session
        ],
        switched=["/dev/pts/2"],
    )
    out = output(run_json([], gw))
    assert out == {
        "id": ID, "action": "switched", "client": "/dev/pts/2", "dry_run": False,
        "socket_path": SOCK, "session": "termctl", "window_id": "@2", "pane_id": "%3",
    }  # fmt: skip
    # Always a named client, -E, and the pane id.
    assert switches(gw) == [[TMUX, "-S", SOCK, "switch-client", "-E", "-c", "/dev/pts/2",
                             "-t", "%3"]]  # fmt: skip
    [record] = records(private_state_home)
    assert (record["event"], record["result"], record["action"], record["id"]) == (
        "focus", "ok", "switched", ID,
    )  # fmt: skip
    assert record["socket_path"] == SOCK


def test_without_a_client_on_the_panes_session_the_most_recent_one_is_switched() -> None:
    gw = gateway(["/dev/pts/1 100 $4 @0", "/dev/pts/3 900 $2 @7"], switched=["/dev/pts/3"])
    assert output(run_json([], gw))["client"] == "/dev/pts/3"


def test_a_linked_window_prefers_a_client_on_any_of_its_sessions() -> None:
    rows = [
        pane_row(overrides={"session_id": "$1"}),
        pane_row(overrides={"session_id": "$6", "session_name": "b"}),
    ]
    gw = gateway(
        ["/dev/pts/1 100 $6 @0", "/dev/pts/3 900 $2 @7"], switched=["/dev/pts/1"], rows=rows
    )
    assert output(run_json([], gw))["client"] == "/dev/pts/1"


def test_a_tie_in_activity_takes_the_first_client_tmux_lists() -> None:
    gw = gateway(["/dev/pts/7 5 $1 @0", "/dev/pts/2 5 $1 @0"], switched=["/dev/pts/7"])
    assert output(run_json([], gw))["client"] == "/dev/pts/7"


def test_clients_termctl_cannot_name_are_never_switched() -> None:
    gw = gateway(["bad;name 900 $1 @2", "/dev/pts/2 100 $3 @0"], switched=["/dev/pts/2"])
    assert output(run_json([], gw))["client"] == "/dev/pts/2"


def test_human_output_is_the_client() -> None:
    gw = gateway(["/dev/pts/2 300 $1 @5"], switched=["/dev/pts/2"])
    result = run([], gw)
    assert (result.code, result.stdout) == (0, "/dev/pts/2\n")


def test_a_dead_pane_is_focused_like_a_running_one() -> None:
    rows = [pane_row(overrides={"pane_dead": "1", "pane_dead_status": "1"})]
    gw = gateway(["/dev/pts/2 300 $1 @5"], switched=["/dev/pts/2"], rows=rows)
    assert output(run_json([], gw))["action"] == "switched"


# --- step 4: no client --------------------------------------------------------------------------


@pytest.mark.parametrize("lines", [[], ["/dev/pts/4 1  "], ["x;y 1 $1 @2"]])
def test_no_client_exits_12_and_changes_nothing(lines: list[str], private_state_home: Path) -> None:
    gw = gateway(lines)
    err = error(run_json([], gw), "no_client", 12)
    assert err["details"] == {"socket_path": SOCK}
    assert switches(gw) == []
    [record] = records(private_state_home)
    assert (record["result"], record["exit_code"], record["action"]) == ("error", 12, "switched")


# --- step 1: inside tmux on the same server -----------------------------------------------------


def test_step_1_switches_a_client_on_the_callers_session_viewing_the_window() -> None:
    gw = gateway(
        [
            "/dev/pts/1 500 $4 @8",
            "/dev/pts/2 100 $4 @2",  # viewing the pane's window
            "/dev/pts/3 900 $1 @2",  # on the pane's session, but not the caller's
        ],
        switched=["/dev/pts/2"],
        extra={CALLER: completed("%9 $4\n")},
    )
    assert output(run_json([], gw, INSIDE))["client"] == "/dev/pts/2"
    assert displays(gw) == [list(CALLER)]


def test_step_1_takes_the_most_recent_client_of_the_session_when_none_views_the_window() -> None:
    gw = gateway(
        ["/dev/pts/1 500 $4 @8", "/dev/pts/2 700 $4 @9", "/dev/pts/3 900 $1 @2"],
        switched=["/dev/pts/2"],
        extra={CALLER: completed("%9 $4\n")},
    )
    assert output(run_json([], gw, INSIDE))["client"] == "/dev/pts/2"


def test_step_1_with_no_client_on_the_callers_session_continues_at_step_3() -> None:
    gw = gateway(
        ["/dev/pts/1 500 $5 @8", "/dev/pts/3 100 $1 @2"],
        switched=["/dev/pts/3"],
        extra={CALLER: completed("%9 $4\n")},
    )
    assert output(run_json([], gw, INSIDE))["client"] == "/dev/pts/3"


GONE_ANSWERS = [
    completed(""),
    completed(" \n"),
    completed("%8 $4\n"),
]


@pytest.mark.parametrize("answer", GONE_ANSWERS)
def test_a_caller_pane_that_is_gone_continues_at_step_3(answer: Any) -> None:
    gw = gateway(["/dev/pts/3 100 $1 @2"], switched=["/dev/pts/3"], extra={CALLER: answer})
    assert output(run_json([], gw, INSIDE))["client"] == "/dev/pts/3"


def test_grouped_sessions_take_the_callers_session_from_tmux() -> None:
    """The caller's window is in $4 and $6 (grouped sessions); TMUX names $6, so the client on
    $6 is switched, whichever session tmux would report for the pane."""
    gw = gateway(
        ["/dev/pts/1 900 $4 @2", "/dev/pts/2 100 $6 @8"],
        switched=["/dev/pts/2"],
        extra={CALLER: completed("%1 $1\n%9 $4\n%9 $6\n")},
    )
    environ = {"TMUX": f"{SOCK},123,6", "TMUX_PANE": "%9"}
    assert output(run_json([], gw, environ))["client"] == "/dev/pts/2"


def test_a_session_that_does_not_hold_the_callers_pane_continues_at_step_3() -> None:
    gw = gateway(
        ["/dev/pts/1 900 $4 @8", "/dev/pts/3 100 $1 @2"],
        switched=["/dev/pts/3"],
        extra={CALLER: completed("%9 $5\n%4 $4\n")},
    )
    assert output(run_json([], gw, INSIDE))["client"] == "/dev/pts/3"


@pytest.mark.parametrize(
    "tmux",
    [
        f"{SOCK},123",
        f"{SOCK},123,",
        f"{SOCK},123,x",
        f"{SOCK},123,-1",
        f"{SOCK},123,$4",
        f"{SOCK},123,04",
        f"{SOCK},123, 4",
        f"{SOCK},123,4,5",
        f"{SOCK},123," + "1" * 21,
    ],
)
def test_an_invalid_session_field_in_tmux_skips_step_1_and_never_attaches(tmux: str) -> None:
    gw = gateway(
        ["/dev/pts/1 900 $4 @8", "/dev/pts/3 100 $1 @2"], switched=["/dev/pts/3"], tty=True
    )
    out = output(run_json([], gw, {"TMUX": tmux, "TMUX_PANE": "%9"}))
    assert (out["action"], out["client"]) == ("switched", "/dev/pts/3")
    assert displays(gw) == [] and gw.execs == []


@pytest.mark.parametrize("tmux_pane", ["", "9", "%9;", "%x", " %9", "%9\n", "$1"])
def test_an_invalid_tmux_pane_skips_step_1_and_never_attaches(tmux_pane: str) -> None:
    gw = gateway(["/dev/pts/3 100 $1 @2"], switched=["/dev/pts/3"], tty=True)
    environ = {"TMUX": f"{SOCK},123,4", "TMUX_PANE": tmux_pane}
    out = output(run_json([], gw, environ))
    assert out["action"] == "switched"
    assert displays(gw) == [] and gw.execs == []


def test_tmux_pane_without_tmux_is_not_inside_tmux() -> None:
    gw = gateway(["/dev/pts/3 100 $1 @2"], switched=["/dev/pts/3"])
    output(run_json([], gw, {"TMUX_PANE": "%9"}))
    assert displays(gw) == []


@pytest.mark.parametrize(
    "tmux", ["/tmp/other/sock,1,0", f"{SOCK}x,1,0", "relative/sock,1,0", "/a\x01b,1,0"]
)
def test_inside_tmux_on_another_server_starts_at_step_3_and_never_attaches(
    tmux: str,
) -> None:
    gw = gateway(["/dev/pts/3 100 $1 @2"], switched=["/dev/pts/3"], tty=True)
    out = output(run_json(["--socket", SOCK], gw, {"TMUX": tmux, "TMUX_PANE": "%9"}))
    assert out["client"] == "/dev/pts/3"
    assert displays(gw) == [] and gw.execs == []


def test_tmux_naming_the_server_through_a_symlink_is_the_same_server() -> None:
    gw = gateway(
        ["/dev/pts/2 100 $4 @2"],
        switched=["/dev/pts/2"],
        extra={CALLER: completed("%9 $4\n")},
        links={"/run/link": f"/tmp/tmux-{UID}"},
        files={**FakeGateway().files, "/run": FakeGateway().files["/tmp"]},
    )
    out = output(run_json([], gw, {"TMUX": "/run/link/default,1,4", "TMUX_PANE": "%9",
                                   "TERMCTL_SOCKET": SOCK}))  # fmt: skip
    assert out["client"] == "/dev/pts/2"
    assert displays(gw) == [list(CALLER)]


@pytest.mark.parametrize("tmux", ["", ",1,0"])
def test_an_empty_tmux_is_not_inside_tmux(tmux: str) -> None:
    gw = gateway(tty=True)
    with pytest.raises(Execed):
        run([], gw, {"TMUX": tmux, "TMUX_PANE": "%9"})
    assert len(gw.execs) == 1


# --- step 2: attach -----------------------------------------------------------------------------

ATTACH = [TMUX, "-S", SOCK, "attach", "-E", "-t", "=$1:@2.%3"]


def test_attach_execs_with_the_callers_unfiltered_environment(private_state_home: Path) -> None:
    gw = gateway(tty=True)
    seen_at_exec: list[list[Any]] = []
    exec_replace = gw.exec_replace

    def exec_and_check(argv: list[str], env: Mapping[str, str]) -> Any:
        seen_at_exec.append(records(private_state_home))
        return exec_replace(argv, env)

    gw.exec_replace = exec_and_check  # type: ignore[method-assign]
    environ = {"SSH_AUTH_SOCK": "/tmp/agent.sock", "SECRET_TOKEN": "x"}
    with pytest.raises(Execed):
        run([], gw, environ)
    [(argv, env)] = gw.execs
    assert argv == ATTACH
    # The whole caller's environment, not the filtered one tmux commands get.
    assert env["SSH_AUTH_SOCK"] == "/tmp/agent.sock" and env["SECRET_TOKEN"] == "x"
    # Logged before the exec, without an exit code: after the exec the status is tmux's.
    [[record]] = seen_at_exec
    assert (record["result"], record["action"], record["id"]) == ("ok", "attach", ID)
    assert "exit_code" not in record
    # Steps 2 needs no client list and switches nothing.
    assert LIST_CLIENTS not in {tuple(call) for call in gw.calls} and switches(gw) == []


def test_attach_with_json_prints_the_argv_and_does_not_exec(private_state_home: Path) -> None:
    gw = gateway(tty=True)
    out = output(run_json([], gw))
    assert out == {
        "id": ID, "action": "attach", "client": None, "argv": ATTACH, "dry_run": False,
        "socket_path": SOCK, "session": "termctl", "window_id": "@2", "pane_id": "%3",
    }  # fmt: skip
    assert gw.execs == []
    [record] = records(private_state_home)
    assert (record["result"], record["action"]) == ("ok", "attach")
    # termctl itself exits 0 here.
    assert record["exit_code"] == 0


def test_a_renamed_session_is_attached_by_its_id() -> None:
    rows = [pane_row(overrides={"session_name": "my work", "session_id": "$12"})]
    out = output(run_json([], gateway(tty=True, rows=rows)))
    assert out["argv"][-1] == "=$12:@2.%3"
    assert out["session"] == "my work"


@pytest.mark.parametrize(("stdin", "stderr"), [(True, False), (False, True), (False, False)])
def test_attach_needs_stdin_and_stderr_to_be_terminals(stdin: bool, stderr: bool) -> None:
    gw = gateway(["/dev/pts/3 100 $1 @2"], switched=["/dev/pts/3"], tty=stdin, stderr_tty=stderr)
    assert output(run_json([], gw))["action"] == "switched"


def test_no_attach_skips_step_2() -> None:
    gw = gateway(["/dev/pts/3 100 $1 @2"], switched=["/dev/pts/3"], tty=True)
    assert output(run_json(["--no-attach"], gw))["action"] == "switched"
    assert gw.execs == []


def test_no_attach_with_no_client_is_no_client() -> None:
    error(run_json(["--no-attach"], gateway(tty=True)), "no_client", 12)


def test_a_failed_exec_is_attach_failed_and_logged(private_state_home: Path) -> None:
    gw = gateway(tty=True, exec_error=PermissionError(13, "Permission denied"))
    result = run([], gw)
    assert result.code == 12
    assert "Permission denied" in result.stderr
    ok, failed = records(private_state_home)
    assert (ok["result"], ok["action"]) == ("ok", "attach")
    assert "exit_code" not in ok
    assert (failed["result"], failed["exit_code"], failed["action"]) == ("error", 12, "attach")


# --- --client -----------------------------------------------------------------------------------


def test_client_switches_that_client_and_skips_steps_1_and_2() -> None:
    gw = gateway(
        ["/dev/pts/1 900 $4 @2", "/dev/pts/5 1 $9 @0"],
        switched=["/dev/pts/5"],
        extra={CALLER: completed("%9 $4\n")},
        tty=True,
    )
    out = output(run_json(["--client", "/dev/pts/5"], gw, INSIDE))
    assert (out["action"], out["client"]) == ("switched", "/dev/pts/5")
    assert displays(gw) == [] and gw.execs == []


def test_a_short_tty_client_name_switches_the_listed_client() -> None:
    """tmux takes ``pts/5`` for ``/dev/pts/5``; termctl switches and reports the listed name."""
    gw = gateway(["/dev/pts/11 9 $9 @0", "/dev/pts/5 1 $9 @0"], switched=["/dev/pts/5"])
    out = output(run_json(["--client", "pts/5"], gw))
    assert out["client"] == "/dev/pts/5"
    assert switches(gw) == [[TMUX, "-S", SOCK, "switch-client", "-E", "-c", "/dev/pts/5",
                             "-t", "%3"]]  # fmt: skip
    human = run(["--client", "pts/5"], gateway(["/dev/pts/5 1 $9 @0"], switched=["/dev/pts/5"]))
    assert (human.code, human.stdout) == (0, "/dev/pts/5\n")


def test_client_with_no_attach_is_allowed() -> None:
    gw = gateway(["/dev/pts/5 1 $9 @0"], switched=["/dev/pts/5"], tty=True)
    assert output(run_json(["--client", "/dev/pts/5", "--no-attach"], gw))["client"] == "/dev/pts/5"


@pytest.mark.parametrize(
    "client",
    [
        "/dev/pts/6",
        "pts/6",
        "dev/pts/5",
        "/pts/5",
        "/dev/pts/1",
        "pts/1",
        "/dev/pts/",
        "pts/",
        "/dev",
        "/",
        ".",
        "client-1",
        "pts/7",
    ],
)
def test_a_client_that_is_not_attached_is_client_not_found(
    client: str, private_state_home: Path
) -> None:
    gw = gateway(["/dev/pts/5 1 $9 @0", "/dev/pts/11 1 $9 @0", "/dev/pts/7 1  "], tty=True)
    error(run_json(["--client", client], gw), "client_not_found", 10)
    assert switches(gw) == []
    [record] = records(private_state_home)
    assert (record["exit_code"], record["action"]) == (10, "switched")


def test_a_client_attached_to_no_session_is_not_found() -> None:
    gw = gateway(["/dev/pts/7 1  "])
    error(run_json(["--client", "/dev/pts/7"], gw), "client_not_found", 10)


@pytest.mark.parametrize("client", ["", "a b", "a;b", "#{x}", "/" + "a" * 101, "é"])
def test_a_client_outside_the_grammar_is_a_usage_error(client: str) -> None:
    gw = FakeGateway()
    error(run_json(["--client", client], gw), "usage", 2)
    assert gw.calls == []


def test_a_client_detached_before_the_switch_is_client_not_found() -> None:
    gw = gateway(
        ["/dev/pts/5 1 $9 @0"],
        extra={switch("/dev/pts/5"): completed(stderr="can't find client: /dev/pts/5\n",
                                               returncode=1)},
    )  # fmt: skip
    error(run_json(["--client", "/dev/pts/5"], gw), "client_not_found", 10)


def test_a_client_termctl_chose_that_went_away_is_no_client(private_state_home: Path) -> None:
    gw = gateway(
        ["/dev/pts/5 1 $1 @0"],
        extra={switch("/dev/pts/5"): completed(stderr="can't find client: /dev/pts/5\n",
                                               returncode=1)},
    )  # fmt: skip
    err = error(run_json([], gw), "no_client", 12)
    assert "/dev/pts/5" in err["message"] and "went away" in err["message"]
    [record] = records(private_state_home)
    assert (record["result"], record["exit_code"], record["action"]) == ("error", 12, "switched")


# --- tmux failures ------------------------------------------------------------------------------


def test_a_pane_gone_before_the_switch_is_not_found() -> None:
    gw = gateway(
        ["/dev/pts/5 1 $1 @0"],
        extra={switch("/dev/pts/5"): completed(stderr="can't find pane: %3\n", returncode=1)},
    )
    error(run_json([], gw), "not_found", 10)


def test_a_server_gone_at_list_clients_is_not_found() -> None:
    gone = completed(stderr=f"no server running on {SOCK}\n", returncode=1)
    error(run_json([], gateway(extra={LIST_CLIENTS: gone})), "not_found", 10)


def test_any_other_tmux_failure_is_server_unavailable() -> None:
    boom = completed(stderr="boom\n", returncode=1)
    err = error(run_json([], gateway(extra={LIST_CLIENTS: boom})), "server_unavailable", 12)
    assert "boom" in err["message"]


def test_no_server_is_no_server_and_logged_without_an_id(private_state_home: Path) -> None:
    files = FakeGateway().files
    del files[SOCK]
    error(run_json([], FakeGateway(files=files)), "no_server", 10)
    [record] = records(private_state_home)
    assert (record["id"], record["socket_path"], record["action"]) == (None, SOCK, "switched")


def test_an_unknown_id_is_not_found_and_logged(private_state_home: Path) -> None:
    error(run_json([], FakeGateway(panes=[])), "not_found", 10)
    [record] = records(private_state_home)
    assert (record["result"], record["exit_code"], record["id"]) == ("error", 10, None)


def test_a_usage_error_is_not_logged(private_state_home: Path) -> None:
    error(Run(["--json", "focus", "nope!"]), "usage", 2)
    assert records(private_state_home) == []


def test_verbose_traces_the_switch() -> None:
    gw = gateway(["/dev/pts/2 300 $1 @5"], switched=["/dev/pts/2"])
    result = run(["--verbose"], gw)
    assert result.code == 0
    assert "switch-client -E -c /dev/pts/2 -t %3" in result.stderr


# --- --dry-run ----------------------------------------------------------------------------------


def test_dry_run_reports_the_switch_and_changes_nothing(private_state_home: Path) -> None:
    gw = gateway(["/dev/pts/2 300 $1 @5"])
    out = output(run_json(["--dry-run"], gw))
    assert (out["action"], out["client"], out["dry_run"]) == ("switched", "/dev/pts/2", True)
    assert switches(gw) == []
    assert records(private_state_home) == []


def test_dry_run_never_attaches(private_state_home: Path) -> None:
    gw = gateway(tty=True)
    result = run(["--dry-run"], gw)
    assert result.code == 0
    assert result.stdout.strip() == " ".join(["/usr/bin/tmux", "-S", SOCK, "attach", "-E",
                                              "-t", "'=$1:@2.%3'"])  # fmt: skip
    assert gw.execs == [] and records(private_state_home) == []
    out = output(run_json(["--dry-run"], gateway(tty=True)))
    assert (out["action"], out["dry_run"], out["argv"]) == ("attach", True, ATTACH)


def test_dry_run_with_a_client_that_is_not_attached_fails_as_a_real_run() -> None:
    error(run_json(["--dry-run", "--client", "/dev/pts/9"], gateway()), "client_not_found", 10)


def test_dry_run_with_no_client_fails_as_a_real_run() -> None:
    error(run_json(["--dry-run"], gateway()), "no_client", 12)


@pytest.mark.parametrize("target", ["=work:@2.%3", "=my work:@2.%3", "$1:@2.%3", "=$1:@2"])
def test_the_schema_takes_only_a_session_id_attach_target(target: str) -> None:
    out = {
        "id": ID, "action": "attach", "client": None, "argv": [*ATTACH[:-1], target],
        "dry_run": False, "socket_path": SOCK, "session": "termctl", "window_id": "@2",
        "pane_id": "%3",
    }  # fmt: skip
    with pytest.raises(Exception, match="does not match"):
        validate(out, "termctl/cli/focus.json")


# --- stop signals (interface.md "Stop signals", "Audit and log") --------------------------------


@pytest.mark.parametrize(("stop", "code"), [(KeyboardInterrupt(), 130), (cli.Terminated(15), 143)])
def test_a_signal_during_the_switch_is_logged_as_stopped(
    private_state_home: Path, stop: BaseException, code: int
) -> None:
    gw = gateway(["/dev/pts/2 300 $1 @5"], extra={switch("/dev/pts/2"): stop})
    result = run_json([], gw)
    assert result.code == code
    # A signal exit prints no error object.
    assert result.stdout == ""
    [record] = records(private_state_home)
    assert (record["event"], record["result"], record["exit_code"], record["action"]) == (
        "focus", "error", code, "switched",
    )  # fmt: skip


def test_a_real_sigterm_during_the_switch_is_logged_as_stopped(
    private_state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def terminated(*args: object, **kwargs: object) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(focus_cmd, "switch", terminated)
    before = signal.getsignal(signal.SIGTERM)
    result = run_json([], gateway(["/dev/pts/2 300 $1 @5"]))
    assert result.code == 128 + signal.SIGTERM
    [record] = records(private_state_home)
    assert (record["result"], record["exit_code"]) == ("error", 128 + signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) == before


def test_the_signal_dispositions_are_restored_before_the_attach_exec() -> None:
    stops = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    before = {signum: signal.getsignal(signum) for signum in stops}
    gw = gateway(tty=True)
    at_exec: list[dict[int, Any]] = []
    exec_replace = gw.exec_replace

    def exec_and_check(argv: list[str], env: Mapping[str, str]) -> Any:
        at_exec.append({signum: signal.getsignal(signum) for signum in stops})
        return exec_replace(argv, env)

    gw.exec_replace = exec_and_check  # type: ignore[method-assign]
    with pytest.raises(Execed):
        run([], gw)
    assert at_exec == [before]


def _sigterm_after_the_ok_record(monkeypatch: pytest.MonkeyPatch) -> None:
    log = cli.Context.log

    def log_then_stop(self: cli.Context, record: dict[str, Any]) -> None:
        log(self, record)
        if record["event"] == "focus" and record["result"] == "ok":
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(cli.Context, "log", log_then_stop)


def test_a_signal_right_after_the_switch_record_is_ignored(
    private_state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sigterm_after_the_ok_record(monkeypatch)
    result = run_json([], gateway(["/dev/pts/2 300 $1 @5"], switched=["/dev/pts/2"]))
    assert result.code == 0
    [record] = records(private_state_home)
    assert (record["result"], record["exit_code"]) == ("ok", 0)


def test_a_signal_right_after_the_attach_record_is_ignored(
    private_state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sigterm_after_the_ok_record(monkeypatch)
    gw = gateway(tty=True)
    with pytest.raises(Execed):
        run([], gw)
    [record] = records(private_state_home)
    assert (record["result"], record["action"]) == ("ok", "attach")
    assert "exit_code" not in record
    assert [argv for argv, _ in gw.execs] == [ATTACH]


def test_an_unexpected_exec_failure_is_internal_and_logged(private_state_home: Path) -> None:
    gw = gateway(tty=True, exec_error=RuntimeError("boom"))
    result = run([], gw)
    assert result.code == 1
    ok, failed = records(private_state_home)
    assert (ok["result"], ok["action"]) == ("ok", "attach")
    assert "exit_code" not in ok
    del failed["ts"]
    assert failed == {
        "level": "error", "event": "focus", "result": "error", "id": ID, "socket_path": SOCK,
        "exit_code": 1, "message": "internal error: RuntimeError: boom", "action": "attach",
    }  # fmt: skip


# --- golden output ------------------------------------------------------------------------------


def golden(name: str) -> Any:
    expected = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/focus.json")
    return expected


def test_switched_golden() -> None:
    gw = gateway(["/dev/pts/2 300 $1 @5"], switched=["/dev/pts/2"])
    assert output(run_json([], gw)) == golden("focus-switched.json")


def test_attach_golden() -> None:
    assert output(run_json([], gateway(tty=True))) == golden("focus-attach.json")
