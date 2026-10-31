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

from .fakes import (
    TMUX,
    UID,
    FakeGateway,
    completed,
    directory,
    is_list_panes,
    pane_row,
    safe_default_server,
)
from .schemas import validate
from .test_cli import ENVIRON, Run

SOCK = f"/tmp/tmux-{UID}/default"
ID = "tc-abcdefgh"
OTHER = "tc-zzzzzzzz"
SESSION = "termctl"
TRUE = {TMUX, "/usr/bin/true"}


def spec(name: str = "true", cwd: str = "/") -> tb.WindowSpec:
    return open_cmd.window_spec(name, cwd)


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
    result = run(["--", "tool"], gateway, {"PATH": "/usr/bin:/opt/bin"})
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


# --- step 5 and on: not wired in yet -------------------------------------------------------------


@pytest.mark.parametrize("server", [True, False], ids=["server", "no-server"])
def test_open_refuses_before_creating_anything(server: bool) -> None:
    # TODO(#15): open stops after step 3 until the pane step lands; step 5 is covered by
    # test_open_cmd.py and test_open_integration.py.
    files = safe_default_server()
    if not server:
        del files[SOCK], files[f"/tmp/tmux-{UID}"]
    gateway = FakeGateway(panes=[], executables=TRUE, files=files)
    err = error(run(["--", "true"], gateway), "internal", 1)
    assert "not implemented yet" in err["message"]
    assert calls(gateway) == (["-V", "list-panes"] if server else ["-V"])
    assert gateway.mkdirs == []
