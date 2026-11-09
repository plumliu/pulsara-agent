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


@pytest.mark.parametrize("termination", ["shutdown", "shutdown-workbench", "eof"])
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
            core = SimpleNamespace(workbench_exit_ready=False, workbench_exit_deadline=None)
            @property
            def workbench_exit_ready(self):
                return self.core.workbench_exit_ready

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
                    reader.feed_data((json.dumps({"type": termination}) + "\n").encode())

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


@pytest.mark.parametrize("close_fails", [False, True])
def test_explicit_workbench_exit_terminates_only_its_original_runtime_after_owner_boundary(desktop_home, close_fails):
    # An actual subprocess verifies self-termination without replacing os._exit
    # or terminating the test runner. Core's physical facts are exercised by
    # the lifecycle tests; this tests only their native runtime boundary.
    code = r'''
import asyncio, json, sys
from time import monotonic
from pulsara_agent import desktop_runtime as module
from pulsara_agent.desktop_app.application import DatabaseDataPlaneState

async def exercise():
    reader = asyncio.StreamReader()
    reader.feed_data((sys.argv[1] + "\n").encode())
    class Core:
        workbench_exit_ready = False
        workbench_exit_deadline = monotonic() - 1
    class App:
        database_state = DatabaseDataPlaneState.READY
        origin = "http://127.0.0.1:12345"
        core = Core()
        @property
        def workbench_exit_ready(self): return self.core.workbench_exit_ready
        def __init__(self, **kwargs): pass
        async def start(self): pass
        async def aclose(self):
            print(json.dumps({"type": "close-started"}), flush=True)
            self.core.workbench_exit_ready = True
            if sys.argv[2] == "True":
                raise RuntimeError("physical owner retained after close failure")
            await asyncio.Future()
    def emit(value):
        print(json.dumps(value), flush=True)
        if value["type"] == "ready":
            reader.feed_data(b'{"type":"shutdown-workbench"}\n')
    await module.run_desktop_child(reader, emit, application_factory=App,
                                  database_preparer=lambda *a, **k: None)
asyncio.run(exercise())
'''
    result = subprocess.run(
        [sys.executable, "-c", code, json.dumps(frame(desktop_home)), str(close_fails)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert "close-started" in [event["type"] for event in events]
    assert events[-1]["type"] == "failed"
    assert events[-1]["phase"] == "shutdown"
    assert "已确认数据保留" in events[-1]["error"]
    assert "未确认的操作不自动重做" in events[-1]["error"]
    if close_fails:
        assert any("physical owner retained after close failure" in event.get("error", "") for event in events[:-1])
        assert "physical owner retained after close failure" in result.stderr
    assert not any(event["type"] == "stopped" for event in events)
    # The OS lifetime lock is released by this exact child's actual exit.
    os.close(module.acquire_kernel_lock())


def test_closed_core_allows_workbench_exit_while_disposable_app_cleanup_waits(desktop_home):
    marker = desktop_home / "desktop" / "accepted.data"
    marker.write_text("already confirmed")
    code = r'''
import asyncio, json, sys
from pulsara_agent import desktop_runtime as module
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.desktop_app.application import DatabaseDataPlaneState
from tests.support.model_config import test_model_runtime as make_runtime

async def exercise():
    reader = asyncio.StreamReader()
    reader.feed_data((sys.argv[1] + "\n").encode())
    core = KernelHostCore.production(model_runtime=make_runtime(postgres_dsn="postgresql://unused/unused"))
    class App:
        database_state = DatabaseDataPlaneState.READY
        origin = "http://127.0.0.1:12345"
        def __init__(self, **kwargs): self.core = core
        @property
        def workbench_exit_ready(self): return core.workbench_exit_ready
        async def start(self): pass
        async def aclose(self):
            await core.shutdown()
            assert core._closed and not core._sessions
            assert core.workbench_exit_deadline is None
            assert core.workbench_exit_ready
            print(json.dumps({"type": "temporary-cleanup-wait"}), flush=True)
            await asyncio.Future()
    def emit(value):
        print(json.dumps(value), flush=True)
        if value["type"] == "ready":
            reader.feed_data(b'{"type":"shutdown-workbench"}\n')
    await module.run_desktop_child(reader, emit, application_factory=App,
                                  database_preparer=lambda *a, **k: None)
asyncio.run(exercise())
'''
    result = subprocess.run(
        [sys.executable, "-c", code, json.dumps(frame(desktop_home))],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(item["type"] == "temporary-cleanup-wait" for item in events)
    assert events[-1]["type"] == "failed" and events[-1]["phase"] == "shutdown"
    assert "未完成的临时收尾已丢弃" in events[-1]["error"]
    assert "结果未确认" not in events[-1]["error"]
    assert not any(item["type"] == "stopped" for item in events)
    assert marker.read_text() == "already confirmed"
    os.close(module.acquire_kernel_lock())


@pytest.mark.parametrize("initializing", [False, True])
def test_workbench_exit_during_startup_joins_the_original_sessionless_core_boundary(desktop_home, initializing):
    code = r'''
import asyncio, json, sys, threading
import pytest
from pulsara_agent import desktop_runtime as module
from pulsara_agent.conversation_kernel import host, host_management
from pulsara_agent.desktop_app.application import DatabaseDataPlaneState
from tests.test_kernel_resource_lifecycle import resources

async def exercise():
    patch = pytest.MonkeyPatch()
    core, leases, entered, release = resources(patch)
    reader = asyncio.StreamReader()
    reader.feed_data((sys.argv[1] + "\n").encode())
    class App:
        database_state = DatabaseDataPlaneState.READY
        origin = "http://127.0.0.1:12345"
        def __init__(self, **kwargs): self.core = core
        @property
        def workbench_exit_ready(self): return core.workbench_exit_ready
        async def start(self):
            if sys.argv[2] == "True":
                pending = asyncio.create_task(core.prepare_resources())
                await entered.wait()
                deadline = core._resource_initialization.deadline_monotonic
            else:
                release.set()
                await core.prepare_resources()
                physical_started = asyncio.Event()
                loop = asyncio.get_running_loop()
                def rows(*args):
                    loop.call_soon_threadsafe(physical_started.set)
                    threading.Event().wait()
                patch.setattr(host_management, "_list_resumable_session_rows_across_workspaces", rows)
                pending = asyncio.create_task(core.list_resumable_sessions_across_workspaces())
                await physical_started.wait()
                deadline = next(iter(core._resource_operations.values()))
            assert not core._sessions and not core._close_attempts
            # Advance only the test observation clock beyond this actual
            # operation's installed deadline; no application timeout is added.
            patch.setattr(host, "monotonic", lambda: deadline + 1)
            patch.setattr(module, "monotonic", lambda: deadline + 1)
            print(json.dumps({"type": "cold-owner-started"}), flush=True)
            reader.feed_data(b'{"type":"shutdown-workbench"}\n')
            await pending
        async def aclose(self):
            print(json.dumps({"type": "close-started"}), flush=True)
            await core.shutdown()
    def emit(value): print(json.dumps(value), flush=True)
    await module.run_desktop_child(reader, emit, application_factory=App,
                                  database_preparer=lambda *a, **k: None)
asyncio.run(exercise())
'''
    result = subprocess.run(
        [sys.executable, "-c", code, json.dumps(frame(desktop_home)), str(initializing)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert "cold-owner-started" in [item["type"] for item in events]
    assert "close-started" in [item["type"] for item in events]
    assert not any(item["type"] in {"ready", "stopped"} for item in events)
    assert events[-1]["type"] == "failed" and events[-1]["phase"] == "shutdown"
    os.close(module.acquire_kernel_lock())


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
        assert events[1]["phase"] == "startup" and events[-1]["clean"] is True
        assert marker.read_text() == "preserve"

    asyncio.run(exercise())


def test_clean_schema_startup_failure_reports_physical_settlement(desktop_home):
    from pulsara_agent.storage.migrations.errors import PostgresSchemaError, PostgresSchemaFailureCode

    async def exercise():
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps(frame(desktop_home)) + "\n").encode())
        events = []
        def failure(*_args, **_kwargs):
            raise PostgresSchemaError(PostgresSchemaFailureCode.MIGRATION_UNIVERSE_RESET_REQUIRED,
                                      "migration universe identity does not match this binary")
        def app_factory(**_kwargs):
            raise AssertionError("application must not start after schema failure")
        result = await module.run_desktop_child(reader, events.append, application_factory=app_factory,
                                                database_preparer=failure)
        assert result == 1
        assert [item["type"] for item in events] == ["starting", "failed", "stopping", "stopped"]
        assert events[1]["phase"] == "startup"
        assert "schema_migration_universe_reset_required" in events[1]["error"]
        assert events[-1] == {"type": "stopped", "clean": True}
        os.close(module.acquire_kernel_lock())
    asyncio.run(exercise())


def test_failed_startup_close_never_reports_clean_settlement(desktop_home):
    async def exercise():
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps(frame(desktop_home)) + "\n").encode())
        events = []
        class App:
            database_state = DatabaseDataPlaneState.UNAVAILABLE
            def __init__(self, **_kwargs): pass
            async def start(self): pass
            async def aclose(self): raise RuntimeError("physical close failed")
        result = await module.run_desktop_child(reader, events.append, application_factory=App,
                                                database_preparer=lambda *_args, **_kwargs: None)
        assert result == 1
        assert [(item["type"], item.get("phase")) for item in events] == [
            ("starting", None), ("failed", "startup"), ("stopping", None),
            ("failed", "shutdown"), ("stopped", None)]
        assert events[-1]["clean"] is False
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


def test_installed_launcher_wsl_manifest_gives_windows_instruction_without_launch(tmp_path):
    resources = tmp_path / "resources"
    interpreter = resources / "python" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    manifest = resources / "desktop-launch.json"
    manifest.write_text(json.dumps({"execution_domain": "wsl"}))
    with pytest.raises(RuntimeError, match="Windows Pulsara 图标启动/聚焦"):
        installed_launch_command(interpreter)
    for invalid in (
        {"execution_domain": "other"},
        {"execution_domain": "wsl", "executable": "arbitrary"},
    ):
        manifest.write_text(json.dumps(invalid))
        with pytest.raises(ValueError, match="manifest is invalid"):
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


def test_control_requests_keep_correlation_and_reject_old_encoding(desktop_home, monkeypatch):
    from pulsara_agent.identity import is_valid_id
    import pulsara_agent.desktop_app.tls_identity as tls
    import pulsara_agent.windows_interop as interop

    async def exercise():
        start = frame(desktop_home)
        start['host'] = {'platform': 'win32', 'executionDomain': 'wsl', 'distribution': 'Ubuntu',
                         'user': 'test', 'interopHelper': '/tmp/interop'}
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps(start) + '\n').encode())
        ready = asyncio.Event()
        outgoing = asyncio.Queue()
        callbacks = {}
        class App:
            database_state = DatabaseDataPlaneState.READY
            origin = 'https://127.0.0.1:12345'
            def __init__(self, **kwargs): callbacks['open'] = kwargs['open_browser']
            async def start(self): pass
            async def aclose(self): pass
        def emit(value):
            if value['type'] == 'ready':
                ready.set()
            if value['type'] in {'open-oauth-browser', 'quit-state'}:
                outgoing.put_nowait(value)
        async def inspection(_application): return {'activeTasks': 7, 'scheduledTasks': 0}
        monkeypatch.setattr(module, 'inspect_quit', inspection)
        monkeypatch.setattr(interop, 'configure_interop', lambda **kwargs: None)
        monkeypatch.setattr(tls, 'create_desktop_tls_identity', lambda _path: SimpleNamespace(certificate_pem='certificate'))
        allocated = iter(('request:' + 'a' * 26, 'request:' + 'a' * 26,
                          'request:' + 'a' * 25 + 'e', 'request:' + 'a' * 25 + 'i'))
        monkeypatch.setattr(module, 'random_id', lambda kind: next(allocated))
        task = asyncio.create_task(module.run_desktop_child(reader, emit, application_factory=App,
                                                           database_preparer=lambda *a, **k: None))
        await ready.wait()
        one = asyncio.create_task(callbacks['open']('https://example.test/one'))
        two = asyncio.create_task(callbacks['open']('https://example.test/two'))
        first, second = await outgoing.get(), await outgoing.get()
        assert all(is_valid_id(item['requestId'], 'request') for item in (first, second))
        assert first['requestId'] != second['requestId']
        inspect = 'request:' + 'a' * 26
        for request in (
            {'type': 'open-oauth-browser-result', 'requestId': second['requestId'], 'opened': False},
            {'type': 'inspect-quit', 'requestId': inspect},
            {'type': 'open-oauth-browser-result', 'requestId': first['requestId'], 'opened': True},
        ):
            reader.feed_data((json.dumps(request) + '\n').encode())
        assert (await outgoing.get())['requestId'] == inspect
        await one
        with pytest.raises(RuntimeError, match='could not open'):
            await two
        pending = asyncio.create_task(callbacks['open']('https://example.test/pending'))
        await outgoing.get()
        reader.feed_data(b'{"type":"inspect-quit","requestId":"oauth-1"}\n')
        assert await task == 1
        with pytest.raises(ConnectionError, match='channel closed'):
            await pending
    asyncio.run(exercise())
