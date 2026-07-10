"""Recursive immutable JSON-like values for durable subagent facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from types import MappingProxyType
from typing import Any


def freeze_json_value(value: Any) -> Any:
    """Return a recursively immutable, detached representation of JSON-like data."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_json_value(item) for item in value)
    return value


def freeze_json_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen = freeze_json_value(value)
    assert isinstance(frozen, Mapping)
    return frozen


def thaw_json_value(value: Any) -> Any:
    """Return an ordinary JSON-serializable copy of a frozen fact value."""

    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: thaw_json_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [thaw_json_value(item) for item in sorted(value, key=repr)]
    if isinstance(value, Enum):
        return thaw_json_value(value.value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


def thaw_json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    thawed = thaw_json_value(value)
    assert isinstance(thawed, dict)
    return thawed
