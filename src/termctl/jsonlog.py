"""``log.jsonl`` and ``audit.jsonl`` records (interface.md "Audit and log").

The record builders check which fields a record carries (presence and exclusivity rules of
``schemas/log.json`` and ``schemas/audit.json``); breaking one is a programming error
(``ValueError``). They do not validate field values: callers pass validated values. A NaN or
infinity anywhere in a record is also a programming error (``ValueError``, nothing written).

Writers append one JSON line per record with exactly one ``O_APPEND`` write, so records from
concurrent termctl processes never interleave; writers hold an exclusive ``flock`` on the file
around the write so the newline check below sees no write in progress. A write cut short
(disk full, file size limit) is a failure and is never continued: a second write could
interleave with another process. It leaves a partial line; the next record starts with a
newline when the file does not end with one, so the partial line damages only itself. A
failure to write raises ``StateDirError``; what a command does about it is the command's
decision.
"""

# open (cli.Context.log) is the first caller. interface.md "Audit and log" ("Records"): a
# record that cannot be written produces a warning and does not change the exit code; #19-#24
# follow that rule. Each command builds its record with the builders below and appends it with
# the CLI context's environ and clock.

from __future__ import annotations

import fcntl
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .output import to_json
from .statedir import StateDirError, open_file, open_state_dir

LOG_FILE = "log.jsonl"
AUDIT_FILE = "audit.jsonl"

_LEVELS = {
    "ok": "info",
    "error": "error",
    "warning": "warn",
    "refused": "warn",
    "vetoed": "warn",
    "escalated": "warn",
}
_COMMAND_EVENTS = {
    "open",
    "close",
    "rename",
    "mark",
    "unmark",
    "label_add",
    "label_remove",
    "focus",
}
_WARNING_EVENTS = {"hook_failed", "warning"}


def format_ts(when: datetime) -> str:
    """``when`` in UTC with millisecond precision, e.g. ``2026-10-30T12:34:56.789Z``."""
    if when.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    utc = when.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def level_for(result: str) -> str:
    """``error`` for ``error``; ``warn`` for ``warning``, ``refused``, ``vetoed`` and
    ``escalated``; ``info`` for ``ok``."""
    try:
        return _LEVELS[result]
    except KeyError:
        raise ValueError(f"unknown result: {result!r}") from None


def _require(event: str, field: str, value: Any, wanted: bool) -> None:
    if wanted and value is None:
        raise ValueError(f"{event} records need {field}")
    if not wanted and value is not None:
        raise ValueError(f"{event} records never carry {field}")


def log_record(
    ts: datetime,
    *,
    event: str,
    result: str,
    id: str | None,
    socket_path: str | None,
    exit_code: int | None = None,
    message: str | None = None,
    details: Mapping[str, Any] | None = None,
    actor: str | None = None,
    decision_ids: Sequence[str] | None = None,
    escalation_override: bool | None = None,
    force: bool | None = None,
    action: str | None = None,
    hooks: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """A ``log.jsonl`` record; fields given as None are omitted (``id`` and ``socket_path``
    are always present, null when none was resolved). ``hooks`` (``open`` and ``close`` only)
    lists the hooks the step started, with their process groups
    (``hooks.HookProcess.log_entry``)."""
    if event not in _COMMAND_EVENTS | _WARNING_EVENTS:
        raise ValueError(f"unknown event: {event!r}")
    if (event in _WARNING_EVENTS) != (result == "warning"):
        raise ValueError("hook_failed and warning records, and only they, have result warning")
    gated = event in ("open", "close")
    _require(event, "actor", actor, gated)
    _require(event, "escalation_override", escalation_override, gated)
    _require(event, "hooks", hooks, gated)
    if gated:
        _require(event, "decision_ids", decision_ids, True)
    _require(event, "force", force, event == "close")
    _require(event, "action", action, event == "focus")
    record: dict[str, Any] = {
        "ts": format_ts(ts),
        "level": level_for(result),
        "event": event,
        "id": id,
        "socket_path": socket_path,
        "result": result,
    }
    optional: dict[str, Any] = {
        "exit_code": exit_code,
        "actor": actor,
        "decision_ids": None if decision_ids is None else list(decision_ids),
        "escalation_override": escalation_override,
        "force": force,
        "action": action,
        "hooks": None if hooks is None else [dict(entry) for entry in hooks],
        "message": message,
        "details": None if details is None else dict(details),
    }
    record.update({key: value for key, value in optional.items() if value is not None})
    return record


def _audit_record(
    ts: datetime,
    *,
    actor: str,
    decision_ids: Sequence[str],
    target: str,
    action: str,
    force: bool,
    escalation_override: bool,
    inputs: dict[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "ts": format_ts(ts),
        "actor": actor,
        "decision_id": list(decision_ids),
        "target": target,
        "action": action,
        "force": force,
        "escalation_override": escalation_override,
        "inputs": inputs,
        "result": dict(result),
    }


def audit_open_record(
    ts: datetime,
    *,
    actor: str,
    decision_ids: Sequence[str],
    target: str,
    socket_path: str,
    name: str,
    argv: Sequence[str],
    cwd: str,
    placement: str,
    session: str,
    env_names: Sequence[str],
    labels: Sequence[str],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """The audit record of an ``open --override-escalation`` once its id was generated.
    ``env_names`` are variable names only, never values. ``result`` is the ``--json`` output
    or the error envelope."""
    inputs: dict[str, Any] = {
        "socket_path": socket_path,
        "pid": None,
        "grace_ms": None,
        "name": name,
        "argv": list(argv),
        "cwd": cwd,
        "placement": placement,
        "session": session,
        "env_names": list(env_names),
        "labels": list(labels),
    }
    return _audit_record(
        ts,
        actor=actor,
        decision_ids=decision_ids,
        target=target,
        action="open",
        force=False,
        escalation_override=True,
        inputs=inputs,
        result=result,
    )


def audit_close_record(
    ts: datetime,
    *,
    actor: str,
    decision_ids: Sequence[str],
    target: str,
    force: bool,
    escalation_override: bool,
    socket_path: str,
    pid: int | None,
    grace_ms: int | None,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """The audit record of a ``close --force`` that saw a running pane, or of a ``close
    --override-escalation``, once its id was resolved. ``grace_ms`` is null unless forced."""
    if not (force or escalation_override):
        raise ValueError("close is audited only with force or escalation_override")
    if not force and grace_ms is not None:
        raise ValueError("grace_ms is null unless forced")
    inputs: dict[str, Any] = {"socket_path": socket_path, "pid": pid, "grace_ms": grace_ms}
    return _audit_record(
        ts,
        actor=actor,
        decision_ids=decision_ids,
        target=target,
        action="close",
        force=force,
        escalation_override=escalation_override,
        inputs=inputs,
        result=result,
    )


def _append(environ: Mapping[str, str], name: str, record: dict[str, Any]) -> None:
    # One ASCII line (to_json escapes every control and non-ASCII character).
    data = to_json(record, allow_nan=False).encode("ascii")
    dir_fd, dir_path = open_state_dir(environ)
    try:
        fd = open_file(dir_fd, dir_path, name, os.O_RDWR | os.O_APPEND)
    finally:
        os.close(dir_fd)
    path = os.path.join(dir_path, name)
    try:
        try:
            # Serializes termctl writers for the newline check below, which another
            # writer's in-progress append would otherwise fool. Held for microseconds; where
            # flock is unsupported, write without it.
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            pass
        size = os.fstat(fd).st_size
        if size > 0 and os.pread(fd, 1, size - 1) != b"\n":
            data = b"\n" + data  # never join a partial line left by a failed write
        written = os.write(fd, data)
    except OSError as exc:
        raise StateDirError(path, exc.strerror or str(exc)) from None
    finally:
        os.close(fd)
    if written != len(data):
        raise StateDirError(path, f"write cut short ({written} of {len(data)} bytes)")


def append_log(environ: Mapping[str, str], record: dict[str, Any]) -> None:
    """Append ``record`` to ``log.jsonl``; raises ``StateDirError`` if it cannot."""
    _append(environ, LOG_FILE, record)


def append_audit(environ: Mapping[str, str], record: dict[str, Any]) -> None:
    """Append ``record`` to ``audit.jsonl``; raises ``StateDirError`` if it cannot."""
    _append(environ, AUDIT_FILE, record)
