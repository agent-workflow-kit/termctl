"""The pane reader: every managed pane of a server from one ``list-panes -a`` read
(interface.md "Pane options", "Managed panes", "Liveness", `list`; design.md D27).

One tab-separated record per pane row, ended by a sentinel that is a fresh random token per
read, so data inside a value cannot end a record; it does not protect against server hooks or
commands run by someone with socket access (a hook can see the format, sentinel included, and
print a forged record). The free-text fields tmux controls (session and window names) are read
with tab and newline replaced by spaces in the format string; the ``@termctl_*`` options are read
as is, so a value holding a tab or newline (one termctl never writes) breaks its record and the
pane is ignored with a warning instead of being read wrongly. tmux prints the other control
characters of every field escaped (``\\033``), and with ``-u`` writes UTF-8 whatever the locale.

A pane is managed only if its ``@termctl_pane`` equals its own pane id: a ``#{@...}`` format
also returns values inherited from the window, session or global scope (spike F4), and such a
value names another pane, or none, unless it was deliberately set to name this pane (forgery).
Every other ``@termctl_*`` value of a managed pane is checked against its grammar; a pane with
a missing or invalid value, or an unknown ``@termctl_v``, is ignored with a warning. A window
linked into several sessions gives one row per session (spike D6); the reader keeps one pane
per pane id, with every session it is linked into.

Everything read here is untrusted: anything with socket access can write these options and
rename sessions and windows (interface.md "Residual risks").
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from . import validate
from .errors import TermctlError
from .tmux_backend import Invocation, Tmux, command, is_no_server, record, tmux_message
from .validate import show

# tmux's own fields: ids and numbers, never text anyone else chooses.
TMUX_FIELDS: tuple[str, ...] = (
    "pane_id",
    "window_id",
    "pane_dead",
    "pane_dead_status",
    # Empty on tmux 3.2, which has no such format (interface.md "Command exit").
    "pane_dead_signal",
    "pane_pid",
    "window_panes",
)
# Free text tmux reports; read sanitized (tab and newline become spaces).
TEXT_FIELDS: tuple[str, ...] = ("session_name", "window_name")
# The pane options (interface.md "Pane options"), read as is. The ownership marker comes first,
# so no tab in another option can shift it.
OPTION_FIELDS: tuple[str, ...] = (
    "@termctl_pane",
    "@termctl_v",
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
)
FIELDS: tuple[str, ...] = TMUX_FIELDS + TEXT_FIELDS + OPTION_FIELDS
_INDEX = {name: index for index, name in enumerate(FIELDS)}
_OWNER = _INDEX["@termctl_pane"]

SCHEMA_VERSION = "1"
_SENTINEL_PREFIX = "termctl-end-"

_PANE_ID = re.compile(r"%[0-9]+")
_WINDOW_ID = re.compile(r"@[0-9]+")
_TERMCTL_ID = re.compile(r"tc-[a-z2-7]{8}")
_COUNT = re.compile(r"0|[1-9][0-9]{0,9}")
_POSITIVE = re.compile(r"[1-9][0-9]{0,9}")
_PID = re.compile(r"[1-9][0-9]{0,9}")
_PID_START = re.compile(r"[0-9]{1,20}")
_CREATED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_KEEP = ("never", "failed", "always")

State = Literal["running", "dead"]


def new_sentinel() -> str:
    """A fresh end-of-record token, unknown to anything that only writes a pane option value
    (not to a server hook, which can see the format it ends)."""
    return _SENTINEL_PREFIX + secrets.token_hex(8)


def invocation(sentinel: str) -> Invocation:
    """``list-panes -a -F <record>``: every pane of the server, read-only."""
    fmt = record(FIELDS, TEXT_FIELDS, sentinel)
    return Invocation.of(command("list-panes").flag("-a").format("-F", fmt).build())


@dataclass(frozen=True)
class ManagedPane:
    """A managed pane as read. The option-derived fields are untrusted (see the module doc)."""

    id: str
    name: str
    marker: str | None
    marker_text: str | None
    labels: tuple[str, ...]
    placement: str
    keep_on_exit: str
    created: str
    # @termctl_window: the window the pane was opened in (rename and mark compare it).
    opened_window: str
    # @termctl_pid and @termctl_pid_start: the recorded process, if any.
    pid: int | None
    pid_start: str | None
    pane_id: str
    window_id: str
    # The first session tmux lists for the pane's window, and every session it is linked into.
    session: str
    sessions: tuple[str, ...]
    display_name: str
    # tmux's #{pane_pid}: close compares it with the recorded pid.
    pane_pid: int | None
    window_panes: int
    dead: bool
    exit_status: int | None
    exit_signal: int | None

    @property
    def state(self) -> State:
        return "dead" if self.dead else "running"

    def pane_object(self, socket_path: str) -> dict[str, Any]:
        """The pane object of ``--json`` output (``schemas/cli/pane.json``)."""
        return {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name,
            "marker": self.marker,
            "marker_text": self.marker_text,
            "labels": list(self.labels),
            "placement": self.placement,
            "session": self.session,
            "pane_id": self.pane_id,
            "window_id": self.window_id,
            "socket_path": socket_path,
            "created": self.created,
            "pid": self.pid,
            "state": self.state,
            "exit_status": self.exit_status,
            "exit_signal": self.exit_signal,
            "keep_on_exit": self.keep_on_exit,
        }


@dataclass(frozen=True)
class PaneRead:
    """The result of one read. ``server_running`` is false when no server runs on the socket
    (``list`` prints an empty list; commands that take an id exit 10, ``no_server``).

    ``warnings`` name the managed panes that were ignored and why; each is untrusted text,
    escaped when printed.
    """

    # The commands print each warning (``cli.read_panes``); TODO(#5): logged commands also
    # write each as a `warning` log record.
    server_running: bool
    panes: tuple[ManagedPane, ...]
    warnings: tuple[str, ...]


# What list-panes -a prints, alone, on a running server with no sessions (tmux 3.5a).
_NO_TARGET = "no current target"


def read(tmux: Tmux, *, sentinel: str | None = None) -> PaneRead:
    """Read every managed pane of ``tmux``'s server with one ``list-panes -a``.

    A refused connection, or a socket file or directory that does not exist, is no server. A
    running server with no sessions makes tmux exit 1 with exactly ``no current target``: no
    panes. Any other tmux failure exits 12 (``server_unavailable``). The read never starts a
    server.
    """
    token = new_sentinel() if sentinel is None else sentinel
    result = tmux.run(invocation(token), utf8=True)
    if result.returncode != 0:
        if is_no_server(result, tmux.socket_path):
            return PaneRead(server_running=False, panes=(), warnings=())
        if result.returncode == 1 and result.stderr.splitlines() == [_NO_TARGET]:
            return PaneRead(server_running=True, panes=(), warnings=())
        raise TermctlError(
            "server_unavailable",
            f"cannot list the panes: {tmux_message(result)}",
            details={"socket_path": tmux.socket_path},
        )
    found, warnings = parse(result.stdout, token)
    return PaneRead(server_running=True, panes=found, warnings=warnings)


class _Ignored(Exception):
    """A managed pane that fails a check; the message says why."""


def parse(stdout: str, sentinel: str) -> tuple[tuple[ManagedPane, ...], tuple[str, ...]]:
    """The managed panes in ``list-panes`` output and the warnings for those ignored.

    Records are split at ``<TAB><sentinel><NEWLINE>``; anything after the last one (a cut-off
    record) is ignored with a warning. The sentinel guards against data inside values, not
    against output from server hooks or from anyone with socket access. Rows are merged by pane
    id before they are checked.
    """
    terminator = "\t" + sentinel + "\n"
    chunks = stdout.split(terminator)
    warnings: list[str] = []
    if chunks[-1]:
        warnings.append("ignoring an incomplete list-panes record")
    rows: dict[str, list[list[str]]] = {}
    for chunk in chunks[:-1]:
        parts = chunk.split("\t")
        pane_id = parts[0]
        if _PANE_ID.fullmatch(pane_id) is None:
            warnings.append(f"ignoring an unreadable list-panes record starting {show(pane_id)}")
            continue
        owner = parts[_OWNER] if len(parts) > _OWNER else ""
        if owner != pane_id:
            continue  # not managed: no value of its own, or one naming another pane
        rows.setdefault(pane_id, []).append(parts)
    found: list[ManagedPane] = []
    for pane_id, pane_rows in rows.items():
        try:
            found.append(_managed_pane(pane_id, pane_rows))
        except _Ignored as problem:
            warnings.append(f"ignoring managed pane {pane_id}: {problem}")
    return tuple(found), tuple(warnings)


def _managed_pane(pane_id: str, pane_rows: list[list[str]]) -> ManagedPane:
    for parts in pane_rows:
        if len(parts) != len(FIELDS):
            raise _Ignored("malformed list-panes record (a value holds a tab or newline)")
    first: dict[str, str] = dict(zip(FIELDS, pane_rows[0], strict=True))
    sessions = _sessions(pane_rows)
    if first["@termctl_v"] != SCHEMA_VERSION:
        raise _Ignored(f"unknown @termctl_v {show(first['@termctl_v'])}")
    dead = _choice(first, "pane_dead", ("0", "1")) == "1"
    exit_status = _optional_int(first, "pane_dead_status", _COUNT)
    exit_signal = _optional_int(first, "pane_dead_signal", _COUNT)
    if exit_status is not None and exit_signal is not None:
        raise _Ignored("tmux reports both an exit status and an exit signal")
    marker = _marker(first)
    pid = _optional_int(first, "@termctl_pid", _PID)
    pid_start = _optional(first, "@termctl_pid_start", _PID_START)
    if (pid is None) != (pid_start is None):
        raise _Ignored("@termctl_pid and @termctl_pid_start must both be set or both empty")
    return ManagedPane(
        id=_match(first, "@termctl_id", _TERMCTL_ID),
        name=_text(first, "@termctl_name", validate.name_problem),
        marker=marker[0],
        marker_text=marker[1],
        labels=_labels(first),
        placement=_check(first, "@termctl_placement", validate.is_placement_name),
        keep_on_exit=_choice(first, "@termctl_keep", _KEEP),
        created=_created(first),
        opened_window=_match(first, "@termctl_window", _WINDOW_ID),
        pid=pid,
        pid_start=pid_start,
        pane_id=pane_id,
        window_id=_match(first, "window_id", _WINDOW_ID),
        session=sessions[0],
        sessions=sessions,
        display_name=first["window_name"],
        pane_pid=_optional_int(first, "pane_pid", _COUNT),
        window_panes=int(_match(first, "window_panes", _POSITIVE)),
        dead=dead,
        exit_status=exit_status if dead else None,
        exit_signal=exit_signal if dead else None,
    )


def _sessions(pane_rows: list[list[str]]) -> tuple[str, ...]:
    sessions: list[str] = []
    for parts in pane_rows:
        name = parts[_INDEX["session_name"]]
        if not name:
            raise _Ignored("tmux reports an empty session name")
        if name not in sessions:
            sessions.append(name)
    return tuple(sessions)


def _invalid(values: dict[str, str], field: str) -> _Ignored:
    return _Ignored(f"invalid {field} {show(values[field])}")


def _match(values: dict[str, str], field: str, pattern: re.Pattern[str]) -> str:
    if pattern.fullmatch(values[field]) is None:
        raise _invalid(values, field)
    return values[field]


def _optional(values: dict[str, str], field: str, pattern: re.Pattern[str]) -> str | None:
    return _match(values, field, pattern) if values[field] else None


def _optional_int(values: dict[str, str], field: str, pattern: re.Pattern[str]) -> int | None:
    value = _optional(values, field, pattern)
    return None if value is None else int(value)


def _choice(values: dict[str, str], field: str, choices: Sequence[str]) -> str:
    if values[field] not in choices:
        raise _invalid(values, field)
    return values[field]


def _check(values: dict[str, str], field: str, predicate: Callable[[str], bool]) -> str:
    if not predicate(values[field]):
        raise _invalid(values, field)
    return values[field]


def _text(values: dict[str, str], field: str, problem_of: Callable[[str], str | None]) -> str:
    problem = problem_of(values[field])
    if problem:
        raise _Ignored(f"invalid {field} {show(values[field])}: {problem}")
    return values[field]


def _marker(values: dict[str, str]) -> tuple[str | None, str | None]:
    """Marker name and text: both set, or both empty (interface.md "Markers")."""
    marker, text = values["@termctl_marker"], values["@termctl_marker_text"]
    if not marker and not text:
        return None, None
    if not marker or not text:
        raise _Ignored("@termctl_marker and @termctl_marker_text must both be set or both empty")
    _check(values, "@termctl_marker", validate.is_marker_name)
    _text(values, "@termctl_marker_text", validate.marker_text_problem)
    return marker, text


def _labels(values: dict[str, str]) -> tuple[str, ...]:
    """Pairs sorted by code point, unique, joined by single spaces, at most 32; or empty."""
    raw = values["@termctl_labels"]
    if not raw:
        return ()
    pairs = raw.split(" ")
    if len(pairs) > validate.MAX_LABELS or pairs != sorted(set(pairs)):
        raise _invalid(values, "@termctl_labels")
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not validate.is_label_key(key) or not validate.is_label_value(value):
            raise _invalid(values, "@termctl_labels")
    return tuple(pairs)


def _created(values: dict[str, str]) -> str:
    """RFC 3339 UTC with millisecond precision, and a real time."""
    value = _match(values, "@termctl_created", _CREATED)
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise _invalid(values, "@termctl_created") from None
    return value


def select(
    panes: Iterable[ManagedPane],
    *,
    labels: Sequence[str] = (),
    session: str | None = None,
    dead: bool = False,
) -> list[ManagedPane]:
    """``list``'s filters, applied in process (never as tmux ``-f`` filters, design.md spike C),
    ordered by ``created``, then id.

    ``labels`` are ``key=value`` pairs, ANDed: a pane matches when it has every pair. ``session``
    matches any session the pane's window is linked into, exactly. ``dead`` keeps only dead
    panes. ``created`` has one fixed width, so its text order is time order.
    """
    wanted = set(labels)
    chosen = [
        pane
        for pane in panes
        if wanted <= set(pane.labels)
        and (session is None or session in pane.sessions)
        and (not dead or pane.dead)
    ]
    return sorted(chosen, key=lambda pane: (pane.created, pane.id))
