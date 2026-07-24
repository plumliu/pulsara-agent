"""Read-only exact-head PostgreSQL schema verifier."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from time import monotonic

import psycopg

from pulsara_agent.storage.migrations.catalog import PostgresCatalogCanonicalizer
from pulsara_agent.storage.migrations.contracts import (
    PostgresDeepSchemaVerificationResult,
    PostgresEffectivePrivilegeResult,
    PostgresFastSchemaVerificationResult,
    postgres_schema_fingerprint,
)
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.storage.migrations.grants import build_postgres_runtime_grant_policy
from pulsara_agent.storage.migrations.manifest import (
    POSTGRES_LATEST_SCHEMA_MANIFEST,
)
from pulsara_agent.storage.migrations.registry import (
    POSTGRES_MIGRATION_REGISTRY,
    PostgresMigrationRegistry,
)
from pulsara_agent.storage.migrations.runner import (
    _acquire_advisory_lock,
    _read_identity_from_connection,
    _runtime_role_can_create_in_public_schema,
    read_migration_ledger,
    requirement_satisfied,
)
from pulsara_agent.storage.schema_contract import (
    VerifiedPostgresSchemaBinding,
    build_verified_postgres_schema_binding,
)


@dataclass(frozen=True, slots=True)
class PostgresFastVerificationBundle:
    binding: VerifiedPostgresSchemaBinding
    result: PostgresFastSchemaVerificationResult


@dataclass(frozen=True, slots=True)
class PostgresDeepVerificationBundle:
    fast: PostgresFastVerificationBundle
    result: PostgresDeepSchemaVerificationResult


class PostgresMigrationHistoryStatus(StrEnum):
    UNMIGRATED = "unmigrated"
    BEHIND = "behind"
    UP_TO_DATE = "up_to_date"
    AHEAD = "ahead"
    CONFLICT = "conflict"


class PostgresSchemaVerifier:
    def __init__(
        self,
        *,
        registry: PostgresMigrationRegistry = POSTGRES_MIGRATION_REGISTRY,
        canonicalizer: PostgresCatalogCanonicalizer | None = None,
    ) -> None:
        self._registry = registry
        self._canonicalizer = canonicalizer or PostgresCatalogCanonicalizer()
        self._expected = _load_expected_catalog()

    def verify_fast_connection(
        self,
        connection: psycopg.Connection,
        *,
        database_target_fingerprint: str,
        deadline_monotonic: float,
    ) -> PostgresFastVerificationBundle:
        _require_remaining(deadline_monotonic)
        identity = _read_identity_from_connection(connection)
        _acquire_advisory_lock(
            connection,
            database_oid=identity.database_oid,
            shared=True,
            deadline_monotonic=deadline_monotonic,
        )
        rows = read_migration_ledger(connection)
        if rows is None:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.VERSION_BEHIND,
                "database is not migrated; run pulsara db migrate",
            )
        _validate_latest_history(rows, self._registry)
        observed = self._canonicalizer.read_fast(connection)
        expected_fast = str(self._expected["fast_executable_schema_fingerprint"])
        if observed.fast_executable_schema_fingerprint != expected_fast:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.CATALOG_DRIFT,
                "PostgreSQL executable schema differs from the packaged manifest",
            )
        extension_version = _require_pgvector(observed.extensions)
        policy = build_postgres_runtime_grant_policy(self._registry.latest_version)
        satisfied = tuple(
            requirement.requirement_fingerprint
            for requirement in policy.requirements
            if requirement_satisfied(connection, identity.runtime_role, requirement)
        )
        missing = tuple(
            requirement.requirement_fingerprint
            for requirement in policy.requirements
            if requirement.requirement_fingerprint not in set(satisfied)
        )
        can_create_in_public = _runtime_role_can_create_in_public_schema(
            connection, identity.runtime_role
        )
        privilege_payload = {
            "runtime_role": identity.runtime_role,
            "satisfied_requirement_fingerprints": satisfied,
            "missing_requirement_fingerprints": missing,
            "runtime_role_can_create_in_public_schema": can_create_in_public,
        }
        privileges = PostgresEffectivePrivilegeResult(
            **privilege_payload,
            result_fingerprint=postgres_schema_fingerprint(
                "pulsara:postgres-effective-privilege-result:v1",
                privilege_payload,
            ),
        )
        if missing:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.PRIVILEGE_MISSING,
                f"runtime role lacks {len(missing)} required privileges",
            )
        verification_contract = postgres_schema_fingerprint(
            "pulsara:postgres-fast-verification-contract:v1",
            {
                "registry_prefix_fingerprint": self._registry.registry_fingerprint,
                "expected_fast_executable_schema_fingerprint": expected_fast,
                "grant_policy_fingerprint": policy.policy_fingerprint,
            },
        )
        binding = build_verified_postgres_schema_binding(
            database_target_fingerprint=database_target_fingerprint,
            database_name=identity.database_name,
            database_oid=identity.database_oid,
            normalized_search_path=identity.normalized_search_path,
            runtime_role=identity.runtime_role,
            server_version_num=identity.server_version_num,
            pgvector_extension_version=extension_version,
            migration_head_version=self._registry.latest_version,
            durable_registry_prefix_fingerprint=rows[-1].registry_prefix_fingerprint,
            fast_executable_schema_fingerprint=observed.fast_executable_schema_fingerprint,
            verification_contract_fingerprint=verification_contract,
        )
        result_payload = {
            "binding_fingerprint": binding.binding_fingerprint,
            "ordered_ledger_rows": rows,
            "expected_registry_prefix_fingerprint": self._registry.registry_fingerprint,
            "observed_registry_prefix_fingerprint": rows[
                -1
            ].registry_prefix_fingerprint,
            "expected_fast_executable_schema_fingerprint": expected_fast,
            "observed_fast_executable_schema_fingerprint": observed.fast_executable_schema_fingerprint,
            "effective_privilege_result": privileges,
        }
        result = PostgresFastSchemaVerificationResult(
            **result_payload,
            result_fingerprint=postgres_schema_fingerprint(
                "pulsara:postgres-fast-schema-verification-result:v1",
                result_payload,
            ),
        )
        return PostgresFastVerificationBundle(binding=binding, result=result)

    def verify_deep_connection(
        self,
        connection: psycopg.Connection,
        *,
        database_target_fingerprint: str,
        deadline_monotonic: float,
    ) -> PostgresDeepVerificationBundle:
        fast = self.verify_fast_connection(
            connection,
            database_target_fingerprint=database_target_fingerprint,
            deadline_monotonic=deadline_monotonic,
        )
        observed = self._canonicalizer.read_deep(connection)
        expected_deep = str(self._expected["deep_catalog_fingerprint"])
        if observed.deep_catalog_fingerprint != expected_deep:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.CATALOG_DRIFT,
                "PostgreSQL deep catalog differs from the packaged manifest",
            )
        payload = {
            "nested_fast_result_fingerprint": fast.result.result_fingerprint,
            "expected_object_manifest_fingerprint": (
                POSTGRES_LATEST_SCHEMA_MANIFEST.manifest_fingerprint
            ),
            "expected_deep_catalog_fingerprint": expected_deep,
            "observed_deep_catalog_fingerprint": observed.deep_catalog_fingerprint,
            "unexpected_object_fingerprints": (),
            "missing_object_fingerprints": (),
        }
        result = PostgresDeepSchemaVerificationResult(
            **payload,
            result_fingerprint=postgres_schema_fingerprint(
                "pulsara:postgres-deep-schema-verification-result:v1", payload
            ),
        )
        return PostgresDeepVerificationBundle(fast=fast, result=result)


def classify_migration_history(
    rows: tuple[object, ...] | None,
    registry: PostgresMigrationRegistry = POSTGRES_MIGRATION_REGISTRY,
) -> PostgresMigrationHistoryStatus:
    if rows is None:
        return PostgresMigrationHistoryStatus.UNMIGRATED
    if not rows:
        return PostgresMigrationHistoryStatus.CONFLICT
    if tuple(row.version for row in rows) != tuple(range(len(rows))):
        return PostgresMigrationHistoryStatus.CONFLICT
    for row, expected in zip(
        rows[: len(registry.definitions)],
        registry.definitions,
        strict=False,
    ):
        if (
            row.version != expected.version
            or row.name != expected.name
            or row.resource_checksum != expected.expected_sha256
            or row.migration_contract_fingerprint
            != expected.migration_contract_fingerprint
            or row.registry_prefix_fingerprint != expected.registry_prefix_fingerprint
        ):
            return PostgresMigrationHistoryStatus.CONFLICT
    if len(rows) > len(registry.definitions):
        previous_prefix = registry.registry_fingerprint
        for row in rows[len(registry.definitions) :]:
            expected_prefix = postgres_schema_fingerprint(
                "pulsara:postgres-migration-registry-prefix:v1",
                {
                    "previous_registry_prefix_fingerprint": previous_prefix,
                    "migration_contract_fingerprint": (
                        row.migration_contract_fingerprint
                    ),
                },
            )
            if row.registry_prefix_fingerprint != expected_prefix:
                return PostgresMigrationHistoryStatus.CONFLICT
            previous_prefix = expected_prefix
    if len(rows) < len(registry.definitions):
        return PostgresMigrationHistoryStatus.BEHIND
    if len(rows) > len(registry.definitions):
        return PostgresMigrationHistoryStatus.AHEAD
    return PostgresMigrationHistoryStatus.UP_TO_DATE


def _validate_latest_history(
    rows: tuple[object, ...], registry: PostgresMigrationRegistry
) -> None:
    status = classify_migration_history(rows, registry)
    if status is PostgresMigrationHistoryStatus.BEHIND:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.VERSION_BEHIND,
            "database migration head is behind this Pulsara binary",
        )
    if status is PostgresMigrationHistoryStatus.AHEAD:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.VERSION_AHEAD,
            "database migration head is ahead of this Pulsara binary",
        )
    if status is not PostgresMigrationHistoryStatus.UP_TO_DATE:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.HISTORY_CONFLICT,
            "migration ledger history conflicts with the packaged registry",
        )


def _load_expected_catalog() -> dict[str, object]:
    resource = files("pulsara_agent.storage.migrations").joinpath(
        "expected_catalog_v4.json"
    )
    payload = json.loads(resource.read_text(encoding="utf-8"))
    expected_fast = postgres_schema_fingerprint(
        "pulsara:postgres-fast-executable-schema:v1",
        {
            "extensions": ({"schema_name": "public", "extension_name": "vector"},),
            "types": _freeze(payload["types"]),
            "relation_execution_shapes": _freeze(payload["relation_execution_shapes"]),
            "function_execution_shapes": _freeze(payload["function_execution_shapes"]),
        },
    )
    if payload.get("fast_executable_schema_fingerprint") != expected_fast:
        raise RuntimeError("packaged expected fast catalog fingerprint mismatch")
    deep_payload = {
        "schema_version": "postgres_deep_observed_catalog.v1",
        "fast_observed_catalog_fingerprint": expected_fast,
        "relations": _freeze(payload["relations"]),
        "functions": _freeze(payload["functions"]),
    }
    expected_deep = postgres_schema_fingerprint(
        "pulsara:postgres-deep-catalog:v1", deep_payload
    )
    if payload.get("deep_catalog_fingerprint") != expected_deep:
        raise RuntimeError("packaged expected deep catalog fingerprint mismatch")
    return payload


def _freeze(value: object) -> object:
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return {str(key): _freeze(item) for key, item in value.items()}
    return value


def _require_pgvector(extensions: tuple[dict[str, object], ...]) -> str:
    matches = tuple(
        item for item in extensions if item.get("extension_name") == "vector"
    )
    if len(matches) != 1:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.EXTENSION_MISSING,
            "pgvector extension is missing",
        )
    version = str(matches[0]["extension_version"])
    parsed = tuple(int(part) for part in version.split(".") if part.isdigit())
    if parsed < (0, 5, 0):
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.EXTENSION_TOO_OLD,
            "pgvector extension must be >= 0.5.0",
        )
    return version


def _require_remaining(deadline_monotonic: float) -> float:
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
            "PostgreSQL verification deadline exceeded",
            retryable=True,
        )
    return remaining


__all__ = [
    "PostgresDeepVerificationBundle",
    "PostgresFastVerificationBundle",
    "PostgresMigrationHistoryStatus",
    "PostgresSchemaVerifier",
    "classify_migration_history",
]
