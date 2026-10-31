"""Command-line entry point: the argparse surface, the check order, output and exit codes.

interface.md "Commands", "Check order", "Global flags", "Exit codes" and "Errors" are
normative for everything here.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn, TextIO

from . import __version__, ids, panes, tmux_bin, tmux_socket, validate
from . import config as config_module
from .argv import Scan, scan
from .backend import Backend, TmuxBackend
from .config import Config
from .errors import TermctlError, UsageError
from .gateway import Clock, Gateway, SystemClock, SystemGateway
from .output import PROG, Output, Result, flush_standard_streams
from .panes import ManagedPane, PaneRead
from .tmux_backend import Tmux
from .tmux_bin import TmuxBinary

__all__ = ["Context", "Output", "Result", "main", "parse_args"]


@dataclass
class Context:
    """Everything a command may touch outside its arguments."""

    gateway: Gateway
    clock: Clock
    environ: Mapping[str, str]
    out: Output = field(repr=False)
    config: Config = field(default_factory=config_module.defaults)
    # Set by step 2 (the resolved server) and step 3 (False when there is no server).
    server: tmux_socket.Server | None = None
    socket_present: bool | None = None
    # `tmux_bin` resolved to an absolute path, once: by `open`'s resolve step or the tmux checks.
    tmux_path: str | None = None
    # The resolved tmux binary and version, set by the tmux checks (step 3) after `tmux -V`.
    tmux: TmuxBinary | None = None
    # The one pane read of this invocation (``read_panes``) and, for a command that takes an
    # id, the pane it resolved to (step 4; None for ``exists`` when nothing matched).
    pane_read: PaneRead | None = None
    pane: ManagedPane | None = None


# --- argument parsing ---------------------------------------------------------------------------


class _VersionRequested(Exception):
    pass


class _HelpRequested(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__()
        self.parser = parser


class _VersionAction(argparse.Action):
    """Lists ``--version`` in the help. ``parse_args`` finds ``--version`` in its pre-scan
    before argparse runs, so this action only raises as a fallback."""

    def __init__(self, option_strings: Sequence[str], dest: str, **kwargs: Any) -> None:
        super().__init__(option_strings, dest=argparse.SUPPRESS, nargs=0, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise _VersionRequested


class _HelpAction(argparse.Action):
    """Lists ``-h``/``--help`` in the help. ``parse_args`` finds it in its pre-scan, so this
    action only raises as a fallback; ``main`` prints the help of the parser that met it."""

    def __init__(self, option_strings: Sequence[str], dest: str, **kwargs: Any) -> None:
        super().__init__(option_strings, dest=argparse.SUPPRESS, nargs=0, **kwargs)

    def __call__(self, parser: argparse.ArgumentParser, *args: Any, **kwargs: Any) -> NoReturn:
        raise _HelpRequested(parser)


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that raises ``UsageError`` instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(message, usage=self.format_usage())

    def _check_value(self, action: argparse.Action, value: Any) -> None:
        # argparse quotes the value with repr(), which would print an undecodable byte as
        # `\udcff`; quoting it as text lets the output escape it as `\xff`.
        if action.choices is not None and value not in action.choices:
            choices = ", ".join(map(str, action.choices))
            raise argparse.ArgumentError(
                action, f"invalid choice: '{value}' (choose from {choices})"
            )


def _add_global_flags(parser: argparse.ArgumentParser, *, top: bool) -> None:
    """Add the global flags. Only the top-level parser sets defaults, so a flag given before
    the subcommand is not reset by the subcommand's parser."""

    def default(value: Any) -> Any:
        return value if top else argparse.SUPPRESS

    group = parser.add_argument_group("global flags")
    group.add_argument(
        "--json", action="store_true", default=default(False), help="machine-readable output"
    )
    group.add_argument(
        "--socket", metavar="NAME|PATH", default=default(None), help="the tmux server"
    )
    group.add_argument(
        "--config", metavar="PATH", default=default(None), help="config file to read"
    )
    group.add_argument(
        "--verbose",
        action="store_true",
        default=default(False),
        help="write each tmux command to stderr",
    )
    group.add_argument(
        "--quiet",
        action="store_true",
        default=default(False),
        help="suppress stderr output except errors",
    )
    group.add_argument("--version", action=_VersionAction, help="print the version and exit")
    group.add_argument("-h", "--help", action=_HelpAction, help="print this help and exit")


def _subparser(
    subparsers: argparse._SubParsersAction[_Parser], name: str, command: str, help_text: str
) -> _Parser:
    parser = subparsers.add_parser(
        name, help=help_text, description=help_text, allow_abbrev=False, add_help=False
    )
    parser.set_defaults(command=command)
    _add_global_flags(parser, top=False)
    return parser


def _add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="report what would change")


def _add_gate_flags(parser: argparse.ArgumentParser) -> None:
    """``--actor``, ``--decision-id``, ``--override-escalation`` and ``--hook``."""
    parser.add_argument("--actor", metavar="ACTOR", help="human:<user> or auto:<policy>")
    parser.add_argument(
        "--decision-id",
        dest="decision_ids",
        metavar="ID",
        action="append",
        default=[],
        help="decision id for hooks and the log (repeatable)",
    )
    parser.add_argument(
        "--override-escalation",
        action="store_true",
        help="treat a hook escalation as allow (humans only)",
    )
    parser.add_argument(
        "--hook",
        dest="hooks",
        metavar="NAME=CMD",
        action="append",
        default=[],
        help="add a hook for this invocation (repeatable)",
    )


def _add_labels(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument(
        "--label", dest="labels", metavar="k=v", action="append", default=[], help=help_text
    )


def build_parser() -> _Parser:
    parser = _Parser(
        prog=PROG,
        description="Terminal placement (tmux backend).",
        allow_abbrev=False,
        add_help=False,
    )
    _add_global_flags(parser, top=True)
    commands = parser.add_subparsers(
        title="commands", metavar="<command>", parser_class=_Parser, required=True
    )

    p = _subparser(commands, "open", "open", "open a managed pane")
    p.add_argument("--name", metavar="NAME", help="the pane's name")
    p.add_argument("--cwd", metavar="DIR", help="working directory")
    p.add_argument(
        "--env",
        metavar="VAR=value",
        action="append",
        default=[],
        help="variable for the new pane only (repeatable; not secret-safe)",
    )
    _add_labels(p, "label (repeatable)")
    p.add_argument("--placement", metavar="NAME", help="placement to use, skipping the rules")
    p.add_argument("--session", metavar="SESSION", help="session, overriding the placement's")
    p.add_argument(
        "--target", choices=["window", "pane"], help="target, overriding the placement's"
    )
    p.add_argument(
        "--keep-on-exit",
        choices=["never", "failed", "always"],
        help="keep the pane after its command exits",
    )
    p.add_argument("--focus", action="store_true", help="focus the pane after opening it")
    _add_dry_run(p)
    _add_gate_flags(p)
    p.add_argument("argv", nargs="*", metavar="-- ARGV", help="command to run (after --)")

    for name, help_text in (
        ("show", "print a managed pane"),
        ("exists", "exit 0 if a managed pane exists, 10 if not"),
    ):
        p = _subparser(commands, name, name, help_text)
        p.add_argument("id")

    p = _subparser(commands, "list", "list", "list managed panes")
    _add_labels(p, "only panes with this label (repeatable, ANDed)")
    p.add_argument("--session", metavar="SESSION", help="only panes in this session")
    p.add_argument("--dead", action="store_true", help="only dead panes")

    p = _subparser(commands, "rename", "rename", "rename a managed pane")
    p.add_argument("id")
    p.add_argument("name")
    _add_dry_run(p)

    p = _subparser(commands, "mark", "mark", "set a managed pane's marker")
    p.add_argument("id")
    p.add_argument("marker")
    _add_dry_run(p)

    p = _subparser(commands, "unmark", "unmark", "clear a managed pane's marker")
    p.add_argument("id")
    _add_dry_run(p)

    p = _subparser(commands, "focus", "focus", "bring a managed pane into view")
    p.add_argument("id")
    p.add_argument("--client", metavar="CLIENT", help="switch this client")
    p.add_argument("--no-attach", action="store_true", help="never attach this terminal")
    _add_dry_run(p)

    p = _subparser(commands, "close", "close", "close a managed pane")
    p.add_argument("id")
    p.add_argument("--force", action="store_true", help="close a running pane (humans only)")
    p.add_argument("--grace", metavar="DURATION", help="wait before SIGTERM (needs --force)")
    _add_gate_flags(p)
    _add_dry_run(p)

    label = _subparser(commands, "label", "label", "edit a managed pane's labels")
    label_commands = label.add_subparsers(
        title="label commands", metavar="<command>", parser_class=_Parser, required=True
    )
    for verb, help_text in (("add", "add labels"), ("remove", "remove labels")):
        p = _subparser(label_commands, verb, f"label {verb}", help_text)
        p.add_argument("id")
        p.add_argument("pairs", nargs="+", metavar="k=v")
        _add_dry_run(p)

    p = _subparser(commands, "placements", "placements", "print the effective placements")
    _add_labels(p, "also show the rule and placement for these labels (repeatable)")
    return parser


def _check_open_argv(args: argparse.Namespace, tokens: Sequence[str], usage: str) -> None:
    """argv is accepted only after ``--``, and ``--`` must be followed by argv.

    ``tokens`` are the scanned tokens, in which every option value is joined to its option,
    so their first ``--`` is the separator.
    """
    if "--" not in tokens:
        if args.argv:
            raise UsageError("the command to run must follow '--'", usage=usage)
        return
    tail = list(tokens[tokens.index("--") + 1 :])
    if not tail:
        raise UsageError("'--' must be followed by the command to run", usage=usage)
    if args.argv != tail:
        raise UsageError("the command to run must follow '--'", usage=usage)


def _is_short_cluster(token: str) -> bool:
    """``-h`` followed by more text, which argparse would read as ``-h`` and further flags
    (``-h`` is termctl's only short option)."""
    return token.startswith("-h") and token != "-h"


def _parse(parser: _Parser, scanned: Scan) -> argparse.Namespace:
    if scanned.version:
        raise _VersionRequested
    if scanned.help is not None:
        raise _HelpRequested(scanned.help)
    usage = scanned.parser.format_usage()
    if scanned.early_separator:
        raise UsageError("'--' cannot come before the command", usage=usage)
    if scanned.unknown and (not scanned.complete or any(map(_is_short_cluster, scanned.unknown))):
        # argparse would report the missing command instead of the unknown flag, and would
        # read `-hx` as `-h -x`.
        raise UsageError(f"unrecognized arguments: {' '.join(scanned.unknown)}", usage=usage)
    args, extra = parser.parse_known_args(scanned.args)
    if extra:
        raise UsageError(f"unrecognized arguments: {' '.join(extra)}", usage=usage)
    if args.config == "":
        raise UsageError("--config must not be empty", usage=usage)
    if args.verbose and args.quiet:
        raise UsageError("--verbose and --quiet cannot be combined", usage=usage)
    if args.command == "open":
        _check_open_argv(args, scanned.args, usage)
    if args.command == "close" and args.grace is not None and not args.force:
        raise UsageError("--grace requires --force", usage=usage)
    return args


def parse_args(raw: Sequence[str]) -> argparse.Namespace:
    """Parse ``raw`` and make the flag-level usage checks (step 1 of the check order).

    Raises ``UsageError`` on any usage error, ``_VersionRequested`` for ``--version`` and
    ``_HelpRequested`` for ``--help``.
    """
    parser = build_parser()
    return _parse(parser, scan(parser, raw))


# --- the check order ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandSpec:
    uses_tmux: bool
    takes_id: bool


COMMANDS: dict[str, CommandSpec] = {
    "open": CommandSpec(uses_tmux=True, takes_id=False),
    "show": CommandSpec(uses_tmux=True, takes_id=True),
    "exists": CommandSpec(uses_tmux=True, takes_id=True),
    "list": CommandSpec(uses_tmux=True, takes_id=False),
    "rename": CommandSpec(uses_tmux=True, takes_id=True),
    "mark": CommandSpec(uses_tmux=True, takes_id=True),
    "unmark": CommandSpec(uses_tmux=True, takes_id=True),
    "focus": CommandSpec(uses_tmux=True, takes_id=True),
    "close": CommandSpec(uses_tmux=True, takes_id=True),
    "label add": CommandSpec(uses_tmux=True, takes_id=True),
    "label remove": CommandSpec(uses_tmux=True, takes_id=True),
    "placements": CommandSpec(uses_tmux=False, takes_id=False),
}


def check_args(args: argparse.Namespace, ctx: Context) -> None:
    """Part of step 1: the grammars and limits of the arguments, and ``open``'s cwd (exit 2).

    Replaces ``open``'s ``args.cwd`` with the resolved working directory. Checks that need
    config (a marker or placement being defined) or the pane list (the label limit of
    ``label add``) belong to later steps. Some step-1 checks are not here yet (see the TODO).
    """
    # TODO: remaining step-1 usage checks, owned by later issues: an argv[0] containing '='
    # and --env PATH=... without argv (#15); the --override-escalation gate (TTY or a human:
    # actor) and --hook name validity (#6, #17); the `close --force` gate (#23).
    if args.socket is not None:
        validate.check_socket_flag(args.socket)
    command = args.command
    if hasattr(args, "id"):
        if command == "close" and args.force:
            ids.require_full(args.id)
        else:
            ids.parse(args.id)
    if getattr(args, "actor", None) is not None:
        validate.check_actor(args.actor)
    if hasattr(args, "decision_ids"):
        validate.check_decision_ids(args.decision_ids)
    if hasattr(args, "labels"):
        labels = validate.parse_labels(args.labels)
        if command == "open":
            validate.check_label_count(labels)

    if command == "open":
        if args.name is not None:
            validate.check_name(args.name)
        if args.cwd is not None:
            validate.check_cwd_flag(args.cwd)
        validate.parse_env(args.env)
        if args.placement is not None:
            validate.check_placement_name(args.placement)
        if args.session is not None:
            validate.check_session_name(args.session)
        args.cwd = validate.resolve_cwd(args.cwd, ctx.gateway)
    elif command == "list" and args.session is not None:
        validate.check_session_filter(args.session)
    elif command == "rename":
        validate.check_name(args.name)
    elif command == "mark":
        validate.check_marker_name(args.marker)
    elif command == "focus" and args.client is not None:
        validate.check_client(args.client)
    elif command == "close" and args.grace is not None:
        validate.parse_duration(args.grace)
    elif command in ("label add", "label remove"):
        validate.parse_labels(args.pairs)


def load_config(args: argparse.Namespace, ctx: Context) -> None:
    """Step 1 (rest): the config file, drop-ins and ``TERMCTL_*`` variables (exit 2)."""
    ctx.config = config_module.load(ctx.environ, args.config)


def resolve_server(args: argparse.Namespace, ctx: Context) -> None:
    """Step 2: resolve the server, without side effects."""
    ctx.server = tmux_socket.resolve(args.socket, ctx.config.socket, ctx.environ, ctx.gateway)


def check_tmux(args: argparse.Namespace, ctx: Context) -> None:
    """Step 3: tmux binary, version and socket safety checks (exit 12)."""
    if ctx.tmux is None:
        if ctx.tmux_path is None:
            ctx.tmux_path = tmux_bin.resolve(ctx.config.tmux_bin, ctx.environ, ctx.gateway)
        # TODO(#5): a version warning also gets a `warning` log record in logged commands.
        ctx.tmux = tmux_bin.probe(
            ctx.config.tmux_bin,
            ctx.environ,
            ctx.gateway,
            ctx.out.warn,
            ctx.out.trace,
            path=ctx.tmux_path,
        )
    assert ctx.server is not None
    ctx.socket_present = tmux_socket.check(ctx.server, ctx.gateway)
    # A missing socket or parent directory (False) is no server: `list` and `exists` still
    # answer, and step 4 exits 10 for the other commands. `read_panes` re-runs these checks
    # before it connects.
    # TODO(#14): open re-checks the socket right before connecting, creates the default
    # directory (default form only, mode 0700), and exits 12 for an explicit path whose parent
    # is missing.


def connect(ctx: Context) -> Backend | None:
    """The backend for the resolved server, or None when no server runs there.

    The socket checks run again immediately before connecting, whatever step 3 found, so a
    *no server* result is never followed by a connect without them (interface.md "Residual
    risks", socket path races), and a socket replaced since step 3 is checked as it is now.
    """
    assert ctx.server is not None and ctx.tmux is not None
    if not tmux_socket.check(ctx.server, ctx.gateway):
        return None
    # TODO(#15): run tmux with the environment filtered by server_env_allow; until then only
    # the caller's locale, as for `tmux -V`.
    env = tmux_bin.locale_env(ctx.environ)
    tmux = Tmux(ctx.gateway, ctx.tmux.path, ctx.server.socket_path, env=env, trace=ctx.out.trace)
    return TmuxBackend(tmux)


def read_panes(ctx: Context) -> PaneRead:
    """Every managed pane of the server, read once per invocation; ``server_running`` is
    false when no server runs (no tmux call is made when the socket checks find none).

    Each reader warning is printed (escaped, like all stderr output).
    """
    if ctx.pane_read is None:
        backend = connect(ctx)
        if backend is None:
            ctx.pane_read = PaneRead(server_running=False, panes=(), warnings=())
        else:
            ctx.pane_read = backend.read_panes()
        for warning in ctx.pane_read.warnings:
            # TODO(#5): logged commands also write each warning as a `warning` log record.
            ctx.out.warn(warning)
    return ctx.pane_read


def _socket_path(ctx: Context) -> str:
    assert ctx.server is not None
    return ctx.server.socket_path


def resolve_id(args: argparse.Namespace, ctx: Context) -> None:
    """Step 4: resolve the id to one managed pane (exit 10 or 21).

    No server is ``no_server`` (exit 10), and no match ``not_found``, except for ``exists``,
    which answers both with its normal output (``ctx.pane`` None). Several matches, two panes
    carrying the same full id included, are ``ambiguous`` (exit 21) for every command.
    """
    read = read_panes(ctx)
    if not read.server_running:
        if args.command == "exists":
            return
        path = _socket_path(ctx)
        raise TermctlError(
            "no_server",
            f"no tmux server is running on {validate.show(path, 200)}",
            details={"socket_path": path},
        )
    try:
        ctx.pane = ids.resolve(ids.parse(args.id), read.panes)
    except TermctlError as err:
        if err.code != "not_found" or args.command != "exists":
            raise


# --- the read commands --------------------------------------------------------------------------


def _pane_line(pane: ManagedPane) -> str:
    """The human line of a pane: id, state, session and displayed name (escaped on output)."""
    return f"{pane.id} {pane.state} {pane.session} {pane.display_name}"


def show(args: argparse.Namespace, ctx: Context) -> Result:
    assert ctx.pane is not None
    return Result(ctx.pane.pane_object(_socket_path(ctx)), _pane_line(ctx.pane))


def exists(args: argparse.Namespace, ctx: Context) -> Result:
    """Exit 0 or 10, both with this output and no error object (interface.md `exists`)."""
    data = {
        "input": args.id,
        "id": None if ctx.pane is None else ctx.pane.id,
        "exists": ctx.pane is not None,
        "socket_path": _socket_path(ctx),
    }
    return Result(data, "", exit_code=0 if ctx.pane is not None else 10)


def list_panes(args: argparse.Namespace, ctx: Context) -> Result:
    """Without a server, an empty list and exit 0."""
    labels = validate.parse_labels(args.labels)
    chosen = panes.select(
        read_panes(ctx).panes, labels=labels, session=args.session, dead=args.dead
    )
    socket_path = _socket_path(ctx)
    data = {"socket_path": socket_path, "panes": [p.pane_object(socket_path) for p in chosen]}
    return Result(data, "\n".join(_pane_line(p) for p in chosen))


COMMAND_STEPS = {"show": show, "exists": exists, "list": list_panes}


def run_command(args: argparse.Namespace, ctx: Context) -> Result:
    """Step 5: the command's own steps."""
    step = COMMAND_STEPS.get(args.command)
    if step is not None:
        return step(args, ctx)
    # TODO: each remaining command lands in its own issue (#7, #13-#24).
    raise TermctlError("internal", f"{PROG} {args.command} is not implemented yet")


def dispatch(args: argparse.Namespace, ctx: Context) -> Result:
    """Run the check order after the flag-level usage checks; stop at the first failure."""
    spec = COMMANDS[args.command]
    check_args(args, ctx)
    load_config(args, ctx)
    if spec.uses_tmux:
        resolve_server(args, ctx)
        check_tmux(args, ctx)
        if spec.takes_id:
            resolve_id(args, ctx)
    return run_command(args, ctx)


# --- entry point --------------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    gateway: Gateway | None = None,
    clock: Clock | None = None,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    # sys.stdout and sys.stderr are None when termctl starts with that descriptor closed.
    if stdout is None or stderr is None:
        _backslashreplace_standard_streams()
    out = Output(
        stdout=stdout or sys.stdout or io.StringIO(),
        stderr=stderr or sys.stderr or io.StringIO(),
    )
    try:
        return _run(
            list(sys.argv[1:] if argv is None else argv),
            out,
            gateway=gateway or SystemGateway(),
            clock=clock or SystemClock(),
            environ=os.environ if environ is None else environ,
        )
    finally:
        if stdout is None or stderr is None:
            flush_standard_streams()


def _backslashreplace_standard_streams() -> None:
    """Write characters the locale cannot encode as backslash escapes instead of failing."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")


def _run(
    raw: list[str], out: Output, *, gateway: Gateway, clock: Clock, environ: Mapping[str, str]
) -> int:
    parser = build_parser()
    scanned = scan(parser, raw)
    # Decided before parsing so that usage errors get the envelope too; never after `--`.
    out.json = "--json" in scanned.options
    try:
        args = _parse(parser, scanned)
    except _VersionRequested:
        out.text(f"{PROG} {__version__}\n")
        return 0
    except _HelpRequested as help_request:
        out.text(help_request.parser.format_help())
        return 0
    except TermctlError as err:
        out.error(err)
        return err.exit_code

    out.json, out.verbose, out.quiet = args.json, args.verbose, args.quiet
    ctx = Context(gateway=gateway, clock=clock, environ=environ, out=out)
    try:
        result = dispatch(args, ctx)
    except TermctlError as err:
        out.error(err)
        return err.exit_code
    except Exception as exc:
        err = TermctlError("internal", f"internal error: {type(exc).__name__}: {exc}")
        out.error(err)
        return err.exit_code
    out.result(result)
    return result.exit_code
