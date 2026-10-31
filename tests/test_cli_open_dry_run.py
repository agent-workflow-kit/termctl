"""``open --dry-run`` against a fake tmux (interface.md ``open``, "Open order" step 3, "Dry
runs"): it stops after step 3, runs only read-only tmux queries, no hooks, writes no record,
and reports what a real run would do. The real-tmux check is in test_cli_open_integration.py.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

from termctl import open_cmd
from termctl import tmux_backend as tb
from termctl.backend import Created, SessionState

from .fakes import TMUX, UID, FakeGateway, completed, directory, pane_row, safe_default_server
from .schemas import validate
from .test_cli import ENVIRON, GOLDEN, Run
from .test_cli_open import (
    CREATED_AT,
    FILTERED,
    ID,
    SESSION,
    SOCK,
    TRUE,
    calls,
    error,
    fixed_id,  # noqa: F401  (a fixture)
    key,
    make_plan,
    run,
)
from .test_cli_open_hooks import records

pytestmark = pytest.mark.usefixtures("fixed_id")

HAS = key(tb.has_session(SESSION))
MISSING = completed(stderr=f"can't find session: {SESSION}\n", returncode=1)
NO_SERVER = completed(stderr=f"no server running on {SOCK}\n", returncode=1)


def no_server_files() -> dict[str, Any]:
    """The default socket directory without a socket file: no server."""
    files = safe_default_server()
    del files[f"/tmp/tmux-{UID}/default"]
    return files


def dry(argv: list[str], gateway: FakeGateway, environ: dict[str, str] | None = None) -> Any:
    result = run(["--dry-run", *argv], gateway, environ)
    assert result.code == 0, result.stdout + result.stderr
    out = result.json()
    validate(out, "termctl/cli/open-dry-run.json")
    return out


def shown(invocation: tb.Invocation) -> list[str]:
    return invocation.shown_argv(TMUX, SOCK)


def tagging(plan: open_cmd.Plan) -> list[str]:
    """The oracle for the tagging invocation as shown: built with ids that appear nowhere else
    in it, then replaced by the tokens."""
    built = shown(plan.tag_invocation(ID, Created("%5", "@3"), CREATED_AT))
    return [e.replace("%5", "%NEW").replace("@3", "@NEW") for e in built]


# --- what runs ----------------------------------------------------------------------------------


def test_an_existing_session_runs_only_read_only_queries(private_state_home: Path) -> None:
    gateway = FakeGateway({HAS: completed()}, panes=[], executables=TRUE)
    out = dry(["--", "true"], gateway)
    assert calls(gateway) == ["-V", "list-panes", "has-session -t"]
    assert gateway.mkdirs == [] and gateway.group_calls == []
    assert records(private_state_home) == [] and records(private_state_home, "audit.jsonl") == []
    assert out["session_exists"] is True
    plan = make_plan()
    assert out["tmux_commands"] == [shown(tb.new_window(SESSION, plan.window)), tagging(plan)]


def test_no_server_runs_only_tmux_v_and_creates_no_directory() -> None:
    files = no_server_files()
    del files[f"/tmp/tmux-{UID}"]
    gateway = FakeGateway(files=files, executables=TRUE)
    out = dry(["--", "true"], gateway)
    assert calls(gateway) == ["-V"]
    assert gateway.mkdirs == []
    assert out["session_exists"] is None
    plan = make_plan()
    start = tb.new_session(SESSION, plan.window, start_server=True, env=FILTERED)
    assert out["tmux_commands"] == [shown(start), tagging(plan)]
    assert out["tmux_commands"][0][3:8] == ["set", "-s", "--", "exit-unattached", "off"]


def test_a_missing_session_on_a_running_server_is_created_without_the_prefix() -> None:
    gateway = FakeGateway({HAS: MISSING}, panes=[], executables=TRUE)
    out = dry(["--", "true"], gateway)
    assert out["session_exists"] is False
    plan = make_plan()
    create = tb.new_session(SESSION, plan.window, start_server=False, env=FILTERED)
    assert out["tmux_commands"] == [shown(create), tagging(plan)]


def test_a_server_gone_after_the_read_is_reported_as_no_server() -> None:
    gateway = FakeGateway({HAS: NO_SERVER}, panes=[], executables=TRUE)
    out = dry(["--", "true"], gateway)
    assert out["session_exists"] is None
    assert out["tmux_commands"][0][3:5] == ["set", "-s"]


def test_a_failing_has_session_exits_12_as_a_real_run_would() -> None:
    gateway = FakeGateway(
        {HAS: completed(stderr="lost server\n", returncode=1)}, panes=[], executables=TRUE
    )
    error(run(["--dry-run", "--", "true"], gateway), "server_unavailable", 12)


def test_hooks_never_run(tmp_path: Path, private_state_home: Path) -> None:
    config = tmp_path / "config.yaml"
    hooks = {"pre-open": [{"cmd": "veto"}], "post-open": [{"cmd": "after"}]}
    config.write_text(yaml.safe_dump({"hooks": hooks}), encoding="utf-8")
    gateway = FakeGateway({HAS: completed()}, panes=[], executables=TRUE)
    argv = ["--json", "--config", str(config), "open", "--dry-run", "--hook", "pre-open=gate"]
    result = Run([*argv, "--decision-id", "d-1", "--", "true"], gateway, environ=ENVIRON)
    assert result.code == 0, result.stderr
    out = result.json()
    assert gateway.group_calls == []
    assert out["decision_ids"] == ["d-1"]
    assert records(private_state_home) == []


def test_an_override_run_is_not_audited(private_state_home: Path) -> None:
    gateway = FakeGateway({HAS: completed()}, panes=[], executables=TRUE)
    out = dry(["--override-escalation", "--actor", "human:pat", "--", "true"], gateway)
    assert (out["override_escalation"], out["actor"]) == (True, "human:pat")
    assert records(private_state_home, "audit.jsonl") == []
    assert records(private_state_home) == []


def test_the_id_skips_a_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    from termctl import ids

    left = [ID, ID, "tc-zzzzzzzz"]
    monkeypatch.setattr(ids, "generate", lambda random_bytes=None: left.pop(0))
    gateway = FakeGateway({HAS: completed()}, panes=[pane_row(tc_id=ID)], executables=TRUE)
    out = dry(["--", "true"], gateway)
    assert out["id"] == "tc-zzzzzzzz"
    assert all("tc-zzzzzzzz" in " ".join(c) for c in out["tmux_commands"][1:])


# --- the report ---------------------------------------------------------------------------------


def test_fields_report_the_resolved_open(tmp_path: Path) -> None:
    gateway = FakeGateway({HAS: completed()}, panes=[], executables=TRUE, dirs={"/", "/srv"})
    out = dry(
        ["--name", "job", "--cwd", "/srv", "--label", "b=2", "--label", "a=1", "--env", "X=1",
         "--keep-on-exit", "failed", "--focus", "--", "true", "arg"],
        gateway,
    )  # fmt: skip
    assert out["dry_run"] is True
    assert (out["id"], out["name"], out["cwd"], out["argv"]) == (ID, "job", "/srv", ["true", "arg"])
    assert (out["socket_path"], out["tmux_version"]) == (SOCK, "3.5")
    assert (out["placement"], out["matched_rule"], out["session"], out["target"]) == (
        "default",
        None,
        SESSION,
        "window",
    )
    assert (out["labels"], out["env_names"]) == (["a=1", "b=2"], ["X"])
    assert (out["keep_on_exit"], out["focus"], out["override_escalation"]) == (
        "failed",
        True,
        False,
    )
    assert out["decision_ids"] == []


def test_an_unparsable_tmux_version_is_null() -> None:
    gateway = FakeGateway(
        {(TMUX, "-V"): completed("tmux next\n"), HAS: completed()}, panes=[], executables=TRUE
    )
    result = run(["--dry-run", "--", "true"], gateway)
    assert result.code == 0, result.stderr
    assert result.json()["tmux_version"] is None


def test_matched_rule_and_placement_come_from_the_rules(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "placements": {"work": {"session": "work"}},
                "rules": [
                    {"match": {"k": "x"}, "placement": "default"},
                    {"match": {"k": "v"}, "placement": "work"},
                ],
            }  # fmt: skip
        ),
        encoding="utf-8",
    )
    has_work = key(tb.has_session("work"))
    gateway = FakeGateway({has_work: completed(), HAS: completed()}, panes=[], executables=TRUE)
    base = ["--json", "--config", str(config), "open", "--dry-run"]
    result = Run([*base, "--label", "k=v", "--", "true"], gateway, environ=ENVIRON)
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, "termctl/cli/open-dry-run.json")
    assert (out["placement"], out["matched_rule"], out["session"]) == ("work", 1, "work")
    result = Run(
        [*base, "--label", "k=v", "--placement", "default", "--session", "s2", "--", "true"],
        FakeGateway({key(tb.has_session("s2")): completed()}, panes=[], executables=TRUE),
        environ=ENVIRON,
    )
    assert result.code == 0, result.stderr
    out = result.json()
    assert (out["placement"], out["matched_rule"], out["session"]) == ("default", None, "s2")


def test_human_output_lists_the_tmux_commands() -> None:
    gateway = FakeGateway({HAS: completed()}, panes=[], executables=TRUE)
    result = Run(["open", "--dry-run", "--", "true"], gateway, environ=ENVIRON)
    assert result.code == 0, result.stderr
    plan = make_plan()
    expected = [shown(tb.new_window(SESSION, plan.window)), tagging(plan)]
    assert result.stdout == "".join(shlex.join(argv) + "\n" for argv in expected)


def test_human_output_escapes_control_characters() -> None:
    gateway = FakeGateway({HAS: completed()}, panes=[], executables=TRUE)
    result = Run(
        ["open", "--dry-run", "--env", "X=1", "--", "true", "a\x1b[31mb"], gateway, environ=ENVIRON
    )
    assert result.code == 0, result.stderr
    assert "\x1b" not in result.stdout and "\\x1b" in result.stdout
    assert len(result.stdout.splitlines()) == 2


# --- tokens and --env values --------------------------------------------------------------------


def test_new_ids_appear_as_tokens() -> None:
    gateway = FakeGateway({HAS: completed()}, panes=[], executables=TRUE)
    out = dry(["--", "true"], gateway)
    text = json.dumps(out["tmux_commands"])
    assert "%NEW" in text and "@NEW" in text
    assert "4294967295" not in text


def test_text_that_looks_like_a_placeholder_id_is_kept() -> None:
    # The name, cwd and argv carry the ids the tokens are built from; only termctl's own ids
    # become tokens.
    tricky = "%4294967295@4294967295%4294967294"
    gateway = FakeGateway({HAS: completed()}, panes=[], executables=TRUE)
    out = dry(["--name", "n", "--", "true", tricky, "%0", "@0"], gateway)
    plan = make_plan(("true", tricky, "%0", "@0"), name="n")
    built = shown(plan.tag_invocation(ID, Created("%5", "@3"), CREATED_AT))
    # Only the ids of the oracle build differ from the dry run's.
    assert len(out["tmux_commands"][1]) == len(built)
    joined = "\n".join(out["tmux_commands"][1])
    assert joined.count(tricky) == "\n".join(built).count(tricky) > 0
    assert joined.count("%NEW") == "\n".join(built).count("%5")
    assert joined.count("@NEW") == "\n".join(built).count("@3")


@pytest.mark.parametrize("with_argv", [True, False], ids=["argv", "shell"])
@pytest.mark.parametrize("server", ["session", "no-session", "no-server"])
def test_env_values_never_appear(with_argv: bool, server: str) -> None:
    responses = {"session": {HAS: completed()}, "no-session": {HAS: MISSING}}.get(server, {})
    files = no_server_files() if server == "no-server" else None
    gateway = FakeGateway(responses, files=files, panes=[], executables=TRUE)
    argv = ["--env", "SECRET=hunter2-value", "--env", "LANG=lang-secret"]
    argv += ["--", "true"] if with_argv else []
    environ = {"LANG": "C.UTF-8"}
    out = dry(argv, gateway, environ)
    text = json.dumps(out)
    assert "hunter2" not in text and "lang-secret" not in text
    assert out["env_names"] == ["SECRET", "LANG"]
    assert "SECRET" in text
    human = Run(["open", "--dry-run", *argv], gateway, environ={**ENVIRON, **environ})
    assert human.code == 0, human.stderr
    assert "hunter2" not in human.stdout and "lang-secret" not in human.stdout


def test_env_path_value_never_appears() -> None:
    gateway = FakeGateway({HAS: completed()}, panes=[], executables={TMUX, "/opt/secret/tool"})
    out = dry(["--env", "PATH=/opt/secret", "--", "tool"], gateway)
    assert "/opt/secret" not in json.dumps(out["tmux_commands"])
    assert "'PATH' 'tool'" in out["tmux_commands"][1][-2]


# --- exit codes as a real run ---------------------------------------------------------------------


def test_not_supported_exits_2() -> None:
    error(run(["--dry-run", "--target", "pane"], FakeGateway(panes=[])), "not_supported", 2)


def test_command_not_found_exits_12() -> None:
    gateway = FakeGateway(panes=[])
    error(run(["--dry-run", "--", "no-such-tool"], gateway), "command_not_found", 12)
    assert gateway.calls == []


def test_an_unsafe_socket_exits_12() -> None:
    files = safe_default_server()
    files[f"/tmp/tmux-{UID}"] = directory(0o777, UID)
    gateway = FakeGateway(files=files, executables=TRUE)
    error(run(["--dry-run", "--", "true"], gateway), "server_unavailable", 12)
    assert calls(gateway) == ["-V"]


def test_an_explicit_socket_with_a_missing_parent_exits_12() -> None:
    gateway = FakeGateway(executables=TRUE)
    result = Run(
        ["--json", "--socket", "/nonexistent/dir/sock", "open", "--dry-run", "--", "true"],
        gateway,
        environ=ENVIRON,
    )
    error(result, "server_unavailable", 12)


def test_tmux_too_old_exits_12() -> None:
    gateway = FakeGateway({(TMUX, "-V"): completed("tmux 2.9\n")}, panes=[], executables=TRUE)
    error(run(["--dry-run", "--", "true"], gateway), "tmux_too_old", 12)


def test_the_override_gate_still_applies() -> None:
    gateway = FakeGateway(panes=[], executables=TRUE)
    error(run(["--dry-run", "--override-escalation", "--", "true"], gateway), "usage", 2)
    assert gateway.calls == []


# --- the tagging oracle itself --------------------------------------------------------------------


def test_dry_run_commands_picks_another_placeholder_when_the_text_has_it() -> None:
    plan = make_plan(("true", "%4294967295", "@4294967294"))
    commands = open_cmd.dry_run_commands(
        SESSION, plan, ID, CREATED_AT, SessionState.EXISTS, {}, TMUX, SOCK
    )
    assert commands[1] == tagging(plan)


# --- golden output --------------------------------------------------------------------------------


def golden(name: str) -> Any:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


def test_golden_existing_session() -> None:
    gateway = FakeGateway({HAS: completed()}, panes=[], executables=TRUE)
    out = dry(
        ["--label", "kind=review", "--env", "TOKEN=s3cret", "--decision-id", "d-7", "--", "true"],
        gateway,
    )
    expected = golden("open-dry-run-session.json")
    validate(expected, "termctl/cli/open-dry-run.json")
    assert out == expected


def test_golden_no_server() -> None:
    gateway = FakeGateway(files=no_server_files(), panes=[], executables=TRUE)
    out = dry([], gateway, {"LANG": "C.UTF-8"})
    expected = golden("open-dry-run-no-server.json")
    validate(expected, "termctl/cli/open-dry-run.json")
    assert out == expected
