from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from termctl.errors import EXIT_CODES, TermctlError, UsageError

from .schemas import SCHEMAS, validate

# interface.md "Errors": code -> exit.
INTERFACE_EXIT_CODES = {
    "usage": 2,
    "config_invalid": 2,
    "not_supported": 2,
    "not_found": 10,
    "no_server": 10,
    "client_not_found": 10,
    "vetoed": 11,
    "tmux_missing": 12,
    "tmux_too_old": 12,
    "server_unavailable": 12,
    "command_not_found": 12,
    "tag_failed": 12,
    "no_client": 12,
    "attach_failed": 12,
    "locked": 12,
    "state_changed": 12,
    "escalated": 13,
    "refused": 20,
    "ambiguous": 21,
    "internal": 1,
}

# The extra fields each code requires in the envelope (interface.md "Errors").
REQUIRED_EXTRAS: dict[str, dict[str, Any]] = {
    "refused": {"reason": "running"},
    "vetoed": {"hook": "policy-check", "hook_stderr": "denied"},
    "escalated": {"hook": "policy-check", "hook_stderr": "ask a human"},
    "ambiguous": {
        "candidates": [
            {"id": "tc-aaaaaaaa", "pane_id": "%1", "session": "termctl"},
            {"id": "tc-aaaaaaab", "pane_id": "%2", "session": "termctl"},
        ]
    },
}


def test_every_schema_is_a_valid_schema() -> None:
    for schema in SCHEMAS.values():
        Draft202012Validator.check_schema(schema)


def test_exit_codes_match_interface_table() -> None:
    assert EXIT_CODES == INTERFACE_EXIT_CODES


def test_exit_codes_cover_exactly_the_schema_codes() -> None:
    schema_codes = SCHEMAS["termctl/cli/error.json"]["properties"]["error"]["properties"]["code"]
    assert set(EXIT_CODES) == set(schema_codes["enum"])


def test_no_tool_specific_exit_codes() -> None:
    assert set(EXIT_CODES.values()) <= {1, 2, 10, 11, 12, 13, 20, 21}


@pytest.mark.parametrize("code", sorted(INTERFACE_EXIT_CODES))
def test_error_exit_code_and_envelope(code: str) -> None:
    err = TermctlError(code, f"{code} happened", **REQUIRED_EXTRAS.get(code, {}))
    assert err.exit_code == INTERFACE_EXIT_CODES[code]
    envelope = err.envelope()
    assert envelope["error"]["code"] == code
    assert envelope["error"]["message"] == f"{code} happened"
    validate(envelope, "termctl/cli/error.json")


def test_envelope_omits_absent_fields() -> None:
    assert TermctlError("not_found", "no such pane").envelope() == {
        "error": {"code": "not_found", "message": "no such pane"}
    }


def test_envelope_carries_decision_ids_and_details() -> None:
    err = TermctlError(
        "vetoed",
        "a pre-open hook vetoed",
        hook="policy-check",
        hook_stderr="no",
        decision_ids=["d-1"],
        details={"hook_name": "pre-open"},
    )
    envelope = err.envelope()
    assert envelope["error"]["decision_ids"] == ["d-1"]
    assert envelope["error"]["details"] == {"hook_name": "pre-open"}
    validate(envelope, "termctl/cli/error.json")


def test_unknown_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown error code"):
        TermctlError("nope", "x")


def test_usage_error_is_code_usage() -> None:
    err = UsageError("bad flag")
    assert (err.code, err.exit_code, str(err)) == ("usage", 2, "bad flag")
