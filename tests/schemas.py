"""Validate instances against the schemas in ``schemas/``, resolved by ``$id``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
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


def validate(instance: object, schema_id: str) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` matches ``schema_id``."""
    Draft202012Validator({"$ref": schema_id}, registry=REGISTRY).validate(instance)
