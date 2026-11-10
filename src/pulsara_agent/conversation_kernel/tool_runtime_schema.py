"""Builtin argument validation and frozen schema lowering."""

from __future__ import annotations

import re
from typing import Mapping
from jsonschema import ValidationError, validators
from pulsara_agent.primitives.name_suggestion import suggest_name


def _tool_argument_error_message(error: ValidationError) -> str:
    """Render the validator's facts; suggestions never alter admission or arguments."""
    hints = {}
    pending = [error]
    while pending:
        item = pending.pop()
        pending.extend(reversed(item.context))
        if item.validator != "additionalProperties":
            continue
        properties = item.schema.get("properties", {})
        patterns = item.schema.get("patternProperties", {})
        for name in item.instance:
            if name in properties or any(re.search(pattern, name) for pattern in patterns):
                continue
            match = suggest_name(name, properties)
            if match is not None:
                hints[f"At {item.json_path}: unknown parameter {name!r}. Did you mean {match!r}?"] = None
    return " ".join((f"{error.json_path}: {error.message}", *hints))


def _validate_builtin_call_schema(
    tool_name: str, schema: dict[str, object], arguments: Mapping[str, object]
) -> None:
    validator = validators.validator_for(schema)
    validator.check_schema(schema)
    for error in validator(schema).iter_errors(dict(arguments)):
        # Display targets settle independently; only their outer call shape is
        # an admission failure. Keep the same structured schema on both wires.
        if (
            tool_name == "open_in_pulsara"
            and len(error.absolute_path) >= 2
            and error.absolute_path[0] == "targets"
            and isinstance(error.absolute_path[1], int)
        ):
            continue
        raise error


def _json_schema_value(value: Mapping[str, object]) -> dict[str, object]:
    """Lower recursively frozen catalog values to JSON Schema containers."""

    return {str(key): _thaw_json(item) for key, item in value.items()}


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value
