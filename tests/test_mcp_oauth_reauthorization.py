"""Real HTTP/SDK regressions for revoked grants and user-owned recovery."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import socket
from time import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from aiohttp import web
import httpx
import httpx2
import pytest

from pulsara_agent.capability.mcp_management import (
    LocalMcpManagementService, LocalMcpTarget, config_to_entry,
)
from pulsara_agent.conversation_kernel.mcp.oauth import McpOAuthNeedsLogin
from pulsara_agent.conversation_kernel.mcp.sdk_facade import (
    BoundedMcpSdkClient, MCPError, _McpHttpClient,
)
from pulsara_agent.conversation_kernel.mcp.wire import DEFAULT_MCP_WIRE_BOUNDS
from pulsara_agent.settings import LocalSettingsStore


@asynccontextmanager
async def revocable_server(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        redirect = f"http://127.0.0.1:{sock.getsockname()[1]}/callback"
    state = SimpleNamespace(
        accepted=None, issued=0, browser_visits=0, callbacks=[], token_error=False,
        auto_callback=True, tool_calls=0, delete_status=200,
        held=asyncio.Event(), release=asyncio.Event(), token_forms=[],
    )
    base = ""

    def challenge(*, sse=False):
        return web.Response(status=401, headers={
            "WWW-Authenticate": f'Bearer resource_metadata="{base}/resource", scope="read"',
            "Content-Type": "text/event-stream" if sse else "text/plain",
        })

    async def serve(request):
        if request.path in {"/mcp", "/hold", "/sse", "/redirect", "/redirect-reject"}:
            if request.path == "/redirect":
                return web.Response(status=302, headers={"Location": "/redirect-reject"})
            if request.path == "/hold":
                state.held.set()
                await state.release.wait()
                return challenge()
            if request.path == "/sse":
                return challenge(sse=True)
            if request.path == "/redirect-reject":
                return challenge()
            if request.method == "DELETE":
                return web.Response(status=state.delete_status)
            message = await request.json() if request.method == "POST" else None
            if message and message["method"] == "tools/call":
                state.tool_calls += 1
            if state.accepted is None or request.headers.get("Authorization") != state.accepted:
                return challenge()
            if message is None:
                return web.Response(status=405)
            method = message["method"]
            if "id" not in message:
                return web.Response(status=202)
            if method == "server/discover":
                return web.json_response({"jsonrpc": "2.0", "id": message["id"],
                    "error": {"code": -32601, "message": "legacy fixture"}})
            if method == "initialize":
                result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "revocable", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "effect", "inputSchema": {"type": "object"}}]}
            elif method == "tools/call":
                result = {"content": [{"type": "text", "text": "done"}]}
            else:
                raise AssertionError(method)
            return web.json_response({"jsonrpc": "2.0", "id": message["id"], "result": result})
        if request.path == "/resource":
            return web.json_response({"resource": base + "/mcp",
                "authorization_servers": [base], "scopes_supported": ["read"]})
        if request.path == "/.well-known/oauth-authorization-server":
            return web.json_response({"issuer": base, "authorization_endpoint": base + "/authorize",
                "token_endpoint": base + "/token", "registration_endpoint": base + "/register",
                "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"]})
        if request.path == "/register":
            return web.json_response({**await request.json(), "client_id": "fixture"}, status=201)
        if request.path == "/token":
            form = dict(await request.post())
            state.token_forms.append(form)
            assert form["grant_type"] == "authorization_code"
            assert form["code"] == "synthetic-code"
            if state.token_error:
                return web.json_response({"error": "invalid_grant"}, status=400)
            state.issued += 1
            token = f"synthetic-grant-{state.issued}"
            state.accepted = f"Bearer {token}"
            return web.json_response({"access_token": token, "token_type": "Bearer",
                "refresh_token": "synthetic-refresh", "expires_in": 3600, "scope": "read"})
        return web.Response(status=404)

    async def browser(url):
        state.browser_visits += 1
        query = parse_qs(urlsplit(url).query)
        assert query["code_challenge_method"] == ["S256"]
        if state.auto_callback:
            async def visit():
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.get(query["redirect_uri"][0], params={
                        "code": "synthetic-code", "state": query["state"][0], "iss": base})
                    assert response.status_code == 200
            state.callbacks.append(asyncio.create_task(visit()))

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", serve)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    state.base = base
    settings = LocalSettingsStore(tmp_path / "settings.yaml")
    service = LocalMcpManagementService(settings, user_config_path=tmp_path / "mcp.yaml", open_browser=browser)
    target = LocalMcpTarget("revocable")
    await service.create(target, {"transport": {"type": "streamable_http", "endpoint": base + "/mcp",
        "allow_http_localhost": True, "stateless_http_asserted": True},
        "auth": {"type": "oauth", "redirect_uri": redirect}})
    state.service, state.target, state.settings = service, target, settings
    try:
        yield state
    finally:
        state.release.set()
        await service.aclose()
        await asyncio.gather(*state.callbacks)
        await runner.cleanup()


async def login(state):
    flow = await state.service.authorize(state.target)
    await asyncio.wait_for(flow.task, 5)
    assert state.service.oauth.login_state(state.target.owner)["state"] == "authorized"
    return state.settings.read().mcp_authorization(state.target.owner)


async def _test_connection(state, tmp_path):
    config = state.service.inspect(state.target)
    return await state.service.test(state.target, config_to_entry(config), workspace_root=tmp_path)


@asynccontextmanager
async def raw_client(state, *, config=None, follow_redirects=False):
    config = config or state.service.inspect(state.target)
    async with _McpHttpClient(config.transport, config=config, bounds=DEFAULT_MCP_WIRE_BOUNDS,
        credential_boundary=state.service.credential_boundary, observe_secrets=lambda _: None,
        follow_redirects=follow_redirects, timeout=httpx2.Timeout(5)) as client:
        yield client


def test_revoked_unexpired_token_recovers_through_public_authorize_without_tool_replay(tmp_path):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            old = await login(state)
            assert old.expires_at > time()
            assert (await _test_connection(state, tmp_path)).status == "ready"
            async def ignore(_): pass
            client = BoundedMcpSdkClient(state.service.inspect(state.target), workspace_root=tmp_path,
                notification_callback=ignore, credential_boundary=state.service.credential_boundary)
            await client.open()
            try:
                await client.session.list_tools()
                state.accepted = None
                with pytest.raises(MCPError) as caught:
                    await asyncio.wait_for(client.session.call_tool("effect", {}), 5)
                # SDK HTTP transport's typed error carrier for an HTTP 401;
                # the test watchdog TimeoutError is never accepted here.
                assert caught.value.code == -32603
                assert state.tool_calls == 1
            finally:
                await client.aclose()
            assert state.service.oauth.login_state(state.target.owner)["state"] == "needs_login"
            assert state.settings.read().mcp_authorization(state.target.owner) == old
            with pytest.raises(McpOAuthNeedsLogin):
                await state.service.inspect(state.target).authorization_provider()
            await login(state)
            assert state.browser_visits == 2
            assert state.tool_calls == 1
            assert (await _test_connection(state, tmp_path)).status == "ready"
            assert state.issued == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_reauthorization_failure_or_cancel_keeps_saved_grant_and_needs_login(tmp_path, outcome):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            old = await login(state)
            state.accepted = None
            assert (await _test_connection(state, tmp_path)).status != "ready"
            state.token_error = outcome == "failed"
            state.auto_callback = outcome != "cancelled"
            flow = await state.service.authorize(state.target)
            if outcome == "failed":
                with pytest.raises(ValueError):
                    await asyncio.wait_for(flow.task, 5)
            else:
                async with asyncio.timeout(5):
                    while flow.state != "awaiting_user":
                        await asyncio.sleep(0)
                assert state.service.oauth.login_state(state.target.owner)["state"] == "awaiting_user"
                await state.service.oauth.cancel(state.target.owner)
                assert flow.task.cancelled()
            assert state.settings.read().mcp_authorization(state.target.owner) == old
            assert state.service.oauth.login_state(state.target.owner)["state"] == "needs_login"
            state.token_error, state.auto_callback = False, True
            await login(state)
            assert (await _test_connection(state, tmp_path)).status == "ready"
    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize("replacement", ["new_grant", "new_login", "new_config", "cached_login"])
def test_late_old_401_cannot_reject_new_grant_login_or_config(tmp_path, replacement, method):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            await login(state)
            async with raw_client(state) as client:
                pending = asyncio.create_task(client.request(method, state.base + "/hold"))
                await asyncio.wait_for(state.held.wait(), 5)
                if replacement == "new_config":
                    entry = config_to_entry(state.service.inspect(state.target))
                    entry["display_name"] = "Changed"
                    await state.service.update(state.target, entry,
                        expected=state.service.inspect(state.target).resolved_config_identity)
                elif replacement == "cached_login":
                    before = state.settings.read().mcp_authorization(state.target.owner)
                    await login(state)
                    assert state.browser_visits == 1
                    assert state.settings.read().mcp_authorization(state.target.owner) == before
                else:
                    state.accepted = None
                    assert (await _test_connection(state, tmp_path)).status != "ready"
                    if replacement == "new_login":
                        state.auto_callback = False
                        flow = await state.service.authorize(state.target)
                        async with asyncio.timeout(5):
                            while flow.state != "awaiting_user":
                                await asyncio.sleep(0)
                    else:
                        await login(state)
                state.release.set()
                assert (await asyncio.wait_for(pending, 5)).status_code == 401
            status = state.service.oauth.login_state(state.target.owner)["state"]
            assert status == {
                "new_login": "awaiting_user", "cached_login": "needs_login",
                "new_grant": "authorized", "new_config": "authorized",
            }[replacement]
            if replacement == "cached_login":
                await login(state)
                assert state.browser_visits == 2
            if replacement == "new_login":
                await state.service.oauth.cancel(state.target.owner)
            else:
                assert (await _test_connection(state, tmp_path)).status == "ready"
    asyncio.run(scenario())


def test_401_observation_precedes_sse_and_ignores_static_and_redirect_response(tmp_path):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            await login(state)
            state.delete_status = 401
            async with raw_client(state) as client:
                assert (await client.delete(state.base + "/mcp")).status_code == 401
                assert state.service.oauth.login_state(state.target.owner)["state"] == "needs_login"
            await login(state)
            async with raw_client(state) as client:
                assert (await client.get(state.base + "/redirect")).status_code == 302
                assert state.service.oauth.login_state(state.target.owner)["state"] == "authorized"
            entry = config_to_entry(state.service.inspect(state.target))
            config = state.service.parse(state.target, {**entry, "auth": {"type": "none"},
                "public_headers": {"Authorization": "Bearer synthetic-static"}})
            async with raw_client(state, config=config) as client:
                assert (await client.get(state.base + "/mcp")).status_code == 401
            assert state.service.oauth.login_state(state.target.owner)["state"] == "authorized"
            async with raw_client(state) as client:
                response = await client.send(client.build_request("GET", state.base + "/sse"), stream=True)
                try:
                    assert response.status_code == 401
                    assert state.service.oauth.login_state(state.target.owner)["state"] == "needs_login"
                finally:
                    await response.aclose()
            await login(state)
            async with raw_client(state, follow_redirects=True) as client:
                assert (await client.get(state.base + "/redirect")).status_code == 401
            # The actual redirected MCP request carried the current token and was rejected.
            assert state.service.oauth.login_state(state.target.owner)["state"] == "needs_login"
    asyncio.run(scenario())


def test_cached_login_waiting_for_acceptance_cannot_clear_a_new_401(tmp_path):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            old = await login(state)
            async with raw_client(state) as client:
                pending = asyncio.create_task(client.get(state.base + "/hold"))
                await asyncio.wait_for(state.held.wait(), 5)
                flow = await state.service.authorize(state.target)
                async with state.service.lane:
                    # The SDK completes its real cached-token path and queues at
                    # Pulsara's existing publication lane; no protocol is faked.
                    async with asyncio.timeout(5):
                        while not state.service.lane._waiters:
                            await asyncio.sleep(0)
                    state.release.set()
                    assert (await asyncio.wait_for(pending, 5)).status_code == 401
                with pytest.raises(ValueError):
                    await asyncio.wait_for(flow.task, 5)
            assert state.browser_visits == 1
            assert state.settings.read().mcp_authorization(state.target.owner) == old
            assert state.service.oauth.login_state(state.target.owner)["state"] == "needs_login"
            await login(state)
            assert (await _test_connection(state, tmp_path)).status == "ready"
    asyncio.run(scenario())


def test_new_grant_401_during_publication_is_not_cleared_as_old_rejection(tmp_path, monkeypatch):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            old = await login(state)
            state.accepted = None
            assert (await _test_connection(state, tmp_path)).status != "ready"
            save = state.settings.replace_mcp_authorization

            async def observe_after_save(owner, record, *, expected):
                await save(owner, record, expected=expected)
                # Exercise a real request with the newly visible grant before
                # the publication await returns to the OAuth owner.
                if record is not None and record != old:
                    state.accepted = None
                    async with raw_client(state) as client:
                        assert (await client.get(state.base + "/sse")).status_code == 401

            monkeypatch.setattr(state.settings, "replace_mcp_authorization", observe_after_save)
            flow = await state.service.authorize(state.target)
            await asyncio.wait_for(flow.task, 5)
            assert state.settings.read().mcp_authorization(state.target.owner) != old
            assert state.service.oauth.login_state(state.target.owner)["state"] == "needs_login"
            with pytest.raises(McpOAuthNeedsLogin):
                await state.service.inspect(state.target).authorization_provider()
    asyncio.run(scenario())
