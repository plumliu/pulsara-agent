"""Original OAuth/SDK owners with an isolated store and an OAuth-only host callback."""

import asyncio
from contextlib import suppress
from dataclasses import replace  # noqa: F401 -- preserve executed research snapshot
import base64
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import sys
import tempfile
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit

from aiohttp import web

MODE = sys.argv[1]
assert MODE in ("mock", "real")
HOME = Path(tempfile.mkdtemp(prefix=".g0oauth6-", dir="/home/plumwsl"))
assert HOME.resolve() == HOME and HOME.stat().st_uid == os.getuid()
assert stat.S_IMODE(HOME.stat().st_mode) == 0o700
os.environ["PULSARA_HOME"] = str(HOME)
original_stderr = sys.stderr
private_diagnostics = os.fdopen(
    os.open(HOME / "diagnostics.private", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
    "w",
    buffering=1,
)
sys.stderr = private_diagnostics
from pulsara_agent.capability.mcp_management import (  # noqa: E402 -- preserve executed research snapshot
    LocalMcpManagementService,
    LocalMcpTarget,
)
from pulsara_agent.settings import LocalSettingsStore  # noqa: E402 -- preserve executed research snapshot
from pulsara_agent.conversation_kernel.mcp import oauth as oauth_module  # noqa: E402 -- preserve executed research snapshot
from pulsara_agent.conversation_kernel.mcp.sdk_facade import BoundedMcpSdkClient  # noqa: E402, F401 -- preserve executed research snapshot
from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet  # noqa: E402 -- preserve executed research snapshot

redactions = ProcessCredentialScrubSet()
original_application = web.Application


def observed_application(*args, **kwargs):
    app = original_application(*args, **kwargs)

    @web.middleware
    async def observe_callback_code(request, handler):
        # Read-only diagnostic observer: the original callback alone validates it.
        code = request.query.get("code")
        redactions.observe(code)
        if code:
            redactions.observe(quote_plus(code))
        return await handler(request)

    app.middlewares.append(observe_callback_code)
    return app


web.Application = observed_application


def observe_private_exchange(service):
    original = service.oauth._request

    async def request(config, value):
        # Observe exact secret values at the existing bounded HTTP owner, without
        # replacing its SDK generator, policy, HTTP client or response.
        authorization = value.headers.get("Authorization")
        redactions.observe(authorization)
        if value.headers.get("Content-Type", "").startswith(
            "application/x-www-form-urlencoded"
        ):
            for key, values in parse_qs(value.content.decode()).items():
                if key in {"code", "code_verifier", "refresh_token", "client_secret"}:
                    for item in values:
                        redactions.observe(item)
        response = await original(config, value)
        with suppress(Exception):
            body = response.json()
            if isinstance(body, dict):
                for key in ("access_token", "refresh_token", "client_secret"):
                    if isinstance(body.get(key), str):
                        redactions.observe(body[key])
        return response

    service.oauth._request = request


def emit(kind, **facts):
    print(json.dumps({"type": kind, **facts}), flush=True)


def unused_port():
    with socket.socket() as descriptor:
        descriptor.bind(("127.0.0.1", 0))
        return descriptor.getsockname()[1]


class Bridge:
    def __init__(self):
        self.pending = {}
        self.request = 0
        self.closed = False
        self.service = None
        self.target = None
        self.transport = None
        self.reader_task = None

    async def start(self):
        reader = asyncio.StreamReader()
        self.transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
        )
        self.reader_task = asyncio.create_task(self.read(reader))

    async def read(self, reader):
        try:
            while line := await reader.readline():
                message = json.loads(line)
                if message.get("type") == "open-oauth-browser-result":
                    if set(message) != {
                        "type",
                        "requestId",
                        "opened",
                    } or not isinstance(message["opened"], bool):
                        raise ValueError("host result schema")
                    future = self.pending.get(message["requestId"])
                    if future is not None and not future.done():
                        future.set_result(message["opened"])
                    else:
                        emit("late-host-result-ignored")
                elif message == {"type": "cancel-login"}:
                    if self.service is not None:
                        await self.service.oauth.cancel(self.target.owner)
                else:
                    raise ValueError("host message is not OAuth-only")
        finally:
            self.closed = True
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError("host control pipe closed"))
            if self.service is not None:
                await self.service.aclose()
            emit("host-pipe-settled", pending_count=len(self.pending))

    async def open_browser(self, url):
        if self.closed:
            raise ConnectionError("host already closed")
        # Original manager has installed its listener before calling this method.
        self.request += 1
        identifier = str(self.request)
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        redactions.observe(url)
        query = parse_qs(urlsplit(url).query)
        for name in ("state", "code_challenge"):
            for value in query.get(name, ()):
                redactions.observe(value)
        try:
            # The only secret-bearing IPC field is the validated authorization URL.
            # Electron consumes this private frame and never forwards it to its log.
            emit("open-oauth-browser", requestId=identifier, url=url)
            if not await future:
                raise ValueError("host could not open OAuth browser")
        finally:
            self.pending.pop(identifier, None)

    async def finish(self):
        if self.transport is not None:
            self.transport.close()
        if self.reader_task is not None and not self.reader_task.done():
            self.reader_task.cancel()
        if self.reader_task is not None:
            with suppress(asyncio.CancelledError):
                await self.reader_task


class MockProvider:
    """Test authorization server only; client PKCE/state/grant remain SDK-owned."""

    def __init__(self, case, redirect_uri):
        self.case, self.redirect_uri = case, redirect_uri
        self.runner = None
        self.base = ""
        self.authorization_seen = asyncio.Event()
        self.token_gate = asyncio.Event()
        self.token_gate.set()
        if case == "reuse":
            self.token_gate.clear()
        self.challenge = None
        self.code = secrets.token_urlsafe(32)
        self.token = secrets.token_urlsafe(32)
        self.refresh = secrets.token_urlsafe(32)
        for value in (self.code, self.token, self.refresh):
            redactions.observe(value)
        self.token_requests = 0
        self.pkce_valid = None
        self.methods = []

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.serve)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def serve(self, request):
        self.methods.append((request.method, request.path))
        if request.path == "/mcp":
            return web.Response(
                status=401,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{self.base}/resource", scope="read"'
                },
            )
        if request.path == "/resource":
            return web.json_response(
                {
                    "resource": self.base + "/mcp",
                    "authorization_servers": [self.base],
                    "scopes_supported": ["read"],
                }
            )
        if request.path == "/.well-known/oauth-authorization-server":
            return web.json_response(
                {
                    "issuer": self.base,
                    "authorization_endpoint": self.base + "/authorize",
                    "token_endpoint": self.base + "/token",
                    "registration_endpoint": self.base + "/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "authorization_response_iss_parameter_supported": True,
                }
            )
        if request.path == "/register":
            metadata = await request.json()
            if metadata["redirect_uris"] != [self.redirect_uri]:
                raise ValueError("registration callback mismatch")
            return web.json_response(
                {**metadata, "client_id": "g0-local-client"}, status=201
            )
        if request.path == "/authorize":
            query = request.query
            if query.get("code_challenge_method") != "S256":
                raise ValueError("PKCE method missing")
            if query.get("redirect_uri") != self.redirect_uri:
                raise ValueError("authorization callback mismatch")
            self.challenge = query["code_challenge"]
            self.authorization_seen.set()
            emit("mock-authorization-visited", case=self.case, pkce_method="S256")
            if self.case in ("cancel", "pipe-loss"):
                return web.Response(
                    text="Local OAuth test is waiting for cancellation.",
                    content_type="text/html",
                )
            parameters = {
                "code": self.code,
                "state": "deliberately-wrong"
                if self.case == "state"
                else query["state"],
                "iss": self.base + "/wrong" if self.case == "issuer" else self.base,
            }
            callback = self.redirect_uri + "?" + urlencode(parameters)
            if self.case == "reuse":
                # Two browser navigations while token exchange is held; no code enters host IPC.
                import html

                escaped = html.escape(callback, quote=True)
                return web.Response(
                    text=f'<!doctype html><title>Local OAuth replay test</title><iframe src="{escaped}"></iframe><iframe src="{escaped}"></iframe>',
                    content_type="text/html",
                )
            raise web.HTTPFound(callback)
        if request.path == "/token":
            self.token_requests += 1
            form = await request.post()
            calculated = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(form["code_verifier"].encode()).digest()
                )
                .decode()
                .rstrip("=")
            )
            self.pkce_valid = calculated == self.challenge
            if (
                form.get("code") != self.code
                or form.get("redirect_uri") != self.redirect_uri
                or not self.pkce_valid
            ):
                return web.json_response({"error": "invalid_grant"}, status=400)
            await self.token_gate.wait()
            if self.case == "pkce":
                return web.json_response({"error": "invalid_grant"}, status=400)
            return web.json_response(
                {
                    "access_token": self.token,
                    "refresh_token": self.refresh,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "read",
                }
            )
        return web.Response(status=404)

    async def close(self):
        self.token_gate.set()
        if self.runner is not None:
            await self.runner.cleanup()


async def mock_case(bridge, name):
    directory = HOME / name
    directory.mkdir(mode=0o700)
    redirect_uri = f"http://127.0.0.1:{unused_port()}/callback"
    provider = MockProvider(name, redirect_uri)
    await provider.start()
    settings = LocalSettingsStore(directory / "local-settings.yaml")
    service = LocalMcpManagementService(
        settings,
        user_config_path=directory / "mcp.yaml",
        open_browser=bridge.open_browser,
    )
    target = LocalMcpTarget("g0-oauth")
    bridge.service, bridge.target = service, target
    await service.create(
        target,
        {
            "transport": {
                "type": "streamable_http",
                "endpoint": provider.base + "/mcp",
                "allow_http_localhost": True,
            },
            "auth": {"type": "oauth", "redirect_uri": redirect_uri},
        },
    )
    observe_private_exchange(service)
    callback_statuses = []
    callback_outcomes = []
    response_seen = asyncio.Event()
    original_response = oauth_module.oauth_callback_response

    def response_observer(*args, **kwargs):
        response = original_response(*args, **kwargs)
        callback_statuses.append(response.status)
        callback_outcomes.append(kwargs.get("outcome", "error"))
        response_seen.set()
        return response

    oauth_module.oauth_callback_response = response_observer
    blocker = None
    if name == "port-conflict":
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", urlsplit(redirect_uri).port))
        blocker.listen()
    emit("case-start", case=name)
    flow = None
    try:
        flow = await service.authorize(target)
        if name == "state":
            await asyncio.wait_for(response_seen.wait(), 25)
            if callback_statuses != [400]:
                raise AssertionError("wrong state accepted")
            await service.oauth.cancel(target.owner)
        elif name == "reuse":
            await asyncio.wait_for(response_seen.wait(), 25)
            if 400 not in callback_statuses:
                raise AssertionError("callback replay not rejected")
            provider.token_gate.set()
        with suppress(asyncio.CancelledError, ValueError):
            await asyncio.wait_for(asyncio.shield(flow.task), 30)
        await service.aclose()
        grant = settings.read().mcp_authorization(target.owner)
        if name in ("success", "reuse"):
            if (
                flow.state != "authorized"
                or grant is None
                or provider.pkce_valid is not True
            ):
                raise AssertionError("positive SDK OAuth did not settle")
        else:
            if grant is not None or flow.state not in ("failed", "cancelled"):
                raise AssertionError("negative OAuth stored a grant")
        if (
            name in ("state", "issuer", "cancel", "pipe-loss", "port-conflict")
            and provider.token_requests != 0
        ):
            raise AssertionError("invalid callback reached token exchange")
        if name == "pkce" and not (
            provider.token_requests == 1 and provider.pkce_valid
        ):
            raise AssertionError("PKCE error path was not reached")
        if flow.listener is not None or bridge.pending:
            raise AssertionError("listener or request remained")
        if blocker is not None:
            blocker.close()
            blocker = None
        emit(
            "case-result",
            case=name,
            state=flow.state,
            grant_present=grant is not None,
            callback_statuses=callback_statuses,
            callback_outcomes=callback_outcomes,
            token_requests=provider.token_requests,
            pkce_valid=provider.pkce_valid,
            listener_closed=True,
            pending_count=len(bridge.pending),
            callback_origin=f"http://127.0.0.1:{urlsplit(redirect_uri).port}",
        )
    finally:
        provider.token_gate.set()
        await service.aclose()
        oauth_module.oauth_callback_response = original_response
        if blocker is not None:
            blocker.close()
        await provider.close()
        bridge.service = None


async def main():
    bridge = Bridge()
    await bridge.start()
    emit(
        "guest-ready",
        mode=MODE,
        home=str(HOME),
        uid=os.getuid(),
        device=HOME.stat().st_dev,
        sdk=importlib.metadata.version("mcp"),
    )
    try:
        if MODE == "real":
            raise RuntimeError("real authorization awaits root/user readiness")
        for name in (
            "success",
            "state",
            "issuer",
            "pkce",
            "reuse",
            "cancel",
            "port-conflict",
            "pipe-loss",
        ):
            await mock_case(bridge, name)
        emit("matrix-complete", cases=8)
    finally:
        if bridge.service is not None:
            await bridge.service.aclose()
        await bridge.finish()
        private_diagnostics.flush()
        raw = (HOME / "diagnostics.private").read_text(errors="replace")
        safe = redactions.scrub_text(raw)
        if redactions.contains(safe):
            raise AssertionError("diagnostic scrub failed")
        if safe:
            emit("guest-diagnostics", text=safe)
        sys.stderr = original_stderr
        private_diagnostics.close()
        if (
            HOME.resolve().parent != Path("/home/plumwsl")
            or HOME.stat().st_uid != os.getuid()
        ):
            raise AssertionError("cleanup scope changed")
        shutil.rmtree(HOME)
        emit("guest-cleaned", home=str(HOME), removed=not HOME.exists())


try:
    asyncio.run(main())
except BaseException as error:
    # Original owner already exposes a bounded public failure. Never print a secret-bearing SDK exception.
    emit("guest-failure", error_type=type(error).__name__)
    raise SystemExit(1)
