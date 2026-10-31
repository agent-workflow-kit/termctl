"""``open`` through the check order up to the session, against a fake tmux (interface.md
"Open order" steps 2, 3 and 5). Real-tmux tests are in test_open_integration.py."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from termctl import cli, ids, open_cmd
from termctl import tmux_backend as tb
from termctl.backend import Created

from .fakes import (
    TMUX,
    UID,
    FakeGateway,
    completed,
    directory,
    is_list_panes,
    pane_row,
    safe_default_server,
    socket_file,
)
from .schemas import validate
from .test_cli import ENVIRON, Run

SOCK = f"/tmp/tmux-{UID}/default"
ID = "tc-abcdefgh"
OTHER = "tc-zzzzzzzz"
SESSION = "termctl"
TRUE = {TMUX, "/usr/bin/true"}


def key(invocation: tb.Invocation, socket: str = SOCK) -> tuple[str, ...]:
    return tuple(invocation.argv(TMUX, socket))


def calls(gateway: FakeGateway) -> list[str]:
    """Each tmux call by its first tmux command (``list-panes`` for the reader)."""
    names = []
    for call in gateway.calls:
        if is_list_panes(call):
            names.append("list-panes")
        elif call[1:] == ["-V"]:
            names.append("-V")
        else:
            names.append(" ".join(call[3:5]))
    return names


def run(
    argv: list[str],
    gateway: FakeGateway,
    environ: Mapping[str, str] | None = None,
) -> Run:
    return Run(["--json", "open", *argv], gateway, environ={**ENVIRON, **(environ or {})})


def error(result: Run, code: str, exit_code: int) -> Any:
    assert result.code == exit_code, result.stderr
    envelope = result.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == code
    return envelope["error"]


# --- step 2: the command check ------------------------------------------------------------------


def test_command_not_found_comes_before_any_tmux_call() -> None:
    gateway = FakeGateway(panes=[])
    err = error(run(["--", "no-such-tool"], gateway), "command_not_found", 12)
    assert "no-such-tool" in err["message"]
    assert gateway.calls == []
    assert gateway.executable_checks == [TMUX, "/usr/bin/no-such-tool"]


def test_command_found_on_the_filtered_path_passes_the_check() -> None:
    gateway = FakeGateway(executables={TMUX, "/opt/bin/tool"}, list_panes=completed(""))
    result = run(["--dry-run", "--", "tool"], gateway, {"PATH": "/usr/bin:/opt/bin"})
    error(result, "internal", 1)
    assert "/opt/bin/tool" in gateway.executable_checks


def test_env_path_decides_where_the_command_is_found() -> None:
    gateway = FakeGateway(executables={TMUX, "/usr/bin/tool"})
    error(run(["--env", "PATH=/opt/bin", "--", "tool"], gateway), "command_not_found", 12)
    assert gateway.executable_checks == [TMUX, "/opt/bin/tool"]


def test_path_not_in_server_env_allow_finds_nothing(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"server_env_allow": ["HOME"]}), encoding="utf-8")
    gateway = FakeGateway(executables={TMUX, "/usr/bin/tool"})
    result = Run(["--json", "--config", str(config), "open", "--", "tool"], gateway)
    error(result, "command_not_found", 12)
    assert gateway.executable_checks == [TMUX]


def test_absolute_command_must_be_executable() -> None:
    gateway = FakeGateway()
    error(run(["--", "/opt/x"], gateway), "command_not_found", 12)
    assert gateway.executable_checks[-1] == "/opt/x"


def test_relative_command_is_resolved_against_the_cwd() -> None:
    gateway = FakeGateway(dirs={"/", "/srv"})
    error(run(["--cwd", "/srv", "--", "./x"], gateway), "command_not_found", 12)
    assert gateway.executable_checks[-1] == "/srv/./x"


def test_tmux_missing_comes_before_the_command_check() -> None:
    gateway = FakeGateway(executables=set())
    error(run(["--", "no-such-tool"], gateway), "tmux_missing", 12)
    assert gateway.executable_checks == [TMUX]


# --- step 2: the size check ---------------------------------------------------------------------


def test_size_check_exits_2_before_any_tmux_call() -> None:
    cwd = "/" + "d" * 13000
    gateway = FakeGateway(dirs={"/", cwd}, panes=[])
    err = error(run(["--cwd", cwd], gateway), "usage", 2)
    assert "12288" in err["message"]
    assert gateway.calls == []


# --- step 3: tmux checks and the id-collision check ---------------------------------------------


def scripted(sequence: list[str], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make id generation return ``sequence`` in order; returns what is left of it."""
    left = list(sequence)
    monkeypatch.setattr(ids, "generate", lambda random_bytes=None: left.pop(0))
    return left


def test_id_collision_generates_another(monkeypatch: pytest.MonkeyPatch) -> None:
    left = scripted([ID, ID, OTHER, "tc-aaaaaaaa"], monkeypatch)
    gateway = FakeGateway(panes=[pane_row(tc_id=ID)])
    error(run(["--dry-run"], gateway), "internal", 1)
    assert left == ["tc-aaaaaaaa"]
    assert calls(gateway) == ["-V", "list-panes"]


def test_dry_run_stops_after_step_3_and_creates_nothing() -> None:
    gateway = FakeGateway(panes=[], executables=TRUE)
    err = error(run(["--dry-run", "--", "true"], gateway), "internal", 1)
    assert "dry-run" in err["message"]
    assert calls(gateway) == ["-V", "list-panes"]


def test_the_id_is_generated_in_step_2(monkeypatch: pytest.MonkeyPatch) -> None:
    # Generated by the resolve step, so it exists even when the tmux checks fail.
    left = scripted([ID], monkeypatch)
    seen: list[str | None] = []
    check_tmux = cli.check_tmux

    def recording(args: Any, ctx: cli.Context) -> None:
        seen.append(ctx.open_id)
        check_tmux(args, ctx)

    monkeypatch.setattr(cli, "check_tmux", recording)
    files = safe_default_server()
    files[f"/tmp/tmux-{UID}"] = directory(0o777, UID)
    error(run([], FakeGateway(files=files)), "server_unavailable", 12)
    assert seen == [ID] and left == []


def test_an_id_without_a_collision_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    left = scripted([ID, OTHER], monkeypatch)
    gateway = FakeGateway(panes=[pane_row(tc_id=OTHER)])
    error(run(["--dry-run"], gateway), "internal", 1)
    assert left == [OTHER]


def test_unsafe_socket_exits_12_in_step_3() -> None:
    files = safe_default_server()
    files[f"/tmp/tmux-{UID}"] = directory(0o777, UID)
    gateway = FakeGateway(files=files)
    error(run([], gateway), "server_unavailable", 12)
    assert calls(gateway) == ["-V"]


def test_explicit_socket_whose_parent_is_missing_exits_12_in_step_3() -> None:
    socket = "/nonexistent/dir/sock"
    gateway = FakeGateway()
    result = Run(["--json", "--socket", socket, "open"], gateway, environ=ENVIRON)
    err = error(result, "server_unavailable", 12)
    assert "does not exist" in err["message"]
    assert err["details"] == {"socket_path": socket}
    assert calls(gateway) == ["-V"]
    assert gateway.mkdirs == []


def test_explicit_socket_whose_parent_is_missing_is_no_server_for_other_commands() -> None:
    gateway = FakeGateway()
    result = Run(["--json", "--socket", "/nonexistent/dir/sock", "list"], gateway, environ=ENVIRON)
    assert result.code == 0, result.stderr


# --- step 1: the argv checks --------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--", "A=b", "x"],
        ["--", "=x"],
        ["--env", "PATH=/opt/bin"],
        ["--env", "A=1", "--env", "A=2", "--", "true"],
    ],
    ids=["argv0-assignment", "argv0-bare-equals", "env-path-without-argv", "env-twice"],
)
def test_argv_and_env_usage_errors_touch_nothing(argv: list[str]) -> None:
    gateway = FakeGateway(panes=[], executables=TRUE)
    err = error(run(argv, gateway), "usage", 2)
    assert "/opt/bin" not in err["message"]
    assert gateway.calls == [] and gateway.executable_checks == []


def test_argv_checks_come_before_not_supported() -> None:
    # Step 1 wins over the placement step's not_supported (#13's note).
    gateway = FakeGateway(panes=[])
    error(run(["--target", "pane", "--", "a=b"], gateway), "usage", 2)


# --- steps 5 and 6 against a fake tmux ----------------------------------------------------------

CREATED_AT = "2026-01-01T00:00:00.000Z"  # FakeClock's start
NEW_PANE = Created("%5", "@3")
FILTERED = {"PATH": "/usr/bin", "XDG_CONFIG_HOME": ENVIRON["XDG_CONFIG_HOME"]}


class Flow(FakeGateway):
    """A fake gateway whose ``list-panes`` read answers ``after`` once the tagging invocation
    ran, and which runs an effect after a given call (a server creating its socket)."""

    def __init__(self, *args: Any, effects: Mapping[tuple[str, ...], Any] | None = None,
                 after: list[dict[str, str]] | None = None, **kwargs: Any) -> None:  # fmt: skip
        super().__init__(*args, **kwargs)
        self.effects = dict(effects or {})
        self.after = after
        self.tag_key: tuple[str, ...] | None = None

    def _respond(self, argv: list[str], env: Mapping[str, str] | None) -> Any:
        response = super()._respond(argv, env)
        effect = self.effects.get(tuple(argv))
        if effect is not None:
            effect(self)
        if tuple(argv) == self.tag_key and self.after is not None:
            self.panes = self.after
        return response


def make_plan(
    argv: tuple[str, ...] = ("true",),
    env: tuple[tuple[str, str], ...] = (),
    *,
    name: str = "true",
    keep: str = "never",
    labels: tuple[str, ...] = (),
) -> open_cmd.Plan:
    return open_cmd.Plan(
        name=name, cwd="/", argv=argv, env=env, labels=labels, placement="default",
        keep_on_exit=keep,
    )  # fmt: skip


def tag_key(plan: open_cmd.Plan, socket: str = SOCK) -> tuple[str, ...]:
    return key(plan.tag_invocation(ID, NEW_PANE, CREATED_AT), socket)


def new_row(**overrides: str) -> dict[str, str]:
    """The pane open creates, as the read after step 6 sees it."""
    return pane_row(
        "%5",
        ID,
        {
            "window_id": "@3",
            "@termctl_window": "@3",
            "window_name": "true",
            "@termctl_name": "true",
            "@termctl_labels": "",
            "@termctl_created": CREATED_AT,
            "@termctl_pid": "",
            "@termctl_pid_start": "",
            **overrides,
        },
    )


def existing_session_flow(
    plan: open_cmd.Plan, tag_result: Any = None, after: list[dict[str, str]] | None = None
) -> Flow:
    gateway = Flow(
        {
            key(tb.has_session(SESSION)): completed(),
            key(tb.new_window(SESSION, plan.window)): completed("%5 @3\n"),
            tag_key(plan): completed() if tag_result is None else tag_result,
            key(tb.cleanup_kill("%5", "@3")): completed(),
        },
        panes=[],
        executables=TRUE,
        after=[new_row()] if after is None else after,
    )
    gateway.tag_key = tag_key(plan)
    return gateway


@pytest.fixture
def fixed_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ids, "generate", lambda random_bytes=None: ID)


@pytest.mark.usefixtures("fixed_id")
def test_open_creates_tags_and_reports_the_pane() -> None:
    plan = make_plan()
    gateway = existing_session_flow(plan)
    result = run(["--", "true"], gateway)
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, "termctl/cli/open.json")
    assert (out["id"], out["pane_id"], out["window_id"], out["state"]) == (
        ID,
        "%5",
        "@3",
        "running",
    )
    assert (out["focused"], out["decision_ids"], out["pid"]) == (False, [], None)
    assert calls(gateway) == ["-V", "list-panes", "has-session -t", "new-window -d", "if-shell -F",
                              "list-panes"]  # fmt: skip


@pytest.mark.usefixtures("fixed_id")
def test_human_output_is_the_id() -> None:
    gateway = existing_session_flow(make_plan())
    result = Run(["open", "--", "true"], gateway, environ=ENVIRON)
    assert (result.code, result.stdout) == (0, f"{ID}\n")


@pytest.mark.usefixtures("fixed_id")
def test_a_pane_gone_before_the_read_reports_state_gone() -> None:
    # Spike C1/E5: `true` exits at once and keep never closes its window.
    gateway = existing_session_flow(make_plan(), after=[])
    result = run(["--", "true"], gateway)
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, "termctl/cli/open.json")
    assert out == {
        "id": ID, "name": "true", "display_name": "true", "marker": None, "marker_text": None,
        "labels": [], "placement": "default", "session": SESSION, "pane_id": "%5",
        "window_id": "@3", "socket_path": SOCK, "created": CREATED_AT, "pid": None,
        "state": "gone", "exit_status": None, "exit_signal": None, "keep_on_exit": "never",
        "focused": False, "decision_ids": [],
    }  # fmt: skip


@pytest.mark.usefixtures("fixed_id")
def test_a_failed_read_after_tagging_reports_gone_with_a_warning() -> None:
    plan = make_plan()
    gateway = existing_session_flow(plan)
    gateway.after = None
    failed_read = completed(stderr="boom\n", returncode=1)
    gateway.effects[tag_key(plan)] = lambda g: setattr(g, "list_panes", failed_read)
    result = run(["--", "true"], gateway)
    assert result.code == 0, result.stderr
    assert result.json()["state"] == "gone"
    assert "warning: could not read pane" in result.stderr


@pytest.mark.usefixtures("fixed_id")
def test_a_dead_pane_kept_on_exit_reports_its_status() -> None:
    plan = make_plan(("false",), name="false", keep="failed")
    row = new_row(**{"pane_dead": "1", "pane_dead_status": "1", "@termctl_keep": "failed",
                     "window_name": "false", "@termctl_name": "false"})  # fmt: skip
    gateway = existing_session_flow(plan, after=[row])
    gateway.executables.add("/usr/bin/false")
    result = run(["--keep-on-exit", "failed", "--", "false"], gateway)
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, "termctl/cli/open.json")
    assert (out["state"], out["exit_status"], out["keep_on_exit"]) == ("dead", 1, "failed")


@pytest.mark.usefixtures("fixed_id")
@pytest.mark.parametrize("keep", ["never", "failed", "always"])
def test_keep_on_exit_from_config_is_used(keep: str, tmp_path: Path) -> None:
    plan = make_plan(keep=keep)
    gateway = existing_session_flow(plan, after=[new_row(**{"@termctl_keep": keep})])
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"keep_on_exit": keep}), encoding="utf-8")
    result = Run(["--json", "--config", str(config), "open", "--", "true"], gateway, ENVIRON)
    assert result.code == 0, result.stderr
    assert tag_key(plan) in [tuple(c) for c in gateway.calls]


@pytest.mark.usefixtures("fixed_id")
def test_the_flag_overrides_the_config_keep_on_exit(tmp_path: Path) -> None:
    plan = make_plan(keep="always")
    gateway = existing_session_flow(plan, after=[new_row(**{"@termctl_keep": "always"})])
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"keep_on_exit": "failed"}), encoding="utf-8")
    argv = ["--json", "--config", str(config), "open", "--keep-on-exit", "always", "--", "true"]
    assert Run(argv, gateway, ENVIRON).code == 0
    assert tag_key(plan) in [tuple(c) for c in gateway.calls]


@pytest.mark.usefixtures("fixed_id")
def test_without_argv_the_shell_gets_the_env_on_new_window() -> None:
    plan = make_plan((), (("FOO", "bar"),), name="shell")
    gateway = existing_session_flow(plan, after=[new_row(**{"@termctl_name": "shell",
                                                            "window_name": "shell"})])  # fmt: skip
    result = run(["--env", "FOO=bar"], gateway)
    assert result.code == 0, result.stderr
    new_window = next(c for c in gateway.calls if "new-window" in c)
    assert new_window[-2:] == ["-e", "FOO=bar"]
    assert "respawn-pane" not in " ".join(next(c for c in gateway.calls if "if-shell" in c))


@pytest.mark.usefixtures("fixed_id")
def test_with_argv_only_the_respawn_gets_the_env() -> None:
    plan = make_plan(env=(("FOO", "bar"),))
    gateway = existing_session_flow(plan)
    assert run(["--env", "FOO=bar", "--", "true"], gateway).code == 0
    new_window = next(c for c in gateway.calls if "new-window" in c)
    assert "-e" not in new_window
    assert "'-e' 'FOO=bar'" in next(c for c in gateway.calls if "if-shell" in c)[9]


@pytest.mark.usefixtures("fixed_id")
def test_no_server_starts_one_with_the_new_session_invocation() -> None:
    plan = make_plan()
    files = safe_default_server()
    del files[SOCK]
    start = key(tb.new_session(SESSION, plan.window, start_server=True, env={}))

    def socket_appears(gateway: FakeGateway) -> None:
        gateway.files[SOCK] = socket_file()

    gateway = Flow(
        {start: completed("%5 @3\n"), key(tb.has_session(SESSION)): completed(),
         tag_key(plan): completed()},
        files=files, executables=TRUE, effects={start: socket_appears}, after=[new_row()],
    )  # fmt: skip
    gateway.tag_key = tag_key(plan)
    result = run(["--", "true"], gateway)
    assert result.code == 0, result.stderr
    assert calls(gateway) == ["-V", "set -s", "has-session -t", "if-shell -F", "list-panes"]


@pytest.mark.usefixtures("fixed_id")
def test_every_tmux_call_gets_the_filtered_environment() -> None:
    # Spike C7/D3: a cron-like caller's credentials never reach tmux or a server it starts.
    gateway = existing_session_flow(make_plan())
    secrets = {"AWS_SECRET_ACCESS_KEY": "k", "SSH_AUTH_SOCK": "/s", "TMUX_PANE": "%1",
               "GITHUB_TOKEN": "t", "LANG": "C.UTF-8", "HOME": "/home/u"}  # fmt: skip
    assert run(["--", "true"], gateway, secrets).code == 0
    expected = {**FILTERED, "LANG": "C.UTF-8", "HOME": "/home/u"}
    assert gateway.envs == [expected] * len(gateway.calls)


@pytest.mark.usefixtures("fixed_id")
def test_server_env_allow_decides_what_reaches_tmux(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"server_env_allow": ["PATH", "SSH_*"]}), encoding="utf-8")
    gateway = existing_session_flow(make_plan())
    environ = {**ENVIRON, "SSH_AUTH_SOCK": "/s", "LANG": "C.UTF-8", "HOME": "/h"}
    result = Run(["--json", "--config", str(config), "open", "--", "true"], gateway, environ)
    assert result.code == 0, result.stderr
    assert gateway.envs == [{"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/s"}] * len(gateway.calls)


@pytest.mark.usefixtures("fixed_id")
def test_verbose_never_shows_env_values() -> None:
    plan = make_plan(env=(("PATH", "/usr/bin:/s3cret-path"), ("TOKENISH", "s3cret-value")))
    gateway = existing_session_flow(plan)
    argv = ["--verbose", "--env", "PATH=/usr/bin:/s3cret-path", "--env", "TOKENISH=s3cret-value",
            "--", "true"]  # fmt: skip
    result = run(argv, gateway)
    assert result.code == 0, result.stderr
    assert "s3cret" not in result.stderr + result.stdout
    assert "if-shell" in result.stderr and "TOKENISH" in result.stderr


@pytest.mark.usefixtures("fixed_id")
@pytest.mark.parametrize(
    "tag_result",
    [completed("termctl-nomatch\n"), completed(stderr="can't find pane: %5\n", returncode=1)],
    ids=["nomatch", "failed"],
)
def test_a_failed_tagging_kills_the_pane_and_exits_12(tag_result: Any) -> None:
    gateway = existing_session_flow(make_plan(), tag_result)
    err = error(run(["--", "true"], gateway), "tag_failed", 12)
    assert "details" not in err
    assert calls(gateway)[-1] == "if-shell -F"
    assert gateway.calls[-1] == list(key(tb.cleanup_kill("%5", "@3")))


@pytest.mark.usefixtures("fixed_id")
def test_a_long_argv_fails_the_size_check_before_any_tmux_call() -> None:
    gateway = FakeGateway(panes=[], executables=TRUE)
    err = error(run(["--", "true", "x" * 12500], gateway), "usage", 2)
    assert "12288" in err["message"]
    assert gateway.calls == []


# --- review round 1 -----------------------------------------------------------------------------


def no_tmux_creation(gateway: FakeGateway) -> None:
    """No call that could create or change anything ran: only ``tmux -V`` and reads."""
    assert set(calls(gateway)) <= {"-V", "list-panes"}
    assert gateway.mkdirs == []


@pytest.mark.usefixtures("fixed_id")
@pytest.mark.parametrize("hook", ["pre-open", "post-open"])
def test_open_hooks_in_config_refuse_before_anything_is_created(hook: str, tmp_path: Path) -> None:
    # R1-1: hooks do not run yet (#17), so a configured veto must not fail open.
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"hooks": {hook: [{"cmd": "exit 1"}]}}), encoding="utf-8")
    gateway = existing_session_flow(make_plan())
    result = Run(["--json", "--config", str(config), "open", "--", "true"], gateway, ENVIRON)
    err = error(result, "internal", 1)
    assert "hooks" in err["message"]
    no_tmux_creation(gateway)


@pytest.mark.usefixtures("fixed_id")
@pytest.mark.parametrize("hook", ["pre-open=exit 1", "post-open=true"])
def test_open_hooks_given_with_the_flag_refuse_before_anything_is_created(hook: str) -> None:
    gateway = existing_session_flow(make_plan())
    err = error(run(["--hook", hook, "--", "true"], gateway), "internal", 1)
    assert "hooks" in err["message"]
    no_tmux_creation(gateway)


@pytest.mark.usefixtures("fixed_id")
def test_close_hooks_do_not_refuse_open(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"hooks": {"pre-close": [{"cmd": "exit 1"}]}}), "utf-8")
    gateway = existing_session_flow(make_plan())
    result = Run(["--json", "--config", str(config), "open", "--", "true"], gateway, ENVIRON)
    assert result.code == 0, result.stderr


def test_usage_errors_win_over_the_hook_refusal() -> None:
    gateway = FakeGateway(panes=[], executables=TRUE)
    error(run(["--hook", "pre-open=exit 1", "--", "A=b"], gateway), "usage", 2)
    assert gateway.calls == []


# An invalid managed pane: the reader ignores it with a warning (interface.md "Pane options").
INVALID = pane_row("%9", OTHER, {"window_id": "@9", "@termctl_window": "@9", "@termctl_v": "99"})


@pytest.mark.usefixtures("fixed_id")
def test_a_warning_seen_by_both_reads_is_printed_once() -> None:
    # R1-5: the id-collision read and the read after tagging both see the invalid pane.
    gateway = existing_session_flow(make_plan(), after=[new_row(), INVALID])
    gateway.panes = [INVALID]
    result = run(["--", "true"], gateway)
    assert result.code == 0, result.stderr
    assert result.stderr.count("warning:") == 1, result.stderr
    assert "%9" in result.stderr


@pytest.mark.usefixtures("fixed_id")
def test_a_warning_only_the_read_after_tagging_sees_is_printed() -> None:
    gateway = existing_session_flow(make_plan(), after=[new_row(), INVALID])
    result = run(["--", "true"], gateway)
    assert result.code == 0, result.stderr
    assert result.stderr.count("warning:") == 1, result.stderr
    assert "%9" in result.stderr


@pytest.mark.usefixtures("fixed_id")
def test_a_failed_cleanup_after_a_failed_tagging_warns_about_the_window() -> None:
    # R1-7: the cleanup guard did not match, so the window stays; the warning names it.
    gateway = existing_session_flow(make_plan(), completed("termctl-nomatch\n"))
    gateway.responses[key(tb.cleanup_kill("%5", "@3"))] = completed("termctl-nomatch\n")
    result = run(["--", "true"], gateway)
    error(result, "tag_failed", 12)
    assert "warning:" in result.stderr and "%5" in result.stderr and "@3" in result.stderr


@pytest.mark.usefixtures("fixed_id")
def test_a_tagging_that_found_no_server_has_no_cleanup() -> None:
    # R1-6: no connect after a no-server result without the socket checks.
    plan = make_plan()
    gateway = existing_session_flow(
        plan, completed(stderr=f"no server running on {SOCK}\n", returncode=1)
    )
    error(run(["--", "true"], gateway), "tag_failed", 12)
    assert calls(gateway)[-1] == "if-shell -F"
    assert list(key(tb.cleanup_kill("%5", "@3"))) not in gateway.calls
