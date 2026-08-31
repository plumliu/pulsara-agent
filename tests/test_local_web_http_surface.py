from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from aiohttp import ClientSession, DummyCookieJar

from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.http_server import LocalHttpServer
from pulsara_agent.web_app.session_controller import LocalSessionController


class _Sessions:
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


class _Bridge:
    def __init__(self) -> None:
        self.connect_calls: list[tuple[str, bool]] = []

    async def connect(
        self, session_id: str, *, takeover: bool = False
    ) -> dict[str, object]:
        self.connect_calls.append((session_id, takeover))
        return {
            "connection_id": "connection-1",
            "connection_generation": 1,
            "session_id": session_id,
            "role": "controller",
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
    server = LocalHttpServer(
        sessions=cast(LocalSessionController, _Sessions()),
        bridge=cast(LocalBrowserBridge, bridge),
        static_root=tmp_path,
        requested_port=0,
        is_ready=lambda: True,
        is_draining=lambda: False,
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
                assert payload["tasks"] == [
                    {"id": "task-1", "status": "COMPLETED"}
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
                json={"takeover": True},
                headers={
                    "Origin": server.origin,
                    "Sec-Fetch-Site": "same-origin",
                },
            ) as response:
                assert response.status == 201
                assert (await response.json())["role"] == "controller"
                assert bridge.connect_calls == [("session-1", True)]
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
