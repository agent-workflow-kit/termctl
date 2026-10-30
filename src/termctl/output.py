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
_SURROGATE = re.compile("[\ud800-\udfff]")


def _surrogate_text(match: re.Match[str]) -> str:
    """An undecodable argument byte (``\\udc80``-``\\udcff``, from surrogateescape) as
    ``\\xNN``; any other lone surrogate as ``\\uXXXX``."""
    point = ord(match.group())
    if 0xDC80 <= point <= 0xDCFF:
        return f"\\x{point - 0xDC00:02x}"
    return f"\\u{point:04x}"


def _map_surrogates(text: str) -> str:
    return _SURROGATE.sub(_surrogate_text, text)


def escape(text: str) -> str:
    """Escape each C0, DEL and C1 control character as ``\\xNN`` and map lone surrogates
    (interface.md "Human output")."""
    text = _map_surrogates(text)
    return _CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", text)


def _escape_lines(text: str) -> str:
    """``escape`` each line of ``text``, keeping its line breaks."""
    return "\n".join(escape(line) for line in text.split("\n"))


def _without_surrogates(value: Any) -> Any:
    """``value`` with every string's lone surrogates mapped as ``escape`` maps them."""
    if isinstance(value, str):
        return _map_surrogates(value)
    if isinstance(value, dict):
        return {_without_surrogates(k): _without_surrogates(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_without_surrogates(item) for item in value]
    return value


def _to_json(data: dict[str, Any]) -> str:
    # interface.md "Human output": JSON string encoding escapes the control characters, so
    # --json output is never escaped as human output is. ensure_ascii also escapes C1 and
    # every other non-ASCII character; it is the default, but stated so it is not lost.
    # Lone surrogates are mapped first, so a strict parser accepts the output.
    return json.dumps(_without_surrogates(data), ensure_ascii=True) + "\n"


def _write(stream: TextIO, text: str) -> None:
    # ValueError covers an encoding error (UnicodeEncodeError) and a closed stream.
    try:
        stream.write(text)
    except (OSError, ValueError):
        pass


def _flush(stream: TextIO) -> None:
    try:
        stream.flush()
    except (OSError, ValueError):
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
            # Flushed now so that it precedes the message when both go to one pipe or file.
            _flush(self.stdout)
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
