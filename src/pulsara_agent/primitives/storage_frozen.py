"""Registered immutable facts for durable storage-only records.

Storage facts are deliberately disjoint from :class:`FrozenFactBase`.  They
may contain encrypted bytes and are accepted only by a typed storage codec;
they are never valid AgentEvent payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, model_validator

from pulsara_agent.primitives._context_base import context_fingerprint


@dataclass(frozen=True, slots=True)
class DurableStorageFactFingerprintSpec:
    schema_version: str
    own_fingerprint_field: str
    domain_separator: str

    def __post_init__(self) -> None:
        if not self.schema_version or not self.own_fingerprint_field:
            raise ValueError("storage fingerprint spec identity is required")
        if not self.domain_separator:
            raise ValueError("storage fingerprint domain separator is required")


class DurableStorageFactFingerprintRegistry:
    """Registry kept separate from event-safe durable fact schemas."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._by_schema: dict[str, DurableStorageFactFingerprintSpec] = {}

    def register(self, spec: DurableStorageFactFingerprintSpec) -> None:
        with self._lock:
            existing = self._by_schema.get(spec.schema_version)
            if existing is not None and existing != spec:
                raise ValueError(
                    f"storage fingerprint spec conflict: {spec.schema_version}"
                )
            self._by_schema[spec.schema_version] = spec

    def resolve(self, schema_version: str) -> DurableStorageFactFingerprintSpec:
        with self._lock:
            try:
                return self._by_schema[schema_version]
            except KeyError as exc:
                raise ValueError(
                    f"storage fingerprint schema is unregistered: {schema_version}"
                ) from exc

    def validate(self, fact: "FrozenStorageFactBase") -> None:
        schema_version = getattr(fact, "schema_version", None)
        if not isinstance(schema_version, str) or not schema_version:
            raise ValueError("storage fact requires schema_version")
        spec = self.resolve(schema_version)
        if spec.own_fingerprint_field not in type(fact).model_fields:
            raise ValueError("registered storage fingerprint field is absent")
        payload = fact.model_dump(mode="python", exclude={spec.own_fingerprint_field})
        expected = context_fingerprint(
            spec.domain_separator,
            _canonical_storage_value(payload),
        )
        if getattr(fact, spec.own_fingerprint_field) != expected:
            raise ValueError(f"{schema_version} storage fingerprint mismatch")

    def snapshot(self) -> tuple[DurableStorageFactFingerprintSpec, ...]:
        with self._lock:
            return tuple(self._by_schema[key] for key in sorted(self._by_schema))


DURABLE_STORAGE_FACT_FINGERPRINT_REGISTRY = (
    DurableStorageFactFingerprintRegistry()
)


class FrozenStorageFactBase(BaseModel):
    """Immutable durable record accepted only by a typed storage repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    _skip_storage_fingerprint_validation: ClassVar[bool] = False

    @model_validator(mode="after")
    def _validate_registered_storage_fingerprint(self) -> "FrozenStorageFactBase":
        if not self._skip_storage_fingerprint_validation:
            DURABLE_STORAGE_FACT_FINGERPRINT_REGISTRY.validate(self)
        return self


_StorageFactT = TypeVar("_StorageFactT", bound=FrozenStorageFactBase)


def register_durable_storage_fact(
    *,
    schema_version: str,
    own_fingerprint_field: str,
    domain_separator: str,
) -> None:
    DURABLE_STORAGE_FACT_FINGERPRINT_REGISTRY.register(
        DurableStorageFactFingerprintSpec(
            schema_version=schema_version,
            own_fingerprint_field=own_fingerprint_field,
            domain_separator=domain_separator,
        )
    )


def build_frozen_storage_fact(
    fact_type: type[_StorageFactT],
    /,
    **payload: Any,
) -> _StorageFactT:
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        raise ValueError("storage fact factory requires schema_version")
    spec = DURABLE_STORAGE_FACT_FINGERPRINT_REGISTRY.resolve(schema_version)
    if spec.own_fingerprint_field in payload:
        raise ValueError("storage fact factory owns the fingerprint field")
    provisional = fact_type.model_construct(
        **payload,
        **{spec.own_fingerprint_field: "pending"},
    )
    canonical = _canonical_storage_value(
        provisional.model_dump(
            mode="python",
            exclude={spec.own_fingerprint_field},
        )
    )
    payload[spec.own_fingerprint_field] = context_fingerprint(
        spec.domain_separator,
        canonical,
    )
    return fact_type(**payload)


def _canonical_storage_value(value: object) -> object:
    """Return a canonical JSON-compatible value without exposing raw bytes."""

    if isinstance(value, bytes):
        return {"$bytes_hex": value.hex()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return _canonical_storage_value(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {
            str(key): _canonical_storage_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_storage_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported storage fact value: {type(value).__name__}")


__all__ = [
    "DURABLE_STORAGE_FACT_FINGERPRINT_REGISTRY",
    "DurableStorageFactFingerprintRegistry",
    "DurableStorageFactFingerprintSpec",
    "FrozenStorageFactBase",
    "build_frozen_storage_fact",
    "register_durable_storage_fact",
]
