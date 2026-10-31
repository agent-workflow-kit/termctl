"""The read-modify-write of a managed pane's options and window name: ``rename``, and
TODO(#20) ``mark`` and ``unmark`` and TODO(#21) ``label add`` and ``label remove`` reuse
``change_pane`` with their own plan (interface.md "Names", "Markers", "Labels", "Audit and log"
Locking; design.md D20, D28, D29).

``change_pane`` holds the per-server lock (``lock.server_lock``) while it reads the pane again
by its full termctl id (the id resolved in step 4 may be stale after the lock wait), decides,
writes and reads the pane back; hooks never run while it is held (these commands have none).
Every write is a guarded write (``Backend.write_pane``), so a ``%N`` that now names another
pane, or a pane that vanished, is never acted on: exit 10.

**No-ops.** A change is a no-op (``changed: false``, nothing written, no log record) when every
option it writes already has that value and, where termctl may rename the window
(``display.may_rename_window``), the window name shows the recomputed displayed name
(``display.shows``, which allows for tmux's escaping of window names). Values are compared as
tmux prints them, and tmux prints a control byte or invalid UTF-8 as backslash escape text
(``\\033``), which cannot be told from a literal backslash (interface.md "Residual risks",
forged control bytes). So a compared value that holds a backslash counts as equal only when
its stored length in bytes, read with ``#{n:...}``, is that of the text as printed; escape text
is always longer than the byte it stands for. Otherwise the change is written again, which
replaces a forged value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from . import ids
from .display import may_rename_window, shows
from .errors import TermctlError
from .gateway import Clock
from .lock import server_lock
from .panes import ManagedPane, PaneRead
from .tmux_bin import TmuxVersion
from .validate import show

# The pane options a change may write, with the value as read for each.
_CURRENT: dict[str, Callable[[ManagedPane], str]] = {
    "@termctl_name": lambda pane: pane.name,
    "@termctl_marker": lambda pane: pane.marker or "",
    "@termctl_marker_text": lambda pane: pane.marker_text or "",
    "@termctl_labels": lambda pane: " ".join(pane.labels),
}


class PaneWriter(Protocol):
    """What a pane change needs of a backend (``backend.Backend`` has all of it)."""

    @property
    def socket_path(self) -> str: ...

    def read_panes(self) -> PaneRead: ...

    def write_pane(
        self, pane: ManagedPane, options: Mapping[str, str], window_name: str | None
    ) -> bool: ...

    def byte_lengths(self, pane_id: str, names: Sequence[str]) -> tuple[int, ...] | None: ...


@dataclass(frozen=True)
class Change:
    """What a command wants a pane to look like: ``options`` to write (by option name, with
    the value as stored; empty is written as ``''``, never unset) and the recomputed
    ``display_name``, or None when the command leaves the window name alone (labels)."""

    options: Mapping[str, str]
    display_name: str | None


@dataclass(frozen=True)
class Outcome:
    """``pane`` as read after the write (as read under the lock for a no-op)."""

    pane: ManagedPane
    changed: bool
    display_updated: bool


def change_pane(
    backend: PaneWriter,
    termctl_id: str,
    plan: Callable[[ManagedPane], Change],
    *,
    version: TmuxVersion | None,
    environ: Mapping[str, str],
    clock: Clock,
) -> Outcome:
    """Apply ``plan(pane)`` to the managed pane carrying the full id ``termctl_id`` under the
    server lock. Raises ``locked`` (exit 12) when the lock cannot be had, ``not_found`` (10)
    when the pane is gone or no longer the one resolved, ``ambiguous`` (21) when two panes now
    carry the id, and the tmux failures of ``Backend.write_pane``."""
    with server_lock(backend.socket_path, environ=environ, clock=clock):
        pane = _resolve(backend, termctl_id)
        change = plan(pane)
        unknown = set(change.options) - set(_CURRENT)
        if unknown:
            raise ValueError(f"a pane change cannot write {sorted(unknown)!r}")
        if _is_no_op(backend, pane, change, version):
            return Outcome(pane=pane, changed=False, display_updated=False)
        rename = change.display_name is not None and may_rename_window(pane)
        window_name = change.display_name if rename else None
        updated = backend.write_pane(pane, change.options, window_name)
        return Outcome(pane=_resolve(backend, termctl_id), changed=True, display_updated=updated)


def _resolve(backend: PaneWriter, termctl_id: str) -> ManagedPane:
    """The pane carrying exactly ``termctl_id``, read now."""
    read = backend.read_panes()
    if not read.server_running:
        raise TermctlError(
            "not_found", f"managed pane {termctl_id} is gone (no tmux server is running)"
        )
    try:
        return ids.resolve(ids.parse(termctl_id), read.panes)
    except TermctlError as err:
        if err.code != "not_found":
            raise
        raise TermctlError("not_found", f"managed pane {show(termctl_id)} is gone") from None


def _is_no_op(
    backend: PaneWriter, pane: ManagedPane, change: Change, version: TmuxVersion | None
) -> bool:
    # Each compared value as printed, by the field that stores it.
    compared: dict[str, str] = {}
    for option, value in change.options.items():
        current = _CURRENT[option](pane)
        if current != value:
            return False
        compared[option] = current
    if change.display_name is not None and may_rename_window(pane):
        if not shows(pane.display_name, change.display_name, version):
            return False
        compared["window_name"] = pane.display_name
    measured = {name: value for name, value in compared.items() if "\\" in value}
    if not measured:
        return True
    lengths = backend.byte_lengths(pane.pane_id, list(measured))
    printed = tuple(len(value.encode("utf-8", "surrogateescape")) for value in measured.values())
    return lengths == printed
