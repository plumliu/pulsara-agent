"""Secret-safe capability produced by PostgreSQL schema verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.storage.migrations.contracts import (
    postgres_schema_fingerprint,
    require_fingerprint,
)


_BINDING_CONSTRUCTION_GUARD = object()


@dataclass(frozen=True, slots=True)
class VerifiedPostgresSchemaBinding:
    database_target_fingerprint: str
    database_name: str
    database_oid: int
    normalized_search_path: tuple[str, ...]
    runtime_role: str
    server_version_num: int
    pgvector_extension_version: str
    migration_head_version: int
    durable_registry_prefix_fingerprint: str
    fast_executable_schema_fingerprint: str
    verification_contract_fingerprint: str
    binding_fingerprint: str
    _construction_guard: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._construction_guard is not _BINDING_CONSTRUCTION_GUARD:
            raise TypeError("VerifiedPostgresSchemaBinding must be verifier-issued")
        for name in (
            "database_target_fingerprint",
            "durable_registry_prefix_fingerprint",
            "fast_executable_schema_fingerprint",
            "verification_contract_fingerprint",
            "binding_fingerprint",
        ):
            require_fingerprint(str(getattr(self, name)), field_name=name)
        if self.database_oid <= 0 or self.migration_head_version < 0:
            raise ValueError("invalid verified PostgreSQL binding identity")
        if self.normalized_search_path != ("public",):
            raise ValueError("verified PostgreSQL search path must be exactly public")
        payload = _binding_payload(self)
        expected = postgres_schema_fingerprint(
            "pulsara:verified-postgres-schema-binding:v1", payload
        )
        if self.binding_fingerprint != expected:
            raise ValueError("verified PostgreSQL binding fingerprint mismatch")

    def __reduce__(self) -> Any:
        raise TypeError("VerifiedPostgresSchemaBinding is process-local and not picklable")

    def __copy__(self) -> Any:
        raise TypeError("VerifiedPostgresSchemaBinding cannot be copied")

    def __deepcopy__(self, memo: object) -> Any:
        del memo
        raise TypeError("VerifiedPostgresSchemaBinding cannot be copied")


def build_verified_postgres_schema_binding(
    *,
    database_target_fingerprint: str,
    database_name: str,
    database_oid: int,
    normalized_search_path: tuple[str, ...],
    runtime_role: str,
    server_version_num: int,
    pgvector_extension_version: str,
    migration_head_version: int,
    durable_registry_prefix_fingerprint: str,
    fast_executable_schema_fingerprint: str,
    verification_contract_fingerprint: str,
) -> VerifiedPostgresSchemaBinding:
    values = {
        "database_target_fingerprint": database_target_fingerprint,
        "database_name": database_name,
        "database_oid": database_oid,
        "normalized_search_path": normalized_search_path,
        "runtime_role": runtime_role,
        "server_version_num": server_version_num,
        "pgvector_extension_version": pgvector_extension_version,
        "migration_head_version": migration_head_version,
        "durable_registry_prefix_fingerprint": durable_registry_prefix_fingerprint,
        "fast_executable_schema_fingerprint": fast_executable_schema_fingerprint,
        "verification_contract_fingerprint": verification_contract_fingerprint,
    }
    return VerifiedPostgresSchemaBinding(
        **values,
        binding_fingerprint=postgres_schema_fingerprint(
            "pulsara:verified-postgres-schema-binding:v1", values
        ),
        _construction_guard=_BINDING_CONSTRUCTION_GUARD,
    )


def _binding_payload(binding: VerifiedPostgresSchemaBinding) -> dict[str, object]:
    return {
        "database_target_fingerprint": binding.database_target_fingerprint,
        "database_name": binding.database_name,
        "database_oid": binding.database_oid,
        "normalized_search_path": binding.normalized_search_path,
        "runtime_role": binding.runtime_role,
        "server_version_num": binding.server_version_num,
        "pgvector_extension_version": binding.pgvector_extension_version,
        "migration_head_version": binding.migration_head_version,
        "durable_registry_prefix_fingerprint": binding.durable_registry_prefix_fingerprint,
        "fast_executable_schema_fingerprint": binding.fast_executable_schema_fingerprint,
        "verification_contract_fingerprint": binding.verification_contract_fingerprint,
    }


__all__ = [
    "VerifiedPostgresSchemaBinding",
    "build_verified_postgres_schema_binding",
]
