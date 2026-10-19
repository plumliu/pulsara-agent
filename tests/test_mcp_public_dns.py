"""Fake DNS answers with real DoH/TLS, HTTP routing and official MCP sessions.

Only DNS answers and the final TCP destination are injected. No TUN interface or
production network/settings are needed; protocol, TLS and request admission run.
"""
import asyncio
from dataclasses import replace
import socket

import dns.message
import dns.rcode
import dns.rdatatype
import dns.rrset
from httpcore2._backends.anyio import AnyIOBackend
import httpx2
import pytest
from starlette.responses import Response

from pulsara_agent.capability.mcp_management import LocalMcpManagementService
from pulsara_agent.conversation_kernel.mcp import sdk_facade
from pulsara_agent.conversation_kernel.mcp.sdk_facade import (
    BoundedMcpSdkClient, MCPError, _McpDohHttpClient, _McpHttpClient,
    _enforce_http_network_policy,
)
from pulsara_agent.conversation_kernel.mcp.supervisor import discover_mcp_catalog
from pulsara_agent.conversation_kernel.mcp.wire import DEFAULT_MCP_WIRE_BOUNDS
from pulsara_agent.mcp_config import McpHttpNetworkPolicy, StreamableHttpTransportConfig, _parse_server
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.settings import LocalSettingsStore
from tests.test_mcp_runtime_proxy import runtime_network as runtime_network, sdk_proxy_service


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_dns(runtime_network, monkeypatch):
    state = {"addresses": ["198.18.0.7"], "calls": []}
    original = asyncio.BaseEventLoop.getaddrinfo

    async def addresses(self, host, port, **kwargs):
        state["calls"].append(host)
        # Bootstrap must never resolve via the intercepted system resolver.
        assert host != "cloudflare-dns.com"
        if host.endswith(".example"):
            return [
                (socket.AF_INET6 if ":" in value else socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, port))
                for value in state["addresses"]
            ]
        return await original(self, host, port, **kwargs)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", addresses)
    return state


def route_public_tcp_to_fixture(monkeypatch, port):
    original = AnyIOBackend.connect_tcp
    calls = []

    async def connect(self, host, remote_port, **kwargs):
        calls.append((host, remote_port))
        if host in {"1.1.1.1", "93.184.216.34", "2001:4860:4860::8888"}:
            # Simulate the OS routing layer below HTTP and certificate checks.
            return await original(self, "127.0.0.1", port, **kwargs)
        assert host == "127.0.0.1"  # Local explicit proxy only; never Fake-IP.
        return await original(self, host, remote_port, **kwargs)

    monkeypatch.setattr(AnyIOBackend, "connect_tcp", connect)
    return calls


async def notify(_):
    pass


def wire_answer(query, v4=(), v6=(), *, cname=False, rcode=dns.rcode.NOERROR):
    response = dns.message.make_response(query)
    response.set_rcode(rcode)
    question = query.question[0]
    owner = "alias.example." if cname else question.name
    if cname:
        response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "CNAME", owner))
    values = v4 if question.rdtype == dns.rdatatype.A else v6
    if values:
        response.answer.append(dns.rrset.from_text(owner, 60, "IN", question.rdtype, *values))
    return Response(response.to_wire(), media_type="application/dns-message")


@pytest.mark.anyio
@pytest.mark.parametrize("fake", ["198.18.0.7", "fdfe:dcba:9876::7", "192.168.5.7"])
@pytest.mark.parametrize("route", ["proxy", "tun", "no_proxy", "sse"])
async def test_real_sdk_recovers_fake_dns_without_connecting_fake_ip(tmp_path, fake_dns, monkeypatch, fake, route):
    fake_dns["addresses"] = [fake]
    monkeypatch.setenv("MCP_DNS_TEST_SECRET", "synthetic-mcp-secret")
    async with sdk_proxy_service(tmp_path, monkeypatch, legacy_sse=route == "sse") as state:
        tcp = route_public_tcp_to_fixture(monkeypatch, state.port)
        if route == "tun":
            monkeypatch.delenv("HTTPS_PROXY")
        elif route == "no_proxy":
            monkeypatch.setenv("NO_PROXY", "mcp.example")
        config = _parse_server("fake-dns", {"transport": {
            "type": "sse" if route == "sse" else "streamable_http",
            "endpoint": "https://mcp.example/sse" if route == "sse" else "https://mcp.example/mcp",
        }, "auth": {"type": "bearer", "reference": {"source": "environment", "name": "MCP_DNS_TEST_SECRET"}}})
        client = BoundedMcpSdkClient(config, workspace_root=tmp_path, notification_callback=notify, credential_boundary=ProcessCredentialBoundary())
        try:
            await client.open()
            snapshot, _ = await discover_mcp_catalog(client, config)
            assert [tool.remote_tool_name for tool in snapshot.tools] == ["echo"]
            result = await client.session.call_tool("echo", {"text": "once"})
            assert not result.is_error and result.structured_content == {"result": "proxy:once"}
        finally:
            await client.aclose()
        assert state.echoes == ["once"]
        assert state.doh
        assert {str(query.question[0].name) for query, _ in state.doh} == {"mcp.example."}
        assert {query.question[0].rdtype for query, _ in state.doh} == {dns.rdatatype.A, dns.rdatatype.AAAA}
        assert all("authorization" not in headers and "cookie" not in headers for _, headers in state.doh)
        assert "cloudflare-dns.com" not in fake_dns["calls"]
        assert all(host != fake for host, _ in tcp)
        assert set(state.connects) <= {"1.1.1.1:443", "93.184.216.34:443"}
        if route == "tun":
            assert not state.connects
            assert {host for host, _ in tcp} == {"1.1.1.1", "93.184.216.34"}
        elif route == "no_proxy":
            assert set(state.connects) == {"1.1.1.1:443"}
            assert ("93.184.216.34", 443) in tcp
        else:
            assert set(state.connects) == {"1.1.1.1:443", "93.184.216.34:443"}


@pytest.mark.anyio
async def test_oauth_validation_metadata_and_token_share_fake_dns_recovery(tmp_path, fake_dns, monkeypatch):
    async with sdk_proxy_service(tmp_path, monkeypatch) as state:
        route_public_tcp_to_fixture(monkeypatch, state.port)
        service = LocalMcpManagementService(LocalSettingsStore(tmp_path / "settings.yaml"), user_config_path=tmp_path / "mcp.yaml")
        config = _parse_server("oauth-fake", {"transport": {"type": "streamable_http", "endpoint": "https://mcp.example/mcp"}})
        try:
            pinned = await service.oauth._validate_url(config, "https://oauth.example/metadata")
            assert pinned.urls == ("https://93.184.216.34/metadata",)
            metadata = await service.oauth._request(config, httpx2.Request("GET", "https://oauth.example/metadata"))
            token = await service.oauth._request(config, httpx2.Request("POST", "https://oauth.example/token", data={"code": "synthetic-oauth-code", "client_secret": "synthetic-client-secret"}))
            assert metadata.status_code == token.status_code == 200
        finally:
            await service.aclose()
        assert len(state.oauth) == 2
        assert b"synthetic-oauth-code" in state.oauth[1][3]
        assert all(str(query.question[0].name) == "oauth.example." for query, _ in state.doh)
        assert all("authorization" not in headers and "cookie" not in headers for _, headers in state.doh)
        assert all(b"synthetic" not in query.to_wire() for query, _ in state.doh)
        assert set(state.connects) == {"1.1.1.1:443", "93.184.216.34:443"}


@pytest.mark.anyio
async def test_doh_sdk_cname_and_ipv6_answers_are_pinned(tmp_path, fake_dns, monkeypatch):
    async def answer(query):
        return wire_answer(query, v4=["93.184.216.34"], v6=["2001:4860:4860::8888"], cname=True)
    async with sdk_proxy_service(tmp_path, monkeypatch, doh_handler=answer) as state:
        route_public_tcp_to_fixture(monkeypatch, state.port)
        pinned = await _enforce_http_network_policy(
            StreamableHttpTransportConfig(endpoint="https://mcp.example:9443/mcp?mode=test"), credential_boundary=ProcessCredentialBoundary(),
        )
        assert pinned.urls == ("https://93.184.216.34:9443/mcp?mode=test", "https://[2001:4860:4860::8888]:9443/mcp?mode=test")
        assert pinned.host_header == "mcp.example:9443" and pinned.sni_hostname == "mcp.example"


@pytest.mark.anyio
@pytest.mark.parametrize("v4,v6", [
    (["192.168.1.1"], []), (["198.18.0.8"], []),
    (["93.184.216.34", "127.0.0.1"], []),
    (["93.184.216.34"], ["fdfe:dcba:9876::1"]),
    ([], []),
])
async def test_untrusted_doh_answers_never_reach_application(tmp_path, fake_dns, monkeypatch, v4, v6):
    async def answer(query):
        return wire_answer(query, v4=v4, v6=v6)
    async with sdk_proxy_service(tmp_path, monkeypatch, doh_handler=answer) as state:
        route_public_tcp_to_fixture(monkeypatch, state.port)
        async with _McpHttpClient(StreamableHttpTransportConfig(endpoint="https://mcp.example/mcp"), bounds=DEFAULT_MCP_WIRE_BOUNDS, credential_boundary=ProcessCredentialBoundary()) as client:
            error = ValueError if v4 or v6 else RuntimeError
            code = "MCP_HTTP_PRIVATE_NETWORK_DENIED" if v4 or v6 else "MCP_HTTP_PUBLIC_DNS_UNAVAILABLE"
            with pytest.raises(error, match=code):
                await client.post("https://mcp.example/mcp", content=b"application operation")
        assert not state.echoes and not state.oauth
        assert set(state.connects) == {"1.1.1.1:443"}


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["nxdomain", "bad_question", "redirect", "oversized_sse", "bad_tls"])
async def test_doh_failure_never_downgrades_to_fake_ip(tmp_path, fake_dns, monkeypatch, mode):
    async def answer(query):
        if mode == "nxdomain":
            return wire_answer(query, rcode=dns.rcode.NXDOMAIN)
        if mode == "bad_question":
            return wire_answer(dns.message.make_query("different.example.", "A"), v4=["93.184.216.34"])
        if mode == "redirect":
            return Response(status_code=307, headers={"Location": "https://oauth.example/token"})
        if mode == "oversized_sse":
            return Response(b"x" * 65536, media_type="text/event-stream")
        return wire_answer(query, v4=["93.184.216.34"])
    async with sdk_proxy_service(tmp_path, monkeypatch, doh_handler=answer) as state:
        route_public_tcp_to_fixture(monkeypatch, state.port)
        if mode == "bad_tls":
            # Keep the actual TLS check on: fixture cert is not valid for this
            # alternate logical authority even though bootstrap reaches it.
            monkeypatch.setattr(sdk_facade, "_MCP_DOH_URL", "https://wrong.example/dns-query")
        async with _McpHttpClient(StreamableHttpTransportConfig(endpoint="https://mcp.example/mcp"), bounds=DEFAULT_MCP_WIRE_BOUNDS, credential_boundary=ProcessCredentialBoundary()) as client:
            with pytest.raises(RuntimeError, match="MCP_HTTP_(PUBLIC_DNS_UNAVAILABLE|DNS_TIMEOUT)"):
                await client.post("https://mcp.example/mcp", content=b"application operation")
        assert not state.echoes and not state.oauth
        assert set(state.connects) == {"1.1.1.1:443"}
        if mode == "bad_tls":
            assert not state.doh


@pytest.mark.anyio
@pytest.mark.parametrize("host", ["198.18.0.7", "192.168.1.1", "127.0.0.1", "[::1]", "printer", "localhost", "a.localhost", "a.local", "a.lan", "a.internal", "a.home.arpa", "a.test", "a.invalid", "a.onion"])
async def test_local_names_and_ip_literals_are_never_sent_to_public_dns(monkeypatch, host):
    async def local(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443))]
    async def unexpected(*args, **kwargs):
        pytest.fail("local target leaked into public DNS")
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", local)
    monkeypatch.setattr(sdk_facade, "_resolve_public_http_addresses", unexpected)
    with pytest.raises(ValueError, match="PRIVATE_NETWORK_DENIED"):
        await _enforce_http_network_policy(StreamableHttpTransportConfig(endpoint=f"https://{host}/mcp"), credential_boundary=ProcessCredentialBoundary())


@pytest.mark.anyio
async def test_explicit_private_policy_and_normal_public_dns_do_not_use_recovery(fake_dns, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("unnecessary public DNS recovery")
    monkeypatch.setattr(sdk_facade, "_resolve_public_http_addresses", unexpected)
    transport = StreamableHttpTransportConfig(endpoint="https://mcp.example/mcp", network_policy=McpHttpNetworkPolicy.ALLOW_PRIVATE)
    private = await _enforce_http_network_policy(transport, credential_boundary=ProcessCredentialBoundary())
    assert private.urls == ("https://198.18.0.7/mcp",)
    fake_dns["addresses"] = ["93.184.216.34"]
    public = await _enforce_http_network_policy(replace(transport, network_policy=McpHttpNetworkPolicy.PUBLIC_ONLY), credential_boundary=ProcessCredentialBoundary())
    assert public.urls == ("https://93.184.216.34/mcp",)


@pytest.mark.anyio
async def test_doh_bootstrap_cannot_redirect_or_recurse(runtime_network):
    async with _McpDohHttpClient(StreamableHttpTransportConfig(endpoint=sdk_facade._MCP_DOH_URL), bounds=DEFAULT_MCP_WIRE_BOUNDS, credential_boundary=ProcessCredentialBoundary()) as client:
        with pytest.raises(ValueError, match="DOH_DESTINATION_CHANGED"):
            await client.get("https://mcp.example/dns-query")


@pytest.mark.anyio
async def test_dns_recovery_cancellation_closes_without_application_request(tmp_path, fake_dns, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def answer(query):
        started.set()
        await release.wait()
        return wire_answer(query, v4=["93.184.216.34"])
    async with sdk_proxy_service(tmp_path, monkeypatch, doh_handler=answer) as state:
        route_public_tcp_to_fixture(monkeypatch, state.port)
        try:
            async with _McpHttpClient(StreamableHttpTransportConfig(endpoint="https://mcp.example/mcp"), bounds=DEFAULT_MCP_WIRE_BOUNDS, credential_boundary=ProcessCredentialBoundary()) as client:
                request = asyncio.create_task(client.get("https://mcp.example/mcp"))
                await asyncio.wait_for(started.wait(), 3)  # Test hang detector.
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
        finally:
            release.set()
        assert not state.echoes and not state.oauth
        assert set(state.connects) == {"1.1.1.1:443"}


@pytest.mark.anyio
async def test_process_credential_rotation_during_public_resolution_blocks_application(tmp_path, fake_dns, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def answer(query):
        started.set()
        await release.wait()
        return wire_answer(query, v4=["93.184.216.34"])
    async with sdk_proxy_service(tmp_path, monkeypatch, doh_handler=answer) as state:
        route_public_tcp_to_fixture(monkeypatch, state.port)
        boundary = ProcessCredentialBoundary("old-protected-secret")
        try:
            async with _McpHttpClient(StreamableHttpTransportConfig(endpoint="https://oauth.example/token"), bounds=DEFAULT_MCP_WIRE_BOUNDS, credential_boundary=boundary) as client:
                request = asyncio.create_task(client.post("https://oauth.example/token", data={"client_secret": "next-protected-secret"}))
                await asyncio.wait_for(started.wait(), 3)
                await boundary.rotate_async("next-protected-secret")
                release.set()
                with pytest.raises(ValueError, match="protected"):
                    await request
        finally:
            release.set()
        assert not state.oauth
        assert set(state.connects) == {"1.1.1.1:443"}


@pytest.mark.anyio
async def test_changed_system_dns_is_not_used_after_public_validation(tmp_path, fake_dns, monkeypatch):
    async def answer(query):
        fake_dns["addresses"] = ["127.0.0.1"]
        return wire_answer(query, v4=["93.184.216.34"])
    async with sdk_proxy_service(tmp_path, monkeypatch, doh_handler=answer) as state:
        tcp = route_public_tcp_to_fixture(monkeypatch, state.port)
        monkeypatch.delenv("HTTPS_PROXY")
        async with _McpHttpClient(StreamableHttpTransportConfig(endpoint="https://oauth.example/metadata"), bounds=DEFAULT_MCP_WIRE_BOUNDS, credential_boundary=ProcessCredentialBoundary()) as client:
            response = await client.get("https://oauth.example/metadata")
        assert response.status_code == 200 and len(state.oauth) == 1
        assert fake_dns["calls"].count("oauth.example") == 1
        assert {host for host, _ in tcp} == {"1.1.1.1", "93.184.216.34"}


@pytest.mark.anyio
async def test_real_sdk_does_not_replay_applied_tool_after_read_failure(tmp_path, fake_dns, monkeypatch):
    async with sdk_proxy_service(tmp_path, monkeypatch) as state:
        route_public_tcp_to_fixture(monkeypatch, state.port)
        original = _McpHttpClient._send_single_request
        async def lose_response(self, request):
            response = await original(self, request)
            # Inject loss after the real SDK server has executed the operation.
            if request.url.path == "/mcp" and b'"tools/call"' in request.content:
                raise httpx2.ReadError("synthetic response loss", request=request)
            return response
        monkeypatch.setattr(_McpHttpClient, "_send_single_request", lose_response)
        config = _parse_server("no-replay", {"transport": {"type": "streamable_http", "endpoint": "https://mcp.example/mcp"}})
        client = BoundedMcpSdkClient(config, workspace_root=tmp_path, notification_callback=notify, credential_boundary=ProcessCredentialBoundary())
        try:
            await client.open()
            await discover_mcp_catalog(client, config)
            with pytest.raises(MCPError):
                await client.session.call_tool("echo", {"text": "once"})
        finally:
            await client.aclose()
        assert state.echoes == ["once"]
