"""The tmux argument builder, guarded writes, command chains and the tmux runner
(interface.md "tmux argument rules"; design.md D15, D24, D28).

Every argument termctl passes to tmux is built here, so the rules cannot be skipped:

1. **Separators.** The final ``;`` of every element becomes ``\\;`` (tmux turns it back into
   ``;``); only termctl's own separators between chained commands are a bare ``;``.
2. **Leading ``-``.** ``--`` goes immediately before a command's first positional argument.
   ``option`` takes only a flag that takes a value for that command, never a value starting
   with ``-``; ``window_name`` (``-n``), ``session_name`` (``-s``) and ``client``
   (``switch-client -c``) take names that may.
3. **Format expansion.** Values of ``-n``, ``-c`` and ``-s`` and the ``rename-window`` name get
   every ``#`` doubled; ``#[`` there is refused (it is rejected as input earlier, exit 2).
   ``display`` takes only termctl's own ``Format`` as its positional, and ``-F`` and ``-f``
   only a ``Format``.
4. **Literal positions.** argv, ``-e`` values and option values need only rule 1.
5. **Guarded writes.** ``guarded`` wraps commands in ``if-shell -F -t %N``; each inner command
   is one of the guarded mutations (or ``display -p`` of ids and numbers), sets only the
   options the spec sets there, targets that pane or, for ``rename-window`` and ``set -w``,
   the window containing it, and its elements are quoted by ``quote``. ``guard_outcome``
   reads the result.

Targets are exact ids (``=S:``, ``@N``, ``%N``, ``=$N:@W.%P``); ``Target`` checks that grammar
however it is built. ``Format`` values come only from this module's factories. Value-taking
flags (``-t``, ``-e``, ``-F``, ``-f``, ``-n``, ``-c``, ``-s`` everywhere, and per command
those such as ``display -d``, ``select-pane -T`` and ``new-session -x``) are refused as bare
flags.
Builder misuse is a programming error and raises ``ValueError`` or ``TypeError``.

Binary resolution and the version check (#10), socket resolution and safety (#8), the filtered
environment (#15) and the pane reader (``panes.py``) are separate; ``Tmux`` takes their
results. ``record`` builds the reader's ``list-panes -F`` format.
"""

from __future__ import annotations

import enum
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, NamedTuple, Self

from .errors import TermctlError
from .gateway import Gateway
from .validate import is_client

SEPARATOR = ";"
NOMATCH = "termctl-nomatch"
# Seconds; a tmux call normally takes ~10 ms (design.md "Live verification").
TMUX_TIMEOUT = 30.0

_COMMAND_NAME = re.compile(r"[a-z]+(-[a-z]+)*")
_FLAG = re.compile(r"-[A-Za-z]")
_FORMAT_EXPANDED_FLAGS = frozenset({"-n", "-c", "-s"})
# Flags whose values have their own, typed method.
_TYPED_FLAGS = frozenset({"-t", "-e", "-F", "-f", "-n", "-s"})
# Flags that take a value: refused by flag(), where they would take the next element as theirs
# or (set -F) switch on format expansion of the value. These letters take a value, or are
# refused as bare flags for other reasons (-s, see server_scope), on every command.
_VALUE_FLAGS = _TYPED_FLAGS | _FORMAT_EXPANDED_FLAGS
# Letters that take a value only on some commands (tmux 3.2a to 3.5a list-commands, for every
# command of interface.md and design.md; review round 5, R5-3); elsewhere they are plain flags
# (switch-client -E, select-window -T, attach -x, new-window -b and -S).
_COMMAND_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "display": frozenset({"-d"}),
    "display-message": frozenset({"-d"}),
    "select-pane": frozenset({"-T"}),
    "switch-client": frozenset({"-T"}),
    "new-session": frozenset({"-x", "-y"}),
    "join-pane": frozenset({"-l"}),
    "capture-pane": frozenset({"-b", "-E", "-S"}),
}
# The flags option() takes, per command: each takes a value in tmux, so the next element is
# always that value (review round 4, R4-1). A flag that takes none would leave its "value" to be
# parsed as further flags (set -w -g). Values starting with "-" are refused besides; -c here is
# a directory, which is absolute. switch-client's -c is a client name, which may start with
# "-", and has its own method, client() (review round 5, R5-2).
_OPTION_FLAGS: dict[str, frozenset[str]] = {
    "new-window": frozenset({"-c"}),
    "new-session": frozenset({"-c"}),
    "respawn-pane": frozenset({"-c"}),
}
# window_name() and session_name(): -n and -s on the commands where they take a value. These
# values may start with "-" (a name "-x"); tmux takes the element after them as the value
# whatever it starts with (spike E).
_WINDOW_NAME_COMMANDS = frozenset({"new-window", "new-session"})
_SESSION_NAME_COMMANDS = frozenset({"new-session"})
_FORMAT_FLAGS = frozenset({"-F", "-f"})
# Commands whose positional is a format: they take only termctl's own Format.
_FORMAT_POSITIONAL_COMMANDS = frozenset({"display", "display-message"})
_SERVER_SCOPE_COMMANDS = frozenset({"set-option", "set", "show-options", "show"})
_PANE_ID = re.compile(r"%[0-9]+")
_WINDOW_ID = re.compile(r"@[0-9]+")
_SESSION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_FIELD_NAME = re.compile(r"@?[a-z][a-z0-9_]*")
_TARGET_GRAMMARS: dict[str, re.Pattern[str]] = {
    "pane": _PANE_ID,
    "window": _WINDOW_ID,
    "session": re.compile(r"=[A-Za-z0-9_-]{1,64}:"),
    "pane_in_session": re.compile(r"=\$[0-9]+:@[0-9]+\.%[0-9]+"),
}
_SESSION_ID = re.compile(r"\$[0-9]+")
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


# Characters quote() writes outside the quotes, as escapes tmux's parser turns back into them.
# "\udcff" is how os.fsdecode carries byte 0xff, which ends tmux's parse even inside quotes.
_QUOTE_ESCAPES = {"\n": "\\n", "\udcff": "\\377"}
_QUOTE_SPLIT = re.compile("([\n\udcff])")


def quote(element: str) -> str:
    """Quote an element for tmux's command parser so it round-trips exactly (spike F5).

    Each run without a newline or byte 0xff is single-quoted with ``'`` written as ``'\\''``,
    and the runs are joined by ``\\n`` or ``\\377`` outside the quotes (``"a\\nb"`` becomes
    ``'a'\\n'b'``). tmux's parser drops a backslash-newline pair anywhere, and blanks and ``#``
    comments after a newline even inside quotes, so a raw newline must never reach it; it treats
    byte 0xff (``"\\udcff"`` after ``os.fsdecode``) as the end of its input, even inside quotes,
    so the rest would run as commands (review round 2). Outside quotes ``\\n`` is a newline and
    ``\\377`` that byte. Every byte 0x01-0xff round-trips this way on tmux 3.5a and 3.2a.
    """
    parts = _QUOTE_SPLIT.split(element)
    return "".join(
        _QUOTE_ESCAPES[part] if index % 2 else "'" + part.replace("'", "'\\''") + "'"
        for index, part in enumerate(parts)
    )


# A lone surrogate outside \udc80-\udcff (where os.fsdecode puts undecodable bytes) cannot be
# encoded for a subprocess argument.
_LONE_SURROGATE = re.compile(r"[\ud800-\udc7f\udd00-\udfff]")


def _check_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("a tmux argument must not contain NUL")
    if _LONE_SURROGATE.search(value):
        raise ValueError("a tmux argument must not contain a lone surrogate")
    return value


# --- typed pieces -------------------------------------------------------------------------------


_FORMAT_KEY = object()


@dataclass(frozen=True)
class Format:
    """A tmux format string written by termctl itself (never caller or tmux data); passed
    verbatim. Only this module's factories (``fields``, the guard conditions) build one.
    Values interpolated into one must come from a grammar without ``#``, ``,`` and ``}``."""

    text: str
    # Not an __init__ argument, so dataclasses.replace cannot copy it: every construction,
    # replace() included, gets None and is refused; only _format sets the key.
    _key: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._key is not _FORMAT_KEY:
            raise TypeError("a Format is built only by the tmux_backend factories")


def _format(text: str) -> Format:
    if not isinstance(text, str) or _CONTROL.search(text) or text.endswith(SEPARATOR):
        raise ValueError(f"invalid format: {text!r}")
    return _new_format(text)


def _new_format(text: str) -> Format:
    fmt = object.__new__(Format)
    object.__setattr__(fmt, "text", text)
    object.__setattr__(fmt, "_key", _FORMAT_KEY)
    return fmt


def fields(*names: str) -> Format:
    """``#{a} #{b}``: tmux fields or user options by name, separated by spaces."""
    if not names:
        raise ValueError("fields needs at least one name")
    return _format(" ".join(f"#{{{_fullmatch(_FIELD_NAME, n, 'field name')}}}" for n in names))


# tmux's regex substitution (#{s/re/replacement/:field}) with a literal tab and newline in the
# bracket: POSIX ERE has no "\t" escape, and a ":" (as in "[[:space:]]") ends the modifier.
_SANITIZE = "s/[\t\n]/ /:"
_RECORD_SEPARATOR = "\t"
_SENTINEL = re.compile(r"[A-Za-z0-9-]{16,64}")


def record(names: Sequence[str], sanitized: Collection[str], sentinel: str) -> Format:
    """``#{a}<TAB>#{b}...<TAB><sentinel>``: one ``list-panes -F`` record per line (design.md
    "Internal architecture", data flow).

    Fields named in ``sanitized`` (free text, such as ``window_name``) are read with every tab
    and newline replaced by a space, so they cannot break the record; tmux itself escapes the
    other control characters on output. Every other field is read as is: a tab or newline in
    one (only a value someone else wrote can hold one) breaks its record, which the reader
    detects. The sentinel ends each record; it is a fresh random token per read, so no value
    can forge the end of a record. It does not protect against server hooks or commands run by
    someone with socket access: an ``after-list-panes`` hook sees the format, sentinel
    included, and can print a forged record.
    """
    names = tuple(names)
    if not names:
        raise ValueError("a record needs at least one field")
    unknown = set(sanitized) - set(names)
    if unknown:
        raise ValueError(f"sanitized fields not in the record: {sorted(unknown)!r}")
    for name in names:
        _fullmatch(_FIELD_NAME, name, "field name")
    pieces = [f"#{{{_SANITIZE if name in sanitized else ''}{name}}}" for name in names]
    pieces.append(_fullmatch(_SENTINEL, sentinel, "record sentinel"))
    # The pieces are checked above; the separator and _SANITIZE hold the only control
    # characters, which a -F value passed as one argv element carries to tmux unchanged.
    return _new_format(_RECORD_SEPARATOR.join(pieces))


TargetKind = Literal["pane", "window", "session", "pane_in_session"]


@dataclass(frozen=True)
class Target:
    """An exact tmux target: ``%N``, ``@N``, ``=S:`` or ``=$N:@W.%P``. Build it with ``pane``,
    ``window``, ``session`` or ``pane_in_session``; any other text is refused."""

    text: str

    def __post_init__(self) -> None:
        self.kind  # noqa: B018 (checks the grammar)

    @property
    def kind(self) -> TargetKind:
        if isinstance(self.text, str):
            for kind, grammar in _TARGET_GRAMMARS.items():
                if grammar.fullmatch(self.text):
                    return kind  # type: ignore[return-value]
        raise ValueError(f"invalid tmux target: {self.text!r}")


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


def pane_in_session(session_id: str, window_id: str, pane_id: str) -> Target:
    """``=$N:@W.%P``: fails unless the pane is in that window and the window in that session
    (spike F7).

    The session is named by its id, not its name: a session a user renamed may have any name
    (``my work``, one with characters tmux prints escaped, or ``$9``, which ``=$9`` would look
    up as a session id), while the id is tmux's own and still enforces containment (#24;
    checked on tmux 3.2a and 3.5a).

    TODO(#25): the ``=$N:`` form on tmux 3.2 beyond that containment check is #25's."""
    _fullmatch(_SESSION_ID, session_id, "session id")
    _fullmatch(_WINDOW_ID, window_id, "window id")
    _fullmatch(_PANE_ID, pane_id, "pane id")
    return Target(f"={session_id}:{window_id}.{pane_id}")


# --- one tmux command ---------------------------------------------------------------------------


_COMMAND_KEY = object()


@dataclass(frozen=True)
class Command:
    """One tmux command after rules 2 to 4. Only ``CommandBuilder.build`` and ``guarded``
    make one, since ``guarded`` trusts its ``target`` and ``flags``.

    ``elements`` are the values tmux must receive; ``shown`` are the same with ``--env``
    values replaced by their names (``--verbose``, dry runs). ``argv`` applies rule 1 for
    passing as separate arguments; ``quoted`` single-quotes each element for a guard, where
    rule 1 does not apply.
    """

    elements: tuple[str, ...]
    shown: tuple[str, ...]
    guarded: bool = False
    # The target given to ``CommandBuilder.target``; ``None`` for a command without one.
    target: Target | None = None
    # The flags given to the builder, in order (``-t`` included), without their values.
    flags: tuple[str, ...] = ()
    # The positional arguments as passed to tmux (after rule 3), without the "--".
    positionals: tuple[str, ...] = ()
    # As for Format: not an __init__ argument, so direct construction and dataclasses.replace
    # get None and are refused; only _command sets the key (review round 3, R3-3).
    _key: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._key is not _COMMAND_KEY:
            raise TypeError("a Command is built only by CommandBuilder.build or guarded")

    @property
    def name(self) -> str:
        return self.elements[0] if self.elements else ""

    @property
    def argv(self) -> tuple[str, ...]:
        return tuple(escape_separator(element) for element in self.elements)

    def quoted(self) -> str:
        return " ".join(quote(element) for element in self.elements)

    def quoted_shown(self) -> str:
        return " ".join(quote(element) for element in self.shown)


def _command(
    elements: tuple[str, ...],
    shown: tuple[str, ...],
    *,
    guarded: bool = False,
    target: Target | None,
    flags: tuple[str, ...],
    positionals: tuple[str, ...],
) -> Command:
    cmd = object.__new__(Command)
    for name, value in (
        ("elements", elements),
        ("shown", shown),
        ("guarded", guarded),
        ("target", target),
        ("flags", flags),
        ("positionals", positionals),
        ("_key", _COMMAND_KEY),
    ):
        object.__setattr__(cmd, name, value)
    return cmd


_BUILDER_KEY = object()


@dataclass
class CommandBuilder:
    """Builds one ``Command``; start with ``command(name)``, the only entry point (its fields
    are not constructor arguments, so a builder cannot start with forged state; review round 4,
    R4-2). Flags and options come before positionals, so rule 2 can put ``--`` in front of the
    first positional."""

    _elements: list[str] = field(default_factory=list, init=False)
    _shown: list[str] = field(default_factory=list, init=False)
    _positional: bool = field(default=False, init=False)
    _target: Target | None = field(default=None, init=False)
    _flags: list[str] = field(default_factory=list, init=False)
    _positionals: list[str] = field(default_factory=list, init=False)
    _key: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._key is not _BUILDER_KEY:
            raise TypeError("a CommandBuilder is started only by command()")

    def _add(self, element: str, shown: str | None = None) -> None:
        self._elements.append(_check_text(element))
        self._shown.append(element if shown is None else shown)

    def _add_flag(self, flag: str) -> None:
        self._add(flag)
        self._flags.append(flag)

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

    @property
    def _name(self) -> str:
        return self._elements[0]

    def flag(self, flag: str) -> Self:
        """A flag without a value, such as ``-d``. Flags that take a value on this command
        (``_VALUE_FLAGS``, ``_COMMAND_VALUE_FLAGS``) are refused."""
        self._before_positionals(flag)
        if flag in _VALUE_FLAGS or flag in _COMMAND_VALUE_FLAGS.get(self._name, frozenset()):
            raise ValueError(f"{self._name} {flag} takes a value; use a method that gives one")
        self._add_flag(flag)
        return self

    def server_scope(self) -> Self:
        """``-s`` of ``set-option``/``show-options``: the server option table."""
        self._before_positionals("-s")
        if self._name not in _SERVER_SCOPE_COMMANDS:
            raise ValueError(f"{self._name} has no server scope flag")
        self._add_flag("-s")
        return self

    def option(self, flag: str, value: str) -> Self:
        """A flag with a data value, only one this command takes a value for
        (``_OPTION_FLAGS``); a value starting with ``-`` is refused. ``-c`` is format-expanded
        by tmux, so its ``#`` are doubled (rule 3)."""
        self._before_positionals(flag)
        if flag in _TYPED_FLAGS:
            raise ValueError(f"{flag} has its own method")
        if flag not in _OPTION_FLAGS.get(self._name, frozenset()):
            raise ValueError(f"{self._name} {flag} takes no value through option")
        if not isinstance(value, str) or value.startswith("-"):
            raise ValueError(f"an option value must not look like a flag: {value!r}")
        self._add_flag(flag)
        self._add(double_hashes(value) if flag in _FORMAT_EXPANDED_FLAGS else value)
        return self

    def client(self, name: str) -> Self:
        """``-c CLIENT`` of ``switch-client``: a name from the ``--client`` grammar
        (interface.md ``focus``), which may start with ``-``; tmux takes the element after
        ``-c`` as the client name whatever it starts with. The grammar has no ``#`` or ``;``,
        so rules 1 and 3 change nothing."""
        self._before_positionals("-c")
        if self._name != "switch-client":
            raise ValueError(f"{self._name} takes no client")
        if not isinstance(name, str) or not is_client(name):
            raise ValueError(f"invalid client name: {name!r}")
        self._add_flag("-c")
        self._add(name)
        return self

    def window_name(self, value: str) -> Self:
        """``-n NAME`` of ``new-window`` and ``new-session``: format-expanded, so its ``#`` are
        doubled (rule 3). The name may start with ``-``; tmux takes it as the value."""
        self._before_positionals("-n")
        if self._name not in _WINDOW_NAME_COMMANDS:
            raise ValueError(f"{self._name} takes no window name")
        self._add_flag("-n")
        self._add(double_hashes(value))
        return self

    def session_name(self, name: str) -> Self:
        """``-s NAME`` of ``new-session``: a name from the session grammar (no ``#``)."""
        self._before_positionals("-s")
        if self._name not in _SESSION_NAME_COMMANDS:
            raise ValueError(f"{self._name} takes no session name")
        self._add_flag("-s")
        self._add(_fullmatch(_SESSION_NAME, name, "session name"))
        return self

    def target(self, target: Target) -> Self:
        """``-t``; a command takes one target, recorded on the built ``Command``."""
        self._before_positionals("-t")
        if not isinstance(target, Target):
            raise TypeError("a target must be built with pane, window, session or pane_in_session")
        if self._target is not None:
            raise ValueError("a command takes one target")
        self._target = target
        self._add_flag("-t")
        self._add(target.text)
        return self

    def format(self, flag: str, fmt: Format) -> Self:
        """``-F`` or ``-f`` with termctl's own format."""
        self._before_positionals(flag)
        if flag not in _FORMAT_FLAGS:
            raise ValueError(f"{flag} does not take a format")
        if not isinstance(fmt, Format):
            raise TypeError("a format must be a Format")
        self._add_flag(flag)
        self._add(fmt.text)
        return self

    def env(self, name: str, value: str) -> Self:
        """``-e NAME=value``; shown as ``-e NAME``."""
        self._before_positionals("-e")
        _fullmatch(_ENV_NAME, name, "environment variable name")
        self._add_flag("-e")
        self._add(f"{name}={value}", shown=name)
        return self

    # Positionals.

    def arg(self, value: str) -> Self:
        """A literal positional (rule 4), such as an argv element or an option value. The
        ``rename-window`` name is format-expanded, so it gets rule 3 as in ``name``; a
        ``display`` positional is a format and takes only ``format_arg``."""
        if self._name == "rename-window":
            return self.name(value)
        if self._name in _FORMAT_POSITIONAL_COMMANDS:
            raise ValueError(f"the {self._name} positional is a format; use format_arg")
        self._add_positional(value)
        return self

    def name(self, value: str) -> Self:
        """A format-expanded positional, the ``rename-window`` name (rule 3)."""
        self._add_positional(double_hashes(value))
        return self

    def format_arg(self, fmt: Format) -> Self:
        """termctl's own format as a positional, such as ``display -p`` output."""
        if not isinstance(fmt, Format):
            raise TypeError("a format must be a Format")
        self._add_positional(fmt.text)
        return self

    def env_arg(self, name: str, value: str) -> Self:
        """A ``NAME=value`` argv element for ``/usr/bin/env``; shown as ``NAME``."""
        _fullmatch(_ENV_NAME, name, "environment variable name")
        self._add_positional(f"{name}={value}", shown=name)
        return self

    def _add_positional(self, element: str, shown: str | None = None) -> None:
        self._positional_next()
        self._add(element, shown)
        self._positionals.append(element)

    def build(self) -> Command:
        return _command(
            tuple(self._elements),
            tuple(self._shown),
            target=self._target,
            flags=tuple(self._flags),
            positionals=tuple(self._positionals),
        )


def command(name: str) -> CommandBuilder:
    """Start a tmux command, such as ``command("kill-pane")``."""
    builder = object.__new__(CommandBuilder)
    for attr, value in (
        ("_elements", []),
        ("_shown", []),
        ("_positional", False),
        ("_target", None),
        ("_flags", []),
        ("_positionals", []),
        ("_key", _BUILDER_KEY),
    ):
        object.__setattr__(builder, attr, value)
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

    def argv(self, tmux_bin: str, socket_path: str, *, utf8: bool = False) -> list[str]:
        """The argv to run: always the absolute binary and ``-S <socket_path>``, never ``-L``.

        ``utf8`` adds ``-u``: tmux then writes UTF-8 output whatever the locale of the
        environment it runs with; without it, a client whose locale is not UTF-8 gets ``_`` for
        every non-ASCII character, tab and newline (the pane reader's case)."""
        _check_tmux_bin(tmux_bin)
        _check_socket(socket_path)
        return [
            *_client(tmux_bin, socket_path, utf8),
            *self._join([c.argv for c in self.commands]),
        ]

    def shown_argv(self, tmux_bin: str, socket_path: str, *, utf8: bool = False) -> list[str]:
        """``argv`` with ``--env`` values replaced by their names (``--verbose``, dry runs)."""
        _check_tmux_bin(tmux_bin)
        _check_socket(socket_path)
        shown = [tuple(escape_separator(e) for e in c.shown) for c in self.commands]
        return [*_client(tmux_bin, socket_path, utf8), *self._join(shown)]


def _client(tmux_bin: str, socket_path: str, utf8: bool) -> list[str]:
    return [tmux_bin, *(["-u"] if utf8 else []), "-S", socket_path]


# --- rule 5: guarded writes ---------------------------------------------------------------------


def _eq(left: str, right: str) -> str:
    return f"#{{==:{left},{right}}}"


def all_of(*conditions: Format) -> Format:
    """``#{&&:a,b}`` nested pairwise (tmux 3.2 takes two operands)."""
    if len(conditions) < 2:
        raise ValueError("all_of needs at least two conditions")
    if not all(isinstance(c, Format) for c in conditions):
        raise TypeError("all_of takes Formats")
    text = conditions[-1].text
    for condition in reversed(conditions[:-1]):
        text = f"#{{&&:{condition.text},{text}}}"
    return _format(text)


def in_window(window_id: str) -> Format:
    """The pane is in ``window_id``."""
    return _format(_eq("#{window_id}", _fullmatch(_WINDOW_ID, window_id, "window id")))


def owned(termctl_id: str) -> Format:
    """The pane's own ``@termctl_pane`` names it and it carries ``termctl_id`` (rule 5)."""
    _fullmatch(_TERMCTL_ID, termctl_id, "termctl id")
    return all_of(
        _format(_eq("#{@termctl_pane}", "#{pane_id}")),
        _format(_eq("#{@termctl_id}", termctl_id)),
    )


def owned_single_pane_window(termctl_id: str) -> Format:
    """``owned`` plus: still in the window it was opened in, and alone there (``rename-window``
    guards; interface.md "Names")."""
    _fullmatch(_TERMCTL_ID, termctl_id, "termctl id")
    return all_of(
        _format(_eq("#{@termctl_pane}", "#{pane_id}")),
        _format(_eq("#{@termctl_id}", termctl_id)),
        _format(_eq("#{window_id}", "#{@termctl_window}")),
        _format(_eq("#{window_panes}", "1")),
    )


def untagged_in_window(window_id: str) -> Format:
    """Empty ``@termctl_pane`` and in ``window_id`` (the tagging guard, Open order step 6)."""
    _fullmatch(_WINDOW_ID, window_id, "window id")
    return all_of(_format(_eq("#{@termctl_pane}", "")), in_window(window_id))


def cleanup(window_id: str) -> Format:
    """In ``window_id`` and ``@termctl_pane`` empty or its own id (the kill after a failed
    tagging, Open order)."""
    _fullmatch(_WINDOW_ID, window_id, "window id")
    either = f"#{{||:{_eq('#{@termctl_pane}', '')},{_eq('#{@termctl_pane}', '#{pane_id}')}}}"
    return all_of(in_window(window_id), _format(either))


_NOMATCH_COMMAND = command("display").flag("-p").format_arg(_format(NOMATCH)).build()


# The commands a guard may run and the flags each may take (interface.md rule 5): the
# mutations of a managed pane, and display -p for Open order step 7 and close --force.
# Anything else, such as -a (all other panes), -s (a source pane, or server options), -g or
# -U, could act outside the pane. set takes -p, or -w for the window options of Open order step 6.
_GUARDED_FLAGS: dict[str, frozenset[str]] = {
    "kill-pane": frozenset({"-t"}),
    "rename-window": frozenset({"-t"}),
    "set-option": frozenset({"-p", "-w", "-t", "-u", "-q", "-o"}),
    "set": frozenset({"-p", "-w", "-t", "-u", "-q", "-o"}),
    "respawn-pane": frozenset({"-k", "-t", "-c", "-e"}),
    "display": frozenset({"-p", "-t"}),
    "display-message": frozenset({"-p", "-t"}),
}


# The options a guard may set (interface.md rule 5): the pane options of "Pane options" and
# remain-on-exit with set -p; the window options of Open order step 6 with set -w (review
# round 4, R4-3).
_GUARDED_PANE_OPTIONS = frozenset(
    {
        "@termctl_v",
        "@termctl_pane",
        "@termctl_window",
        "@termctl_id",
        "@termctl_name",
        "@termctl_marker",
        "@termctl_marker_text",
        "@termctl_labels",
        "@termctl_placement",
        "@termctl_keep",
        "@termctl_created",
        "@termctl_pid",
        "@termctl_pid_start",
        "remain-on-exit",
    }
)
_GUARDED_WINDOW_OPTIONS = frozenset({"automatic-rename", "allow-rename"})
# The fields a guarded display may print: tmux ids and numbers, never caller or tmux-controlled
# text, so no output can read as termctl-nomatch (review round 4, R4-4).
_GUARDED_DISPLAY_FIELDS = ("pane_pid", "pane_dead", "pid", "pane_id", "window_id")
_GUARDED_DISPLAY_FIELD = "#{(?:" + "|".join(_GUARDED_DISPLAY_FIELDS) + ")}"
_GUARDED_DISPLAY_FORMAT = re.compile(f"{_GUARDED_DISPLAY_FIELD}( {_GUARDED_DISPLAY_FIELD})*")
# The constant line the tagging invocation of Open order step 6 ends with: the only fixed
# text a guarded display may print besides those fields (review round 2 of #17, R2-1).
TAG_CONFIRMATION = "termctl-tagged"


def _check_guarded_arguments(inner: Command) -> None:
    """Refuse a guarded set of an option the spec does not set there, and a guarded display
    that could print anything but ids and numbers."""
    if inner.name in ("set-option", "set"):
        allowed = _GUARDED_WINDOW_OPTIONS if "-w" in inner.flags else _GUARDED_PANE_OPTIONS
        if not inner.positionals or inner.positionals[0] not in allowed:
            option = inner.positionals[0] if inner.positionals else "no option"
            raise ValueError(f"{inner.name} cannot set {option} inside a guard")
    if inner.name in _FORMAT_POSITIONAL_COMMANDS:
        if "-p" not in inner.flags:
            raise ValueError(f"{inner.name} inside a guard takes -p")
        if len(inner.positionals) != 1 or not (
            _GUARDED_DISPLAY_FORMAT.fullmatch(inner.positionals[0])
            or inner.positionals[0] == TAG_CONFIRMATION
        ):
            raise ValueError(
                f"{inner.name} inside a guard prints only {', '.join(_GUARDED_DISPLAY_FIELDS)}"
                f" or {TAG_CONFIRMATION}"
            )


def _check_guarded_target(guard: Target, inner: Command) -> Target | None:
    """Refuse an inner command that could act outside the guarded pane; return a window target
    whose containment the condition must check."""
    if inner.guarded or inner.name == "if-shell":
        raise ValueError("guards do not nest")
    allowed = _GUARDED_FLAGS.get(inner.name)
    if allowed is None:
        raise ValueError(f"{inner.name or 'an empty command'} cannot run inside a guard")
    refused = [f for f in inner.flags if f not in allowed]
    if refused:
        raise ValueError(f"{inner.name} inside a guard cannot take {' '.join(refused)}")
    window_scoped = inner.name == "rename-window"
    if inner.name in ("set-option", "set"):
        if ("-p" in inner.flags) == ("-w" in inner.flags):
            raise ValueError(f"{inner.name} inside a guard takes exactly one of -p and -w")
        window_scoped = "-w" in inner.flags
    _check_guarded_arguments(inner)
    if inner.target is None:
        raise ValueError("a guarded command must name its target with target()")
    if inner.target.kind == "window":
        if not window_scoped:
            raise ValueError("only rename-window and set -w take a window target inside a guard")
        return inner.target
    if inner.target != guard:
        raise ValueError(
            f"a guarded command must target the guarded pane {guard.text} or its window,"
            f" not {inner.target.text}"
        )
    return None


def guarded(target: Target, condition: Format, *commands: Command) -> Command:
    """``if-shell -F -t %N -- <condition> '<commands>' 'display -p termctl-nomatch'``.

    The inner commands run only while ``condition`` holds for pane ``target``, in the same tmux
    command; a missing pane evaluates the condition as false (spike F5). Inner commands are
    limited to ``kill-pane``, ``rename-window``, ``set -p`` and ``set -w`` of the options the
    spec sets (``_GUARDED_PANE_OPTIONS``, ``_GUARDED_WINDOW_OPTIONS``), ``respawn-pane`` and
    ``display -p`` of ``_GUARDED_DISPLAY_FIELDS``, each with only the flags it needs
    (``_GUARDED_FLAGS``; never ``-a``, ``-s``, ``-g`` or ``-U``). Each targets that same pane
    (``set -w -t %N`` sets the options of the pane's own window), or for ``rename-window`` and
    ``set -w`` only a window (``-t @W``); for a window target the condition also requires the
    pane to be in that window, so no inner command can act outside the guarded pane or its
    window. Inner elements are quoted by ``quote``, where rule 1 does not apply; several inner
    commands are joined by `` ; ``.
    """
    if not isinstance(target, Target) or not isinstance(condition, Format):
        raise TypeError("a guard takes a Target and a Format")
    if target.kind != "pane":
        raise ValueError(f"a guard target must be a pane id, not {target.text}")
    if not commands:
        raise ValueError("a guard needs at least one command")
    windows: list[Format] = []
    for inner in commands:
        window_target = _check_guarded_target(target, inner)
        if window_target is not None:
            contained = in_window(window_target.text)
            if contained not in windows:
                windows.append(contained)
    if windows:
        condition = all_of(condition, *windows)
    inner_text = f" {SEPARATOR} ".join(c.quoted() for c in commands)
    inner_shown = f" {SEPARATOR} ".join(c.quoted_shown() for c in commands)
    built = command("if-shell")
    built._before_positionals("-F")
    built._add("-F")
    built.target(target).format_arg(condition)
    built._positional_next()
    built._add(inner_text)
    built._add(_NOMATCH_COMMAND.quoted())
    plain = built.build()
    shown = (*plain.shown[:-2], inner_shown, plain.shown[-1])
    return _command(
        plain.elements,
        shown,
        guarded=True,
        target=target,
        flags=plain.flags,
        positionals=plain.positionals,
    )


class GuardOutcome(enum.Enum):
    """How a guarded invocation ended."""

    MATCHED = "matched"
    NOMATCH = "nomatch"
    # tmux exited non-zero: no server, a failing inner command, a refused command. Never a
    # match, since whether the guard ran is unknown.
    FAILED = "failed"


def guard_outcome(result: subprocess.CompletedProcess[str]) -> GuardOutcome:
    """Read a guarded invocation's result: a non-zero exit is ``FAILED``; otherwise any stdout
    line equal to ``termctl-nomatch`` is ``NOMATCH`` (so output that merely looks like one
    fails closed), and anything else, empty stdout included, is ``MATCHED``.

    So the inner commands must never print a line that could be ``termctl-nomatch``: only
    termctl's own formats over tmux ids and numbers, never caller or tmux-controlled text. A
    false non-match would make ``close`` report an open pane as already closed.
    """
    if result.returncode != 0:
        return GuardOutcome.FAILED
    if NOMATCH in result.stdout.splitlines():
        return GuardOutcome.NOMATCH
    return GuardOutcome.MATCHED


# What tmux prints when it cannot reach a server (client.c), naming the -S path exactly as given:
# a refused connection, and a missing socket file or parent directory. tmux sets only LC_CTYPE and
# LC_TIME from the environment, so strerror (the text in parentheses) is not translated.
_NO_SERVER = "no server running on {}"
_CONNECT_MISSING = "error connecting to {} (No such file or directory)"
# What tmux prints when a target is gone (cmd-find.c), as tmux 3.5a prints it for the targets
# termctl builds: the pane or window id, or the session id or bare name (``=S:`` gives ``S``).
_GONE = re.compile(
    "can't find "
    f"(pane: {_PANE_ID.pattern}|window: {_WINDOW_ID.pattern}"
    rf"|session: (\$[0-9]+|{_SESSION_NAME.pattern}))"
)
_MESSAGE_MAX = 200


def _stderr_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return result.stderr.splitlines()


def is_no_server(result: subprocess.CompletedProcess[str], socket_path: str) -> bool:
    """Whether a failed tmux call failed because no server runs on ``socket_path`` (interface.md
    "Server selection": the connection is refused, or the socket file or its parent directory
    does not exist). Any other connection error, such as permission denied, is not.

    Only stderr's first line counts, and it must be exactly tmux's message for this socket: a
    server hook can print any text, but only after tmux has connected.
    """
    if result.returncode == 0:
        return False
    lines = _stderr_lines(result)
    # tmux got the path as os.fsencode's bytes and stderr is decoded as UTF-8, so a non-ASCII path
    # under a non-UTF-8 locale comes back as those bytes decode, not as the str.
    printed = os.fsencode(socket_path).decode("utf-8", "surrogateescape")
    return bool(lines) and lines[0] in (
        _NO_SERVER.format(printed),
        _CONNECT_MISSING.format(printed),
    )


def _is_gone(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether stderr's first line is exactly tmux's message for a missing target: a server hook
    can print any text, even one that starts like it, but only after tmux's own error."""
    lines = _stderr_lines(result)
    return bool(lines) and _GONE.fullmatch(lines[0]) is not None


def is_gone(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether a tmux call failed because its target pane, window or session is gone (only
    stderr's first line counts, as for ``_is_gone``)."""
    return result.returncode > 0 and _is_gone(result)


def tmux_message(result: subprocess.CompletedProcess[str]) -> str:
    """tmux's first stderr line, bounded, for an error message (escaped when printed)."""
    lines = [line.strip() for line in _stderr_lines(result) if line.strip()]
    if not lines:
        return f"tmux exited {result.returncode} without a message"
    first = lines[0]
    return first if len(first) <= _MESSAGE_MAX else first[:_MESSAGE_MAX] + "..."


def tmux_failure(
    result: subprocess.CompletedProcess[str], termctl_id: str, socket_path: str
) -> TermctlError:
    """The error for a failed tmux call on ``socket_path`` that acts on managed pane
    ``termctl_id`` after id resolution (interface.md "Panes that vanish"): ``not_found`` (exit
    10) when tmux says the
    pane, window or session is gone, or that no server runs any more (the pane went with it);
    ``server_unavailable`` (exit 12) for anything else, a tmux killed by a signal included.
    ``close`` counts the first case as closed instead."""
    if result.returncode > 0 and (_is_gone(result) or is_no_server(result, socket_path)):
        return TermctlError(
            "not_found", f"managed pane {termctl_id} is gone ({tmux_message(result)})"
        )
    return TermctlError(
        "server_unavailable",
        f"tmux failed: {tmux_message(result)}",
        details={"socket_path": socket_path},
    )


def check_guard(
    result: subprocess.CompletedProcess[str], termctl_id: str, socket_path: str
) -> None:
    """Raise ``not_found`` (exit 10) on ``NOMATCH``, and ``tmux_failure`` on ``FAILED``;
    ``close`` instead treats ``NOMATCH`` as already closed and calls ``guard_outcome``."""
    outcome = guard_outcome(result)
    if outcome is GuardOutcome.FAILED:
        raise tmux_failure(result, termctl_id, socket_path)
    if outcome is GuardOutcome.NOMATCH:
        raise TermctlError(
            "not_found", f"managed pane {termctl_id} is gone or no longer the one resolved"
        )


# --- open: server and session (interface.md "Open order" step 5) --------------------------------

# What new-session and new-window print with -P: the ids of the pane and window they created.
CREATED_FORMAT = fields("pane_id", "window_id")
_CREATED = re.compile(r"(%[0-9]+) (@[0-9]+)\n?")


@dataclass(frozen=True)
class WindowSpec:
    """The window ``open`` creates: its displayed name, its absolute working directory, the
    command (``None`` for tmux's default shell) and its ``-e`` variables."""

    name: str
    cwd: str
    command: tuple[str, ...] | None
    env: tuple[tuple[str, str], ...] = ()


def has_session(name: str) -> Invocation:
    """``has-session -t =S:``: whether the session exists, and whether a server runs."""
    return Invocation.of(command("has-session").target(session(name)).build())


def _create(builder: CommandBuilder, spec: WindowSpec) -> Command:
    """The rest of ``new-session`` or ``new-window``: always ``-d`` (never the current window),
    the name, the directory, the variables and the command."""
    if not os.path.isabs(spec.cwd):
        raise ValueError(f"the working directory must be absolute: {spec.cwd!r}")
    builder.window_name(spec.name).option("-c", spec.cwd)
    for name, value in spec.env:
        builder.env(name, value)
    for element in spec.command or ():
        builder.arg(element)
    return builder.build()


def _created(name: str) -> CommandBuilder:
    return command(name).flag("-d").flag("-P").format("-F", CREATED_FORMAT)


def new_window(name: str, spec: WindowSpec) -> Invocation:
    """A window in the existing session ``name`` (exact ``=S:``)."""
    return Invocation.of(_create(_created("new-window").target(session(name)), spec))


def new_session(
    name: str, spec: WindowSpec, *, start_server: bool, env: Mapping[str, str]
) -> Invocation:
    """The session ``name`` with its first window, chained with ``destroy-unattached off``
    (spike C2) and, for each ``-e`` variable ``K``, a ``set-environment`` that replaces the
    ``-e`` value in the session environment, so that only the first pane keeps it: ``K`` with
    its value in ``env``, the filtered environment the invocation runs with, or, when ``env``
    lacks ``K``, a removal marker (``-r``). A plain removal (``-u``) would let later windows
    inherit the server's global ``K`` (review round 1, R1-2). The entry takes precedence for
    later windows, also for a ``K`` outside ``update-environment`` (review round 2, R2-1:
    documented, not restored exactly). ``start_server``
    prefixes ``set -s exit-unattached off``, for a server this invocation starts (spike F2);
    never for a server termctl did not start."""
    target = session(name)
    commands: list[Command] = []
    if start_server:
        commands.append(command("set").server_scope().arg("exit-unattached").arg("off").build())
    commands.append(_create(_created("new-session").session_name(name), spec))
    commands.append(command("set").target(target).arg("destroy-unattached").arg("off").build())
    for variable, _ in spec.env:
        restore = command("set-environment").target(target)
        if variable in env:
            restore.arg(variable).arg(env[variable])
        else:
            restore.flag("-r").arg(variable)
        commands.append(restore.build())
    return Invocation.of(*commands)


# --- open: the pane (interface.md "Open order" step 6, "Command") -------------------------------

# Every command termctl runs in a pane starts with this, so tmux always receives at least two
# arguments and executes them directly (tmux runs a single argument through ``sh -c``).
COMMAND_PREFIX = ("/usr/bin/env", "--")


@dataclass(frozen=True)
class Respawn:
    """The real command that replaces the placeholder (``respawn-pane -k``): ``argv`` run as
    ``/usr/bin/env -- [PATH=<path>] <argv...>`` in ``cwd``, with the ``--env`` variables
    ``env`` as ``-e`` (``PATH`` excluded: tmux ignores ``-e PATH``, spike C7, so it reaches the
    program through ``env`` instead, spike G4)."""

    cwd: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    path: str | None = None


def _respawn(target: Target, respawn: Respawn) -> Command:
    if not respawn.argv:
        raise ValueError("a respawn needs a command")
    if not os.path.isabs(respawn.cwd):
        raise ValueError(f"the working directory must be absolute: {respawn.cwd!r}")
    builder = command("respawn-pane").flag("-k").target(target).option("-c", respawn.cwd)
    for name, value in respawn.env:
        if name == "PATH":
            raise ValueError("PATH is passed through /usr/bin/env, never as -e")
        builder.env(name, value)
    for element in COMMAND_PREFIX:
        builder.arg(element)
    if respawn.path is not None:
        builder.env_arg("PATH", respawn.path)
    for element in respawn.argv:
        builder.arg(element)
    return builder.build()


def tag_pane(
    pane_id: str,
    window_id: str,
    options: Sequence[tuple[str, str]],
    remain_on_exit: str,
    respawn: Respawn | None,
) -> Invocation:
    """The tagging invocation of Open order step 6, guarded by ``untagged_in_window``: the
    pane options ``options`` (each written with ``set -p``, an empty value as ``''``), the
    window options ``automatic-rename off`` and ``allow-rename off``, the pane option
    ``remain-on-exit``, then, with argv, ``respawn-pane -k`` with the real command, the
    query of step 7 (``PROCESS_QUERY``), and last ``display -p termctl-tagged``
    (``TAG_CONFIRMATION``), which says the chain ran to the end; ``tagged_query`` reads the
    output.
    """
    if remain_on_exit not in ("off", "failed", "on"):
        raise ValueError(f"invalid remain-on-exit value: {remain_on_exit!r}")
    target = pane(pane_id)
    inner = [
        command("set").flag("-p").target(target).arg(name).arg(value).build()
        for name, value in options
    ]
    for name in ("automatic-rename", "allow-rename"):
        inner.append(command("set").flag("-w").target(target).arg(name).arg("off").build())
    inner.append(
        command("set").flag("-p").target(target).arg("remain-on-exit").arg(remain_on_exit).build()
    )
    if respawn is not None:
        inner.append(_respawn(target, respawn))
    inner.append(_process_query(target))
    inner.append(
        command("display").flag("-p").target(target).format_arg(_format(TAG_CONFIRMATION)).build()
    )
    return Invocation.of(guarded(target, untagged_in_window(window_id), *inner))


def tagged_query(stdout: str) -> str | None:
    """What a tagging invocation (``tag_pane``) that exited 0 printed before its last line
    (the query's answer, for ``parse_process_query``), or None when that last line is not
    ``TAG_CONFIRMATION``: tmux dropped the rest of the chain (its client was killed, say), so
    the pane may be untagged."""
    text = stdout.removesuffix("\n")
    head, newline, last = text.rpartition("\n")
    if last != TAG_CONFIRMATION:
        return None
    return head + newline


# --- open: the process (interface.md "Open order" step 7) ---------------------------------------

# The query of step 7: the pane's process, whether the pane is dead, and the server's PID.
PROCESS_QUERY = fields("pane_pid", "pane_dead", "pid")
# What PROCESS_QUERY prints: PIDs in the @termctl_pid grammar (interface.md "Pane options").
_PROCESS_QUERY = re.compile(r"([1-9][0-9]{0,9}) ([01]) ([1-9][0-9]{0,9})\n?")
_PID = re.compile(r"[1-9][0-9]{0,9}")
_PID_START = re.compile(r"[0-9]{1,20}")


class ProcessQuery(NamedTuple):
    """What the query of step 7 reported: the pane's PID, whether the pane is dead, and the
    tmux server's PID."""

    pane_pid: int
    dead: bool
    server_pid: int


def _process_query(target: Target) -> Command:
    return command("display").flag("-p").target(target).format_arg(PROCESS_QUERY).build()


def parse_process_query(stdout: str) -> ProcessQuery | None:
    """The query's answer, or None for anything but exactly one such line: the query failed
    (``display`` of a missing pane prints empty fields, spike E6; anything else that printed
    is not trusted)."""
    match = _PROCESS_QUERY.fullmatch(stdout)
    if match is None:
        return None
    return ProcessQuery(int(match.group(1)), match.group(2) == "1", int(match.group(3)))


def owned_process(termctl_id: str, pid: str) -> Format:
    """``owned`` plus: the pane is not dead and still runs ``pid`` (the PID recording guard of
    Open order step 7)."""
    _fullmatch(_TERMCTL_ID, termctl_id, "termctl id")
    _fullmatch(_PID, pid, "PID")
    return all_of(
        owned(termctl_id),
        _format(_eq("#{pane_dead}", "0")),
        _format(_eq("#{pane_pid}", pid)),
    )


def record_process(pane_id: str, termctl_id: str, pid: str, start: str) -> Invocation:
    """Write ``@termctl_pid`` and ``@termctl_pid_start`` together, in one guarded invocation,
    so no reader ever sees one without the other (#11), guarded by ``owned_process``."""
    _fullmatch(_PID_START, start, "process start time")
    target = pane(pane_id)
    return Invocation.of(
        guarded(
            target,
            owned_process(termctl_id, pid),
            command("set").flag("-p").target(target).arg("@termctl_pid").arg(pid).build(),
            command("set").flag("-p").target(target).arg("@termctl_pid_start").arg(start).build(),
        )
    )


# --- close (interface.md `close`) ---------------------------------------------------------------


def query_process(pane_id: str, termctl_id: str) -> Invocation:
    """``close --force`` before the kill: ``PROCESS_QUERY`` of the pane, guarded by its being
    managed as ``termctl_id`` (``owned``). Read-only."""
    target = pane(pane_id)
    return Invocation.of(guarded(target, owned(termctl_id), _process_query(target)))


def kill_pane(pane_id: str, termctl_id: str, *, query: bool) -> Invocation:
    """``kill-pane`` of the pane, never its window (spike D5), guarded by its being managed as
    ``termctl_id`` (``owned``). With ``query`` (``close --force`` of a running pane) the same
    invocation first prints ``PROCESS_QUERY``, so the PID read is the one killed."""
    target = pane(pane_id)
    inner = [_process_query(target)] if query else []
    inner.append(command("kill-pane").target(target).build())
    return Invocation.of(guarded(target, owned(termctl_id), *inner))


def cleanup_kill(pane_id: str, window_id: str) -> Invocation:
    """The kill after a failed tagging (Open order): the pane by its exact id, only while it
    is in ``window_id`` and its ``@termctl_pane`` is empty or its own id (``cleanup``)."""
    target = pane(pane_id)
    return Invocation.of(
        guarded(target, cleanup(window_id), command("kill-pane").target(target).build())
    )


def parse_created(stdout: str) -> tuple[str, str] | None:
    """The pane and window ids that ``-P -F CREATED_FORMAT`` printed, or None for anything but
    exactly one such line."""
    match = _CREATED.fullmatch(stdout)
    return None if match is None else (match.group(1), match.group(2))


def _first_line_is(result: subprocess.CompletedProcess[str], line: str) -> bool:
    lines = _stderr_lines(result)
    return result.returncode != 0 and bool(lines) and lines[0] == line


def is_duplicate_session(result: subprocess.CompletedProcess[str], name: str) -> bool:
    """Whether ``new-session`` failed because the session ``name`` exists (another caller
    created it first). Only stderr's first line counts, as for ``is_no_server``."""
    return _first_line_is(result, f"duplicate session: {name}")


def is_session_missing(result: subprocess.CompletedProcess[str], name: str) -> bool:
    """Whether a command targeting ``=name:`` failed because no such session exists."""
    return _first_line_is(result, f"can't find session: {name}")


# --- focus (interface.md `focus`) ---------------------------------------------------------------

# One line per client: its name, last activity (seconds), and its session's id and current
# window. A client attached to no session prints empty session and window fields.
# TODO(#25): `list-clients -F` with these fields is checked on tmux 3.5a only; #25 verifies
# tmux 3.2 (the floor).
CLIENT_FORMAT = fields("client_name", "client_activity", "session_id", "window_id")
_CLIENT_LINE = re.compile(r"(\S+) ([0-9]{1,20}) (\$[0-9]+) (@[0-9]+)")
# Every pane with its session, once per session its window is in (focus step 1).
PANE_SESSION_FORMAT = fields("pane_id", "session_id")


class ClientInfo(NamedTuple):
    """A client attached to a session, as ``list-clients`` reported it."""

    name: str
    activity: int
    session_id: str
    window_id: str


def list_clients() -> Invocation:
    """``list-clients -F CLIENT_FORMAT``: every client of the server, read-only."""
    return Invocation.of(command("list-clients").format("-F", CLIENT_FORMAT).build())


# TODO(#25): parse_clients follows tmux 3.5a's `list-clients -F` output; #25 checks tmux 3.2.
def parse_clients(stdout: str) -> tuple[ClientInfo, ...]:
    """The clients ``list_clients`` printed that termctl can name: a line that is not exactly
    the format (a client attached to no session) or whose name breaks the ``--client`` grammar
    (``switch-client -c`` could not name it safely) is left out."""
    found: list[ClientInfo] = []
    for line in stdout.split("\n"):
        match = _CLIENT_LINE.fullmatch(line)
        if match is None or not is_client(match.group(1)):
            continue
        name, activity, session_id, window_id = match.groups()
        found.append(ClientInfo(name, int(activity), session_id, window_id))
    return tuple(found)


def pane_sessions() -> Invocation:
    """``list-panes -a -F '#{pane_id} #{session_id}'``: every pane with each session its window
    is in, read-only (focus step 1 checks the caller's pane against the session ``TMUX``
    names; ``display -t %N`` would report one session of tmux's choosing)."""
    return Invocation.of(command("list-panes").flag("-a").format("-F", PANE_SESSION_FORMAT).build())


def has_pane_in_session(stdout: str, pane_id: str, session_id: str) -> bool:
    """Whether ``pane_sessions`` printed exactly the line ``pane_id session_id``: the pane
    exists and its window is in that session."""
    return f"{pane_id} {session_id}" in stdout.split("\n")


def switch_client(client: str, pane_id: str) -> Invocation:
    """``switch-client -E -c CLIENT -t %N``: always a named client, never the client's
    environment imported into the session (spike F1), and the pane id, which selects the
    session, window and pane."""
    return Invocation.of(
        command("switch-client").flag("-E").client(client).target(pane(pane_id)).build()
    )


def attach_argv(
    tmux_bin: str, socket_path: str, session_id: str, window_id: str, pane_id: str
) -> list[str]:
    """``<tmux_bin> -S <socket_path> attach -E -t '=$N:@W.%P'``: the 7-element command that
    attaches a terminal with the pane selected (spike D2), failing unless the pane is still in
    that window and session (spike F7), with ``-E`` keeping the attaching terminal's
    environment out of the session (spike F1)."""
    attach = (
        command("attach").flag("-E").target(pane_in_session(session_id, window_id, pane_id)).build()
    )
    return Invocation.of(attach).argv(tmux_bin, socket_path)


def is_client_missing(result: subprocess.CompletedProcess[str], name: str) -> bool:
    """Whether ``switch-client -c name`` failed because no such client is attached."""
    return _first_line_is(result, f"can't find client: {name}")


# --- the runner ---------------------------------------------------------------------------------


def _escape_controls(text: str) -> str:
    """C0, DEL and C1 characters as ``\\xNN``, so a trace cannot drive the terminal."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


class Tmux:
    """Runs invocations against one server through the gateway.

    ``tmux_bin`` is the absolute path resolved once per invocation (#10); ``socket_path`` the
    resolved, checked socket (#8); ``env`` the environment filtered by ``server_env_allow``
    (#15). ``trace`` receives each command as shown under ``--verbose``. ``configured_bin``
    is the configured ``tmux_bin`` reported in ``tmux_missing`` details (``tmux_bin`` when
    not given).
    """

    def __init__(
        self,
        gateway: Gateway,
        tmux_bin: str,
        socket_path: str,
        *,
        env: Mapping[str, str],
        trace: Callable[[str], None] | None = None,
        configured_bin: str | None = None,
    ) -> None:
        _check_tmux_bin(tmux_bin)
        _check_socket(socket_path)
        self.gateway = gateway
        self.tmux_bin = tmux_bin
        self.socket_path = socket_path
        self.env = dict(env)
        self.trace = trace
        self.configured_bin = tmux_bin if configured_bin is None else configured_bin

    def run(
        self, invocation: Invocation, *, utf8: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Run ``invocation`` and return tmux's result; mapping tmux's failures to exit codes
        is the caller's, since it depends on the command (``tmux_failure``, ``is_no_server``).
        ``utf8``: see ``Invocation.argv``."""
        argv = invocation.argv(self.tmux_bin, self.socket_path, utf8=utf8)
        if self.trace is not None:
            shown = shlex.join(invocation.shown_argv(self.tmux_bin, self.socket_path, utf8=utf8))
            self.trace("tmux: " + _escape_controls(shown))
        try:
            return self.gateway.run(argv, timeout=TMUX_TIMEOUT, env=self.env)
        except subprocess.TimeoutExpired:
            raise TermctlError(
                "server_unavailable",
                f"tmux did not answer within {TMUX_TIMEOUT:g} seconds",
                details={"socket_path": self.socket_path},
            ) from None
        except OSError as exc:
            raise TermctlError(
                "tmux_missing",
                f"cannot run {self.tmux_bin}: {exc.strerror or exc}",
                details={"tmux_bin": self.configured_bin, "path": self.tmux_bin},
            ) from None
