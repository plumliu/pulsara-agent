"""The initial remote model directory is owned without blocking the local shell."""

import asyncio
from unittest.mock import AsyncMock

from aiohttp import ClientSession
import pytest

from pulsara_agent.desktop_app.application import (
    DatabaseDataPlaneState,
    DesktopApplication,
)
from pulsara_agent.llm.model_catalog import ModelCatalogOwner
from pulsara_agent.tool_permission import default_permission_policy
from tests.support.model_config import test_model_runtime as make_runtime


def _application(tmp_path, monkeypatch, client):
    (tmp_path / "index.html").write_text("Pulsara")
    runtime = make_runtime(postgres_dsn="postgresql://localhost/catalog_test_unused")
    snapshot = runtime.catalog.snapshot
    runtime.catalog = ModelCatalogOwner(client)
    app = DesktopApplication(
        settings=runtime.settings, catalog=runtime.catalog, model_runtime=runtime,
        permission_policy=default_permission_policy(), static_root=tmp_path,
    )
    monkeypatch.setattr(app.sessions, "prepare", AsyncMock())
    monkeypatch.setattr(app.scheduled_tasks, "start", AsyncMock())
    return app, snapshot


async def _bootstrap(app):
    async with ClientSession() as client:
        async with client.get(app.origin + "/api/app/bootstrap") as response:
            assert response.status == 200
            return await response.json()


@pytest.mark.parametrize("failed", [False, True])
def test_initial_catalog_does_not_block_ready_and_settles_saved_model_status(tmp_path, monkeypatch, failed):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        class Client:
            calls = 0

            async def fetch(self):
                self.calls += 1
                entered.set()
                await release.wait()
                if failed:
                    raise RuntimeError("catalog unavailable")
                return snapshot

        client = Client()
        app, snapshot = _application(tmp_path, monkeypatch, client)
        try:
            await asyncio.wait_for(app.start(), 1)
            await asyncio.wait_for(entered.wait(), 1)
            assert app.ready
            assert app.database_state is DatabaseDataPlaneState.READY
            app.scheduled_tasks.start.assert_not_awaited()
            before = await _bootstrap(app)
            assert before["model_catalog_loading"] is True
            assert before["model_configurations"][0]["source"] == "models_dev"
            assert before["model_configurations"][0]["status"] == "unavailable"
            release.set()
            await asyncio.wait_for(app._initial_catalog_task, 1)
            after = await _bootstrap(app)
            assert after["model_catalog_loading"] is False
            assert after["model_configurations"][0]["status"] == ("unavailable" if failed else "ready")
            app.scheduled_tasks.start.assert_awaited_once()
            assert client.calls == 1
        finally:
            release.set()
            await app.aclose()

    asyncio.run(exercise())


def test_local_ready_still_waits_for_schema_preparation(tmp_path, monkeypatch):
    async def exercise():
        prepared, release = asyncio.Event(), asyncio.Event()

        class Client:
            async def fetch(self):
                return snapshot

        app, snapshot = _application(tmp_path, monkeypatch, Client())

        async def prepare():
            prepared.set()
            await release.wait()

        monkeypatch.setattr(app.sessions, "prepare", prepare)
        start = asyncio.create_task(app.start())
        try:
            await asyncio.wait_for(prepared.wait(), 1)
            assert not start.done()
            assert not app.ready
            app.scheduled_tasks.start.assert_not_awaited()
            release.set()
            await asyncio.wait_for(start, 1)
            await asyncio.wait_for(app._initial_catalog_task, 1)
            assert app.ready
            assert app.database_state is DatabaseDataPlaneState.READY
            assert app.scheduled_tasks.start.await_count >= 1
        finally:
            release.set()
            await start
            await app.aclose()

    asyncio.run(exercise())


def test_shutdown_joins_cancelled_catalog_physical_cleanup(tmp_path, monkeypatch):
    async def exercise():
        entered, cleaning, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Client:
            async def fetch(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release_cleanup.wait()

        app, _ = _application(tmp_path, monkeypatch, Client())
        await app.start()
        await asyncio.wait_for(entered.wait(), 1)
        close = asyncio.create_task(app.aclose())
        try:
            await asyncio.wait_for(cleaning.wait(), 1)
            assert not close.done()
            assert not app._initial_catalog_task.done()
            app.scheduled_tasks.start.assert_not_awaited()
            release_cleanup.set()
            await asyncio.wait_for(close, 1)
            assert app._initial_catalog_task.done()
            app.scheduled_tasks.start.assert_not_awaited()
        finally:
            release_cleanup.set()
            await close

    asyncio.run(exercise())


def test_catalog_settlement_does_not_start_scheduler_until_database_ready(tmp_path, monkeypatch):
    async def exercise():
        class Client:
            async def fetch(self):
                raise RuntimeError("initial catalog failed")

        app, _ = _application(tmp_path, monkeypatch, Client())
        monkeypatch.setattr(app.sessions, "prepare", AsyncMock(side_effect=RuntimeError("database unavailable")))
        try:
            await app.start()
            await asyncio.wait_for(app._initial_catalog_task, 1)
            assert app.database_state is DatabaseDataPlaneState.UNAVAILABLE
            assert not app.model_catalog_loading
            app.scheduled_tasks.start.assert_not_awaited()
            monkeypatch.setattr(app.sessions, "prepare", AsyncMock())
            await app.refresh_database_state()
            assert app.database_state is DatabaseDataPlaneState.READY
            app.scheduled_tasks.start.assert_awaited_once()
        finally:
            await app.aclose()

    asyncio.run(exercise())


def test_unexpected_scheduler_failure_is_observed_and_close_still_joins(tmp_path, monkeypatch, caplog):
    async def exercise():
        class Client:
            async def fetch(self):
                return snapshot

        app, snapshot = _application(tmp_path, monkeypatch, Client())
        monkeypatch.setattr(app.scheduled_tasks, "start", AsyncMock(side_effect=RuntimeError("scheduler startup failed")))
        await app.start()
        with pytest.raises(RuntimeError, match="scheduler startup failed"):
            await app._initial_catalog_task
        await asyncio.sleep(0)  # Deliver the owned task's completion observer.
        assert not app.model_catalog_loading
        assert "Initial model catalog startup continuation failed" in caplog.text
        with pytest.raises(RuntimeError, match="scheduler startup failed"):
            await app.aclose()
        assert app.http is None
        assert app.protocol_server is None

    asyncio.run(exercise())
