"""``open``'s own steps: the argv checks of the usage step, the command and size checks of the
resolve step, the server and session, and the pane (interface.md "Open order" steps 1, 2, 5
and 6, "Command", "Command exit", "Environment", "Server selection").

``cli.py`` runs them in the check order. Every tmux command is built by ``tmux_backend`` and
run through a ``Backend``; every filesystem access goes through the gateway.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from . import procs, tmux_socket, validate
from . import tmux_backend as tb
from .backend import Backend, Created, NoServerTagFailure, SessionState
from .errors import TermctlError, UsageError
from .gateway import Gateway
from .tmux_backend import Invocation, WindowSpec

# The placeholder a window runs until its real command replaces it (Open order step 6); it
# ends on its own if termctl dies. ``sleep`` is looked up on a fixed PATH, never the caller's
# (review round 1, R1-3).
PLACEHOLDER = (*tb.COMMAND_PREFIX, "PATH=/usr/bin:/bin", "sleep", "30")
# --keep-on-exit as tmux's remain-on-exit (interface.md "Command exit").
REMAIN_ON_EXIT = {"never": "off", "failed": "failed", "always": "on"}
# The pane options' schema version (interface.md "Pane options").
SCHEMA_VERSION = "1"
# The largest pane and window ids tmux can assign (unsigned 32-bit), for the size check.
_LARGEST_PANE_ID = "%4294967295"
_LARGEST_WINDOW_ID = "@4294967295"
# The most UTF-8 bytes in one tmux invocation's argv: tmux rejects commands near 16 KB.
SIZE_LIMIT = 12288
_NAME_SHOWN = 200


# --- step 1: the command line -------------------------------------------------------------------


def check_argv(argv: Sequence[str], env: Mapping[str, str]) -> None:
    """The step-1 checks of argv (exit 2): an argv[0] containing ``=``, which ``/usr/bin/env``
    would take as a variable assignment, and ``--env PATH`` without argv, which only
    ``/usr/bin/env`` in the command can apply (interface.md "Command")."""
    if argv and "=" in argv[0]:
        raise UsageError(
            f"the command {validate.show(argv[0], _NAME_SHOWN)} contains '=', which"
            " /usr/bin/env would take as a variable assignment"
        )
    if not argv and "PATH" in env:
        raise UsageError("--env PATH needs a command to run (after --)")


# --- the environment tmux runs with --------------------------------------------------------------


def filtered_env(environ: Mapping[str, str], allow: Iterable[str]) -> dict[str, str]:
    """The caller's environment filtered by ``server_env_allow`` (interface.md "Environment"):
    every tmux command runs with exactly this, so it decides the server's global environment
    when the command starts the server, the variables a new session imports and the new pane's
    ``PATH``. The default allowlist keeps ``LANG`` and ``LC_*``: tmux needs a UTF-8 locale."""
    allow = list(allow)
    return {name: value for name, value in environ.items() if allowed(name, allow)}


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


@dataclass(frozen=True)
class Plan:
    """What ``open`` creates, from its arguments and config (steps 1 and 2): the name, the
    resolved cwd, argv, the ``--env`` variables, the labels (sorted pairs), the placement's
    name and the keep-on-exit value."""

    name: str
    cwd: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    labels: tuple[str, ...]
    placement: str
    keep_on_exit: str

    @property
    def window(self) -> WindowSpec:
        """The window step 5 creates. With argv: the placeholder, without ``-e`` variables
        (the real command gets them, step 6). Without argv: tmux's default shell as a login
        shell (``command=None``) with the ``--env`` variables as ``-e`` (spike E2, G3)."""
        if self.argv:
            return WindowSpec(name=self.name, cwd=self.cwd, command=PLACEHOLDER)
        return WindowSpec(name=self.name, cwd=self.cwd, command=None, env=self.env)

    @property
    def respawn(self) -> tb.Respawn | None:
        """The real command of step 6 (None without argv): ``--env PATH`` goes through
        ``/usr/bin/env`` (spike G4), every other variable as ``-e``."""
        if not self.argv:
            return None
        env = dict(self.env)
        path = env.pop("PATH", None)
        return tb.Respawn(cwd=self.cwd, argv=self.argv, env=tuple(env.items()), path=path)

    def options(self, termctl_id: str, created: Created, created_at: str) -> list[tuple[str, str]]:
        """The pane options of step 6 (interface.md "Pane options"), the ownership marker
        ``@termctl_pane`` last, so a chain that stops early never leaves a managed pane with
        some options missing. No marker yet; the PID options stay empty until step 7 records
        the process (``record``)."""
        return [
            ("@termctl_v", SCHEMA_VERSION),
            ("@termctl_window", created.window_id),
            ("@termctl_id", termctl_id),
            ("@termctl_name", self.name),
            ("@termctl_marker", ""),
            ("@termctl_marker_text", ""),
            ("@termctl_labels", " ".join(self.labels)),
            ("@termctl_placement", self.placement),
            ("@termctl_keep", self.keep_on_exit),
            ("@termctl_created", created_at),
            ("@termctl_pid", ""),
            ("@termctl_pid_start", ""),
            ("@termctl_pane", created.pane_id),
        ]

    def tag_invocation(self, termctl_id: str, created: Created, created_at: str) -> Invocation:
        return tb.tag_pane(
            created.pane_id,
            created.window_id,
            self.options(termctl_id, created, created_at),
            REMAIN_ON_EXIT[self.keep_on_exit],
            self.respawn,
        )


def timestamp(now: datetime) -> str:
    """``@termctl_created``: RFC 3339 UTC with millisecond precision."""
    utc = now.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def size_invocations(
    session: str, plan: Plan, termctl_id: str, env: Mapping[str, str]
) -> list[Invocation]:
    """The invocations the size check judges: step 5's in the new-session case (it starts the
    server, so it carries the most, including the ``set-environment`` values from ``env``, the
    filtered environment) and step 6's tagging invocation, with the largest ids tmux can
    assign."""
    largest = Created(_LARGEST_PANE_ID, _LARGEST_WINDOW_ID)
    return [
        tb.new_session(session, plan.window, start_server=True, env=env),
        plan.tag_invocation(termctl_id, largest, timestamp(datetime(2000, 1, 1, tzinfo=UTC))),
    ]


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


# --- step 6: the pane ---------------------------------------------------------------------------


def tag(
    backend: Backend,
    created: Created,
    termctl_id: str,
    plan: Plan,
    created_at: str,
    warn: Callable[[str], None],
) -> tb.ProcessQuery | None:
    """Open order step 6: tag the pane step 5 created and, with argv, respawn it with the real
    command, in one guarded invocation that ends with the query of step 7, whose answer this
    returns (None when the query failed).

    If tmux fails or the guard does not match, the pane is killed by its exact id, guarded by
    its being in the window step 5 reported with an empty or its own ``@termctl_pane``, and
    ``open`` exits 12 (``tag_failed``). When the tagging found no server, there is no cleanup:
    a no-server result is never followed by a connect without the socket checks. When the
    cleanup kill does not run (its guard did not match, a forged ``@termctl_pane`` say, or
    it failed with anything but tmux reporting the pane gone), ``warn`` names the window
    that may be left behind (interface.md "Residual risks", forged options). The caller logs
    the ``tag_failed`` record (``result: error``) and runs no ``post-open`` hook.
    """
    try:
        return backend.tag_pane(
            created,
            plan.options(termctl_id, created, created_at),
            REMAIN_ON_EXIT[plan.keep_on_exit],
            plan.respawn,
        )
    except NoServerTagFailure:
        raise
    except TermctlError as err:
        if not backend.cleanup_pane(created):
            warn(
                f"could not remove pane {created.pane_id} (window {created.window_id}) after"
                " its tagging failed; it is left running unmanaged"
            )
        if err.code == "tag_failed":
            raise
        message = f"tagging pane {created.pane_id} failed: {err.message}"
        raise TermctlError("tag_failed", message) from None


# --- step 7: the process ------------------------------------------------------------------------


def record(
    backend: Backend,
    gateway: Gateway,
    socket_path: str,
    created: Created,
    termctl_id: str,
    query: tb.ProcessQuery | None,
    warn: Callable[[str], None],
) -> bool:
    """Open order step 7: record the pane's process in ``@termctl_pid`` and
    ``@termctl_pid_start``, only if the query of step 6's invocation answered, the pane was not
    dead, on Linux the server at ``socket_path`` is the queried server as termctl's PID
    namespace sees it and ``/proc`` shows that namespace (``procs.namespace_problem``), and
    the process is alive (not a zombie) with the tmux server as its
    parent, so the PID cannot belong to an unrelated process (spike E6). Whether it was
    recorded.

    Otherwise both options stay empty (``pid: null``). Nothing here undoes the open: a server
    or process that cannot be checked, or a recording that tmux fails, only warns. A server
    that is not the queried one (another PID namespace) and a recording guard that no longer
    matches (the pane died, closed or changed meanwhile) record nothing, silently: the read for
    the output reports the pane's state.
    """
    if query is None or query.dead:
        return False
    if gateway.platform() != "darwin":
        # #{pid} and #{pane_pid} are in the server's PID namespace, /proc in termctl's: the
        # server's PID as the kernel shows it to termctl must be #{pid} (macOS has no PID
        # namespaces). close --force repeats this check before signaling (procs.verify).
        try:
            peer = gateway.socket_peer_pid(socket_path)
        except OSError as err:
            warn(
                f"could not check the server of pane {created.pane_id}: "
                f"{err.strerror or err}; its PID is not recorded"
            )
            return False
        if peer != query.server_pid:
            return False
        problem = procs.namespace_problem(gateway)
        if problem is not None:
            warn(
                f"could not check the process of pane {created.pane_id}: {problem};"
                " its PID is not recorded"
            )
            return False
    try:
        process = procs.read(gateway, query.pane_pid)
    except procs.ProcessError as err:
        warn(
            f"could not check the process of pane {created.pane_id}: {err}; its PID is not recorded"
        )
        return False
    if process is None or process.zombie or process.ppid != query.server_pid:
        return False
    outcome = backend.record_process(created, termctl_id, query.pane_pid, process.start)
    if outcome is tb.GuardOutcome.FAILED:
        warn(f"could not record the PID of pane {created.pane_id}")
    return outcome is tb.GuardOutcome.MATCHED
