from __future__ import annotations

import asyncio
import json
import os
import ssl
from unittest.mock import AsyncMock

from aiohttp import ClientSession, ClientConnectorCertificateError, TCPConnector
import pytest

from pulsara_agent import desktop_runtime
from pulsara_agent.desktop_app.application import DatabaseDataPlaneState
from pulsara_agent.desktop_app.http_server import LocalHttpServer
from pulsara_agent.desktop_app.tls_identity import create_desktop_tls_identity
from pulsara_agent.desktop_app.workspace_directories import WorkspaceDirectories, WorkspaceDirectoryError
from tests.test_desktop_http_surface import _Sessions, _Bridge, _model_server_dependencies
from tests.test_desktop_runtime import desktop_home as desktop_home


@pytest.fixture
def directories(tmp_path, monkeypatch):
    from pulsara_agent.desktop_app import workspace_directories as module
    home = tmp_path / "user"
    home.mkdir()
    service = WorkspaceDirectories()
    service.home, service.root = home, home / "workspaces"
    monkeypatch.setattr(module, "require_native_ext4", lambda *a, **k: None)
    return service


def test_project_root_is_read_only_until_explicit_initialization(directories):
    with pytest.raises(WorkspaceDirectoryError) as failure:
        directories.list(None, None)
    assert failure.value.code == "WORKSPACE_ROOT_MISSING"
    assert not directories.root.exists()
    assert directories.initialize() == {"path": str(directories.root)}
    first = directories.create(str(directories.root), "中文 Case")
    assert first == {"path": str(directories.root / "中文 Case")}
    with pytest.raises(FileExistsError):
        directories.create(str(directories.root), "中文 Case")
    assert directories.list(None, None)["entries"] == [{"name": "中文 Case", "path": first["path"]}]
    for invalid in ("../escape", "COM1", "trailing.", "a/b", "NUL.txt", "a\\b"):
        with pytest.raises(WorkspaceDirectoryError):
            directories.create(str(directories.root), invalid)


def test_directory_pagination_has_no_total_cap_and_selection_rechecks_links(directories):
    directories.initialize()
    for number in range(103):
        (directories.root / f"project-{number:03}").mkdir()
    first = directories.list(None, None)
    assert len(first["entries"]) == 100
    second = directories.list(None, first["next_cursor"])
    assert len(second["entries"]) == 3 and second["next_cursor"] is None
    target = directories.root / "project-000"
    assert directories.select(str(target))["path"] == str(target)
    target.rmdir()
    target.symlink_to(directories.home, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        directories.select(str(target))


def test_mkdir_parent_replacement_reports_written_unknown_without_rollback(directories, monkeypatch):
    directories.initialize()
    parent = directories.root / "parent"
    parent.mkdir()
    moved = directories.root / "moved"
    original = os.mkdir

    def swapping(name, mode=0o777, *, dir_fd=None):
        if name == "child" and dir_fd is not None:
            parent.rename(moved)
            parent.symlink_to(directories.home, target_is_directory=True)
        return original(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", swapping)
    with pytest.raises(WorkspaceDirectoryError) as failure:
        directories.create(str(parent), "child")
    assert failure.value.code == "WORKSPACE_WRITE_UNKNOWN"
    assert (moved / "child").is_dir()
    assert not (directories.home / "child").exists()


def test_wsl_canonical_admission_rejects_swap_after_picker_check(directories, monkeypatch):
    from pulsara_agent.conversation_kernel import host as host_module
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    directories.initialize()
    selected = directories.root / "selected"
    selected.mkdir()
    outside = directories.home / "outside"
    outside.mkdir()
    assert directories.select(str(selected)) == {"path": str(selected)}
    original = host_module.resolve_workspace

    def swapping(value, *, intent):
        selected.rmdir()
        selected.symlink_to(outside, target_is_directory=True)
        # Preserve the original canonical resolver; it deliberately resolves
        # NEW aliases on native platforms. The WSL boundary must reject this.
        resolved = original(value, intent=intent)
        assert resolved.workspace_root == outside
        return resolved

    monkeypatch.setattr(host_module, "resolve_workspace", swapping)
    core = host_module.KernelHostCore.production(
        model_runtime=_model_server_dependencies()["model_runtime"],
        workspace_validator=lambda path: directories.select(str(path)),
    )
    resource_owner = AsyncMock(side_effect=AssertionError("must reject before DB/capability owners"))
    monkeypatch.setattr(core, "_ensure_resources", resource_owner)

    async def exercise():
        try:
            with pytest.raises(ValueError, match="path identity changed"):
                await core.open_session(HostWorkspaceInput("project", selected))
            resource_owner.assert_not_awaited()
            assert core._open_attempts == {}
        finally:
            await core.shutdown()
    asyncio.run(exercise())


def test_tls_http_and_user_directory_routes_keep_security_and_create_admission(tmp_path, directories):
    async def exercise():
        (tmp_path / "index.html").write_text("Pulsara")
        identity = create_desktop_tls_identity(tmp_path)
        assert not list(tmp_path.glob(".tls-*"))
        sessions = _Sessions()
        server = LocalHttpServer(
            sessions=sessions, bridge=_Bridge(), static_root=tmp_path, requested_port=0,
            is_ready=lambda: True, is_draining=lambda: False,
            workspace_directories=directories, ssl_context=identity.context,
            host={"platform": "win32", "executionDomain": "wsl", "distribution": "Ubuntu", "user": "test"},
            **_model_server_dependencies(),
        )
        await server.start()
        try:
            assert server.origin.startswith("https://127.0.0.1:")
            trusted = ssl.create_default_context(cadata=identity.certificate_pem)
            headers = {"X-Pulsara-Connection-Id": "connection-1", "X-Pulsara-Connection-Generation": "1", "Origin": server.origin, "Sec-Fetch-Site": "same-origin"}
            async with ClientSession(connector=TCPConnector(ssl=trusted), headers=headers) as client:
                async with client.get(server.origin + "/api/workspace-directories") as response:
                    assert response.status == 404
                    assert (await response.json())["error"]["code"] == "WORKSPACE_ROOT_MISSING"
                async with client.post(server.origin + "/api/workspace-directories/initialize", json={}) as response:
                    assert response.status == 200
                async with client.post(server.origin + "/api/workspace-directories", json={"path": str(directories.root), "name": "selected"}) as response:
                    assert response.status == 200
                selected = directories.root / "selected"
                selected.rmdir()
                selected.symlink_to(directories.home, target_is_directory=True)
                async with client.post(server.origin + "/api/sessions", json={"workspace_kind": "project", "workspace_path": str(selected)}) as response:
                    assert response.status in {400, 409}
                assert sessions.create_model_bindings == []
                async with client.post(server.origin + "/api/workspace-directories/initialize", json={}, headers={"Origin": "https://wrong.invalid"}) as response:
                    assert response.status == 403
            wrong = create_desktop_tls_identity(tmp_path)
            async with ClientSession(connector=TCPConnector(ssl=ssl.create_default_context(cadata=wrong.certificate_pem))) as client:
                with pytest.raises(ClientConnectorCertificateError):
                    await client.get(server.origin + "/api/healthz")
        finally:
            await server.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("answer", [True, False, None, "shutdown"])
def test_wsl_start_tls_and_oauth_finite_bridge_settle_through_original_close(desktop_home, monkeypatch, answer):
    import pulsara_agent.windows_interop as interop
    calls = []
    monkeypatch.setattr(interop, "configure_interop", lambda **kwargs: calls.append(kwargs))
    home = desktop_home
    profile = {"platform": "win32", "executionDomain": "wsl", "distribution": "Ubuntu", "user": "test", "interopHelper": "/mnt/c/test/interop.exe"}
    async def exercise():
        reader = asyncio.StreamReader()
        postgres = {"runtime_dsn": f"host={home}/desktop/socket dbname=pulsara user=pulsara_runtime", "admin_dsn": f"host={home}/desktop/socket dbname=pulsara user=pulsara_admin"}
        reader.feed_data((json.dumps({"type": "start", "initialize": False, "postgres": postgres, "host": profile}) + "\n").encode())
        events, closed = [], []

        class App:
            database_state = DatabaseDataPlaneState.READY
            origin = "https://127.0.0.1:12345"
            def __init__(self, **kwargs):
                self.options = kwargs
                assert kwargs["host"] == profile
                assert kwargs["tls_identity"].certificate_pem.startswith("-----BEGIN CERTIFICATE-----")
            async def start(self):
                await self.options["open_browser"]("https://local-test.invalid/authorize")
            async def aclose(self):
                closed.append(True)

        def emit(event):
            events.append(event)
            if event["type"] == "open-oauth-browser":
                if answer is None:
                    reader.feed_eof()
                elif answer == "shutdown":
                    reader.feed_data(b'{"type":"shutdown"}\n')
                else:
                    reader.feed_data((json.dumps({"type": "open-oauth-browser-result", "requestId": event["requestId"], "opened": answer}) + "\n").encode())
            elif event["type"] == "ready":
                assert event["certificatePem"].startswith("-----BEGIN CERTIFICATE-----")
                reader.feed_eof()
            elif event["type"] == "stopped":
                assert closed == [True]

        result = await desktop_runtime.run_desktop_child(reader, emit, application_factory=App, database_preparer=lambda *a, **k: None)
        assert result == (0 if answer is True else 1)
        assert events[-1]["type"] == "stopped"
        assert closed == [True]
        assert any(event["type"] == "ready" for event in events) is (answer is True)
    asyncio.run(exercise())
    assert calls == [{"helper_path": profile["interopHelper"], "distribution": "Ubuntu", "user": "test"}]
