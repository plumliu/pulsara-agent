"""Stage 5 clean-universe migration, binding-v2, and reset-only gates."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from importlib.resources import files
from time import monotonic

import psycopg
import pytest
from psycopg import sql

from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.storage.migrations.contracts import (
    BASELINE_RESOURCE,
    CATALOG_RESOURCE,
    GRANT_RESOURCE,
    UNIVERSE_GENERATION,
    UNIVERSE_ID,
    build_migration_universe_identity,
)
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.storage.migrations.registry import POSTGRES_MIGRATION_REGISTRY
from pulsara_agent.storage.migrations.runner import (
    BaselineCommitConfirmation,
    PostgresMigrationRunner,
    _require_compatible_vector_if_present,
    _verify_vector_capability,
    classify_clean_baseline_confirmation,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    PostgresRuntimeConnectionFactory,
)
from pulsara_agent.storage.schema_verification_service import (
    acquire_verified_postgres_access_sync,
)
from tests.support.postgres_database import (
    admin_root_dsn,
    create_empty_postgres_test_database,
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
)


pytestmark = pytest.mark.postgres


@contextmanager
def _empty_database():
    database = create_empty_postgres_test_database()
    try:
        yield database
    finally:
        drop_postgres_test_database(admin_root_dsn(), database.database_name)


@contextmanager
def _migrated_database():
    database = create_migrated_postgres_test_database()
    try:
        yield database
    finally:
        drop_postgres_test_database(admin_root_dsn(), database.database_name)


def _runner(database) -> PostgresMigrationRunner:
    return PostgresMigrationRunner(
        admin_dsn=database.admin_dsn,
        runtime_dsn=database.runtime_dsn,
    )


class _Rows:
    def __init__(self, row) -> None:
        self._row = row

    def fetchone(self):
        return self._row


class _VectorCapabilityProbe:
    def __init__(self, rows: list[object]) -> None:
        self._rows = iter(rows)

    def execute(self, _statement, _parameters=None):
        return _Rows(next(self._rows))


class _CommitFaultConnection:
    def __init__(self, connection, *, disposition: str, admin_dsn: str) -> None:
        self._connection = connection
        self._disposition = disposition
        self._admin_dsn = admin_dsn

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def commit(self) -> None:
        if self._disposition == "NONE":
            self._connection.rollback()
        else:
            self._connection.commit()
            if self._disposition == "CONFLICT":
                with psycopg.connect(self._admin_dsn) as observer:
                    observer.execute(
                        "UPDATE public.pulsara_schema_migrations "
                        "SET registry_prefix_fingerprint = %s WHERE version = 0",
                        ("sha256:" + "0" * 64,),
                    )
                    observer.commit()
        raise OSError("injected lost commit acknowledgement")


class _CommitFaultFactory:
    def __init__(self, wrapped, *, disposition: str, admin_dsn: str) -> None:
        self._wrapped = wrapped
        self._disposition = disposition
        self._admin_dsn = admin_dsn
        self._injected = False

    @contextmanager
    def connect(self, **kwargs):
        with self._wrapped.connect(**kwargs) as connection:
            if self._injected:
                yield connection
                return
            self._injected = True
            yield _CommitFaultConnection(
                connection,
                disposition=self._disposition,
                admin_dsn=self._admin_dsn,
            )


def test_clean_migration_identity_golden_and_resource_tree_are_exact() -> None:
    identity = build_migration_universe_identity(
        baseline_sql_sha256="11" * 32,
        catalog_sha256="22" * 32,
        grant_sha256="33" * 32,
    )
    assert identity.baseline_contract_fingerprint == (
        "sha256:8390ab92c98ed167b03a3fd73943750bd23b148538c4eb5f75714b5398cbd240"
    )
    assert identity.universe_fingerprint == (
        "sha256:9f3b3cc41831e3dd7ddff91ff9b0c4f35d421745c25a3d346331c95a2073ca19"
    )
    assert identity.genesis_registry_prefix_fingerprint == (
        "sha256:62c84b5c8e9dec93c3c76f1ba4da1892983dd431bc1be51d6d3d9cb12d7cdcc4"
    )
    assert UNIVERSE_ID == "pulsara.conversation-kernel.v1"
    assert UNIVERSE_GENERATION == 1
    assert POSTGRES_MIGRATION_REGISTRY.latest_version == 0
    root = files("pulsara_agent.storage.migrations")
    sql_names = tuple(
        item.name
        for item in root.joinpath("sql").iterdir()
        if item.name.endswith(".sql")
    )
    resource_names = tuple(
        sorted(
            item.name
            for item in root.joinpath("resources").iterdir()
            if item.name.endswith(".json")
        )
    )
    assert sql_names == (BASELINE_RESOURCE,)
    assert resource_names == tuple(sorted((CATALOG_RESOURCE, GRANT_RESOURCE)))
    definition = POSTGRES_MIGRATION_REGISTRY.definition(0)
    assert sha256(definition.resource_bytes()).hexdigest() == definition.expected_sha256


def test_empty_install_second_migrate_and_binding_v2_checkout() -> None:
    with _empty_database() as database:
        runner = _runner(database)
        first = runner.migrate(deadline_monotonic=monotonic() + 60)
        second = runner.migrate(deadline_monotonic=monotonic() + 60)
        assert first.applied_versions == (0,)
        assert second.applied_versions == ()
        assert first.universe_fingerprint == second.universe_fingerprint
        assert first.baseline_commit_confirmation == "FULL"

        with acquire_verified_postgres_access_sync(
            database.runtime_dsn,
            deadline_monotonic=monotonic() + 30,
        ) as access:
            binding = access.schema_binding
            assert binding.migration_universe_id == UNIVERSE_ID
            assert binding.migration_universe_generation == 1
            assert binding.migration_head_version == 0
            assert not hasattr(binding, "runtime_write_epoch")
            assert not hasattr(binding, "guard_secret")
            provider = access.connection_provider
            repository = ConversationKernelRepository(provider)
            with provider.connection(
                lane=PostgresConnectionLane.HOST_CONTROL,
                deadline_monotonic=monotonic() + 30,
            ) as connection:
                observed = tuple(
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT c.relname FROM pg_catalog.pg_class c
                        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'pulsara_v3' AND c.relkind = 'r'
                        ORDER BY c.relname
                        """
                    ).fetchall()
                )
            assert observed == tuple(sorted(CONVERSATION_KERNEL_RELATIONS))
            assert repository.connection_provider is provider


def test_old_ledger_and_unmanaged_object_require_reset_without_advancing_ddl() -> None:
    with _empty_database() as database:
        with psycopg.connect(database.admin_dsn) as connection:
            connection.execute(
                "CREATE TABLE public.pulsara_schema_migrations "
                "(version integer PRIMARY KEY, name text NOT NULL)"
            )
            connection.execute(
                "INSERT INTO public.pulsara_schema_migrations VALUES (13, 'old_head')"
            )
            connection.commit()
        with pytest.raises(PostgresSchemaError) as caught:
            _runner(database).migrate(deadline_monotonic=monotonic() + 30)
        assert caught.value.code is (
            PostgresSchemaFailureCode.MIGRATION_UNIVERSE_RESET_REQUIRED
        )
        with psycopg.connect(database.admin_dsn) as connection:
            assert connection.execute(
                "SELECT to_regnamespace('pulsara_v3'), count(*) "
                "FROM public.pulsara_schema_migrations"
            ).fetchone() == (None, 1)

    with _empty_database() as database:
        with psycopg.connect(database.admin_dsn) as connection:
            connection.execute("CREATE TABLE public.sessions (id text PRIMARY KEY)")
            connection.commit()
        with pytest.raises(PostgresSchemaError) as caught:
            _runner(database).migrate(deadline_monotonic=monotonic() + 30)
        assert caught.value.code is (
            PostgresSchemaFailureCode.MIGRATION_UNIVERSE_RESET_REQUIRED
        )
        with psycopg.connect(database.admin_dsn) as connection:
            assert connection.execute(
                "SELECT to_regnamespace('pulsara_v3'), to_regclass('public.sessions')"
            ).fetchone() == (None, "sessions")


def test_unrelated_public_relation_is_preserved_in_shared_database() -> None:
    with _empty_database() as database:
        with psycopg.connect(database.admin_dsn) as connection:
            connection.execute(
                "CREATE TABLE public.unrelated_app_state "
                "(id integer PRIMARY KEY, value text NOT NULL)"
            )
            connection.execute(
                "INSERT INTO public.unrelated_app_state VALUES (1, 'preserve-me')"
            )
            connection.commit()
        _runner(database).migrate(deadline_monotonic=monotonic() + 60)
        with psycopg.connect(database.admin_dsn) as connection:
            assert connection.execute(
                "SELECT value FROM public.unrelated_app_state WHERE id = 1"
            ).fetchone() == ("preserve-me",)


def test_baseline_confirmation_is_closed_full_none_conflict() -> None:
    with _empty_database() as database:
        runner = _runner(database)
        with psycopg.connect(database.admin_dsn) as connection:
            assert classify_clean_baseline_confirmation(
                connection,
                runtime_role=psycopg.conninfo.conninfo_to_dict(
                    database.runtime_dsn
                )["user"],
            ) is BaselineCommitConfirmation.NONE
        runner.migrate(deadline_monotonic=monotonic() + 30)
        assert runner.confirm_baseline(
            deadline_monotonic=monotonic() + 30
        ) is BaselineCommitConfirmation.FULL
        with psycopg.connect(database.admin_dsn) as connection:
            connection.execute(
                "UPDATE public.pulsara_schema_migrations "
                "SET registry_prefix_fingerprint = %s WHERE version = 0",
                ("sha256:" + "0" * 64,),
            )
            connection.commit()
            assert classify_clean_baseline_confirmation(
                connection,
                runtime_role=psycopg.conninfo.conninfo_to_dict(
                    database.runtime_dsn
                )["user"],
            ) is BaselineCommitConfirmation.CONFLICT


@pytest.mark.parametrize(
    ("disposition", "expected_code"),
    (
        ("FULL", None),
        ("NONE", None),
        (
            "CONFLICT",
            PostgresSchemaFailureCode.MIGRATION_CONFIRMATION_CONFLICT,
        ),
    ),
)
def test_lost_baseline_commit_ack_is_exactly_confirmed(
    disposition: str,
    expected_code: PostgresSchemaFailureCode | None,
) -> None:
    with _empty_database() as database:
        runner = _runner(database)
        runner._admin = _CommitFaultFactory(  # type: ignore[assignment]
            runner._admin,
            disposition=disposition,
            admin_dsn=database.admin_dsn,
        )
        if expected_code is None:
            report = runner.migrate(deadline_monotonic=monotonic() + 60)
            assert report.baseline_commit_confirmation == "FULL"
            assert report.applied_versions == (0,)
            return
        with pytest.raises(PostgresSchemaError) as caught:
            runner.migrate(deadline_monotonic=monotonic() + 60)
        assert caught.value.code is expected_code


def test_catalog_and_grant_drift_fail_closed() -> None:
    with _migrated_database() as database:
        with psycopg.connect(database.admin_dsn) as connection:
            connection.execute(
                "ALTER TABLE pulsara_v3.sessions DROP COLUMN updated_at"
            )
            connection.commit()
        with pytest.raises(PostgresSchemaError) as caught:
            PostgresRuntimeConnectionFactory(database.runtime_dsn).verify_deep(
                deadline_monotonic=monotonic() + 30
            )
        assert caught.value.code is PostgresSchemaFailureCode.CATALOG_DRIFT

    with _migrated_database() as database:
        runtime_role = psycopg.conninfo.conninfo_to_dict(database.runtime_dsn)["user"]
        with psycopg.connect(database.admin_dsn) as connection:
            connection.execute(
                sql.SQL("REVOKE INSERT ON pulsara_v3.agent_events FROM {}").format(
                    sql.Identifier(runtime_role)
                )
            )
            connection.commit()
        with pytest.raises(PostgresSchemaError) as caught:
            PostgresRuntimeConnectionFactory(database.runtime_dsn).verify_deep(
                deadline_monotonic=monotonic() + 30
            )
        assert caught.value.code is PostgresSchemaFailureCode.PRIVILEGE_MISSING


def test_function_trigger_and_forbidden_grant_drift_fail_closed() -> None:
    with _migrated_database() as database:
        with psycopg.connect(database.admin_dsn) as connection:
            connection.execute(
                "ALTER TABLE pulsara_v3.transcript_entries DISABLE TRIGGER "
                "trg_pulsara_v3_entry_source_integrity"
            )
            connection.commit()
        with pytest.raises(PostgresSchemaError) as caught:
            PostgresRuntimeConnectionFactory(database.runtime_dsn).verify_deep(
                deadline_monotonic=monotonic() + 30
            )
        assert caught.value.code is PostgresSchemaFailureCode.CATALOG_DRIFT

    with _migrated_database() as database:
        with psycopg.connect(database.admin_dsn) as connection:
            connection.execute(
                """
                CREATE OR REPLACE FUNCTION
                    pulsara_v3.enforce_conversation_kernel_invariants()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    RETURN NEW;
                END;
                $$
                """
            )
            connection.commit()
        with pytest.raises(PostgresSchemaError) as caught:
            PostgresRuntimeConnectionFactory(database.runtime_dsn).verify_deep(
                deadline_monotonic=monotonic() + 30
            )
        assert caught.value.code is PostgresSchemaFailureCode.CATALOG_DRIFT

    with _migrated_database() as database:
        runtime_role = psycopg.conninfo.conninfo_to_dict(database.runtime_dsn)["user"]
        with psycopg.connect(database.admin_dsn) as connection:
            connection.execute(
                sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(
                    sql.Identifier(runtime_role)
                )
            )
            connection.commit()
        with pytest.raises(PostgresSchemaError) as caught:
            PostgresRuntimeConnectionFactory(database.runtime_dsn).verify_deep(
                deadline_monotonic=monotonic() + 30
            )
        assert caught.value.code is PostgresSchemaFailureCode.PRIVILEGE_MISSING

    with _migrated_database() as database:
        runtime_role = psycopg.conninfo.conninfo_to_dict(database.runtime_dsn)["user"]
        with psycopg.connect(database.admin_dsn) as connection:
            connection.execute(
                sql.SQL(
                    "REVOKE EXECUTE ON FUNCTION "
                    "pulsara_v3.enforce_conversation_kernel_invariants() FROM {}"
                ).format(sql.Identifier(runtime_role))
            )
            connection.commit()
        with pytest.raises(PostgresSchemaError) as caught:
            PostgresRuntimeConnectionFactory(database.runtime_dsn).verify_deep(
                deadline_monotonic=monotonic() + 30
            )
        assert caught.value.code is PostgresSchemaFailureCode.PRIVILEGE_MISSING


def test_compatible_vector_and_unrelated_pgcrypto_are_adopted_not_removed() -> None:
    with _empty_database() as database:
        with psycopg.connect(database.admin_dsn) as connection:
            connection.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            connection.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
            connection.commit()
        _runner(database).migrate(deadline_monotonic=monotonic() + 60)
        with psycopg.connect(database.admin_dsn) as connection:
            assert connection.execute(
                "SELECT extname FROM pg_catalog.pg_extension "
                "WHERE extname IN ('vector', 'pgcrypto') ORDER BY extname"
            ).fetchall() == [("pgcrypto",), ("vector",)]


def test_vector_wrong_schema_too_old_and_incompatible_shape_fail_closed() -> None:
    with _empty_database() as database:
        with psycopg.connect(database.admin_dsn) as connection:
            connection.execute("CREATE SCHEMA alternate")
            connection.execute("CREATE EXTENSION vector WITH SCHEMA alternate")
            connection.commit()
        with pytest.raises(PostgresSchemaError) as caught:
            _runner(database).migrate(deadline_monotonic=monotonic() + 30)
        assert caught.value.code is PostgresSchemaFailureCode.CATALOG_DRIFT
        with psycopg.connect(database.admin_dsn) as connection:
            assert connection.execute(
                "SELECT to_regnamespace('pulsara_v3'), "
                "to_regclass('public.pulsara_schema_migrations')"
            ).fetchone() == (None, None)

    too_old = _VectorCapabilityProbe([("public", "0.4.9")])
    with pytest.raises(PostgresSchemaError) as caught:
        _require_compatible_vector_if_present(too_old)  # type: ignore[arg-type]
    assert caught.value.code is PostgresSchemaFailureCode.EXTENSION_TOO_OLD

    missing_operator = _VectorCapabilityProbe(
        [("public", "0.8.1"), ("vector",), None]
    )
    with pytest.raises(PostgresSchemaError) as caught:
        _verify_vector_capability(missing_operator)  # type: ignore[arg-type]
    assert caught.value.code is PostgresSchemaFailureCode.CATALOG_DRIFT
