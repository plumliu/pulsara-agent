"""Shared helpers for built-in tool schemas and argument validation."""

from __future__ import annotations

import json
from typing import Any


DEFAULT_MAX_OUTPUT_CHARS = 32_000
MIN_TERMINAL_OUTPUT_CHARS = 512


def object_schema(*, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def str_arg(args: dict[str, Any], name: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def required_str_arg(args: dict[str, Any], name: str) -> str:
    value = str_arg(args, name)
    if value is None or not value:
        raise ValueError(f"{name} is required")
    return value


def int_arg(args: dict[str, Any], name: str, default: int) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def bounded_int_arg(
    args: dict[str, Any],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = int_arg(args, name, default)
    if value <= 0:
        return default
    return max(minimum, min(value, maximum))


def bool_arg(args: dict[str, Any], name: str, default: bool) -> bool:
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
