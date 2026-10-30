"""A pre-scan of the command line, made before argparse sees it (interface.md "Global flags").

argparse refuses an option value that begins with ``-``. termctl follows getopt instead: an
option that takes a value always consumes the next token as its value. ``scan`` walks the
tokens with the parsers' own option tables, so it knows which options take values, which
(sub)parser each token belongs to and where the ``--`` separator is. The ``--json`` and
``--version`` checks read its result, so they agree with argparse about what is a value.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from dataclasses import dataclass

# argparse treats tokens like these as positionals, not options.
_NEGATIVE_NUMBER = re.compile(r"^-\d+$|^-\d*\.\d+$")


@dataclass(frozen=True)
class Scan:
    args: list[str]
    """The tokens for argparse, with each separately given option value joined as ``--opt=v``."""
    options: list[str]
    """The option names given before the separator, without their values."""
    unknown: list[str]
    """Option tokens the (sub)parser that met them does not define."""
    parser: argparse.ArgumentParser
    """The deepest parser reached: the subcommand's, or the top level's if none was named."""
    early_separator: bool
    """A ``--`` came before the command was complete."""

    @property
    def complete(self) -> bool:
        """A full command was named (``label`` alone is not one)."""
        return not subcommands(self.parser)


def subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _is_option(token: str) -> bool:
    return token.startswith("-") and token != "-" and not _NEGATIVE_NUMBER.match(token)


def scan(parser: argparse.ArgumentParser, raw: Sequence[str]) -> Scan:
    tokens = list(raw)
    args: list[str] = []
    options: list[str] = []
    unknown: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            args.extend(tokens[i:])
            return Scan(args, options, unknown, parser, early_separator=bool(subcommands(parser)))
        if _is_option(token):
            name = token.split("=", 1)[0]
            options.append(name)
            action = parser._option_string_actions.get(name)
            if action is None:
                unknown.append(token)
            elif action.nargs != 0 and name == token and i + 1 < len(tokens):
                # Only long options take values, so the `=` form is always available.
                i += 1
                token = f"{token}={tokens[i]}"
        else:
            parser = subcommands(parser).get(token, parser)
        args.append(token)
        i += 1
    return Scan(args, options, unknown, parser, early_separator=False)
