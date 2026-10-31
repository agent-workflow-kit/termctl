"""``log.jsonl`` and ``audit.jsonl`` records (interface.md "Audit and log").

The record builders enforce the field rules of ``schemas/log.json`` and ``schemas/audit.json``;
breaking one is a programming error (``ValueError``), not a user error. Writers append one
JSON line per record with a single ``O_APPEND`` write, so records from concurrent termctl
processes never interleave. A failure to write raises ``StateDirError``; what a command does
about it is the command's decision.
"""

# TODO(#17): open is the first caller; it decides what a write failure does (the action has
# already happened by then) and #19-#24 follow it. Each command builds its record with the
# builders below and appends it with the CLI context's environ and clock.

from __future__ import annotations

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
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


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
) -> dict[str, Any]:
    """A ``log.jsonl`` record; fields given as None are omitted (``id`` and ``socket_path``
    are always present, null when none was resolved)."""
    if event not in _COMMAND_EVENTS | _WARNING_EVENTS:
        raise ValueError(f"unknown event: {event!r}")
    if (event in _WARNING_EVENTS) != (result == "warning"):
        raise ValueError("hook_failed and warning records, and only they, have result warning")
    gated = event in ("open", "close")
    _require(event, "actor", actor, gated)
    _require(event, "escalation_override", escalation_override, gated)
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
    # One ASCII line (to_json escapes every control and non-ASCII character), written with
    # one O_APPEND write so concurrent appends never interleave.
    data = to_json(record).encode("ascii")
    dir_fd = open_state_dir(environ)
    try:
        fd = open_file(dir_fd, name, os.O_WRONLY | os.O_APPEND)
    finally:
        os.close(dir_fd)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def append_log(environ: Mapping[str, str], record: dict[str, Any]) -> None:
    """Append ``record`` to ``log.jsonl``; raises ``StateDirError`` if it cannot."""
    try:
        _append(environ, LOG_FILE, record)
    except OSError as exc:
        raise StateDirError(f"{LOG_FILE}: {exc.strerror or exc}") from None


def append_audit(environ: Mapping[str, str], record: dict[str, Any]) -> None:
    """Append ``record`` to ``audit.jsonl``; raises ``StateDirError`` if it cannot."""
    try:
        _append(environ, AUDIT_FILE, record)
    except OSError as exc:
        raise StateDirError(f"{AUDIT_FILE}: {exc.strerror or exc}") from None
