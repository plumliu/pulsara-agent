"""Explicit database reset: atomic storage and the ordinary local HTTP path."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event
from time import monotonic
from unittest.mock import AsyncMock

from aiohttp import ClientSession
import psycopg
import pytest

from pulsara_agent.settings import LocalPostgresConfig
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresRuntimeConnectionFactory,
)
from pulsara_agent.tool_permission import default_permission_policy
from pulsara_agent.web_app.application import (
    DatabaseDataPlaneState,
    LocalWebApplication,
)
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_runtime as make_runtime
from tests.test_stage5_clean_migration import (
    _migrated_database,
    _runner,
    _CommitFaultFactory,
)

pytestmark = pytest.mark.postgres


def _seed_old_database(database):
    with psycopg.connect(database.admin_dsn) as conn:
        conn.execute("CREATE TABLE pulsara_v3.reset_marker(value text)")
        conn.execute("INSERT INTO pulsara_v3.reset_marker VALUES ('keep-on-failure')")
        conn.execute("CREATE TABLE public.unrelated_data(value text)")
        conn.execute("INSERT INTO public.unrelated_data VALUES ('keep-always')")
        conn.execute(
            "UPDATE public.pulsara_schema_migrations SET universe_fingerprint = %s",
            ("sha256:" + "0" * 64,),
        )


def test_explicit_reset_reinstalls_old_universe_and_preserves_unrelated_data():
    with _migrated_database() as database:
        _seed_old_database(database)
        runner = _runner(database)
        with pytest.raises(PostgresSchemaError) as failure:
            runner.migrate(deadline_monotonic=monotonic() + 30)
        assert (
            failure.value.code
            is PostgresSchemaFailureCode.MIGRATION_UNIVERSE_RESET_REQUIRED
        )
        report = runner.reset(deadline_monotonic=monotonic() + 30)
        assert report.applied_versions == (0,)
        PostgresRuntimeConnectionFactory(database.runtime_dsn).verify(
            deadline_monotonic=monotonic() + 30
        )
        with psycopg.connect(database.admin_dsn) as conn:
            assert (
                conn.execute(
                    "SELECT to_regclass('pulsara_v3.reset_marker')"
                ).fetchone()[0]
                is None
            )
            assert (
                conn.execute("SELECT value FROM public.unrelated_data").fetchone()[0]
                == "keep-always"
            )


def test_failed_reset_rolls_back_the_deletion(monkeypatch):
    with _migrated_database() as database:
        _seed_old_database(database)
        runner = _runner(database)

        def fail(*args, **kwargs):
            raise RuntimeError("grant failure after baseline installation")

        monkeypatch.setattr(runner._grant_executor, "reconcile", fail)
        with pytest.raises(PostgresSchemaError):
            runner.reset(deadline_monotonic=monotonic() + 30)
        with psycopg.connect(database.admin_dsn) as conn:
            assert (
                conn.execute("SELECT value FROM pulsara_v3.reset_marker").fetchone()[0]
                == "keep-on-failure"
            )
            assert (
                conn.execute(
                    "SELECT universe_fingerprint FROM public.pulsara_schema_migrations"
                ).fetchone()[0]
                == "sha256:" + "0" * 64
            )


@pytest.mark.parametrize("disposition", ("FULL", "NONE"))
def test_reset_does_not_confirm_or_retry_ambiguous_commit(disposition):
    with _migrated_database() as database:
        _seed_old_database(database)
        runner = _runner(database)
        runner._admin = _CommitFaultFactory(
            runner._admin, disposition=disposition, admin_dsn=database.admin_dsn
        )
        with pytest.raises(PostgresSchemaError) as failure:
            runner.reset(deadline_monotonic=monotonic() + 30)
        assert (
            failure.value.code
            is PostgresSchemaFailureCode.MIGRATION_CONFIRMATION_UNRESOLVED
        )
        with psycopg.connect(database.admin_dsn) as conn:
            marker = conn.execute(
                "SELECT to_regclass('pulsara_v3.reset_marker')"
            ).fetchone()[0]
            assert (marker is not None) == (disposition == "NONE")


@pytest.mark.parametrize("prepared", (False, True))
def test_reset_http_confirms_target_and_obeys_data_plane_lifetime(
    tmp_path, monkeypatch, prepared
):
    async def run(database):
        runtime = make_runtime(postgres_dsn=database.runtime_dsn)
        settings = runtime.settings
        postgres = LocalPostgresConfig(database.runtime_dsn, database.admin_dsn)
        settings.value = replace(settings.read(), postgres=postgres)
        saved = settings.read()
        monkeypatch.setattr(type(runtime.catalog), "refresh", AsyncMock())
        app = LocalWebApplication(
            settings=settings,
            model_runtime=runtime,
            catalog=runtime.catalog,
            workspace_input=HostWorkspaceInput("project", tmp_path),
            permission_policy=default_permission_policy(),
        )
        await app.start()
        try:
            assert app.database_state is (
                DatabaseDataPlaneState.READY
                if prepared
                else DatabaseDataPlaneState.RESET_REQUIRED
            )
            reset = AsyncMock(wraps=app.http._reset_postgres_data)
            app.http._reset_postgres_data = reset
            target = {
                "runtime_dsn": database.runtime_dsn,
                "admin_dsn": database.admin_dsn,
            }
            async with ClientSession() as client:
                url = app.origin + "/api/local-settings/postgres/reset"
                for body in (
                    target,
                    {**target, "confirmed": False},
                    {**target, "runtime_dsn": "changed", "confirmed": True},
                ):
                    async with client.post(url, json=body) as response:
                        assert response.status == 409
                assert reset.await_count == 0
                async with client.post(
                    url, json={**target, "confirmed": True}
                ) as response:
                    assert response.status == 200, await response.text()
                    assert (await response.json())["restart_required"] is prepared
                expected_state = (
                    DatabaseDataPlaneState.RESTART_REQUIRED
                    if prepared
                    else DatabaseDataPlaneState.READY
                )
                assert app.database_state is expected_state
                await app.refresh_database_state()
                assert app.database_state is expected_state
                async with client.get(app.origin + "/api/sessions") as response:
                    assert response.status == (503 if prepared else 200)
                assert settings.read() == saved
        finally:
            await app.aclose()

    with _migrated_database() as database:
        if not prepared:
            _seed_old_database(database)
        asyncio.run(run(database))


def test_reset_close_failure_preserves_database(tmp_path, monkeypatch):
    async def run(database):
        runtime = make_runtime(postgres_dsn=database.runtime_dsn)
        monkeypatch.setattr(type(runtime.catalog), "refresh", AsyncMock())
        app = LocalWebApplication(
            settings=runtime.settings,
            model_runtime=runtime,
            catalog=runtime.catalog,
            workspace_input=HostWorkspaceInput("project", tmp_path),
            permission_policy=default_permission_policy(),
        )
        await app.start()
        close = app.sessions.aclose
        try:
            with psycopg.connect(database.admin_dsn) as conn:
                conn.execute("CREATE TABLE pulsara_v3.reset_marker(value text)")
            monkeypatch.setattr(
                app.sessions,
                "aclose",
                AsyncMock(side_effect=RuntimeError("close failed")),
            )
            with pytest.raises(RuntimeError, match="close failed"):
                await app.reset_postgres(
                    LocalPostgresConfig(database.runtime_dsn, database.admin_dsn)
                )
            with psycopg.connect(database.admin_dsn) as conn:
                assert (
                    conn.execute(
                        "SELECT to_regclass('pulsara_v3.reset_marker')"
                    ).fetchone()[0]
                    is not None
                )
            assert app.database_state is DatabaseDataPlaneState.RESTART_REQUIRED
        finally:
            monkeypatch.setattr(app.sessions, "aclose", close)
            await app.aclose()

    with _migrated_database() as database:
        asyncio.run(run(database))


def test_cancelled_reset_waits_for_physical_transaction_before_readmission(
    tmp_path, monkeypatch
):
    async def run(database):
        runtime = make_runtime(postgres_dsn=database.runtime_dsn)
        monkeypatch.setattr(type(runtime.catalog), "refresh", AsyncMock())
        app = LocalWebApplication(
            settings=runtime.settings,
            model_runtime=runtime,
            catalog=runtime.catalog,
            workspace_input=HostWorkspaceInput("project", tmp_path),
            permission_policy=default_permission_policy(),
        )
        await app.start()
        entered = asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()
        original = PostgresMigrationRunner.reset

        def reset(runner, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "test did not release the physical reset"
            return original(runner, **kwargs)

        monkeypatch.setattr(PostgresMigrationRunner, "reset", reset)
        work = asyncio.create_task(
            app.reset_postgres(
                LocalPostgresConfig(database.runtime_dsn, database.admin_dsn)
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 10)
            work.cancel()
            await asyncio.sleep(0)
            assert not work.done()
            assert app.database_state is DatabaseDataPlaneState.RESETTING
            assert app._database_lock.locked()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await work
            assert app.database_state is DatabaseDataPlaneState.READY
        finally:
            release.set()
            await asyncio.gather(work, return_exceptions=True)
            await app.aclose()

    with _migrated_database() as database:
        _seed_old_database(database)
        asyncio.run(run(database))
