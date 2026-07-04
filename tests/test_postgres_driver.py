import pytest
import psycopg

from pulsara_agent.settings import StorageConfig
from pulsara_agent.storage import RUNTIME_TRUTH_TABLES


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def test_psycopg_driver_is_available() -> None:
    assert psycopg.__version__


def test_configured_postgres_dsn_connects_to_runtime_database() -> None:
    dsn = StorageConfig.from_env().postgres_dsn

    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select current_database(), current_user")
            database, user = cursor.fetchone()

    assert database == "pulsara"
    assert user == "pulsara"


def test_configured_postgres_database_has_runtime_truth_tables() -> None:
    dsn = StorageConfig.from_env().postgres_dsn

    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select tablename
                from pg_tables
                where schemaname = 'public'
                order by tablename
                """
            )
            table_names = {row[0] for row in cursor.fetchall()}

    assert set(RUNTIME_TRUTH_TABLES).issubset(table_names)
