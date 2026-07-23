"""Stable failure taxonomy for PostgreSQL schema migration and verification."""

from __future__ import annotations

from enum import StrEnum


class PostgresSchemaFailureCode(StrEnum):
    ADMIN_DSN_REQUIRED = "schema_admin_dsn_required"
    CONNECTION_FAILED = "schema_connection_failed"
    DEADLINE_EXCEEDED = "schema_deadline_exceeded"
    DATABASE_IDENTITY_MISMATCH = "schema_database_identity_mismatch"
    SEARCH_PATH_MISMATCH = "schema_search_path_mismatch"
    SERVER_VERSION_UNSUPPORTED = "schema_server_version_unsupported"
    UNMANAGED_DATABASE = "schema_unmanaged_database"
    VERSION_BEHIND = "schema_version_behind"
    VERSION_AHEAD = "schema_version_ahead"
    HISTORY_CONFLICT = "schema_migration_history_conflict"
    RESOURCE_CHECKSUM_MISMATCH = "schema_migration_resource_checksum_mismatch"
    MIGRATION_FAILED = "schema_migration_failed"
    MIGRATION_CONFIRMATION_CONFLICT = "schema_migration_confirmation_conflict"
    MIGRATION_CONFIRMATION_UNRESOLVED = "schema_migration_confirmation_unresolved"
    CATALOG_DRIFT = "schema_catalog_drift"
    EXTENSION_MISSING = "schema_extension_missing"
    EXTENSION_TOO_OLD = "schema_extension_too_old"
    PRIVILEGE_MISSING = "schema_runtime_privilege_missing"
    PRIVILEGE_RECONCILIATION_FAILED = "schema_privilege_reconciliation_failed"
    PRIVILEGE_CONFIRMATION_CONFLICT = (
        "schema_runtime_grant_confirmation_conflict"
    )
    PRIVILEGE_CONFIRMATION_UNRESOLVED = (
        "schema_runtime_grant_confirmation_unresolved"
    )
    CONNINFO_UNSUPPORTED = "schema_conninfo_unsupported"
    ACCESS_LEASE_RELEASED = "schema_access_lease_released"


class PostgresSchemaError(RuntimeError):
    """Typed schema failure that remains a normal, traceback-bearing exception."""

    def __init__(
        self,
        code: PostgresSchemaFailureCode,
        detail: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable

    def __str__(self) -> str:
        return f"{self.code.value}: {self.detail}"


class PostgresSchemaUnresolvedError(PostgresSchemaError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            PostgresSchemaFailureCode.MIGRATION_CONFIRMATION_UNRESOLVED,
            detail,
            retryable=True,
        )


__all__ = [
    "PostgresSchemaError",
    "PostgresSchemaFailureCode",
    "PostgresSchemaUnresolvedError",
]
