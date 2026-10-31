"""`mark` and `unmark` end to end against real tmux, through ``main`` with the system gateway,
on a private server (``tmux_server`` in conftest.py): both marker options written together,
the displayed name, the no-op rules under supervise-style polling, the bell, dead panes and
forged control bytes (interface.md "Markers", `mark`, `unmark`, "Names").

The CI ``tmux`` job runs these on the runner's tmux; tmux 3.2 runs are TODO(#25).
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from .conftest import TmuxServer, wait_for
from .schemas import validate
from .test_cli_read_integration import NO_CONFIG
from .test_panes_integration import ID, new_window, tag
from .test_rename_integration import log, managed, option, option_bytes, termctl, window_name

pytestmark = pytest.mark.tmux

BELL = "\U0001f514 "


def config(server: TmuxServer, text: str) -> list[str]:
    path = server.root / "termctl-config" / "config.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return ["--config", str(path)]


def test_mark_and_unmark(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    result = termctl(tmux_server, "--json", "mark", ID, "attention")
    assert (result.code, result.stderr) == (0, "")
    out = result.json()
    validate(out, "termctl/cli/mark.json")
    assert (out["marker"], out["marker_text"], out["display_name"]) == (
        "attention",
        BELL,
        BELL + "KEY-12",
    )
    assert (out["changed"], out["display_updated"]) == (True, True)
    assert option(tmux_server, pane_id, "@termctl_marker") == "attention"
    assert option(tmux_server, pane_id, "@termctl_marker_text") == BELL
    assert window_name(tmux_server, pane_id) == BELL + "KEY-12"

    result = termctl(tmux_server, "--json", "unmark", ID)
    assert (result.code, result.stderr) == (0, "")
    out = result.json()
    validate(out, "termctl/cli/unmark.json")
    assert (out["marker"], out["marker_text"], out["display_name"]) == (None, None, "KEY-12")
    assert option(tmux_server, pane_id, "@termctl_marker") == ""
    assert option(tmux_server, pane_id, "@termctl_marker_text") == ""
    assert window_name(tmux_server, pane_id) == "KEY-12"
    records = log(tmux_server)
    for record in records:
        validate(record, "termctl/log.json")
    assert [(r["event"], r["result"]) for r in records] == [("mark", "ok"), ("unmark", "ok")]
    # The pane reader accepts what was written (both options set, then both empty).
    assert termctl(tmux_server, "show", ID).stderr == ""


def test_marker_display_option_keeps_the_window_name(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    tmux_server.raw("rename-window", "-t", pane_id, "KEY-12")
    flags = config(tmux_server, "marker_display: option\n")
    out = termctl(tmux_server, *flags, "--json", "mark", ID, "attention").json()
    assert (out["display_name"], out["marker_text"]) == ("KEY-12", BELL)
    assert window_name(tmux_server, pane_id) == "KEY-12"
    assert option(tmux_server, pane_id, "@termctl_marker_text") == BELL
    # The option is there for a user's status format.
    shown = tmux_server.raw("display", "-p", "-t", pane_id, "#{@termctl_marker_text}#W").stdout
    assert shown == BELL + "KEY-12\n"


@pytest.mark.parametrize("name", ["a\\b", "$HOME", "#{pane_id}", "x;"])
def test_marking_again_is_a_no_op(tmux_server: TmuxServer, name: str) -> None:
    managed(tmux_server)
    assert termctl(tmux_server, "rename", ID, "--", name).code == 0
    assert termctl(tmux_server, "mark", ID, "attention").code == 0
    again = termctl(tmux_server, "--json", "mark", ID, "attention")
    assert (again.code, again.json()["changed"]) == (0, False), again.stderr
    assert termctl(tmux_server, "unmark", ID).code == 0
    again = termctl(tmux_server, "--json", "unmark", ID)
    assert (again.code, again.json()["changed"]) == (0, False), again.stderr
    assert [r["event"] for r in log(tmux_server)] == ["rename", "mark", "unmark"]


def test_a_dead_pane_accepts_mark_and_unmark(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    pane_id = new_window(tmux_server)
    tmux_server.raw("set", "-p", "-t", pane_id, "remain-on-exit", "on")
    tag(tmux_server, pane_id)
    tmux_server.raw("respawn-pane", "-k", "-t", pane_id, "--", "/usr/bin/env", "--", "true")
    assert wait_for(
        lambda: tmux_server.raw("display", "-p", "-t", pane_id, "#{pane_dead}").stdout == "1\n"
    )
    flags = config(tmux_server, "markers:\n  attention:\n    bell: true\n")
    result = termctl(tmux_server, *flags, "--json", "mark", ID, "attention")
    assert (result.code, result.stderr) == (0, "")  # no bell, so no bell warning
    assert (result.json()["state"], result.json()["marker"]) == ("dead", "attention")
    assert window_name(tmux_server, pane_id) == BELL + "KEY-12"
    result = termctl(tmux_server, "--json", "unmark", ID)
    assert (result.code, result.json()["marker"]) == (0, None)
    assert window_name(tmux_server, pane_id) == "KEY-12"


def test_a_marker_removed_from_config_stays_until_unmark(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    flags = config(tmux_server, "markers:\n  review:\n    text: 'R '\n")
    assert termctl(tmux_server, *flags, "mark", ID, "review").code == 0
    # Without that config: shown and kept by rename, refused by mark, cleared by unmark.
    shown = termctl(tmux_server, "--json", "show", ID).json()
    assert (shown["marker"], shown["display_name"]) == ("review", "R KEY-12")
    assert termctl(tmux_server, "rename", ID, "other").stdout == "R other\n"
    assert termctl(tmux_server, "mark", ID, "review").code == 2
    result = termctl(tmux_server, "unmark", ID)
    assert (result.code, result.stdout) == (0, "other\n")
    assert option(tmux_server, pane_id, "@termctl_marker") == ""


def test_a_forged_control_byte_in_the_marker_text_is_rewritten(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    flags = config(tmux_server, "marker_display: option\nmarkers:\n  esc:\n    text: '\\033 '\n")
    # Someone with socket access stores ESC and a space; tmux prints it as the text \033.
    tmux_server.raw("set", "-p", "-t", pane_id, "@termctl_marker", "esc")
    tmux_server.raw("set", "-p", "-t", pane_id, "@termctl_marker_text", "\x1b ")
    assert option(tmux_server, pane_id, "@termctl_marker_text") == "\\033 "
    result = termctl(tmux_server, *flags, "--json", "mark", ID, "esc")
    assert (result.code, result.json()["changed"]) == (0, True), result.stderr
    assert option_bytes(tmux_server, pane_id, "@termctl_marker_text") == 5
    again = termctl(tmux_server, *flags, "--json", "mark", ID, "esc")
    assert again.json()["changed"] is False


# --- the bell -------------------------------------------------------------------------------------


def bell_flag(server: TmuxServer, pane_id: str) -> str:
    return server.raw("display", "-p", "-t", pane_id, "#{window_bell_flag}").stdout.strip()


def test_a_bell_marker_sets_the_window_bell_flag(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    pane_id = new_window(tmux_server)  # -d: not the session's current window
    tag(tmux_server, pane_id)
    flags = config(tmux_server, "markers:\n  attention:\n    bell: true\n")
    assert bell_flag(tmux_server, pane_id) == "0"
    result = termctl(tmux_server, *flags, "mark", ID, "attention")
    assert (result.code, result.stderr) == (0, "")
    assert wait_for(lambda: bell_flag(tmux_server, pane_id) == "1")


def test_without_bell_no_bell_is_written(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    pane_id = new_window(tmux_server)
    tag(tmux_server, pane_id)
    assert termctl(tmux_server, "mark", ID, "attention").code == 0
    # Give a stray BEL time to arrive.
    assert not wait_for(lambda: bell_flag(tmux_server, pane_id) == "1", timeout=0.5)


def test_a_no_op_rings_no_bell(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    pane_id = new_window(tmux_server)
    tag(tmux_server, pane_id)
    # Already marked, with the window showing it: mark changes nothing and rings nothing.
    tag(tmux_server, pane_id, **{"@termctl_marker": "attention", "@termctl_marker_text": BELL})
    tmux_server.raw("rename-window", "-t", pane_id, BELL + "KEY-12")
    flags = config(tmux_server, "markers:\n  attention:\n    bell: true\n")
    result = termctl(tmux_server, *flags, "--json", "mark", ID, "attention")
    assert (result.code, result.json()["changed"]) == (0, False)
    assert not wait_for(lambda: bell_flag(tmux_server, pane_id) == "1", timeout=0.5)


# --- supervise-style polling ----------------------------------------------------------------------

POLLED = 20


def _ids() -> list[str]:
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    return [f"tc-pollin{alphabet[i // 32]}{alphabet[i % 32]}" for i in range(POLLED)]


def _poll(server: TmuxServer, argvs: list[list[str]]) -> list[subprocess.CompletedProcess[str]]:
    """Run each argv as its own termctl process, all at once, as a supervisor polling its
    panes would: they contend for the server lock for real."""
    env = {**server.env, "XDG_CONFIG_HOME": NO_CONFIG}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "termctl", "--socket", server.socket_path, "--json", *argv],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for argv in argvs
    ]
    results = []
    for proc, argv in zip(procs, argvs, strict=True):
        stdout, stderr = proc.communicate(timeout=60)
        results.append(subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr))
    return results


def test_polling_mark_and_unmark_on_twenty_panes_writes_nothing(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    ids = _ids()
    for tc_id in ids:
        tag(tmux_server, new_window(tmux_server), tc_id)
    marking = [["mark", tc_id, "attention"] for tc_id in ids]
    first = _poll(tmux_server, marking)
    assert all(r.returncode == 0 for r in first), [r.stderr for r in first]
    assert all(json.loads(r.stdout)["changed"] for r in first)
    assert len(log(tmux_server)) == POLLED
    # Three polls of every pane, each all at once: nothing changes, nothing is logged, and no
    # poll fails on the lock.
    for _ in range(3):
        polled = _poll(tmux_server, marking)
        assert [(r.returncode, r.stderr) for r in polled] == [(0, "")] * POLLED
        assert not any(json.loads(r.stdout)["changed"] for r in polled)
    assert len(log(tmux_server)) == POLLED
    unmarking = [["unmark", tc_id] for tc_id in ids]
    assert all(r.returncode == 0 for r in _poll(tmux_server, unmarking))
    for _ in range(2):
        polled = _poll(tmux_server, unmarking)
        assert [(r.returncode, r.stderr) for r in polled] == [(0, "")] * POLLED
        assert not any(json.loads(r.stdout)["changed"] for r in polled)
    assert len(log(tmux_server)) == 2 * POLLED
