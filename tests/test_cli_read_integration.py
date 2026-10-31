"""`show`, `exists` and `list` end to end against real tmux: the whole check order (tmux
binary and version, socket resolution and safety checks, the pane reader) through ``main``
with the system gateway, on a private server (``tmux_server`` in conftest.py).

The CI ``tmux`` job runs these on the runner's tmux; tmux 3.2 runs are TODO(#25).
"""

from __future__ import annotations

import io
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from termctl.cli import main

from .conftest import TMUX_VERSION, TmuxServer, wait_for
from .schemas import validate
from .test_panes_integration import ID, OTHER_ID, new_window, tag

pytestmark = pytest.mark.tmux

NO_CONFIG = str(Path(__file__).parent / "no-config")


@dataclass
class Outcome:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)


def termctl(server: TmuxServer, *argv: str, socket_path: str | None = None) -> Outcome:
    stdout, stderr = io.StringIO(), io.StringIO()
    environ = {**server.env, "XDG_CONFIG_HOME": NO_CONFIG}
    code = main(
        ["--socket", socket_path or server.socket_path, *argv],
        environ=environ,
        stdout=stdout,
        stderr=stderr,
    )
    return Outcome(code, stdout.getvalue(), stderr.getvalue())


def started(server: TmuxServer) -> str:
    """Start the server with one managed pane carrying ``ID``; returns its pane id."""
    pane_id = server.start()
    tag(server, pane_id)
    return pane_id


# --- a running server ---------------------------------------------------------------------------


def test_show_a_running_pane(tmux_server: TmuxServer) -> None:
    pane_id = started(tmux_server)
    result = termctl(tmux_server, "--json", "show", "abcd")
    assert (result.code, result.stderr) == (0, "")
    obj = result.json()
    validate(obj, "termctl/cli/show.json")
    assert (obj["id"], obj["pane_id"], obj["session"], obj["state"]) == (
        ID,
        pane_id,
        "base",
        "running",
    )
    assert obj["socket_path"] == tmux_server.socket_path
    human = termctl(tmux_server, "show", ID)
    window_name = tmux_server.raw("display", "-p", "-t", pane_id, "#{window_name}").stdout.strip()
    assert human.stdout == f"{ID} running base {window_name}\n"


def test_exists_found_and_not_found(tmux_server: TmuxServer) -> None:
    started(tmux_server)
    found = termctl(tmux_server, "--json", "exists", ID)
    assert found.code == 0
    validate(found.json(), "termctl/cli/exists.json")
    assert found.json()["id"] == ID
    missing = termctl(tmux_server, "--json", "exists", OTHER_ID)
    assert (missing.code, missing.stderr) == (10, "")
    validate(missing.json(), "termctl/cli/exists.json")
    assert missing.json() == {
        "input": OTHER_ID,
        "id": None,
        "exists": False,
        "socket_path": tmux_server.socket_path,
    }
    assert termctl(tmux_server, "exists", ID).stdout == ""


def test_list_with_filters(tmux_server: TmuxServer) -> None:
    started(tmux_server)
    tmux_server.raw("new-session", "-d", "-s", "other", "--", "/usr/bin/env", "--", "sleep", "600")
    other = new_window(tmux_server, target="=other:")
    tag(tmux_server, other, OTHER_ID, **{"@termctl_created": "2026-10-30T12:00:01.000Z"})
    tmux_server.raw("new-window", "-d", "-t", "=base:")  # unmanaged
    result = termctl(tmux_server, "--json", "list")
    assert result.code == 0
    validate(result.json(), "termctl/cli/list.json")
    assert [p["id"] for p in result.json()["panes"]] == [ID, OTHER_ID]
    by_session = termctl(tmux_server, "--json", "list", "--session", "other").json()
    assert [p["id"] for p in by_session["panes"]] == [OTHER_ID]
    assert termctl(tmux_server, "--json", "list", "--label", "kind=x").json()["panes"] == []
    assert termctl(tmux_server, "--json", "list", "--dead").json()["panes"] == []
    assert len(termctl(tmux_server, "list").stdout.splitlines()) == 2


def test_a_copied_full_id_is_ambiguous(tmux_server: TmuxServer) -> None:
    first = started(tmux_server)
    copy = new_window(tmux_server)
    tag(tmux_server, copy)  # a process in the pane copying another pane's options
    for command in ("show", "exists"):
        result = termctl(tmux_server, "--json", command, ID)
        assert result.code == 21
        error = result.json()["error"]
        assert error["code"] == "ambiguous"
        assert sorted(c["pane_id"] for c in error["candidates"]) == sorted([first, copy])


def test_a_forged_but_valid_option_is_reported_as_read(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    tag(tmux_server, pane_id, **{"@termctl_name": "trusted-by-nobody", "@termctl_labels": "a=b"})
    obj = termctl(tmux_server, "--json", "show", ID).json()
    assert (obj["name"], obj["labels"]) == ("trusted-by-nobody", ["a=b"])


def test_tmux_text_is_escaped_in_human_output(tmux_server: TmuxServer) -> None:
    pane_id = started(tmux_server)
    assert tmux_server.raw("rename-window", "-t", pane_id, "w\x1b[2J\x07\u009b").returncode == 0
    for argv in (("show", ID), ("list",)):
        out = termctl(tmux_server, *argv).stdout
        assert out.startswith(f"{ID} running base w")
        assert not any(ord(c) < 0x20 and c != "\n" or 0x7F <= ord(c) <= 0x9F for c in out)
    shown = termctl(tmux_server, "--json", "show", ID).json()["display_name"]
    assert shown.startswith("w") and "\\x" not in shown  # --json is not escaped as human output


def test_a_forged_invalid_pane_is_ignored_with_an_escaped_warning(tmux_server: TmuxServer) -> None:
    started(tmux_server)
    bad = new_window(tmux_server)
    tag(tmux_server, bad, OTHER_ID, **{"@termctl_name": "x\u009by"})
    result = termctl(tmux_server, "list")
    assert result.code == 0 and len(result.stdout.splitlines()) == 1
    assert result.stderr == (
        f"termctl: warning: ignoring managed pane {bad}: invalid @termctl_name "
        "'x\\x9by': must not contain control characters\n"
    )


# --- dead panes ---------------------------------------------------------------------------------


def _dead(server: TmuxServer, script: str) -> str:
    server.start()
    result = server.raw(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "=base:",
        "--", "/usr/bin/env", "--", "sh", "-c", f"sleep 0.3; {script}",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    pane_id = result.stdout.strip()
    assert server.raw("set", "-p", "-t", pane_id, "remain-on-exit", "on").returncode == 0
    tag(server, pane_id, **{"@termctl_keep": "always"})
    assert wait_for(lambda: termctl(server, "--json", "show", ID).json()["state"] == "dead")
    return pane_id


def test_show_a_dead_pane_with_its_exit_status(tmux_server: TmuxServer) -> None:
    _dead(tmux_server, "exit 3")
    assert wait_for(lambda: termctl(tmux_server, "--json", "show", ID).json()["exit_status"] == 3)
    obj = termctl(tmux_server, "--json", "show", ID).json()
    validate(obj, "termctl/cli/show.json")
    assert (obj["state"], obj["exit_signal"]) == ("dead", None)
    assert termctl(tmux_server, "exists", ID).code == 0
    listed = termctl(tmux_server, "--json", "list", "--dead").json()["panes"]
    assert [p["id"] for p in listed] == [ID]


def test_show_a_pane_killed_by_a_signal(tmux_server: TmuxServer) -> None:
    _dead(tmux_server, "kill -9 $$")
    if TMUX_VERSION is not None and TMUX_VERSION < (3, 3):
        # tmux 3.2 has no #{pane_dead_signal}: both exit fields are null.
        obj = termctl(tmux_server, "--json", "show", ID).json()
        assert (obj["exit_status"], obj["exit_signal"]) == (None, None)
        return
    assert wait_for(lambda: termctl(tmux_server, "--json", "show", ID).json()["exit_signal"] == 9)
    obj = termctl(tmux_server, "--json", "show", ID).json()
    validate(obj, "termctl/cli/show.json")
    assert obj["exit_status"] is None


# --- no server ----------------------------------------------------------------------------------


def _assert_no_server(server: TmuxServer, socket_path: str) -> None:
    listed = termctl(server, "--json", "list", socket_path=socket_path)
    assert (listed.code, listed.json()) == (0, {"socket_path": socket_path, "panes": []})
    exists = termctl(server, "--json", "exists", ID, socket_path=socket_path)
    assert exists.code == 10
    validate(exists.json(), "termctl/cli/exists.json")
    assert exists.json()["exists"] is False
    shown = termctl(server, "--json", "show", ID, socket_path=socket_path)
    assert (shown.code, shown.json()["error"]["code"]) == (10, "no_server")
    assert shown.json()["error"]["details"] == {"socket_path": socket_path}


def test_no_socket_file(tmux_server: TmuxServer) -> None:
    _assert_no_server(tmux_server, tmux_server.socket_path)
    assert not os.path.exists(tmux_server.socket_path)


def test_missing_parent_directory(tmux_server: TmuxServer) -> None:
    path = str(tmux_server.root / "missing" / "sock")
    _assert_no_server(tmux_server, path)
    assert not os.path.exists(os.path.dirname(path))  # nothing is created


def test_a_refused_connection(tmux_server: TmuxServer) -> None:
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(tmux_server.socket_path)
    stale.close()
    _assert_no_server(tmux_server, tmux_server.socket_path)


def test_a_killed_server(tmux_server: TmuxServer) -> None:
    started(tmux_server)
    assert tmux_server.raw("kill-server").returncode == 0
    _assert_no_server(tmux_server, tmux_server.socket_path)
    assert not tmux_server.alive()  # no read started a server


def test_a_server_without_sessions(tmux_server: TmuxServer) -> None:
    result = tmux_server.raw(
        "-f", "/dev/null", "start-server", ";", "set", "-s", "exit-empty", "off"
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    listed = termctl(tmux_server, "--json", "list")
    assert (listed.code, listed.json()["panes"]) == (0, [])
    shown = termctl(tmux_server, "--json", "show", ID)
    assert (shown.code, shown.json()["error"]["code"]) == (10, "not_found")
    assert termctl(tmux_server, "exists", ID).code == 10


def test_an_unsafe_socket_directory_exits_12(tmux_server: TmuxServer) -> None:
    started(tmux_server)
    os.chmod(tmux_server.root, 0o777)
    try:
        for argv in (("list",), ("exists", ID), ("show", ID)):
            result = termctl(tmux_server, "--json", *argv)
            assert (result.code, result.json()["error"]["code"]) == (12, "server_unavailable")
    finally:
        os.chmod(tmux_server.root, 0o700)
