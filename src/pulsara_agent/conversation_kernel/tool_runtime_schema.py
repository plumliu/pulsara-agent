"""Builtin argument validation and frozen schema lowering."""

from __future__ import annotations

from typing import Mapping
from jsonschema import validators


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
