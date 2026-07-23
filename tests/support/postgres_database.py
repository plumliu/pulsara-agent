"""Per-worker migrated PostgreSQL database ownership for integration tests."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from time import monotonic
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from pulsara_agent.settings import load_env_file
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner


@dataclass(frozen=True, slots=True)
class MigratedPostgresTestDatabase:
    database_name: str
    admin_dsn: str
    runtime_dsn: str


def create_migrated_postgres_test_database() -> MigratedPostgresTestDatabase:
    database = create_empty_postgres_test_database()
    try:
        PostgresMigrationRunner(
            admin_dsn=database.admin_dsn,
            runtime_dsn=database.runtime_dsn,
        ).migrate(deadline_monotonic=monotonic() + 120.0)
    except BaseException:
        drop_postgres_test_database(admin_root_dsn(), database.database_name)
        raise
    return database


def create_empty_postgres_test_database() -> MigratedPostgresTestDatabase:
    _load_local_env_if_present()
    admin_root_dsn = os.getenv("PULSARA_POSTGRES_ADMIN_DSN") or os.getenv(
        "PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN"
    )
    runtime_root_dsn = os.getenv("PULSARA_POSTGRES_DSN")
    if not admin_root_dsn or not runtime_root_dsn:
        raise RuntimeError(
            "PostgreSQL integration tests require PULSARA_POSTGRES_ADMIN_DSN "
            "or PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN and PULSARA_POSTGRES_DSN"
        )

    worker = os.getenv("PYTEST_XDIST_WORKER", "main").replace("-", "_")
    database_name = f"pulsara_test_{worker}_{os.getpid()}_{uuid4().hex[:10]}"
    admin_dsn = dsn_with_database(admin_root_dsn, database_name)
    runtime_dsn = dsn_with_database(runtime_root_dsn, database_name)

    with psycopg.connect(admin_root_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    return MigratedPostgresTestDatabase(
        database_name=database_name,
        admin_dsn=admin_dsn,
        runtime_dsn=runtime_dsn,
    )


def drop_postgres_test_database(admin_root_dsn: str, database_name: str) -> None:
    with psycopg.connect(admin_root_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(database_name)
            )
        )


def admin_root_dsn() -> str:
    _load_local_env_if_present()
    value = os.getenv("PULSARA_POSTGRES_ADMIN_DSN") or os.getenv(
        "PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN"
    )
    if not value:
        raise RuntimeError("PostgreSQL admin DSN is unavailable")
    return value


def dsn_with_database(dsn: str, database_name: str) -> str:
    parameters = conninfo_to_dict(dsn)
    parameters["dbname"] = database_name
    return make_conninfo(**parameters)


def _load_local_env_if_present() -> None:
    path = Path.cwd() / ".env"
    if path.is_file():
        load_env_file(path, override=False)


__all__ = [
    "MigratedPostgresTestDatabase",
    "admin_root_dsn",
    "create_empty_postgres_test_database",
    "create_migrated_postgres_test_database",
    "dsn_with_database",
    "drop_postgres_test_database",
]
