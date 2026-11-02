"""Displayed names (interface.md "Names", "Markers"; design.md D29).

The displayed name is the tmux window name of a managed pane: the stored marker text followed
by the name while a marker is set and ``marker_display`` is ``name``, else the name. termctl
renames the window only while the pane is still in the window it was opened in and that window
has exactly one pane (``may_rename_window``); otherwise the pane-changing commands (``rename``,
TODO(#20) ``mark`` and ``unmark``) update the pane options only and report
``display_updated: false``.

The "nothing changed" rules compare the window name tmux reports with the recomputed one
(``shows``). tmux does not always report a window name as it was given: ``rename-window``
stores it escaped, so a ``\\`` reads back as ``\\\\`` (3.5a), and before 3.3 a ``$`` reads back
as ``\\$`` (3.2a); ``new-window -n`` keeps the name as given (design.md spikes A and E). Every
form tmux may produce from the recomputed name counts as showing it.
"""

from __future__ import annotations

from typing import Protocol

from .tmux_bin import TmuxVersion

MARKER_DISPLAYS = ("name", "option")

# The first tmux version whose rename-window leaves "$" unescaped (design.md spike A).
_DOLLAR_KEPT = (3, 3)


def displayed_name(name: str, marker_text: str | None, marker_display: str) -> str:
    """The window name for ``name`` with the stored ``marker_text`` (None: no marker)."""
    if marker_display not in MARKER_DISPLAYS:
        raise ValueError(f"unknown marker_display: {marker_display!r}")
    if marker_text is not None and marker_display == "name":
        return marker_text + name
    return name


class _Placed(Protocol):
    """What eligibility needs from a managed pane."""

    @property
    def window_id(self) -> str: ...

    @property
    def opened_window(self) -> str: ...

    @property
    def window_panes(self) -> int: ...


def may_rename_window(pane: _Placed) -> bool:
    """Whether termctl may rename the pane's window: it is the window the pane was opened in
    (``#{window_id}`` equals ``@termctl_window``) and has exactly one pane. The guarded
    ``rename-window`` checks the same in tmux (``owned_single_pane_window``)."""
    return pane.window_id == pane.opened_window and pane.window_panes == 1


def _tmux_forms(name: str, version: TmuxVersion | None) -> set[str]:
    """``name`` and each form tmux may report it in after ``rename-window``."""
    forms = {name, name.replace("\\", "\\\\")}
    if version is not None and (version.major, version.minor) < _DOLLAR_KEPT:
        forms |= {form.replace("$", "\\$") for form in forms}
    return forms


def shows(reported: str, computed: str, version: TmuxVersion | None) -> bool:
    """Whether the window name tmux reports (``reported``, as the pane reader reads it) shows
    the recomputed displayed name ``computed`` on tmux ``version`` (None: unknown, assumed
    new). Only exact forms count, so a difference is never taken for a match; a form tmux
    produces that is not listed here only makes a no-op a rewrite."""
    return reported in _tmux_forms(computed, version)
