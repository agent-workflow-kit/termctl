"""Command-line entry point: the argparse surface, the check order, output and exit codes.

interface.md "Commands", "Check order", "Global flags", "Exit codes" and "Errors" are
normative for everything here.
"""

from __future__ import annotations

import argparse
import io
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn, TextIO

from . import (
    __version__,
    display,
    hooks,
    ids,
    jsonlog,
    open_cmd,
    panes,
    placement,
    tmux_bin,
    tmux_socket,
    validate,
)
from . import config as config_module
from .argv import Scan, scan
from .backend import Backend, Created, TmuxBackend
from .config import Config
from .errors import TermctlError, UsageError
from .gateway import (
    Clock,
    Gateway,
    RunSignals,
    SystemClock,
    SystemGateway,
    Terminated,
    stop_signals,
)
from .output import PROG, Output, Result, flush_standard_streams
from .pane_change import Change, Outcome, change_pane
from .panes import ManagedPane, PaneRead
from .statedir import StateDirError
from .tmux_backend import Tmux, WindowSpec
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
    # `open`'s resolved placement (Open order step 2); None for other commands.
    selection: placement.Selection | None = None
    # `open`'s plan, its window and its id (Open order step 2; step 3 replaces the id on a
    # collision).
    plan: open_cmd.Plan | None = None
    window: WindowSpec | None = None
    open_id: str | None = None
    # A command that writes log records (``open`` or a pane change of ``CHANGE_EVENTS``, not a
    # dry run, once past step 1): each of its warnings also becomes a ``warning`` log record
    # (``warn``).
    logged: bool = False
    # A logged pane change: its ``warning`` records, held until its outcome is known, then
    # written before its own record, or dropped if it changed nothing (``_logged_change``).
    held_warnings: list[dict[str, Any]] | None = None
    # The actor, resolved once per run (``resolve_actor``).
    actor: str | None = None
    # ``open``: the --decision-id values, then the ids its pre-open hooks returned (step 4),
    # and every hook it started (interface.md "Hooks").
    decision_ids: list[str] = field(default_factory=list[str])
    hook_processes: list[hooks.HookProcess] = field(default_factory=list[hooks.HookProcess])
    # ``open``: whether its audit record was written (it is written once).
    audited: bool = False
    # A logged ``open``: its stop-signal handling (``gateway.stop_signals``).
    signals: RunSignals | None = None
    # A pane change (``rename``, the label commands) that changed nothing: no log record
    # (interface.md "Audit and log").
    no_op: bool = False

    def socket_path(self) -> str | None:
        """The resolved server's socket path; None before step 2 resolved it."""
        return None if self.server is None else self.server.socket_path

    def record_id(self) -> str | None:
        """The id a log record names: ``open``'s generated id, else the pane step 4 resolved;
        None before either exists."""
        if self.open_id is not None:
            return self.open_id
        return None if self.pane is None else self.pane.id

    def warn(self, message: str) -> None:
        """Print a warning (unless ``--quiet``); a logged command also writes it as a
        ``warning`` log record (interface.md "Audit and log")."""
        self.out.warn(message)
        if self.logged:
            record = jsonlog.log_record(
                self.clock.now(),
                event="warning",
                result="warning",
                id=self.record_id(),
                socket_path=self.socket_path(),
                message=message,
            )
            if self.held_warnings is not None:
                self.held_warnings.append(record)
            else:
                self.log(record)

    def write_held_warnings(self) -> None:
        """Write the held ``warning`` records, in order, and stop holding them."""
        held, self.held_warnings = self.held_warnings or [], None
        for record in held:
            self.log(record)

    def log(self, record: dict[str, Any]) -> None:
        """Append a ``log.jsonl`` record. One that cannot be written only warns: the action
        has happened, and the exit code stays the command's ("Records")."""
        try:
            jsonlog.append_log(self.environ, record)
        except StateDirError as exc:
            self.out.warn(f"could not write the log record: {exc}")

    def audit(self, record: dict[str, Any]) -> None:
        """Append an ``audit.jsonl`` record; like ``log``, a failure only warns (and, in a
        logged command, gets a ``warning`` log record)."""
        try:
            jsonlog.append_audit(self.environ, record)
        except StateDirError as exc:
            self.warn(f"could not write the audit record: {exc}")


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

    Replaces ``open``'s ``args.cwd`` with the resolved working directory and, for ``open``
    and ``close``, sets ``args.flag_hooks`` to the parsed ``--hook`` values. Checks that need
    config (a marker or placement being defined) or the pane list (the label limit of
    ``label add``) belong to later steps. Some step-1 checks are not here yet (see the TODO).
    """
    # TODO(#23): remaining step-1 usage check: the `close --force` gate.
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
    if hasattr(args, "hooks"):
        args.flag_hooks = hooks.parse_flag_hooks(command, args.hooks)
        hooks.check_override_gate(
            args.override_escalation, actor=args.actor, stdin_is_tty=ctx.gateway.stdin_isatty()
        )
    if hasattr(args, "labels"):
        labels = validate.parse_labels(args.labels)
        if command in ("open", "placements"):
            validate.check_label_count(labels)

    if command == "open":
        if args.name is not None:
            validate.check_name(args.name)
        if args.cwd is not None:
            validate.check_cwd_flag(args.cwd)
        open_cmd.check_argv(args.argv, validate.parse_env(args.env))
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
    """Step 1 (rest): the config file, drop-ins and ``TERMCTL_*`` variables, then the
    arguments that need config: ``mark``'s marker must be defined (exit 2)."""
    ctx.config = config_module.load(ctx.environ, args.config)
    if args.command == "mark" and args.marker not in ctx.config.markers:
        raise UsageError(
            f"unknown marker {validate.show(args.marker)}: not defined in config (markers)"
        )


def resolve_server(args: argparse.Namespace, ctx: Context) -> None:
    """Step 2: resolve the server, without side effects."""
    ctx.server = tmux_socket.resolve(args.socket, ctx.config.socket, ctx.environ, ctx.gateway)


def resolve_tmux_bin(ctx: Context) -> None:
    """Resolve ``tmux_bin`` to an absolute path, once (exit 12, ``tmux_missing``)."""
    if ctx.tmux_path is None:
        ctx.tmux_path = tmux_bin.resolve(ctx.config.tmux_bin, ctx.environ, ctx.gateway)


def resolve_placement(args: argparse.Namespace, ctx: Context) -> None:
    """Step 2 of ``open``: resolve the placement, without side effects (exit 2, ``usage`` for
    an undefined ``--placement``, ``not_supported``)."""
    ctx.selection = placement.select(
        ctx.config,
        validate.parse_labels(args.labels),
        placement=args.placement,
        session=args.session,
        target=args.target,
    )


def resolve_command(args: argparse.Namespace, ctx: Context) -> None:
    """Step 2 of ``open``, after the placement: the plan and the id, then the command check
    (exit 12, ``command_not_found``) and the size check (exit 2), without side effects."""
    assert ctx.selection is not None and ctx.server is not None and ctx.tmux_path is not None
    env = validate.parse_env(args.env)
    name = args.name if args.name is not None else open_cmd.default_name(args.argv)
    ctx.plan = open_cmd.Plan(
        name=name,
        cwd=args.cwd,
        argv=tuple(args.argv),
        env=tuple(env.items()),
        labels=tuple(validate.parse_labels(args.labels)),
        placement=ctx.selection.name,
        keep_on_exit=args.keep_on_exit or ctx.config.keep_on_exit,
    )
    ctx.window = ctx.plan.window
    # The id; step 3 checks it against the managed panes (a replacement has the same length).
    ctx.open_id = ids.generate()
    if args.argv:
        path = open_cmd.command_path(env, ctx.environ, ctx.config.server_env_allow)
        open_cmd.check_command(args.argv[0], args.cwd, path, ctx.gateway)
    invocations = open_cmd.size_invocations(
        ctx.selection.session, ctx.plan, ctx.open_id, server_env(ctx)
    )
    open_cmd.check_size(invocations, ctx.tmux_path, ctx.server.socket_path)


def check_tmux(args: argparse.Namespace, ctx: Context) -> None:
    """Step 3: tmux binary, version and socket safety checks (exit 12)."""
    if ctx.tmux is None:
        resolve_tmux_bin(ctx)
        ctx.tmux = tmux_bin.probe(
            ctx.config.tmux_bin,
            ctx.environ,
            ctx.gateway,
            ctx.warn,
            ctx.out.trace,
            path=ctx.tmux_path,
            env=server_env(ctx),
        )
    assert ctx.server is not None
    ctx.socket_present = tmux_socket.check(ctx.server, ctx.gateway)
    # A missing socket or parent directory (False) is no server: `list` and `exists` still
    # answer, step 4 exits 10 for the other commands, and `open` starts a server in its step 5
    # (`open_cmd.create_window`). `read_panes` and step 5 re-run these checks before they
    # connect.
    if args.command == "open" and not ctx.socket_present:
        # `open` could not start a server there: it never creates an explicit path's parent.
        tmux_socket.require_parent(ctx.server, ctx.gateway)


def check_id_collision(ctx: Context) -> None:
    """Step 3 of ``open``, after the tmux checks: the id from step 2 is kept unless a managed
    pane carries it, from one read-only ``list-panes -a`` (none without a server)."""
    assert ctx.open_id is not None
    taken = {pane.id for pane in read_panes(ctx).panes}
    if ctx.open_id in taken:
        ctx.open_id = ids.generate_unique(taken)


def connect(ctx: Context) -> Backend | None:
    """The backend for the resolved server, or None when no server runs there.

    The socket checks run again immediately before connecting, whatever step 3 found, so a
    *no server* result is never followed by a connect without them (interface.md "Residual
    risks", socket path races), and a socket replaced since step 3 is checked as it is now.
    """
    assert ctx.server is not None
    if not tmux_socket.check(ctx.server, ctx.gateway):
        return None
    return unchecked_backend(ctx)


def unchecked_backend(ctx: Context) -> TmuxBackend:
    """The backend for the resolved server, without the socket checks: only for a caller that
    runs them itself immediately before each connect (``open_cmd.create_window``)."""
    assert ctx.server is not None and ctx.tmux is not None
    tmux = Tmux(
        ctx.gateway,
        ctx.tmux.path,
        ctx.server.socket_path,
        env=server_env(ctx),
        trace=ctx.out.trace,
        configured_bin=ctx.config.tmux_bin,
    )
    return TmuxBackend(tmux)


def server_env(ctx: Context) -> dict[str, str]:
    """The environment every tmux command runs with: the caller's, filtered by
    ``server_env_allow`` through ``open_cmd.allowed``, the single matcher (interface.md
    "Environment"). It becomes the server's global environment when a command starts one."""
    return open_cmd.filtered_env(ctx.environ, ctx.config.server_env_allow)


def read_panes(ctx: Context) -> PaneRead:
    """Every managed pane of the server, read once per invocation; ``server_running`` is
    false when no server runs (no tmux call is made when the socket checks find none).

    Each reader warning is printed (escaped, like all stderr output) and, in a logged
    command, written as a ``warning`` log record (``Context.warn``).
    """
    if ctx.pane_read is None:
        backend = connect(ctx)
        if backend is None:
            ctx.pane_read = PaneRead(server_running=False, panes=(), warnings=())
        else:
            ctx.pane_read = backend.read_panes()
        for warning in ctx.pane_read.warnings:
            ctx.warn(warning)
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


def open_pane(args: argparse.Namespace, ctx: Context) -> Result:
    """Steps 4 on of the open order (steps 1 to 3 ran in ``dispatch``)."""
    assert ctx.selection is not None and ctx.server is not None and ctx.window is not None
    if args.dry_run:
        # TODO(#18): the dry run stops after step 3 and prints what a real run would do: its
        # placement, matched_rule, session and target are in ctx.selection, its id in
        # ctx.open_id, the window in ctx.window.
        raise TermctlError("internal", f"{PROG} open --dry-run is not implemented yet")
    assert ctx.plan is not None and ctx.open_id is not None and ctx.signals is not None
    run_pre_open_hooks(args, ctx)
    # Steps 5 to 8 run with stop signals deferred: a signal there is raised only once the
    # output exists and its audit record is written, so it never leaves an untagged window or
    # an override run's pane without an audit record (interface.md "Audit and log").
    with ctx.signals.deferred():
        backend = unchecked_backend(ctx)
        created = open_cmd.create_window(
            backend, ctx.server, ctx.gateway, ctx.selection.session, ctx.window
        )
        created_at = open_cmd.timestamp(ctx.clock.now())
        # tag_failed raises here, after the guarded cleanup; no post-open hook runs then.
        query = open_cmd.tag(backend, created, ctx.open_id, ctx.plan, created_at, ctx.warn)
        open_cmd.record(
            backend, ctx.gateway, ctx.server.socket_path, created, ctx.open_id, query, ctx.warn
        )
        # TODO(#24): step 8, --focus.
        result = _opened(ctx, backend, created, created_at)
        _audit_open(args, ctx, result.data)
    # Step 9: the post-open hooks, the log record and the output.
    run_post_open_hooks(args, ctx, result.data)
    ctx.signals.finish()
    ctx.log(_open_record(args, ctx, result="ok", exit_code=0))
    return result


def resolve_actor(args: argparse.Namespace, ctx: Context) -> str:
    """``--actor`` or the default, resolved once per run, so the ``human:unknown`` warning is
    printed (and logged) once."""
    if ctx.actor is None:
        ctx.actor, warning = validate.resolve_actor(args.actor, ctx.gateway)
        if warning is not None:
            ctx.warn(warning)
    return ctx.actor


def run_pre_open_hooks(args: argparse.Namespace, ctx: Context) -> None:
    """Step 4: the ``pre-open`` hooks, run once; a veto exits 11 and an escalation 13 (not on
    an ``--override-escalation`` run), before anything is created. The ids they return
    follow the ``--decision-id`` values in ``ctx.decision_ids``."""
    assert ctx.plan is not None and ctx.selection is not None and ctx.open_id is not None
    plan = ctx.plan
    override = bool(args.override_escalation)
    payload = hooks.pre_open_payload(
        object_id=ctx.open_id,
        labels=plan.labels,
        name=plan.name,
        cwd=plan.cwd,
        argv=plan.argv,
        env_names=[name for name, _ in plan.env],
        placement=plan.placement,
        session=ctx.selection.session,
        target=ctx.selection.target,
        socket_path=_socket_path(ctx),
        keep_on_exit=plan.keep_on_exit,
        focus=bool(args.focus),
        actor=resolve_actor(args, ctx),
        decision_ids=args.decision_ids,
        override_escalation=override,
    )
    # The ids and processes go straight into ctx, so a stop signal loses neither.
    outcome = hooks.run_pre_hooks(
        hooks.hooks_for(ctx.config, "pre-open", args.flag_hooks),
        payload,
        decision_ids=args.decision_ids,
        override_escalation=override,
        gateway=ctx.gateway,
        environ=ctx.environ,
        ids=ctx.decision_ids,
        processes=ctx.hook_processes,
    )
    _hook_warnings(ctx, outcome.warnings)
    outcome.check()


def run_post_open_hooks(args: argparse.Namespace, ctx: Context, output: dict[str, Any]) -> None:
    """Step 9: the ``post-open`` hooks, with the ``open`` output as their payload's ``pane``
    and the labels termctl was given as its top-level ``labels``; a failure is a warning and
    a ``hook_failed`` record, never an error."""
    assert ctx.plan is not None
    outcome = hooks.run_post_hooks(
        hooks.hooks_for(ctx.config, "post-open", args.flag_hooks),
        hooks.post_open_payload(pane=output, labels=ctx.plan.labels),
        gateway=ctx.gateway,
        environ=ctx.environ,
        processes=ctx.hook_processes,
    )
    _hook_warnings(ctx, outcome.warnings)


def _hook_warnings(ctx: Context, warnings: Sequence[hooks.HookWarning]) -> None:
    """Print each hook warning, followed in human mode by the hook's stderr on lines of their
    own (``Output.hook_stderr``), and write its ``hook_failed`` record. The warning itself
    never holds hook output."""
    for warning in warnings:
        ctx.out.warn(warning.message)
        if warning.hook_stderr and not (ctx.out.json or ctx.out.quiet):
            ctx.out.hook_stderr(warning.hook_stderr)
        ctx.log(warning.log_record(ctx.clock.now(), id=ctx.open_id, socket_path=ctx.socket_path()))


def _open_record(
    args: argparse.Namespace,
    ctx: Context,
    *,
    result: str,
    exit_code: int,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``log.jsonl`` record of this ``open`` (interface.md "Audit and log")."""
    actor = resolve_actor(args, ctx)  # first: its warning record precedes this one
    return jsonlog.log_record(
        ctx.clock.now(),
        event="open",
        result=result,
        id=ctx.open_id,
        socket_path=ctx.socket_path(),
        exit_code=exit_code,
        message=message,
        details=details,
        actor=actor,
        decision_ids=ctx.decision_ids,
        escalation_override=bool(args.override_escalation),
        hooks=[process.log_entry() for process in ctx.hook_processes],
    )


def _audit_open(args: argparse.Namespace, ctx: Context, result: Mapping[str, Any]) -> None:
    """The audit record of an ``--override-escalation`` run, once its id exists (step 2), and
    only once; ``result`` is the ``--json`` output or the error envelope."""
    if not args.override_escalation or ctx.open_id is None or ctx.audited:
        return
    ctx.audited = True
    assert ctx.plan is not None and ctx.selection is not None and ctx.server is not None
    actor = resolve_actor(args, ctx)
    ctx.audit(
        jsonlog.audit_open_record(
            ctx.clock.now(),
            actor=actor,
            decision_ids=ctx.decision_ids,
            target=ctx.open_id,
            socket_path=ctx.server.socket_path,
            name=ctx.plan.name,
            argv=ctx.plan.argv,
            cwd=ctx.plan.cwd,
            placement=ctx.plan.placement,
            session=ctx.selection.session,
            env_names=[name for name, _ in ctx.plan.env],
            labels=ctx.plan.labels,
            result=result,
        )
    )


def _open_failed(args: argparse.Namespace, ctx: Context, err: TermctlError) -> None:
    """Log (and, for an override run, audit) an ``open`` that failed after step 1. The error
    carries the decision ids known so far (interface.md "Errors")."""
    if err.decision_ids is None and ctx.decision_ids:
        err.decision_ids = list(ctx.decision_ids)
    if err.code in ("vetoed", "escalated"):
        result, details = err.code, {"hook": err.hook}
    else:
        result, details = "error", err.details
    ctx.log(
        _open_record(
            args, ctx, result=result, exit_code=err.exit_code, message=err.message, details=details
        )
    )
    _audit_open(args, ctx, err.envelope())


def _open_stopped(args: argparse.Namespace, ctx: Context, stop: BaseException) -> None:
    """Log an ``open`` stopped by a signal (exit 128 plus its number). Its audit record, if
    any, was written once the output existed (``open_pane``); a run stopped before then has
    neither output nor error object, and gets none."""
    exit_code, message = _stopped(stop)
    ctx.log(_open_record(args, ctx, result="error", exit_code=exit_code, message=message))


def _stopped(stop: BaseException) -> tuple[int, str]:
    """The exit code (128 plus the signal number) and message of a run a signal stopped."""
    if isinstance(stop, Terminated):
        return 128 + stop.signum, f"terminated by signal {stop.signum}"
    return 128 + int(signal.SIGINT), "interrupted"


def _opened(ctx: Context, backend: Backend, created: Created, created_at: str) -> Result:
    """The ``open`` output, from a read after step 6: the pane object, or, if the pane is no
    longer there, ``state: gone`` with the last known values (interface.md ``open``). A read
    that fails also reports ``state: gone``, with a warning."""
    assert ctx.plan is not None and ctx.open_id is not None and ctx.selection is not None
    socket_path = _socket_path(ctx)
    pane: ManagedPane | None = None
    try:
        read = backend.read_panes()
    except TermctlError as err:
        ctx.warn(f"could not read pane {ctx.open_id} after opening it: {err.message}")
    else:
        seen = () if ctx.pane_read is None else ctx.pane_read.warnings
        for warning in read.warnings:
            if warning not in seen:
                ctx.warn(warning)
        pane = next(
            (p for p in read.panes if p.id == ctx.open_id and p.pane_id == created.pane_id),
            None,
        )
    if pane is not None:
        data = pane.pane_object(socket_path)
    else:
        data = {
            "id": ctx.open_id,
            "name": ctx.plan.name,
            "display_name": ctx.plan.name,
            "marker": None,
            "marker_text": None,
            "labels": list(ctx.plan.labels),
            "placement": ctx.plan.placement,
            "session": ctx.selection.session,
            "pane_id": created.pane_id,
            "window_id": created.window_id,
            "socket_path": socket_path,
            "created": created_at,
            "pid": None,
            "state": "gone",
            "exit_status": None,
            "exit_signal": None,
            "keep_on_exit": ctx.plan.keep_on_exit,
        }
    # TODO(#24): `focused` reports the outcome of --focus (step 8).
    data["focused"] = False
    data["decision_ids"] = list(ctx.decision_ids)
    return Result(data, ctx.open_id)


# --- pane changes -------------------------------------------------------------------------------


def _change_backend(ctx: Context) -> Backend:
    """The backend for a pane change. The server may have gone since step 4: the pane went
    with it (interface.md "Panes that vanish")."""
    backend = connect(ctx)
    if backend is None:
        assert ctx.pane is not None
        raise TermctlError(
            "not_found", f"managed pane {ctx.pane.id} is gone (no tmux server is running)"
        )
    return backend


def _apply_change(
    ctx: Context, plan: Callable[[ManagedPane], Change]
) -> tuple[dict[str, Any], Outcome]:
    """Apply ``plan`` to the resolved pane under the lock; a change that changes nothing is a
    no-op. Returns the pane-change ``--json`` object and the outcome."""
    assert ctx.pane is not None and ctx.tmux is not None
    outcome = change_pane(
        _change_backend(ctx),
        ctx.pane.id,
        plan,
        version=ctx.tmux.version,
        environ=ctx.environ,
        clock=ctx.clock,
        warn=ctx.warn,
        seen=() if ctx.pane_read is None else ctx.pane_read.warnings,
    )
    ctx.no_op = not outcome.changed
    data = {
        **outcome.pane.pane_object(_socket_path(ctx)),
        "changed": outcome.changed,
        "display_updated": outcome.display_updated,
        "dry_run": False,
    }
    return data, outcome


def rename(args: argparse.Namespace, ctx: Context) -> Result:
    """Set ``@termctl_name`` and recompute the displayed name, keeping a set marker; the
    window is renamed only while termctl may (interface.md `rename`, "Names")."""
    if args.dry_run:
        # TODO(#22): rename --dry-run prints the pane as it would be (interface.md "Dry runs").
        raise TermctlError("internal", f"{PROG} rename --dry-run is not implemented yet")

    def plan(pane: ManagedPane) -> Change:
        shown = display.displayed_name(args.name, pane.marker_text, ctx.config.marker_display)
        return Change(options={"@termctl_name": args.name}, display_name=shown)

    data, outcome = _apply_change(ctx, plan)
    return Result(data, outcome.pane.display_name)


def mark(args: argparse.Namespace, ctx: Context) -> Result:
    """Set the marker, with its text from the current config, in one write with the
    recomputed displayed name; a ``bell`` marker rings when something changed on a running
    pane (interface.md "Markers", `mark`). ``load_config`` checked that it is defined."""
    if args.dry_run:
        # TODO(#22): mark --dry-run prints the pane as it would be (interface.md "Dry runs").
        raise TermctlError("internal", f"{PROG} mark --dry-run is not implemented yet")
    marker = ctx.config.markers[args.marker]

    def plan(pane: ManagedPane) -> Change:
        shown = display.displayed_name(pane.name, marker.text, ctx.config.marker_display)
        options = {"@termctl_marker": args.marker, "@termctl_marker_text": marker.text}
        return Change(options=options, display_name=shown)

    data, outcome = _apply_change(ctx, plan)
    if outcome.changed and marker.bell:
        _ring_bell(ctx, outcome.pane)
    return Result(data, outcome.pane.display_name)


def _ring_bell(ctx: Context, pane: ManagedPane) -> None:
    """The bell of a changed ``mark``; a failure is a warning only (interface.md "Markers")."""
    if pane.dead:
        return
    backend = connect(ctx)
    warning = (
        f"bell not written: no tmux server is running for managed pane {pane.id}"
        if backend is None
        else display.ring_bell(backend, ctx.gateway, pane)
    )
    if warning is not None:
        ctx.warn(warning)


def unmark(args: argparse.Namespace, ctx: Context) -> Result:
    """Clear the marker, whether or not config still defines it; the displayed name becomes
    the name (interface.md `unmark`)."""
    if args.dry_run:
        # TODO(#22): unmark --dry-run prints the pane as it would be (interface.md "Dry runs").
        raise TermctlError("internal", f"{PROG} unmark --dry-run is not implemented yet")

    def plan(pane: ManagedPane) -> Change:
        shown = display.displayed_name(pane.name, None, ctx.config.marker_display)
        options = {"@termctl_marker": "", "@termctl_marker_text": ""}
        return Change(options=options, display_name=shown)

    data, outcome = _apply_change(ctx, plan)
    return Result(data, outcome.pane.display_name)


def label_change(args: argparse.Namespace, ctx: Context) -> Result:
    """``label add`` and ``label remove``: the pane's labels are a set of pairs, so adding a
    present pair or removing an absent one changes nothing; they are written in canonical
    form (sorted by code point, unique, single spaces) and the window is never renamed
    (interface.md `label add` / `label remove`, "Labels")."""
    if args.dry_run:
        # TODO(#22): label --dry-run prints the pane as it would be (interface.md "Dry runs").
        raise TermctlError("internal", f"{PROG} {args.command} --dry-run is not implemented yet")
    pairs = set(validate.parse_labels(args.pairs))
    adding = args.command == "label add"

    def plan(pane: ManagedPane) -> Change:
        current = set(pane.labels)
        labels = sorted(current | pairs if adding else current - pairs)
        # The limit applies to the labels read under the lock, after id resolution, so the
        # failure is logged; a removal never grows the set.
        if len(labels) > validate.MAX_LABELS:
            raise UsageError(
                f"managed pane {pane.id} would carry {len(labels)} labels "
                f"(at most {validate.MAX_LABELS})"
            )
        return Change(options={"@termctl_labels": " ".join(labels)}, display_name=None)

    data, outcome = _apply_change(ctx, plan)
    return Result(data, " ".join(outcome.pane.labels))


COMMAND_STEPS = {
    "show": show,
    "exists": exists,
    "list": list_panes,
    "open": open_pane,
    "rename": rename,
    "mark": mark,
    "unmark": unmark,
    "label add": label_change,
    "label remove": label_change,
}


def run_command(args: argparse.Namespace, ctx: Context) -> Result:
    """Step 5: the command's own steps."""
    step = COMMAND_STEPS.get(args.command)
    if step is not None:
        return step(args, ctx)
    if args.command == "placements":
        labels = validate.parse_labels(args.labels) if args.labels else None
        return placement.report(ctx.config, labels)
    # TODO(#23): close.
    # TODO(#24): focus.
    raise TermctlError("internal", f"{PROG} {args.command} is not implemented yet")


def dispatch(args: argparse.Namespace, ctx: Context) -> Result:
    """Run the check order after the flag-level usage checks; stop at the first failure."""
    check_args(args, ctx)
    load_config(args, ctx)
    if args.command == "open" and not args.dry_run:
        return _logged_open(args, ctx)
    if args.command in CHANGE_EVENTS and not args.dry_run:
        return _logged_change(args, ctx)
    return _resolve_and_run(args, ctx)


def _resolve_and_run(args: argparse.Namespace, ctx: Context) -> Result:
    """Check order steps 2 to 5 (``open``: Open order steps 2 on)."""
    spec = COMMANDS[args.command]
    if spec.uses_tmux:
        resolve_server(args, ctx)
        if args.command == "open":
            # Open order step 2: the server, then tmux_bin (tmux_missing is reported here,
            # before not_supported; `tmux -V` runs in step 3), then the placement.
            resolve_tmux_bin(ctx)
            resolve_placement(args, ctx)
            resolve_command(args, ctx)
        check_tmux(args, ctx)
        if args.command == "open":
            check_id_collision(ctx)
        if spec.takes_id:
            resolve_id(args, ctx)
    return run_command(args, ctx)


def _logged_open(args: argparse.Namespace, ctx: Context) -> Result:
    """``open`` past step 1, not a dry run: every outcome gets a ``log.jsonl`` record, and an
    ``--override-escalation`` run an audit record once its id exists (interface.md "Audit
    and log"). Warnings become ``warning`` records from here on."""
    ctx.logged = True
    ctx.decision_ids = list(args.decision_ids)
    # SIGINT, SIGTERM and SIGHUP raise for the whole run, so it is logged whenever one stops it.
    with stop_signals() as signals:
        ctx.signals = signals
        try:
            return _resolve_and_run(args, ctx)
        except TermctlError as err:
            signals.finish()
            _open_failed(args, ctx, err)
            raise
        except (KeyboardInterrupt, Terminated) as stop:
            signals.finish()
            _open_stopped(args, ctx, stop)
            raise
        except Exception as exc:
            signals.finish()
            err = internal_error(exc)
            _open_failed(args, ctx, err)
            raise err from exc


# The pane-change commands, by their log.jsonl event (interface.md "Audit and log").
CHANGE_EVENTS = {
    "rename": "rename",
    "mark": "mark",
    "unmark": "unmark",
    "label add": "label_add",
    "label remove": "label_remove",
}


def _logged_change(args: argparse.Namespace, ctx: Context) -> Result:
    """A pane change past step 1, not a dry run: one ``log.jsonl`` record for its outcome,
    failures from step 2 on and a stop by a signal (exit 128 plus its number) included, except
    a no-op, which writes no record at all (interface.md "Audit and log"). Its warnings are
    printed at once; their ``warning`` records are held and written before that record, or
    dropped with a no-op. Stop signals raise as for ``open`` (``stop_signals``) but are never
    deferred: a change stopped after its write was applied is logged as stopped."""
    ctx.logged = True
    ctx.held_warnings = []
    event = CHANGE_EVENTS[args.command]

    def write(record: dict[str, Any]) -> None:
        ctx.write_held_warnings()
        ctx.log(record)

    with stop_signals() as signals:
        try:
            result = _resolve_and_run(args, ctx)
        except TermctlError as err:
            signals.finish()
            write(_change_record(ctx, event, "error", err.exit_code, err))
            raise
        except (KeyboardInterrupt, Terminated) as stop:
            signals.finish()
            exit_code, message = _stopped(stop)
            write(_change_record(ctx, event, "error", exit_code, message=message))
            raise
        except Exception as exc:
            signals.finish()
            err = internal_error(exc)
            write(_change_record(ctx, event, "error", err.exit_code, err))
            raise err from exc
        signals.finish()
        if ctx.no_op:
            ctx.held_warnings = None
        else:
            write(_change_record(ctx, event, "ok", result.exit_code))
    return result


def _change_record(
    ctx: Context,
    event: str,
    result: str,
    exit_code: int,
    err: TermctlError | None = None,
    *,
    message: str | None = None,
) -> dict[str, Any]:
    return jsonlog.log_record(
        ctx.clock.now(),
        event=event,
        result=result,
        id=ctx.record_id(),
        socket_path=ctx.socket_path(),
        exit_code=exit_code,
        message=message if err is None else err.message,
        details=None if err is None else err.details,
    )


def internal_error(exc: Exception) -> TermctlError:
    """An unexpected exception as the ``internal`` error (exit 1)."""
    return TermctlError("internal", f"internal error: {type(exc).__name__}: {exc}")


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
    except KeyboardInterrupt:
        # A hook that was running has been killed (gateway.run_group).
        out.interrupted("interrupted")
        return 128 + signal.SIGINT
    except Terminated as stop:
        out.interrupted(f"terminated by signal {stop.signum}")
        return 128 + stop.signum
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
        if isinstance(err, UsageError) and err.usage is None:
            # A usage error found after parsing concerns the subcommand that was parsed.
            err.usage = scanned.parser.format_usage()
        out.error(err)
        return err.exit_code
    except Exception as exc:
        err = internal_error(exc)
        out.error(err)
        return err.exit_code
    out.result(result)
    return result.exit_code
