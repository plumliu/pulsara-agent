"""Modern catalog observation through official SDK servers over local HTTP."""

import asyncio
from contextlib import asynccontextmanager
import socket

import anyio
from mcp.server.lowlevel import Server
from mcp.server.subscriptions import (
    InMemorySubscriptionBus, ListenHandler,
    PromptsListChanged, ResourcesListChanged, ToolsListChanged,
)
from mcp.shared.exceptions import MCPError
import mcp_types as types
import pytest
import uvicorn

from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary


@asynccontextmanager
async def _sdk_http(server):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    sock.setblocking(False)
    app = server.streamable_http_app(stateless_http=True)
    runner = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="none"))
    task = asyncio.create_task(runner.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not runner.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
    finally:
        runner.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


def _supervisor(tmp_path, endpoint, *, required=True):
    config = _parse_server("catalog", {
        "required": required,
        "catalog_refresh_interval_ms": "DISABLED",
        "transport": {
            "type": "streamable_http", "endpoint": endpoint,
            "allow_http_localhost": True,
        },
    })
    return McpHostSupervisor(
        session_id="session:subscription-test", workspace_root=tmp_path,
        configs=(config,), credential_boundary=ProcessCredentialBoundary(),
        optional_fast_start_seconds=0.01,
    )


@pytest.mark.parametrize("surface", ["tools", "resources", "prompts"])
def test_modern_catalog_changes_relist_same_physical_session(tmp_path, surface):
    async def scenario():
        bus = InMemorySubscriptionBus()
        handler = ListenHandler(bus)
        filters = []
        revision = 1

        async def subscriptions(ctx, params):
            filters.append(params.notifications.model_dump(exclude_none=True))
            return await handler(ctx, params)

        async def tools(_ctx, _params):
            return types.ListToolsResult(tools=[types.Tool(
                name=f"tool_{revision}", input_schema={"type": "object"},
            )])

        async def resources(_ctx, _params):
            return types.ListResourcesResult(resources=[types.Resource(
                name=f"resource_{revision}", uri=f"test://resource/{revision}",
            )])

        async def templates(_ctx, _params):
            return types.ListResourceTemplatesResult(resource_templates=[])

        async def prompts(_ctx, _params):
            return types.ListPromptsResult(prompts=[types.Prompt(name=f"prompt_{revision}")])

        server = Server(
            "dynamic-catalog", on_subscriptions_listen=subscriptions,
            on_list_tools=tools if surface == "tools" else None,
            on_list_resources=resources if surface == "resources" else None,
            on_list_resource_templates=templates if surface == "resources" else None,
            on_list_prompts=prompts if surface == "prompts" else None,
        )
        async with _sdk_http(server) as endpoint:
            supervisor = _supervisor(tmp_path, endpoint)
            first = second = None
            try:
                await supervisor.start()
                first = supervisor.install_pending_at_safe_point()
                slot = first.slot_lease_by_server["catalog"]._slot
                assert filters == [{f"{surface}_list_changed": True}]
                revision = 2
                event = {
                    "tools": ToolsListChanged(), "resources": ResourcesListChanged(),
                    "prompts": PromptsListChanged(),
                }[surface]
                await bus.publish(event)
                async with asyncio.timeout(5):
                    while "catalog" not in supervisor._pending:
                        await asyncio.sleep(0.01)
                snapshot = supervisor._pending["catalog"].discovery_snapshot
                items = getattr(snapshot, surface)
                name = items[0].remote_tool_name if surface == "tools" else items[0].name
                assert name == {"tools": "tool_2", "resources": "resource_2", "prompts": "prompt_2"}[surface]
                second = supervisor.install_pending_at_safe_point()
                assert second.slot_lease_by_server["catalog"]._slot is slot
                assert not slot.lease_is_current(first.slot_lease_by_server["catalog"])
                assert len(filters) == 1
            finally:
                if first:
                    first.release()
                if second:
                    second.release()
                await supervisor.aclose()
            assert not bus._listeners
            assert not handler._streams

    asyncio.run(scenario())


@pytest.mark.parametrize("closure", ["graceful", "lost"])
def test_modern_subscription_end_reconnects_and_refetches(tmp_path, closure):
    async def scenario():
        bus = InMemorySubscriptionBus()
        handler = ListenHandler(bus)
        listeners = 0
        revision = 1
        drop = asyncio.Event()

        async def subscriptions(ctx, params):
            nonlocal listeners
            listeners += 1
            if closure == "lost" and listeners == 1:
                async with anyio.create_task_group() as group:
                    group.start_soon(handler, ctx, params)
                    await drop.wait()
                    group.cancel_scope.cancel()
                raise MCPError(types.INTERNAL_ERROR, "fixture subscription lost")
            return await handler(ctx, params)

        async def tools(_ctx, _params):
            return types.ListToolsResult(tools=[types.Tool(
                name=f"tool_{revision}", input_schema={"type": "object"},
            )])

        server = Server("subscription-close", on_list_tools=tools,
                        on_subscriptions_listen=subscriptions)
        async with _sdk_http(server) as endpoint:
            supervisor = _supervisor(tmp_path, endpoint)
            try:
                await supervisor.start()
                original = supervisor._slots["catalog"]
                revision = 2
                if closure == "lost":
                    drop.set()
                else:
                    handler.close()
                async with asyncio.timeout(5):
                    while (supervisor._slots.get("catalog") is None
                           or supervisor._slots["catalog"] is original):
                        await asyncio.sleep(0.01)
                assert listeners == 2
                assert supervisor._pending["catalog"].discovery_snapshot.tools[0].remote_tool_name == "tool_2"
                assert original.client._closed
            finally:
                await supervisor.aclose()
            assert not bus._listeners

    asyncio.run(scenario())


def test_partial_subscription_ack_is_retryable_observation_failure(tmp_path):
    async def scenario():
        bus = InMemorySubscriptionBus()
        handler = ListenHandler(bus)

        async def subscriptions(ctx, _params):
            return await handler(ctx, types.SubscriptionsListenRequestParams(
                notifications=types.SubscriptionFilter(),
            ))

        async def tools(_ctx, _params):
            return types.ListToolsResult(tools=[])

        server = Server("partial-ack", on_list_tools=tools,
                        on_subscriptions_listen=subscriptions)
        async with _sdk_http(server) as endpoint:
            supervisor = _supervisor(tmp_path, endpoint, required=False)
            try:
                await supervisor.start()
                async with asyncio.timeout(5):
                    while supervisor._state["catalog"].value == "CONNECTING":
                        await asyncio.sleep(0.01)
                assert supervisor._state["catalog"].value == "FAILED_RETRYABLE"
                assert "catalog" in supervisor._retry_tasks
                assert "catalog" not in supervisor._pending
            finally:
                await supervisor.aclose()

    asyncio.run(scenario())
