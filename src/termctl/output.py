"""Writing results, errors, warnings and traces (interface.md "Human output" and "Errors").

stdout carries results; stderr carries everything else. All human output is escaped with
``escape``; ``--json`` output is not. A failed write never changes the exit code: termctl has
nowhere better to report it.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, TextIO

from .errors import TermctlError, UsageError

PROG = "termctl"

_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")


def escape(text: str) -> str:
    """Escape each C0, DEL and C1 control character as ``\\xNN`` (interface.md "Human output")."""
    return _CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", text)


def _escape_lines(text: str) -> str:
    """``escape`` each line of ``text``, keeping its line breaks."""
    return "\n".join(escape(line) for line in text.split("\n"))


def _to_json(data: dict[str, Any]) -> str:
    # interface.md "Human output": JSON string encoding escapes the control characters, so
    # --json output is never escaped as human output is. ensure_ascii also escapes C1 and
    # every other non-ASCII character; it is the default, but stated so it is not lost.
    return json.dumps(data, ensure_ascii=True) + "\n"


def _write(stream: TextIO, text: str) -> None:
    try:
        stream.write(text)
    except OSError:
        pass


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
    """Where and how termctl writes."""

    stdout: TextIO
    stderr: TextIO
    json: bool = False
    verbose: bool = False
    quiet: bool = False

    def result(self, result: Result) -> None:
        if self.json:
            _write(self.stdout, _to_json(result.data))
        elif result.human:
            _write(self.stdout, _escape_lines(result.human) + "\n")

    def text(self, text: str) -> None:
        """Other human output on stdout, such as the help or the version."""
        _write(self.stdout, _escape_lines(text))

    def error(self, err: TermctlError) -> None:
        """Errors always reach stderr (even with ``--quiet``); with ``--json`` stdout first."""
        if self.json:
            _write(self.stdout, _to_json(err.envelope()))
        self._stderr(err.message)
        if isinstance(err, UsageError) and err.usage and not self.json:
            _write(self.stderr, _escape_lines(err.usage))

    def warn(self, message: str) -> None:
        if not self.quiet:
            self._stderr(f"warning: {message}")

    def trace(self, message: str) -> None:
        """``--verbose`` detail, such as each tmux command termctl runs."""
        if self.verbose:
            self._stderr(message)

    def _stderr(self, message: str) -> None:
        _write(self.stderr, f"{PROG}: {escape(message)}\n")


def flush_standard_streams() -> None:
    """Flush ``sys.stdout`` and ``sys.stderr``. If one fails, point its descriptor at the null
    device, so that the interpreter's own flush at exit cannot fail too, print "Exception
    ignored" and exit 120."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.flush()
        except OSError:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, stream.fileno())
            os.close(devnull)
