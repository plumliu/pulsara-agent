"""Shared immutable context primitives.

This private module exists solely to keep ``primitives.context`` and
``primitives.tool_result`` acyclic.  Public callers import these names from
``pulsara_agent.primitives.context``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class FrozenContextFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolArgumentsParseErrorCode(StrEnum):
    INVALID_JSON_SYNTAX = "invalid_json_syntax"
    JSON_ROOT_NOT_OBJECT = "json_root_not_object"


FrozenJsonScalar: TypeAlias = str | int | float | bool | None


class FrozenJsonArrayFact(FrozenContextFact):
    items: tuple["FrozenJsonValue", ...]

    @field_validator("items")
    @classmethod
    def _finite_items(cls, value: tuple["FrozenJsonValue", ...]):
        _reject_non_finite(value)
        return value


class FrozenJsonEntryFact(FrozenContextFact):
    key: str
    value: "FrozenJsonValue"


class FrozenJsonObjectFact(FrozenContextFact):
    entries: tuple[FrozenJsonEntryFact, ...]

    @model_validator(mode="after")
    def _ordered_unique(self) -> "FrozenJsonObjectFact":
        keys = tuple(entry.key for entry in self.entries)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("frozen JSON object keys must be sorted and unique")
        _reject_non_finite(self.entries)
        return self


FrozenJsonValue: TypeAlias = (
    FrozenJsonScalar | FrozenJsonArrayFact | FrozenJsonObjectFact
)


def _reject_non_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON floats must be finite")
    if isinstance(value, FrozenJsonArrayFact):
        for item in value.items:
            _reject_non_finite(item)
    elif isinstance(value, FrozenJsonObjectFact):
        for entry in value.entries:
            _reject_non_finite(entry.value)
    elif isinstance(value, tuple):
        for item in value:
            _reject_non_finite(item)


def freeze_json(value: object) -> FrozenJsonValue:
    """Recursively freeze a strict JSON value into immutable typed facts."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON floats must be finite")
        return value
    if isinstance(value, (list, tuple)):
        return FrozenJsonArrayFact(items=tuple(freeze_json(item) for item in value))
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return FrozenJsonObjectFact(
            entries=tuple(
                FrozenJsonEntryFact(key=key, value=freeze_json(value[key]))
                for key in sorted(value)
            )
        )
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def thaw_json(value: FrozenJsonValue) -> Any:
    """Return a new mutable JSON-compatible object."""

    if isinstance(value, FrozenJsonArrayFact):
        return [thaw_json(item) for item in value.items]
    if isinstance(value, FrozenJsonObjectFact):
        return {entry.key: thaw_json(entry.value) for entry in value.entries}
    return value


def canonical_utc_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_value(value: object) -> object:
    if isinstance(value, FrozenJsonArrayFact | FrozenJsonObjectFact):
        return thaw_json(value)
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON floats must be finite")
    return value


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def context_fingerprint(namespace: str, value: object) -> str:
    digest = sha256()
    digest.update(namespace.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(value))
    return f"sha256:{digest.hexdigest()}"


FrozenJsonArrayFact.model_rebuild()
FrozenJsonEntryFact.model_rebuild()
FrozenJsonObjectFact.model_rebuild()


__all__ = [
    "FrozenContextFact",
    "FrozenJsonArrayFact",
    "FrozenJsonEntryFact",
    "FrozenJsonObjectFact",
    "FrozenJsonScalar",
    "FrozenJsonValue",
    "canonical_json_bytes",
    "canonical_utc_timestamp",
    "context_fingerprint",
    "freeze_json",
    "thaw_json",
]
