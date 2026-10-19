"""Runtime proxy selection through the production HTTP owner and real SDK."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import ipaddress
import socket
import ssl
from types import SimpleNamespace
from urllib.request import getproxies_environment

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import dns.message
import dns.rdatatype
import dns.rrset
import httpx2
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from pulsara_agent.capability.mcp_management import LocalMcpManagementService
from pulsara_agent.conversation_kernel.mcp.sdk_facade import (
    BoundedMcpSdkClient,
    _McpHttpClient,
)
from pulsara_agent.conversation_kernel.mcp.supervisor import discover_mcp_catalog
from pulsara_agent.conversation_kernel.mcp.wire import DEFAULT_MCP_WIRE_BOUNDS
from pulsara_agent.mcp_config import StreamableHttpTransportConfig, _parse_server
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.settings import LocalSettingsStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def runtime_network(monkeypatch):
    # Use the dependency's actual environment parser, isolated from host proxy
    # settings. A separate case covers its system-proxy discovery entry point.
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr("httpx2._utils.getproxies", getproxies_environment)
    resolve = asyncio.BaseEventLoop.getaddrinfo

    async def addresses(self, host, port, **kwargs):
        if host.endswith(".example"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return await resolve(self, host, port, **kwargs)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", addresses)


def http_client(**kwargs):
    return _McpHttpClient(
        StreamableHttpTransportConfig(endpoint="https://mcp.example/mcp"),
        bounds=DEFAULT_MCP_WIRE_BOUNDS,
        credential_boundary=kwargs.pop("credential_boundary", ProcessCredentialBoundary()),
        **kwargs,
    )


def recorded_transports(monkeypatch, handler=None):
    calls = []

    def transport(label):
        async def respond(request):
            calls.append((label, request))
            if handler is not None:
                return await handler(label, request)
            return httpx2.Response(200, json={"ok": True})
        return httpx2.MockTransport(respond)

    monkeypatch.setattr(httpx2.AsyncClient, "_init_transport", lambda *a, **k: transport("direct"))
    monkeypatch.setattr(httpx2.AsyncClient, "_init_proxy_transport", lambda *a, **k: transport("proxy"))
    return calls


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["environment", "system", "no_proxy", "none"])
async def test_runtime_selects_against_logical_hostname(runtime_network, monkeypatch, mode):
    if mode == "system":
        monkeypatch.setattr("httpx2._utils.getproxies", lambda: {"https": "http://127.0.0.1:9876"})
    elif mode != "none":
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9876")
    if mode == "no_proxy":
        monkeypatch.setenv("NO_PROXY", "mcp.example")
    calls = recorded_transports(monkeypatch)
    async with http_client() as client:
        await client.get("https://mcp.example/mcp")
    assert [label for label, _ in calls] == ["direct" if mode in {"none", "no_proxy"} else "proxy"]
    assert calls[0][1].url.host == "93.184.216.34"
    assert calls[0][1].headers["host"] == "mcp.example"
    assert calls[0][1].extensions["sni_hostname"] == "mcp.example"


@pytest.mark.anyio
async def test_loopback_stays_local_without_no_proxy(runtime_network, monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9876")
    calls = recorded_transports(monkeypatch)
    async with _McpHttpClient(
        StreamableHttpTransportConfig(endpoint="http://localhost:17839/callback", allow_http_localhost=True),
        bounds=DEFAULT_MCP_WIRE_BOUNDS, credential_boundary=ProcessCredentialBoundary(),
    ) as client:
        await client.get("http://localhost:17839/callback")
    assert len(calls) == 1 and calls[0][0] == "direct"
    assert ipaddress.ip_address(calls[0][1].url.host).is_loopback


@pytest.mark.anyio
async def test_redirect_and_concurrent_requests_keep_their_own_proxy_target(runtime_network, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9876")
    monkeypatch.setenv("NO_PROXY", "direct.example")

    async def respond(label, request):
        await asyncio.sleep(0)
        if request.url.path == "/redirect":
            return httpx2.Response(302, headers={"Location": "https://direct.example/result"})
        return httpx2.Response(200, json={"ok": True})

    calls = recorded_transports(monkeypatch, respond)
    async with http_client(follow_redirects=True) as client:
        await asyncio.gather(client.get("https://mcp.example/redirect"), client.get("https://direct.example/other"))
    assert {(label, req.headers["host"], req.url.path) for label, req in calls} == {
        ("proxy", "mcp.example", "/redirect"), ("direct", "direct.example", "/result"),
        ("direct", "direct.example", "/other"),
    }


@pytest.mark.anyio
async def test_proxy_failure_never_falls_back_to_direct(runtime_network, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9876")

    async def fail(label, request):
        raise httpx2.ConnectError("proxy unreachable", request=request)

    calls = recorded_transports(monkeypatch, fail)
    async with http_client() as client:
        with pytest.raises(httpx2.ConnectError):
            await client.post("https://mcp.example/mcp", content=b"application operation")
    assert len(calls) == 1 and calls[0][0] == "proxy"


@pytest.mark.anyio
async def test_proxy_credentials_join_current_process_gate(runtime_network, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-user:next-process-secret@127.0.0.1:9876")
    calls = recorded_transports(monkeypatch)
    observed = []
    boundary = ProcessCredentialBoundary("old-process-secret")
    async with http_client(credential_boundary=boundary, observe_secrets=observed.extend) as client:
        await client.get("https://mcp.example/mcp")
        await boundary.rotate_async("next-process-secret")
        with pytest.raises(ValueError, match="proxy contains the protected credential"):
            await client.get("https://mcp.example/mcp")
    assert len(calls) == 1
    assert "next-process-secret" in observed


@pytest.mark.anyio
async def test_bypassed_proxy_credentials_do_not_block_direct_admission(runtime_network, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-user:process-secret@127.0.0.1:9876")
    monkeypatch.setenv("NO_PROXY", "mcp.example")
    calls = recorded_transports(monkeypatch)
    async with http_client(credential_boundary=ProcessCredentialBoundary("process-secret")) as client:
        await client.get("https://mcp.example/mcp")
    assert len(calls) == 1 and calls[0][0] == "direct"


@pytest.mark.anyio
async def test_proxy_does_not_bypass_private_network_policy(runtime_network, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9876")

    async def private(self, host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port))]

    async def private_recovery(*args, **kwargs):
        return frozenset({ipaddress.ip_address("192.168.1.10")})

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", private)
    monkeypatch.setattr("pulsara_agent.conversation_kernel.mcp.sdk_facade._resolve_public_http_addresses", private_recovery)
    calls = recorded_transports(monkeypatch)
    async with http_client() as client:
        with pytest.raises(ValueError, match="PRIVATE_NETWORK_DENIED"):
            await client.get("https://mcp.example/mcp")
    assert calls == []


@pytest.mark.anyio
async def test_proxy_discovery_does_not_adopt_certificate_environment(runtime_network, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/untrusted-env-ca.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent/untrusted-env-ca-dir")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9876")
    async with http_client() as client:
        assert client._transport is not None


@asynccontextmanager
async def sdk_proxy_service(tmp_path, monkeypatch, *, doh_handler=None, legacy_sse=False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MCP proxy test")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2000, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2100, 1, 1, tzinfo=timezone.utc))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("mcp.example"), x509.DNSName("oauth.example"), x509.DNSName("cloudflare-dns.com")]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    def verified_test_ca(**kwargs):
        # Trust this disposable test CA only; certificate/hostname verification
        # remains enabled, without modifying the user's macOS trust store.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=str(cert_path))
        return context

    monkeypatch.setattr(httpx2, "create_ssl_context", verified_test_ca)
    state = SimpleNamespace(connects=[], echoes=[], oauth=[], doh=[], tasks=set(), writers=set())
    sdk = MCPServer("proxy-regression")

    @sdk.tool()
    def echo(text: str) -> str:
        state.echoes.append(text)
        return "proxy:" + text

    security = TransportSecuritySettings(allowed_hosts=["mcp.example"])
    mcp_app = sdk.sse_app(transport_security=security) if legacy_sse else sdk.streamable_http_app(stateless_http=True, transport_security=security)

    async def oauth(request):
        state.oauth.append((request.method, request.url.hostname, request.url.path, await request.body()))
        return JSONResponse({"ok": True})

    async def doh(request):
        query = dns.message.from_wire(await request.body())
        state.doh.append((query, dict(request.headers)))
        if doh_handler is not None:
            return await doh_handler(query)
        reply = dns.message.make_response(query)
        question = query.question[0]
        if question.rdtype == dns.rdatatype.A:
            reply.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "93.184.216.34"))
        return Response(reply.to_wire(), media_type="application/dns-message")

    app = Starlette(routes=[Route("/dns-query", doh, methods=["POST"]), Route("/metadata", oauth), Route("/token", oauth, methods=["POST"]), Mount("/", app=mcp_app)], lifespan=mcp_app.router.lifespan_context)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    state.port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", ssl_certfile=str(cert_path), ssl_keyfile=str(key_path)))
    serving = asyncio.create_task(server.serve(sockets=[sock]))

    async def connection(reader, writer):
        task = asyncio.current_task()
        state.tasks.add(task)
        state.writers.add(writer)
        upstream = None
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            method, authority, _ = header.split(b"\r\n", 1)[0].split()
            assert method == b"CONNECT"
            state.connects.append(authority.decode())
            remote, upstream = await asyncio.open_connection("127.0.0.1", sock.getsockname()[1])
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()

            async def pump(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()
                destination.close()

            await asyncio.gather(pump(reader, upstream), pump(remote, writer))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            if upstream:
                upstream.close()
            state.writers.discard(writer)
            state.tasks.discard(task)

    proxy = await asyncio.start_server(connection, "127.0.0.1", 0)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.sockets[0].getsockname()[1]}")
    try:
        async with asyncio.timeout(5):  # Test startup hang detector.
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(.01)
        yield state
    finally:
        proxy.close()
        await proxy.wait_closed()
        for writer in state.writers:
            writer.close()
        await asyncio.gather(*tuple(state.tasks), return_exceptions=True)
        server.should_exit = True
        await serving
        sock.close()


@pytest.mark.anyio
async def test_real_sdk_discovers_and_calls_through_automatic_connect_proxy(tmp_path, runtime_network, monkeypatch):
    async with sdk_proxy_service(tmp_path, monkeypatch) as state:
        async def notify(_):
            pass

        config = _parse_server("proxy-test", {"transport": {"type": "streamable_http", "endpoint": "https://mcp.example/mcp"}})
        client = BoundedMcpSdkClient(config, workspace_root=tmp_path, notification_callback=notify, credential_boundary=ProcessCredentialBoundary())
        try:
            await client.open()
            snapshot, _ = await discover_mcp_catalog(client, config)
            assert [tool.remote_tool_name for tool in snapshot.tools] == ["echo"]
            result = await client.session.call_tool("echo", {"text": "once"})
            assert not result.is_error
            assert result.structured_content == {"result": "proxy:once"}
            assert state.echoes == ["once"]
        finally:
            await client.aclose()
        assert state.connects and set(state.connects) == {"93.184.216.34:443"}


@pytest.mark.anyio
async def test_production_oauth_http_uses_same_automatic_proxy(tmp_path, runtime_network, monkeypatch):
    async with sdk_proxy_service(tmp_path, monkeypatch) as state:
        service = LocalMcpManagementService(LocalSettingsStore(tmp_path / "settings.yaml"), user_config_path=tmp_path / "mcp.yaml")
        config = _parse_server("proxy-oauth", {"transport": {"type": "streamable_http", "endpoint": "https://mcp.example/mcp"}})
        try:
            metadata = await service.oauth._request(config, httpx2.Request("GET", "https://oauth.example/metadata"))
            token = await service.oauth._request(config, httpx2.Request("POST", "https://oauth.example/token", data={"code": "synthetic-code"}))
            assert metadata.status_code == token.status_code == 200
        finally:
            await service.aclose()
        assert state.oauth == [("GET", "oauth.example", "/metadata", b""), ("POST", "oauth.example", "/token", b"code=synthetic-code")]
        assert state.connects and set(state.connects) == {"93.184.216.34:443"}
