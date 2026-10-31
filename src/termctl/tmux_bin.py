"""The tmux binary: resolution of ``tmux_bin`` and the version check (interface.md "Check order"
step 3, "Config" ``tmux_bin``, "Errors" tmux version, "Platform notes").

``probe`` runs once per invocation, before the first tmux operation, and its ``TmuxBinary``
carries the absolute path every later tmux call uses and the parsed version.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

from .errors import TermctlError
from .gateway import Gateway

# The oldest supported tmux (design.md D16).
FLOOR = (3, 2)
# Seconds; `tmux -V` prints and exits without reaching a server.
VERSION_TIMEOUT = 10.0
# The first `\d+\.\d+` of the output (interface.md), ASCII digits only. The lookbehind starts a
# match only at the start of a run of digits: the first match is the same, but the search stays
# linear on a long run of digits with no dot, where a plain search backtracks quadratically.
_VERSION = re.compile(r"(?<![0-9])([0-9]+)\.([0-9]+)", re.ASCII)
# A version component with more digits than this compares as this large number. Python
# refuses to convert strings of more than 4300 digits to int.
_MAX_DIGITS = 9
_HUGE = 10**_MAX_DIGITS
# How much of unparsable `tmux -V` output a warning quotes.
_QUOTED = 80


class TmuxVersion(NamedTuple):
    """A parsed ``tmux -V``: ``tmux next-3.6`` is ``(3, 6, "3.6")``. Compares with ``FLOOR``
    as a tuple, by ``major`` and then ``minor``."""

    major: int
    minor: int
    text: str


@dataclass(frozen=True)
class TmuxBinary:
    """The resolved tmux: ``path`` is absolute; ``version`` is None when ``tmux -V`` printed
    nothing parsable (assumed new enough)."""

    path: str
    version: TmuxVersion | None

    @property
    def version_text(self) -> str | None:
        """The version as reported in outputs (`open --dry-run`'s ``tmux_version``)."""
        return None if self.version is None else self.version.text


def _component(digits: str) -> int:
    digits = digits.lstrip("0") or "0"
    return _HUGE if len(digits) > _MAX_DIGITS else int(digits)


def parse_version(output: str) -> TmuxVersion | None:
    """The first ``major.minor`` in ``output``, or None if it has none."""
    match = _VERSION.search(output)
    if match is None:
        return None
    return TmuxVersion(_component(match[1]), _component(match[2]), match[0])


def _missing(tmux_bin: str, message: str) -> TermctlError:
    return TermctlError("tmux_missing", message, details={"tmux_bin": tmux_bin})


def resolve(tmux_bin: str, environ: Mapping[str, str], gateway: Gateway) -> str:
    """The absolute path of ``tmux_bin``: kept if absolute, else searched on the caller's
    ``PATH``. Raises ``tmux_missing`` (exit 12) if there is no executable regular file.

    Only absolute ``PATH`` entries are searched: an empty or relative entry would depend on the
    current directory. Without ``PATH`` nothing is searched (no default path). A relative
    ``tmux_bin`` containing ``/`` is refused, as is ``.`` or ``..``.
    """
    if os.path.isabs(tmux_bin):
        if gateway.is_executable_file(tmux_bin):
            return tmux_bin
        raise _missing(tmux_bin, f"tmux not found: {tmux_bin} is not an executable file")
    if "/" in tmux_bin or tmux_bin in (".", ".."):
        raise _missing(
            tmux_bin,
            f"tmux not found: tmux_bin {tmux_bin} must be a command name or an absolute path",
        )
    for entry in environ.get("PATH", "").split(os.pathsep):
        if not os.path.isabs(entry):
            continue
        candidate = os.path.join(entry, tmux_bin)
        if gateway.is_executable_file(candidate):
            return candidate
    raise _missing(tmux_bin, f"tmux not found: no executable {tmux_bin} on PATH")


def check_version(
    path: str, gateway: Gateway, trace: Callable[[str], None] | None = None
) -> tuple[TmuxVersion | None, str | None]:
    """Run ``<path> -V`` and enforce the 3.2 floor.

    Returns the version and a warning: output with no version is assumed new enough, with a
    warning. Raises ``tmux_too_old`` below the floor and ``tmux_missing`` when the binary
    cannot be run, times out or exits non-zero.
    """
    argv = [path, "-V"]
    if trace is not None:
        trace(shlex.join(argv))
    try:
        # TODO(#15): run with the filtered environment; `tmux -V` needs none, so it gets none.
        result = gateway.run(argv, timeout=VERSION_TIMEOUT, env={})
    except UnicodeDecodeError:
        return None, f"cannot decode the output of {path} -V; assuming tmux 3.2 or newer"
    except subprocess.TimeoutExpired:
        raise _missing(path, f"cannot run {path} -V: timed out") from None
    except OSError as exc:
        raise _missing(path, f"cannot run {path}: {exc.strerror or exc}") from None
    if result.returncode != 0:
        raise _missing(path, f"cannot run {path} -V: exit status {result.returncode}")
    version = parse_version(result.stdout)
    if version is None:
        shown = result.stdout.strip()[:_QUOTED]
        return None, f"no version in {path} -V output '{shown}'; assuming tmux 3.2 or newer"
    if version < FLOOR:
        raise TermctlError(
            "tmux_too_old",
            f"tmux {version.text} is too old; termctl needs tmux 3.2 or newer ({path})",
            details={"tmux_bin": path, "tmux_version": version.text},
        )
    return version, None


def probe(
    tmux_bin: str,
    environ: Mapping[str, str],
    gateway: Gateway,
    warn: Callable[[str], None],
    trace: Callable[[str], None] | None = None,
) -> TmuxBinary:
    """Resolve ``tmux_bin`` and check its version (the first two tmux checks of step 3)."""
    path = resolve(tmux_bin, environ, gateway)
    version, warning = check_version(path, gateway, trace)
    if warning is not None:
        warn(warning)
    return TmuxBinary(path, version)
