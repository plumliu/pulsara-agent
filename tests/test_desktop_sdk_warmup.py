"""Dependency preparation does not own shell readiness or orphan import workers."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
from threading import Event, get_ident
from types import ModuleType, SimpleNamespace

import pytest

from pulsara_agent.capability import mcp_management
from pulsara_agent.capability.mcp_management import LocalMcpManagementService, LocalMcpTarget
from pulsara_agent.conversation_kernel.mcp import supervisor
from pulsara_agent.desktop_app import application
from pulsara_agent.settings import LocalSettingsStore
from tests.test_desktop_catalog_startup import _application, _bootstrap


def test_empty_mcp_owners_do_not_import_sdk_during_shell_or_session_preparation(tmp_path):
    code = """
import asyncio,sys
from pathlib import Path
from pulsara_agent.desktop_app.application import DesktopApplication
from pulsara_agent.capability.mcp_management import LocalMcpManagementService
from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.settings import LocalSettingsStore
service=LocalMcpManagementService(LocalSettingsStore(Path(sys.argv[1])/'settings.yaml'),user_config_path=Path(sys.argv[1])/'mcp.yaml')
owner=McpHostSupervisor(session_id='unused',workspace_root=Path(sys.argv[1]),configs=(),credential_boundary=ProcessCredentialBoundary())
async def run():
    await owner.start()
    await owner.aclose()
    await service.aclose()
asyncio.run(run())
assert service._oauth is None
assert not any(name=='openai' or name.startswith('openai.') or name=='mcp' or name.startswith('mcp.') or name.endswith('.mcp.sdk_facade') or name.endswith('.mcp.oauth') for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path)],
        check=True, capture_output=True, text=True)


def test_oauth_first_use_publishes_one_owner_and_close_prevents_recreation(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    created = []
    closed = []

    class Manager:
        def __init__(self, settings, **kwargs):
            created.append((settings, kwargs))
            entered.set()
            assert release.wait(5)

        async def aclose(self):
            closed.append(self)

    module = ModuleType("pulsara_agent.conversation_kernel.mcp.oauth")
    module.McpOAuthManager = Manager
    monkeypatch.setitem(sys.modules, module.__name__, module)
    browser = object()
    service = LocalMcpManagementService(LocalSettingsStore(tmp_path / "settings.yaml"),
        user_config_path=tmp_path / "mcp.yaml", open_browser=browser)
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(lambda: service.oauth) for _ in range(8)]
        try:
            assert entered.wait(5)
        finally:
            release.set()
        owners = [future.result() for future in pending]
    assert len(created) == 1
    assert all(owner is owners[0] for owner in owners)
    assert created[0][1] == dict(lane=service.lane,
        credential_boundary=service.credential_boundary, open_browser=browser)
    asyncio.run(service.aclose())
    assert closed == [owners[0]]
    with pytest.raises(RuntimeError, match="service is closed"):
        _ = service.oauth
    assert len(created) == 1


@pytest.mark.parametrize("failed", [False, True])
def test_sdk_warmup_keeps_http_available_and_close_joins_the_sequential_worker(tmp_path, monkeypatch, caplog, failed):
    entered = {"openai": Event(), "pulsara_agent.conversation_kernel.mcp.sdk_facade": Event()}
    release = {name: Event() for name in entered}
    finished = []
    threads = []

    def load(name):
        if name in entered:
            threads.append(get_ident())
            entered[name].set()
            assert release[name].wait(5)
            finished.append(name)
            if failed:
                raise ImportError(name + " unavailable")

    monkeypatch.setattr(application, "import_module", load)

    async def exercise():
        class Client:
            async def fetch(self):
                return snapshot

        app, snapshot = _application(tmp_path, monkeypatch, Client())
        close = None
        try:
            await asyncio.wait_for(app.start(), 1)
            assert await asyncio.to_thread(entered["openai"].wait, 5)
            assert not entered["pulsara_agent.conversation_kernel.mcp.sdk_facade"].is_set()
            assert app.ready
            assert not app._sdk_warmup_task.done()
            assert (await asyncio.wait_for(_bootstrap(app), 1))["runtime"]["database_state"] == "ready"
            release["openai"].set()
            assert await asyncio.to_thread(entered["pulsara_agent.conversation_kernel.mcp.sdk_facade"].wait, 5)
            assert finished == ["openai"]
            assert (await asyncio.wait_for(_bootstrap(app), 1))["runtime"]["database_state"] == "ready"
            close = asyncio.create_task(app.aclose())
            await asyncio.sleep(0)
            assert app._close_task is not None
            for _ in range(2):
                close.cancel()
                await asyncio.sleep(0)
                assert not close.done()
            release["pulsara_agent.conversation_kernel.mcp.sdk_facade"].set()
            with pytest.raises(asyncio.CancelledError):
                await close
            assert finished == list(entered)
            assert len(set(threads)) == 1
            assert app._sdk_warmup_task.done()
            assert app.state is application.DesktopApplicationState.STOPPED
        finally:
            for event in release.values():
                event.set()
            await app.aclose()

    asyncio.run(exercise())
    assert sum("SDK background import failed" in record.message for record in caplog.records) == (2 if failed else 0)


@pytest.mark.parametrize("cancelled", [False, True])
def test_first_mcp_sdk_use_keeps_loop_available_and_joins_import_on_cancel(monkeypatch, cancelled):
    entered, release, finished = Event(), Event(), Event()
    factory = object()

    def load(name):
        assert name == "pulsara_agent.conversation_kernel.mcp.sdk_facade"
        entered.set()
        assert release.wait(5)
        finished.set()
        return SimpleNamespace(BoundedMcpSdkClient=factory)

    monkeypatch.setattr(supervisor, "import_module", load)

    async def exercise():
        task = asyncio.create_task(supervisor._prepare_sdk_client_factory())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            for _ in range(2):
                if cancelled:
                    task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
        finally:
            release.set()
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is factory
        assert finished.is_set()

    asyncio.run(exercise())


def test_first_mcp_sdk_import_failure_remains_the_original_failure(monkeypatch):
    error = ImportError("MCP SDK unavailable")

    def load(_name):
        raise error

    monkeypatch.setattr(supervisor, "import_module", load)
    with pytest.raises(ImportError) as caught:
        asyncio.run(supervisor._prepare_sdk_client_factory())
    assert caught.value is error


def test_first_oauth_action_keeps_loop_available_and_cancel_does_not_lose_owner(tmp_path, monkeypatch):
    entered, release, finished = Event(), Event(), Event()
    closed, begun = [], []

    class Manager:
        def __init__(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            finished.set()

        async def begin(self, *args):
            begun.append(self)

        async def aclose(self):
            closed.append(self)

    module = ModuleType("pulsara_agent.conversation_kernel.mcp.oauth")
    module.McpOAuthManager = Manager
    monkeypatch.setitem(sys.modules, module.__name__, module)
    service = LocalMcpManagementService(LocalSettingsStore(tmp_path / "settings.yaml"),
        user_config_path=tmp_path / "mcp.yaml")

    async def exercise():
        task = asyncio.create_task(service.authorize(LocalMcpTarget("fixture")))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert service._oauth is not None
        assert begun == []
        await service.aclose()
        assert closed == [service._oauth]

    asyncio.run(exercise())


def test_oauth_close_between_worker_result_and_async_resume_rejects_the_closed_owner(tmp_path, monkeypatch):
    begun, closed = [], []

    class Manager:
        def __init__(self, *args, **kwargs):
            pass

        async def begin(self, *args):
            begun.append(self)

        async def aclose(self):
            closed.append(self)

    module = ModuleType("pulsara_agent.conversation_kernel.mcp.oauth")
    module.McpOAuthManager = Manager
    monkeypatch.setitem(sys.modules, module.__name__, module)
    service = LocalMcpManagementService(LocalSettingsStore(tmp_path / "settings.yaml"),
        user_config_path=tmp_path / "mcp.yaml")
    original = asyncio.to_thread

    async def close_after_worker(*args, **kwargs):
        manager = await original(*args, **kwargs)
        await service.aclose()
        return manager

    monkeypatch.setattr(mcp_management.asyncio, "to_thread", close_after_worker)
    with pytest.raises(RuntimeError, match="service is closed"):
        asyncio.run(service.authorize(LocalMcpTarget("fixture")))
    assert begun == []
    assert closed == [service._oauth]


def test_cancelled_plugin_normalization_joins_worker_and_closes_its_returned_anchors():
    from pulsara_agent.conversation_kernel.host import _shielded_plugin_filesystem_call

    entered, release, finished = Event(), Event(), Event()
    closed = []

    class Result:
        def close_plugin_anchors(self):
            closed.append(self)

    result = Result()

    def normalize():
        entered.set()
        assert release.wait(5)
        finished.set()
        return result

    async def exercise():
        task = asyncio.create_task(_shielded_plugin_filesystem_call(
            normalize, close_result=lambda value: value.close_plugin_anchors(),
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert closed == [result]

    asyncio.run(exercise())
