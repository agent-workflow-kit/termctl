"""The tmux argument builder, guarded writes, command chains and the tmux runner
(interface.md "tmux argument rules"; design.md D15, D24, D28).

Every argument termctl passes to tmux is built here, so the rules cannot be skipped:

1. **Separators.** The final ``;`` of every element becomes ``\\;`` (tmux turns it back into
   ``;``); only termctl's own separators between chained commands are a bare ``;``.
2. **Leading ``-``.** ``--`` goes immediately before a command's first positional argument.
3. **Format expansion.** Values of ``-n``, ``-c`` and ``-s`` and the ``rename-window`` name get
   every ``#`` doubled; ``#[`` there is refused (it is rejected as input earlier, exit 2).
4. **Literal positions.** argv, ``-e`` values and option values need only rule 1.
5. **Guarded writes.** ``guarded`` wraps commands in ``if-shell -F`` with each inner element
   single-quoted; ``termctl-nomatch`` on stdout means the guard did not match.

Targets are exact ids (``=S:``, ``@N``, ``%N``) built only by ``session``, ``window``, ``pane``
and ``pane_in_session``. Builder misuse is a programming error and raises ``ValueError``.

Binary resolution and the version check (#10), socket resolution and safety (#8), the filtered
environment (#15) and the pane reader (#11) are separate; ``Tmux`` takes their results.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Self

from .errors import TermctlError
from .gateway import Gateway

SEPARATOR = ";"
NOMATCH = "termctl-nomatch"
# Seconds; a tmux call normally takes ~10 ms (design.md "Live verification").
TMUX_TIMEOUT = 30.0

_COMMAND_NAME = re.compile(r"[a-z]+(-[a-z]+)*")
_FLAG = re.compile(r"-[A-Za-z]")
_FORMAT_EXPANDED_FLAGS = frozenset({"-n", "-c", "-s"})
# Flags whose values have their own, typed method.
_TYPED_FLAGS = frozenset({"-t", "-e", "-F"})
_PANE_ID = re.compile(r"%[0-9]+")
_WINDOW_ID = re.compile(r"@[0-9]+")
_SESSION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TERMCTL_ID = re.compile(r"tc-[a-z2-7]{8}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# --- the rules ----------------------------------------------------------------------------------


def escape_separator(element: str) -> str:
    """Rule 1: replace a final ``;`` with ``\\;``. tmux strips the ``;`` and turns a ``\\``
    then left at the end back into ``;``, so every string round-trips."""
    if element.endswith(SEPARATOR):
        return element[:-1] + "\\;"
    return element


def double_hashes(value: str) -> str:
    """Rule 3 for a format-expanded value: every ``#`` doubled, so tmux expands nothing.

    Doubling does not neutralize ``#[`` (spike C9), so such a value is refused; input grammars
    reject it earlier with exit 2.
    """
    if "#[" in value:
        raise ValueError("a format-expanded value must not contain '#['")
    return value.replace("#", "##")


def quote(element: str) -> str:
    """Single-quote an element for tmux's command parser, ``'`` written as ``'\\''``."""
    return "'" + element.replace("'", "'\\''") + "'"


def _check_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("a tmux argument must not contain NUL")
    return value


# --- typed pieces -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Format:
    """A tmux format string written by termctl itself (never caller or tmux data); passed
    verbatim. Values interpolated into one must come from a grammar without ``#``, ``,``
    and ``}``."""

    text: str

    def __post_init__(self) -> None:
        if _CONTROL.search(self.text) or self.text.endswith(SEPARATOR):
            raise ValueError(f"invalid format: {self.text!r}")


@dataclass(frozen=True)
class Target:
    """An exact tmux target; build it with ``pane``, ``window``, ``session`` or
    ``pane_in_session``."""

    text: str


def _fullmatch(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def pane(pane_id: str) -> Target:
    return Target(_fullmatch(_PANE_ID, pane_id, "pane id"))


def window(window_id: str) -> Target:
    return Target(_fullmatch(_WINDOW_ID, window_id, "window id"))


def session(name: str) -> Target:
    """``=S:``: an exact session match (``-t S`` would prefix-match, and ``=S`` without the
    colon fails for ``set``; spike C)."""
    return Target(f"={_fullmatch(_SESSION_NAME, name, 'session name')}:")


def pane_in_session(name: str, window_id: str, pane_id: str) -> Target:
    """``=S:@W.%P``: fails unless the pane is in that window and the window in that session
    (spike F7)."""
    _fullmatch(_SESSION_NAME, name, "session name")
    _fullmatch(_WINDOW_ID, window_id, "window id")
    _fullmatch(_PANE_ID, pane_id, "pane id")
    return Target(f"={name}:{window_id}.{pane_id}")


# --- one tmux command ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """One tmux command after rules 2 to 4.

    ``elements`` are the values tmux must receive; ``shown`` are the same with ``--env``
    values replaced by their names (``--verbose``, dry runs). ``argv`` applies rule 1 for
    passing as separate arguments; ``quoted`` single-quotes each element for a guard, where
    rule 1 does not apply.
    """

    elements: tuple[str, ...]
    shown: tuple[str, ...]
    guarded: bool = False

    @property
    def has_target(self) -> bool:
        """Whether ``-t`` is among the flags (before any positional)."""
        flags = (
            self.elements[: self.elements.index("--")] if "--" in self.elements else self.elements
        )
        return "-t" in flags

    @property
    def argv(self) -> tuple[str, ...]:
        return tuple(escape_separator(element) for element in self.elements)

    def quoted(self) -> str:
        return " ".join(quote(element) for element in self.elements)

    def quoted_shown(self) -> str:
        return " ".join(quote(element) for element in self.shown)


@dataclass
class CommandBuilder:
    """Builds one ``Command``; start with ``command(name)``. Flags and options come before
    positionals, so rule 2 can put ``--`` in front of the first positional."""

    _elements: list[str] = field(default_factory=list)
    _shown: list[str] = field(default_factory=list)
    _positional: bool = False

    def _add(self, element: str, shown: str | None = None) -> None:
        self._elements.append(_check_text(element))
        self._shown.append(element if shown is None else shown)

    def _before_positionals(self, flag: str) -> None:
        if self._positional:
            raise ValueError(f"{flag} after a positional argument")
        if not isinstance(flag, str) or _FLAG.fullmatch(flag) is None:
            raise ValueError(f"invalid flag: {flag!r}")

    def _positional_next(self) -> None:
        if not self._positional:
            self._add("--")
            self._positional = True

    # Flags and options.

    def flag(self, flag: str) -> Self:
        """A flag without a value, such as ``-d``."""
        self._before_positionals(flag)
        self._add(flag)
        return self

    def option(self, flag: str, value: str) -> Self:
        """A flag with a data value; ``-n``, ``-c`` and ``-s`` values are format-expanded by
        tmux, so their ``#`` are doubled (rule 3)."""
        self._before_positionals(flag)
        if flag in _TYPED_FLAGS:
            raise ValueError(f"{flag} has its own method")
        self._add(flag)
        self._add(double_hashes(value) if flag in _FORMAT_EXPANDED_FLAGS else value)
        return self

    def target(self, target: Target) -> Self:
        self._before_positionals("-t")
        if not isinstance(target, Target):
            raise TypeError("a target must be built with pane, window, session or pane_in_session")
        self._add("-t")
        self._add(target.text)
        return self

    def format(self, flag: str, fmt: Format) -> Self:
        """A flag whose value is termctl's own format, such as ``-F``."""
        self._before_positionals(flag)
        if not isinstance(fmt, Format):
            raise TypeError("a format must be a Format")
        self._add(flag)
        self._add(fmt.text)
        return self

    def env(self, name: str, value: str) -> Self:
        """``-e NAME=value``; shown as ``-e NAME``."""
        self._before_positionals("-e")
        _fullmatch(_ENV_NAME, name, "environment variable name")
        self._add("-e")
        self._add(f"{name}={value}", shown=name)
        return self

    # Positionals.

    def arg(self, value: str) -> Self:
        """A literal positional (rule 4), such as an argv element or an option value."""
        self._positional_next()
        self._add(value)
        return self

    def name(self, value: str) -> Self:
        """A format-expanded positional, the ``rename-window`` name (rule 3)."""
        self._positional_next()
        self._add(double_hashes(value))
        return self

    def format_arg(self, fmt: Format) -> Self:
        """termctl's own format as a positional, such as ``display -p`` output."""
        if not isinstance(fmt, Format):
            raise TypeError("a format must be a Format")
        self._positional_next()
        self._add(fmt.text)
        return self

    def env_arg(self, name: str, value: str) -> Self:
        """A ``NAME=value`` argv element for ``/usr/bin/env``; shown as ``NAME``."""
        _fullmatch(_ENV_NAME, name, "environment variable name")
        self._positional_next()
        self._add(f"{name}={value}", shown=name)
        return self

    def build(self) -> Command:
        return Command(tuple(self._elements), tuple(self._shown))


def command(name: str) -> CommandBuilder:
    """Start a tmux command, such as ``command("kill-pane")``."""
    builder = CommandBuilder()
    builder._add(_fullmatch(_COMMAND_NAME, name, "tmux command name"))
    return builder


# --- chains -------------------------------------------------------------------------------------


def _check_tmux_bin(tmux_bin: str) -> None:
    if not os.path.isabs(tmux_bin):
        raise ValueError(f"tmux_bin must be an absolute path: {tmux_bin!r}")


def _check_socket(socket_path: str) -> None:
    # Resolution and the full grammar are #8's; these are the properties tmux argv needs.
    if (
        not os.path.isabs(socket_path)
        or socket_path.endswith(SEPARATOR)
        or "," in socket_path
        or _CONTROL.search(socket_path)
    ):
        raise ValueError(f"invalid socket path: {socket_path!r}")


@dataclass(frozen=True)
class Invocation:
    """One tmux process: commands chained with bare ``;`` elements. tmux stops the chain at
    the first failing command; earlier commands are not rolled back (spike C)."""

    commands: tuple[Command, ...]

    @classmethod
    def of(cls, *commands: Command) -> Invocation:
        if not commands:
            raise ValueError("an invocation needs at least one command")
        return cls(commands)

    def _join(self, parts: list[tuple[str, ...]]) -> list[str]:
        joined: list[str] = []
        for index, part in enumerate(parts):
            if index:
                joined.append(SEPARATOR)
            joined.extend(part)
        return joined

    def argv(self, tmux_bin: str, socket_path: str) -> list[str]:
        """The argv to run: always the absolute binary and ``-S <socket_path>``, never ``-L``."""
        _check_tmux_bin(tmux_bin)
        _check_socket(socket_path)
        return [tmux_bin, "-S", socket_path, *self._join([c.argv for c in self.commands])]

    def shown_argv(self, tmux_bin: str, socket_path: str) -> list[str]:
        """``argv`` with ``--env`` values replaced by their names (``--verbose``, dry runs)."""
        _check_tmux_bin(tmux_bin)
        _check_socket(socket_path)
        shown = [tuple(escape_separator(e) for e in c.shown) for c in self.commands]
        return [tmux_bin, "-S", socket_path, *self._join(shown)]


# --- rule 5: guarded writes ---------------------------------------------------------------------


def _eq(left: str, right: str) -> str:
    return f"#{{==:{left},{right}}}"


def all_of(*conditions: Format) -> Format:
    """``#{&&:a,b}`` nested pairwise (tmux 3.2 takes two operands)."""
    if len(conditions) < 2:
        raise ValueError("all_of needs at least two conditions")
    text = conditions[-1].text
    for condition in reversed(conditions[:-1]):
        text = f"#{{&&:{condition.text},{text}}}"
    return Format(text)


def owned(termctl_id: str) -> Format:
    """The pane's own ``@termctl_pane`` names it and it carries ``termctl_id`` (rule 5)."""
    _fullmatch(_TERMCTL_ID, termctl_id, "termctl id")
    return all_of(
        Format(_eq("#{@termctl_pane}", "#{pane_id}")),
        Format(_eq("#{@termctl_id}", termctl_id)),
    )


def owned_single_pane_window(termctl_id: str) -> Format:
    """``owned`` plus: still in the window it was opened in, and alone there (``rename-window``
    guards; interface.md "Names")."""
    _fullmatch(_TERMCTL_ID, termctl_id, "termctl id")
    return all_of(
        Format(_eq("#{@termctl_pane}", "#{pane_id}")),
        Format(_eq("#{@termctl_id}", termctl_id)),
        Format(_eq("#{window_id}", "#{@termctl_window}")),
        Format(_eq("#{window_panes}", "1")),
    )


def untagged_in_window(window_id: str) -> Format:
    """Empty ``@termctl_pane`` and in ``window_id`` (the tagging guard, Open order step 6)."""
    _fullmatch(_WINDOW_ID, window_id, "window id")
    return all_of(Format(_eq("#{@termctl_pane}", "")), Format(_eq("#{window_id}", window_id)))


def cleanup(window_id: str) -> Format:
    """In ``window_id`` and ``@termctl_pane`` empty or its own id (the kill after a failed
    tagging, Open order)."""
    _fullmatch(_WINDOW_ID, window_id, "window id")
    either = f"#{{||:{_eq('#{@termctl_pane}', '')},{_eq('#{@termctl_pane}', '#{pane_id}')}}}"
    return all_of(Format(_eq("#{window_id}", window_id)), Format(either))


_NOMATCH_COMMAND = command("display").flag("-p").arg(NOMATCH).build()


def guarded(target: Target, condition: Format, *commands: Command) -> Command:
    """``if-shell -F -t <target> -- <condition> '<commands>' 'display -p termctl-nomatch'``.

    The inner commands run only while ``condition`` holds for ``target``, in the same tmux
    command; a missing target evaluates the condition as false (spike F5). Each inner command
    must name its own target with ``-t`` (without one it would act on tmux's default target).
    Inner elements are single-quoted, where rule 1 does not apply; several inner commands are
    joined by `` ; ``.
    """
    if not commands:
        raise ValueError("a guard needs at least one command")
    if any(c.guarded for c in commands):
        raise ValueError("guards do not nest")
    if not all(c.has_target for c in commands):
        raise ValueError("a guarded command must name its target with -t")
    if not isinstance(target, Target) or not isinstance(condition, Format):
        raise TypeError("a guard takes a Target and a Format")
    inner = f" {SEPARATOR} ".join(c.quoted() for c in commands)
    inner_shown = f" {SEPARATOR} ".join(c.quoted_shown() for c in commands)
    built = command("if-shell").flag("-F").target(target).format_arg(condition)
    built.arg(inner).arg(_NOMATCH_COMMAND.quoted())
    plain = built.build()
    shown = (*plain.shown[:-2], inner_shown, plain.shown[-1])
    return Command(plain.elements, shown, guarded=True)


def guard_matched(stdout: str) -> bool:
    """Whether a guarded invocation's guard matched: no stdout line is ``termctl-nomatch``.

    Any such line counts as a non-match, so output that merely looks like one fails closed.
    """
    return NOMATCH not in stdout.splitlines()


def check_guard(stdout: str, termctl_id: str) -> None:
    """Raise ``not_found`` (exit 10) when the guard did not match; ``close`` instead treats
    that as already closed and calls ``guard_matched`` itself."""
    if not guard_matched(stdout):
        raise TermctlError(
            "not_found", f"managed pane {termctl_id} is gone or no longer the one resolved"
        )


# --- the runner ---------------------------------------------------------------------------------


class Tmux:
    """Runs invocations against one server through the gateway.

    ``tmux_bin`` is the absolute path resolved once per invocation (#10); ``socket_path`` the
    resolved, checked socket (#8); ``env`` the environment filtered by ``server_env_allow``
    (#15). ``trace`` receives each command as shown under ``--verbose``.
    """

    def __init__(
        self,
        gateway: Gateway,
        tmux_bin: str,
        socket_path: str,
        *,
        env: Mapping[str, str],
        trace: Callable[[str], None] | None = None,
    ) -> None:
        _check_tmux_bin(tmux_bin)
        _check_socket(socket_path)
        self.gateway = gateway
        self.tmux_bin = tmux_bin
        self.socket_path = socket_path
        self.env = dict(env)
        self.trace = trace

    def run(self, invocation: Invocation) -> subprocess.CompletedProcess[str]:
        """Run ``invocation`` and return tmux's result; mapping tmux's failures to exit codes
        is the caller's, since it depends on the command."""
        argv = invocation.argv(self.tmux_bin, self.socket_path)
        if self.trace is not None:
            self.trace(
                "tmux: " + shlex.join(invocation.shown_argv(self.tmux_bin, self.socket_path))
            )
        try:
            return self.gateway.run(argv, timeout=TMUX_TIMEOUT, env=self.env)
        except subprocess.TimeoutExpired:
            raise TermctlError(
                "server_unavailable", f"tmux did not answer within {TMUX_TIMEOUT:g} seconds"
            ) from None
        except OSError as exc:
            raise TermctlError(
                "tmux_missing", f"cannot run {self.tmux_bin}: {exc.strerror or exc}"
            ) from None
