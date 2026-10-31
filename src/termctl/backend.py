"""The ``Backend`` protocol the commands use (design.md "Internal architecture"), and its tmux
implementation.

tmux is the only backend (interface.md "Config", ``backend``). Commands talk to a ``Backend``
so that they never build tmux commands themselves; each operation lands with the issue that
implements it:

- ``read_panes``: every managed pane of the server (``panes.py``); ``show``, ``exists`` and
  ``list`` and id resolution use it through ``cli.read_panes``.
- TODO(#14, #15, #16, #17): ``open`` (server and session, pane creation, PID recording).
- ``write_pane`` and ``byte_lengths``: the guarded read-modify-write of a managed pane's
  options and window name (``pane_change.py``); ``rename`` uses them, and TODO(#20) ``mark``
  and ``unmark`` and TODO(#21) label changes reuse them.
- TODO(#23): ``close``; TODO(#24): ``focus``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from . import panes
from . import tmux_backend as tb
from .panes import ManagedPane, PaneRead
from .tmux_backend import GuardOutcome, Invocation, Tmux, check_guard, command, tmux_failure

_LENGTH = re.compile(r"0|[1-9][0-9]{0,9}")


class Backend(Protocol):
    """A terminal backend bound to one resolved server."""

    @property
    def socket_path(self) -> str:
        """The resolved, canonical socket path every ``--json`` pane output carries."""
        ...

    def read_panes(self) -> PaneRead:
        """Every managed pane of the server, or ``server_running=False`` when none runs."""
        ...

    def write_pane(
        self, pane: ManagedPane, options: Mapping[str, str], window_name: str | None
    ) -> bool:
        """Set ``options`` on ``pane`` and, with ``window_name``, rename its window while
        termctl may (interface.md "Names"); every write is guarded by the pane's ownership.
        Returns whether the window was renamed. A pane that is gone or no longer the one
        resolved exits 10 (``not_found``)."""
        ...

    def byte_lengths(self, pane_id: str, names: Sequence[str]) -> tuple[int, ...] | None:
        """The stored length in bytes of each named field or option of ``pane_id`` (read-only),
        or None when tmux does not answer with one number per name."""
        ...


class TmuxBackend:
    """The tmux backend. ``tmux`` carries the resolved ``tmux_bin`` (#10), the checked socket
    (#8) and the filtered environment (#15); this class takes them as given."""

    def __init__(self, tmux: Tmux, *, sentinel: Callable[[], str] = panes.new_sentinel) -> None:
        self.tmux = tmux
        self._sentinel = sentinel

    @property
    def socket_path(self) -> str:
        return self.tmux.socket_path

    def read_panes(self) -> PaneRead:
        return panes.read(self.tmux, sentinel=self._sentinel())

    def write_pane(
        self, pane: ManagedPane, options: Mapping[str, str], window_name: str | None
    ) -> bool:
        """One guarded invocation when the window may be renamed: the options and
        ``rename-window`` under ``owned_single_pane_window``, so both happen or neither. If that
        guard does not match (the window was split, or the pane moved, since it was read; or
        the pane is gone), the options alone under ``owned``; if that does not match either,
        the pane is gone or no longer the one resolved: exit 10. Without options, the second
        guard runs only a ``display -p`` of the pane id, to tell those cases apart."""
        if not options and window_name is None:
            raise ValueError("write_pane needs options or a window name")
        target = tb.pane(pane.pane_id)
        sets = [
            command("set").flag("-p").target(target).arg(name).arg(value).build()
            for name, value in options.items()
        ]
        if window_name is not None:
            rename = command("rename-window").target(target).name(window_name).build()
            guard = tb.guarded(target, tb.owned_single_pane_window(pane.id), *sets, rename)
            result = self.tmux.run(Invocation.of(guard), utf8=True)
            outcome = tb.guard_outcome(result)
            if outcome is GuardOutcome.FAILED:
                raise tmux_failure(result, pane.id, self.socket_path)
            if outcome is GuardOutcome.MATCHED:
                return True
        inner = sets or [
            command("display").flag("-p").target(target).format_arg(tb.fields("pane_id")).build()
        ]
        guard = tb.guarded(target, tb.owned(pane.id), *inner)
        check_guard(self.tmux.run(Invocation.of(guard), utf8=True), pane.id, self.socket_path)
        return False

    def byte_lengths(self, pane_id: str, names: Sequence[str]) -> tuple[int, ...] | None:
        probe = command("display").flag("-p").target(tb.pane(pane_id))
        result = self.tmux.run(
            Invocation.of(probe.format_arg(tb.byte_lengths(*names)).build()), utf8=True
        )
        lines = result.stdout.splitlines()
        if result.returncode != 0 or len(lines) != 1:
            return None
        values = lines[0].split(" ")
        if len(values) != len(names) or not all(_LENGTH.fullmatch(v) for v in values):
            return None
        return tuple(int(v) for v in values)
