from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from enum import StrEnum
import pickle
from threading import Event, get_ident
from time import monotonic
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from pulsara_agent import cli
from pulsara_agent.storage.migrations.catalog import PostgresCatalogCanonicalizer
from pulsara_agent.storage.migrations.contracts import (
    PostgresMigrationLedgerRowFact,
    canonical_json_bytes,
    postgres_schema_fingerprint,
)
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.storage.migrations.grants import (
    PostgresRelationGrantTargetFact,
    PostgresRuntimeGrantRequirementFact,
)
from pulsara_agent.storage.migrations.manifest import (
    POSTGRES_LATEST_SCHEMA_MANIFEST,
    POSTGRES_SCHEMA_MANIFESTS,
)
from pulsara_agent.storage.migrations.registry import (
    POSTGRES_MIGRATION_REGISTRY,
    PostgresMigrationRegistry,
)
from pulsara_agent.storage.migrations.runner import (
    PostgresCommitConfirmation,
    PostgresDatabaseIdentity,
    PostgresMigrationRunner,
    read_migration_ledger,
)
from pulsara_agent.runtime.projection_jobs.contracts import (
    DurableProjectionKind,
)
from pulsara_agent.runtime.projection_jobs.pre_activation import (
    PostgresProjectionMigrationPreparationCoordinator,
)
from pulsara_agent.storage.migrations.verifier import (
    PostgresMigrationHistoryStatus,
    PostgresSchemaVerifier,
    classify_migration_history,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    PostgresRuntimeConnectionFactory,
    VerifiedPostgresConnectionProvider,
)
from pulsara_agent.storage.schema_verification_service import (
    VerifiedPostgresAccessLease,
    acquire_verified_postgres_access_sync,
)
from tests.support.postgres_database import (
    MigratedPostgresTestDatabase,
    admin_root_dsn,
    create_empty_postgres_test_database,
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
)


@contextmanager
def _fresh_database(*, migrated: bool):
    database = (
        create_migrated_postgres_test_database()
        if migrated
        else create_empty_postgres_test_database()
    )
    try:
        yield database
    finally:
        drop_postgres_test_database(admin_root_dsn(), database.database_name)


def _deep_fingerprint(database: MigratedPostgresTestDatabase) -> str:
    factory = PostgresRuntimeConnectionFactory(database.runtime_dsn)
    with factory.connect(
        deadline_monotonic=monotonic() + 30.0,
        autocommit=True,
    ) as connection:
        return (
            PostgresSchemaVerifier()
            .verify_deep_connection(
                connection,
                database_target_fingerprint=factory.database_target_fingerprint,
                deadline_monotonic=monotonic() + 30.0,
            )
            .result.observed_deep_catalog_fingerprint
        )


def _advance_projection_migrations(
    *,
    database: MigratedPostgresTestDatabase,
    runner: PostgresMigrationRunner,
):
    deadline = monotonic() + 180.0
    coordinator = PostgresProjectionMigrationPreparationCoordinator(
        admin_dsn=database.admin_dsn,
        runtime_dsn=database.runtime_dsn,
    )
    coordinator.prepare_legacy_surface_bindings(deadline_monotonic=deadline)
    report = runner.migrate(deadline_monotonic=deadline)
    assert report.migration_head_version == 6
    coordinator.drain_pre_activation(
        kind=DurableProjectionKind.RUN_TIMELINE,
        deadline_monotonic=deadline,
    )
    report = runner.migrate(deadline_monotonic=deadline)
    assert report.migration_head_version == 7
    coordinator.drain_pre_activation(
        kind=DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE,
        deadline_monotonic=deadline,
    )
    return runner.migrate(deadline_monotonic=deadline)


def test_canonical_schema_hash_has_stable_golden_vector() -> None:
    value = {"z": None, "a": (True, 7, "文本")}
    assert canonical_json_bytes(value) == '{"a":[true,7,"文本"],"z":null}'.encode()
    assert (
        postgres_schema_fingerprint("pulsara:test-schema-golden:v1", value)
        == "sha256:eac7a7b8a047e49da0eebe5e5a9abb05949267c5ae4c68185f3126c1ffd13f16"
    )


@pytest.mark.parametrize("value", [{"items": [1]}, {"items": {1}}, 1.5, b"x"])
def test_canonical_schema_hash_rejects_noncanonical_values(value: object) -> None:
    with pytest.raises(TypeError):
        canonical_json_bytes(value)


def test_canonical_schema_hash_rejects_unlowered_enum() -> None:
    class _Value(StrEnum):
        ITEM = "item"

    with pytest.raises(TypeError, match="canonical string value"):
        canonical_json_bytes(_Value.ITEM)


def test_manifest_and_grant_fingerprints_cannot_be_self_reported() -> None:
    with pytest.raises(ValueError, match="manifest fingerprint mismatch"):
        replace(
            POSTGRES_LATEST_SCHEMA_MANIFEST,
            manifest_fingerprint="sha256:" + "0" * 64,
        )
    target = PostgresRelationGrantTargetFact(
        target_kind="relation",
        schema_name="public",
        relation_name="sessions",
        relation_kind="table",
    )
    with pytest.raises(ValueError, match="invalid for its target kind"):
        PostgresRuntimeGrantRequirementFact(
            target=target,
            ordered_required_privileges=("EXECUTE",),
            requirement_fingerprint="sha256:" + "0" * 64,
        )


def test_registry_validates_resources_and_prefix_recurrence() -> None:
    POSTGRES_MIGRATION_REGISTRY.verify_resources()
    assert tuple(
        definition.version for definition in POSTGRES_MIGRATION_REGISTRY.definitions
    ) == tuple(range(9))
    assert (
        POSTGRES_MIGRATION_REGISTRY.registry_fingerprint
        == POSTGRES_MIGRATION_REGISTRY.definitions[-1].registry_prefix_fingerprint
    )
    tampered = replace(
        POSTGRES_MIGRATION_REGISTRY.definitions[0],
        registry_prefix_fingerprint="sha256:" + "0" * 64,
    )
    with pytest.raises(ValueError, match="prefix recurrence"):
        PostgresMigrationRegistry(
            definitions=(
                tampered,
                *POSTGRES_MIGRATION_REGISTRY.definitions[1:],
            ),
            registry_fingerprint=POSTGRES_MIGRATION_REGISTRY.registry_fingerprint,
        )


def test_historical_migration_identity_has_append_only_golden_vectors() -> None:
    assert tuple(
        (
            manifest.manifest_fingerprint,
            definition.migration_contract_fingerprint,
            definition.registry_prefix_fingerprint,
        )
        for manifest, definition in zip(
            POSTGRES_SCHEMA_MANIFESTS,
            POSTGRES_MIGRATION_REGISTRY.definitions,
            strict=True,
        )
    ) == (
        (
            "sha256:126a57fadde80e48c463a60a2e885f79ac49a4f4ff0c33847400c5bd4af14fdd",
            "sha256:5ba8a4c202aa001096646154b2354b1c2bfa07ee60a4b2757f1618cd41e9e5c7",
            "sha256:e9092a92bce0fc039d387f65feac45a1e64315133a75f2b7f434ea97bb35688a",
        ),
        (
            "sha256:ddb2506fe30b0a3337f50691e8fc00a400571bd7c9ec6d51cc7c8ece9a16919d",
            "sha256:0e7de5fd62b4be89e62cbc91f90717fc58f2cfc158b37d67da97b3e7f1c11d9a",
            "sha256:906ca42deff42743cc8a867bfc6ef021c6b1052af5fe38d24985cbc27db53f49",
        ),
        (
            "sha256:227bb92c5b3939827355083eeb936f808893385bf7bf739a9a04fa4039c3e5ed",
            "sha256:549d07d2d9db310ae1a68f585863d811d2dc1960df5870a0b1bedc7bb388da0d",
            "sha256:89f19dd94c4275d34312a2cfbd86d2a886741a58676910c62619ea1291edefd4",
        ),
        (
            "sha256:deffede1aad70a8fb00af378bbe51875c5f7e99cc79f0e487ff394f0421c9d2e",
            "sha256:282689016b603d524d2d9fcad3800b1ab826cf8ae282a3fd2d33e8bbd2e3115e",
            "sha256:d6ae2f454f07b39740e76d13138973eac5416b29099f44fa117c9bc9e2b8fd08",
        ),
        (
            "sha256:e76cddc7b6489d4733187571dd42257e87ec4f80e08c581afe2df72a711e77c7",
            "sha256:786879007815a04c9fc68f759f42c9b726073d9ac123fa80a1688c6c140a7069",
            "sha256:15a224ceebb327d24b5f36c38bd9da0305defb71dff98f48aa8223647c8444e1",
        ),
        (
            "sha256:5bd653db8cac3df03c016b4a5d89bfb189ad8cf6c17982703e7b5f325f63852a",
            "sha256:2ba93cc6fc90947c583c9bca8227c0eef196abe286f5ce71a3af40575fe31697",
            "sha256:5300c97701d3e6d9ccd1b67c5ce4bc3497c7cc21685b37445af6359a84710500",
        ),
        (
            "sha256:f172f247d959ab1fa8d5028882cd92c32dd40bc1fdd4a4f51b77b13350192874",
            "sha256:557c5a373591eb37e9cf9a6263926a562357c110522a04a0474d36be0da9c61f",
            "sha256:188c78d4e3e8a828b3a80ec8507a6c1c5f800a61fa7cf6950c2407abb0b48c2f",
        ),
        (
            "sha256:a7689928d14f88a5c08e12ddc1311c86de76befcb1b816b5410526580e484835",
            "sha256:64dfd448b9c76e9a6e35395c3b3b2854893384afd80e0802fcbeb66fbb540f3c",
            "sha256:227ac1442218692078a1d7a2e73b434d4c4ed907032b02edfbd22f45e0a3e6fe",
        ),
        (
            "sha256:b591518375945dcb2f8e077a9064e0c2887c358ba1a186a9d8d8cb78c80afd5b",
            "sha256:52f77c411126306ea35bb6e8753113dfbf3fb7ea28bd5ab0ae3c441afd60e7d6",
            "sha256:d513c4e4dd549a1ed8810ae4ce4ebf190f984f9a9c97bd1e1b91ff174806d849",
        ),
    )
    assert tuple(
        len(item.reserved_object_names) for item in POSTGRES_SCHEMA_MANIFESTS
    ) == (
        1,
        1,
        11,
        21,
        27,
        61,
        61,
        61,
        61,
    )
    assert all(
        identity.object_name != "memory_governance_decisions"
        for identity in POSTGRES_SCHEMA_MANIFESTS[0].reserved_object_names
    )


def test_migration_history_classifier_checks_every_historical_row() -> None:
    definitions = POSTGRES_MIGRATION_REGISTRY.definitions
    rows = tuple(
        PostgresMigrationLedgerRowFact(
            schema_version="postgres_migration_ledger_row.v1",
            version=definition.version,
            name=definition.name,
            resource_checksum=definition.expected_sha256,
            migration_contract_fingerprint=(definition.migration_contract_fingerprint),
            registry_prefix_fingerprint=definition.registry_prefix_fingerprint,
            application_version="test",
            applied_at_utc="2026-07-23T00:00:00Z",
        )
        for definition in definitions
    )
    assert classify_migration_history(rows) is PostgresMigrationHistoryStatus.UP_TO_DATE
    tampered = replace(rows[0], name="tampered_genesis")
    assert classify_migration_history((tampered, *rows[1:])) is (
        PostgresMigrationHistoryStatus.CONFLICT
    )
    assert classify_migration_history(()) is PostgresMigrationHistoryStatus.CONFLICT


def test_fresh_database_migrates_to_latest_and_second_run_is_noop() -> None:
    with _fresh_database(migrated=False) as database:
        runner = PostgresMigrationRunner(
            admin_dsn=database.admin_dsn,
            runtime_dsn=database.runtime_dsn,
        )
        first = runner.migrate(deadline_monotonic=monotonic() + 120.0)
        second = runner.migrate(deadline_monotonic=monotonic() + 120.0)
        assert first.status == "preparation_required"
        assert first.applied_versions == (0, 1, 2, 3, 4, 5)
        assert second.status == "preparation_required"
        assert second.applied_versions == ()
        assert second.migration_head_version == 5
        final = _advance_projection_migrations(
            database=database,
            runner=runner,
        )
        assert final.status == "migrated"
        assert final.migration_head_version == 8
        assert final.registry_prefix_fingerprint == (
            POSTGRES_MIGRATION_REGISTRY.registry_fingerprint
        )
        assert _deep_fingerprint(database)


def test_final_binary_advances_historical_v4_database_through_all_dpj_gates() -> None:
    with _fresh_database(migrated=False) as database:
        historical_definitions = POSTGRES_MIGRATION_REGISTRY.definitions[:5]
        historical_registry = PostgresMigrationRegistry(
            definitions=historical_definitions,
            registry_fingerprint=(
                historical_definitions[-1].registry_prefix_fingerprint
            ),
        )
        historical = PostgresMigrationRunner(
            admin_dsn=database.admin_dsn,
            runtime_dsn=database.runtime_dsn,
            registry=historical_registry,
        ).migrate(deadline_monotonic=monotonic() + 120.0)
        assert historical.status == "migrated"
        assert historical.applied_versions == (0, 1, 2, 3, 4)
        assert historical.migration_head_version == 4

        final_runner = PostgresMigrationRunner(
            admin_dsn=database.admin_dsn,
            runtime_dsn=database.runtime_dsn,
        )
        v5 = final_runner.migrate(deadline_monotonic=monotonic() + 120.0)
        assert v5.status == "preparation_required"
        assert v5.applied_versions == (5,)
        assert v5.migration_head_version == 5

        final = _advance_projection_migrations(
            database=database,
            runner=final_runner,
        )
        assert final.status == "migrated"
        assert final.applied_versions == (8,)
        assert final.migration_head_version == 8
        assert final.registry_prefix_fingerprint == (
            POSTGRES_MIGRATION_REGISTRY.registry_fingerprint
        )
        assert _deep_fingerprint(database)


def test_concurrent_migrators_serialize_on_database_advisory_lock() -> None:
    with _fresh_database(migrated=False) as database:

        def migrate():
            return PostgresMigrationRunner(
                admin_dsn=database.admin_dsn,
                runtime_dsn=database.runtime_dsn,
            ).migrate(deadline_monotonic=monotonic() + 120.0)

        with ThreadPoolExecutor(max_workers=2) as executor:
            reports = tuple(executor.map(lambda _index: migrate(), range(2)))
        assert tuple(report.status for report in reports) == (
            "preparation_required",
            "preparation_required",
        )
        assert sum(len(report.applied_versions) for report in reports) == 6


def test_two_fresh_databases_have_same_logical_catalog_fingerprint() -> None:
    with _fresh_database(migrated=True) as first:
        with _fresh_database(migrated=True) as second:
            assert first.database_name != second.database_name
            assert _deep_fingerprint(first) == _deep_fingerprint(second)


def test_unmanaged_pulsara_object_is_not_adopted() -> None:
    with _fresh_database(migrated=False) as database:
        with psycopg.connect(database.admin_dsn, autocommit=True) as connection:
            connection.execute("CREATE TABLE public.sessions (id text primary key)")
        with pytest.raises(PostgresSchemaError) as failure:
            PostgresMigrationRunner(
                admin_dsn=database.admin_dsn,
                runtime_dsn=database.runtime_dsn,
            ).migrate(deadline_monotonic=monotonic() + 30.0)
        assert failure.value.code is PostgresSchemaFailureCode.UNMANAGED_DATABASE


def test_admin_and_runtime_database_identity_must_match(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    with _fresh_database(migrated=False) as other:
        with pytest.raises(PostgresSchemaError) as failure:
            PostgresMigrationRunner(
                admin_dsn=other.admin_dsn,
                runtime_dsn=migrated_postgres_database.runtime_dsn,
            ).migrate(deadline_monotonic=monotonic() + 30.0)
        assert (
            failure.value.code is PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH
        )


def test_migrator_rejects_different_admin_runtime_network_targets() -> None:
    with pytest.raises(PostgresSchemaError) as failure:
        PostgresMigrationRunner(
            admin_dsn="postgresql://admin@localhost:5432/pulsara",
            runtime_dsn="postgresql://runtime@localhost:5433/pulsara",
        )
    assert failure.value.code is PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("checksum", "0" * 64),
        ("migration_contract_fingerprint", "sha256:" + "0" * 64),
        ("registry_prefix_fingerprint", "sha256:" + "0" * 64),
    ),
)
def test_migration_ledger_tamper_is_rejected(column: str, value: str) -> None:
    with _fresh_database(migrated=True) as database:
        with psycopg.connect(database.admin_dsn, autocommit=True) as connection:
            connection.execute(
                f"UPDATE public.pulsara_schema_migrations SET {column} = %s WHERE version = 4",
                (value,),
            )
        with pytest.raises(PostgresSchemaError) as failure:
            PostgresMigrationRunner(
                admin_dsn=database.admin_dsn,
                runtime_dsn=database.runtime_dsn,
            ).migrate(deadline_monotonic=monotonic() + 30.0)
        assert failure.value.code is PostgresSchemaFailureCode.HISTORY_CONFLICT


def test_fast_verifier_detects_runtime_default_drift() -> None:
    with _fresh_database(migrated=True) as database:
        with psycopg.connect(database.admin_dsn, autocommit=True) as connection:
            connection.execute(
                "ALTER TABLE public.sessions ALTER COLUMN created_at DROP DEFAULT"
            )
        factory = PostgresRuntimeConnectionFactory(database.runtime_dsn)
        with factory.connect(
            deadline_monotonic=monotonic() + 30.0,
            autocommit=True,
        ) as connection:
            with pytest.raises(PostgresSchemaError) as failure:
                PostgresSchemaVerifier().verify_fast_connection(
                    connection,
                    database_target_fingerprint=factory.database_target_fingerprint,
                    deadline_monotonic=monotonic() + 30.0,
                )
        assert failure.value.code is PostgresSchemaFailureCode.CATALOG_DRIFT


def test_migration_commit_confirmation_has_distinct_full_none_and_conflict() -> None:
    with _fresh_database(migrated=True) as full_database:
        runner = PostgresMigrationRunner(
            admin_dsn=full_database.admin_dsn,
            runtime_dsn=full_database.runtime_dsn,
        )
        identity = (
            PostgresRuntimeConnectionFactory(full_database.runtime_dsn)
            .preflight(deadline_monotonic=monotonic() + 30.0)
            .database_identity
        )
        outcome, connection = runner._confirm_migration_commit(  # noqa: SLF001
            runtime_identity=identity,
            definition=POSTGRES_MIGRATION_REGISTRY.definition(8),
            deadline_monotonic=monotonic() + 30.0,
        )
        assert outcome is PostgresCommitConfirmation.FULL
        assert connection is not None
        connection.close()

    with _fresh_database(migrated=False) as none_database:
        runner = PostgresMigrationRunner(
            admin_dsn=none_database.admin_dsn,
            runtime_dsn=none_database.runtime_dsn,
        )
        identity = (
            PostgresRuntimeConnectionFactory(none_database.runtime_dsn)
            .preflight(deadline_monotonic=monotonic() + 30.0)
            .database_identity
        )
        outcome, connection = runner._confirm_migration_commit(  # noqa: SLF001
            runtime_identity=identity,
            definition=POSTGRES_MIGRATION_REGISTRY.definition(0),
            deadline_monotonic=monotonic() + 30.0,
        )
        assert outcome is PostgresCommitConfirmation.NONE
        assert connection is not None
        connection.close()

    with _fresh_database(migrated=False) as conflict_database:
        with psycopg.connect(
            conflict_database.admin_dsn, autocommit=True
        ) as connection:
            connection.execute("CREATE TABLE public.sessions (id text primary key)")
        runner = PostgresMigrationRunner(
            admin_dsn=conflict_database.admin_dsn,
            runtime_dsn=conflict_database.runtime_dsn,
        )
        identity = (
            PostgresRuntimeConnectionFactory(conflict_database.runtime_dsn)
            .preflight(deadline_monotonic=monotonic() + 30.0)
            .database_identity
        )
        outcome, connection = runner._confirm_migration_commit(  # noqa: SLF001
            runtime_identity=identity,
            definition=POSTGRES_MIGRATION_REGISTRY.definition(0),
            deadline_monotonic=monotonic() + 30.0,
        )
        assert outcome is PostgresCommitConfirmation.CONFLICT
        assert connection is not None
        connection.close()


def test_migration_commit_confirmation_reports_unresolved_without_corruption_claim(
    monkeypatch,
) -> None:
    def unavailable(*_args, **_kwargs):
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.CONNECTION_FAILED,
            "temporary outage",
            retryable=True,
        )

    monkeypatch.setattr(
        "pulsara_agent.storage.migrations.runner.PostgresAdminConnectionFactory.connect",
        unavailable,
    )
    runner = PostgresMigrationRunner(
        admin_dsn="postgresql://admin@localhost/database",
        runtime_dsn="postgresql://runtime@localhost/database",
    )
    outcome, connection = runner._confirm_migration_commit(  # noqa: SLF001
        runtime_identity=PostgresDatabaseIdentity(
            database_name="database",
            database_oid=1,
            runtime_role="runtime",
            normalized_search_path=("public",),
            server_version_num=160000,
        ),
        definition=POSTGRES_MIGRATION_REGISTRY.definition(0),
        deadline_monotonic=monotonic() + 30.0,
    )
    assert outcome is PostgresCommitConfirmation.UNRESOLVED
    assert connection is None


def test_runtime_role_can_read_ledger_but_cannot_create_schema_objects(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    with psycopg.connect(
        migrated_postgres_database.runtime_dsn,
        autocommit=True,
    ) as connection:
        rows = read_migration_ledger(connection)
        assert rows is not None and rows[-1].version == 8
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                f"CREATE TABLE public.forbidden_{uuid4().hex} (id integer)"
            )


def test_up_to_date_database_can_rebind_restricted_runtime_role() -> None:
    role_name = f"pulsara_test_role_{uuid4().hex[:12]}"
    password = uuid4().hex
    with psycopg.connect(admin_root_dsn(), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role_name),
                sql.Literal(password),
            )
        )
    try:
        with _fresh_database(migrated=True) as database:
            parameters = conninfo_to_dict(database.runtime_dsn)
            parameters["user"] = role_name
            parameters["password"] = password
            replacement_runtime_dsn = make_conninfo(**parameters)
            report = PostgresMigrationRunner(
                admin_dsn=database.admin_dsn,
                runtime_dsn=replacement_runtime_dsn,
            ).migrate(deadline_monotonic=monotonic() + 120.0)
            assert report.status == "up_to_date"
            assert report.added_grant_fingerprints
            factory = PostgresRuntimeConnectionFactory(replacement_runtime_dsn)
            bundle = factory.verify(deadline_monotonic=monotonic() + 30.0)
            assert bundle.binding.runtime_role == role_name
            assert not bundle.result.effective_privilege_result.missing_requirement_fingerprints
            with factory.connect(
                deadline_monotonic=monotonic() + 30.0,
                autocommit=True,
            ) as connection:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(
                        f"CREATE TABLE public.forbidden_{uuid4().hex} (id integer)"
                    )
    finally:
        with psycopg.connect(admin_root_dsn(), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name))
            )


def test_verified_direct_connection_commits_and_pool_validates(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    lease = acquire_verified_postgres_access_sync(
        migrated_postgres_database.runtime_dsn,
        deadline_monotonic=monotonic() + 30.0,
    )
    session_id = f"runtime:schema-provider:{uuid4().hex}"
    try:
        provider = lease.connection_provider
        with provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=monotonic() + 30.0,
        ) as connection:
            connection.execute(
                "INSERT INTO public.sessions (id, workspace_root) VALUES (%s, %s)",
                (session_id, "/tmp/schema-provider"),
            )
        pool = provider.pool(
            lane=PostgresConnectionLane.EVENT_LOG,
            deadline_monotonic=monotonic() + 30.0,
        )
        with pool.connection(timeout=5.0) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM public.sessions WHERE id = %s", (session_id,)
                ).fetchone()[0]
                == 1
            )
        provider.close_pool(PostgresConnectionLane.EVENT_LOG)
    finally:
        lease.release()


@pytest.mark.parametrize(
    "dsn",
    (
        "postgresql://runtime@host1,host2/database",
        "postgresql://runtime@localhost/database?options=-csearch_path%3Dpublic",
        "postgresql://runtime@localhost/database?target_session_attrs=read-write",
        "postgresql://runtime@localhost/database?application_name=caller-owned",
        "postgresql://runtime@localhost/database?unknown_parameter=x",
        "postgresql:///database?user=runtime",
    ),
)
def test_runtime_conninfo_rejects_ambiguous_authority(dsn: str) -> None:
    with pytest.raises(PostgresSchemaError) as failure:
        PostgresRuntimeConnectionFactory(dsn)
    assert failure.value.code is PostgresSchemaFailureCode.CONNINFO_UNSUPPORTED


def test_verified_access_lease_is_not_serializable_and_rejects_after_release(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    lease = acquire_verified_postgres_access_sync(
        migrated_postgres_database.runtime_dsn,
        deadline_monotonic=monotonic() + 30.0,
    )
    with pytest.raises(TypeError):
        pickle.dumps(lease)
    lease.release()
    with pytest.raises(RuntimeError, match="released"):
        _ = lease.connection_provider


def test_saved_borrower_facade_and_pool_reject_after_lease_release(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    factory = PostgresRuntimeConnectionFactory(migrated_postgres_database.runtime_dsn)
    bundle = factory.verify(deadline_monotonic=monotonic() + 30.0)
    shared_provider = VerifiedPostgresConnectionProvider(
        factory=factory,
        binding=bundle.binding,
    )
    first = VerifiedPostgresAccessLease(shared_provider)
    second = VerifiedPostgresAccessLease(shared_provider)
    saved_facade = first.connection_provider
    saved_pool = saved_facade.pool(
        lane=PostgresConnectionLane.EVENT_LOG,
        deadline_monotonic=monotonic() + 30.0,
    )
    first.release()

    with pytest.raises(PostgresSchemaError) as direct_failure:
        with saved_facade.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=monotonic() + 30.0,
        ):
            pass
    assert direct_failure.value.code is PostgresSchemaFailureCode.ACCESS_LEASE_RELEASED
    with pytest.raises(PostgresSchemaError) as pool_failure:
        with saved_pool.connection(timeout=5.0):
            pass
    assert pool_failure.value.code is PostgresSchemaFailureCode.ACCESS_LEASE_RELEASED

    with second.connection_provider.connection(
        lane=PostgresConnectionLane.HOST_CONTROL,
        deadline_monotonic=monotonic() + 30.0,
    ) as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    second.connection_provider.close_pool(PostgresConnectionLane.EVENT_LOG)
    second.release()
    shared_provider.close()


def test_pool_identity_failure_retires_pool_outside_configure_thread(
    migrated_postgres_database: MigratedPostgresTestDatabase,
    monkeypatch,
) -> None:
    factory = PostgresRuntimeConnectionFactory(migrated_postgres_database.runtime_dsn)
    bundle = factory.verify(deadline_monotonic=monotonic() + 30.0)
    provider = VerifiedPostgresConnectionProvider(
        factory=factory,
        binding=bundle.binding,
    )
    closed = Event()
    close_thread_ids: list[int] = []

    class FakePool:
        def close(self) -> None:
            close_thread_ids.append(get_ident())
            closed.set()

    class FakeConnection:
        closed = False

        def close(self) -> None:
            self.closed = True

    provider._pools[PostgresConnectionLane.EVENT_LOG] = FakePool()  # type: ignore[assignment]  # noqa: SLF001
    monkeypatch.setattr(
        factory,
        "validate_effective_endpoint",
        lambda _connection: (_ for _ in ()).throw(
            PostgresSchemaError(
                PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH,
                "mismatch",
            )
        ),
    )
    configure_thread = get_ident()
    with pytest.raises(PostgresSchemaError):
        provider._validate_physical_connection(  # type: ignore[arg-type]  # noqa: SLF001
            FakeConnection(),
            deadline_monotonic=monotonic() + 30.0,
        )
    assert closed.wait(2.0)
    assert close_thread_ids and close_thread_ids[0] != configure_thread
    with pytest.raises(PostgresSchemaError) as invalidated:
        provider.borrow()
    assert invalidated.value.code is PostgresSchemaFailureCode.ACCESS_LEASE_RELEASED


def test_transient_physical_probe_failure_does_not_invalidate_provider(
    migrated_postgres_database: MigratedPostgresTestDatabase,
    monkeypatch,
) -> None:
    factory = PostgresRuntimeConnectionFactory(migrated_postgres_database.runtime_dsn)
    bundle = factory.verify(deadline_monotonic=monotonic() + 30.0)
    provider = VerifiedPostgresConnectionProvider(
        factory=factory,
        binding=bundle.binding,
    )
    connection = factory.connect(
        deadline_monotonic=monotonic() + 30.0,
        autocommit=True,
    )
    with monkeypatch.context() as probe_patch:
        probe_patch.setattr(
            "pulsara_agent.storage.postgres_connection_provider._read_identity_from_connection",
            lambda _connection: (_ for _ in ()).throw(
                psycopg.errors.QueryCanceled("probe timeout")
            ),
        )
        with pytest.raises(PostgresSchemaError) as failure:
            provider._validate_physical_connection(  # noqa: SLF001
                connection,
                deadline_monotonic=monotonic() + 30.0,
            )
    assert failure.value.code is PostgresSchemaFailureCode.DEADLINE_EXCEEDED
    assert connection.closed

    borrower = provider.borrow()
    with borrower.connection(
        lane=PostgresConnectionLane.HOST_CONTROL,
        deadline_monotonic=monotonic() + 30.0,
    ) as recovered_connection:
        assert recovered_connection.execute("SELECT 1").fetchone()[0] == 1
    borrower.release()
    provider.close()


def test_catalog_semantic_fingerprint_contains_no_database_oid(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    with psycopg.connect(
        migrated_postgres_database.runtime_dsn,
        autocommit=True,
    ) as connection:
        catalog = PostgresCatalogCanonicalizer().read_deep(connection)
    encoded = canonical_json_bytes(catalog)
    assert b'"oid"' not in encoded


def test_db_cli_verify_does_not_load_llm_configuration_or_expose_dsn(
    migrated_postgres_database: MigratedPostgresTestDatabase,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "SCHEMA_TEST_POSTGRES_DSN", migrated_postgres_database.runtime_dsn
    )
    monkeypatch.setattr(
        cli.PulsaraSettings,
        "from_env",
        classmethod(
            lambda cls, prefix="PULSARA": (_ for _ in ()).throw(
                AssertionError("db command must not load LLM settings")
            )
        ),
    )
    args = cli.build_parser().parse_args(
        ["db", "verify", "--prefix", "SCHEMA_TEST", "--deep"]
    )
    report = cli._database_command(args)  # noqa: SLF001
    assert report["status"] == "verified"
    assert report["mode"] == "deep"
    rendered = repr(report)
    assert "postgresql://" not in rendered
    assert "password=" not in rendered
    assert "dsn" not in rendered.lower()
    assert report["expected_object_manifest_fingerprint"] == (
        POSTGRES_LATEST_SCHEMA_MANIFEST.manifest_fingerprint
    )
    assert (
        report["expected_deep_catalog_fingerprint"]
        == (report["observed_deep_catalog_fingerprint"])
    )
    assert "runtime_role_can_create_in_public_schema" in report
    assert "warnings" in report


def test_db_projection_status_is_bounded_and_secret_safe(
    migrated_postgres_database: MigratedPostgresTestDatabase,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "DPJ_CLI_POSTGRES_DSN",
        migrated_postgres_database.runtime_dsn,
    )
    args = cli.build_parser().parse_args(
        [
            "db",
            "projections",
            "status",
            "--prefix",
            "DPJ_CLI",
            "--limit",
            "2",
        ]
    )
    report = cli._database_command(args)  # noqa: SLF001
    assert report["job_page"]["limit"] == 2
    assert len(report["jobs"]) <= 2
    assert len(report["surface_deliveries"]) <= 2
    rendered = repr(report)
    assert "postgresql://" not in rendered
    assert "password=" not in rendered
    assert "dsn" not in rendered.lower()

    drain = cli.build_parser().parse_args(
        [
            "db",
            "projections",
            "drain-pre-activation",
            "--kind",
            "run_timeline.v1",
        ]
    )
    assert drain.kind == "run_timeline.v1"

    surface_retry = cli.build_parser().parse_args(
        [
            "db",
            "projections",
            "surface-retry",
            "--mutation",
            "mutation:test",
            "--surface",
            "vector_index.v1",
            "--authority-id",
            "operator:test",
        ]
    )
    assert surface_retry.db_projection_command == "surface-retry"
    assert surface_retry.mutation == "mutation:test"
    assert surface_retry.surface == "vector_index.v1"

    surface_decommission = cli.build_parser().parse_args(
        [
            "db",
            "projections",
            "surface-decommission",
            "--mutation",
            "mutation:test",
            "--surface",
            "oxigraph.v1",
            "--authority-id",
            "operator:test",
            "--rebuild-result-receipt",
            "projection-result-receipt:test",
        ]
    )
    assert surface_decommission.db_projection_command == (
        "surface-decommission"
    )
    assert surface_decommission.rebuild_result_receipt == (
        "projection-result-receipt:test"
    )

    seed_repair = cli.build_parser().parse_args(
        [
            "db",
            "projections",
            "seed-repair",
            "--failure",
            "projection-seed-failure:test",
            "--action",
            "retry_after_authority_repair",
            "--authority-id",
            "operator:test",
        ]
    )
    assert seed_repair.db_projection_command == "seed-repair"
    assert seed_repair.failure == "projection-seed-failure:test"


def test_db_status_reports_old_row_tamper_as_conflict(monkeypatch) -> None:
    with _fresh_database(migrated=True) as database:
        with psycopg.connect(database.admin_dsn, autocommit=True) as connection:
            connection.execute(
                "UPDATE public.pulsara_schema_migrations "
                "SET name = 'tampered_genesis' WHERE version = 0"
            )
        monkeypatch.setattr(
            PostgresRuntimeConnectionFactory,
            "preflight",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("db status must use one physical connection snapshot")
            ),
        )
        monkeypatch.setenv("SCHEMA_STATUS_POSTGRES_DSN", database.runtime_dsn)
        args = cli.build_parser().parse_args(
            ["db", "status", "--prefix", "SCHEMA_STATUS"]
        )
        report = cli._database_command(args)  # noqa: SLF001
        assert report["status"] == "conflict"


def test_db_verify_projects_public_schema_create_warning(monkeypatch) -> None:
    with _fresh_database(migrated=True) as database:
        PostgresMigrationRunner(
            admin_dsn=database.admin_dsn,
            runtime_dsn=database.admin_dsn,
        ).migrate(deadline_monotonic=monotonic() + 120.0)
        monkeypatch.setenv("SCHEMA_DDL_POSTGRES_DSN", database.admin_dsn)
        args = cli.build_parser().parse_args(["db", "verify", "--prefix", "SCHEMA_DDL"])
        report = cli._database_command(args)  # noqa: SLF001
        assert report["runtime_role_can_create_in_public_schema"] is True
        assert report["warnings"] == ("runtime_role_can_create_in_public_schema",)


def test_db_migrate_has_no_admin_dsn_fallback(monkeypatch) -> None:
    monkeypatch.setenv(
        "SCHEMA_NO_ADMIN_POSTGRES_DSN",
        "postgresql://runtime@localhost/database",
    )
    monkeypatch.delenv("SCHEMA_NO_ADMIN_POSTGRES_ADMIN_DSN", raising=False)
    args = cli.build_parser().parse_args(
        ["db", "migrate", "--prefix", "SCHEMA_NO_ADMIN"]
    )
    with pytest.raises(PostgresSchemaError) as failure:
        cli._database_command(args)  # noqa: SLF001
    assert failure.value.code is PostgresSchemaFailureCode.ADMIN_DSN_REQUIRED
