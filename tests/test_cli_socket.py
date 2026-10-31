"""Server selection and socket safety in the check order (steps 2 and 3)."""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from termctl import cli
from termctl.cli import Context, Result, main

from .fakes import TMUX, UID, FakeClock, FakeGateway, directory, safe_default_server, socket_file
from .schemas import validate

GOLDEN = Path(__file__).parent / "golden"
NO_CONFIG = {"XDG_CONFIG_HOME": str(Path(__file__).parent / "no-config")}
# A PATH on which the fake gateway finds tmux: the binary checks come first in step 3.
ENVIRON = {**NO_CONFIG, "PATH": "/usr/bin"}
# The only tmux call before the socket checks: the version check.
VERSION_ONLY = [[TMUX, "-V"]]
ID = "tc-abcdefgh"
TMUX_COMMANDS = [
    ["open"],
    ["show", ID],
    ["exists", ID],
    ["list"],
    ["rename", ID, "n"],
    ["mark", ID, "attention"],
    ["unmark", ID],
    ["focus", ID],
    ["close", ID],
    ["label", "add", ID, "k=v"],
    ["label", "remove", ID, "k=v"],
]


def run(
    argv: list[str], gateway: FakeGateway, environ: Mapping[str, str] | None = None
) -> tuple[int, Any, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(
        ["--json", *argv],
        gateway=gateway,
        clock=FakeClock(),
        environ={**ENVIRON, **(environ or {})},
        stdout=stdout,
        stderr=stderr,
    )
    return code, json.loads(stdout.getvalue()), stderr.getvalue()


def unsafe_ancestor() -> FakeGateway:
    files = safe_default_server()
    files["/tmp"] = directory(0o777)  # world-writable without the sticky bit
    return FakeGateway(files=files)


@pytest.mark.parametrize("argv", TMUX_COMMANDS)
def test_unsafe_ancestor_exits_12_and_never_connects(argv: list[str]) -> None:
    gateway = unsafe_ancestor()
    code, envelope, _ = run(argv, gateway)
    validate(envelope, "termctl/cli/error.json")
    assert code == 12
    assert envelope["error"]["code"] == "server_unavailable"
    assert gateway.calls == VERSION_ONLY


def test_server_unavailable_golden() -> None:
    expected = json.loads((GOLDEN / "error-server-unavailable.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/error.json")
    assert run(["show", ID], unsafe_ancestor())[1] == expected


def test_ancestor_owned_by_another_user_exits_12() -> None:
    files = {"/": directory(), "/srv": directory(0o755, UID + 1)}
    files["/srv/t"] = directory(0o700, UID)
    files["/srv/t/s"] = socket_file()
    code, envelope, _ = run(["--socket", "/srv/t/s", "list"], FakeGateway(files=files))
    assert (code, envelope["error"]["details"]["path"]) == (12, "/srv")


ID_COMMANDS = [c for c in TMUX_COMMANDS if c[0] not in ("open", "list", "exists")]


@pytest.mark.parametrize("argv", ID_COMMANDS)
def test_missing_socket_is_no_server(argv: list[str]) -> None:
    files = safe_default_server()
    del files[f"/tmp/tmux-{UID}/default"]
    code, envelope, _ = run(argv, FakeGateway(files=files))
    validate(envelope, "termctl/cli/error.json")
    assert (code, envelope["error"]["code"]) == (10, "no_server")
    assert envelope["error"]["details"] == {"socket_path": f"/tmp/tmux-{UID}/default"}


def test_missing_default_directory_is_no_server_and_creates_nothing() -> None:
    gateway = FakeGateway(files={"/": directory(), "/tmp": directory(0o1777)})
    code, envelope, _ = run(["show", ID], gateway)
    assert (code, envelope["error"]["code"]) == (10, "no_server")
    assert gateway.calls == VERSION_ONLY


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[Context]:
    seen: list[Context] = []

    def capture(args: Any, ctx: Context) -> Result:
        seen.append(ctx)
        return Result({}, "")

    monkeypatch.setattr(cli, "run_command", capture)
    return seen


def test_context_carries_the_resolved_server(captured: list[Context]) -> None:
    files = safe_default_server()
    files["/srv"] = directory()
    files["/srv/s"] = socket_file()
    run(["show", ID], FakeGateway(files=files), {"TMUX": "/srv/s,99,0"})
    server = captured[0].server
    assert server is not None
    assert (server.socket_path, server.source) == ("/srv/s", "TMUX")
    assert captured[0].socket_present is True


def test_flag_wins_over_termctl_socket_and_tmux(captured: list[Context]) -> None:
    environ = {"TERMCTL_SOCKET": "envname", "TMUX": "/srv/s,1,0"}
    run(["--socket", "flagname", "list"], FakeGateway(), environ)
    assert captured[0].server is not None
    assert captured[0].server.socket_path == f"/tmp/tmux-{UID}/flagname"


def test_termctl_socket_wins_over_tmux(captured: list[Context]) -> None:
    run(["list"], FakeGateway(), {"TERMCTL_SOCKET": "envname", "TMUX": "/srv/s,1,0"})
    assert captured[0].server is not None
    assert captured[0].server.socket_path == f"/tmp/tmux-{UID}/envname"


def test_list_and_open_reach_their_steps_without_a_server(captured: list[Context]) -> None:
    files = safe_default_server()
    del files[f"/tmp/tmux-{UID}/default"]
    for argv in (["list"], ["open"], ["exists", ID]):
        run(argv, FakeGateway(files=files, dirs={"/"}))
    assert [ctx.socket_present for ctx in captured] == [False, False, False]


def test_placements_never_resolves_a_server(captured: list[Context]) -> None:
    gateway = unsafe_ancestor()
    code, _, _ = run(["placements"], gateway, {"TMUX": "relative,1,0"})
    assert code == 0
    assert gateway.lstats == []
    assert captured[0].server is None


def test_invalid_tmux_exits_2_naming_the_variable() -> None:
    code, envelope, _ = run(["list"], FakeGateway(), {"TMUX": "relative,1,0"})
    validate(envelope, "termctl/cli/error.json")
    assert (code, envelope["error"]["code"]) == (2, "config_invalid")
    assert envelope["error"]["details"] == {"variable": "TMUX"}


def test_relative_socket_over_100_bytes_after_resolution_is_usage() -> None:
    cwd = "/" + "c" * 91
    gateway = FakeGateway(cwd=cwd, files={"/": directory(), cwd: directory(uid=UID)})
    code, envelope, _ = run(["--socket", "sub/sock", "list"], gateway)
    assert (code, envelope["error"]["code"]) == (2, "usage")


def test_control_characters_in_socket_flag_exit_2_before_checks() -> None:
    files = {"/": directory(), "/s\x1b[2J": directory(0o777)}
    code, _, stderr = run(["--socket", "/s\x1b[2J/x", "list"], FakeGateway(files=files))
    # The grammar rejects control characters in the path as given (exit 2), before any check.
    assert code == 2
    assert "\x1b" not in stderr
