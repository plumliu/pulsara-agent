"""Fresh-database lifecycle for measured durable-runtime iterations."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from hashlib import sha256
import re
from time import monotonic

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from network_guard import validate_local_postgres_dsn
from pulsara_agent.storage.migrations.contracts import postgres_schema_fingerprint
from pulsara_agent.storage.migrations.manifest import (
    POSTGRES_LATEST_SCHEMA_MANIFEST,
)
from pulsara_agent.storage.migrations.verifier import PostgresSchemaVerifier
from pulsara_agent.storage.postgres_connection_provider import (
    POSTGRES_POOL_POLICIES,
    PostgresConnectionLane,
    VerifiedPostgresConnectionProvider,
)
from pulsara_agent.storage.schema_contract import VerifiedPostgresSchemaBinding
from pulsara_agent.storage.schema_verification_service import (
    VerifiedPostgresAccessLease,
    acquire_verified_postgres_access_sync,
)


_DATABASE_COMPONENT = re.compile(r"[^a-z0-9_]+")


@dataclass(slots=True)
class VerifiedBenchmarkDatabaseLease:
    database_name: str
    deep_catalog_fingerprint: str
    business_empty: bool
    lease_fingerprint: str
    _access_lease: VerifiedPostgresAccessLease = field(repr=False)
    _released: bool = False

    @property
    def schema_binding(self) -> VerifiedPostgresSchemaBinding:
        self._require_active()
        return self._access_lease.schema_binding

    @property
    def connection_provider(self) -> VerifiedPostgresConnectionProvider:
        self._require_active()
        return self._access_lease.connection_provider

    @property
    def connection_pool_policy_fingerprint(self) -> str:
        self._require_active()
        return POSTGRES_POOL_POLICIES[
            PostgresConnectionLane.EVENT_LOG
        ].policy_fingerprint

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._access_lease.release()

    def _require_active(self) -> None:
        if self._released:
            raise RuntimeError("verified benchmark database lease is released")


class PostgresTemplateDatabaseSandbox(
    AbstractContextManager[VerifiedBenchmarkDatabaseLease]
):
    """Clone one clean database before timing and drop it after timing."""

    def __init__(
        self,
        *,
        application_dsn: str,
        admin_dsn: str,
        template_database: str,
        benchmark_run_id: str,
        case_contract_fingerprint: str,
        iteration: int,
    ) -> None:
        if iteration < 0:
            raise ValueError("benchmark iteration must be non-negative")
        validate_local_postgres_dsn(application_dsn)
        validate_local_postgres_dsn(admin_dsn)
        if not template_database.strip():
            raise ValueError("benchmark template database is required")
        if not benchmark_run_id.strip():
            raise ValueError("benchmark run identity is required")
        self._admin_dsn = postgres_dsn_with_database(admin_dsn, "postgres")
        self._template_database = template_database
        self._database_name = iteration_database_name(
            benchmark_run_id,
            case_contract_fingerprint,
            iteration,
        )
        self._database_dsn = postgres_dsn_with_database(
            application_dsn,
            self._database_name,
        )
        self._entered = False
        self._lease: VerifiedBenchmarkDatabaseLease | None = None

    def __enter__(self) -> VerifiedBenchmarkDatabaseLease:
        if self._entered:
            raise RuntimeError("PostgreSQL benchmark sandbox is not reentrant")
        with psycopg.connect(self._admin_dsn, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select pg_terminate_backend(pid)
                    from pg_stat_activity
                    where datname = %s
                      and pid <> pg_backend_pid()
                    """,
                    (self._database_name,),
                )
                cursor.execute(
                    sql.SQL("drop database if exists {}").format(
                        sql.Identifier(self._database_name)
                    )
                )
                cursor.execute(
                    sql.SQL("create database {} template {}").format(
                        sql.Identifier(self._database_name),
                        sql.Identifier(self._template_database),
                    )
                )
        try:
            lease = acquire_verified_benchmark_database_lease(
                self._database_dsn,
                database_name=self._database_name,
            )
        except BaseException:
            self._drop_database()
            raise
        self._lease = lease
        self._entered = True
        return lease

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if not self._entered:
            return None
        try:
            if self._lease is not None:
                self._lease.release()
                self._lease = None
            self._drop_database()
        finally:
            self._entered = False
        return None

    def _drop_database(self) -> None:
        with psycopg.connect(self._admin_dsn, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select pg_terminate_backend(pid)
                    from pg_stat_activity
                    where datname = %s
                      and pid <> pg_backend_pid()
                    """,
                    (self._database_name,),
                )
                cursor.execute(
                    sql.SQL("drop database if exists {}").format(
                        sql.Identifier(self._database_name)
                    )
                )


def acquire_verified_benchmark_database_lease(
    application_dsn: str,
    *,
    database_name: str,
) -> VerifiedBenchmarkDatabaseLease:
    validate_local_postgres_dsn(application_dsn)
    deadline = monotonic() + 30.0
    access_lease = acquire_verified_postgres_access_sync(
        application_dsn,
        deadline_monotonic=deadline,
    )
    try:
        provider = access_lease.connection_provider
        with provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=deadline,
        ) as connection:
            deep = PostgresSchemaVerifier().verify_deep_connection(
                connection,
                database_target_fingerprint=(
                    access_lease.schema_binding.database_target_fingerprint
                ),
                deadline_monotonic=deadline,
            )
            business_empty = _business_tables_are_empty(connection)
        if not business_empty:
            raise ValueError("benchmark database contains mutable business rows")
        payload = {
            "database_name": database_name,
            "binding_fingerprint": access_lease.schema_binding.binding_fingerprint,
            "deep_catalog_fingerprint": (
                deep.result.observed_deep_catalog_fingerprint
            ),
            "business_empty": business_empty,
        }
        return VerifiedBenchmarkDatabaseLease(
            database_name=database_name,
            deep_catalog_fingerprint=deep.result.observed_deep_catalog_fingerprint,
            business_empty=business_empty,
            lease_fingerprint=postgres_schema_fingerprint(
                "pulsara:verified-benchmark-database-lease:v1", payload
            ),
            _access_lease=access_lease,
        )
    except BaseException:
        access_lease.release()
        raise


def _business_tables_are_empty(connection: psycopg.Connection) -> bool:
    relation_names = tuple(
        str(item["relation_name"])
        for item in POSTGRES_LATEST_SCHEMA_MANIFEST.owned_relations
        if item["relation_name"] != "pulsara_schema_migrations"
    )
    with connection.cursor() as cursor:
        for relation_name in relation_names:
            cursor.execute(
                sql.SQL("SELECT 1 FROM public.{} LIMIT 1").format(
                    sql.Identifier(relation_name)
                )
            )
            if cursor.fetchone() is not None:
                return False
    return True


def iteration_database_name(
    benchmark_run_id: str,
    case_contract_fingerprint: str,
    iteration: int,
) -> str:
    digest = sha256(
        f"{benchmark_run_id}:{case_contract_fingerprint}:{iteration}".encode(
            "utf-8"
        )
    ).hexdigest()[:20]
    prefix = _DATABASE_COMPONENT.sub("_", "pulsara_bench").strip("_")
    return f"{prefix}_{digest}"


def postgres_dsn_with_database(dsn: str, database_name: str) -> str:
    parameters = conninfo_to_dict(dsn)
    parameters["dbname"] = database_name
    return make_conninfo(**parameters)
