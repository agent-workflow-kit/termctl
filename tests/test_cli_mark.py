"""`mark` and `unmark` through the whole check order against a fake tmux (interface.md
"Markers", `mark`, `unmark`, "Names", "Audit and log"). Real-tmux tests are in
test_mark_integration.py."""

from __future__ import annotations

import errno
import json
import subprocess
from pathlib import Path

import pytest

from termctl import cli, lock
from termctl import tmux_backend as tb
from termctl.tmux_backend import command

from .fakes import TMUX, FakeGateway, completed, is_list_panes, pane_row
from .schemas import validate
from .test_cli_rename import ID, NOMATCH, PANE, SOCK, Server, _argv, run, writes

GOLDEN = Path(__file__).parent / "golden"
BELL = "\U0001f514 "
TTY = "/dev/pts/7"
TTY_QUERY = tuple(tb.tty_query(PANE).argv(TMUX, SOCK, utf8=True))
MARKED = {"@termctl_marker": "attention", "@termctl_marker_text": BELL,
          "window_name": BELL + "KEY-12"}  # fmt: skip


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _sets(marker: str, text: str) -> list[tb.Command]:
    target = tb.pane(PANE)
    return [
        command("set").flag("-p").target(target).arg("@termctl_marker").arg(marker).build(),
        command("set").flag("-p").target(target).arg("@termctl_marker_text").arg(text).build(),
    ]


def combined(marker: str, text: str, display_name: str) -> tuple[str, ...]:
    """Both marker options and the window rename, in one guarded invocation."""
    rename = command("rename-window").target(tb.pane(PANE)).name(display_name).build()
    return _argv(
        tb.guarded(tb.pane(PANE), tb.owned_single_pane_window(ID), *_sets(marker, text), rename)
    )


def options_only(marker: str, text: str) -> tuple[str, ...]:
    return _argv(tb.guarded(tb.pane(PANE), tb.owned(ID), *_sets(marker, text)))


def lengths(*names: str) -> tuple[str, ...]:
    probe = command("display").flag("-p").target(tb.pane(PANE))
    return _argv(probe.format_arg(tb.byte_lengths(*names)).build())


def tty_answer(tty: str = TTY, dead: str = "0") -> subprocess.CompletedProcess[str]:
    """The bell's terminal query answered for the managed pane."""
    return completed(f"{PANE} {PANE} {ID} {dead} {tty}\n")


def marking(
    text: str = BELL, marker: str = "attention", name: str = "KEY-12", **row: str
) -> Server:
    """A server with one managed pane, KEY-12 without a marker, that ``mark`` can mark."""
    return Server(
        [pane_row(overrides=row)],
        writes={
            combined(marker, text, text + name): (
                completed(),
                {
                    "@termctl_marker": marker,
                    "@termctl_marker_text": text,
                    "window_name": text + name,
                },  # fmt: skip
            )
        },
    )


def unmarking(**row: str) -> Server:
    """A server with one managed pane carrying the attention marker that ``unmark`` clears."""
    return Server(
        [pane_row(overrides={**MARKED, **row})],
        writes={
            combined("", "", "KEY-12"): (
                completed(),
                {"@termctl_marker": "", "@termctl_marker_text": "", "window_name": "KEY-12"},
            )
        },
    )


def config(tmp_path: Path, text: str) -> list[str]:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return ["--config", str(path)]


BELL_CONFIG = "markers:\n  attention:\n    bell: true\n"


# --- mark -----------------------------------------------------------------------------------------


def test_mark_golden(state: Path) -> None:
    expected = json.loads((GOLDEN / "mark.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/mark.json")
    result = run(["--json", "mark", "abcd", "attention"], marking(), state)
    assert (result.code, result.stderr) == (0, "")
    assert result.json() == expected


def test_mark_writes_both_options_and_the_window_name_in_one_invocation(state: Path) -> None:
    gateway = marking()
    result = run(["mark", ID, "attention"], gateway, state)
    assert (result.code, result.stdout, result.stderr) == (0, BELL + "KEY-12\n", "")
    tmux_calls = [c for c in gateway.calls if c != [TMUX, "-V"]]
    # Step 4, then under the lock: the read again, one guarded write, the read back.
    assert [is_list_panes(c) for c in tmux_calls] == [True, True, False, True]
    assert writes(gateway) == [list(combined("attention", BELL, BELL + "KEY-12"))]
    assert not lock.held()


def test_mark_writes_an_ok_log_record(state: Path) -> None:
    result = run(["mark", ID, "attention"], marking(), state)
    assert result.code == 0
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert record == {
        "ts": "2026-01-01T00:00:00.000Z",
        "level": "info",
        "event": "mark",
        "id": ID,
        "socket_path": SOCK,
        "result": "ok",
        "exit_code": 0,
    }


def test_mark_with_marker_display_option_keeps_the_name(state: Path, tmp_path: Path) -> None:
    gateway = Server(
        [pane_row()],
        writes={
            combined("attention", BELL, "KEY-12"): (
                completed(),
                {"@termctl_marker": "attention", "@termctl_marker_text": BELL},
            )
        },
    )
    argv = [*config(tmp_path, "marker_display: option\n"), "--json", "mark", ID, "attention"]
    result = run(argv, gateway, state)
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, "termctl/cli/mark.json")
    assert (out["display_name"], out["marker"], out["marker_text"]) == ("KEY-12", "attention", BELL)
    assert (out["changed"], out["display_updated"]) == (True, True)


def test_marker_display_from_the_environment(state: Path) -> None:
    gateway = Server(
        [pane_row()],
        writes={
            combined("attention", BELL, "KEY-12"): (
                completed(),
                {"@termctl_marker": "attention", "@termctl_marker_text": BELL},
            )
        },
    )
    result = run(
        ["mark", ID, "attention"], gateway, state, environ={"TERMCTL_MARKER_DISPLAY": "option"}
    )
    assert (result.code, result.stdout) == (0, "KEY-12\n")


def test_another_marker_replaces_the_set_one(state: Path, tmp_path: Path) -> None:
    gateway = Server(
        [pane_row(overrides=MARKED)],
        writes={
            combined("review", "R ", "R KEY-12"): (
                completed(),
                {
                    "@termctl_marker": "review",
                    "@termctl_marker_text": "R ",
                    "window_name": "R KEY-12",
                },  # fmt: skip
            )
        },
    )
    argv = [
        *config(tmp_path, "markers:\n  review:\n    text: 'R '\n"),
        "--json",
        "mark",
        ID,
        "review",
    ]
    result = run(argv, gateway, state)
    assert result.code == 0, result.stderr
    out = result.json()
    assert (out["marker"], out["marker_text"], out["display_name"]) == ("review", "R ", "R KEY-12")


@pytest.mark.parametrize(
    "row",
    [{"window_panes": "2"}, {"window_id": "@7"}],
    ids=["split window", "moved pane"],
)
def test_mark_of_a_window_it_does_not_own_alone_sets_the_options_only(
    state: Path, row: dict[str, str]
) -> None:
    gateway = Server(
        [pane_row(overrides={**row, "window_name": "user's"})],
        writes={
            options_only("attention", BELL): (
                completed(),
                {"@termctl_marker": "attention", "@termctl_marker_text": BELL},
            )
        },
    )
    result = run(["--json", "mark", ID, "attention"], gateway, state)
    assert result.code == 0
    out = result.json()
    validate(out, "termctl/cli/mark.json")
    assert (out["changed"], out["display_updated"], out["display_name"]) == (True, False, "user's")
    assert writes(gateway) == [list(options_only("attention", BELL))]
    # The options are set: marking again is a no-op, whatever the window is called.
    again = run(["--json", "mark", ID, "attention"], gateway, state)
    assert (again.json()["changed"], len(writes(gateway))) == (False, 1)


def test_a_window_split_between_the_read_and_the_write_gets_the_options_only(state: Path) -> None:
    gateway = Server(
        [pane_row()],
        writes={
            combined("attention", BELL, BELL + "KEY-12"): (NOMATCH, {}),
            options_only("attention", BELL): (
                completed(),
                {"@termctl_marker": "attention", "@termctl_marker_text": BELL},
            ),
        },
    )
    result = run(["--json", "mark", ID, "attention"], gateway, state)
    assert result.code == 0
    assert (result.json()["changed"], result.json()["display_updated"]) == (True, False)


@pytest.mark.parametrize(
    "row",
    [
        {"pane_dead": "1", "pane_dead_status": "0"},
        {"pane_dead": "1", "pane_dead_signal": "9"},
        {"pane_dead": "1"},
    ],
)
def test_a_dead_pane_is_marked_and_unmarked(state: Path, row: dict[str, str]) -> None:
    result = run(["--json", "mark", ID, "attention"], marking(**row), state)
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, "termctl/cli/mark.json")
    assert (out["state"], out["marker"], out["changed"]) == ("dead", "attention", True)
    result = run(["--json", "unmark", ID], unmarking(**row), state)
    assert result.code == 0, result.stderr
    assert (result.json()["state"], result.json()["marker"]) == ("dead", None)


@pytest.mark.parametrize("text", ["#{pane_id}", "a;", "-x", "a\\b", "$HOME ", "##x", "é中"])
def test_marker_texts_reach_tmux_through_the_argument_builder(
    state: Path, tmp_path: Path, text: str
) -> None:
    gateway = marking(text=text, marker="m")
    yaml = "markers:\n  m:\n    text: " + json.dumps(text) + "\n"
    result = run([*config(tmp_path, yaml), "mark", ID, "m"], gateway, state)
    assert result.code == 0, result.stderr
    assert writes(gateway) == [list(combined("m", text, text + "KEY-12"))]


# --- mark: no-ops ---------------------------------------------------------------------------------


def test_marking_with_the_set_marker_is_a_no_op(state: Path, tmp_path: Path) -> None:
    # bell: true, but a no-op rings nothing (no tty query or write is scripted).
    gateway = Server([pane_row(overrides=MARKED)])
    argv = [*config(tmp_path, BELL_CONFIG), "--json", "mark", ID, "attention"]
    result = run(argv, gateway, state)
    assert (result.code, result.stderr) == (0, "")
    out = result.json()
    validate(out, "termctl/cli/mark.json")
    assert (out["changed"], out["display_updated"], out["dry_run"]) == (False, False, False)
    assert writes(gateway) == []
    assert gateway.tty_writes == []
    assert result.log() == []


def test_a_no_op_with_marker_display_option(state: Path, tmp_path: Path) -> None:
    marked = {"@termctl_marker": "attention", "@termctl_marker_text": BELL}
    gateway = Server([pane_row(overrides=marked)])
    argv = [*config(tmp_path, "marker_display: option\n"), "--json", "mark", ID, "attention"]
    result = run(argv, gateway, state)
    assert (result.code, result.json()["changed"]) == (0, False)
    assert writes(gateway) == []


def test_a_changed_config_text_rewrites_the_marker(state: Path, tmp_path: Path) -> None:
    old = {"@termctl_marker": "attention", "@termctl_marker_text": "! ", "window_name": "! KEY-12"}
    gateway = Server(
        [pane_row(overrides=old)],
        writes={
            combined("attention", "A ", "A KEY-12"): (
                completed(),
                {"@termctl_marker_text": "A ", "window_name": "A KEY-12"},
            )
        },
    )
    yaml = "markers:\n  attention:\n    text: 'A '\n"
    result = run([*config(tmp_path, yaml), "--json", "mark", ID, "attention"], gateway, state)
    assert result.code == 0, result.stderr
    assert (result.json()["changed"], result.json()["marker_text"]) == (True, "A ")


def test_a_stale_window_name_is_not_a_no_op(state: Path) -> None:
    stale = {**MARKED, "window_name": "renamed by a program"}
    gateway = Server(
        [pane_row(overrides=stale)],
        writes={
            combined("attention", BELL, BELL + "KEY-12"): (
                completed(),
                {"window_name": BELL + "KEY-12"},
            )
        },
    )
    result = run(["--json", "mark", ID, "attention"], gateway, state)
    assert result.code == 0
    assert (result.json()["changed"], result.json()["display_updated"]) == (True, True)
    assert len(result.log()) == 1


def test_a_no_op_with_a_backslash_in_the_name(state: Path) -> None:
    # rename-window stored the window name with the backslash doubled.
    row = {**MARKED, "@termctl_name": "a\\b", "window_name": BELL + "a\\\\b"}
    gateway = Server(
        [pane_row(overrides=row)],
        responses={lengths("window_name"): completed(f"{len((BELL + 'a\\\\b').encode())}\n")},
    )
    result = run(["--json", "mark", ID, "attention"], gateway, state)
    assert (result.code, result.json()["changed"]) == (0, False)
    assert writes(gateway) == []


@pytest.mark.parametrize(("stored", "changed"), [("2", True), ("5", False)])
def test_a_forged_control_byte_in_the_marker_text_is_rewritten(
    state: Path, tmp_path: Path, stored: str, changed: bool
) -> None:
    # The config text is the literal text \033 and a space. A forged ESC and a space read back
    # as the same text, but are 2 bytes stored, not 5.
    printed = "\\033 "
    row = {"@termctl_marker": "esc", "@termctl_marker_text": printed}
    gateway = Server(
        [pane_row(overrides=row)],
        writes={combined("esc", printed, "KEY-12"): (completed(), {})},
        responses={lengths("@termctl_marker_text"): completed(stored + "\n")},
    )
    yaml = "marker_display: option\nmarkers:\n  esc:\n    text: '\\033 '\n"
    result = run([*config(tmp_path, yaml), "--json", "mark", ID, "esc"], gateway, state)
    assert result.code == 0, result.stderr
    assert result.json()["changed"] is changed
    assert len(writes(gateway)) == (1 if changed else 0)


# --- mark: markers and config ---------------------------------------------------------------------


def test_a_marker_not_in_config_exits_2_before_any_tmux_call(state: Path) -> None:
    gateway = FakeGateway()
    result = run(["--json", "mark", ID, "review"], gateway, state)
    assert result.code == 2
    envelope = result.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "usage"
    assert "review" in envelope["error"]["message"]
    assert gateway.calls == []
    assert result.log() == []


def test_a_marker_removed_from_config_stays_until_unmark(state: Path) -> None:
    # "old" was defined when it was set; the config no longer has it.
    old = {"@termctl_marker": "old", "@termctl_marker_text": "O ", "window_name": "O KEY-12"}
    shown = run(["--json", "show", ID], Server([pane_row(overrides=old)]), state).json()
    assert (shown["marker"], shown["marker_text"], shown["display_name"]) == (
        "old",
        "O ",
        "O KEY-12",
    )
    # Setting it again is refused; renaming keeps it displayed.
    assert run(["mark", ID, "old"], Server([pane_row(overrides=old)]), state).code == 2
    gateway = Server(
        [pane_row(overrides=old)],
        writes={
            combined("", "", "KEY-12"): (
                completed(),
                {"@termctl_marker": "", "@termctl_marker_text": "", "window_name": "KEY-12"},
            )
        },
    )
    result = run(["--json", "unmark", ID], gateway, state)
    assert result.code == 0, result.stderr
    assert (result.json()["marker"], result.json()["display_name"]) == (None, "KEY-12")


def test_the_built_in_marker_keeps_its_text_when_config_sets_bell_only(
    state: Path, tmp_path: Path
) -> None:
    gateway = marking()
    gateway.responses[TTY_QUERY] = tty_answer()
    gateway.ttys[TTY] = None
    result = run(
        [*config(tmp_path, BELL_CONFIG), "--json", "mark", ID, "attention"], gateway, state
    )
    assert result.code == 0, result.stderr
    assert result.json()["marker_text"] == BELL


# --- the bell -------------------------------------------------------------------------------------


def test_a_bell_marker_writes_one_bel_after_the_change(state: Path, tmp_path: Path) -> None:
    gateway = marking()
    gateway.responses[TTY_QUERY] = tty_answer()
    gateway.ttys[TTY] = None
    result = run([*config(tmp_path, BELL_CONFIG), "mark", ID, "attention"], gateway, state)
    assert (result.code, result.stderr) == (0, "")
    assert gateway.tty_writes == [(TTY, b"\a")]
    calls = [c for c in gateway.calls if c != [TMUX, "-V"]]
    write = calls.index(list(combined("attention", BELL, BELL + "KEY-12")))
    assert calls.index(list(TTY_QUERY)) > write
    assert [r["event"] for r in result.log()] == ["mark"]


def test_without_bell_no_tty_is_touched(state: Path) -> None:
    gateway = marking()
    result = run(["mark", ID, "attention"], gateway, state)
    assert result.code == 0
    assert list(TTY_QUERY) not in gateway.calls
    assert gateway.tty_writes == []


def test_a_dead_pane_gets_no_bell(state: Path, tmp_path: Path) -> None:
    gateway = marking(pane_dead="1", pane_dead_status="0")
    result = run([*config(tmp_path, BELL_CONFIG), "mark", ID, "attention"], gateway, state)
    assert (result.code, result.stderr) == (0, "")
    assert list(TTY_QUERY) not in gateway.calls
    assert gateway.tty_writes == []


def test_unmark_never_rings(state: Path, tmp_path: Path) -> None:
    gateway = unmarking()
    result = run([*config(tmp_path, BELL_CONFIG), "unmark", ID], gateway, state)
    assert (result.code, result.stderr) == (0, "")
    assert list(TTY_QUERY) not in gateway.calls


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.ENXIO, "No such device or address"),
        OSError(errno.ENOTTY, "not a character device"),
        PermissionError(errno.EACCES, "Permission denied"),
    ],
)
def test_a_failed_bell_warns_and_keeps_the_exit_code(
    state: Path, tmp_path: Path, error: OSError
) -> None:
    gateway = marking()
    gateway.responses[TTY_QUERY] = tty_answer()
    gateway.ttys[TTY] = error
    result = run(
        [*config(tmp_path, BELL_CONFIG), "--json", "mark", ID, "attention"], gateway, state
    )
    assert result.code == 0
    assert result.json()["changed"] is True
    assert "bell" in result.stderr and TTY in result.stderr
    warning, done = result.log()
    for record in (warning, done):
        validate(record, "termctl/log.json")
    assert (warning["event"], warning["result"], warning["id"]) == ("warning", "warning", ID)
    assert (done["event"], done["result"]) == ("mark", "ok")


@pytest.mark.parametrize(
    "answer",
    [tty_answer(dead="1"), tty_answer(tty="/dev/sda"), completed(stderr="no pane\n", returncode=1)],
    ids=["died", "not a pty", "gone"],
)
def test_an_unreadable_terminal_warns_and_writes_nothing(
    state: Path, tmp_path: Path, answer: subprocess.CompletedProcess[str]
) -> None:
    gateway = marking()
    gateway.responses[TTY_QUERY] = answer
    result = run([*config(tmp_path, BELL_CONFIG), "mark", ID, "attention"], gateway, state)
    assert result.code == 0
    assert "bell" in result.stderr
    assert gateway.tty_writes == []


def test_quiet_silences_the_bell_warning(state: Path, tmp_path: Path) -> None:
    gateway = marking()
    gateway.responses[TTY_QUERY] = tty_answer()
    gateway.ttys[TTY] = OSError(errno.EIO, "I/O error")
    argv = [*config(tmp_path, BELL_CONFIG), "--quiet", "mark", ID, "attention"]
    result = run(argv, gateway, state)
    assert (result.code, result.stderr) == (0, "")


# --- unmark ---------------------------------------------------------------------------------------


def test_unmark_golden(state: Path) -> None:
    expected = json.loads((GOLDEN / "unmark.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/unmark.json")
    result = run(["--json", "unmark", "abcd"], unmarking(), state)
    assert (result.code, result.stderr) == (0, "")
    assert result.json() == expected


def test_unmark_clears_both_options_in_one_invocation_and_logs(state: Path) -> None:
    gateway = unmarking()
    result = run(["unmark", ID], gateway, state)
    assert (result.code, result.stdout) == (0, "KEY-12\n")
    assert writes(gateway) == [list(combined("", "", "KEY-12"))]
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert (record["event"], record["result"], record["id"]) == ("unmark", "ok", ID)


def test_unmark_without_a_marker_is_a_no_op(state: Path) -> None:
    gateway = Server([pane_row()])
    result = run(["--json", "unmark", ID], gateway, state)
    assert result.code == 0
    out = result.json()
    validate(out, "termctl/cli/unmark.json")
    assert (out["changed"], out["display_updated"], out["marker"]) == (False, False, None)
    assert writes(gateway) == []
    assert result.log() == []


def test_unmark_without_a_marker_fixes_a_stale_window_name(state: Path) -> None:
    gateway = Server(
        [pane_row(overrides={"window_name": "stale"})],
        writes={combined("", "", "KEY-12"): (completed(), {"window_name": "KEY-12"})},
    )
    result = run(["--json", "unmark", ID], gateway, state)
    assert (result.code, result.json()["changed"], result.json()["display_updated"]) == (
        0,
        True,
        True,
    )


def test_unmark_of_a_split_window_without_a_marker_is_a_no_op(state: Path) -> None:
    gateway = Server([pane_row(overrides={"window_panes": "2", "window_name": "user's"})])
    result = run(["--json", "unmark", ID], gateway, state)
    assert (result.code, result.json()["changed"]) == (0, False)


def test_unmark_with_marker_display_option_leaves_the_name(state: Path, tmp_path: Path) -> None:
    marked = {"@termctl_marker": "attention", "@termctl_marker_text": BELL}
    gateway = Server(
        [pane_row(overrides=marked)],
        writes={
            combined("", "", "KEY-12"): (
                completed(),
                {"@termctl_marker": "", "@termctl_marker_text": ""},
            )
        },
    )
    argv = [*config(tmp_path, "marker_display: option\n"), "--json", "unmark", ID]
    result = run(argv, gateway, state)
    assert (result.code, result.json()["display_name"], result.json()["marker"]) == (
        0,
        "KEY-12",
        None,
    )


# --- failures, dry runs and what is logged --------------------------------------------------------


@pytest.mark.parametrize("argv", [["mark", ID, "attention"], ["unmark", ID]])
def test_a_guard_mismatch_exits_10_and_is_logged(state: Path, argv: list[str]) -> None:
    gateway = Server([pane_row(overrides=MARKED if argv[0] == "unmark" else {})])
    for write in (
        combined("attention", BELL, BELL + "KEY-12"),
        options_only("attention", BELL),
        combined("", "", "KEY-12"),
        options_only("", ""),
    ):
        gateway.responses[write] = NOMATCH
    result = run(["--json", *argv], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (10, "not_found")
    [record] = result.log()
    assert (record["event"], record["exit_code"], record["id"]) == (argv[0], 10, ID)


@pytest.mark.parametrize(
    "argv",
    [
        ["mark", ID, "Bad"],
        ["mark", ID],
        ["mark", "x", "attention"],
        ["unmark"],
        ["unmark", ID, "x"],
    ],
)
def test_usage_errors_are_not_logged(state: Path, argv: list[str]) -> None:
    result = run(argv, marking(), state)
    assert result.code == 2
    assert result.log() == []


def test_an_unknown_id_is_logged(state: Path) -> None:
    result = run(["--json", "mark", "tc-zzzzzzzz", "attention"], marking(), state)
    assert (result.code, result.json()["error"]["code"]) == (10, "not_found")
    [record] = result.log()
    assert (record["event"], record["id"]) == ("mark", None)


@pytest.mark.parametrize(
    "argv", [["mark", ID, "attention", "--dry-run"], ["unmark", ID, "--dry-run"]]
)
def test_dry_run_is_not_implemented_yet_and_not_logged(state: Path, argv: list[str]) -> None:
    gateway = marking()
    result = run(["--json", *argv], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (1, "internal")
    assert writes(gateway) == []
    assert result.log() == []


def test_an_unusable_state_directory_exits_12_locked_before_any_write(state: Path) -> None:
    state.parent.mkdir(exist_ok=True)
    state.write_text("a file, not a directory")
    gateway = marking()
    result = run(["--json", "mark", ID, "attention"], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (12, "locked")
    assert writes(gateway) == []


def test_mark_and_unmark_are_logged_change_events() -> None:
    assert (cli.CHANGE_EVENTS["mark"], cli.CHANGE_EVENTS["unmark"]) == ("mark", "unmark")
