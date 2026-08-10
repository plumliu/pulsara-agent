"""Deep verifier for the clean conversation-kernel PostgreSQL catalog."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from time import monotonic

from psycopg import Connection

from pulsara_agent.storage.migrations.contracts import postgres_schema_fingerprint
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.storage.migrations.grants import build_postgres_runtime_grant_policy
from pulsara_agent.storage.migrations.manifest import (
    CONVERSATION_KERNEL_RELATIONS,
    build_postgres_schema_manifest,
)
from pulsara_agent.storage.migrations.registry import POSTGRES_MIGRATION_REGISTRY
from pulsara_agent.storage.migrations.runner import (
    _read_identity_from_connection,
    _validate_clean_ledger,
    _verify_product_relations,
    _verify_runtime_grants,
    _verify_vector_capability,
    read_migration_ledger,
)
from pulsara_agent.storage.schema_contract import (
    VerifiedPostgresSchemaBinding,
    build_verified_postgres_schema_binding,
)


class PostgresMigrationHistoryStatus(StrEnum):
    UNMANAGED = "unmanaged"
    UP_TO_DATE = "up_to_date"
    RESET_REQUIRED = "reset_required"


def classify_migration_history(rows) -> PostgresMigrationHistoryStatus:
    if rows is None:
        return PostgresMigrationHistoryStatus.UNMANAGED
    try:
        _validate_clean_ledger(rows, POSTGRES_MIGRATION_REGISTRY.definition(0))
    except (PostgresSchemaError, ValueError):
        return PostgresMigrationHistoryStatus.RESET_REQUIRED
    return PostgresMigrationHistoryStatus.UP_TO_DATE


@dataclass(frozen=True, slots=True)
class CleanVerificationResult:
    status: str
    universe_fingerprint: str
    registry_prefix_fingerprint: str
    verified_catalog_fingerprint: str
    runtime_grant_policy_fingerprint: str
    result_fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PostgresFastVerificationBundle:
    binding: VerifiedPostgresSchemaBinding
    result: CleanVerificationResult


PostgresDeepVerificationBundle = PostgresFastVerificationBundle


class PostgresSchemaVerifier:
    def verify_fast_connection(
        self,
        connection: Connection,
        *,
        database_target_fingerprint: str,
        deadline_monotonic: float,
    ) -> PostgresFastVerificationBundle:
        return self._verify(
            connection,
            database_target_fingerprint=database_target_fingerprint,
            deadline_monotonic=deadline_monotonic,
        )

    def verify_deep_connection(
        self,
        connection: Connection,
        *,
        database_target_fingerprint: str,
        deadline_monotonic: float,
    ) -> PostgresDeepVerificationBundle:
        return self._verify(
            connection,
            database_target_fingerprint=database_target_fingerprint,
            deadline_monotonic=deadline_monotonic,
        )

    def _verify(
        self,
        connection: Connection,
        *,
        database_target_fingerprint: str,
        deadline_monotonic: float,
    ) -> PostgresFastVerificationBundle:
        if deadline_monotonic <= monotonic():
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
                "PostgreSQL verification deadline expired",
                retryable=True,
            )
        identity = _read_identity_from_connection(connection)
        rows = read_migration_ledger(connection)
        if rows is None:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.UNMANAGED_DATABASE,
                "clean migration ledger is absent",
            )
        definition = POSTGRES_MIGRATION_REGISTRY.definition(0)
        _validate_clean_ledger(rows, definition)
        _verify_vector_capability(connection)
        _verify_product_relations(connection)
        _verify_runtime_grants(connection, identity.runtime_role)

        extension = connection.execute(
            """
            SELECT e.extversion FROM pg_catalog.pg_extension e
            JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
            WHERE e.extname = 'vector' AND n.nspname = 'public'
            """
        ).fetchone()
        if extension is None:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.EXTENSION_MISSING,
                "public.vector extension is absent",
            )
        catalog_fingerprint = _observed_catalog_fingerprint(connection)
        if (
            catalog_fingerprint
            != build_postgres_schema_manifest().observed_catalog_fingerprint
        ):
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.CATALOG_DRIFT,
                "observed conversation-kernel catalog differs from clean baseline",
            )
        grant_policy = build_postgres_runtime_grant_policy()
        verification_contract = postgres_schema_fingerprint(
            "pulsara:clean-schema-verification-contract:v1",
            {
                "universe_fingerprint": definition.identity.universe_fingerprint,
                "relations": CONVERSATION_KERNEL_RELATIONS,
                "required_extension": ("public", "vector", ">=0.5.0"),
                "grant_policy_fingerprint": grant_policy.policy_fingerprint,
            },
        )
        binding = build_verified_postgres_schema_binding(
            database_target_fingerprint=database_target_fingerprint,
            database_name=identity.database_name,
            database_oid=identity.database_oid,
            normalized_search_path=identity.normalized_search_path,
            runtime_role=identity.runtime_role,
            server_version_num=identity.server_version_num,
            pgvector_extension_version=str(extension[0]),
            migration_universe_id="pulsara.conversation-kernel.v1",
            migration_universe_generation=1,
            migration_universe_fingerprint=definition.identity.universe_fingerprint,
            migration_head_version=0,
            durable_registry_prefix_fingerprint=definition.registry_prefix_fingerprint,
            verified_catalog_fingerprint=catalog_fingerprint,
            runtime_grant_policy_fingerprint=grant_policy.policy_fingerprint,
            verification_contract_fingerprint=verification_contract,
        )
        result_payload = {
            "binding_fingerprint": binding.binding_fingerprint,
            "universe_fingerprint": definition.identity.universe_fingerprint,
            "registry_prefix_fingerprint": definition.registry_prefix_fingerprint,
            "verified_catalog_fingerprint": catalog_fingerprint,
            "runtime_grant_policy_fingerprint": grant_policy.policy_fingerprint,
        }
        result = CleanVerificationResult(
            status="verified",
            universe_fingerprint=definition.identity.universe_fingerprint,
            registry_prefix_fingerprint=definition.registry_prefix_fingerprint,
            verified_catalog_fingerprint=catalog_fingerprint,
            runtime_grant_policy_fingerprint=grant_policy.policy_fingerprint,
            result_fingerprint=postgres_schema_fingerprint(
                "pulsara:clean-schema-verification-result:v1", result_payload
            ),
        )
        return PostgresFastVerificationBundle(binding=binding, result=result)


def _observed_catalog_fingerprint(connection: Connection) -> str:
    columns = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT table_name, ordinal_position, column_name, udt_schema, udt_name,
                   is_nullable, COALESCE(column_default, '')
            FROM information_schema.columns
            WHERE table_schema = 'pulsara_v3'
            ORDER BY table_name, ordinal_position
            """
        ).fetchall()
    )
    constraints = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT c.relname, con.conname, con.contype,
                   pg_catalog.pg_get_constraintdef(con.oid, true)
            FROM pg_catalog.pg_constraint con
            JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'pulsara_v3'
            ORDER BY c.relname, con.conname
            """
        ).fetchall()
    )
    indexes = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT tablename, indexname, indexdef FROM pg_catalog.pg_indexes
            WHERE schemaname = 'pulsara_v3' ORDER BY tablename, indexname
            """
        ).fetchall()
    )
    functions = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT p.proname, pg_catalog.pg_get_function_identity_arguments(p.oid),
                   pg_catalog.pg_get_function_result(p.oid), l.lanname,
                   p.provolatile, p.proisstrict, p.prosecdef, p.proleakproof,
                   p.proparallel, COALESCE(array_to_string(p.proconfig, E'\\x1f'), ''),
                   pg_catalog.pg_get_functiondef(p.oid)
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_catalog.pg_language l ON l.oid = p.prolang
            WHERE n.nspname = 'pulsara_v3' ORDER BY p.proname
            """
        ).fetchall()
    )
    triggers = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT c.relname, t.tgname, t.tgenabled,
                   pg_catalog.pg_get_triggerdef(t.oid, true),
                   pn.nspname, p.proname,
                   pg_catalog.pg_get_function_identity_arguments(p.oid)
            FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid
            JOIN pg_catalog.pg_namespace pn ON pn.oid = p.pronamespace
            WHERE n.nspname = 'pulsara_v3' AND NOT t.tgisinternal
            ORDER BY c.relname, t.tgname
            """
        ).fetchall()
    )
    return postgres_schema_fingerprint(
        "pulsara:postgres-observed-catalog:v2",
        {
            "relations": tuple(sorted(CONVERSATION_KERNEL_RELATIONS)),
            "columns": columns,
            "constraints": constraints,
            "indexes": indexes,
            "functions": functions,
            "triggers": triggers,
        },
    )


__all__ = [
    "CleanVerificationResult",
    "PostgresDeepVerificationBundle",
    "PostgresFastVerificationBundle",
    "PostgresMigrationHistoryStatus",
    "PostgresSchemaVerifier",
    "classify_migration_history",
]
