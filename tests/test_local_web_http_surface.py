from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from aiohttp import ClientSession, DummyCookieJar
import pytest

from pulsara_agent import mcp_config
from pulsara_agent.web_app import session_controller as session_controller_module
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.http_server import LocalHttpServer
from pulsara_agent.web_app.session_controller import LocalSessionController
from pulsara_agent.llm.model_catalog import ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.local_credentials import (
    CredentialState,
    DashScopeEmbeddingCredential,
    DashScopeRerankCredential,
    InMemoryCredentialStore,
    ModelProviderCredential,
)
from pulsara_agent.settings import LocalSettingsStore
from pulsara_agent.capability.user_skill_config import (
    load_user_skill_config,
    set_user_skill_enabled,
)
from tests.support.model_config import test_model_runtime


def _model_server_dependencies() -> dict[str, object]:
    runtime = test_model_runtime()

    async def refresh_database_state() -> None:
        return None

    return {
        "settings": runtime.settings,
        "catalog": runtime.catalog,
        "credentials": runtime.credentials,
        "model_runtime": runtime,
        "database_state": lambda: "ready",
        "refresh_database_state": refresh_database_state,
        "postgres_settings_saved": lambda: None,
    }


class _Sessions:
    def __init__(self) -> None:
        self.reconnect_calls: list[tuple[str, str]] = []
        self.install_calls: list[tuple[str, str]] = []
        self.project_skill_toggle_calls: list[tuple[str, str, bool]] = []
        self.project_mcp_create_calls: list[tuple[str, str]] = []
        self.project_mcp_toggle_calls: list[tuple[str, str, str, bool]] = []
        self.project_mcp_remove_calls: list[tuple[str, str, str]] = []
        self.project_mcp_toggle_error: Exception | None = None
        self.user_mcp_toggle_calls: list[tuple[str, bool, str | None]] = []
        self.user_skill_toggle_calls: list[tuple[str, bool, str | None]] = []

    def bootstrap_payload(self) -> dict[str, object]:
        return {"application": {"name": "Pulsara", "version": "test"}}

    async def list_sessions(self) -> list[dict[str, object]]:
        return [
            {
                "id": "session-1",
                "title": "Bare loopback session",
                "status": "ready",
            }
        ]

    async def create_session(
        self, *, workspace_kind: str, workspace_path: str | None
    ) -> SimpleNamespace:
        assert workspace_kind == "quick"
        assert workspace_path is None
        return SimpleNamespace(session_id="session-1")

    async def list_session_tasks(
        self,
        session_id: str,
        *,
        maximum_items: int,
        cursor: str | None,
    ) -> dict[str, object]:
        assert session_id == "session-1"
        assert maximum_items == 17
        assert cursor == "next-page"
        return {
            "session_id": session_id,
            "tasks": [{"id": "task-1", "status": "COMPLETED"}],
            "total_count": 1,
            "page_count": 1,
            "remaining_count": 0,
            "next_cursor": None,
        }

    async def inspect_session_capabilities(self, session_id: str) -> dict[str, object]:
        return _capabilities(session_id)

    async def reconnect_session_mcp(
        self, session_id: str, server_id: str
    ) -> dict[str, object]:
        self.reconnect_calls.append((session_id, server_id))
        return _capabilities(session_id)

    async def install_session_skill(
        self,
        session_id: str,
        *,
        source_path: str,
    ) -> dict[str, object]:
        self.install_calls.append((session_id, source_path))
        return {
            "installation": {
                "status": "INSTALLED",
                "installed": True,
                "message": "技能已经安装。",
            },
            "capabilities": _capabilities(session_id),
        }

    async def set_session_skill_enabled(
        self, session_id: str, *, skill_id: str, enabled: bool
    ) -> dict[str, object]:
        self.project_skill_toggle_calls.append((session_id, skill_id, enabled))
        return {
            "operation": {"status": "DISABLED", "success": True},
            "capabilities": _capabilities(session_id),
        }

    async def create_session_mcp_server(
        self, session_id: str, *, server_id: str, **_values: object
    ) -> dict[str, object]:
        self.project_mcp_create_calls.append((session_id, server_id))
        return {
            "operation": {"status": "ADDED", "success": True},
            "capabilities": _capabilities(session_id),
        }

    async def set_session_mcp_enabled(
        self,
        session_id: str,
        *,
        server_id: str,
        enabled: bool,
        expected_config_identity: str,
    ) -> dict[str, object]:
        if self.project_mcp_toggle_error is not None:
            raise self.project_mcp_toggle_error
        self.project_mcp_toggle_calls.append(
            (session_id, server_id, expected_config_identity, enabled)
        )
        return {
            "operation": {"status": "DISABLED", "success": True},
            "capabilities": _capabilities(session_id),
        }

    async def remove_session_mcp_server(
        self,
        session_id: str,
        *,
        server_id: str,
        expected_config_identity: str,
    ) -> dict[str, object]:
        self.project_mcp_remove_calls.append(
            (session_id, server_id, expected_config_identity)
        )
        return {
            "operation": {"status": "REMOVED", "success": True},
            "capabilities": _capabilities(session_id),
        }

    async def inspect_user_capabilities(
        self, *, active_session_id: str | None
    ) -> dict[str, object]:
        return _user_capabilities(active_session_id)

    async def set_user_mcp_enabled(
        self,
        *,
        server_id: str,
        enabled: bool,
        active_session_id: str | None,
    ) -> dict[str, object]:
        self.user_mcp_toggle_calls.append((server_id, enabled, active_session_id))
        return {
            "operation": {"status": "DISABLED", "success": True},
            "capabilities": _user_capabilities(active_session_id),
        }

    async def set_user_skill_enabled(
        self,
        *,
        skill_path: str,
        enabled: bool,
        active_session_id: str | None,
    ) -> dict[str, object]:
        self.user_skill_toggle_calls.append((skill_path, enabled, active_session_id))
        return {
            "operation": {"status": "DISABLED", "success": True},
            "capabilities": _user_capabilities(active_session_id),
        }


def _capabilities(session_id: str) -> dict[str, object]:
    return {
        "session_id": session_id,
        "workspace_path": "/tmp/project",
        "skills": {
            "status": "ready",
            "items": [],
            "issues": [],
            "details": [],
            "roots": [],
        },
        "mcp": {
            "servers": [{"id": "docs", "name": "Docs", "status": "READY"}],
            "collisions": [],
        },
    }


def _user_capabilities(active_session_id: str | None) -> dict[str, object]:
    return {
        "active_session_id": active_session_id,
        "roots": [
            {"kind": "agents", "path": "/Users/test/.agents"},
            {"kind": "pulsara", "path": "/Users/test/.pulsara"},
        ],
        "skills": {
            "status": "ready",
            "config_path": "/Users/test/.pulsara/skills.yaml",
            "items": [],
            "issues": [],
            "details": [],
            "roots": [],
        },
        "mcp": {
            "config_path": "/Users/test/.pulsara/mcp.yaml",
            "servers": [{"id": "docs", "name": "Docs", "enabled": True}],
        },
        "plugins": {"status": "ready", "items": [], "details": []},
    }


class _Bridge:
    def __init__(self) -> None:
        self.connect_calls: list[tuple[str, str, bool]] = []

    async def connect(
        self,
        session_id: str,
        *,
        browser_instance_id: str,
        takeover: bool = False,
    ) -> dict[str, object]:
        self.connect_calls.append((session_id, browser_instance_id, takeover))
        return {
            "connection_id": "connection-1",
            "connection_generation": 1,
            "session_id": session_id,
            "role": "controller",
        }


def test_zero_config_settings_and_database_surface_stays_usable(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_zero_config_settings_and_database(tmp_path))


async def _exercise_zero_config_settings_and_database(tmp_path: Path) -> None:
    static_root = tmp_path / "static"
    static_root.mkdir()
    (static_root / "index.html").write_text("Pulsara settings", encoding="utf-8")
    base = test_model_runtime()
    settings = LocalSettingsStore(tmp_path / "pulsara" / "local-settings.yaml")
    credentials = InMemoryCredentialStore()
    runtime = ModelRuntime(
        settings=settings,
        catalog=base.catalog,
        credentials=credentials,
        route_wires=base.route_wires,
    )
    database_state = "database_not_configured"
    state_refreshes = 0
    settings_saved = 0

    async def refresh_database_state() -> None:
        nonlocal state_refreshes
        state_refreshes += 1

    def postgres_settings_saved() -> None:
        nonlocal settings_saved, database_state
        settings_saved += 1
        database_state = "database_configured_unverified"

    server = LocalHttpServer(
        sessions=cast(LocalSessionController, _Sessions()),
        bridge=cast(LocalBrowserBridge, _Bridge()),
        static_root=static_root,
        requested_port=0,
        is_ready=lambda: True,
        is_draining=lambda: False,
        settings=settings,
        catalog=base.catalog,
        credentials=credentials,
        model_runtime=runtime,
        database_state=lambda: database_state,
        refresh_database_state=refresh_database_state,
        postgres_settings_saved=postgres_settings_saved,
    )
    await server.start()
    mutation_headers = {
        "Origin": server.origin,
        "Sec-Fetch-Site": "same-origin",
    }
    secret = "model-api-key-sentinel"
    try:
        async with ClientSession(cookie_jar=DummyCookieJar()) as client:
            async with client.get(f"{server.origin}/api/app/bootstrap") as response:
                assert response.status == 200
                payload = await response.json()
                assert payload["database_state"] == "database_not_configured"
                assert payload["model_configurations"] == []

            async with client.get(f"{server.origin}/api/sessions") as response:
                assert response.status == 503
                assert (await response.json())["error"]["code"] == (
                    "DATABASE_DATA_PLANE_UNAVAILABLE"
                )

            async with client.get(f"{server.origin}/api/model-catalog") as response:
                assert response.status == 200
                catalog = await response.json()
                assert catalog["status"] == "ready"
                assert catalog["routes"][0]["route_id"] == "test"
                model = catalog["routes"][0]["models"][0]
                assert model["model_id"] == "test-model"
                executable = [
                    item for item in model["wire_apis"] if item["executable"]
                ]
                assert [item["wire_api"] for item in executable] == [
                    "openai_responses"
                ]

            async with client.post(
                f"{server.origin}/api/model-configurations",
                json={
                    "route_id": "test",
                    "model_id": "test-model",
                    "wire_api": "openai_responses",
                    "api_key": secret,
                },
                headers=mutation_headers,
            ) as response:
                assert response.status == 201
                payload = await response.json()
                rendered = str(payload)
                assert secret not in rendered
                assert payload["model_configuration"]["credential_state"] == "PRESENT"
                connection_id = payload["model_configuration"]["id"]

            stored = settings.read()
            assert len(stored.model_connections) == 1
            assert stored.model_connections[0].id.value == connection_id
            assert secret not in settings.path.read_text(encoding="utf-8")
            assert credentials.state(
                ModelProviderCredential(stored.model_connections[0].id)
            ) is CredentialState.PRESENT

            for kind, key in (
                ("embedding", DashScopeEmbeddingCredential()),
                ("rerank", DashScopeRerankCredential()),
            ):
                async with client.put(
                    f"{server.origin}/api/local-settings/dashscope-credentials/{kind}",
                    json={"api_key": f"{kind}-secret"},
                    headers=mutation_headers,
                ) as response:
                    assert response.status == 200
                    assert (await response.json()) == {
                        "credential_state": "PRESENT"
                    }
                assert credentials.state(key) is CredentialState.PRESENT

            async with client.delete(
                f"{server.origin}/api/local-settings/dashscope-credentials/embedding",
                headers=mutation_headers,
            ) as response:
                assert response.status == 200
                assert (await response.json()) == {"credential_state": "MISSING"}
            assert credentials.state(DashScopeEmbeddingCredential()) is (
                CredentialState.MISSING
            )
            assert credentials.state(DashScopeRerankCredential()) is (
                CredentialState.PRESENT
            )

            async with client.put(
                f"{server.origin}/api/local-settings/postgres",
                json={
                    "runtime_dsn": "postgresql://pulsara@localhost:5432/pulsara",
                    "admin_dsn": None,
                },
                headers=mutation_headers,
            ) as response:
                assert response.status == 200
                payload = await response.json()
                assert payload["database_state"] == "database_configured_unverified"
                assert payload["restart_required"] is False
            assert settings_saved == 1
            assert state_refreshes == 0
            assert settings.read().postgres is not None
    finally:
        await server.aclose()


def test_settings_catalog_refresh_reports_invalid_without_losing_snapshot(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_settings_catalog_refresh_failure(tmp_path))


async def _exercise_settings_catalog_refresh_failure(tmp_path: Path) -> None:
    static_root = tmp_path / "static"
    static_root.mkdir()
    (static_root / "index.html").write_text("Pulsara settings", encoding="utf-8")
    runtime = test_model_runtime()

    async def invalid_fetch() -> object:
        raise ValueError("invalid json")

    runtime.catalog.client = ModelsDevCatalogClient(fetch_override=invalid_fetch)
    server = LocalHttpServer(
        sessions=cast(LocalSessionController, _Sessions()),
        bridge=cast(LocalBrowserBridge, _Bridge()),
        static_root=static_root,
        requested_port=0,
        is_ready=lambda: True,
        is_draining=lambda: False,
        **_model_server_dependencies_for_runtime(runtime),
    )
    await server.start()
    try:
        async with ClientSession(cookie_jar=DummyCookieJar()) as client:
            async with client.post(
                f"{server.origin}/api/model-catalog/refresh",
                headers={"Origin": server.origin, "Sec-Fetch-Site": "same-origin"},
            ) as response:
                assert response.status == 502
                assert (await response.json())["error"]["code"] == (
                    "model_catalog_invalid"
                )
            async with client.get(f"{server.origin}/api/model-catalog") as response:
                assert response.status == 200
                assert (await response.json())["status"] == "ready"
    finally:
        await server.aclose()


def _model_server_dependencies_for_runtime(runtime: ModelRuntime) -> dict[str, object]:
    async def refresh_database_state() -> None:
        return None

    return {
        "settings": runtime.settings,
        "catalog": runtime.catalog,
        "credentials": runtime.credentials,
        "model_runtime": runtime,
        "database_state": lambda: "ready",
        "refresh_database_state": refresh_database_state,
        "postgres_settings_saved": lambda: None,
    }


def test_bare_loopback_origin_serves_app_and_api_without_authentication(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_bare_loopback_origin(tmp_path))


async def _exercise_bare_loopback_origin(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>Pulsara</title><main>local workbench</main>",
        encoding="utf-8",
    )
    bridge = _Bridge()
    sessions = _Sessions()
    server = LocalHttpServer(
        sessions=cast(LocalSessionController, sessions),
        bridge=cast(LocalBrowserBridge, bridge),
        static_root=tmp_path,
        requested_port=0,
        is_ready=lambda: True,
        is_draining=lambda: False,
        **_model_server_dependencies(),
    )
    await server.start()
    try:
        async with ClientSession(cookie_jar=DummyCookieJar()) as client:
            async with client.get(
                f"{server.origin}/", allow_redirects=False
            ) as response:
                assert response.status == 200
                assert "local workbench" in await response.text()
                assert response.headers.get("Location") is None
                assert "Set-Cookie" not in response.headers

            async with client.get(f"{server.origin}/api/app/bootstrap") as response:
                assert response.status == 200
                payload = await response.json()
                assert payload["runtime"]["origin"] == server.origin

            async with client.get(
                f"{server.origin}/api/sessions/session-1/tasks",
                params={"limit": "17", "cursor": "next-page"},
            ) as response:
                assert response.status == 200
                payload = await response.json()
                assert payload["tasks"] == [{"id": "task-1", "status": "COMPLETED"}]

            async with client.get(
                f"{server.origin}/api/sessions/session-1/capabilities"
            ) as response:
                assert response.status == 200
                assert (await response.json())["mcp"]["servers"][0]["id"] == "docs"

            async with client.get(
                f"{server.origin}/api/capabilities",
                params={"active_session_id": "session-1"},
            ) as response:
                assert response.status == 200
                payload = await response.json()
                assert [item["kind"] for item in payload["roots"]] == [
                    "agents",
                    "pulsara",
                ]

            async with client.post(
                f"{server.origin}/api/capabilities/skills/enabled",
                json={
                    "path": "/Users/test/.agents/skills/review/SKILL.md",
                    "enabled": False,
                    "active_session_id": "session-1",
                },
                headers={
                    "Origin": server.origin,
                    "Sec-Fetch-Site": "same-origin",
                },
            ) as response:
                assert response.status == 200
                assert sessions.user_skill_toggle_calls == [
                    (
                        "/Users/test/.agents/skills/review/SKILL.md",
                        False,
                        "session-1",
                    )
                ]

            async with client.post(
                f"{server.origin}/api/capabilities/mcp/docs/enabled",
                json={"enabled": False, "active_session_id": "session-1"},
                headers={
                    "Origin": server.origin,
                    "Sec-Fetch-Site": "same-origin",
                },
            ) as response:
                assert response.status == 200
                assert sessions.user_mcp_toggle_calls == [("docs", False, "session-1")]

            async with client.post(
                f"{server.origin}/api/sessions/session-1/capabilities/mcp/docs/reconnect",
                headers={
                    "Origin": server.origin,
                    "Sec-Fetch-Site": "same-origin",
                },
            ) as response:
                assert response.status == 200
                assert sessions.reconnect_calls == [("session-1", "docs")]

            async with client.post(
                f"{server.origin}/api/sessions/session-1/capabilities/skills/install",
                json={"source_path": "/tmp/pdf"},
                headers={
                    "Origin": server.origin,
                    "Sec-Fetch-Site": "same-origin",
                },
            ) as response:
                assert response.status == 200
                assert (await response.json())["installation"]["installed"] is True
                assert sessions.install_calls == [("session-1", "/tmp/pdf")]

            async with client.post(
                f"{server.origin}/api/sessions/session-1/capabilities/skills/enabled",
                json={"skill_id": "pulsara:review", "enabled": False},
                headers={"Origin": server.origin, "Sec-Fetch-Site": "same-origin"},
            ) as response:
                assert response.status == 200
                assert sessions.project_skill_toggle_calls == [
                    ("session-1", "pulsara:review", False)
                ]

            async with client.post(
                f"{server.origin}/api/sessions/session-1/capabilities/mcp",
                json={
                    "server_id": "project-docs",
                    "display_name": "Project Docs",
                    "transport": "http",
                    "endpoint": "https://example.com/mcp",
                    "args": [],
                    "available_to_subagents": True,
                },
                headers={"Origin": server.origin, "Sec-Fetch-Site": "same-origin"},
            ) as response:
                assert response.status == 201
                assert sessions.project_mcp_create_calls == [
                    ("session-1", "project-docs")
                ]

            async with client.post(
                f"{server.origin}/api/sessions/session-1/capabilities/mcp/project-docs/enabled",
                json={"enabled": False, "config_identity": "config-v1"},
                headers={"Origin": server.origin, "Sec-Fetch-Site": "same-origin"},
            ) as response:
                assert response.status == 200
                assert sessions.project_mcp_toggle_calls == [
                    ("session-1", "project-docs", "config-v1", False)
                ]

            sessions.project_mcp_toggle_error = mcp_config.WorkspaceMcpConfigStaleError(
                "changed"
            )
            async with client.post(
                f"{server.origin}/api/sessions/session-1/capabilities/mcp/project-docs/enabled",
                json={"enabled": True, "config_identity": "config-v1"},
                headers={"Origin": server.origin, "Sec-Fetch-Site": "same-origin"},
            ) as response:
                assert response.status == 409
                payload = await response.json()
                assert payload["error"]["code"] == "PROJECT_CAPABILITY_STALE"
                assert payload["error"]["retryable"] is True
            sessions.project_mcp_toggle_error = None

            async with client.delete(
                f"{server.origin}/api/sessions/session-1/capabilities/mcp/project-docs",
                json={"config_identity": "config-v1"},
                headers={"Origin": server.origin, "Sec-Fetch-Site": "same-origin"},
            ) as response:
                assert response.status == 200
                assert sessions.project_mcp_remove_calls == [
                    ("session-1", "project-docs", "config-v1")
                ]

            async with client.post(
                f"{server.origin}/api/sessions",
                json={"workspace_kind": "quick"},
                headers={
                    "Origin": server.origin,
                    "Sec-Fetch-Site": "same-origin",
                },
            ) as response:
                assert response.status == 201
                assert (await response.json())["session"]["id"] == "session-1"

            async with client.post(
                f"{server.origin}/api/sessions/session-1/connections",
                json={
                    "browser_instance_id": ("00000000-0000-4000-8000-000000000001"),
                    "takeover": True,
                },
                headers={
                    "Origin": server.origin,
                    "Sec-Fetch-Site": "same-origin",
                },
            ) as response:
                assert response.status == 201
                assert (await response.json())["role"] == "controller"
                assert bridge.connect_calls == [
                    (
                        "session-1",
                        "00000000-0000-4000-8000-000000000001",
                        True,
                    )
                ]
    finally:
        await server.aclose()


def test_loopback_surface_keeps_host_and_cross_site_mutation_guards(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_request_guards(tmp_path))


async def _exercise_request_guards(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("Pulsara", encoding="utf-8")
    server = LocalHttpServer(
        sessions=cast(LocalSessionController, _Sessions()),
        bridge=cast(LocalBrowserBridge, object()),
        static_root=tmp_path,
        requested_port=0,
        is_ready=lambda: True,
        is_draining=lambda: False,
        **_model_server_dependencies(),
    )
    await server.start()
    try:
        async with ClientSession(cookie_jar=DummyCookieJar()) as client:
            async with client.get(
                f"{server.origin}/api/app/bootstrap",
                headers={"Host": f"localhost:{server.port}"},
            ) as response:
                assert response.status == 421
                assert (await response.json())["error"]["code"] == "HOST_REJECTED"

            async with client.post(
                f"{server.origin}/api/sessions",
                json={"workspace_kind": "quick"},
                headers={"Origin": "https://example.invalid"},
            ) as response:
                assert response.status == 403
                assert (await response.json())["error"]["code"] == "ORIGIN_REJECTED"

            async with client.post(
                f"{server.origin}/api/sessions",
                json={"workspace_kind": "quick"},
                headers={"Sec-Fetch-Site": "cross-site"},
            ) as response:
                assert response.status == 403
                assert (await response.json())["error"][
                    "code"
                ] == "CROSS_SITE_REQUEST_REJECTED"
    finally:
        await server.aclose()


def test_user_skill_switch_is_an_atomic_path_based_user_config(tmp_path: Path) -> None:
    config_path = tmp_path / ".pulsara" / "skills.yaml"
    skill_path = tmp_path / ".agents" / "skills" / "review" / "SKILL.md"

    set_user_skill_enabled(
        skill_path=skill_path,
        enabled=False,
        config_path=config_path,
    )
    disabled = load_user_skill_config(config_path=config_path)
    assert disabled.available is True
    assert disabled.enabled_for(skill_path) is False

    set_user_skill_enabled(
        skill_path=skill_path,
        enabled=True,
        config_path=config_path,
    )
    enabled = load_user_skill_config(config_path=config_path)
    assert enabled.enabled_for(skill_path) is True

    config_path.write_text("skills: [broken", encoding="utf-8")
    unavailable = load_user_skill_config(config_path=config_path)
    assert unavailable.available is False
    assert unavailable.enabled_for(skill_path) is False


def test_user_mcp_live_overlay_requires_exact_user_source_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_path = tmp_path / "user-mcp.yaml"
    workspace = tmp_path / "workspace"
    workspace_config_path = workspace / ".pulsara" / "mcp.yaml"
    workspace_config_path.parent.mkdir(parents=True)
    user_path.write_text(
        "servers:\n"
        "  docs:\n"
        "    transport:\n"
        "      type: streamable_http\n"
        "      endpoint: https://user.example/mcp\n",
        encoding="utf-8",
    )
    workspace_config_path.write_text(
        "servers:\n"
        "  docs:\n"
        "    transport:\n"
        "      type: streamable_http\n"
        "      endpoint: https://workspace.example/mcp\n",
        encoding="utf-8",
    )
    (user_config,) = mcp_config.load_mcp_server_configs(user_config_path=user_path)
    (workspace_config,) = mcp_config.load_mcp_server_configs(
        workspace_root=workspace,
        user_config_path=user_path,
        trust_workspace_config=True,
    )

    live_server = SimpleNamespace(
        server_id="docs",
        status=SimpleNamespace(value="READY"),
        exposed_tool_count=1,
        resource_count=0,
        resource_template_count=0,
        prompt_count=0,
        sanitized_instructions="workspace instructions",
        stable_failure_category=None,
    )
    live_tool = SimpleNamespace(
        server_id="docs",
        provider_name="workspace_tool",
        remote_name="workspace_tool",
        description="workspace-only description",
        effect="READ_ONLY",
        available_to_subagents=False,
        parallel_safe=False,
    )

    monkeypatch.setattr(
        session_controller_module,
        "_user_plugins_payload",
        lambda _inspection: {"status": "ready", "items": [], "details": []},
    )

    def project(live_config: object) -> dict[str, object]:
        return session_controller_module._user_capability_payload(
            skills={},
            mcp_configs=(user_config,),
            plugins=object(),
            live_inspection=SimpleNamespace(
                mcp_catalog=SimpleNamespace(servers=(live_server,)),
                mcp_configured_servers=(live_config,),
                mcp_tools=(live_tool,),
            ),
        )

    workspace_payload = project(
        SimpleNamespace(
            server_id="docs",
            resolved_config_identity=workspace_config.resolved_config_identity,
            source_kind=mcp_config.McpLocalConfigSourceKind.WORKSPACE.value,
            status_matches_config=True,
        )
    )
    workspace_item = workspace_payload["mcp"]["servers"][0]  # type: ignore[index]
    assert workspace_item["status"] == "CONFIGURED"
    assert workspace_item["tool_count"] == 0
    assert workspace_item["tools"] == []

    joined_payload = project(
        SimpleNamespace(
            server_id="docs",
            resolved_config_identity=user_config.resolved_config_identity,
            source_kind=mcp_config.McpLocalConfigSourceKind.USER.value,
            status_matches_config=True,
        )
    )
    joined_item = joined_payload["mcp"]["servers"][0]  # type: ignore[index]
    assert joined_item["status"] == "READY"
    assert joined_item["tool_count"] == 1
    assert joined_item["tools"][0]["name"] == "workspace_tool"

    stale_user_path = tmp_path / "stale-user-mcp.yaml"
    stale_user_path.write_text(
        "servers:\n"
        "  docs:\n"
        "    transport:\n"
        "      type: streamable_http\n"
        "      endpoint: https://old-user.example/mcp\n",
        encoding="utf-8",
    )
    (stale_user_config,) = mcp_config.load_mcp_server_configs(
        user_config_path=stale_user_path
    )
    stale_payload = project(
        SimpleNamespace(
            server_id="docs",
            resolved_config_identity=stale_user_config.resolved_config_identity,
            source_kind=mcp_config.McpLocalConfigSourceKind.USER.value,
            status_matches_config=True,
        )
    )
    stale_item = stale_payload["mcp"]["servers"][0]  # type: ignore[index]
    assert stale_item["status"] == "CONFIGURED"
    assert stale_item["tool_count"] == 0
    assert stale_item["tools"] == []

    retained_status_payload = project(
        SimpleNamespace(
            server_id="docs",
            resolved_config_identity=user_config.resolved_config_identity,
            source_kind=mcp_config.McpLocalConfigSourceKind.USER.value,
            status_matches_config=False,
        )
    )
    retained_status_item = retained_status_payload["mcp"]["servers"][0]  # type: ignore[index]
    assert retained_status_item["status"] == "CONFIGURED"
    assert retained_status_item["tool_count"] == 0
    assert retained_status_item["tools"] == []
