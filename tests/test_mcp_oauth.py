from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import base64
import hashlib
import socket
from time import time
from urllib.parse import parse_qs, urlencode, urlsplit

from aiohttp import web
import httpx
import pytest

from pulsara_agent.capability.mcp_management import (
    LocalMcpManagementService,
    LocalMcpTarget,
)
from pulsara_agent.conversation_kernel.mcp.oauth import (
    McpOAuthManager,
    McpOAuthNeedsLogin,
)
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.settings import LocalSettingsStore


@asynccontextmanager
async def oauth_server(
    tmp_path,
    *,
    auto_callback=True,
    user_scope=None,
    resource_path="/mcp",
    client_id=None,
    trace=None,
    metadata_redirect=False,
):
    requests = []
    authorizations = []
    token_forms = []
    if trace is not None:
        trace.update(
            authorizations=authorizations, token_forms=token_forms, requests=requests
        )
    callbacks = []
    refresh_started = asyncio.Event()
    refresh_release = asyncio.Event()
    refresh_release.set()
    with socket.socket() as callback_socket:
        callback_socket.bind(("127.0.0.1", 0))
        callback_port = callback_socket.getsockname()[1]
    redirect_uri = f"http://127.0.0.1:{callback_port}/callback"
    base = ""

    async def serve(request):
        requests.append(
            (request.method, request.path, request.headers.get("Authorization"))
        )
        if request.path == "/mcp":
            return web.Response(
                status=401,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{base}/resource", scope="read"'
                },
            )
        if request.path == "/resource":
            return web.json_response(
                {
                    "resource": resource_path
                    if resource_path.startswith("https://")
                    else base + resource_path,
                    "authorization_servers": [base],
                    "scopes_supported": ["read"],
                }
            )
        if (
            request.path == "/.well-known/oauth-authorization-server"
            and metadata_redirect
        ):
            return web.Response(
                status=302, headers={"Location": "/authorization-metadata"}
            )
        if request.path in {
            "/.well-known/oauth-authorization-server",
            "/authorization-metadata",
        }:
            return web.json_response(
                {
                    "issuer": base,
                    "authorization_endpoint": base + "/authorize",
                    "token_endpoint": base + "/token",
                    "registration_endpoint": base + "/register",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_methods_supported": ["none"],
                }
            )
        if request.path == "/register":
            metadata = await request.json()
            assert metadata["redirect_uris"] == [redirect_uri]
            return web.json_response(
                {**metadata, "client_id": "local-test-client"}, status=201
            )
        if request.path == "/token":
            form = dict(await request.post())
            token_forms.append(form)
            if form["grant_type"] == "authorization_code":
                challenge = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(form["code_verifier"].encode()).digest()
                    )
                    .decode()
                    .rstrip("=")
                )
                assert challenge == authorizations[-1]["code_challenge"][0]
                assert form["redirect_uri"] == redirect_uri
                assert form["code"] == "single-use-code"
                access = "initial-private-token"
            else:
                assert form["grant_type"] == "refresh_token"
                assert form["refresh_token"] == "private-refresh-token"
                refresh_started.set()
                await refresh_release.wait()
                access = "refreshed-private-token"
            return web.json_response(
                {
                    "access_token": access,
                    "token_type": "Bearer",
                    "refresh_token": "private-refresh-token",
                    "expires_in": 3600,
                    "scope": user_scope or "read",
                }
            )
        return web.Response(status=404)

    async def browser(url):
        query = parse_qs(urlsplit(url).query)
        authorizations.append(query)
        assert query["code_challenge_method"] == ["S256"]
        callback = (
            query["redirect_uri"][0]
            + "?"
            + urlencode(
                {"code": "single-use-code", "state": query["state"][0], "iss": base}
            )
        )
        callbacks.append(callback)
        if auto_callback:
            async with httpx.AsyncClient(trust_env=False) as client:
                wrong = await client.get(
                    query["redirect_uri"][0],
                    params={"code": "wrong", "state": "wrong-state"},
                )
                assert wrong.status_code == 400
                assert wrong.headers["content-type"].startswith("text/html")
                assert "此授权链接已失效" in wrong.text
                assert "wrong-state" not in wrong.text
                missing = await client.get(
                    query["redirect_uri"][0], params={"state": query["state"][0]}
                )
                assert missing.status_code == 400
                assert "未收到完整的授权信息" in missing.text
                assert missing.headers["cache-control"] == "no-store"
                response = await client.get(callback)
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/html")
                assert response.headers["cache-control"] == "no-store"
                assert response.headers["referrer-policy"] == "no-referrer"
                assert "已收到授权回调" in response.text
                assert "最终结果会显示在能力页中" in response.text
                assert "single-use-code" not in response.text
                assert query["state"][0] not in response.text

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", serve)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    settings = LocalSettingsStore(tmp_path / "local-settings.yaml")
    service = LocalMcpManagementService(
        settings, user_config_path=tmp_path / "mcp.yaml"
    )
    target = LocalMcpTarget("oauth-test")
    result = await service.create(
        target,
        {
            "transport": {
                "type": "streamable_http",
                "endpoint": base + "/mcp",
                "allow_http_localhost": True,
            },
            "auth": {
                "type": "oauth",
                "redirect_uri": redirect_uri,
                "scope": user_scope,
                "resource": (
                    resource_path
                    if resource_path.startswith("https://")
                    else base + resource_path
                )
                if resource_path != "/mcp"
                else None,
                "client_id": client_id,
            },
        },
    )
    manager = McpOAuthManager(
        settings,
        lane=service.lane,
        credential_boundary=ProcessCredentialBoundary(),
        open_browser=browser,
    )
    try:
        yield (
            manager,
            target,
            result.config,
            requests,
            token_forms,
            callbacks,
            refresh_started,
            refresh_release,
        )
    finally:
        refresh_release.set()
        await manager.aclose()
        await runner.cleanup()


def test_oauth_redirect_and_status_after_manager_restart(tmp_path):
    async def scenario():
        async with oauth_server(tmp_path, metadata_redirect=True) as (
            manager,
            target,
            config,
            requests,
            *_,
        ):
            flow = await manager.begin(target.owner, lambda: config)
            await flow.task
            assert any(path == "/authorization-metadata" for _, path, _ in requests)
            restarted = McpOAuthManager(
                manager.settings,
                lane=manager.lane,
                credential_boundary=ProcessCredentialBoundary(),
                open_browser=manager.open_browser,
            )
            assert restarted.login_state(target.owner, config) == {
                "state": "authorized",
                "error": None,
            }
            await restarted.logout(target.owner)
            assert restarted.login_state(target.owner, config) == {
                "state": "idle",
                "error": None,
            }
            await restarted.aclose()

    asyncio.run(scenario())


def test_oauth_pkce_dcr_refresh_singleflight_and_logout(tmp_path):
    async def scenario():
        async with oauth_server(tmp_path) as (
            manager,
            target,
            config,
            requests,
            forms,
            _,
            started,
            release,
        ):
            with pytest.raises(McpOAuthNeedsLogin):
                await manager.authorization(target.owner, config, lambda: config)
            assert requests == []  # Background use cannot start user login.
            flow = await manager.begin(target.owner, lambda: config)
            assert await manager.begin(target.owner, lambda: config) is flow
            record = await asyncio.wait_for(flow.task, 5)
            assert flow.state == "authorized"
            assert flow.authorization_url is None
            assert record.resource_url == config.transport.endpoint
            assert record.client_id == "local-test-client"
            assert manager.settings.read().mcp_authorization(target.owner) == record
            assert await manager.authorization(
                target.owner, config, lambda: config
            ) == (
                "Bearer initial-private-token",
                ("initial-private-token",),
            )
            assert all(auth is None for _, _, auth in requests)
            assert [path for _, path, _ in requests].count("/mcp") == 1
            expired = replace(record, expires_at=time() - 1)
            await manager.settings.replace_mcp_authorization(
                target.owner, expired, expected=record
            )
            release.clear()
            first = asyncio.create_task(
                manager.authorization(target.owner, config, lambda: config)
            )
            await asyncio.wait_for(started.wait(), 5)
            second = asyncio.create_task(
                manager.authorization(target.owner, config, lambda: config)
            )
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second)
            assert (
                results
                == [("Bearer refreshed-private-token", ("refreshed-private-token",))]
                * 2
            )
            assert [f["grant_type"] for f in forms] == [
                "authorization_code",
                "refresh_token",
            ]
            assert [path for _, path, _ in requests].count("/mcp") == 1
            await manager.logout(target.owner)
            assert manager.settings.read().mcp_authorization(target.owner) is None
            with pytest.raises(McpOAuthNeedsLogin):
                await manager.authorization(target.owner, config, lambda: config)

    asyncio.run(scenario())


def test_oauth_denied_callback_shows_unfinished_page_without_grant(tmp_path):
    async def scenario():
        async with oauth_server(tmp_path, auto_callback=False) as (
            manager,
            target,
            config,
            _,
            forms,
            callbacks,
            _,
            _,
        ):
            flow = await manager.begin(target.owner, lambda: config)
            async with asyncio.timeout(5):
                while not callbacks:
                    await asyncio.sleep(0.01)
            uri = urlsplit(callbacks[0])
            state = parse_qs(uri.query)["state"][0]
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(
                    uri._replace(query="").geturl(),
                    params={
                        "state": state,
                        "error": "access_denied",
                        "error_description": "<script>untrusted</script>",
                    },
                )
            assert response.status_code == 200
            assert "本次授权未完成" in response.text
            assert "已收到授权回调" not in response.text
            assert "untrusted" not in response.text
            assert state not in response.text
            with pytest.raises(ValueError, match="登录未完成"):
                await flow.task
            assert flow.state == "failed"
            assert forms == []
            assert manager.settings.read().mcp_authorization(target.owner) is None

    asyncio.run(scenario())


def test_oauth_cancel_closes_listener_and_preserves_no_grant(tmp_path):
    async def scenario():
        async with oauth_server(tmp_path, auto_callback=False) as (
            manager,
            target,
            config,
            _,
            _,
            callbacks,
            _,
            _,
        ):
            flow = await manager.begin(target.owner, lambda: config)
            async with asyncio.timeout(5):
                while not callbacks:
                    await asyncio.sleep(0.01)
            await manager.cancel(target.owner)
            assert flow.task.cancelled()
            assert flow.authorization_url is None
            assert manager.settings.read().mcp_authorization(target.owner) is None
            async with httpx.AsyncClient(trust_env=False) as client:
                with pytest.raises(httpx.ConnectError):
                    await client.get(callbacks[0])

    asyncio.run(scenario())


def test_logout_during_refresh_cannot_restore_grant(tmp_path):
    async def scenario():
        async with oauth_server(tmp_path) as (
            manager,
            target,
            config,
            _,
            _,
            _,
            started,
            release,
        ):
            record = await asyncio.wait_for(
                (await manager.begin(target.owner, lambda: config)).task, 5
            )
            expired = replace(record, expires_at=time() - 1)
            await manager.settings.replace_mcp_authorization(
                target.owner, expired, expected=record
            )
            release.clear()
            pending = asyncio.create_task(
                manager.authorization(target.owner, config, lambda: config)
            )
            await asyncio.wait_for(started.wait(), 5)
            await manager.logout(target.owner)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert manager.settings.read().mcp_authorization(target.owner) is None

    asyncio.run(scenario())


def test_explicit_scope_pre_registered_client_and_resource_survive_refresh(tmp_path):
    async def scenario():
        trace = {}
        async with oauth_server(
            tmp_path,
            user_scope="limited",
            resource_path="https://resource.example.org/different-audience",
            client_id="my-registration",
            trace=trace,
        ) as values:
            manager, target, config, _, _, _, _, _ = values
            record = await asyncio.wait_for(
                (await manager.begin(target.owner, lambda: config)).task, 5
            )
            assert record.client_id == "my-registration"
            assert trace["authorizations"][0]["scope"] == ["limited"]
            assert trace["authorizations"][0]["resource"] == [config.auth.resource]
            assert not any(path == "/register" for _, path, _ in trace["requests"])
            expired = replace(record, expires_at=time() - 1)
            await manager.settings.replace_mcp_authorization(
                target.owner, expired, expected=record
            )
            assert (await manager.authorization(target.owner, config, lambda: config))[
                0
            ] == "Bearer refreshed-private-token"
            assert [form["resource"] for form in trace["token_forms"]] == [
                config.auth.resource,
                config.auth.resource,
            ]
            assert (
                manager.settings.read().mcp_authorization(target.owner).scope
                == "limited"
            )

    asyncio.run(scenario())
