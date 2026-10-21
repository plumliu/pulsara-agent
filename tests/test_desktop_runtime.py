from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from pulsara_agent import desktop_runtime as module
from pulsara_agent.desktop_launcher import installed_launch_command
from pulsara_agent.settings import LocalPostgresConfig
from pulsara_agent.desktop_app.application import DatabaseDataPlaneState


@pytest.fixture
def desktop_home(monkeypatch):
    # Unix socket paths have a platform pathname bound; keep fixture below it.
    root = Path(tempfile.mkdtemp(prefix="pd-", dir="/tmp")).resolve()
    monkeypatch.setenv("PULSARA_HOME", str(root))
    (root / "desktop" / "socket").mkdir(parents=True, mode=0o700)
    (root / "desktop").chmod(0o700)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def config(home):
    socket = home / "desktop" / "socket"
    return LocalPostgresConfig(
        f"host={socket} dbname=pulsara user=pulsara_runtime",
        f"host={socket} dbname=pulsara user=pulsara_admin",
    )


def frame(home):
    value = config(home)
    return {"type": "start", "initialize": True,
            "postgres": {"runtime_dsn": value.runtime_dsn, "admin_dsn": value.admin_dsn}}


def test_managed_database_rejects_external_targets_and_privilege_role_swap(desktop_home):
    module.validate_managed_postgres(config(desktop_home))
    for value in (
        LocalPostgresConfig("host=127.0.0.1 dbname=pulsara user=pulsara_runtime", config(desktop_home).admin_dsn),
        LocalPostgresConfig(config(desktop_home).admin_dsn, config(desktop_home).admin_dsn),
        LocalPostgresConfig(config(desktop_home).runtime_dsn + " password=unexpected", config(desktop_home).admin_dsn),
    ):
        with pytest.raises(ValueError):
            module.validate_managed_postgres(value)
    (desktop_home / "desktop" / "socket").chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        module.validate_managed_postgres(config(desktop_home))


def test_kernel_lock_prevents_second_owner_until_existing_close(desktop_home):
    owner = module.acquire_kernel_lock()
    try:
        with pytest.raises(BlockingIOError):
            module.acquire_kernel_lock()
    finally:
        os.close(owner)
    os.close(module.acquire_kernel_lock())


def test_wait_owner_does_not_initialize_settings_or_application(desktop_home):
    # A fresh process exercises the actual CLI branch. The OS lock needs no
    # settings, schema or provider owners, even when an application is installed.
    code = """
import runpy, sys
sys.argv = ["pulsara_agent.desktop_runtime", "--wait-owner"]
runpy.run_module("pulsara_agent.desktop_runtime", run_name="__main__")
assert "pulsara_agent.settings" not in sys.modules
assert "pulsara_agent.desktop_app.application" not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize("termination", ["shutdown", "eof"])
def test_host_channel_readiness_and_parent_loss_use_existing_close(desktop_home, termination):
    async def exercise():
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps(frame(desktop_home)) + "\n").encode())
        events = []
        closes = []
        prepared = []

        class App:
            database_state = DatabaseDataPlaneState.READY
            origin = "http://127.0.0.1:12345"

            def __init__(self, *, settings, permission_policy):
                assert settings.read().postgres == config(desktop_home)
                assert permission_policy.to_dict()["execution_boundary"] == "host"

            async def start(self):
                events.append("app.start")

            async def aclose(self):
                closes.append("closed")
                events.append("app.close")

        def emit(value):
            events.append(value["type"])
            if value["type"] == "ready":
                assert value["database_state"] == "ready"
                assert value["origin"] == App.origin
                assert module.diagnostics_path().is_socket()
                if termination == "eof":
                    reader.feed_eof()
                else:
                    reader.feed_data(b'{"type":"shutdown"}\n')

        def prepare(postgres, *, initialize):
            prepared.append((postgres, initialize))

        result = await module.run_desktop_child(reader, emit, application_factory=App, database_preparer=prepare)
        assert result == 0
        assert prepared == [(config(desktop_home), True)]
        assert closes == ["closed"]
        assert events == ["starting", "app.start", "ready", "stopping", "app.close", "stopped"]
        assert not module.diagnostics_path().exists()
        os.close(module.acquire_kernel_lock())

    asyncio.run(exercise())


def test_database_failure_never_announces_ready_or_deletes_data(desktop_home):
    async def exercise():
        marker = desktop_home / "desktop" / "data"
        marker.write_text("preserve")
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps(frame(desktop_home)) + "\n").encode())
        events = []

        def failure(*args, **kwargs):
            raise RuntimeError("pg_search extension is missing")

        result = await module.run_desktop_child(reader, events.append, database_preparer=failure)
        assert result == 1
        assert [item["type"] for item in events] == ["starting", "failed", "stopping", "stopped"]
        assert "pg_search" in events[1]["error"]
        assert marker.read_text() == "preserve"

    asyncio.run(exercise())


def test_http_ready_is_not_database_ready_and_failed_app_is_closed(desktop_home):
    async def exercise():
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps(frame(desktop_home)) + "\n").encode())
        events = []
        closed = []

        class App:
            database_state = DatabaseDataPlaneState.UNAVAILABLE
            def __init__(self, **kwargs): pass
            async def start(self): pass
            async def aclose(self): closed.append(True)

        result = await module.run_desktop_child(reader, events.append, application_factory=App,
                                                database_preparer=lambda *a, **k: None)
        assert result == 1
        assert "ready" not in [item["type"] for item in events]
        assert closed == [True]

    asyncio.run(exercise())


def test_diagnostics_are_read_only_and_use_current_injected_target(desktop_home, monkeypatch):
    async def exercise():
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps(frame(desktop_home)) + "\n").encode())
        calls = []
        ready = asyncio.Event()

        class App:
            database_state = DatabaseDataPlaneState.READY
            origin = "http://127.0.0.1:12345"
            def __init__(self, **kwargs): pass
            async def start(self): pass
            async def aclose(self): pass

        def diagnostic(postgres, **kwargs):
            calls.append((postgres, kwargs))
            return {"status": "verified"}

        monkeypatch.setattr(module, "database_diagnostic", diagnostic)
        task = asyncio.create_task(module.run_desktop_child(
            reader, lambda event: ready.set() if event["type"] == "ready" else None,
            application_factory=App, database_preparer=lambda *a, **k: None))
        await ready.wait()
        result = await asyncio.to_thread(module.request_database_diagnostic,
                                         command="verify", deadline_seconds=30.0)
        assert result == {"status": "verified"}
        assert calls == [(config(desktop_home), {"command": "verify", "deadline_seconds": 30.0, "deep": False})]
        reader.feed_eof()
        assert await task == 0

    asyncio.run(exercise())


def test_installed_launcher_resolves_relative_product_manifest(tmp_path):
    resources = tmp_path / "Resources"
    interpreter = resources / "python" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    target = tmp_path / "MacOS" / "Pulsara"
    target.parent.mkdir()
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    (resources / "desktop-launch.json").write_text(json.dumps({"executable": "../MacOS/Pulsara"}))
    assert installed_launch_command(interpreter) == [str(target), "--pulsara-launch"]
    (resources / "desktop-launch.json").unlink()
    with pytest.raises(RuntimeError, match="not installed"):
        installed_launch_command(interpreter)


def test_lost_status_pipe_still_closes_application(desktop_home):
    async def exercise():
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps(frame(desktop_home)) + "\n").encode())
        closed = []
        class App:
            database_state = DatabaseDataPlaneState.READY
            origin = "http://127.0.0.1:12345"
            def __init__(self, **kwargs): pass
            async def start(self): pass
            async def aclose(self): closed.append(True)
        def emit(value):
            if value["type"] == "ready":
                raise BrokenPipeError("parent died")
        assert await module.run_desktop_child(
            reader, emit, application_factory=App, database_preparer=lambda *a, **k: None,
        ) == 0
        assert closed == [True]
        os.close(module.acquire_kernel_lock())
    asyncio.run(exercise())


@pytest.mark.parametrize("existing", [False, True])
def test_bootstrap_reuses_migration_owner_and_never_resets(desktop_home, monkeypatch, existing):
    import pulsara_agent.storage.postgres_endpoint as endpoints
    import pulsara_agent.storage.migrations.runner as migrations

    statements = []
    migrations_called = []

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query):
            statements.append(query)
            if "FROM pg_catalog.pg_roles" in query:
                return SimpleNamespace(fetchone=lambda: (False, False, False, False, False, True) if existing else None)
            if "FROM pg_catalog.pg_database" in query:
                return SimpleNamespace(fetchone=lambda: ("pulsara_admin",) if existing else None)
            return SimpleNamespace(fetchone=lambda: None)

    class Factory:
        def __init__(self, dsn, *, application_name):
            from psycopg.conninfo import conninfo_to_dict
            assert conninfo_to_dict(dsn)["dbname"] == "postgres"
            assert conninfo_to_dict(dsn)["user"] == "pulsara_admin"
        def connect(self, **kwargs):
            assert kwargs["autocommit"] is True
            return Connection()

    class Runner:
        def __init__(self, **kwargs):
            assert kwargs == {"admin_dsn": config(desktop_home).admin_dsn, "runtime_dsn": config(desktop_home).runtime_dsn}
        def migrate(self, **kwargs): migrations_called.append(True)
        def reset(self, **kwargs): raise AssertionError("startup must never reset")

    monkeypatch.setattr(endpoints, "ResolvedPostgresConnectionFactory", Factory)
    monkeypatch.setattr(migrations, "PostgresMigrationRunner", Runner)
    module.prepare_database(config(desktop_home), initialize=True)
    assert migrations_called == [True]
    creates = [query for query in statements if query.startswith("CREATE")]
    if existing:
        assert creates == []
    else:
        assert len(creates) == 2
        assert "NOSUPERUSER NOCREATEDB NOCREATEROLE" in creates[0]
        assert creates[1] == "CREATE DATABASE pulsara OWNER pulsara_admin"
