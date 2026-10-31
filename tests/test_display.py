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
