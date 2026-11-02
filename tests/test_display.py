"""Displayed names, rename eligibility and the displayed-name comparison (interface.md "Names",
"Markers"; design.md spike E and the #19 notes on tmux's escaping of window names)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from termctl import display, panes
from termctl.tmux_bin import TmuxVersion

from .fakes import list_panes_stdout, pane_row

SENTINEL = "termctl-end-0123456789abcdef"
V35 = TmuxVersion(3, 5, "3.5a")
V33 = TmuxVersion(3, 3, "3.3")
V32 = TmuxVersion(3, 2, "3.2a")


def pane(**overrides: str) -> panes.ManagedPane:
    found, warnings = panes.parse(
        list_panes_stdout([pane_row(overrides=overrides)], SENTINEL), SENTINEL
    )
    assert warnings == ()
    return found[0]


# --- displayed_name ------------------------------------------------------------------------------


def test_displayed_name_without_a_marker_is_the_name() -> None:
    assert display.displayed_name("KEY-12", None, "name") == "KEY-12"
    assert display.displayed_name("KEY-12", None, "option") == "KEY-12"


def test_displayed_name_with_a_marker_shown_in_the_name() -> None:
    assert display.displayed_name("KEY-12", "\U0001f514 ", "name") == "\U0001f514 KEY-12"


def test_displayed_name_with_marker_display_option_is_the_name() -> None:
    assert display.displayed_name("KEY-12", "\U0001f514 ", "option") == "KEY-12"


def test_displayed_name_keeps_text_literally() -> None:
    # Nothing is escaped here: the tmux argument builder doubles '#' when it writes the name.
    assert display.displayed_name("#{pane_id} a\\b $x", "! ", "name") == "! #{pane_id} a\\b $x"


def test_displayed_name_refuses_an_unknown_marker_display() -> None:
    with pytest.raises(ValueError):
        display.displayed_name("n", "! ", "both")


# --- may_rename_window -------------------------------------------------------------------------


def test_a_single_pane_window_it_was_opened_in_may_be_renamed() -> None:
    assert display.may_rename_window(pane())


def test_a_split_window_is_never_renamed() -> None:
    assert not display.may_rename_window(pane(window_panes="2"))


def test_a_pane_moved_to_another_window_never_renames_it() -> None:
    assert not display.may_rename_window(pane(window_id="@7"))


def test_a_moved_pane_alone_in_its_new_window_never_renames_it() -> None:
    moved = replace(pane(), window_id="@9", window_panes=1)
    assert not display.may_rename_window(moved)


# --- shows: the displayed-name comparison ------------------------------------------------------


@pytest.mark.parametrize("version", [V35, V33, V32, None])
def test_an_identical_window_name_shows_the_name(version: TmuxVersion | None) -> None:
    assert display.shows("KEY-12", "KEY-12", version)


@pytest.mark.parametrize("version", [V35, V33, V32, None])
def test_a_different_window_name_does_not(version: TmuxVersion | None) -> None:
    assert not display.shows("KEY-13", "KEY-12", version)
    assert not display.shows("", "KEY-12", version)
    assert not display.shows("KEY-12 ", "KEY-12", version)


@pytest.mark.parametrize("version", [V35, V33, V32, None])
def test_rename_window_doubles_backslashes(version: TmuxVersion | None) -> None:
    # tmux 3.5a: rename-window 'a\b' reads back as 'a\\b'; new-window -n keeps 'a\b'.
    assert display.shows("a\\\\b", "a\\b", version)
    assert display.shows("a\\b", "a\\b", version)
    assert display.shows("\\\\\\\\", "\\\\", version)


def test_only_full_doubling_counts() -> None:
    # Two backslashes, one doubled and one not, is neither form tmux produces.
    assert not display.shows("a\\\\b\\c", "a\\b\\c", V35)
    assert not display.shows("a\\\\\\b", "a\\b", V35)
    # Doubling a name with no backslash changes nothing; a backslash added is a difference.
    assert not display.shows("a\\b", "ab", V35)


def test_tmux_3_2_escapes_dollar() -> None:
    assert display.shows("a\\$b", "a$b", V32)
    assert display.shows("a\\$b\\\\c", "a$b\\c", V32)
    assert display.shows("a$b", "a$b", V32)


@pytest.mark.parametrize("version", [V35, V33, None])
def test_tmux_3_3_and_newer_never_escape_dollar(version: TmuxVersion | None) -> None:
    assert not display.shows("a\\$b", "a$b", version)


def test_other_characters_are_compared_exactly() -> None:
    for name in ('a"b', "a`b", "a'b", "é中\U0001f514", "#", "##", "a b"):
        assert display.shows(name, name, V32)
        assert not display.shows("\\" + name, name, V32)


# --- the bell (#20) -----------------------------------------------------------------------------


class _Terminals:
    """A backend that reports ``tty`` as the pane's terminal and a gateway that writes to it
    (or raises ``error``)."""

    def __init__(self, tty: str | None, error: OSError | None = None) -> None:
        self.tty, self.error = tty, error
        self.queried: list[str] = []
        self.written: list[tuple[str, bytes]] = []

    def pane_tty(self, pane: panes.ManagedPane) -> str | None:
        self.queried.append(pane.pane_id)
        return self.tty

    def write_tty(self, path: str, data: bytes) -> None:
        if self.error is not None:
            raise self.error
        self.written.append((path, data))


def test_ring_bell_writes_one_bel_to_the_pane_terminal() -> None:
    terminals = _Terminals("/dev/pts/7")
    assert display.ring_bell(terminals, terminals, pane()) is None
    assert terminals.written == [("/dev/pts/7", b"\a")]
    assert terminals.queried == ["%3"]


def test_ring_bell_without_a_terminal_warns_and_writes_nothing() -> None:
    terminals = _Terminals(None)
    warning = display.ring_bell(terminals, terminals, pane())
    assert warning is not None and "tc-abcdefgh" in warning and "bell" in warning
    assert terminals.written == []


@pytest.mark.parametrize("tty", ["/dev/sda", "/dev/pts/../sda", "/tmp/x", "/dev/pts/", "/dev/tty1"])
def test_ring_bell_never_writes_to_a_terminal_that_is_not_a_pseudo_terminal(tty: str) -> None:
    terminals = _Terminals(tty)
    warning = display.ring_bell(terminals, terminals, pane())
    assert warning == (
        f"bell not written: terminal {tty} of managed pane tc-abcdefgh is not a pseudo-terminal"
    )
    assert terminals.written == []


@pytest.mark.parametrize("tty", ["/dev/pts/0", "/dev/pts/17", "/dev/ttys003"])
def test_ring_bell_writes_to_a_pseudo_terminal(tty: str) -> None:
    terminals = _Terminals(tty)
    assert display.ring_bell(terminals, terminals, pane()) is None
    assert terminals.written == [(tty, b"\a")]


@pytest.mark.parametrize(
    "error",
    [
        OSError(6, "No such device or address"),
        OSError(25, "not a character device"),
        PermissionError(13, "Permission denied"),
        BlockingIOError(11, "Resource temporarily unavailable"),
        OSError("short write"),
    ],
)
def test_ring_bell_failure_is_a_warning(error: OSError) -> None:
    warning = display.ring_bell(_Terminals("/dev/pts/7"), _Terminals(None, error), pane())
    assert warning is not None
    assert "/dev/pts/7" in warning and "bell" in warning


def test_ring_bell_never_rings_a_dead_pane() -> None:
    terminals = _Terminals("/dev/pts/7")
    warning = display.ring_bell(terminals, terminals, pane(pane_dead="1"))
    assert warning is None
    assert terminals.queried == [] and terminals.written == []
