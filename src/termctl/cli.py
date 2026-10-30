"""Command-line entry point: the argparse surface, the check order, output and exit codes.

interface.md "Commands", "Check order", "Global flags", "Exit codes" and "Errors" are
normative for everything here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn, TextIO

from . import __version__, validate
from .errors import TermctlError, UsageError
from .gateway import Clock, Gateway, SystemClock, SystemGateway

PROG = "termctl"


# --- output -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    """A command's output: ``data`` for ``--json``, ``human`` for human mode.

    ``exit_code`` is non-zero only for outcomes that print normal output rather than an
    error object (``exists`` exiting 10).
    """

    data: dict[str, Any]
    human: str
    exit_code: int = 0


@dataclass
class Output:
    """Where and how termctl writes. stdout carries results; stderr carries everything else."""

    stdout: TextIO
    stderr: TextIO
    json: bool = False
    verbose: bool = False
    quiet: bool = False

    def result(self, result: Result) -> None:
        if self.json:
            # ensure_ascii escapes C0, DEL and every non-ASCII character.
            self.stdout.write(json.dumps(result.data) + "\n")
        elif result.human:
            self.stdout.write(result.human + "\n")

    def error(self, err: TermctlError) -> None:
        """Errors always reach stderr (even with ``--quiet``); with ``--json`` also stdout."""
        self.stderr.write(f"{PROG}: {err.message}\n")
        if isinstance(err, UsageError) and err.usage and not self.json:
            self.stderr.write(err.usage)
        if self.json:
            self.stdout.write(json.dumps(err.envelope()) + "\n")

    def warn(self, message: str) -> None:
        if not self.quiet:
            self.stderr.write(f"{PROG}: warning: {message}\n")

    def trace(self, message: str) -> None:
        """``--verbose`` detail, such as each tmux command termctl runs."""
        if self.verbose:
            self.stderr.write(f"{PROG}: {message}\n")


@dataclass
class Context:
    """Everything a command may touch outside its arguments."""

    gateway: Gateway
    clock: Clock
    environ: Mapping[str, str]
    out: Output = field(repr=False)


# --- argument parsing ---------------------------------------------------------------------------


class _VersionRequested(Exception):
    pass


class _VersionAction(argparse.Action):
    """``--version`` anywhere before ``--`` wins over every other check."""

    def __init__(self, option_strings: Sequence[str], dest: str, **kwargs: Any) -> None:
        super().__init__(option_strings, dest=argparse.SUPPRESS, nargs=0, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise _VersionRequested


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that raises ``UsageError`` instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(message, usage=self.format_usage())


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


def _subparser(
    subparsers: argparse._SubParsersAction[_Parser], name: str, command: str, help_text: str
) -> _Parser:
    parser = subparsers.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
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
        prog=PROG, description="Terminal placement (tmux backend).", allow_abbrev=False
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


def _before_double_dash(raw: Sequence[str]) -> Sequence[str]:
    return raw[: raw.index("--")] if "--" in raw else raw


def _check_open_argv(args: argparse.Namespace, raw: Sequence[str]) -> None:
    """argv is accepted only after ``--``, and ``--`` must be followed by argv.

    No option takes ``--`` as its value, so the first ``--`` in ``raw`` is the separator.
    """
    if "--" not in raw:
        if args.argv:
            raise UsageError("the command to run must follow '--'")
        return
    tail = list(raw[raw.index("--") + 1 :])
    if not tail:
        raise UsageError("'--' must be followed by the command to run")
    if args.argv != tail:
        raise UsageError("the command to run must follow '--'")


def parse_args(raw: Sequence[str]) -> argparse.Namespace:
    """Parse ``raw`` and make the flag-level usage checks (step 1 of the check order).

    Raises ``UsageError`` on any usage error and ``_VersionRequested`` for ``--version``.
    """
    args = build_parser().parse_args(list(raw))
    if args.verbose and args.quiet:
        raise UsageError("--verbose and --quiet cannot be combined")
    if args.command == "open":
        _check_open_argv(args, raw)
    if args.command == "close" and args.grace is not None and not args.force:
        raise UsageError("--grace requires --force")
    return args


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


def load_config(args: argparse.Namespace, ctx: Context) -> None:
    """Step 1 (rest): the config file, drop-ins and ``TERMCTL_*`` variables (exit 2)."""
    # TODO: config loading - issue #3 (config.py)


def resolve_server(args: argparse.Namespace, ctx: Context) -> None:
    """Step 2: resolve the server, without side effects."""
    # TODO: socket resolution - issue #8


def check_tmux(args: argparse.Namespace, ctx: Context) -> None:
    """Step 3: tmux binary, version and socket safety checks (exit 12)."""
    # TODO: tmux binary and version - issue #10; socket safety - issue #8


def resolve_id(args: argparse.Namespace, ctx: Context) -> None:
    """Step 4: resolve the id to one managed pane (exit 10 or 21)."""
    # TODO: read the managed panes (issue #11) and pick one with ids.resolve - issue #12


def run_command(args: argparse.Namespace, ctx: Context) -> Result:
    """Step 5: the command's own steps."""
    # TODO: each command lands in its own issue (#7, #12-#24).
    raise TermctlError("internal", f"{PROG} {args.command} is not implemented yet")


def dispatch(args: argparse.Namespace, ctx: Context) -> Result:
    """Run the check order after the flag-level usage checks; stop at the first failure."""
    spec = COMMANDS[args.command]
    validate.check_args(args)
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
    raw = list(sys.argv[1:] if argv is None else argv)
    # Decided before parsing so that usage errors get the envelope too; never after `--`.
    out = Output(
        stdout=stdout or sys.stdout,
        stderr=stderr or sys.stderr,
        json="--json" in _before_double_dash(raw),
    )
    try:
        args = parse_args(raw)
    except _VersionRequested:
        out.stdout.write(f"{PROG} {__version__}\n")
        return 0
    except TermctlError as err:
        out.error(err)
        return err.exit_code
    except SystemExit as exc:  # --help
        return exc.code if isinstance(exc.code, int) else 0

    out.json, out.verbose, out.quiet = args.json, args.verbose, args.quiet
    ctx = Context(
        gateway=gateway or SystemGateway(),
        clock=clock or SystemClock(),
        environ=os.environ if environ is None else environ,
        out=out,
    )
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
