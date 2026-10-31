"""The ``Backend`` protocol the commands use (design.md "Internal architecture"), and its tmux
implementation.

tmux is the only backend (interface.md "Config", ``backend``). Commands talk to a ``Backend``
so that they never build tmux commands themselves; each operation lands with the issue that
implements it:

- ``read_panes``: every managed pane of the server (``panes.py``); ``show``, ``exists`` and
  ``list`` and id resolution use it through ``cli.read_panes``.
- ``session_state``, ``create_session`` and ``create_window``: ``open``'s server and session
  (``open_cmd.create_window`` runs them in the order of interface.md "Open order" step 5).
- TODO(#15, #16, #17): the rest of ``open`` (tagging and respawn, PID recording).
- TODO(#19): ``rename``; TODO(#20): ``mark`` and ``unmark``; TODO(#21): label changes.
- TODO(#23): ``close``; TODO(#24): ``focus``.
"""

from __future__ import annotations

import enum
import subprocess
from collections.abc import Callable
from typing import NamedTuple, Protocol

from . import panes
from . import tmux_backend as tb
from .errors import TermctlError
from .panes import PaneRead
from .tmux_backend import Tmux, WindowSpec


class SessionState(enum.Enum):
    """What ``has-session`` found: no server, or a server with or without the session."""

    NO_SERVER = "no_server"
    EXISTS = "exists"
    MISSING = "missing"


class Created(NamedTuple):
    """The ids of the pane and window ``open`` created."""

    pane_id: str
    window_id: str


class Backend(Protocol):
    """A terminal backend bound to one resolved server."""

    @property
    def socket_path(self) -> str:
        """The resolved, canonical socket path every ``--json`` pane output carries."""
        ...

    def read_panes(self) -> PaneRead:
        """Every managed pane of the server, or ``server_running=False`` when none runs."""
        ...

    def session_state(self, session: str) -> SessionState:
        """Whether a server runs and has the session (exactly that name); a read-only probe.
        Any other failure exits 12 (``server_unavailable``)."""
        ...

    def create_session(
        self, session: str, spec: WindowSpec, *, start_server: bool
    ) -> Created | None:
        """Create the session with its first window, or None when it already exists (another
        caller created it first). ``start_server``: this call starts the server. Any other
        failure, no server included, exits 12 (``server_unavailable``)."""
        ...

    def create_window(self, session: str, spec: WindowSpec) -> Created | None:
        """Create a window in the existing session, or None when the session is gone, or the
        server with it (the last session ending takes the server with it). Any other failure
        exits 12 (``server_unavailable``)."""
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

    def session_state(self, session: str) -> SessionState:
        result = self.tmux.run(tb.has_session(session))
        if result.returncode == 0:
            return SessionState.EXISTS
        if tb.is_no_server(result, self.socket_path):
            return SessionState.NO_SERVER
        if tb.is_session_missing(result, session):
            return SessionState.MISSING
        raise self._unavailable(result)

    def create_session(
        self, session: str, spec: WindowSpec, *, start_server: bool
    ) -> Created | None:
        result = self.tmux.run(tb.new_session(session, spec, start_server=start_server))
        if tb.is_duplicate_session(result, session):
            return None
        return self._created(result)

    def create_window(self, session: str, spec: WindowSpec) -> Created | None:
        result = self.tmux.run(tb.new_window(session, spec))
        if tb.is_session_missing(result, session) or tb.is_no_server(result, self.socket_path):
            return None
        return self._created(result)

    def _created(self, result: subprocess.CompletedProcess[str]) -> Created:
        """The ids a successful creation printed. A failed chain is never taken as created,
        even when its first command printed ids."""
        if result.returncode != 0:
            raise self._unavailable(result)
        ids = tb.parse_created(result.stdout)
        if ids is None:
            raise TermctlError(
                "server_unavailable",
                "tmux did not report the pane and window it created",
                details={"socket_path": self.socket_path},
            )
        return Created(*ids)

    def _unavailable(self, result: subprocess.CompletedProcess[str]) -> TermctlError:
        return TermctlError(
            "server_unavailable",
            f"tmux failed: {tb.tmux_message(result)}",
            details={"socket_path": self.socket_path},
        )
