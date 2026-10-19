"""DNS pinning must preserve availability without replaying HTTP operations."""
import asyncio
import ipaddress
import socket

import httpx2
import pytest

from pulsara_agent.conversation_kernel.mcp.sdk_facade import _McpHttpClient
from pulsara_agent.conversation_kernel.mcp.wire import DEFAULT_MCP_WIRE_BOUNDS
from pulsara_agent.mcp_config import StreamableHttpTransportConfig
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def addresses(monkeypatch):
    values = ["93.184.216.34", "93.184.216.35"]
    calls = []

    async def resolve(self, host, port, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in values]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    return values, calls


def client(handler):
    return _McpHttpClient(
        StreamableHttpTransportConfig(endpoint="https://mcp.example/mcp"),
        bounds=DEFAULT_MCP_WIRE_BOUNDS,
        credential_boundary=ProcessCredentialBoundary(),
        transport=httpx2.MockTransport(handler),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [httpx2.ConnectError, httpx2.ConnectTimeout])
async def test_connect_failure_uses_other_validated_address(addresses, failure):
    seen = []

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            raise failure("connection did not open", request=request)
        return httpx2.Response(200, json={"ok": True})

    async with client(handle) as connection:
        response = await connection.post("https://mcp.example/mcp?mode=test", content=b"operation")
    assert response.status_code == 200
    assert str(response.request.url) == "https://mcp.example/mcp?mode=test"
    assert [r.url.host for r in seen] == addresses[0]
    assert addresses[1] == ["mcp.example"]
    assert all(r.headers["host"] == "mcp.example" for r in seen)
    assert all(r.extensions["sni_hostname"] == "mcp.example" for r in seen)
    assert all(r.content == b"operation" for r in seen)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [httpx2.ReadError, httpx2.WriteError, httpx2.ReadTimeout, httpx2.RemoteProtocolError])
async def test_possible_transmission_is_never_replayed(addresses, failure):
    seen = []

    def handle(request):
        seen.append(request)
        raise failure("operation may have reached server", request=request)

    async with client(handle) as connection:
        with pytest.raises(failure):
            await connection.post("https://mcp.example/mcp", content=b"operation")
    assert len(seen) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_http_result_is_returned_without_another_address(addresses, status):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx2.Response(status)

    async with client(handle) as connection:
        response = await connection.post("https://mcp.example/mcp", content=b"operation")
    assert response.status_code == status
    assert len(seen) == 1


@pytest.mark.anyio
async def test_mixed_public_private_dns_answer_blocks_all_candidates(addresses, monkeypatch):
    addresses[0].append("127.0.0.1")
    async def mixed_public_recovery(*args, **kwargs):
        return frozenset(ipaddress.ip_address(value) for value in addresses[0])
    monkeypatch.setattr("pulsara_agent.conversation_kernel.mcp.sdk_facade._resolve_public_http_addresses", mixed_public_recovery)
    seen = []

    def handle(request):
        seen.append(request)
        return httpx2.Response(200)

    async with client(handle) as connection:
        with pytest.raises(ValueError, match="PRIVATE_NETWORK_DENIED"):
            await connection.get("https://mcp.example/mcp")
    assert seen == []


@pytest.mark.anyio
async def test_exhausted_addresses_propagate_connect_failure(addresses):
    seen = []

    def handle(request):
        seen.append(request.url.host)
        raise httpx2.ConnectError("unreachable", request=request)

    async with client(handle) as connection:
        with pytest.raises(httpx2.ConnectError):
            await connection.get("https://mcp.example/mcp")
    assert seen == addresses[0]
    assert addresses[1] == ["mcp.example"]
