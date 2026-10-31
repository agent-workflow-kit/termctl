from __future__ import annotations

import pytest
from jsonschema import ValidationError

from tests.schemas import ecma_pattern, validate

MARKER_NAME = "termctl/defs.json#/$defs/marker_name"


def test_anchored_pattern_accepts_plain_value() -> None:
    validate("build-1", MARKER_NAME)


def test_anchored_pattern_rejects_trailing_newline() -> None:
    # JSON Schema patterns are ECMA-262: '$' matches only at the end of the string.
    with pytest.raises(ValidationError):
        validate("build-1\n", MARKER_NAME)


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("^a$", "^a\\Z"),
        ("^(?!.*#$)x$", "^(?!.*#\\Z)x\\Z"),
        ("^\\$[$]$", "^\\$[$]\\Z"),
        ("^[\\]$]$", "^[\\]$]\\Z"),
    ],
)
def test_ecma_pattern_rewrites_only_end_anchors(pattern: str, expected: str) -> None:
    assert ecma_pattern(pattern) == expected


@pytest.mark.parametrize(
    "instance",
    [
        {"server_env_allow": ["PATH\n"]},
        {"rules": [{"match": {"kind": "review\n"}, "placement": "default"}]},
        {"rules": [{"match": {"kind\n": "review"}, "placement": "default"}]},
    ],
)
def test_anchored_patterns_apply_through_refs(instance: object) -> None:
    # Following "$ref" into a schema with "$schema" must keep the ECMA-262 '$'.
    with pytest.raises(ValidationError):
        validate(instance, "termctl/config.json")
