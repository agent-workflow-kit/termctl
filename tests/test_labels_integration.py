"""`label add` and `label remove` end to end against real tmux, through ``main`` with the system
gateway, on a private server (``tmux_server`` in conftest.py): the stored value round-trips in
canonical form through the reader, no-ops, the limit, dead panes, guarded writes and the window
left alone (interface.md `label add` / `label remove`, "Labels").

The CI ``tmux`` jobs run these on tmux 3.2a (the floor) and on the runner image's tmux.
"""

from __future__ import annotations

import pytest

from .conftest import TmuxServer, wait_for
from .schemas import validate
from .test_panes_integration import ID, OTHER_ID, new_window, tag
from .test_rename_integration import Interposing, log, managed, option, termctl, window_name

pytestmark = pytest.mark.tmux

LABELS = "@termctl_labels"


def test_label_add_and_remove_round_trip_through_the_reader(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    tmux_server.raw("rename-window", "-t", pane_id, "kept")
    added = termctl(tmux_server, "--json", "label", "add", "abcd", "z=1", "a=PR#42", "z=1")
    assert (added.code, added.stderr) == (0, "")
    out = added.json()
    validate(out, "termctl/cli/label-add.json")
    assert out["labels"] == ["a=PR#42", "kind=review", "z=1"]
    assert (out["changed"], out["display_updated"], out["dry_run"]) == (True, False, False)
    assert option(tmux_server, pane_id, LABELS) == "a=PR#42 kind=review z=1"
    assert window_name(tmux_server, pane_id) == "kept"
    shown = termctl(tmux_server, "--json", "show", ID).json()
    assert shown["labels"] == ["a=PR#42", "kind=review", "z=1"]

    removed = termctl(tmux_server, "label", "remove", ID, "kind=review", "absent=x")
    assert (removed.code, removed.stdout) == (0, "a=PR#42 z=1\n")
    assert option(tmux_server, pane_id, LABELS) == "a=PR#42 z=1"
    records = log(tmux_server)
    for record in records:
        validate(record, "termctl/log.json")
    assert [(r["event"], r["result"], r["id"]) for r in records] == [
        ("label_add", "ok", ID),
        ("label_remove", "ok", ID),
    ]


def test_removing_every_label_stores_an_empty_value(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    result = termctl(tmux_server, "--json", "label", "remove", ID, "kind=review")
    assert result.code == 0, result.stderr
    assert result.json()["labels"] == []
    assert option(tmux_server, pane_id, LABELS) == ""
    assert termctl(tmux_server, "--json", "show", ID).json()["labels"] == []


def test_no_ops_write_nothing_and_log_nothing(tmux_server: TmuxServer) -> None:
    managed(tmux_server)
    for argv in (["add", ID, "kind=review"], ["remove", ID, "absent=x"]):
        result = termctl(tmux_server, "--json", "label", *argv)
        assert (result.code, result.json()["changed"]) == (0, False), result.stderr
    assert log(tmux_server) == []


def test_the_limit_is_logged_and_changes_nothing(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    pairs = [f"k{index:02d}=v" for index in range(31)]
    assert termctl(tmux_server, "label", "add", ID, *pairs).code == 0
    before = option(tmux_server, pane_id, LABELS)
    assert len(before.split(" ")) == 32
    result = termctl(tmux_server, "--json", "label", "add", ID, "one=more")
    assert (result.code, result.json()["error"]["code"]) == (2, "usage")
    assert option(tmux_server, pane_id, LABELS) == before
    record = log(tmux_server)[-1]
    validate(record, "termctl/log.json")
    assert (record["event"], record["exit_code"], record["id"]) == ("label_add", 2, ID)


def test_a_dead_pane_accepts_label_changes(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    pane_id = new_window(tmux_server)
    tmux_server.raw("set", "-p", "-t", pane_id, "remain-on-exit", "on")
    tag(tmux_server, pane_id)
    tmux_server.raw("respawn-pane", "-k", "-t", pane_id, "--", "/usr/bin/env", "--", "true")
    assert wait_for(
        lambda: tmux_server.raw("display", "-p", "-t", pane_id, "#{pane_dead}").stdout == "1\n"
    )
    result = termctl(tmux_server, "--json", "label", "add", ID, "x=1")
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, "termctl/cli/label-add.json")
    assert (out["state"], out["labels"]) == ("dead", ["kind=review", "x=1"])
    assert option(tmux_server, pane_id, LABELS) == "kind=review x=1"


def test_a_restarted_server_reusing_the_pane_id_is_never_acted_on(
    tmux_server: TmuxServer,
) -> None:
    pane_id = managed(tmux_server)

    def restart() -> None:
        tmux_server.raw("kill-server")
        assert wait_for(lambda: not tmux_server.alive())
        assert tmux_server.start() == pane_id
        tag(tmux_server, pane_id, OTHER_ID, **{LABELS: "theirs=1"})

    result = termctl(tmux_server, "--json", "label", "add", ID, "x=1", gateway=Interposing(restart))
    assert (result.code, result.json()["error"]["code"]) == (10, "not_found")
    assert option(tmux_server, pane_id, LABELS) == "theirs=1"
    [record] = log(tmux_server)
    assert (record["event"], record["exit_code"], record["id"]) == ("label_add", 10, ID)
