from __future__ import annotations

import json
import multiprocessing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from termctl import jsonlog
from termctl.errors import TermctlError
from termctl.statedir import StateDirError

from .schemas import validate

TS = datetime(2026, 10, 30, 12, 34, 56, 789123, tzinfo=UTC)
SOCKET = "/tmp/tmux-1000/default"
ID = "tc-abcdefgh"


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    return {"XDG_STATE_HOME": str(tmp_path / "state")}


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- timestamps -----------------------------------------------------------------------------


def test_format_ts_is_utc_milliseconds() -> None:
    assert jsonlog.format_ts(TS) == "2026-10-30T12:34:56.789Z"


def test_format_ts_converts_to_utc() -> None:
    local = datetime(2026, 10, 30, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert jsonlog.format_ts(local) == "2026-10-30T12:00:00.000Z"


def test_format_ts_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError):
        jsonlog.format_ts(datetime(2026, 1, 1))


# --- level ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "level"),
    [
        ("ok", "info"),
        ("error", "error"),
        ("warning", "warn"),
        ("refused", "warn"),
        ("vetoed", "warn"),
        ("escalated", "warn"),
    ],
)
def test_level_is_derived_from_result(result: str, level: str) -> None:
    assert jsonlog.level_for(result) == level


def test_unknown_result_is_rejected() -> None:
    with pytest.raises(ValueError):
        jsonlog.level_for("fine")


# --- log records ----------------------------------------------------------------------------


def open_record(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "event": "open",
        "result": "ok",
        "id": ID,
        "socket_path": SOCKET,
        "exit_code": 0,
        "actor": "auto:unknown",
        "decision_ids": [],
        "escalation_override": False,
    }
    fields.update(overrides)
    return jsonlog.log_record(TS, **fields)


def test_open_record_validates() -> None:
    record = open_record()
    validate(record, "termctl/log.json")
    assert record["level"] == "info"
    assert record["ts"] == "2026-10-30T12:34:56.789Z"


@pytest.mark.parametrize(
    ("result", "code", "level"),
    [("error", 12, "error"), ("vetoed", 11, "warn"), ("escalated", 13, "warn")],
)
def test_failed_open_records_validate(result: str, code: int, level: str) -> None:
    record = open_record(result=result, exit_code=code, message="m", details={"k": "v"})
    validate(record, "termctl/log.json")
    assert record["level"] == level


def test_record_without_resolved_id_or_socket_validates() -> None:
    record = open_record(id=None, socket_path=None, result="error", exit_code=12)
    validate(record, "termctl/log.json")
    assert record["id"] is None and record["socket_path"] is None


def test_close_record_needs_force() -> None:
    with pytest.raises(ValueError, match="force"):
        open_record(event="close")
    record = open_record(event="close", force=True, result="refused", exit_code=20)
    validate(record, "termctl/log.json")
    assert record["level"] == "warn"


def test_focus_record_needs_action_and_no_actor() -> None:
    record = jsonlog.log_record(
        TS, event="focus", result="ok", id=ID, socket_path=SOCKET, action="attach"
    )
    validate(record, "termctl/log.json")
    with pytest.raises(ValueError, match="action"):
        jsonlog.log_record(TS, event="focus", result="ok", id=ID, socket_path=SOCKET)


@pytest.mark.parametrize("event", ["rename", "mark", "unmark", "label_add", "label_remove"])
def test_option_write_records_validate(event: str) -> None:
    record = jsonlog.log_record(
        TS, event=event, result="error", id=ID, socket_path=SOCKET, exit_code=12, message="locked"
    )
    validate(record, "termctl/log.json")
    assert record["level"] == "error"


@pytest.mark.parametrize("event", ["rename", "focus", "hook_failed"])
def test_actor_and_override_only_on_open_and_close(event: str) -> None:
    extra: dict[str, Any] = {"action": "switched"} if event == "focus" else {}
    result = "warning" if event == "hook_failed" else "ok"
    with pytest.raises(ValueError, match="actor"):
        jsonlog.log_record(
            TS, event=event, result=result, id=ID, socket_path=SOCKET, actor="auto:x", **extra
        )
    with pytest.raises(ValueError, match="escalation_override"):
        jsonlog.log_record(
            TS,
            event=event,
            result=result,
            id=ID,
            socket_path=SOCKET,
            escalation_override=False,
            **extra,
        )


def test_open_needs_actor_decision_ids_and_override() -> None:
    for missing in ("actor", "decision_ids", "escalation_override"):
        with pytest.raises(ValueError, match=missing):
            open_record(**{missing: None})


def test_force_only_on_close_and_action_only_on_focus() -> None:
    with pytest.raises(ValueError, match="force"):
        open_record(force=False)
    with pytest.raises(ValueError, match="action"):
        open_record(action="attach")


@pytest.mark.parametrize("event", ["hook_failed", "warning"])
def test_warning_events_carry_result_warning(event: str) -> None:
    record = jsonlog.log_record(
        TS, event=event, result="warning", id=None, socket_path=None, message="m"
    )
    validate(record, "termctl/log.json")
    assert record["level"] == "warn"
    with pytest.raises(ValueError, match="warning"):
        jsonlog.log_record(TS, event=event, result="ok", id=None, socket_path=None)


def test_command_events_never_carry_result_warning() -> None:
    with pytest.raises(ValueError, match="warning"):
        jsonlog.log_record(TS, event="rename", result="warning", id=ID, socket_path=SOCKET)


def test_unknown_event_is_rejected() -> None:
    with pytest.raises(ValueError, match="event"):
        jsonlog.log_record(TS, event="list", result="ok", id=None, socket_path=None)


def test_absent_optional_fields_are_omitted() -> None:
    record = jsonlog.log_record(TS, event="mark", result="ok", id=ID, socket_path=SOCKET)
    assert set(record) == {"ts", "level", "event", "id", "socket_path", "result"}


def test_decision_ids_are_copied() -> None:
    ids = ["a"]
    record = open_record(decision_ids=ids)
    ids.append("b")
    assert record["decision_ids"] == ["a"]


# --- audit records --------------------------------------------------------------------------

OPEN_OUTPUT: dict[str, Any] = {
    "error": {
        "code": "vetoed",
        "message": "no",
        "hook": "h",
        "hook_stderr": "",
        "decision_ids": ["d1"],
    }
}


def audit_open(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "actor": "human:someone",
        "decision_ids": ["d1"],
        "target": ID,
        "socket_path": SOCKET,
        "name": "build",
        "argv": ["make", "-j8"],
        "cwd": "/work",
        "placement": "default",
        "session": "main",
        "env_names": ["FOO"],
        "labels": ["k=v"],
        "result": OPEN_OUTPUT,
    }
    fields.update(overrides)
    return jsonlog.audit_open_record(TS, **fields)


def test_audit_open_record_validates() -> None:
    record = audit_open()
    validate(record, "termctl/audit.json")
    assert record["action"] == "open"
    assert record["force"] is False and record["escalation_override"] is True
    assert record["decision_id"] == ["d1"]
    assert record["inputs"]["pid"] is None and record["inputs"]["grace_ms"] is None


def test_audit_records_carry_env_names_only() -> None:
    assert audit_open()["inputs"]["env_names"] == ["FOO"]


CLOSE_ERROR: dict[str, Any] = {
    "error": {"code": "refused", "message": "running", "reason": "running"}
}


@pytest.mark.parametrize(
    ("force", "override", "grace_ms"), [(True, False, 5000), (False, True, None), (True, True, 0)]
)
def test_audit_close_record_validates(force: bool, override: bool, grace_ms: int | None) -> None:
    record = jsonlog.audit_close_record(
        TS,
        actor="human:someone",
        decision_ids=[],
        target=ID,
        force=force,
        escalation_override=override,
        socket_path=SOCKET,
        pid=4242,
        grace_ms=grace_ms,
        result=CLOSE_ERROR,
    )
    validate(record, "termctl/audit.json")
    assert set(record["inputs"]) == {"socket_path", "pid", "grace_ms"}


def test_audit_close_needs_force_or_override() -> None:
    with pytest.raises(ValueError, match="force"):
        jsonlog.audit_close_record(
            TS,
            actor="human:x",
            decision_ids=[],
            target=ID,
            force=False,
            escalation_override=False,
            socket_path=SOCKET,
            pid=None,
            grace_ms=None,
            result=CLOSE_ERROR,
        )


def test_audit_close_grace_only_when_forced() -> None:
    with pytest.raises(ValueError, match="grace_ms"):
        jsonlog.audit_close_record(
            TS,
            actor="human:x",
            decision_ids=[],
            target=ID,
            force=False,
            escalation_override=True,
            socket_path=SOCKET,
            pid=None,
            grace_ms=100,
            result=CLOSE_ERROR,
        )


def test_error_envelope_is_an_audit_result() -> None:
    err = TermctlError("vetoed", "vetoed", hook="h", hook_stderr="why", decision_ids=["d"])
    validate(audit_open(result=err.envelope()), "termctl/audit.json")


# --- writing --------------------------------------------------------------------------------


def test_append_log_writes_one_line_per_record(environ: dict[str, str], tmp_path: Path) -> None:
    first = open_record()
    second = jsonlog.log_record(TS, event="mark", result="ok", id=ID, socket_path=SOCKET)
    jsonlog.append_log(environ, first)
    jsonlog.append_log(environ, second)
    records = lines(tmp_path / "state" / "termctl" / "log.jsonl")
    assert records == [first, second]
    for record in records:
        validate(record, "termctl/log.json")


def test_append_audit_writes_audit_jsonl(environ: dict[str, str], tmp_path: Path) -> None:
    record = audit_open()
    jsonlog.append_audit(environ, record)
    assert lines(tmp_path / "state" / "termctl" / "audit.jsonl") == [record]
    assert not (tmp_path / "state" / "termctl" / "log.jsonl").exists()


def test_hostile_strings_stay_on_one_line(environ: dict[str, str], tmp_path: Path) -> None:
    hostile = 'a\nb\r  \x00\x9b"}\n{"forged": 1}\udcff🔔'
    record = jsonlog.log_record(
        TS, event="warning", result="warning", id=None, socket_path=None, message=hostile
    )
    jsonlog.append_log(environ, record)
    raw = (tmp_path / "state" / "termctl" / "log.jsonl").read_bytes()
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    assert raw.isascii()
    (parsed,) = lines(tmp_path / "state" / "termctl" / "log.jsonl")
    assert parsed["message"].startswith('a\nb\r  \x00\x9b"}\n{"forged": 1}')
    assert parsed["message"].endswith("\\xff🔔")
    validate(parsed, "termctl/log.json")


def test_large_record_is_written_whole(environ: dict[str, str], tmp_path: Path) -> None:
    record = jsonlog.log_record(
        TS, event="warning", result="warning", id=None, socket_path=None, message="x" * 1_000_000
    )
    jsonlog.append_log(environ, record)
    assert lines(tmp_path / "state" / "termctl" / "log.jsonl") == [record]


def test_existing_log_is_appended_not_truncated(environ: dict[str, str], tmp_path: Path) -> None:
    log = tmp_path / "state" / "termctl" / "log.jsonl"
    log.parent.parent.mkdir()
    log.parent.mkdir(mode=0o700)
    log.write_text('{"old": true}\n')
    log.chmod(0o600)
    jsonlog.append_log(environ, open_record())
    assert lines(log)[0] == {"old": True}
    assert len(lines(log)) == 2


def test_symlinked_log_is_refused(environ: dict[str, str], tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "termctl").mkdir(mode=0o700)
    (tmp_path / "state" / "termctl" / "log.jsonl").symlink_to(victim)
    with pytest.raises(StateDirError):
        jsonlog.append_log(environ, open_record())
    assert victim.read_text() == "keep\n"


def _append_many(environ: dict[str, str], worker: int, count: int) -> None:
    for i in range(count):
        record = jsonlog.log_record(
            TS,
            event="warning",
            result="warning",
            id=None,
            socket_path=None,
            message=f"{worker}:{i}:" + "y" * 5000,
        )
        jsonlog.append_log(environ, record)


def test_concurrent_writers_never_interleave(environ: dict[str, str], tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    workers = [
        context.Process(target=_append_many, args=(environ, worker, 50)) for worker in range(4)
    ]
    for proc in workers:
        proc.start()
    for proc in workers:
        proc.join(60)
        assert proc.exitcode == 0
    records = lines(tmp_path / "state" / "termctl" / "log.jsonl")
    assert len(records) == 200
    assert {r["message"].rsplit(":", 1)[0] for r in records} == {
        f"{w}:{i}" for w in range(4) for i in range(50)
    }
