"""The ``Backend`` protocol the commands use (design.md "Internal architecture"), and its tmux
implementation.

tmux is the only backend (interface.md "Config", ``backend``). Commands talk to a ``Backend``
so that they never build tmux commands themselves; each operation lands with the issue that
implements it:

- ``read_panes``: every managed pane of the server (``panes.py``); ``show``, ``exists`` and
  ``list`` and id resolution use it through ``cli.read_panes``.
- ``session_state``, ``create_session`` and ``create_window``: ``open``'s server and session
  (``open_cmd.create_window`` runs them in the order of interface.md "Open order" step 5).
- ``tag_pane`` and ``cleanup_pane``: ``open``'s pane (Open order step 6, ``open_cmd.tag``).
- TODO(#16, #17): the rest of ``open`` (PID recording, hooks).
- TODO(#19): ``rename``; TODO(#20): ``mark`` and ``unmark``; TODO(#21): label changes.
- TODO(#23): ``close``; TODO(#24): ``focus``.
"""

from __future__ import annotations

import enum
import subprocess
from collections.abc import Callable, Sequence
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


class NoServerTagFailure(TermctlError):
    """``tag_failed`` because no server answered the tagging invocation: the caller must not
    connect again for the cleanup without the socket checks (interface.md "Residual risks",
    socket path races; review round 1, R1-6)."""

    def __init__(self, message: str) -> None:
        super().__init__("tag_failed", message)


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

    def tag_pane(
        self,
        created: Created,
        options: Sequence[tuple[str, str]],
        remain_on_exit: str,
        respawn: tb.Respawn | None,
    ) -> None:
        """Tag the pane step 5 created, guarded by its being untagged and in the window step 5
        reported, and, with ``respawn``, replace its placeholder with the real command. A guard
        that does not match or a failing tmux call exits 12 (``tag_failed``); a call that found
        no server raises ``NoServerTagFailure``."""
        ...

    def cleanup_pane(self, created: Created) -> bool:
        """Kill the pane after a failed tagging, guarded (``tmux_backend.cleanup_kill``);
        whether the pane is gone: the guarded kill ran, or tmux reported the pane or its
        window gone. False when it may still exist: the guard did not match, or the kill
        failed otherwise (review round 2, R2-2). Never raises for a tmux failure."""
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
        invocation = tb.new_session(session, spec, start_server=start_server, env=self.tmux.env)
        result = self.tmux.run(invocation)
        if tb.is_duplicate_session(result, session):
            return None
        return self._created(result)

    def create_window(self, session: str, spec: WindowSpec) -> Created | None:
        result = self.tmux.run(tb.new_window(session, spec))
        if tb.is_session_missing(result, session) or tb.is_no_server(result, self.socket_path):
            return None
        return self._created(result)

    def tag_pane(
        self,
        created: Created,
        options: Sequence[tuple[str, str]],
        remain_on_exit: str,
        respawn: tb.Respawn | None,
    ) -> None:
        invocation = tb.tag_pane(
            created.pane_id, created.window_id, options, remain_on_exit, respawn
        )
        result = self.tmux.run(invocation)
        outcome = tb.guard_outcome(result)
        if outcome is tb.GuardOutcome.FAILED:
            message = f"tagging pane {created.pane_id} failed: {tb.tmux_message(result)}"
            if tb.is_no_server(result, self.socket_path):
                raise NoServerTagFailure(message)
            raise TermctlError("tag_failed", message)
        if outcome is tb.GuardOutcome.NOMATCH:
            raise TermctlError(
                "tag_failed",
                f"pane {created.pane_id} was gone, moved or already tagged before termctl"
                " could tag it",
            )

    def cleanup_pane(self, created: Created) -> bool:
        try:
            result = self.tmux.run(tb.cleanup_kill(created.pane_id, created.window_id))
        except TermctlError:
            return False
        return tb.guard_outcome(result) is tb.GuardOutcome.MATCHED or tb.is_gone(result)

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
