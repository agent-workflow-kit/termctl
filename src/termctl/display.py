"""Displayed names (interface.md "Names", "Markers"; design.md D29).

The displayed name is the tmux window name of a managed pane: the stored marker text followed
by the name while a marker is set and ``marker_display`` is ``name``, else the name. termctl
renames the window only while the pane is still in the window it was opened in and that window
has exactly one pane (``may_rename_window``); otherwise the pane-changing commands (``rename``,
``mark`` and ``unmark``) update the pane options only and report ``display_updated: false``.

The "nothing changed" rules compare the window name tmux reports with the recomputed one
(``shows``). tmux does not always report a window name as it was given: ``rename-window``
stores it escaped, so a ``\\`` reads back as ``\\\\`` (3.2a and 3.5a), and before 3.3 a ``$``
followed by an ASCII letter, ``_`` or ``{`` reads back as ``\\$`` (3.2a); ``new-window -n`` keeps
the name as given (design.md spikes A and E). Exactly those two forms count as showing it.

A marker with ``bell: true`` writes one BEL to the terminal of a running pane when ``mark``
changed something (``ring_bell``); a failure is only a warning (interface.md "Markers").
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from .tmux_bin import TmuxVersion

if TYPE_CHECKING:
    from .panes import ManagedPane

MARKER_DISPLAYS = ("name", "option")

# The first tmux version whose rename-window leaves "$" unescaped (design.md spike A).
_DOLLAR_KEPT = (3, 3)
# The "$" that tmux before 3.3 stores escaped: one followed by an ASCII letter, "_" or "{".
_DOLLAR_ESCAPED = re.compile(r"\$(?=[A-Za-z_{])")


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
    """``name`` (as ``new-window -n`` keeps it) and the one form ``rename-window`` stores it in:
    tmux vis-escapes the name in a single pass, doubling every backslash and, before 3.3, writing
    a ``$`` followed by an ASCII letter, ``_`` or ``{`` as ``\\$`` (checked on 3.2a and 3.5a,
    #25). Doubling inserts only after a backslash, so it never changes what follows a ``$``."""
    stored = name.replace("\\", "\\\\")
    if version is not None and (version.major, version.minor) < _DOLLAR_KEPT:
        stored = _DOLLAR_ESCAPED.sub(r"\\$", stored)
    return {name, stored}


def shows(reported: str, computed: str, version: TmuxVersion | None) -> bool:
    """Whether the window name tmux reports (``reported``, as the pane reader reads it) shows
    the recomputed displayed name ``computed`` on tmux ``version`` (None: unknown, assumed
    new). Only exact forms count, so a difference is never taken for a match; a form tmux
    produces that is not listed here only makes a no-op a rewrite."""
    return reported in _tmux_forms(computed, version)


# --- the bell -----------------------------------------------------------------------------------

BEL = b"\a"
# The pseudo-terminal device paths the bell is written to: Linux, WSL and the BSDs (/dev/pts/N)
# and macOS (/dev/ttysN). Anything else is never opened.
_PSEUDO_TERMINAL = re.compile(r"/dev/(?:pts/[0-9]{1,10}|ttys[0-9]{1,10})")


class _TtyReader(Protocol):
    def pane_tty(self, pane: ManagedPane) -> str | None: ...


class _TtyWriter(Protocol):
    def write_tty(self, path: str, data: bytes) -> None: ...


def ring_bell(backend: _TtyReader, gateway: _TtyWriter, pane: ManagedPane) -> str | None:
    """Write one BEL to the terminal of ``pane`` if it is running; returns the warning to give
    when that fails, or None. The terminal is read again right before the write, so a pane
    that died, vanished or is no longer the one resolved gets nothing (``Backend.pane_tty``);
    only a pseudo-terminal path is opened, and the gateway writes only to a character device
    (``Gateway.write_tty``)."""
    if pane.dead:
        return None
    tty = backend.pane_tty(pane)
    if tty is None:
        return (
            f"bell not written: cannot read the terminal of managed pane {pane.id} "
            "(it is gone, dead or no longer the pane resolved)"
        )
    if not _PSEUDO_TERMINAL.fullmatch(tty):
        return (
            f"bell not written: terminal {tty} of managed pane {pane.id} is not a pseudo-terminal"
        )
    try:
        gateway.write_tty(tty, BEL)
    except OSError as exc:
        return f"bell not written to {tty}: {exc.strerror or exc}"
    return None
