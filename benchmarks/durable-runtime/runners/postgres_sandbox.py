"""Fresh-database lifecycle for measured durable-runtime iterations."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from hashlib import sha256
import re

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from network_guard import validate_local_postgres_dsn


_DATABASE_COMPONENT = re.compile(r"[^a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class PostgresIterationDatabase:
    database_name: str
    dsn: str


class PostgresTemplateDatabaseSandbox(
    AbstractContextManager[PostgresIterationDatabase]
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
        self._admin_dsn = _dsn_with_database(admin_dsn, "postgres")
        self._template_database = template_database
        self._database_name = iteration_database_name(
            benchmark_run_id,
            case_contract_fingerprint,
            iteration,
        )
        self._database_dsn = _dsn_with_database(
            application_dsn,
            self._database_name,
        )
        self._entered = False

    def __enter__(self) -> PostgresIterationDatabase:
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
        self._entered = True
        return PostgresIterationDatabase(
            database_name=self._database_name,
            dsn=self._database_dsn,
        )

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if not self._entered:
            return None
        try:
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
        finally:
            self._entered = False
        return None


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


def _dsn_with_database(dsn: str, database_name: str) -> str:
    parameters = conninfo_to_dict(dsn)
    parameters["dbname"] = database_name
    return make_conninfo(**parameters)
