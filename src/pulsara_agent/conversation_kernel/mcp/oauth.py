"""User-owned MCP OAuth using SDK protocol logic and bounded HTTP requests.

The SDK auth generator is driven only with a synthetic authentication probe.
It is never installed on an MCP tools/call HTTP client, so it cannot replay tools.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import logging
import secrets
from time import time
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from aiohttp import web
import httpx2
from mcp.client.auth.oauth2 import OAuthClientProvider, OAuthContext
from pydantic import PrivateAttr
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url

from pulsara_agent.capability.mcp_management import auth_to_entry
from pulsara_agent.mcp_config import (
    McpServerConfig,
    OAuthAuthorization,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
)
from pulsara_agent.mcp_credentials import (
    LocalMcpOAuthRecord,
    McpCredentialOwner,
    resolve_secret,
)
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
)
from pulsara_agent.settings import LocalSettingsStore
from .oauth_callback_page import oauth_callback_response
from .sdk_facade import (
    _McpHttpClient,
    _enforce_http_network_policy,
)
from .wire import DEFAULT_MCP_WIRE_BOUNDS


class McpOAuthNeedsLogin(ValueError):
    def __init__(self):
        super().__init__("MCP connection needs browser authorization")


class McpOAuthStale(ValueError):
    def __init__(self):
        super().__init__("MCP authorization target changed")


_private_auth_flow = ContextVar("pulsara_mcp_private_auth_flow", default=False)


class _SdkAuthLogFilter(logging.Filter):
    def filter(self, record):
        # SDK validation exceptions can contain raw token responses. Public outcomes
        # are emitted by this owner, not by the SDK's logger.exception branches.
        return not _private_auth_flow.get()


logging.getLogger("mcp.client.auth.oauth2").addFilter(_SdkAuthLogFilter())


@dataclass(slots=True)
class McpOAuthLogin:
    owner: McpCredentialOwner
    config: McpServerConfig
    current_target: Callable[[], McpServerConfig | None] = field(repr=False)
    task: asyncio.Task[LocalMcpOAuthRecord] | None = field(default=None, repr=False)
    authorization_url: str | None = field(default=None, repr=False)
    state: str = "connecting"
    error: str | None = None


class _DraftTokenStorage:
    """SDK writes remain private/call-local until exact target acceptance."""

    def __init__(self, record: LocalMcpOAuthRecord | None):
        self.tokens = (
            OAuthToken.model_validate_json(record.token_json) if record else None
        )
        self.client = (
            OAuthClientInformationFull.model_validate_json(record.client_json)
            if record
            else None
        )

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, value):
        self.tokens = value

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, value):
        self.client = value


class _UserScopedClientMetadata(OAuthClientMetadata):
    """The SDK may choose default scopes, but not widen explicit user scopes."""

    _requested_scope: str | None = PrivateAttr(default=None)

    def __setattr__(self, name, value):
        if name == "scope" and self._requested_scope is not None:
            value = self._requested_scope
        super().__setattr__(name, value)


@dataclass
class _ConfiguredResourceContext(OAuthContext):
    requested_resource: str | None = None

    def get_resource_url(self):
        return self.requested_resource or super().get_resource_url()

    def should_include_resource_param(self, protocol_version=None):
        return (
            self.requested_resource is not None
            or super().should_include_resource_param(protocol_version)
        )


class _ConfiguredOAuthProvider(OAuthClientProvider):
    def __init__(self, *args, requested_resource=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.context = _ConfiguredResourceContext(
            **vars(self.context), requested_resource=requested_resource
        )


class McpOAuthManager:
    def __init__(
        self,
        settings: LocalSettingsStore,
        *,
        lane: asyncio.Lock,
        credential_boundary: ProcessCredentialBoundary,
        open_browser: Callable[[str], Awaitable[None]],
    ):
        self.settings = settings
        self.lane = lane
        self.credential_boundary = credential_boundary
        self.open_browser = open_browser
        self._logins: dict[McpCredentialOwner, McpOAuthLogin] = {}
        self._refreshes: dict[
            McpCredentialOwner, asyncio.Task[LocalMcpOAuthRecord]
        ] = {}

    def login_state(
        self, owner: McpCredentialOwner, config=None
    ) -> dict[str, str | None]:
        flow = self._logins.get(owner)
        if flow is None:
            record = self.settings.read().mcp_authorization(owner)
            if record is not None and (config is None or self._matches(record, config)):
                token = OAuthToken.model_validate_json(record.token_json)
                expired = record.expires_at is not None and record.expires_at <= time()
                return {
                    "state": "needs_login"
                    if expired and not token.refresh_token
                    else "authorized",
                    "error": None,
                }
        return (
            {"state": flow.state, "error": flow.error}
            if flow
            else {"state": "idle", "error": None}
        )

    async def begin(
        self,
        owner: McpCredentialOwner,
        current_target: Callable[[], McpServerConfig | None],
    ) -> McpOAuthLogin:
        async with self.lane:
            return self._begin(owner, current_target)

    def _begin(self, owner, current_target) -> McpOAuthLogin:
        current = current_target()
        if current is None or not isinstance(current.auth, OAuthAuthorization):
            raise ValueError("save an OAuth connection before logging in")
        prior = self._logins.get(owner)
        if prior is not None and prior.task is not None and not prior.task.done():
            return prior
        flow = McpOAuthLogin(owner, current, current_target)
        self._logins[owner] = flow
        flow.task = asyncio.create_task(self._login(flow), name="mcp-oauth-login")
        # UI may observe state without awaiting user interaction; retrieve errors
        # so abandoned HTTP clients do not create unhandled secret-bearing traces.
        flow.task.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
        return flow

    async def _login(self, flow: McpOAuthLogin) -> LocalMcpOAuthRecord:
        try:
            record = await self._exchange(
                flow.owner, flow.config, current_target=flow.current_target, flow=flow
            )
            flow.state = "authorized"
            return record
        except asyncio.CancelledError:
            flow.state = "cancelled"
            raise
        except Exception:
            flow.state = "failed"
            flow.error = "登录未完成，请检查服务端客户端注册、权限或重新登录。"
            raise ValueError(flow.error) from None
        finally:
            flow.authorization_url = None

    def invalidate(self, owner: McpCredentialOwner) -> tuple[asyncio.Task, ...]:
        """Call under canonical lane before changing/removing/logout of the owner."""
        flow = self._logins.pop(owner, None)
        refresh = self._refreshes.pop(owner, None)
        tasks = tuple(
            task
            for task in ((flow.task if flow else None), refresh)
            if task is not None and not task.done()
        )
        for task in tasks:
            task.cancel()
        return tasks

    async def cancel(self, owner: McpCredentialOwner, *, require_current=None) -> None:
        async with self.lane:
            if require_current is not None:
                require_current()
            tasks = self.invalidate(owner)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def logout(self, owner: McpCredentialOwner, *, require_current=None) -> None:
        async with self.lane:
            if require_current is not None:
                require_current()
            tasks = self.invalidate(owner)
            old = self.settings.read().mcp_authorization(owner)
            if old is not None:
                await self.settings.replace_mcp_authorization(owner, None, expected=old)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def authorization(
        self,
        owner: McpCredentialOwner,
        config: McpServerConfig,
        current_target: Callable[[], McpServerConfig | None],
    ) -> tuple[str, tuple[str, ...]]:
        record = self.settings.read().mcp_authorization(owner)
        if not self._matches(record, config):
            raise McpOAuthNeedsLogin()
        assert record is not None
        if record.expires_at is not None and record.expires_at <= time():
            task = self._refreshes.get(owner)
            if task is None:
                task = asyncio.create_task(
                    self._exchange(
                        owner, config, current_target=current_target, flow=None
                    ),
                    name="mcp-oauth-refresh",
                )
                self._refreshes[owner] = task
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
            try:
                record = await asyncio.shield(task)
            finally:
                if task.done() and self._refreshes.get(owner) is task:
                    self._refreshes.pop(owner)
        token = OAuthToken.model_validate_json(record.token_json)
        return f"Bearer {token.access_token}", (token.access_token,)

    @staticmethod
    def _matches(record, config):
        return (
            record is not None
            and not isinstance(config.transport, StdioTransportConfig)
            and record.resource_url == config.transport.endpoint
            and json.loads(record.auth_json) == auth_to_entry(config.auth)
        )

    async def _exchange(self, owner, config, *, current_target, flow):
        if not isinstance(config.auth, OAuthAuthorization) or isinstance(
            config.transport, StdioTransportConfig
        ):
            raise ValueError("MCP OAuth requires HTTP")
        auth = config.auth
        prior = self.settings.read().mcp_authorization(owner)
        record = prior if self._matches(prior, config) else None
        if flow is None and record is None:
            raise McpOAuthNeedsLogin()
        storage = _DraftTokenStorage(record)
        metadata = _UserScopedClientMetadata(
            client_name="Pulsara",
            redirect_uris=[auth.redirect_uri],
            scope=auth.scope,
            token_endpoint_auth_method="client_secret_post"
            if auth.client_secret is not None
            else "none",
        )
        metadata._requested_scope = auth.scope
        if storage.client is None and auth.client_id:
            storage.client = OAuthClientInformationFull(
                **metadata.model_dump(),
                client_id=auth.client_id,
                client_secret=resolve_secret(auth.client_secret, config.secret_resolver)
                if auth.client_secret is not None
                else None,
            )
        callback_result = asyncio.get_running_loop().create_future()
        expected_state: str | None = None
        listener: web.AppRunner | None = None

        async def callback(request):
            if (
                expected_state is None
                or not secrets.compare_digest(
                    request.query.get("state", ""), expected_state
                )
                or callback_result.done()
            ):
                return oauth_callback_response(
                    "此授权链接已失效",
                    "回调无效或已被使用。请回到 Pulsara 重新发起登录授权。",
                    status=400,
                )
            if "error" in request.query:
                callback_result.set_exception(McpOAuthNeedsLogin())
                return oauth_callback_response(
                    "本次授权未完成",
                    "服务商没有返回授权许可。你可以回到 Pulsara，准备好后再次登录。",
                )
            elif request.query.get("code"):
                callback_result.set_result(
                    AuthorizationCodeResult(
                        code=request.query["code"],
                        state=request.query.get("state"),
                        iss=request.query.get("iss"),
                    )
                )
            else:
                return oauth_callback_response(
                    "未收到完整的授权信息",
                    "回调中缺少授权码。请回到 Pulsara 重新发起登录授权。",
                    status=400,
                )
            return oauth_callback_response(
                "已收到授权回调",
                "Pulsara 将继续核验授权信息，最终结果会显示在能力页中。",
            )

        async def redirect(url):
            nonlocal listener, expected_state
            if flow is None:
                raise McpOAuthNeedsLogin()
            await self._validate_url(config, url)
            expected_state = parse_qs(urlsplit(url).query).get("state", [None])[0]
            if not expected_state:
                raise ValueError("OAuth state is missing")
            uri = urlsplit(auth.redirect_uri)
            app = web.Application(
                client_max_size=DEFAULT_MCP_WIRE_BOUNDS.maximum_http_json_body_bytes
            )
            app.router.add_get(uri.path, callback)
            listener = web.AppRunner(app, access_log=None)
            await listener.setup()
            await web.TCPSite(listener, uri.hostname, uri.port).start()
            flow.authorization_url = url
            flow.state = "awaiting_user"
            await self.open_browser(url)

        async def validate_resource(server_url, resource):
            if resource is None:
                return
            allowed = (
                resource.rstrip("/") == auth.resource.rstrip("/")
                if auth.resource
                else check_resource_allowed(
                    requested_resource=resource_url_from_server_url(server_url),
                    configured_resource=resource,
                )
            )
            if not allowed:
                raise ValueError("OAuth resource does not match configured resource")

        provider = _ConfiguredOAuthProvider(
            config.transport.endpoint,
            metadata,
            storage,
            redirect_handler=redirect,
            callback_handler=lambda: callback_result,
            client_metadata_url=auth.client_metadata_url,
            validate_resource_url=validate_resource,
            requested_resource=auth.resource,
        )
        # Initialize explicitly to restore true expiration, not a fresh token lifetime.
        await provider._initialize()
        if record is not None:
            provider.context.token_expiry_time = record.expires_at
            provider.context.auth_server_url = record.issuer
            stored_metadata = json.loads(record.metadata_json)
            if stored_metadata["authorization_server"] is not None:
                provider.context.oauth_metadata = OAuthMetadata.model_validate(
                    stored_metadata["authorization_server"]
                )
            if stored_metadata["protected_resource"] is not None:
                provider.context.protected_resource_metadata = (
                    ProtectedResourceMetadata.model_validate(
                        stored_metadata["protected_resource"]
                    )
                )
        probe = httpx2.Request("GET", config.transport.endpoint)
        generator = provider.async_auth_flow(probe)
        private_context = _private_auth_flow.set(True)
        try:
            request = await generator.__anext__()
            while True:
                if request is probe and request.headers.get("Authorization"):
                    # SDK wants to retry the authenticated probe. Stop here: tokens
                    # have been validated, and no MCP application operation is replayed.
                    break
                if request is probe and flow is None:
                    raise McpOAuthNeedsLogin()
                response = await self._request(config, request)
                try:
                    request = await generator.asend(response)
                except StopAsyncIteration:
                    raise McpOAuthNeedsLogin() from None
            tokens, client = storage.tokens, storage.client
            if tokens is None or client is None:
                raise McpOAuthNeedsLogin()
            issuer = provider.context.auth_server_url or (
                str(provider.context.oauth_metadata.issuer)
                if provider.context.oauth_metadata
                else client.issuer
            )
            if not issuer:
                raise ValueError("OAuth issuer unavailable")
            if client.issuer is None:
                client.issuer = issuer
            accepted = LocalMcpOAuthRecord(
                owner,
                config.transport.endpoint,
                issuer,
                client.client_id,
                tokens.scope or metadata.scope or "",
                tokens.model_dump_json(),
                client.model_dump_json(),
                provider.context.token_expiry_time,
                json.dumps(
                    {
                        "authorization_server": provider.context.oauth_metadata.model_dump(
                            mode="json"
                        )
                        if provider.context.oauth_metadata
                        else None,
                        "protected_resource": provider.context.protected_resource_metadata.model_dump(
                            mode="json"
                        )
                        if provider.context.protected_resource_metadata
                        else None,
                    }
                ),
                json.dumps(auth_to_entry(auth), sort_keys=True),
            )
            async with self.lane:
                if current_target() != config:
                    raise McpOAuthStale()
                if flow is not None:
                    if self._logins.get(owner) is not flow:
                        raise McpOAuthStale()
                elif self._refreshes.get(owner) is not asyncio.current_task():
                    raise McpOAuthStale()
                await self.settings.replace_mcp_authorization(
                    owner, accepted, expected=prior
                )
            return accepted
        except asyncio.CancelledError:
            raise
        except McpOAuthNeedsLogin:
            raise
        except Exception:
            raise ValueError(
                "MCP OAuth exchange failed; check connection and client registration"
            ) from None
        finally:
            await generator.aclose()
            _private_auth_flow.reset(private_context)
            if listener is not None:
                await listener.cleanup()
            if not callback_result.done():
                callback_result.cancel()
            elif not callback_result.cancelled():
                callback_result.exception()

    async def _validate_url(self, config, url):
        return await _enforce_http_network_policy(
            StreamableHttpTransportConfig(
                endpoint=url,
                allow_http_localhost=config.transport.allow_http_localhost,
                network_policy=config.transport.network_policy,
            )
        )

    async def _request(self, config, request):
        """SDK-native HTTP; OAuth redirects remain in the auth-only context."""
        async with _McpHttpClient(
            config.transport,
            bounds=DEFAULT_MCP_WIRE_BOUNDS,
            credential_boundary=self.credential_boundary,
            timeout=httpx2.Timeout(10),
            follow_redirects=True,
        ) as client:
            return await client.send(request)

    async def aclose(self):
        async with self.lane:
            tasks = tuple(
                task
                for owner in set(self._logins) | set(self._refreshes)
                for task in self.invalidate(owner)
            )
        await asyncio.gather(*tasks, return_exceptions=True)
