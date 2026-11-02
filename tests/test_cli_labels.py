"""`label add` and `label remove` through the whole check order against a fake tmux
(interface.md `label add` / `label remove`, "Labels", "Check order", "Audit and log"). Real-tmux
tests are in test_labels_integration.py; the fake server and helpers are rename's."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from termctl import cli, lock
from termctl import tmux_backend as tb
from termctl.tmux_backend import command

from .fakes import TMUX, FakeGateway, completed, is_list_panes, pane_row
from .schemas import validate
from .test_cli_rename import GOLDEN, ID, NOMATCH, PANE, SOCK, Server, Vanishing, run, writes

SCHEMA = {"add": "termctl/cli/label-add.json", "remove": "termctl/cli/label-remove.json"}


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def write(labels: str) -> tuple[str, ...]:
    """The guarded write of ``@termctl_labels`` (labels never rename the window)."""
    target = tb.pane(PANE)
    set_labels = command("set").flag("-p").target(target).arg("@termctl_labels").arg(labels)
    guard = tb.guarded(target, tb.owned(ID), set_labels.build())
    return tuple(tb.Invocation.of(guard).argv(TMUX, SOCK, utf8=True))


def labelled(current: str, after: str, **row: str) -> Server:
    """A server with one managed pane carrying ``current`` whose label write to ``after`` is
    scripted."""
    return Server(
        [pane_row(overrides={"@termctl_labels": current, **row})],
        writes={write(after): (completed(), {"@termctl_labels": after})},
    )


def labels_of(count: int) -> list[str]:
    return [f"k{index:02d}=v" for index in range(count)]


# --- a real change ------------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["add", "remove"])
def test_label_golden(state: Path, verb: str) -> None:
    expected = json.loads((GOLDEN / f"label-{verb}.json").read_text(encoding="utf-8"))
    validate(expected, SCHEMA[verb])
    gateway = (
        labelled("kind=review", "kind=review repo=app")
        if verb == "add"
        else labelled("kind=review repo=app", "kind=review")
    )
    result = run(["--json", "label", verb, "abcd", "repo=app"], gateway, state)
    assert (result.code, result.stderr) == (0, "")
    assert result.json() == expected


def test_label_add_reads_writes_and_reads_back_under_the_lock(state: Path) -> None:
    gateway = labelled("kind=review", "kind=review repo=app")
    result = run(["label", "add", ID, "repo=app"], gateway, state)
    assert (result.code, result.stdout, result.stderr) == (0, "kind=review repo=app\n", "")
    tmux_calls = [c for c in gateway.calls if c != [TMUX, "-V"]]
    # Step 4, then under the lock: the read again, the guarded write, the read back.
    assert [is_list_panes(c) for c in tmux_calls] == [True, True, False, True]
    assert tmux_calls[2] == list(write("kind=review repo=app"))
    assert not lock.held()
    assert (state / "termctl" / "locks" / lock.lock_name(SOCK)).exists()


@pytest.mark.parametrize(
    ("current", "pairs", "after"),
    [
        # Canonical form: sorted by code point, unique, single spaces.
        ("kind=review", ["a=1", "z=9"], "a=1 kind=review z=9"),
        ("", ["b=2", "a=1"], "a=1 b=2"),
        # A pair given twice counts once.
        ("kind=review", ["x=1", "x=1"], "kind=review x=1"),
        # Pairs, not keys: a second value for a key is another pair.
        ("kind=review", ["kind=build"], "kind=build kind=review"),
        # Split at the first "=", empty values allowed.
        ("kind=review", ["k=a=b", "e="], "e= k=a=b kind=review"),
        # Some new, some present.
        ("a=1 kind=review", ["a=1", "b=2"], "a=1 b=2 kind=review"),
        # Code point order: upper case before lower case, "-" before "." before digits.
        ("", ["k=b", "k=B", "k=-", "k=.", "k=0"], "k=- k=. k=0 k=B k=b"),
    ],
)
def test_label_add_writes_the_canonical_set(
    state: Path, current: str, pairs: list[str], after: str
) -> None:
    gateway = labelled(current, after)
    result = run(["--json", "label", "add", ID, *pairs], gateway, state)
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, SCHEMA["add"])
    assert out["labels"] == after.split(" ")
    assert (out["changed"], out["display_updated"], out["dry_run"]) == (True, False, False)
    assert writes(gateway) == [list(write(after))]


@pytest.mark.parametrize(
    ("current", "pairs", "after"),
    [
        ("a=1 b=2 kind=review", ["b=2"], "a=1 kind=review"),
        ("a=1 b=2", ["b=2", "a=1"], ""),
        # Absent pairs among present ones are ignored; a pair given twice counts once.
        ("a=1 b=2", ["a=1", "c=3", "a=1"], "b=2"),
        # Only the exact pair: the same key with another value stays.
        ("kind=build kind=review", ["kind=review"], "kind=build"),
    ],
)
def test_label_remove_writes_the_canonical_set(
    state: Path, current: str, pairs: list[str], after: str
) -> None:
    gateway = labelled(current, after)
    result = run(["--json", "label", "remove", ID, *pairs], gateway, state)
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, SCHEMA["remove"])
    assert out["labels"] == (after.split(" ") if after else [])
    assert (out["changed"], out["display_updated"]) == (True, False)
    assert writes(gateway) == [list(write(after))]


def test_removing_the_last_label_writes_an_empty_value_and_prints_nothing(state: Path) -> None:
    gateway = labelled("kind=review", "")
    result = run(["label", "remove", ID, "kind=review"], gateway, state)
    assert (result.code, result.stdout, result.stderr) == (0, "", "")
    assert writes(gateway) == [list(write(""))]


def test_labels_never_rename_the_window(state: Path) -> None:
    # The window is termctl's and alone, so a rename would be allowed; labels still leave it.
    gateway = labelled("kind=review", "kind=review x=1", window_name="stale")
    result = run(["--json", "label", "add", ID, "x=1"], gateway, state)
    assert result.code == 0
    assert result.json()["display_name"] == "stale"
    assert result.json()["display_updated"] is False
    assert not any("rename-window" in arg for call in writes(gateway) for arg in call)


@pytest.mark.parametrize(
    "row",
    [
        {"pane_dead": "1", "pane_dead_status": "0"},
        {"pane_dead": "1", "pane_dead_signal": "9"},
        {"pane_dead": "1"},
    ],
)
@pytest.mark.parametrize("verb", ["add", "remove"])
def test_a_dead_pane_accepts_label_changes(state: Path, row: dict[str, str], verb: str) -> None:
    current, after = ("kind=review", "kind=review x=1")
    if verb == "remove":
        current, after = after, current
    result = run(["--json", "label", verb, ID, "x=1"], labelled(current, after, **row), state)
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, SCHEMA[verb])
    assert (out["state"], out["changed"]) == ("dead", True)


@pytest.mark.parametrize("verb", ["add", "remove"])
def test_a_change_writes_an_ok_log_record(state: Path, verb: str) -> None:
    current, after = ("kind=review", "kind=review x=1")
    if verb == "remove":
        current, after = after, current
    result = run(["label", verb, ID, "x=1"], labelled(current, after), state)
    assert result.code == 0
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert record == {
        "ts": "2026-01-01T00:00:00.000Z",
        "level": "info",
        "event": f"label_{verb}",
        "id": ID,
        "socket_path": SOCK,
        "result": "ok",
        "exit_code": 0,
    }


# --- no-ops -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verb", "current", "pairs"),
    [
        ("add", "kind=review", ["kind=review"]),
        ("add", "a=1 kind=review", ["kind=review", "a=1", "a=1"]),
        ("remove", "kind=review", ["kind=build"]),
        ("remove", "kind=review", ["other=x", "kind=reviewer"]),
        ("remove", "", ["kind=review"]),
        # At the limit, adding a present pair is still a no-op, not a limit error.
        ("add", " ".join(labels_of(32)), ["k00=v"]),
    ],
)
def test_a_change_that_changes_nothing_is_a_no_op(
    state: Path, verb: str, current: str, pairs: list[str]
) -> None:
    gateway = Server([pane_row(overrides={"@termctl_labels": current})])
    result = run(["--json", "label", verb, ID, *pairs], gateway, state)
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, SCHEMA[verb])
    assert (out["changed"], out["display_updated"], out["dry_run"]) == (False, False, False)
    assert writes(gateway) == []
    assert result.log() == []


# --- the 32-label limit -------------------------------------------------------------------------


def test_32_labels_are_accepted(state: Path) -> None:
    after = " ".join(labels_of(32))
    gateway = labelled(" ".join(labels_of(31)), after)
    result = run(["--json", "label", "add", ID, "k31=v"], gateway, state)
    assert result.code == 0, result.stderr
    assert len(result.json()["labels"]) == 32


@pytest.mark.parametrize(
    ("current", "pairs", "total"),
    [
        (" ".join(labels_of(32)), ["new=1"], 33),
        ("kind=review", labels_of(32), 33),
        ("", labels_of(33), 33),
        # Some given pairs are present already; only the new ones count against the limit.
        (" ".join(labels_of(30)), ["k00=v", "x=1", "y=2", "z=3"], 33),
    ],
)
def test_more_than_32_labels_exits_2_after_id_resolution_and_is_logged(
    state: Path, current: str, pairs: list[str], total: int
) -> None:
    gateway = Server([pane_row(overrides={"@termctl_labels": current})])
    result = run(["--json", "label", "add", ID, *pairs], gateway, state)
    assert result.code == 2
    envelope = result.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "usage"
    assert f"{total} labels" in envelope["error"]["message"]
    assert "32" in envelope["error"]["message"]
    assert writes(gateway) == []
    # The pane was resolved (step 4) and re-read under the lock before the limit check.
    assert sum(map(is_list_panes, gateway.calls)) == 2
    assert not lock.held()
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert (record["event"], record["result"], record["level"]) == ("label_add", "error", "error")
    assert (record["exit_code"], record["id"], record["socket_path"]) == (2, ID, SOCK)
    assert record["message"] == envelope["error"]["message"]


def test_the_limit_is_checked_against_the_labels_read_under_the_lock(state: Path) -> None:
    # Step 4 sees 31 labels; by the time the lock is held another writer has added one more.
    class Growing(Server):
        def _respond(self, argv: list[str], env: Any) -> Any:
            if is_list_panes(argv):
                assert self.panes is not None
                reads = sum(map(is_list_panes, self.calls))
                labels = labels_of(31) if reads == 0 else [*labels_of(31), "zz=1"]
                self.panes[0]["@termctl_labels"] = " ".join(labels)
            return super()._respond(argv, env)

    gateway = Growing([pane_row()])
    result = run(["--json", "label", "add", ID, "new=1"], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (2, "usage")
    assert writes(gateway) == []


def test_label_remove_never_hits_the_limit(state: Path) -> None:
    # Any number of pairs: 41 distinct pairs, one of them present, leave 31 labels.
    gateway = labelled(" ".join(labels_of(32)), " ".join(labels_of(32)[1:]))
    pairs = ["k00=v", *(f"x{index:02d}=v" for index in range(40))]
    result = run(["--json", "label", "remove", ID, *pairs], gateway, state)
    assert result.code == 0, result.stderr
    assert result.json()["labels"] == labels_of(32)[1:]


def test_human_mode_limit_error_names_the_pane(state: Path) -> None:
    gateway = Server([pane_row(overrides={"@termctl_labels": " ".join(labels_of(32))})])
    result = run(["label", "add", ID, "new=1"], gateway, state)
    assert (result.code, result.stdout) == (2, "")
    assert ID in result.stderr and "33" in result.stderr
    assert "usage: termctl label add" in result.stderr


# --- arguments ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["label", "add", ID],
        ["label", "remove", ID],
        ["label", "add", ID, "k"],
        ["label", "add", ID, "=v"],
        ["label", "add", ID, "K=v"],
        ["label", "add", ID, "k=a b"],
        ["label", "add", ID, "k=\x1b[2J"],
        ["label", "add", ID, "k=$(id)"],
        ["label", "add", ID, "k=a;b"],
        ["label", "add", ID, "k=#{pane_id}"],
        ["label", "add", ID, "k=é"],
        ["label", "add", ID, "k=" + "v" * 257],
        ["label", "add", ID, "k" * 65 + "=v"],
        ["label", "remove", ID, "k=v", "bad key=v"],
        ["label", "add", ID, "a=b", "--dry-run", "c=d"],
        ["label", "add", "x", "k=v"],
        ["label", "add", "tc-ab", "k=v"],
        ["label", "frob", ID, "k=v"],
        ["label"],
    ],
)
def test_usage_errors_exit_2_and_are_not_logged(state: Path, argv: list[str]) -> None:
    gateway = labelled("kind=review", "kind=review k=v")
    result = run(["--json", *argv], gateway, state)
    assert result.code == 2
    validate(result.json(), "termctl/cli/error.json")
    assert writes(gateway) == []
    assert result.log() == []


@pytest.mark.parametrize("value", ["PR#42", "a/b:c@d+e=f,g-h.i", "#", "v" * 256, "-x"])
def test_label_values_reach_tmux_through_the_argument_builder(state: Path, value: str) -> None:
    after = f"k={value} kind=review"
    gateway = labelled("kind=review", after)
    result = run(["label", "add", ID, "--", f"k={value}"], gateway, state)
    assert result.code == 0, result.stderr
    assert writes(gateway) == [list(write(after))]


# --- panes that vanish or change --------------------------------------------------------------


def test_a_guard_mismatch_exits_10_and_is_logged(state: Path) -> None:
    gateway = Server([pane_row()], writes={write("kind=review x=1"): (NOMATCH, {})})
    result = run(["--json", "label", "add", ID, "x=1"], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (10, "not_found")
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert (record["event"], record["exit_code"], record["id"]) == ("label_add", 10, ID)


def test_another_tmux_failure_exits_12(state: Path) -> None:
    failed = completed(stderr="server exited unexpectedly\n", returncode=1)
    gateway = Server([pane_row()], writes={write(""): (failed, {})})
    result = run(["--json", "label", "remove", ID, "kind=review"], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (12, "server_unavailable")
    assert result.log()[0]["event"] == "label_remove"


def test_a_pane_gone_before_the_lock_exits_10_without_a_write(state: Path) -> None:
    gateway = Vanishing([pane_row()], reads=1)
    result = run(["--json", "label", "add", ID, "x=1"], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (10, "not_found")
    assert writes(gateway) == []


def test_an_unknown_id_is_logged_without_an_id(state: Path) -> None:
    result = run(["--json", "label", "remove", "tc-zzzzzzzz", "k=v"], Server([pane_row()]), state)
    assert result.code == 10
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert (record["event"], record["id"], record["exit_code"]) == ("label_remove", None, 10)


def test_an_ambiguous_id_exits_21_and_is_logged(state: Path) -> None:
    gateway = Server([pane_row(), pane_row(pane_id="%4")])
    result = run(["--json", "label", "add", ID, "x=1"], gateway, state)
    assert result.code == 21
    assert writes(gateway) == []
    assert result.log()[0]["exit_code"] == 21


def test_an_unusable_state_directory_exits_12_locked_before_any_write(state: Path) -> None:
    state.parent.mkdir(exist_ok=True)
    state.write_text("a file, not a directory")
    gateway = labelled("kind=review", "kind=review x=1")
    result = run(["--json", "label", "add", ID, "x=1"], gateway, state)
    assert result.code == 12
    envelope = result.json()
    validate(envelope, "termctl/cli/error.json")
    assert (envelope["error"]["code"], envelope["error"]["details"]["cause"]) == (
        "locked",
        "unusable",
    )
    assert writes(gateway) == []


# --- dry runs and unexpected failures ---------------------------------------------------------


@pytest.mark.parametrize("verb", ["add", "remove"])
def test_dry_run_is_not_implemented_yet_and_not_logged(state: Path, verb: str) -> None:
    gateway = labelled("kind=review", "kind=review")
    result = run(["--json", "label", verb, ID, "x=1", "--dry-run"], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (1, "internal")
    assert writes(gateway) == []
    assert result.log() == []
    assert not (state / "termctl" / "locks").exists()


def test_an_unexpected_failure_is_logged_as_internal(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "change_pane", broken)
    result = run(["--json", "label", "add", ID, "x=1"], FakeGateway(panes=[pane_row()]), state)
    assert result.code == 1
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert (record["event"], record["exit_code"], record["id"]) == ("label_add", 1, ID)
