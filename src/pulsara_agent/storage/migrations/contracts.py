"""Canonical, secret-safe contracts for PostgreSQL schema ownership."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Literal, TypeAlias


_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")


CanonicalScalar: TypeAlias = type(None) | bool | int | str
CanonicalValue: TypeAlias = (
    CanonicalScalar | tuple["CanonicalValue", ...] | dict[str, "CanonicalValue"]
)


def _canonical_value(value: object) -> CanonicalValue:
    if isinstance(value, StrEnum):
        raise TypeError(
            "schema enums must be converted to their canonical string value"
        )
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float | bytes | bytearray | datetime):
        raise TypeError(f"non-canonical schema value: {type(value).__name__}")
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, tuple):
        return tuple(_canonical_value(item) for item in value)
    if isinstance(value, list | set | frozenset):
        raise TypeError(
            "schema fingerprints require ordered tuples, not mutable/unordered collections"
        )
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("schema fingerprint object keys must be strings")
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    raise TypeError(f"unsupported schema fingerprint value: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    canonical = _canonical_value(value)
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def postgres_schema_fingerprint(domain: str, value: object) -> str:
    if not domain:
        raise ValueError("schema fingerprint domain must be non-empty")
    digest = sha256(domain.encode("utf-8") + b"\0" + canonical_json_bytes(value))
    return f"sha256:{digest.hexdigest()}"


def require_fingerprint(value: str, *, field_name: str) -> str:
    if _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be sha256:<64 lowercase hex>")
    return value


def require_checksum(value: str) -> str:
    if _CHECKSUM_RE.fullmatch(value) is None:
        raise ValueError("resource checksum must be 64 lowercase hex")
    return value


@dataclass(frozen=True, slots=True)
class PostgresObjectIdentityFact:
    object_kind: str
    schema_name: str
    object_name: str
    identity_fingerprint: str

    @classmethod
    def build(
        cls, *, object_kind: str, schema_name: str, object_name: str
    ) -> "PostgresObjectIdentityFact":
        payload = {
            "object_kind": object_kind,
            "schema_name": schema_name,
            "object_name": object_name,
        }
        return cls(
            **payload,
            identity_fingerprint=postgres_schema_fingerprint(
                "pulsara:postgres-object-identity:v1", payload
            ),
        )


@dataclass(frozen=True, slots=True)
class PostgresMigrationLedgerRowFact:
    schema_version: Literal["postgres_migration_ledger_row.v1"]
    version: int
    name: str
    resource_checksum: str
    migration_contract_fingerprint: str
    registry_prefix_fingerprint: str
    application_version: str
    applied_at_utc: str

    def __post_init__(self) -> None:
        if self.version < 0 or not self.name or not self.application_version:
            raise ValueError("invalid migration ledger row")
        require_checksum(self.resource_checksum)
        require_fingerprint(
            self.migration_contract_fingerprint,
            field_name="migration_contract_fingerprint",
        )
        require_fingerprint(
            self.registry_prefix_fingerprint,
            field_name="registry_prefix_fingerprint",
        )
        parsed = datetime.fromisoformat(self.applied_at_utc.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(
            parsed
        ):
            raise ValueError("applied_at_utc must be an UTC RFC 3339 timestamp")


@dataclass(frozen=True, slots=True)
class PostgresSchemaObjectManifest:
    schema_version: Literal["postgres_schema_object_manifest.v1"]
    through_version: int
    required_extensions: tuple[dict[str, CanonicalValue], ...]
    required_types: tuple[dict[str, CanonicalValue], ...]
    owned_relations: tuple[dict[str, CanonicalValue], ...]
    required_functions: tuple[dict[str, CanonicalValue], ...]
    reserved_object_names: tuple[PostgresObjectIdentityFact, ...]
    manifest_fingerprint: str

    def __post_init__(self) -> None:
        if self.through_version < 0:
            raise ValueError("manifest through_version must be non-negative")
        require_fingerprint(
            self.manifest_fingerprint, field_name="manifest_fingerprint"
        )
        payload = {
            "schema_version": self.schema_version,
            "through_version": self.through_version,
            "required_extensions": self.required_extensions,
            "required_types": self.required_types,
            "owned_relations": self.owned_relations,
            "required_functions": self.required_functions,
            "reserved_object_names": self.reserved_object_names,
        }
        expected = postgres_schema_fingerprint(
            "pulsara:postgres-schema-object-manifest:v1", payload
        )
        if self.manifest_fingerprint != expected:
            raise ValueError("manifest fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class PostgresFastObservedCatalogFact:
    schema_version: Literal["postgres_fast_observed_catalog.v1"]
    server_version_num: int
    extensions: tuple[dict[str, CanonicalValue], ...]
    types: tuple[dict[str, CanonicalValue], ...]
    relation_execution_shapes: tuple[dict[str, CanonicalValue], ...]
    function_execution_shapes: tuple[dict[str, CanonicalValue], ...]
    fast_executable_schema_fingerprint: str

    def __post_init__(self) -> None:
        require_fingerprint(
            self.fast_executable_schema_fingerprint,
            field_name="fast_executable_schema_fingerprint",
        )
        expected = postgres_schema_fingerprint(
            "pulsara:postgres-fast-executable-schema:v1",
            {
                "extensions": tuple(
                    {
                        "schema_name": item["schema_name"],
                        "extension_name": item["extension_name"],
                    }
                    for item in self.extensions
                ),
                "types": self.types,
                "relation_execution_shapes": self.relation_execution_shapes,
                "function_execution_shapes": self.function_execution_shapes,
            },
        )
        if self.fast_executable_schema_fingerprint != expected:
            raise ValueError("fast observed catalog fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class PostgresDeepObservedCatalogFact:
    schema_version: Literal["postgres_deep_observed_catalog.v1"]
    fast_observed_catalog_fingerprint: str
    relations: tuple[dict[str, CanonicalValue], ...]
    functions: tuple[dict[str, CanonicalValue], ...]
    deep_catalog_fingerprint: str
    observed_catalog_fingerprint: str

    def __post_init__(self) -> None:
        require_fingerprint(
            self.fast_observed_catalog_fingerprint,
            field_name="fast_observed_catalog_fingerprint",
        )
        payload = {
            "schema_version": self.schema_version,
            "fast_observed_catalog_fingerprint": self.fast_observed_catalog_fingerprint,
            "relations": self.relations,
            "functions": self.functions,
        }
        expected_deep = postgres_schema_fingerprint(
            "pulsara:postgres-deep-catalog:v1", payload
        )
        if self.deep_catalog_fingerprint != expected_deep:
            raise ValueError("deep observed catalog fingerprint mismatch")
        expected_observed = postgres_schema_fingerprint(
            "pulsara:postgres-observed-catalog:v1",
            {
                "fast": self.fast_observed_catalog_fingerprint,
                "deep": expected_deep,
            },
        )
        if self.observed_catalog_fingerprint != expected_observed:
            raise ValueError("observed catalog fingerprint mismatch")


PostgresObservedCatalogFact: TypeAlias = (
    PostgresFastObservedCatalogFact | PostgresDeepObservedCatalogFact
)


@dataclass(frozen=True, slots=True)
class PostgresEffectivePrivilegeResult:
    runtime_role: str
    satisfied_requirement_fingerprints: tuple[str, ...]
    missing_requirement_fingerprints: tuple[str, ...]
    runtime_role_can_create_in_public_schema: bool
    result_fingerprint: str

    def __post_init__(self) -> None:
        payload = {
            "runtime_role": self.runtime_role,
            "satisfied_requirement_fingerprints": self.satisfied_requirement_fingerprints,
            "missing_requirement_fingerprints": self.missing_requirement_fingerprints,
            "runtime_role_can_create_in_public_schema": (
                self.runtime_role_can_create_in_public_schema
            ),
        }
        if self.result_fingerprint != postgres_schema_fingerprint(
            "pulsara:postgres-effective-privilege-result:v1", payload
        ):
            raise ValueError("effective privilege result fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class PostgresFastSchemaVerificationResult:
    binding_fingerprint: str
    ordered_ledger_rows: tuple[PostgresMigrationLedgerRowFact, ...]
    expected_registry_prefix_fingerprint: str
    observed_registry_prefix_fingerprint: str
    expected_fast_executable_schema_fingerprint: str
    observed_fast_executable_schema_fingerprint: str
    effective_privilege_result: PostgresEffectivePrivilegeResult
    result_fingerprint: str

    def __post_init__(self) -> None:
        payload = {
            "binding_fingerprint": self.binding_fingerprint,
            "ordered_ledger_rows": self.ordered_ledger_rows,
            "expected_registry_prefix_fingerprint": self.expected_registry_prefix_fingerprint,
            "observed_registry_prefix_fingerprint": self.observed_registry_prefix_fingerprint,
            "expected_fast_executable_schema_fingerprint": self.expected_fast_executable_schema_fingerprint,
            "observed_fast_executable_schema_fingerprint": self.observed_fast_executable_schema_fingerprint,
            "effective_privilege_result": self.effective_privilege_result,
        }
        if self.result_fingerprint != postgres_schema_fingerprint(
            "pulsara:postgres-fast-schema-verification-result:v1", payload
        ):
            raise ValueError("fast verification result fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class PostgresDeepSchemaVerificationResult:
    nested_fast_result_fingerprint: str
    expected_object_manifest_fingerprint: str
    expected_deep_catalog_fingerprint: str
    observed_deep_catalog_fingerprint: str
    unexpected_object_fingerprints: tuple[str, ...]
    missing_object_fingerprints: tuple[str, ...]
    result_fingerprint: str

    def __post_init__(self) -> None:
        payload = {
            "nested_fast_result_fingerprint": self.nested_fast_result_fingerprint,
            "expected_object_manifest_fingerprint": self.expected_object_manifest_fingerprint,
            "expected_deep_catalog_fingerprint": self.expected_deep_catalog_fingerprint,
            "observed_deep_catalog_fingerprint": self.observed_deep_catalog_fingerprint,
            "unexpected_object_fingerprints": self.unexpected_object_fingerprints,
            "missing_object_fingerprints": self.missing_object_fingerprints,
        }
        if self.result_fingerprint != postgres_schema_fingerprint(
            "pulsara:postgres-deep-schema-verification-result:v1", payload
        ):
            raise ValueError("deep verification result fingerprint mismatch")


def canonical_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "CanonicalValue",
    "PostgresDeepObservedCatalogFact",
    "PostgresDeepSchemaVerificationResult",
    "PostgresEffectivePrivilegeResult",
    "PostgresFastObservedCatalogFact",
    "PostgresFastSchemaVerificationResult",
    "PostgresMigrationLedgerRowFact",
    "PostgresObjectIdentityFact",
    "PostgresObservedCatalogFact",
    "PostgresSchemaObjectManifest",
    "canonical_json_bytes",
    "canonical_utc",
    "postgres_schema_fingerprint",
    "require_checksum",
    "require_fingerprint",
]
