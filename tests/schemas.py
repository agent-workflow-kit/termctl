"""Validate instances against the schemas in ``schemas/``, resolved by ``$id``."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError, validators
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def load_schemas() -> dict[str, dict[str, Any]]:
    """Every schema in ``schemas/``, keyed by its ``$id``."""
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(SCHEMA_DIR.rglob("*.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        schemas[contents["$id"]] = contents
    return schemas


SCHEMAS = load_schemas()
REGISTRY: Registry[Any] = Registry().with_resources(
    (schema_id, Resource.from_contents(contents)) for schema_id, contents in SCHEMAS.items()
)


def ecma_pattern(pattern: str) -> str:
    """Rewrite ``pattern`` so ``$`` means end of string, as in ECMA-262.

    Python's ``$`` also matches before a final newline; ``\\Z`` does not. Escaped
    ``$`` and ``$`` inside a character class are literal and left alone.
    """
    out: list[str] = []
    in_class = False
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\":
            out.append(pattern[i : i + 2])
            i += 2
            continue
        if in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif char == "$":
            char = "\\Z"
        out.append(char)
        i += 1
    return "".join(out)


def _ecma_pattern_keyword(
    validator: Any, pattern: str, instance: object, schema: dict[str, Any]
) -> Iterator[ValidationError]:
    if isinstance(instance, str) and not re.search(ecma_pattern(pattern), instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


EcmaValidator = validators.extend(Draft202012Validator, {"pattern": _ecma_pattern_keyword})


def validate(instance: object, schema_id: str) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` matches ``schema_id``."""
    EcmaValidator({"$ref": schema_id}, registry=REGISTRY).validate(instance)
