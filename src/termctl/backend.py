"""The ``Backend`` protocol the commands use (design.md "Internal architecture"), and its tmux
implementation.

tmux is the only backend (interface.md "Config", ``backend``). Commands talk to a ``Backend``
so that they never build tmux commands themselves; each operation lands with the issue that
implements it:

- ``read_panes``: every managed pane of the server (``panes.py``); ``show``, ``exists`` and
  ``list`` and id resolution use it through ``cli.read_panes``.
- TODO(#14, #15, #16, #17): ``open`` (server and session, pane creation, PID recording).
- TODO(#19): ``rename``; TODO(#20): ``mark`` and ``unmark``; TODO(#21): label changes.
- TODO(#23): ``close``; TODO(#24): ``focus``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from . import panes
from .panes import PaneRead
from .tmux_backend import Tmux


class Backend(Protocol):
    """A terminal backend bound to one resolved server."""

    @property
    def socket_path(self) -> str:
        """The resolved, canonical socket path every ``--json`` pane output carries."""
        ...

    def read_panes(self) -> PaneRead:
        """Every managed pane of the server, or ``server_running=False`` when none runs."""
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
