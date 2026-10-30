"""Host networking owns addresses; Pulsara never replays an HTTP operation."""
import asyncio

import httpx2
import pytest

from pulsara_agent.conversation_kernel.mcp.sdk_facade import _McpHttpClient
from pulsara_agent.conversation_kernel.mcp.wire import DEFAULT_MCP_WIRE_BOUNDS
from pulsara_agent.mcp_config import StreamableHttpTransportConfig, _parse_server
from pulsara_agent.capability.mcp_management import config_to_entry
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary


@pytest.fixture
def anyio_backend():
    return "asyncio"


def client(handler):
    return _McpHttpClient(
        StreamableHttpTransportConfig(endpoint="https://mcp.example/mcp"),
        bounds=DEFAULT_MCP_WIRE_BOUNDS,
        credential_boundary=ProcessCredentialBoundary(),
        transport=httpx2.MockTransport(handler),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("host", ["mcp.example", "192.168.1.10", "198.18.0.59", "127.0.0.1", "[::1]"])
async def test_url_reaches_httpx_without_local_resolution_or_rewriting(monkeypatch, host):
    async def forbidden(*args, **kwargs):
        raise AssertionError("product-level DNS resolution must not run")

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", forbidden)
    seen = []
    def handle(request):
        seen.append(request)
        return httpx2.Response(200)

    url = f"https://{host}:9443/mcp?mode=test"
    async with client(handle) as connection:
        response = await connection.post(url, content=b"operation")
    assert response.status_code == 200
    assert len(seen) == 1
    assert str(seen[0].url) == url
    assert seen[0].headers["host"] == f"{host}:9443"
    assert "sni_hostname" not in seen[0].extensions
    assert seen[0].content == b"operation"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.ReadError, httpx2.WriteError, httpx2.ReadTimeout, httpx2.RemoteProtocolError])
async def test_product_does_not_replay_http_requests(failure):
    seen = []
    def handle(request):
        seen.append(request)
        raise failure("operation failed", request=request)

    async with client(handle) as connection:
        with pytest.raises(failure):
            await connection.post("https://mcp.example/mcp", content=b"operation")
    assert len(seen) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_http_result_is_returned_without_retry(status):
    seen = []
    def handle(request):
        seen.append(request)
        return httpx2.Response(status)

    async with client(handle) as connection:
        response = await connection.post("https://mcp.example/mcp", content=b"operation")
    assert response.status_code == status
    assert len(seen) == 1


@pytest.mark.anyio
async def test_redirect_cannot_downgrade_https():
    seen = []
    def handle(request):
        seen.append(request)
        return httpx2.Response(302, headers={"Location": "http://192.168.1.10/token"})

    async with client(handle) as connection:
        with pytest.raises(ValueError, match="requires HTTPS"):
            await connection.get("https://mcp.example/authorize", follow_redirects=True)
    assert len(seen) == 1


@pytest.mark.parametrize("kind", ["streamable_http", "sse"])
def test_network_policy_is_not_part_of_config_contract(kind):
    transport = {"type": kind, "endpoint": "https://192.168.1.10/mcp"}
    config = _parse_server("local", {"transport": transport})
    assert "network_policy" not in config_to_entry(config)["transport"]
    for removed in ("PUBLIC_ONLY", "ALLOW_PRIVATE"):
        with pytest.raises(ValueError, match="unknown"):
            _parse_server("local", {"transport": {**transport, "network_policy": removed}})
