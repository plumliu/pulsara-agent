"""SDK 2.x integration experiments, not an activation of the new transport.

The helpers below inject only HTTP policy/byte streams, never MCP/SSE parsing.
Gap tests deliberately characterize behavior that is NOT acceptance evidence.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import gzip
import json
import socket
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx2
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client
from mcp.shared.message import SessionMessage
import mcp_types as types
import pytest

from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.conversation_kernel.mcp.sdk_facade import (
    _enforce_http_network_policy,
    _McpHttpClient,
)
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.conversation_kernel.mcp.wire import (
    DEFAULT_MCP_WIRE_BOUNDS,
    McpWireBoundExceeded,
    validate_json_shape,
)


class Chunks(httpx2.AsyncByteStream):
    def __init__(self, chunks, *, wait=False):
        self.chunks, self.wait = chunks, wait
        self.closed = False
        self.started = asyncio.Event()
        self.cancelled = False

    async def __aiter__(self):
        self.started.set()
        try:
            for chunk in self.chunks:
                yield chunk
            if self.wait:
                await anyio.sleep_forever()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.closed = True


class SizedBody(httpx2.AsyncByteStream):
    """Plain response byte limit; this does not claim a decoded/shape limit."""

    def __init__(self, stream, maximum):
        self.stream, self.maximum = stream, maximum

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > self.maximum:
                raise httpx2.StreamError("probe response byte limit")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class PolicyTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner, config, *, body_limit=None):
        self.inner, self.config, self.body_limit = inner, config, body_limit
        self.checked = []
        self.closed = False

    async def handle_async_request(self, request):
        self.checked.append(str(request.url))
        pinned = await _enforce_http_network_policy(
            replace(self.config, endpoint=str(request.url))
        )
        headers = dict(request.headers)
        headers["host"] = pinned.host_header
        physical = httpx2.Request(
            request.method,
            pinned.url,
            headers=headers,
            content=await request.aread(),
            extensions={
                **request.extensions,
                **(
                    {"sni_hostname": pinned.sni_hostname} if pinned.sni_hostname else {}
                ),
            },
        )
        response = await self.inner.handle_async_request(physical)
        if self.body_limit is not None and response.headers.get(
            "content-type", ""
        ).startswith("application/json"):
            response.stream = SizedBody(response.stream, self.body_limit)
        return response

    async def aclose(self):
        self.closed = True
        await self.inner.aclose()


def config():
    return _parse_server(
        "probe",
        {
            "transport": {
                "type": "streamable_http",
                "endpoint": "https://public.example/mcp",
            }
        },
    ).transport


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def dns(monkeypatch):
    async def addresses(self, host, port, **kwargs):
        address = (
            "127.0.0.1" if host in {"private.example", "127.0.0.1"} else "93.184.216.34"
        )
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", addresses)


def call():
    return SessionMessage(
        types.JSONRPCRequest(
            jsonrpc="2.0",
            id=7,
            method="tools/call",
            params={"name": "probe", "arguments": {}},
        )
    )


def result(value):
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"content": [{"type": "text", "text": value}]},
    }


async def exchange(client):
    with anyio.fail_after(3):  # Test hang detector, not a production call lifetime.
        async with streamable_http_client(
            "https://public.example/mcp", http_client=client
        ) as (read, write):
            await write.send(call())
            return await read.receive()


@pytest.mark.anyio
async def test_sdk_custom_client_network_pin_and_body_limit(dns):
    sent = []
    stream = Chunks([json.dumps(result("x" * 100)).encode()])

    async def respond(request):
        sent.append(request)
        return httpx2.Response(
            200, headers={"content-type": "application/json"}, stream=stream
        )

    policy = PolicyTransport(httpx2.MockTransport(respond), config(), body_limit=64)
    async with httpx2.AsyncClient(transport=policy, trust_env=False) as client:
        reply = await exchange(client)
        assert isinstance(reply.message, types.JSONRPCError)
        assert "probe response byte limit" in reply.message.error.message
        assert len(sent) == 1
        assert sent[0].url.host == "93.184.216.34"
        assert sent[0].headers["host"] == "public.example"
        assert sent[0].extensions["sni_hostname"] == "public.example"
        assert not client.is_closed  # Injected client remains caller-owned.
    assert policy.closed and stream.closed


@pytest.mark.anyio
async def test_sdk_checks_redirect_destination_before_connecting(dns):
    sent = []

    async def respond(request):
        sent.append(request)
        return httpx2.Response(307, headers={"location": "https://private.example/mcp"})

    policy = PolicyTransport(httpx2.MockTransport(respond), config())
    async with httpx2.AsyncClient(
        transport=policy, follow_redirects=True, trust_env=False
    ) as client:
        # The SDK propagates this policy ValueError through its task group;
        # it does not convert arbitrary application policy errors to RPC errors.
        with pytest.raises(ExceptionGroup) as caught:
            await exchange(client)
        assert len(caught.value.exceptions) == 1
        assert type(caught.value.exceptions[0]) is ValueError
        assert str(caught.value.exceptions[0]) == "MCP_HTTP_PRIVATE_NETWORK_DENIED"
    assert policy.checked == [
        "https://public.example/mcp",
        "https://private.example/mcp",
    ]
    assert len(sent) == 1
    assert policy.closed


@pytest.mark.anyio
async def test_gap_follow_redirects_can_repeat_tool_post(dns):
    sent = []

    async def respond(request):
        sent.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/mcp":
            return httpx2.Response(307, headers={"location": "/other-mcp"})
        return httpx2.Response(200, json=result("redirected"))

    async with httpx2.AsyncClient(
        transport=PolicyTransport(httpx2.MockTransport(respond), config()),
        trust_env=False,
        follow_redirects=True,
    ) as client:
        reply = await exchange(client)
        assert reply.message.result == result("redirected")["result"]
    assert [path for path, _ in sent] == ["/mcp", "/other-mcp"]
    assert sent[0][1] == sent[1][1]
    assert sent[0][1]["method"] == "tools/call"


@pytest.mark.anyio
async def test_gap_raw_body_limit_does_not_bound_decompressed_json(dns):
    payload = json.dumps(result("x" * 4096)).encode()
    encoded = gzip.compress(payload)
    maximum = 256
    assert len(encoded) < maximum < len(payload)

    async def respond(request):
        return httpx2.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            stream=Chunks([encoded]),
        )

    async with httpx2.AsyncClient(
        transport=PolicyTransport(
            httpx2.MockTransport(respond), config(), body_limit=maximum
        ),
        trust_env=False,
    ) as client:
        reply = await exchange(client)
        assert reply.message.result == result("x" * 4096)["result"]


@pytest.mark.anyio
async def test_gap_byte_limit_alone_does_not_preserve_preparse_node_limit(dns):
    bounds = DEFAULT_MCP_WIRE_BOUNDS
    document = result("many nodes")
    document["result"]["structuredContent"] = {
        "items": [0] * bounds.maximum_wire_json_nodes
    }
    payload = json.dumps(document).encode()
    assert len(payload) < bounds.maximum_http_json_body_bytes

    async def respond(request):
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            stream=Chunks([payload]),
        )

    async with httpx2.AsyncClient(
        transport=PolicyTransport(
            httpx2.MockTransport(respond),
            config(),
            body_limit=bounds.maximum_http_json_body_bytes,
        ),
        trust_env=False,
    ) as client:
        reply = await exchange(client)
        assert reply.message.result == document["result"]
        with pytest.raises(McpWireBoundExceeded, match="node"):
            validate_json_shape(
                reply.message.result,
                maximum_nodes=bounds.maximum_wire_json_nodes,
                maximum_depth=bounds.maximum_wire_json_depth,
            )


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_sdk_without_auth_replay_does_not_repeat_tool_post(dns, status):
    sent = []

    async def respond(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(status)

    async with httpx2.AsyncClient(
        transport=PolicyTransport(httpx2.MockTransport(respond), config()),
        trust_env=False,
    ) as client:
        reply = await exchange(client)
        assert isinstance(reply.message, types.JSONRPCError)
    assert sent == [
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "probe", "arguments": {}},
        }
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 403])
async def test_gap_sdk_oauth_on_tool_client_repeats_tool_post(dns, status):
    """Do not attach automatic SDK auth to the tool transport in production."""

    class Storage:
        tokens = None
        client = None

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, tokens):
            self.tokens = tokens

        async def get_client_info(self):
            return self.client

        async def set_client_info(self, client):
            self.client = client

    authorization = {}
    sent = []
    base = "https://public.example"

    async def redirect(url):
        authorization.update(parse_qs(urlsplit(url).query))

    async def callback():
        return AuthorizationCodeResult(
            code="test-only-code", state=authorization["state"][0], iss=base
        )

    async def respond(request):
        if request.url.path == "/mcp":
            sent.append(json.loads(request.content))
            if len(sent) == 1:
                return httpx2.Response(
                    status,
                    headers={
                        "www-authenticate": f'Bearer resource_metadata="{base}/resource"'
                    },
                )
            return httpx2.Response(200, json=result("retried"))
        if request.url.path == "/resource":
            return httpx2.Response(
                200,
                json={"resource": base + "/mcp", "authorization_servers": [base]},
            )
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": base,
                    "authorization_endpoint": base + "/authorize",
                    "token_endpoint": base + "/token",
                    "registration_endpoint": base + "/register",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "grant_types_supported": ["authorization_code"],
                    "token_endpoint_auth_methods_supported": ["none"],
                },
            )
        if request.url.path == "/register":
            return httpx2.Response(
                201,
                json={**json.loads(request.content), "client_id": "test-only-client"},
            )
        if request.url.path == "/token":
            return httpx2.Response(
                200,
                json={"access_token": "test-only-token", "token_type": "Bearer"},
            )
        raise AssertionError(
            f"unexpected auth request: {request.method} {request.url.path}"
        )

    provider = OAuthClientProvider(
        base + "/mcp",
        OAuthClientMetadata(
            redirect_uris=["http://127.0.0.1:12345/callback"],
            grant_types=["authorization_code"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        Storage(),
        redirect_handler=redirect,
        callback_handler=callback,
    )
    async with httpx2.AsyncClient(
        transport=PolicyTransport(httpx2.MockTransport(respond), config()),
        auth=provider,
        trust_env=False,
    ) as client:
        reply = await exchange(client)
        assert reply.message.result == result("retried")["result"]
    assert len(sent) == 2
    assert sent[0] == sent[1]
    assert sent[0]["method"] == "tools/call"


@pytest.mark.anyio
async def test_sdk_cancel_closes_pending_stream_without_tool_replay(dns):
    stream = Chunks([], wait=True)
    sent = []

    async def respond(request):
        sent.append(request.method)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream
        )

    policy = PolicyTransport(httpx2.MockTransport(respond), config())
    async with httpx2.AsyncClient(transport=policy, trust_env=False) as client:
        async with anyio.create_task_group() as group:
            group.start_soon(exchange, client)
            await stream.started.wait()
            group.cancel_scope.cancel()
        assert stream.closed and stream.cancelled
    assert sent == ["POST"] and policy.closed


@pytest.mark.anyio
async def test_sdk_response_resumption_is_get_not_second_tool_post(dns):
    sent = []

    async def respond(request):
        sent.append((request.method, request.headers.get("last-event-id")))
        data = (
            b"id: event-1\nretry: 0\ndata: \n\n"
            if request.method == "POST"
            else b"data: " + json.dumps(result("resumed")).encode() + b"\n\n"
        )
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Chunks([data])
        )

    async with httpx2.AsyncClient(
        transport=PolicyTransport(httpx2.MockTransport(respond), config()),
        trust_env=False,
    ) as client:
        reply = await exchange(client)
        assert reply.message.result == result("resumed")["result"]
    assert sent == [("POST", None), ("GET", "event-1")]


@pytest.mark.anyio
async def test_gap_post_sse_ignores_custom_client_sse_limit(dns):
    """SDK-owned POST events stay at 1 MiB despite a larger client.sse limit."""
    requested_limit = 16 * 1024 * 1024

    class LargerSseClient(httpx2.AsyncClient):
        def sse(self, *args, **kwargs):
            return super().sse(
                *args,
                **kwargs,
                max_event_size=requested_limit,
            )

    payload = b"data: " + json.dumps(result("x" * (1024 * 1024 + 1))).encode() + b"\n\n"
    assert len(payload) < requested_limit

    async def respond(request):
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Chunks([payload])
        )

    async with LargerSseClient(
        transport=PolicyTransport(httpx2.MockTransport(respond), config()),
        trust_env=False,
    ) as client:
        async with client.sse("https://public.example/mcp") as events:
            assert len([event async for event in events]) == 1
        reply = await exchange(client)
        assert isinstance(reply.message, types.JSONRPCError)
        assert reply.message.error.message == "SSE stream ended without a response"


@pytest.mark.anyio
async def test_sdk_legacy_sse_uses_injected_client_for_endpoint_and_post(dns):
    queue = asyncio.Queue()
    sent = []

    class Events(Chunks):
        async def __aiter__(self):
            yield b"event: endpoint\ndata: /messages\n\n"
            yield b"event: message\ndata: " + await queue.get() + b"\n\n"
            await anyio.sleep_forever()

    events = Events([])

    async def respond(request):
        sent.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=events
            )
        await queue.put(json.dumps(result("legacy")).encode())
        return httpx2.Response(202)

    policy = PolicyTransport(httpx2.MockTransport(respond), config())

    @asynccontextmanager
    async def factory(**kwargs):
        async with httpx2.AsyncClient(
            transport=policy, trust_env=False, **kwargs
        ) as client:
            yield client

    with anyio.fail_after(3):
        async with sse_client(
            "https://public.example/sse", httpx_client_factory=factory
        ) as (read, write):
            await write.send(call())
            reply = await read.receive()
            assert reply.message.result == result("legacy")["result"]
    assert sent == [("GET", "/sse"), ("POST", "/messages")]
    assert policy.closed and events.closed


@pytest.mark.anyio
async def test_production_client_bounds_decoded_body_before_sdk(dns):
    payload = gzip.compress(json.dumps(result("x" * 4096)).encode())
    stream = Chunks([payload])

    async def respond(request):
        return httpx2.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            stream=stream,
        )

    async with _McpHttpClient(
        config(),
        bounds=replace(DEFAULT_MCP_WIRE_BOUNDS, maximum_http_json_body_bytes=256),
        credential_boundary=ProcessCredentialBoundary(),
        transport=httpx2.MockTransport(respond),
    ) as client:
        with pytest.raises(McpWireBoundExceeded, match="body"):
            await client.get("https://public.example/mcp")
    assert stream.closed


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_production_client_every_request_crosses_credential_gate(dns, method):
    sent = []

    async def respond(request):
        sent.append(request)
        return httpx2.Response(200, json=result("ok"))

    async with _McpHttpClient(
        config(),
        bounds=DEFAULT_MCP_WIRE_BOUNDS,
        credential_boundary=ProcessCredentialBoundary("protected-fixture-key"),
        transport=httpx2.MockTransport(respond),
    ) as client:
        with pytest.raises(ValueError, match="protected credential"):
            await client.request(
                method,
                "https://public.example/mcp",
                headers={"x-test": "protected-fixture-key"},
            )
    assert sent == []


@pytest.mark.anyio
async def test_production_tool_client_does_not_follow_post_redirect(dns):
    sent = []

    async def respond(request):
        sent.append(request.method)
        return httpx2.Response(307, headers={"location": "/another"})

    async with _McpHttpClient(
        config(),
        bounds=DEFAULT_MCP_WIRE_BOUNDS,
        credential_boundary=ProcessCredentialBoundary(),
        transport=httpx2.MockTransport(respond),
    ) as client:
        response = await client.post(
            "https://public.example/mcp", json={"method": "tools/call"}
        )
        assert response.status_code == 307
    assert sent == ["POST"]
