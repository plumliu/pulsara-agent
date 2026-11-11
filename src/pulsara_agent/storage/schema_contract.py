"""Verifier-issued PostgreSQL schema binding."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.storage.migrations.contracts import require_fingerprint


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
    verified_catalog_fingerprint: str
    runtime_grant_policy_fingerprint: str
    _construction_guard: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._construction_guard is not _GUARD:
            raise TypeError("VerifiedPostgresSchemaBinding must be verifier-issued")
        if (
            self.normalized_search_path != ("public",)
            or self.database_oid <= 0
        ):
            raise ValueError("invalid clean PostgreSQL binding identity")
        for name in (
            "database_target_fingerprint",
            "verified_catalog_fingerprint",
            "runtime_grant_policy_fingerprint",
        ):
            require_fingerprint(str(getattr(self, name)), field_name=name)

    def __reduce__(self) -> Any:
        raise TypeError("VerifiedPostgresSchemaBinding is process-local")


def build_verified_postgres_schema_binding(**values: object) -> VerifiedPostgresSchemaBinding:
    return VerifiedPostgresSchemaBinding(**values, _construction_guard=_GUARD)


__all__ = ["VerifiedPostgresSchemaBinding", "build_verified_postgres_schema_binding"]
