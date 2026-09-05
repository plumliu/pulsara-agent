import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientSession
import pytest

from pulsara_agent.conversation_kernel.memory.contracts import canonical_json_bytes
from pulsara_agent.conversation_kernel.memory.management import (
    MemoryManagementError,
    with_end,
)
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.http_server import LocalHttpServer
from tests.test_local_web_http_surface import _model_server_dependencies


def records(*, preview=False, count=1):
    header = dict(type="HEADER", view="global", workspace_id=None, root="memory:root")
    if not preview:
        header["disposition"] = "READY"
    entries = [header]
    if not preview:
        for index in range(count):
            entries.append(
                {
                    "type": "FACT_DELETE",
                    "fact": dict(
                        fact_id=f"memory:{index}",
                        context_id="ctx:global",
                        kind="FACT",
                        lifecycle="ACTIVE",
                        statement="x" * 8000,
                        recorded_at="2026-09-05T00:00:00Z",
                        updated_at="2026-09-05T00:00:00+00:00",
                    ),
                }
            )
    return tuple(canonical_json_bytes(r) for r in with_end(entries))


async def server_for(tmp_path, *, state="ready"):
    (tmp_path / "index.html").write_text("<html>Pulsara</html>")
    core = SimpleNamespace(
        memory_deletion_preview=AsyncMock(return_value=records()),
        execute_memory_deletion=AsyncMock(return_value=records()),
        memory_management_catalog=AsyncMock(
            return_value={"items": [], "next_cursor": None}
        ),
        memory_management_projects=AsyncMock(
            return_value={"items": [], "next_cursor": None}
        ),
        memory_management_detail=AsyncMock(return_value={"source": None}),
    )
    sessions = SimpleNamespace(
        core=core,
        workspace_input=SimpleNamespace(memory_domain_id="server-owned"),
        bootstrap_payload=lambda: {"workspace": {"id": "actual-workspace"}},
    )
    deps = _model_server_dependencies()
    deps["database_state"] = lambda: state
    server = LocalHttpServer(
        sessions=sessions,
        bridge=LocalBrowserBridge(sessions=sessions, protocol_server=None),
        static_root=tmp_path,
        requested_port=0,
        is_ready=lambda: True,
        is_draining=lambda: False,
        **deps,
    )
    await server.start()
    return server, core


def test_stream_over_eight_mib_reaches_executor_with_exact_complete_records(tmp_path):
    async def run():
        server, core = await server_for(tmp_path)
        confirmation = records(count=1100)
        assert sum(map(len, confirmation)) > 8 << 20

        async def execute(**kwargs):
            assert kwargs["memory_domain_id"] == "server-owned"
            assert tuple(kwargs["expected_records"]()) == confirmation
            assert tuple(kwargs["expected_records"]()) == confirmation
            return confirmation

        core.execute_memory_deletion.side_effect = execute

        async def chunks():
            for raw in confirmation:
                yield raw + b"\n"

        try:
            async with ClientSession() as client:
                async with client.delete(
                    server.origin + "/api/memories/memory:root",
                    data=chunks(),
                    headers={
                        "Content-Type": "application/x-ndjson",
                        "Origin": server.origin,
                    },
                ) as response:
                    assert response.status == 200, await response.text()
                    assert (await response.read()).splitlines() == list(confirmation)
            core.execute_memory_deletion.assert_awaited_once()
        finally:
            await server.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_end",
        "extra_after_end",
        "count",
        "wrong_root",
        "extra_field",
        "not_ready",
    ],
)
def test_malformed_confirmation_never_opens_executor(tmp_path, mutation):
    async def run():
        server, core = await server_for(tmp_path)
        stream = list(records())
        if mutation == "missing_end":
            stream.pop()
        if mutation == "extra_after_end":
            stream.append(stream[-1])
        if mutation == "count":
            stream[-1] = b'{"type":"END","counts":{"HEADER":1}}'
        if mutation in {"wrong_root", "extra_field", "not_ready"}:
            header = json.loads(stream[0])
            if mutation == "wrong_root":
                header["root"] = "other"
            if mutation == "extra_field":
                header["memory_domain_id"] = "attacker"
            if mutation == "not_ready":
                header["disposition"] = "NEEDS_RESOLUTION"
            stream[0] = canonical_json_bytes(header)
        try:
            async with ClientSession() as client:
                async with client.delete(
                    server.origin + "/api/memories/memory:root",
                    data=b"\n".join(stream) + b"\n",
                    headers={"Content-Type": "application/x-ndjson"},
                ) as response:
                    assert response.status == 400, await response.text()
            core.execute_memory_deletion.assert_not_called()
        finally:
            await server.aclose()

    asyncio.run(run())


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
                for method, path in (
                    ("GET", ""),
                    ("GET", "/projects"),
                    ("GET", "/memory:root"),
                    ("POST", "/memory:root/deletion-preview"),
                    ("DELETE", "/memory:root"),
                ):
                    async with client.request(
                        method, server.origin + "/api/memories" + path
                    ) as response:
                        assert response.status == 503
            core.execute_memory_deletion.assert_not_called()
            core.memory_management_catalog.assert_not_called()
        finally:
            await server.aclose()

    asyncio.run(run())


def test_temp_storage_failure_never_calls_database(tmp_path, monkeypatch):
    import errno
    from pulsara_agent.web_app import memory_controller

    def fail():
        raise OSError(errno.ENOSPC, "fixture disk full")

    async def run():
        server, core = await server_for(tmp_path)
        monkeypatch.setattr(memory_controller, "TemporaryFile", lambda **kwargs: fail())
        try:
            async with ClientSession() as client:
                async with client.delete(
                    server.origin + "/api/memories/memory:root",
                    data=b"\n".join(records()) + b"\n",
                    headers={"Content-Type": "application/x-ndjson"},
                ) as response:
                    assert response.status == 507
                    assert (await response.json())["error"][
                        "code"
                    ] == "MEMORY_CONFIRMATION_STORAGE_EXHAUSTED"
            core.execute_memory_deletion.assert_not_called()
        finally:
            await server.aclose()

    asyncio.run(run())


def test_preview_drift_and_same_origin_boundary(tmp_path):
    async def run():
        server, core = await server_for(tmp_path)
        core.execute_memory_deletion.side_effect = MemoryManagementError(
            "MEMORY_DELETION_PLAN_DRIFTED", 409, "重新确认", preview=records()
        )
        try:
            async with ClientSession() as client:
                body = b"\n".join(records()) + b"\n"
                async with client.delete(
                    server.origin + "/api/memories/memory:root",
                    data=body,
                    headers={
                        "Content-Type": "application/x-ndjson",
                        "Origin": "https://evil.test",
                    },
                ) as response:
                    assert response.status == 403
                core.execute_memory_deletion.assert_not_called()
                async with client.delete(
                    server.origin + "/api/memories/memory:root",
                    data=body,
                    headers={
                        "Content-Type": "application/x-ndjson",
                        "Origin": server.origin,
                    },
                ) as response:
                    assert response.status == 409
                    assert (await response.read()).splitlines() == list(records())
                async with client.get(
                    server.origin + "/api/memories?memory_domain_id=other"
                ) as response:
                    assert response.status == 400
        finally:
            await server.aclose()

    asyncio.run(run())
