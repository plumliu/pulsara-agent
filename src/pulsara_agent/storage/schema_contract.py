"""Verifier-issued PostgreSQL schema binding v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.storage.migrations.contracts import (
    UNIVERSE_GENERATION,
    UNIVERSE_ID,
    postgres_schema_fingerprint,
    require_fingerprint,
)


_GUARD = object()


@dataclass(frozen=True, slots=True)
class VerifiedPostgresSchemaBinding:
    database_target_fingerprint: str
    database_name: str
    database_oid: int
    normalized_search_path: tuple[str, ...]
    runtime_role: str
    server_version_num: int
    pgvector_extension_version: str
    migration_universe_id: str
    migration_universe_generation: int
    migration_universe_fingerprint: str
    migration_head_version: int
    durable_registry_prefix_fingerprint: str
    verified_catalog_fingerprint: str
    runtime_grant_policy_fingerprint: str
    verification_contract_fingerprint: str
    binding_fingerprint: str
    _construction_guard: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._construction_guard is not _GUARD:
            raise TypeError("VerifiedPostgresSchemaBinding must be verifier-issued")
        if (
            self.migration_universe_id != UNIVERSE_ID
            or self.migration_universe_generation != UNIVERSE_GENERATION
            or self.migration_head_version != 0
            or self.normalized_search_path != ("public",)
            or self.database_oid <= 0
        ):
            raise ValueError("invalid clean PostgreSQL binding identity")
        for name in (
            "database_target_fingerprint",
            "migration_universe_fingerprint",
            "durable_registry_prefix_fingerprint",
            "verified_catalog_fingerprint",
            "runtime_grant_policy_fingerprint",
            "verification_contract_fingerprint",
            "binding_fingerprint",
        ):
            require_fingerprint(str(getattr(self, name)), field_name=name)
        expected = postgres_schema_fingerprint(
            "pulsara:verified-postgres-schema-binding:v2", _binding_payload(self)
        )
        if self.binding_fingerprint != expected:
            raise ValueError("verified PostgreSQL binding fingerprint mismatch")

    def __reduce__(self) -> Any:
        raise TypeError("VerifiedPostgresSchemaBinding is process-local")


def build_verified_postgres_schema_binding(**values: object) -> VerifiedPostgresSchemaBinding:
    return VerifiedPostgresSchemaBinding(
        **values,
        binding_fingerprint=postgres_schema_fingerprint(
            "pulsara:verified-postgres-schema-binding:v2", values
        ),
        _construction_guard=_GUARD,
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
        "migration_universe_id": binding.migration_universe_id,
        "migration_universe_generation": binding.migration_universe_generation,
        "migration_universe_fingerprint": binding.migration_universe_fingerprint,
        "migration_head_version": binding.migration_head_version,
        "durable_registry_prefix_fingerprint": binding.durable_registry_prefix_fingerprint,
        "verified_catalog_fingerprint": binding.verified_catalog_fingerprint,
        "runtime_grant_policy_fingerprint": binding.runtime_grant_policy_fingerprint,
        "verification_contract_fingerprint": binding.verification_contract_fingerprint,
    }


__all__ = ["VerifiedPostgresSchemaBinding", "build_verified_postgres_schema_binding"]
