"""Shared immutable contracts and deterministic fingerprint validation."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, model_validator

from pulsara_agent.primitives._context_base import context_fingerprint


FingerprintJoinKind = Literal[
    "nested_path",
    "hydrated_reference",
    "transaction_context",
]
FingerprintProofOwner = Literal[
    "dto_validator",
    "artifact_hydrator",
    "postgres_writer",
]


@dataclass(frozen=True, slots=True)
class DurableFingerprintJoinSpec:
    """Declares where a copied fingerprint is authoritatively checked."""

    joined_field: str
    join_kind: FingerprintJoinKind
    source_path: tuple[str, ...] | None
    proof_owner: FingerprintProofOwner

    def __post_init__(self) -> None:
        if not self.joined_field:
            raise ValueError("joined fingerprint field must be non-empty")
        if self.join_kind == "nested_path":
            if not self.source_path:
                raise ValueError("nested fingerprint join requires source_path")
            if self.proof_owner != "dto_validator":
                raise ValueError("nested fingerprint join must be DTO-owned")
        elif self.source_path is not None:
            raise ValueError("external fingerprint join cannot declare source_path")


@dataclass(frozen=True, slots=True)
class DurableFactFingerprintSpec:
    """The single own-fingerprint rule for one concrete durable schema."""

    schema_version: str
    own_fingerprint_field: str | None
    domain_separator: str
    joined_fingerprints: tuple[DurableFingerprintJoinSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.schema_version or not self.domain_separator:
            raise ValueError("fingerprint spec identity must be non-empty")
        names = tuple(item.joined_field for item in self.joined_fingerprints)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("fingerprint joins must be sorted and unique")


class DurableFactFingerprintRegistry:
    """Process registry for exact durable schema fingerprint contracts."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._by_schema: dict[str, DurableFactFingerprintSpec] = {}

    def register(self, spec: DurableFactFingerprintSpec) -> None:
        with self._lock:
            existing = self._by_schema.get(spec.schema_version)
            if existing is not None and existing != spec:
                raise ValueError(
                    f"durable fingerprint spec conflict: {spec.schema_version}"
                )
            self._by_schema[spec.schema_version] = spec

    def resolve(self, schema_version: str) -> DurableFactFingerprintSpec:
        with self._lock:
            try:
                return self._by_schema[schema_version]
            except KeyError as exc:
                raise ValueError(
                    f"durable fingerprint schema is unregistered: {schema_version}"
                ) from exc

    def validate(self, fact: "FrozenFactBase") -> None:
        schema_version = getattr(fact, "schema_version", None)
        if not isinstance(schema_version, str) or not schema_version:
            raise ValueError("durable fact requires schema_version")
        spec = self.resolve(schema_version)
        own_field = spec.own_fingerprint_field
        model_fields = type(fact).model_fields
        if own_field is not None:
            if own_field not in model_fields:
                raise ValueError(
                    f"registered own fingerprint field is absent: {own_field}"
                )
            expected = context_fingerprint(
                spec.domain_separator,
                fact.model_dump(mode="json", exclude={own_field}),
            )
            if getattr(fact, own_field) != expected:
                raise ValueError(f"{schema_version} own fingerprint mismatch")
        self._validate_nested_joins(fact, spec)

    @staticmethod
    def _validate_nested_joins(
        fact: "FrozenFactBase",
        spec: DurableFactFingerprintSpec,
    ) -> None:
        for join in spec.joined_fingerprints:
            if join.join_kind != "nested_path":
                continue
            nested: object = fact
            assert join.source_path is not None
            for item in join.source_path:
                nested = getattr(nested, item)
            if getattr(fact, join.joined_field) != nested:
                raise ValueError(
                    f"{spec.schema_version} fingerprint join mismatch: "
                    f"{join.joined_field}"
                )

    def snapshot(self) -> tuple[DurableFactFingerprintSpec, ...]:
        with self._lock:
            return tuple(self._by_schema[key] for key in sorted(self._by_schema))


DURABLE_FACT_FINGERPRINT_REGISTRY = DurableFactFingerprintRegistry()


_FrozenFactT = TypeVar("_FrozenFactT", bound="FrozenFactBase")


class FrozenFactBase(BaseModel):
    """Base for immutable event-safe facts registered by schema version."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    _skip_fingerprint_validation: ClassVar[bool] = False

    @model_validator(mode="after")
    def _validate_registered_fingerprint(self) -> "FrozenFactBase":
        if not self._skip_fingerprint_validation:
            DURABLE_FACT_FINGERPRINT_REGISTRY.validate(self)
        return self


class FrozenRuntimeStateBase(BaseModel):
    """Immutable process-local state that must never be serialized as authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class StableEventIdentityFact(FrozenFactBase):
    schema_version: Literal["stable_event_identity.v2"]
    runtime_session_id: str
    event_id: str
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: str
    payload_fingerprint: str
    identity_fingerprint: str


DURABLE_FACT_FINGERPRINT_REGISTRY.register(
    DurableFactFingerprintSpec(
        schema_version="stable_event_identity.v2",
        own_fingerprint_field="identity_fingerprint",
        domain_separator="stable-event-identity:v2",
    )
)


@dataclass(frozen=True, slots=True)
class PreparedRuntimeValueBase:
    """Marker for process-local prepared values."""


def register_durable_fact(
    *,
    schema_version: str,
    own_fingerprint_field: str | None,
    domain_separator: str,
    joined_fingerprints: tuple[DurableFingerprintJoinSpec, ...] = (),
) -> None:
    """Register one concrete durable schema at module import time."""

    DURABLE_FACT_FINGERPRINT_REGISTRY.register(
        DurableFactFingerprintSpec(
            schema_version=schema_version,
            own_fingerprint_field=own_fingerprint_field,
            domain_separator=domain_separator,
            joined_fingerprints=joined_fingerprints,
        )
    )


def build_frozen_fact(
    fact_type: type[_FrozenFactT],
    /,
    **payload: Any,
) -> _FrozenFactT:
    """Build one registered fact from its canonical own-fingerprint contract."""

    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        raise ValueError("durable fact factory requires schema_version")
    spec = DURABLE_FACT_FINGERPRINT_REGISTRY.resolve(schema_version)
    own_field = spec.own_fingerprint_field
    if own_field is None:
        return fact_type(**payload)
    if own_field in payload:
        raise ValueError("durable fact factory owns the fingerprint field")
    provisional = fact_type.model_construct(
        **payload,
        **{own_field: "pending"},
    )
    payload[own_field] = context_fingerprint(
        spec.domain_separator,
        provisional.model_dump(mode="json", exclude={own_field}),
    )
    return fact_type(**payload)


__all__ = [
    "DURABLE_FACT_FINGERPRINT_REGISTRY",
    "DurableFactFingerprintRegistry",
    "DurableFactFingerprintSpec",
    "DurableFingerprintJoinSpec",
    "FingerprintJoinKind",
    "FingerprintProofOwner",
    "FrozenFactBase",
    "FrozenRuntimeStateBase",
    "PreparedRuntimeValueBase",
    "StableEventIdentityFact",
    "build_frozen_fact",
    "register_durable_fact",
]
