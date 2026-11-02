"""``focus``: which client to switch, or whether to attach (interface.md `focus`, "Open order"
step 8; design.md D13).

The four steps, in order:

1. Inside tmux on the same server, with a valid ``TMUX_PANE``: a client attached to the
   caller's own session (the session id in the third field of ``TMUX``, used only when that
   session holds ``$TMUX_PANE``), preferring one viewing the pane's window, else the most
   recently active one. Never a client on another session; with none, step 3.
2. Not inside tmux, stdin and stderr terminals, attaching allowed: attach this terminal.
3. Any client, the most recently active, preferring one attached to a session of the pane.
4. Otherwise ``no_client`` (exit 12).

``--client`` skips steps 1 and 2 and names the client (``client_not_found``, exit 10, when it
is not attached; ``pts/N`` names ``/dev/pts/N`` as in tmux). A client termctl chose that is
gone by the switch is ``no_client``. ``open --focus`` uses the same rules without attaching.
Clients are compared by tmux's ids (``$N``, ``@N``), never by session names, which a user may
have renamed to anything.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from . import tmux_backend as tb
from . import tmux_socket, validate
from .errors import TermctlError
from .gateway import Gateway
from .panes import ManagedPane
from .tmux_backend import ClientInfo

_PANE_ID = re.compile(r"%[0-9]+")
# The third field of TMUX: the number of the caller's session id (``$N``), as tmux writes it.
_SESSION_NUMBER = re.compile(r"0|[1-9][0-9]{0,19}")

Action = Literal["switched", "attach"]


class FocusBackend(Protocol):
    """The backend operations focus uses (``backend.Backend``)."""

    @property
    def socket_path(self) -> str: ...

    def list_clients(self, termctl_id: str) -> tuple[ClientInfo, ...]: ...

    def session_has_pane(self, session_id: str, pane_id: str, termctl_id: str) -> bool: ...

    def switch_client(self, client: str, pane_id: str, termctl_id: str) -> None: ...


@dataclass(frozen=True)
class Caller:
    """Where termctl runs, from the caller's ``TMUX`` and ``TMUX_PANE`` (read before the
    environment is filtered)."""

    inside_tmux: bool
    # Whether TMUX names the resolved server (its canonical socket path).
    same_server: bool
    # TMUX_PANE when it matches %N and TMUX names the resolved server; else None.
    pane_id: str | None
    # ``$N`` from the third field of TMUX when it is a session number and TMUX names the
    # resolved server; else None. Step 1 uses it only if that session holds ``pane_id``.
    session_id: str | None = None


def caller(environ: Mapping[str, str], socket_path: str, gateway: Gateway) -> Caller:
    """Read ``TMUX`` and ``TMUX_PANE``. A ``TMUX`` that is empty or starts with ``,`` counts as
    unset, as in tmux. One whose socket field is not a valid absolute socket path is inside
    tmux on some other server: termctl then neither attaches nor switches a client by the
    caller's session (the conservative reading). The caller's session is the third field of
    ``TMUX`` (``socket,pid,N`` for session ``$N``), never derived from the pane: a pane whose
    window is in several sessions (grouped sessions, ``link-window``) has no single one."""
    value = environ.get(tmux_socket.TMUX, "")
    if value == "" or value.startswith(","):
        return Caller(inside_tmux=False, same_server=False, pane_id=None)
    parts = value.split(",")
    path = parts[0]
    same = (
        path.startswith("/")
        and validate.socket_problem(path, allow_relative=False) is None
        and tmux_socket.canonical_path(path, gateway) == socket_path
    )
    pane = environ.get("TMUX_PANE", "")
    valid_pane = same and _PANE_ID.fullmatch(pane) is not None
    valid_session = same and len(parts) == 3 and _SESSION_NUMBER.fullmatch(parts[2]) is not None
    return Caller(
        inside_tmux=True,
        same_server=same,
        pane_id=pane if valid_pane else None,
        session_id="$" + parts[2] if valid_session else None,
    )


@dataclass(frozen=True)
class Decision:
    """What focus does: switch ``client``, or attach with ``argv``."""

    action: Action
    client: str | None
    argv: list[str] | None


def most_recent(clients: Iterable[ClientInfo]) -> ClientInfo | None:
    """The client with the latest ``client_activity``; on a tie, the first tmux listed."""
    best: ClientInfo | None = None
    for client in clients:
        if best is None or client.activity > best.activity:
            best = client
    return best


def pick_on_session(
    clients: Sequence[ClientInfo], session_id: str, window_id: str
) -> ClientInfo | None:
    """Step 1: among the clients attached to ``session_id`` only, one viewing ``window_id``
    if any, else the most recently active."""
    on_session = [c for c in clients if c.session_id == session_id]
    viewing = [c for c in on_session if c.window_id == window_id]
    return most_recent(viewing) or most_recent(on_session)


def pick_any(clients: Sequence[ClientInfo], session_ids: Sequence[str]) -> ClientInfo | None:
    """Step 3: the most recently active client, preferring one attached to a session the
    pane's window is in."""
    preferred = [c for c in clients if c.session_id in session_ids]
    return most_recent(preferred) or most_recent(clients)


def named_client(clients: Sequence[ClientInfo], value: str) -> ClientInfo | None:
    """The listed client ``--client value`` names: the one whose name is ``value``, or, for a
    value not starting with ``/``, the one named ``/dev/<value>`` (tmux takes ``pts/3`` for
    ``/dev/pts/3``). Nothing else matches (no prefixes)."""
    for c in clients:
        if c.name == value:
            return c
    if not value.startswith("/"):
        for c in clients:
            if c.name == "/dev/" + value:
                return c
    return None


def switch(backend: FocusBackend, pane: ManagedPane, client: str, *, named: bool) -> None:
    """Switch ``client`` to ``pane``. ``named``: the caller gave the client (``--client``), so
    a client that is gone by now is ``client_not_found``; a client termctl chose itself that
    went away is ``no_client`` (exit 12): none is left to switch."""
    try:
        backend.switch_client(client, pane.pane_id, pane.id)
    except TermctlError as err:
        if named or err.code != "client_not_found":
            raise
        raise TermctlError(
            "no_client",
            f"the tmux client {client} chosen to focus pane {pane.id} went away",
            details={"socket_path": backend.socket_path},
        ) from None


def decide(
    backend: FocusBackend,
    pane: ManagedPane,
    where: Caller,
    *,
    client: str | None,
    attach_ok: bool,
    tmux_bin: str,
) -> Decision:
    """Steps 1 to 4 for ``pane``, read-only: the clients are listed, nothing is switched.

    ``client`` is ``--client``; ``attach_ok`` whether step 2 may attach (stdin and stderr are
    terminals and neither ``--no-attach`` nor ``open`` forbids it). Raises
    ``client_not_found`` (exit 10) for a ``--client`` that is not attached and ``no_client``
    (exit 12) when no client is left to switch; tmux failures as ``tmux_failure`` maps them.
    """
    if client is not None:
        named = named_client(backend.list_clients(pane.id), client)
        if named is None:
            raise TermctlError(
                "client_not_found",
                f"client {client} is not attached to the server",
                details={"socket_path": backend.socket_path},
            )
        return Decision("switched", named.name, None)

    clients: tuple[ClientInfo, ...] | None = None
    if where.pane_id is not None and where.session_id is not None:
        if backend.session_has_pane(where.session_id, where.pane_id, pane.id):
            clients = backend.list_clients(pane.id)
            chosen = pick_on_session(clients, where.session_id, pane.window_id)
            if chosen is not None:
                return Decision("switched", chosen.name, None)
    elif not where.inside_tmux and attach_ok:
        argv = tb.attach_argv(
            tmux_bin, backend.socket_path, pane.session_id, pane.window_id, pane.pane_id
        )
        return Decision("attach", None, argv)

    if clients is None:
        clients = backend.list_clients(pane.id)
    chosen = pick_any(clients, pane.session_ids)
    if chosen is None:
        raise TermctlError(
            "no_client",
            f"no tmux client is attached to the server to focus pane {pane.id}",
            details={"socket_path": backend.socket_path},
        )
    return Decision("switched", chosen.name, None)


def output(
    pane: ManagedPane, decision: Decision, socket_path: str, *, dry_run: bool
) -> dict[str, Any]:
    """The ``--json`` output (``schemas/cli/focus.json``)."""
    data: dict[str, Any] = {
        "id": pane.id,
        "action": decision.action,
        "client": decision.client,
    }
    if decision.argv is not None:
        data["argv"] = list(decision.argv)
    data.update(
        {
            "dry_run": dry_run,
            "socket_path": socket_path,
            "session": pane.session,
            "window_id": pane.window_id,
            "pane_id": pane.pane_id,
        }
    )
    return data
