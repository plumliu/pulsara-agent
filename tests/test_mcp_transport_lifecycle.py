"""Physical shutdown and EOF through the real SDK and local MCP peers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import signal
import sys

from aiohttp import web
import httpx2
import mcp_types as types
import pytest

from pulsara_agent.conversation_kernel.mcp.sdk_facade import BoundedMcpSdkClient, MCPError
from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary


@asynccontextmanager
async def _local_server(app):
    runner = web.AppRunner(app, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    finally:
        await runner.cleanup()


def _client(tmp_path: Path, transport, callback=None):
    async def ignore_notification(_method):
        pass

    return BoundedMcpSdkClient(
        _parse_server("lifecycle", {"transport": transport}),
        workspace_root=tmp_path,
        notification_callback=callback or ignore_notification,
        credential_boundary=ProcessCredentialBoundary(),
    )


def _legacy_reply(message):
    if message["method"] == "server/discover":
        return {
            "jsonrpc": "2.0", "id": message["id"],
            "error": {"code": -32601, "message": "legacy peer"},
        }
    assert message["method"] == "initialize"
    return {
        "jsonrpc": "2.0", "id": message["id"],
        "result": {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "lifecycle", "version": "1"},
        },
    }


@pytest.mark.parametrize("ignore_term", [False, True])
def test_stdio_close_terminates_peer_with_blocked_stdin(tmp_path, ignore_term):
    async def scenario():
        # The real peer negotiates, then stops reading. A valid large tool
        # request fills its pipe while shutdown must still terminate it.
        script = """
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if request['method'] == 'server/discover':
        reply = {'error': {'code': -32601, 'message': 'legacy peer'}}
    elif request['method'] == 'initialize':
        reply = {'result': {'protocolVersion': '2025-11-25',
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'blocked-stdin', 'version': '1'}}}
    else:
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], **reply}), flush=True)
    if request['method'] == 'initialize':
        time.sleep(3600)
"""
        if ignore_term:
            script = (
                "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                + script
            )
        client = _client(tmp_path, {
            "type": "stdio", "command": sys.executable, "args": ["-c", script],
        })
        await asyncio.wait_for(client.open(), 5)
        process = client._transport._process
        call = asyncio.create_task(client.session.call_tool(
            "probe", {"value": "x" * 1_000_000},
        ))
        close = None
        try:
            async with asyncio.timeout(5):  # Test hang detector, not a call limit.
                while process.stdin.transport.get_write_buffer_size() < 65_536:
                    await asyncio.sleep(0.01)
            close = asyncio.create_task(client.aclose())
            await asyncio.wait_for(asyncio.shield(close), 4)
            assert process.returncode == -(
                signal.SIGKILL if ignore_term else signal.SIGTERM
            )
            assert all(task.done() for task in client._transport._tasks)
            with pytest.raises(MCPError) as caught:
                await asyncio.wait_for(call, 1)
            assert caught.value.code == types.CONNECTION_CLOSED
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
            await client.aclose()
            if close is not None:
                await close
            await asyncio.gather(call, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["network_reset", "invalid_json"])
def test_sse_receive_failure_keeps_network_and_protocol_classification(tmp_path, failure):
    async def scenario():
        queues = []
        connections = []

        async def events(request):
            messages = asyncio.Queue()
            queues.append(messages)
            connections.append(request.transport)
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"event: endpoint\ndata: /messages\n\n")
            while (message := await messages.get()) is not None:
                data = message if isinstance(message, bytes) else json.dumps(message).encode()
                await response.write(b"event: message\ndata: " + data + b"\n\n")
            return response

        async def post(request):
            message = await request.json()
            if "id" in message:
                reply = (
                    {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}}
                    if message["method"] == "tools/list" else _legacy_reply(message)
                )
                await queues[-1].put(reply)
            return web.Response(status=202)

        app = web.Application()
        app.router.add_get("/sse", events)
        app.router.add_post("/messages", post)
        async with _local_server(app) as base:
            config = _parse_server("failure", {"transport": {
                "type": "sse", "endpoint": base + "/sse", "allow_http_localhost": True,
            }})
            supervisor = McpHostSupervisor(
                session_id="session:receive-failure", workspace_root=tmp_path,
                configs=(config,), credential_boundary=ProcessCredentialBoundary(),
            )
            try:
                await supervisor.start()
                await asyncio.wait_for(supervisor._tasks["failure"], 3)
                original = supervisor._slots["failure"]
                if failure == "network_reset":
                    connections[0].abort()
                    async with asyncio.timeout(5):
                        while (supervisor._slots.get("failure") is None
                               or supervisor._slots["failure"] is original):
                            await asyncio.sleep(0.01)
                    assert len(connections) == 2
                    assert supervisor._state["failure"].value == "READY"
                    assert original.client._closed
                else:
                    await queues[0].put(b"not-json")
                    async with asyncio.timeout(3):
                        while supervisor._state["failure"].value == "READY":
                            await asyncio.sleep(0.01)
                    assert supervisor._state["failure"].value == "FAILED_TERMINAL"
                    assert "failure" not in supervisor._retry_tasks
                    assert len(connections) == 1
            finally:
                await supervisor.aclose()
                for messages in queues:
                    await messages.put(None)

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", ["reconnect", "reload"])
def test_shutdown_joins_replaced_connect_attempt_cleanup(tmp_path, replacement):
    async def scenario():
        catalog_started = asyncio.Event()
        deleting_old = asyncio.Event()
        release = asyncio.Event()
        initialized = 0

        async def endpoint(request):
            nonlocal initialized
            if request.method == "GET":
                return web.Response(status=405)
            if request.method == "DELETE":
                if request.headers["mcp-session-id"] == "session-1":
                    deleting_old.set()
                    await release.wait()
                return web.Response(status=204)
            message = await request.json()
            if "id" not in message:
                return web.Response(status=202)
            headers = {}
            if message["method"] == "tools/list":
                catalog_started.set()
                await release.wait()
                reply = {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}}
            else:
                reply = _legacy_reply(message)
                if message["method"] == "initialize":
                    initialized += 1
                    headers["mcp-session-id"] = f"session-{initialized}"
                    if initialized > 1:
                        reply["result"]["capabilities"] = {}
            return web.json_response(reply, headers=headers)

        app = web.Application()
        app.router.add_route("*", "/mcp", endpoint)
        async with _local_server(app) as base:
            config = _parse_server("replacement", {"required": False, "transport": {
                "type": "streamable_http", "endpoint": base + "/mcp",
                "allow_http_localhost": True,
            }})
            supervisor = McpHostSupervisor(
                session_id="session:replacement", workspace_root=tmp_path,
                configs=(config,), credential_boundary=ProcessCredentialBoundary(),
            )
            close = None
            try:
                await supervisor.start()
                await asyncio.wait_for(catalog_started.wait(), 3)
                original = supervisor._tasks["replacement"]
                if replacement == "reconnect":
                    supervisor.reconnect("replacement")
                else:
                    supervisor.reload_configs(())
                await asyncio.wait_for(deleting_old.wait(), 3)
                assert original in supervisor._connection_tasks
                owners = [task for task in asyncio.all_tasks()
                          if task.get_name().startswith(("mcp-connect", "mcp-sdk"))]
                close = asyncio.create_task(supervisor.aclose())
                await asyncio.sleep(0.05)
                assert not close.done()
                assert not original.done()
                # Re-cancelling the old attempt during cleanup must not detach
                # its already-running physical close from supervisor ownership.
                original.cancel()
                await asyncio.sleep(0.01)
                assert not original.done()
                release.set()
                await asyncio.wait_for(close, 3)
                assert original.done()
                assert not supervisor._connection_tasks
                assert all(task.done() for task in owners)
            finally:
                release.set()
                if close is not None:
                    await close
                await supervisor.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("delete_response", ["success", "no_headers", "body_stalls"])
def test_sessionful_http_close_settles_sdk_delete(tmp_path, delete_response):
    async def scenario():
        delete_seen = asyncio.Event()
        release = asyncio.Event()
        methods = []

        async def endpoint(request):
            methods.append(request.method)
            if request.method == "DELETE":
                assert request.headers["mcp-session-id"] == "test-session"
                delete_seen.set()
                if delete_response == "success":
                    return web.Response(status=204)
                if delete_response == "body_stalls":
                    response = web.StreamResponse(headers={
                        "Content-Type": "application/json",
                        "Content-Length": "100",
                    })
                    await response.prepare(request)
                    await response.write(b"{")
                await release.wait()
                return web.Response(status=204)
            if request.method == "GET":
                return web.Response(status=405)
            message = await request.json()
            if "id" not in message:
                return web.Response(status=202)
            return web.json_response(
                _legacy_reply(message),
                headers={"mcp-session-id": "test-session"}
                if message["method"] == "initialize" else {},
            )

        app = web.Application()
        app.router.add_route("*", "/mcp", endpoint)
        async with _local_server(app) as base:
            client = _client(tmp_path, {
                "type": "streamable_http", "endpoint": base + "/mcp",
                "allow_http_localhost": True,
            })
            await asyncio.wait_for(client.open(), 5)
            physical = client._transport
            # Supported client configuration shortens the existing control I/O
            # budget for this test; actual HTTP and SDK termination still run.
            physical._client.timeout = httpx2.Timeout(0.05, read=None)
            try:
                await asyncio.wait_for(client.aclose(), 2)
                assert delete_seen.is_set()
                assert methods.count("DELETE") == 1
                assert physical._owner.done()
                assert physical._client.is_closed
                assert physical._client.timeout.read is None
            finally:
                release.set()
                await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("during_call", [False, True])
def test_legacy_sse_eof_settles_public_session(tmp_path, during_call):
    async def scenario():
        messages = asyncio.Queue()
        call_seen = asyncio.Event()
        failed = asyncio.Event()
        notifications = []
        calls = 0

        async def events(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"event: endpoint\ndata: /messages\n\n")
            while (message := await messages.get()) is not None:
                await response.write(
                    b"event: message\ndata: " + json.dumps(message).encode() + b"\n\n"
                )
            return response  # Normal HTTP/SSE EOF, without a synthetic error.

        async def post(request):
            nonlocal calls
            message = await request.json()
            if message["method"] == "tools/call":
                calls += 1
                call_seen.set()
            elif "id" in message:
                await messages.put(_legacy_reply(message))
            return web.Response(status=202)

        async def notify(method):
            notifications.append(method)
            if method == "pulsara/transport_failure":
                failed.set()

        app = web.Application()
        app.router.add_get("/sse", events)
        app.router.add_post("/messages", post)
        async with _local_server(app) as base:
            client = _client(tmp_path, {
                "type": "sse", "endpoint": base + "/sse",
                "allow_http_localhost": True,
            }, notify)
            await asyncio.wait_for(client.open(), 5)
            call = None
            try:
                if during_call:
                    call = asyncio.create_task(client.session.call_tool("probe", {}))
                    await asyncio.wait_for(call_seen.wait(), 2)
                await messages.put(None)
                await asyncio.wait_for(failed.wait(), 2)
                if call is None:
                    call = asyncio.create_task(client.session.call_tool("probe", {}))
                with pytest.raises(MCPError) as caught:
                    await asyncio.wait_for(call, 2)
                assert caught.value.code == types.CONNECTION_CLOSED
                await asyncio.wait_for(asyncio.shield(client._transport._owner), 2)
                assert calls == int(during_call)
                assert notifications == ["pulsara/transport_failure"]
            finally:
                await messages.put(None)
                await client.aclose()
                if call is not None:
                    await asyncio.gather(call, return_exceptions=True)

    asyncio.run(scenario())
