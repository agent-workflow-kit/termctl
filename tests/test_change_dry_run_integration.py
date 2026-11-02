"""`--dry-run` of `rename`, `mark`, `unmark`, `label add` and `label remove` end to end against
real tmux, through ``main`` with the system gateway, on a private server (``tmux_server`` in
conftest.py): the pane is reported as the real run then makes it, and nothing changes: no
option, no window name, no bell, no lock file and no log record (interface.md "Dry runs").
The one exception is ``display_name`` of a renamed window: the dry run reports the name termctl
computes, the real run the form tmux reads back, which can be escaped (interface.md "Names").

The CI ``tmux`` job runs these on the runner's tmux; tmux 3.2 runs are TODO(#25).
"""

from __future__ import annotations

from typing import Any

import pytest

from termctl.display import _tmux_forms
from termctl.tmux_bin import TmuxVersion

from .conftest import TMUX_VERSION, TmuxServer, wait_for
from .schemas import validate
from .test_mark_integration import bell_flag, config
from .test_panes_integration import ID, new_window, tag
from .test_rename_integration import managed, option, termctl, window_name

pytestmark = pytest.mark.tmux

OPTIONS = ("@termctl_name", "@termctl_marker", "@termctl_marker_text", "@termctl_labels")

# Each command with its schema; the pane starts as `managed` leaves it (KEY-12, no marker,
# kind=review) after `setup`, run for real.
CASES: list[tuple[list[str], str, list[str]]] = [
    (["rename", ID, "new name"], "termctl/cli/rename.json", []),
    (["mark", ID, "attention"], "termctl/cli/mark.json", []),
    (["unmark", ID], "termctl/cli/unmark.json", ["mark", ID, "attention"]),
    (["label", "add", ID, "repo=app", "a=1"], "termctl/cli/label-add.json", []),
    (["label", "remove", ID, "kind=review"], "termctl/cli/label-remove.json", []),
]


def snapshot(server: TmuxServer, pane_id: str) -> dict[str, str]:
    values = {name: option(server, pane_id, name) for name in OPTIONS}
    values["window_name"] = window_name(server, pane_id)
    values["window_bell_flag"] = server.raw(
        "display", "-p", "-t", pane_id, "#{window_bell_flag}"
    ).stdout
    return values


def comparable(out: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in out.items() if key != "dry_run"}


@pytest.mark.parametrize(("argv", "schema", "setup"), CASES, ids=lambda v: str(v))
def test_a_dry_run_reports_what_the_real_run_does_and_changes_nothing(
    tmux_server: TmuxServer, argv: list[str], schema: str, setup: list[str]
) -> None:
    pane_id = managed(tmux_server)
    if setup:
        assert termctl(tmux_server, *setup).code == 0
    state = tmux_server.root / ".local" / "state" / "termctl"
    log_lines = (state / "log.jsonl").read_text() if (state / "log.jsonl").exists() else ""
    locks = sorted((state / "locks").iterdir()) if (state / "locks").exists() else []
    before = snapshot(tmux_server, pane_id)

    dry = termctl(tmux_server, "--json", *argv, "--dry-run")
    assert (dry.code, dry.stderr) == (0, "")
    out = dry.json()
    validate(out, schema)
    assert (out["dry_run"], out["changed"]) == (True, True)
    assert snapshot(tmux_server, pane_id) == before
    current = (state / "log.jsonl").read_text() if (state / "log.jsonl").exists() else ""
    assert current == log_lines
    assert (sorted((state / "locks").iterdir()) if (state / "locks").exists() else []) == locks

    real = termctl(tmux_server, "--json", *argv)
    assert real.code == 0
    assert comparable(out) == comparable(real.json())


def test_a_bell_marker_dry_run_rings_no_bell(tmux_server: TmuxServer) -> None:
    # The setup of test_mark_integration's bell test, where a real mark sets the flag.
    tmux_server.start()
    pane_id = new_window(tmux_server)  # -d: not the session's current window
    tag(tmux_server, pane_id)
    flags = config(tmux_server, "markers:\n  attention:\n    bell: true\n")
    dry = termctl(tmux_server, *flags, "mark", ID, "attention", "--dry-run")
    assert (dry.code, dry.stderr) == (0, "")
    assert not wait_for(lambda: bell_flag(tmux_server, pane_id) == "1", timeout=0.5)
    assert option(tmux_server, pane_id, "@termctl_marker") == ""


def test_a_dry_run_on_a_split_window_reports_the_current_window_name(
    tmux_server: TmuxServer,
) -> None:
    pane_id = managed(tmux_server)
    tmux_server.raw("split-window", "-d", "-t", pane_id, "--", "/usr/bin/env", "--", "sleep", "600")
    shown = window_name(tmux_server, pane_id)
    dry = termctl(tmux_server, "--json", "rename", ID, "new", "--dry-run")
    out = dry.json()
    assert (out["name"], out["display_name"], out["display_updated"]) == ("new", shown, False)
    assert option(tmux_server, pane_id) == "KEY-12"


def test_a_dry_run_reports_the_computed_name_where_tmux_reports_it_escaped(
    tmux_server: TmuxServer,
) -> None:
    # tmux stores a renamed window's name escaped (interface.md "Names"): the dry run reports
    # the name termctl computes, the real run the form tmux reads back.
    pane_id = managed(tmux_server)
    name = "a\\b $c"
    dry = termctl(tmux_server, "--json", "rename", ID, name, "--dry-run")
    assert (dry.code, dry.stderr) == (0, "")
    out = dry.json()
    assert out["display_name"] == name
    real = termctl(tmux_server, "--json", "rename", ID, name)
    assert real.code == 0
    reported = real.json()
    assert reported["display_name"] == window_name(tmux_server, pane_id)
    assert reported["display_name"] != name
    assert reported["display_name"] in _tmux_forms(name, version())
    del out["display_name"], reported["display_name"]
    assert comparable(out) == comparable(reported)


def version() -> TmuxVersion | None:
    if TMUX_VERSION is None:
        return None
    major, minor = TMUX_VERSION
    return TmuxVersion(major, minor, f"{major}.{minor}")
