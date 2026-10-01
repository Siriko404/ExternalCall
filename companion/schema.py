"""Offline JSON Schema validation shared by preparation and response checking."""
from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing.exceptions import NoSuchResource

MAX_SCHEMA_NODES = 20_000
MAX_JSON_DEPTH = 80


def _deny_reference(uri: str):
    raise NoSuchResource(ref=uri)


def _bounded(value: Any, depth: int = 0, count: list[int] | None = None) -> None:
    count = [0] if count is None else count
    count[0] += 1
    if depth > MAX_JSON_DEPTH or count[0] > MAX_SCHEMA_NODES:
        raise ValueError("JSON exceeds the supported depth or node budget")
    values = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    for child in values:
        _bounded(child, depth + 1, count)


def validator(schema: dict[str, Any]) -> Draft202012Validator:
    _bounded(schema)
    draft = schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
    if draft != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("only JSON Schema Draft 2020-12 is supported")
    Draft202012Validator.check_schema(schema)
    # Only document-local references are supported. Never resolve a network URL.
    def check_refs(value):
        if isinstance(value, dict):
            for key in ("$ref", "$dynamicRef"):
                if key in value and not value[key].startswith("#"):
                    raise ValueError("schema references must be local fragments")
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)
    check_refs(schema)
    return Draft202012Validator(
        schema, registry=Registry(retrieve=_deny_reference), format_checker=FormatChecker()
    )


def errors(schema: dict[str, Any], value: Any) -> list[str]:
    _bounded(value)
    found = []
    for error in validator(schema).iter_errors(value):
        location = "/".join(str(part) for part in error.absolute_path) or "$"
        # Reports reach the browser as metadata. Never embed an offending answer value.
        found.append(f"{location}: failed schema keyword {error.validator}")
        if len(found) >= 100:
            found.append("Further schema errors omitted; fix these and validate again.")
            break
    return found
