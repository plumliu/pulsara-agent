"""Scheduling isolation through real SDK sessions and local HTTP peers."""

import asyncio
from contextlib import asynccontextmanager

from aiohttp import web
from mcp.server.lowlevel import Server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
import mcp_types as types
import pytest

from pulsara_agent.conversation_kernel.mcp.contracts import MAXIMUM_MCP_HOST_IN_FLIGHT
from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.test_mcp_catalog_subscriptions import _sdk_http


class _Peers:
    def __init__(self, *, block_listing=False, parallel=False):
        self.block_listing = block_listing
        self.parallel = parallel
        self.release_listing = asyncio.Event()
        self.listing_started = asyncio.Event()
        self.release_calls = asyncio.Event()
        self.requests = {"slow": [], "fast": []}
        self.calls = {"slow": 0, "fast": 0}

    async def endpoint(self, request):
        peer = request.match_info["peer"]
        message = await request.json()
        method = message["method"]
        self.requests[peer].append(method)
        if "id" not in message:
            return web.Response(status=202)
        if method == "server/discover":
            payload = {"error": {"code": -32601, "message": "legacy fixture"}}
        elif method == "initialize":
            payload = {"result": {
                "protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                "serverInfo": {"name": peer, "version": "1"},
            }}
        elif method == "tools/list":
            if peer == "slow" and self.block_listing:
                self.listing_started.set()
                await self.release_listing.wait()
            payload = {"result": {"tools": [{
                "name": "probe", "inputSchema": {"type": "object"},
            }]}}
        else:
            assert method == "tools/call"
            self.calls[peer] += 1
            if peer == "slow":
                await self.release_calls.wait()
            payload = {"result": {"content": [{"type": "text", "text": "ok"}]}}
        return web.json_response({"jsonrpc": "2.0", "id": message["id"], **payload})

    @asynccontextmanager
    async def serve(self):
        app = web.Application()
        app.router.add_post("/{peer}", self.endpoint)
        runner = web.AppRunner(app, shutdown_timeout=0.1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            yield tuple(_parse_server(server_id, {
                "required": not self.block_listing,
                "effect_policy": {"default_effect": "READ_ONLY"},
                "supports_parallel_tool_calls": self.parallel,
                "stateless_http_max_in_flight": MAXIMUM_MCP_HOST_IN_FLIGHT,
                "transport": {
                    "type": "streamable_http",
                    "endpoint": f"http://127.0.0.1:{port}/{peer}",
                    "allow_http_localhost": True,
                    "stateless_http_asserted": self.parallel,
                },
            }) for server_id, peer in (("a_slow", "slow"), ("b_fast", "fast")))
        finally:
            self.release_listing.set()
            self.release_calls.set()
            await runner.cleanup()


def _supervisor(tmp_path, configs, *, attempt_timeout=120):
    return McpHostSupervisor(
        session_id="session:scheduling", workspace_root=tmp_path, configs=configs,
        credential_boundary=ProcessCredentialBoundary(),
        optional_fast_start_seconds=0.01,
        connect_attempt_timeout_seconds=attempt_timeout,
    )


def _invoke(executor, call_id):
    permit = executor.admit(
        session_id="session:scheduling", scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None, turn_id="turn:scheduling", tool_call_id=call_id,
    )
    permit.mark_attempt_accepted()
    return asyncio.create_task(executor.invoke(permit, {}))


async def _until(predicate):
    async with asyncio.timeout(5):  # Test hang detector, not a product lifetime.
        while not predicate():
            await asyncio.sleep(0.01)


def test_discovery_queue_does_not_consume_connection_attempt_budget(tmp_path):
    async def scenario():
        peers = _Peers(block_listing=True)
        async with peers.serve() as configs:
            # Two preceding attempts consume their own budgets. The healthy
            # peer waits longer than one complete budget before admission.
            another_slow = _parse_server("b_also_slow", {"transport": {
                "type": "streamable_http", "endpoint": configs[0].transport.endpoint,
                "allow_http_localhost": True,
            }})
            configs = (configs[0], another_slow, configs[1])
            supervisor = _supervisor(tmp_path, configs, attempt_timeout=0.25)
            try:
                await supervisor.start()
                await asyncio.wait_for(peers.listing_started.wait(), 2)
                assert not peers.requests["fast"]
                await _until(lambda: supervisor._state["b_fast"].value == "READY")
                assert peers.requests["fast"] == ["server/discover", "initialize",
                                                  "notifications/initialized", "tools/list"]
                assert supervisor._state["a_slow"].value == "FAILED_RETRYABLE"
                assert supervisor._state["b_also_slow"].value == "FAILED_RETRYABLE"
                assert peers.requests["slow"].count("tools/list") == 2
                assert "b_fast" not in supervisor._failure
                assert supervisor._attempt_generation["b_fast"] == 1
            finally:
                peers.release_listing.set()
                await supervisor.aclose()
            assert supervisor._discovery_reservations._value == 1
            assert not supervisor._connection_tasks

    asyncio.run(scenario())


def test_cancelling_a_connection_queued_for_discovery_keeps_reservation_usable(tmp_path):
    async def scenario():
        peers = _Peers(block_listing=True)
        async with peers.serve() as configs:
            supervisor = _supervisor(tmp_path, configs)
            try:
                await supervisor.start()
                await asyncio.wait_for(peers.listing_started.wait(), 2)
                queued = supervisor._tasks["b_fast"]
                supervisor.reload_configs((configs[0],))
                await asyncio.wait_for(queued, 2)
                assert not peers.requests["fast"]
                assert supervisor._discovery_reservations._value == 0
                peers.release_listing.set()
                await _until(lambda: supervisor._state["a_slow"].value == "READY")
                supervisor.reload_configs(configs)
                await _until(lambda: supervisor._state["b_fast"].value == "READY")
                assert peers.requests["fast"]
            finally:
                peers.release_listing.set()
                await supervisor.aclose()
            assert supervisor._discovery_reservations._value == 1
            assert not supervisor._connection_tasks

    asyncio.run(scenario())


def test_serial_server_queue_leaves_host_capacity_for_healthy_server(tmp_path):
    async def scenario():
        peers = _Peers()
        async with peers.serve() as configs:
            supervisor = _supervisor(tmp_path, configs)
            runtime = None
            tasks = []
            try:
                await supervisor.start()
                runtime = supervisor.install_pending_at_safe_point()
                executors = {item.semantic.server_id: item for item in runtime.executors.values()}
                slow_slot = runtime.slot_lease_by_server["a_slow"]._slot
                tasks = [_invoke(executors["a_slow"], f"call:slow:{index}")
                         for index in range(MAXIMUM_MCP_HOST_IN_FLIGHT)]
                await _until(lambda: peers.calls["slow"] == 1)
                assert slow_slot.active_dispatch_count == MAXIMUM_MCP_HOST_IN_FLIGHT
                healthy = _invoke(executors["b_fast"], "call:healthy")
                tasks.append(healthy)
                assert (await asyncio.wait_for(healthy, 2)).state == "SUCCESS"
                assert peers.calls == {"slow": 1, "fast": 1}
                assert supervisor._host_operation_lane._value == MAXIMUM_MCP_HOST_IN_FLIGHT - 1
                for task in tasks[1:-1]:
                    task.cancel()
                cancelled = await asyncio.gather(*tasks[1:-1], return_exceptions=True)
                assert all(isinstance(item, asyncio.CancelledError) for item in cancelled)
                assert slow_slot.active_dispatch_count == 1
                peers.release_calls.set()
                assert (await asyncio.wait_for(tasks[0], 2)).state == "SUCCESS"
                assert slow_slot.active_dispatch_count == 0
                assert supervisor._host_operation_lane._value == MAXIMUM_MCP_HOST_IN_FLIGHT
            finally:
                peers.release_calls.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if runtime:
                    runtime.release()
                await supervisor.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("waiting_operation", ["tool", "catalog"])
def test_cancel_waiting_for_host_releases_server_lane(tmp_path, waiting_operation):
    async def scenario():
        peers = _Peers(parallel=True)
        async with peers.serve() as configs:
            supervisor = _supervisor(tmp_path, configs)
            runtime = None
            tasks = []
            try:
                await supervisor.start()
                runtime = supervisor.install_pending_at_safe_point()
                executors = {item.semantic.server_id: item for item in runtime.executors.values()}
                fast_slot = runtime.slot_lease_by_server["b_fast"]._slot
                tasks = [_invoke(executors["a_slow"], f"call:occupy:{index}")
                         for index in range(MAXIMUM_MCP_HOST_IN_FLIGHT)]
                await _until(lambda: peers.calls["slow"] == MAXIMUM_MCP_HOST_IN_FLIGHT)
                assert supervisor._host_operation_lane._value == 0
                assert sum(slot.active_operation_count for slot in supervisor._slots.values()) == MAXIMUM_MCP_HOST_IN_FLIGHT

                async def catalog():
                    async with fast_slot.catalog_operation(
                        discovery_reservation=supervisor._discovery_reservations
                    ):
                        return await fast_slot.client.session.list_tools()

                waiter = (
                    _invoke(executors["b_fast"], "call:queued-host")
                    if waiting_operation == "tool" else asyncio.create_task(catalog())
                )
                tasks.append(waiter)
                await _until(lambda: fast_slot._lane._value == MAXIMUM_MCP_HOST_IN_FLIGHT - 1)
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                assert fast_slot._lane._value == MAXIMUM_MCP_HOST_IN_FLIGHT
                assert fast_slot.active_dispatch_count == 0
                assert fast_slot.active_operation_count == 0
                assert supervisor._discovery_reservations._value == 1
                assert peers.calls["fast"] == 0
                assert peers.requests["fast"].count("tools/list") == 1
                peers.release_calls.set()
                await asyncio.wait_for(asyncio.gather(*tasks[:-1]), 3)
                await asyncio.wait_for(catalog(), 2)
                healthy = _invoke(executors["b_fast"], "call:after-cancel")
                tasks.append(healthy)
                assert (await asyncio.wait_for(healthy, 2)).state == "SUCCESS"
                assert supervisor._host_operation_lane._value == MAXIMUM_MCP_HOST_IN_FLIGHT
            finally:
                peers.release_calls.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if runtime:
                    runtime.release()
                await supervisor.aclose()

    asyncio.run(scenario())


def test_catalog_refresh_queue_does_not_reserve_host_io_capacity(tmp_path):
    async def scenario():
        bus = InMemorySubscriptionBus()
        listener = ListenHandler(bus)
        release_refresh = asyncio.Event()
        refresh_started = asyncio.Event()
        refreshing = False
        refresh_requests = 0

        async def tools(_ctx, _params):
            nonlocal refresh_requests
            if refreshing:
                refresh_requests += 1
                refresh_started.set()
                await release_refresh.wait()
            return types.ListToolsResult(tools=[types.Tool(
                name="probe", input_schema={"type": "object"},
            )])

        server = Server("refresh-isolation", on_list_tools=tools,
                        on_subscriptions_listen=listener)
        peers = _Peers()
        async with _sdk_http(server) as endpoint, peers.serve() as peer_configs:
            slow_ids = [f"slow_{index:02}" for index in range(MAXIMUM_MCP_HOST_IN_FLIGHT)]
            configs = tuple(_parse_server(server_id, {
                "required": True,
                "transport": {"type": "streamable_http", "endpoint": endpoint,
                              "allow_http_localhost": True},
            }) for server_id in slow_ids) + (peer_configs[1],)
            supervisor = _supervisor(tmp_path, configs)
            runtime = None
            try:
                await supervisor.start()
                runtime = supervisor.install_pending_at_safe_point()
                refreshing = True
                await bus.publish(ToolsListChanged())
                await asyncio.wait_for(refresh_started.wait(), 3)
                await _until(lambda: all(supervisor._slots[key]._lane._value == 0
                                         for key in slow_ids))
                assert len(supervisor._refresh_tasks) == MAXIMUM_MCP_HOST_IN_FLIGHT
                assert refresh_requests == 1
                assert supervisor._discovery_reservations._value == 0
                assert supervisor._host_operation_lane._value == MAXIMUM_MCP_HOST_IN_FLIGHT - 1
                assert sum(slot.active_operation_count for slot in supervisor._slots.values()) == 1
                healthy = next(item for item in runtime.executors.values()
                               if item.semantic.server_id == "b_fast")
                assert (await asyncio.wait_for(_invoke(healthy, "call:during-refresh"), 2)).state == "SUCCESS"
                assert peers.calls["fast"] == 1

                # Cancel a real relist waiting for discovery memory, leaving
                # the active relist's reservation and Host permit untouched.
                waiting_id = next(key for key in slow_ids
                                  if supervisor._slots[key].active_operation_count == 0)
                waiter = supervisor._refresh_tasks[waiting_id]
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                assert supervisor._slots[waiting_id]._lane._value == 1
                assert supervisor._discovery_reservations._value == 0
                assert supervisor._host_operation_lane._value == MAXIMUM_MCP_HOST_IN_FLIGHT - 1
                release_refresh.set()
                await asyncio.wait_for(asyncio.gather(*(
                    task for task in supervisor._refresh_tasks.values() if task is not waiter
                )), 5)
                assert supervisor._discovery_reservations._value == 1
                assert supervisor._host_operation_lane._value == MAXIMUM_MCP_HOST_IN_FLIGHT
            finally:
                release_refresh.set()
                if runtime:
                    runtime.release()
                await supervisor.aclose()
            assert not bus._listeners

    asyncio.run(scenario())
