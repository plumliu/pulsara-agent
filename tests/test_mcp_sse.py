from __future__ import annotations

import asyncio
import json

from aiohttp import web
import mcp_types as types
from mcp.shared.message import SessionMessage
import pytest

from pulsara_agent.mcp_config import _parse_server, LegacySseTransportConfig
from pulsara_agent.conversation_kernel.mcp.sdk_facade import _SdkHttpTransport
from pulsara_agent.conversation_kernel.mcp.wire import DEFAULT_MCP_WIRE_BOUNDS
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary


def test_explicit_sse_auth_message_correlation_and_close():
    async def scenario():
        messages = asyncio.Queue()
        finished = asyncio.Event()
        observed = []

        async def events(request):
            observed.append(("GET", request.headers.get("Authorization")))
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"event: endpoint\ndata: /messages\n\n")
            while True:
                data = await messages.get()
                if data is None:
                    break
                await response.write(
                    b"event: message\ndata: " + json.dumps(data).encode() + b"\n\n"
                )
            finished.set()
            return response

        async def post(request):
            observed.append(("POST", request.headers.get("Authorization")))
            message = await request.json()
            await messages.put(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"text": "echo test-sse-secret"},
                }
            )
            return web.Response(status=202)

        app = web.Application()
        app.router.add_get("/sse", events)
        app.router.add_post("/messages", post)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        import os

        prior = os.environ.get("TEST_SSE_CREDENTIAL")
        os.environ["TEST_SSE_CREDENTIAL"] = "test-sse-secret"
        config = _parse_server(
            "sse",
            {
                "transport": {
                    "type": "sse",
                    "endpoint": f"http://127.0.0.1:{port}/sse",
                    "allow_http_localhost": True,
                },
                "auth": {
                    "type": "bearer",
                    "reference": {
                        "source": "environment",
                        "name": "TEST_SSE_CREDENTIAL",
                    },
                },
            },
        )
        assert isinstance(config.transport, LegacySseTransportConfig)
        transport = _SdkHttpTransport(
            config,
            config.transport,
            credential_boundary=ProcessCredentialBoundary(),
            bounds=DEFAULT_MCP_WIRE_BOUNDS,
        )
        try:
            await asyncio.wait_for(transport.start(), 5)
            await transport.write_stream.send(
                SessionMessage(
                    types.jsonrpc_message_adapter.validate_python(
                        {"jsonrpc": "2.0", "id": 7, "method": "ping"}
                    )
                )
            )
            received = await asyncio.wait_for(transport.read_stream.receive(), 5)
            assert received.message.id == 7
            assert received.message.result == {"text": "echo [credential removed]"}
            assert transport.sessionful
            assert observed == [
                ("GET", "Bearer test-sse-secret"),
                ("POST", "Bearer test-sse-secret"),
            ]
            await transport.aclose()
            await transport.aclose()
            assert transport._secret_values == ()
        finally:
            await transport.aclose()
            await messages.put(None)
            await asyncio.wait_for(finished.wait(), 5)
            await runner.cleanup()
            if prior is None:
                os.environ.pop("TEST_SSE_CREDENTIAL", None)
            else:
                os.environ["TEST_SSE_CREDENTIAL"] = prior

    asyncio.run(scenario())


def test_sse_cannot_claim_stateless_parallel_transport():
    with pytest.raises(ValueError, match="sessionful"):
        _parse_server(
            "sse",
            {
                "transport": {
                    "type": "sse",
                    "endpoint": "https://example.org/sse",
                    "proved_stateless": True,
                }
            },
        )
