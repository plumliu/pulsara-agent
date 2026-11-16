"""User memory HTTP CRUD uses server authority and explicit page selections."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiohttp import ClientSession
import pytest
from pulsara_agent.desktop_app.renderer_bridge import RendererBridge
from pulsara_agent.desktop_app.http_server import LocalHttpServer
from tests.test_desktop_http_surface import _model_server_dependencies
from pulsara_agent.identity import random_id


async def server_for(tmp_path, *, state="ready"):
    (tmp_path / "index.html").write_text("<html>Pulsara</html>")
    core = SimpleNamespace(
        **{
            name: AsyncMock(return_value={})
            for name in (
                "memory_management_detail",
                "memory_management_create",
                "memory_management_edit",
                "memory_management_delete",
            )
        },
        memory_management_catalog=AsyncMock(
            return_value={"items": [], "next_cursor": None}
        ),
        memory_management_projects=AsyncMock(
            return_value={"items": [], "next_cursor": None}
        ),
    )
    sessions = SimpleNamespace(
        core=core,
        memory_domain_id="server-owned",
        bootstrap_payload=lambda: (_ for _ in ()).throw(
            AssertionError("must not inspect active session")
        ),
    )
    deps = _model_server_dependencies()
    deps["database_state"] = lambda: state
    server = LocalHttpServer(
        sessions=sessions,
        bridge=RendererBridge(sessions=sessions, protocol_server=None),
        static_root=tmp_path,
        requested_port=0,
        is_ready=lambda: True,
        is_draining=lambda: False,
        **deps,
    )
    await server.start()
    return server, core


@pytest.mark.parametrize(
    "state",
    [
        "database_not_configured",
        "database_configured_unverified",
        "database_unavailable",
        "database_schema_action_required",
    ],
)
def test_all_memory_routes_share_readiness_gate(tmp_path, state):
    async def run():
        server, core = await server_for(tmp_path, state=state)
        try:
            async with ClientSession() as client:
                for method, path in [
                    ("GET", ""),
                    ("POST", ""),
                    ("GET", "/projects"),
                    ("GET", "/" + random_id("memory")),
                    ("PATCH", "/" + random_id("memory")),
                    ("DELETE", "/" + random_id("memory")),
                ]:
                    async with client.request(
                        method, server.origin + "/api/memories" + path
                    ) as response:
                        assert response.status == 503, await response.text()
            core.memory_management_catalog.assert_not_called()
            core.memory_management_create.assert_not_called()
        finally:
            await server.aclose()

    asyncio.run(run())


def test_page_crud_is_session_independent_and_preserves_exact_complete_values(tmp_path):
    async def run():
        server, core = await server_for(tmp_path)
        identifier = random_id("memory")
        stamp = "2026-09-05T00:00:00.123456Z"
        body = "字" * 8000
        try:
            async with ClientSession() as client:
                for path in (
                    "/projects",
                    "?view=global",
                    "/" + identifier + "?view=global",
                ):
                    async with client.get(
                        server.origin + "/api/memories" + path
                    ) as response:
                        assert response.status == 200, await response.text()
                for method, path, payload, status in [
                    (
                        "POST",
                        "",
                        {
                            "view": "global",
                            "kind": "FACT",
                            "title": "标题",
                            "statement": body,
                        },
                        201,
                    ),
                    (
                        "PATCH",
                        "/" + identifier,
                        {
                            "view": "global",
                            "expected_updated_at": stamp,
                            "statement": body,
                            "destination": {
                                "view": "project",
                                "workspace_id": random_id("workspace"),
                            },
                        },
                        200,
                    ),
                    (
                        "DELETE",
                        "/" + identifier,
                        {"view": "global", "expected_updated_at": stamp},
                        200,
                    ),
                ]:
                    async with client.request(
                        method,
                        server.origin + "/api/memories" + path,
                        json=payload,
                        headers={"Origin": server.origin},
                    ) as response:
                        assert response.status == status, await response.text()
            assert core.memory_management_create.await_args.kwargs["statement"] == body
            assert core.memory_management_edit.await_args.kwargs["statement"] == body
            assert (
                core.memory_management_edit.await_args.kwargs["expected_updated_at"]
                == stamp
            )
            for name in (
                "memory_management_projects",
                "memory_management_catalog",
                "memory_management_detail",
                "memory_management_create",
                "memory_management_edit",
                "memory_management_delete",
            ):
                assert (
                    getattr(core, name).await_args.kwargs["memory_domain_id"]
                    == "server-owned"
                )
        finally:
            await server.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "method,payload",
    [
        (
            "POST",
            {
                "view": "global",
                "memory_domain_id": "attacker",
                "kind": "FACT",
                "title": "x",
                "statement": "y",
            },
        ),
        ("PATCH", {"view": "global", "expected_updated_at": "x"}),
        ("PATCH", {"view": "global", "expected_updated_at": "x", "title": "mutate"}),
        ("DELETE", {"view": "global", "expected_updated_at": "x", "extra": True}),
    ],
)
def test_malformed_mutation_never_reaches_business_owner(tmp_path, method, payload):
    async def run():
        server, core = await server_for(tmp_path)
        try:
            async with ClientSession() as client:
                path = "" if method == "POST" else "/" + random_id("memory")
                async with client.request(
                    method, server.origin + "/api/memories" + path, json=payload
                ) as response:
                    assert response.status == 400, await response.text()
            core.memory_management_create.assert_not_called()
            core.memory_management_edit.assert_not_called()
            core.memory_management_delete.assert_not_called()
        finally:
            await server.aclose()

    asyncio.run(run())


def test_mutations_reject_cross_origin_before_business_owner(tmp_path):
    async def run():
        server, core = await server_for(tmp_path)
        try:
            async with ClientSession() as client:
                async with client.post(
                    server.origin + "/api/memories",
                    json={
                        "view": "global",
                        "kind": "FACT",
                        "title": "x",
                        "statement": "y",
                    },
                    headers={"Origin": "https://attacker.example"},
                ) as response:
                    assert response.status == 403
            core.memory_management_create.assert_not_called()
        finally:
            await server.aclose()

    asyncio.run(run())
