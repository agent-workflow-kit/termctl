"""`--dry-run` of `rename`, `mark`, `unmark`, `label add` and `label remove` through the whole
check order against a fake tmux (interface.md "Dry runs", "Names", "Audit and log"). A dry run
makes the checks of a real run, read-only tmux queries included, takes no lock, writes nothing,
rings no bell and writes no log record; it prints the pane as it would be with `dry_run: true`.

The fake tmux fails any call a test does not script, so a write, a bell's terminal query or a
second `list-panes` would fail these tests. Real-tmux tests are in
test_change_dry_run_integration.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from termctl import cli

from .fakes import TMUX, FakeGateway, completed, is_list_panes, pane_row
from .schemas import validate
from .test_cli_mark import BELL, BELL_CONFIG, MARKED, config
from .test_cli_rename import GOLDEN, ID, SOCK, Outcome, lengths, measured, run

SCHEMA = {
    "rename": "termctl/cli/rename.json",
    "mark": "termctl/cli/mark.json",
    "unmark": "termctl/cli/unmark.json",
    "label add": "termctl/cli/label-add.json",
    "label remove": "termctl/cli/label-remove.json",
}

# One dry run of each command, with the pane its fake server carries.
CASES = {
    "rename": (["rename", ID, "new"], {}),
    "mark": (["mark", ID, "attention"], {}),
    "unmark": (["unmark", ID], MARKED),
    "label add": (["label", "add", ID, "repo=app"], {"@termctl_labels": "kind=review"}),
    "label remove": (
        ["label", "remove", ID, "repo=app"],
        {"@termctl_labels": "kind=review repo=app"},
    ),
}


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def server(**row: str) -> FakeGateway:
    """A fake tmux that answers `tmux -V` and the pane reader only."""
    return FakeGateway(panes=[pane_row(overrides=row)])


def dry(argv: list[str], gateway: FakeGateway, state: Path, *extra: str) -> Outcome:
    return run([*extra, *argv, "--dry-run"], gateway, state)


def assert_untouched(gateway: FakeGateway, state: Path, *, probes: int = 0) -> None:
    """Only read-only queries ran (`tmux -V`, one `list-panes`, ``probes`` length probes) and
    nothing reached the state directory: no lock file, no log record."""
    calls = [c for c in gateway.calls if c != [TMUX, "-V"]]
    assert [is_list_panes(c) for c in calls] == [True] + [False] * probes
    assert all("display" in c for c in calls[1:])
    assert not state.exists()


# --- output -------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", list(CASES))
def test_dry_run_golden(state: Path, command: str) -> None:
    expected = json.loads(
        (GOLDEN / f"{command.replace(' ', '-')}-dry-run.json").read_text(encoding="utf-8")
    )
    validate(expected, SCHEMA[command])
    assert expected["dry_run"] is True
    argv, row = CASES[command]
    # Resolved by a short id, as a real run may be.
    argv = [("abcd" if arg == ID else arg) for arg in argv]
    gateway = server(**row)
    result = dry(["--json", *argv], gateway, state)
    assert (result.code, result.stderr) == (0, "")
    assert result.json() == expected
    assert_untouched(gateway, state)


def test_rename_dry_run_reports_the_computed_displayed_name(state: Path) -> None:
    gateway = server()
    result = dry(["--json", "rename", ID, "new"], gateway, state)
    out = result.json()
    validate(out, SCHEMA["rename"])
    assert (out["name"], out["display_name"]) == ("new", "new")
    assert (out["changed"], out["display_updated"], out["dry_run"]) == (True, True, True)


def test_rename_dry_run_keeps_a_set_marker(state: Path) -> None:
    result = dry(["--json", "rename", ID, "new"], server(**MARKED), state)
    out = result.json()
    assert (out["name"], out["display_name"], out["marker"]) == ("new", BELL + "new", "attention")


def test_rename_dry_run_human_output_is_the_displayed_name(state: Path) -> None:
    result = dry(["rename", ID, "new"], server(), state)
    assert (result.code, result.stdout, result.stderr) == (0, "new\n", "")


@pytest.mark.parametrize(
    "row", [{"window_panes": "2"}, {"window_id": "@7"}], ids=["split window", "moved pane"]
)
def test_a_window_it_may_not_rename_reports_the_current_window_name(
    state: Path, row: dict[str, str]
) -> None:
    gateway = server(**row, window_name="user's")
    result = dry(["--json", "rename", ID, "new"], gateway, state)
    out = result.json()
    validate(out, SCHEMA["rename"])
    assert (out["name"], out["display_name"]) == ("new", "user's")
    assert (out["changed"], out["display_updated"]) == (True, False)
    assert_untouched(gateway, state)


def test_mark_dry_run_reports_the_marker_and_rings_no_bell(state: Path, tmp_path: Path) -> None:
    gateway = server()
    result = dry(
        ["--json", "mark", ID, "attention"], gateway, state, *config(tmp_path, BELL_CONFIG)
    )
    assert (result.code, result.stderr) == (0, "")
    out = result.json()
    validate(out, SCHEMA["mark"])
    assert (out["marker"], out["marker_text"], out["display_name"]) == (
        "attention",
        BELL,
        BELL + "KEY-12",
    )
    assert (out["changed"], out["display_updated"], out["dry_run"]) == (True, True, True)
    assert_untouched(gateway, state)  # no `display -p #{pane_tty}`, no write


def test_mark_dry_run_with_marker_display_option_keeps_the_name(
    state: Path, tmp_path: Path
) -> None:
    gateway = server()
    argv = ["--json", "mark", ID, "attention"]
    result = dry(argv, gateway, state, *config(tmp_path, "marker_display: option\n"))
    out = result.json()
    # The window shows the name already: only the options would change.
    assert (out["marker"], out["display_name"]) == ("attention", "KEY-12")
    assert (out["changed"], out["display_updated"]) == (True, True)


def test_unmark_dry_run_reports_the_name(state: Path) -> None:
    gateway = server(**MARKED)
    result = dry(["--json", "unmark", ID], gateway, state)
    out = result.json()
    validate(out, SCHEMA["unmark"])
    assert (out["marker"], out["marker_text"], out["display_name"]) == (None, None, "KEY-12")
    assert (out["changed"], out["display_updated"], out["dry_run"]) == (True, True, True)


def test_unmark_dry_run_human_output(state: Path) -> None:
    result = dry(["unmark", ID], server(**MARKED), state)
    assert (result.code, result.stdout) == (0, "KEY-12\n")


@pytest.mark.parametrize(
    ("verb", "current", "pairs", "after"),
    [
        ("add", "kind=review", ["z=9", "a=1"], ["a=1", "kind=review", "z=9"]),
        ("add", "", ["x=1", "x=1"], ["x=1"]),
        ("remove", "a=1 kind=review", ["a=1", "absent=1"], ["kind=review"]),
        ("remove", "a=1", ["a=1"], []),
    ],
)
def test_label_dry_run_reports_the_canonical_labels(
    state: Path, verb: str, current: str, pairs: list[str], after: list[str]
) -> None:
    gateway = server(**{"@termctl_labels": current, "window_name": "shown"})
    result = dry(["--json", "label", verb, ID, *pairs], gateway, state)
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, SCHEMA[f"label {verb}"])
    assert out["labels"] == after
    assert out["display_name"] == "shown"
    assert (out["changed"], out["display_updated"], out["dry_run"]) == (True, False, True)
    assert_untouched(gateway, state)


def test_label_dry_run_human_output_is_the_labels(state: Path) -> None:
    gateway = server(**{"@termctl_labels": "kind=review"})
    result = dry(["label", "add", ID, "repo=app"], gateway, state)
    assert (result.code, result.stdout) == (0, "kind=review repo=app\n")
    result = dry(["label", "remove", ID, "kind=review"], gateway, state)
    assert (result.code, result.stdout) == (0, "")  # no labels: nothing


# --- no-ops ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "row"),
    [
        (["rename", ID, "KEY-12"], {}),
        (["unmark", ID], {}),
        (["label", "add", ID, "kind=review"], {"@termctl_labels": "kind=review"}),
        (["label", "remove", ID, "absent=1"], {"@termctl_labels": "kind=review"}),
    ],
)
def test_a_dry_run_that_would_change_nothing_reports_changed_false(
    state: Path, argv: list[str], row: dict[str, str]
) -> None:
    gateway = server(**row)
    result = dry(["--json", *argv], gateway, state)
    assert result.code == 0
    out = result.json()
    assert (out["changed"], out["display_updated"], out["dry_run"]) == (False, False, True)
    assert_untouched(gateway, state)


def test_a_mark_dry_run_no_op_uses_the_read_only_length_probe(state: Path) -> None:
    gateway = server(**MARKED)
    gateway.responses[lengths("window_name")] = measured(len((BELL + "KEY-12").encode()))
    result = dry(["--json", "mark", ID, "attention"], gateway, state)
    out = result.json()
    assert (out["changed"], out["display_name"]) == (False, BELL + "KEY-12")
    assert_untouched(gateway, state, probes=1)


def test_a_dry_run_no_op_that_cannot_be_proven_reports_a_change(state: Path) -> None:
    gateway = server(**{"@termctl_name": "a\\b", "window_name": "a\\\\b"})
    gateway.responses[lengths("@termctl_name", "window_name")] = completed("garbage\n")
    result = dry(["--json", "rename", ID, "a\\b"], gateway, state)
    out = result.json()
    assert (out["changed"], out["display_updated"]) == (True, True)
    assert_untouched(gateway, state, probes=1)


# --- exit codes match a real run's checks -------------------------------------------------------


@pytest.mark.parametrize("command", list(CASES))
def test_an_unknown_id_exits_10(state: Path, command: str) -> None:
    argv, row = CASES[command]
    argv = [("tc-zzzzzzzz" if arg == ID else arg) for arg in argv]
    result = dry(["--json", *argv], server(**row), state)
    assert (result.code, result.json()["error"]["code"]) == (10, "not_found")
    assert not state.exists()


@pytest.mark.parametrize("command", list(CASES))
def test_an_ambiguous_id_exits_21(state: Path, command: str) -> None:
    argv, row = CASES[command]
    gateway = FakeGateway(panes=[pane_row(overrides=row), pane_row(pane_id="%4", overrides=row)])
    result = dry(["--json", *argv], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (21, "ambiguous")
    assert not state.exists()


@pytest.mark.parametrize("command", list(CASES))
def test_no_server_exits_10(state: Path, command: str) -> None:
    argv, _ = CASES[command]
    gateway = FakeGateway(
        list_panes=completed(stderr=f"no server running on {SOCK}\n", returncode=1)
    )
    result = dry(["--json", *argv], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (10, "no_server")
    assert not state.exists()


@pytest.mark.parametrize("command", list(CASES))
def test_tmux_too_old_exits_12(state: Path, command: str) -> None:
    argv, row = CASES[command]
    gateway = server(**row)
    gateway.responses[(TMUX, "-V")] = completed("tmux 2.9\n")
    result = dry(["--json", *argv], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (12, "tmux_too_old")
    assert not state.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["rename", ID, "a\tb"],
        ["rename", ID, "#[fg=red]"],
        ["mark", ID, "undefined"],
        ["mark", ID, "Bad"],
        ["label", "add", ID, "nokey"],
        ["label", "add", ID, "K=v"],
        ["label", "remove", ID, "k=v v"],
    ],
)
def test_usage_errors_exit_2(state: Path, argv: list[str]) -> None:
    result = dry(["--json", *argv], server(), state)
    assert (result.code, result.json()["error"]["code"]) == (2, "usage")
    assert not state.exists()


def test_the_label_limit_is_checked_after_id_resolution(state: Path) -> None:
    current = " ".join(f"k{index:02d}=v" for index in range(31))
    gateway = server(**{"@termctl_labels": current})
    result = dry(["--json", "label", "add", ID, "x=1", "y=1"], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (2, "usage")
    assert "would carry 33 labels" in result.json()["error"]["message"]
    assert_untouched(gateway, state)
    result = dry(["--json", "label", "add", ID, "x=1"], gateway, state)
    assert (result.code, len(result.json()["labels"])) == (0, 32)


def test_an_unusable_state_directory_does_not_stop_a_dry_run(state: Path) -> None:
    # A dry run takes no lock, so a state directory a real run would refuse (exit 12,
    # `locked`) is never opened.
    state.parent.mkdir(exist_ok=True)
    state.write_text("a file, not a directory")
    result = dry(["--json", "rename", ID, "new"], server(), state)
    assert (result.code, result.json()["dry_run"]) == (0, True)
    assert state.read_text() == "a file, not a directory"


def test_a_dry_run_never_calls_the_locked_change(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("a dry run must not run change_pane")

    monkeypatch.setattr(cli, "change_pane", refused)
    for command, (argv, row) in CASES.items():
        result = dry(["--json", *argv], server(**row), state)
        assert result.code == 0, (command, result.stderr)


def test_reader_warnings_go_to_stderr_without_a_record(state: Path) -> None:
    bad = pane_row(pane_id="%9", tc_id="tc-zzzzzzzz", overrides={"@termctl_keep": "sometimes"})
    gateway = FakeGateway(panes=[pane_row(), bad])
    result = dry(["--json", "rename", ID, "new"], gateway, state)
    assert result.code == 0
    assert result.stderr.count("ignoring managed pane %9") == 1
    assert not state.exists()
