"""``open``'s own steps: the command and size checks of the resolve step, and the server and
session (interface.md "Open order" steps 2 and 5, "Command", "Server selection").

``cli.py`` runs them in the check order. Every tmux command is built by ``tmux_backend`` and
run through a ``Backend``; every filesystem access goes through the gateway.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Iterable, Mapping, Sequence

from . import tmux_socket, validate
from .backend import Backend, Created, SessionState
from .errors import TermctlError, UsageError
from .gateway import Gateway
from .tmux_backend import Invocation, WindowSpec

# The placeholder a window runs until its real command replaces it (Open order step 6); it
# ends on its own if termctl dies.
PLACEHOLDER = ("/usr/bin/env", "--", "sleep", "30")
# The most UTF-8 bytes in one tmux invocation's argv: tmux rejects commands near 16 KB.
SIZE_LIMIT = 12288
_NAME_SHOWN = 200


# --- step 2: the name, the command and the size -------------------------------------------------


def default_name(argv: Sequence[str]) -> str:
    """The name without ``--name``: the basename of argv[0]; ``command`` if that is empty or
    not a valid name; ``shell`` without argv (interface.md "Names")."""
    if not argv:
        return "shell"
    base = posixpath.basename(argv[0])
    if base == "" or validate.name_problem(base) is not None:
        return "command"
    return base


def allowed(name: str, allow: Iterable[str]) -> bool:
    """Whether ``server_env_allow`` keeps the variable ``name``: an entry equal to it, or a
    ``PREFIX*`` entry whose prefix it starts with."""
    for entry in allow:
        if entry.endswith("*"):
            if name.startswith(entry[:-1]):
                return True
        elif name == entry:
            return True
    return False


def command_path(
    env: Mapping[str, str], environ: Mapping[str, str], allow: Iterable[str]
) -> str | None:
    """The ``PATH`` the command will get: the ``--env PATH`` value if given, else the caller's
    ``PATH`` if ``server_env_allow`` keeps it (interface.md "Command"); None when it gets none.
    """
    if "PATH" in env:
        return env["PATH"]
    if "PATH" in environ and allowed("PATH", allow):
        return environ["PATH"]
    return None


def check_command(argv0: str, cwd: str, path: str | None, gateway: Gateway) -> None:
    """The command check (exit 12, ``command_not_found``).

    An argv[0] containing ``/`` must be an executable regular file, a relative one resolved
    against ``cwd``. Any other argv[0] must be found in ``path``. Only absolute ``PATH``
    entries are searched: termctl deliberately does not search an empty or relative entry
    (conservative: what it finds there would depend on the pane's working directory), so a
    command found only through one is reported as not found.
    """
    if "\x00" not in argv0 and argv0 not in ("", ".", ".."):
        if "/" in argv0:
            if gateway.is_executable_file(posixpath.join(cwd, argv0)):
                return
        else:
            for entry in (path or "").split(os.pathsep):
                if os.path.isabs(entry) and gateway.is_executable_file(
                    posixpath.join(entry, argv0)
                ):
                    return
    where = "" if "/" in argv0 else " on the command's PATH"
    raise TermctlError(
        "command_not_found",
        f"command not found{where}: {validate.show(argv0, _NAME_SHOWN)}",
    )


def window_spec(name: str, cwd: str) -> WindowSpec:
    """The window step 5 creates: with argv, the placeholder and no ``-e`` variables.

    TODO(#15): without argv the window gets tmux's default shell (``command=None``) and the
    ``--env`` variables as ``-e`` (take argv and the variables here); until the pane step
    lands, every window runs the placeholder, so a window that is never tagged ends on its own.
    """
    return WindowSpec(name=name, cwd=cwd, command=PLACEHOLDER)


def _size(invocation: Invocation, tmux_bin: str, socket_path: str) -> int:
    return sum(
        len(element.encode("utf-8", "surrogateescape"))
        for element in invocation.argv(tmux_bin, socket_path)
    )


def check_size(invocations: Iterable[Invocation], tmux_bin: str, socket_path: str) -> None:
    """The size check (exit 2): the largest invocation's argv, as tmux gets it, must be at
    most ``SIZE_LIMIT`` UTF-8 bytes (an undecodable byte counts as one)."""
    largest = max(_size(invocation, tmux_bin, socket_path) for invocation in invocations)
    if largest > SIZE_LIMIT:
        raise UsageError(
            f"the tmux command for this open would be {largest} bytes, over the {SIZE_LIMIT}-byte"
            " limit; shorten the working directory, the --env values or the command"
        )


# --- step 5: server and session -----------------------------------------------------------------


def _probe(
    backend: Backend, server: tmux_socket.Server, gateway: Gateway, session: str
) -> SessionState:
    """The socket checks, then, only if they found a socket, ``has-session``: the probe, and
    the confirmation after ``new-session``."""
    if not tmux_socket.check(server, gateway):
        return SessionState.NO_SERVER
    return backend.session_state(session)


def _gone(server: tmux_socket.Server, session: str) -> TermctlError:
    return TermctlError(
        "server_unavailable",
        f"session {session} on the tmux server {validate.show(server.socket_path, 200)} went"
        " away while opening a window in it, again after a retry",
        details={"socket_path": server.socket_path},
    )


def create_window(
    backend: Backend,
    server: tmux_socket.Server,
    gateway: Gateway,
    session: str,
    spec: WindowSpec,
) -> Created:
    """Open order step 5: create the window ``spec`` in ``session``, starting the server and
    creating the session when needed.

    The hooks may have run long, so the socket checks and the server probe run again first,
    and the socket checks run again immediately before every later connect: a check that
    finds no socket is no server, and an unsafe socket exits 12. With no server: the default
    directory is made ready (``prepare_start``, which repeats the checks); if a socket file is
    then there, the server is probed again and the prefix is used only if none answers.
    Otherwise the server is started by the invocation that creates the session, prefixed with
    ``exit-unattached off`` (spike F2). Every ``new-session`` is confirmed with
    ``has-session``. A session another caller created first (duplicate session) takes the
    new-window path. A session or server that is not there at ``new-window`` or at the
    confirmation (or a server that appeared after the probe) is retried once, after probing
    again, by whichever path the probe selects. Every other failure, and a second one, exits
    12 (``server_unavailable``): ``open`` never exits 10.
    """
    state = _probe(backend, server, gateway, session)
    retried = False
    while True:
        if state is SessionState.NO_SERVER and tmux_socket.prepare_start(server, gateway):
            # A socket file is there: a stale one, or another caller's server started since
            # the probe, which must never get the prefix.
            state = backend.session_state(session)
            if state is not SessionState.NO_SERVER:
                if retried:
                    raise _gone(server, session)
                retried = True
        if state is SessionState.EXISTS:
            if tmux_socket.check(server, gateway):
                created = backend.create_window(session, spec)
                if created is not None:
                    return created
        else:
            start = state is SessionState.NO_SERVER
            if start or tmux_socket.check(server, gateway):
                created = backend.create_session(session, spec, start_server=start)
                if created is None:
                    # Another caller created the session first.
                    state = SessionState.EXISTS
                    continue
                if _probe(backend, server, gateway, session) is SessionState.EXISTS:
                    return created
        if retried:
            raise _gone(server, session)
        retried = True
        state = _probe(backend, server, gateway, session)
