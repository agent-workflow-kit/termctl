from __future__ import annotations

import pytest
from jsonschema import ValidationError

from tests.schemas import SCHEMAS, ecma_pattern, validate

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


# interface.md "Commands", pane object: the fields read from pane options that anything with
# socket access can write.
UNTRUSTED_PANE_FIELDS = {"name", "labels", "marker", "marker_text", "placement", "pid"}


def test_pane_schema_marks_the_option_derived_fields_untrusted() -> None:
    properties = SCHEMAS["termctl/cli/pane.json"]["properties"]
    marked = {name for name, schema in properties.items() if schema.get("x-untrusted") is True}
    assert marked == UNTRUSTED_PANE_FIELDS


def test_display_name_is_untrusted_through_its_definition() -> None:
    properties = SCHEMAS["termctl/cli/pane.json"]["properties"]
    assert properties["display_name"] == {"$ref": "../defs.json#/$defs/display_name"}
    assert SCHEMAS["termctl/defs.json"]["$defs"]["display_name"]["x-untrusted"] is True


@pytest.mark.parametrize(
    ("schema_id", "path"),
    [("termctl/cli/show.json", ("$ref",)), ("termctl/cli/list.json", ("properties", "panes"))],
)
def test_show_and_list_use_the_pane_object(schema_id: str, path: tuple[str, ...]) -> None:
    node: object = SCHEMAS[schema_id]
    for key in path:
        assert isinstance(node, dict)
        node = node[key]
    assert node in ("pane.json", {"type": "array", "items": {"$ref": "pane.json"}})
