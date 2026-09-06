"""Official MCP SDK protocol/transports with Pulsara policy and process boundaries."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date
import ipaddress
import logging
import os
from pathlib import Path
import signal
import socket
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpx2
from mcp import ClientSession
import mcp_types as types
from mcp_types.version import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    LATEST_MODERN_VERSION,
)
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

from pulsara_agent.mcp_config import (
    ExactAbsoluteMcpCwd,
    LegacySseTransportConfig,
    McpHttpNetworkPolicy,
    McpServerConfig,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    WorkspaceRelativeMcpCwd,
)
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialBoundMcpClient,
)

from .wire import (
    DEFAULT_MCP_WIRE_BOUNDS,
    McpWireBoundExceeded,
    McpWireBounds,
    bounded_json_loads,
    result_type_presence,
)


NotificationCallback = Callable[[str], Awaitable[None]]

_private_sdk_wire = ContextVar("pulsara_mcp_private_sdk_wire", default=False)


class _SdkWireLogFilter(logging.Filter):
    def filter(self, record):
        # SDK trace/validation logs may contain raw credential-bearing carriers.
        # Product diagnostics are emitted after the exact secret scrub boundary.
        return not _private_sdk_wire.get()


for _logger_name in ("mcp.client.sse", "mcp.client.streamable_http"):
    logging.getLogger(_logger_name).addFilter(_SdkWireLogFilter())


@dataclass(frozen=True, slots=True)
class McpWireResultPresence:
    present: bool
    value: str | None


@dataclass(frozen=True, slots=True)
class McpAdvertisedCapabilities:
    """Closed listing surface frozen from the negotiated SDK result."""

    tools: bool
    resources: bool
    prompts: bool


@dataclass(frozen=True, slots=True)
class _PinnedHttpEndpoint:
    """One DNS-validated physical target with the logical authority preserved."""

    url: str
    host_header: str
    sni_hostname: str | None


class McpProtocolConformanceError(ValueError):
    """A bounded peer response violated the negotiated closed MCP contract."""


class McpTransportOperationError(RuntimeError):
    """Secret-safe physical transport settlement for one request path."""

    def __init__(self, *, may_have_reached_server: bool) -> None:
        self.may_have_reached_server = may_have_reached_server
        super().__init__(
            "MCP_TRANSPORT_OUTCOME_UNKNOWN"
            if may_have_reached_server
            else "MCP_TRANSPORT_UNWRITTEN"
        )


def _has_legacy_discovery_fallback_evidence(message: str) -> bool:
    """Recognize an HTTP pre-validation rejection that proves a legacy era."""

    if message == "Bad Request: No valid session ID provided":
        return True
    prefixes = (
        "Bad Request: Unsupported protocol version: "
        f"{LATEST_MODERN_VERSION} (supported versions: ",
        "Bad Request: Unsupported protocol version (supported versions: ",
    )
    supported: str | None = None
    for prefix in prefixes:
        if message.startswith(prefix) and message.endswith(")"):
            supported = message[len(prefix) : -1]
            break
    if supported is None:
        return False
    versions = tuple(item.strip() for item in supported.split(","))
    if not versions or not all(versions):
        return False
    if not any(item in HANDSHAKE_PROTOCOL_VERSIONS for item in versions):
        return False
    if not all(
        len(item) == 10
        and item[4] == "-"
        and item[7] == "-"
        and (item[:4] + item[5:7] + item[8:]).isdigit()
        for item in versions
    ):
        return False
    try:
        parsed_versions = tuple(date.fromisoformat(item) for item in versions)
        modern_version = date.fromisoformat(LATEST_MODERN_VERSION)
    except ValueError:
        return False
    return all(item < modern_version for item in parsed_versions)


def _normalize_legacy_discovery_http_error(
    raw: object,
    *,
    request: dict[str, object],
    response_status: int,
    has_session_id: bool,
) -> object:
    """Correlate a proved legacy-only HTTP rejection with its discovery request."""

    request_id = request.get("id")
    if (
        response_status != 400
        or has_session_id
        or request.get("method") != "server/discover"
        or not isinstance(request_id, str | int)
        or isinstance(request_id, bool)
        or not isinstance(raw, dict)
        or raw.get("id") is not None
    ):
        return raw
    error = raw.get("error")
    if not isinstance(error, dict):
        return raw
    message = error.get("message")
    if (
        error.get("code") != -32000
        or not isinstance(message, str)
        or not _has_legacy_discovery_fallback_evidence(message)
    ):
        return raw
    return {
        **raw,
        "id": request_id,
        "error": {
            **error,
            "code": types.METHOD_NOT_FOUND,
        },
    }


class _BoundedTransport:
    def __init__(self, bounds: McpWireBounds) -> None:
        self.bounds = bounds
        self.read_writer, self.read_stream = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](1)
        self.write_stream, self.write_reader = anyio.create_memory_object_stream[
            SessionMessage
        ](1)
        self.last_result_presence = McpWireResultPresence(False, None)
        self.enforce_closed_result_type = False
        self.allow_legacy_implicit_complete = False
        self._closed = False
        self._secret_values: tuple[str, ...] = ()

    async def start(self) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError

    def _decode(self, data: bytes | bytearray, *, maximum_bytes: int) -> SessionMessage:
        return self._decode_parsed(self._parse(data, maximum_bytes=maximum_bytes))

    def _parse(self, data: bytes | bytearray, *, maximum_bytes: int) -> object:
        try:
            parsed = bounded_json_loads(
                data,
                maximum_bytes=maximum_bytes,
                maximum_nodes=self.bounds.maximum_wire_json_nodes,
                maximum_depth=self.bounds.maximum_wire_json_depth,
            )
            return self._scrub(parsed)
        except McpProtocolConformanceError:
            raise
        except BaseException:
            # A peer frame is already physically present.  Malformed JSON,
            # shape overflow and SDK carrier validation are therefore exact
            # protocol failures, never evidence of an unknown remote effect.
            raise McpProtocolConformanceError("MCP_RESPONSE_CARRIER_INVALID") from None

    def _scrub(self, value: object, *, identity: bool = False) -> object:
        if isinstance(value, str):
            for secret in self._secret_values:
                if secret in value:
                    if identity:
                        raise McpProtocolConformanceError(
                            "MCP_RESPONSE_CONTAINS_CREDENTIAL_IN_IDENTITY"
                        )
                    value = value.replace(secret, "[credential removed]")
            return value
        if isinstance(value, list):
            return [self._scrub(item, identity=identity) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                self._scrub(key, identity=True)
                result[key] = self._scrub(
                    item,
                    identity=identity
                    or key
                    in {
                        "id",
                        "name",
                        "method",
                        "inputSchema",
                        "outputSchema",
                        "uri",
                        "uriTemplate",
                    },
                )
            return result
        return value

    def _decode_parsed(self, raw: object) -> SessionMessage:
        try:
            present, value = result_type_presence(raw)
            self.last_result_presence = McpWireResultPresence(present, value)
            if (
                self.enforce_closed_result_type
                and isinstance(raw, dict)
                and "result" in raw
            ):
                if present:
                    if value not in {"complete", "input_required"}:
                        raise McpProtocolConformanceError(
                            "MCP_RESULT_TYPE_CONFORMANCE_FAILED"
                        )
                elif not self.allow_legacy_implicit_complete:
                    raise McpProtocolConformanceError(
                        "MCP_RESULT_TYPE_CONFORMANCE_FAILED"
                    )
            message = types.jsonrpc_message_adapter.validate_python(raw)
            return SessionMessage(message)
        except McpProtocolConformanceError:
            raise
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise McpProtocolConformanceError("MCP_RESPONSE_CARRIER_INVALID") from exc

    def _encode(self, value: SessionMessage) -> bytes:
        data = value.message.model_dump_json(by_alias=True, exclude_none=True).encode(
            "utf-8"
        )
        if len(data) > self.bounds.maximum_stdio_frame_bytes:
            raise McpWireBoundExceeded("outbound MCP frame exceeds the byte bound")
        return data

    async def _offer_failure(self, exc: BaseException, fallback: str) -> None:
        if self._closed:
            return
        value = exc if isinstance(exc, Exception) else RuntimeError(fallback)
        with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            await self.read_writer.send(value)


class _BoundedStdioTransport(_BoundedTransport):
    def __init__(
        self,
        config: StdioTransportConfig,
        *,
        workspace_root: Path,
        credential_boundary: ProcessCredentialBoundary,
        bounds: McpWireBounds,
        secret_resolver=None,
    ) -> None:
        super().__init__(bounds)
        self._config = config
        self._secret_resolver = secret_resolver
        self._workspace_root = workspace_root
        self._credential_boundary = credential_boundary
        self._process: asyncio.subprocess.Process | None = None
        self._tasks: tuple[asyncio.Task[object], ...] = ()
        self._closed = False

    async def start(self) -> None:
        binding = self._config.cwd
        if isinstance(binding, WorkspaceRelativeMcpCwd):
            candidate = (self._workspace_root / binding.relative_path).resolve()
            try:
                candidate.relative_to(self._workspace_root)
            except ValueError as exc:
                raise ValueError("MCP stdio cwd escapes the workspace") from exc
            cwd = candidate
        elif isinstance(binding, ExactAbsoluteMcpCwd):
            cwd = binding.absolute_path
        else:  # pragma: no cover - closed dataclass validation
            raise TypeError("MCP stdio cwd binding union is open")
        environment = {
            key: value
            for key in ("HOME", "LANG", "LC_ALL", "LOGNAME", "TMPDIR", "USER")
            if (value := os.environ.get(key)) is not None
        }
        environment["PATH"] = self._config.lookup_path
        environment.update(dict(self._config.environment))
        from pulsara_agent.mcp_credentials import resolved_secret_values

        for target, reference in self._config.secret_environment:
            values = resolved_secret_values(reference, self._secret_resolver)
            environment[target] = values[0]
            self._secret_values += values
        process: asyncio.subprocess.Process | None = None
        cancelled: asyncio.CancelledError | None = None
        async with self._credential_boundary.async_guard() as guard:
            if (
                guard.contains(self._config.command)
                or any(guard.contains(item) for item in self._config.args)
                or guard.contains(str(cwd))
                or any(
                    guard.contains(name) or guard.contains(value)
                    for name, value in environment.items()
                )
            ):
                raise ValueError("MCP stdio admission contains a caller credential")
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    self._config.command,
                    *self._config.args,
                    cwd=str(cwd),
                    env=environment,
                    limit=self.bounds.maximum_stdio_frame_bytes + 1,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                ),
                name="mcp-stdio-spawn-admission",
            )
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError as exc:
                cancelled = exc
                process = await asyncio.shield(spawn)
        if cancelled is not None:
            assert process is not None
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            raise cancelled
        self._process = process
        self._tasks = (
            asyncio.create_task(self._reader(), name="mcp-stdio-reader"),
            asyncio.create_task(self._writer(), name="mcp-stdio-writer"),
            asyncio.create_task(self._stderr(), name="mcp-stdio-stderr"),
        )

    async def _reader(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                frame = await self._process.stdout.readline()
                if not frame:
                    if not self._closed:
                        # EOF is a physical transport failure, not a successful
                        # reader settlement.  Closing the exact receive stream
                        # wakes ClientSession and lets the supervisor fence and
                        # reconnect this slot generation.
                        await self._offer_failure(
                            McpTransportOperationError(may_have_reached_server=True),
                            "MCP stdio peer closed stdout",
                        )
                        await self.read_writer.aclose()
                    return
                if len(frame) > self.bounds.maximum_stdio_frame_bytes:
                    raise McpProtocolConformanceError("MCP_RESPONSE_CARRIER_INVALID")
                await self.read_writer.send(
                    self._decode(
                        frame.rstrip(b"\r\n"),
                        maximum_bytes=self.bounds.maximum_stdio_frame_bytes,
                    )
                )
        except BaseException as exc:
            await self._offer_failure(exc, "MCP reader cancelled")

    async def _writer(self) -> None:
        assert self._process is not None and self._process.stdin is not None
        async with self.write_reader:
            async for message in self.write_reader:
                try:
                    payload = self._encode(message) + b"\n"
                except BaseException:
                    await self._offer_failure(
                        McpTransportOperationError(may_have_reached_server=False),
                        "MCP stdio encode failed",
                    )
                    return
                try:
                    self._process.stdin.write(payload)
                    await self._process.stdin.drain()
                except BaseException:
                    await self._offer_failure(
                        McpTransportOperationError(may_have_reached_server=True),
                        "MCP stdio write failed",
                    )
                    return

    async def _stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            while True:
                chunk = await self._process.stderr.read(4096)
                if not chunk:
                    return
                # stderr is intentionally discarded and never exposed.  Bound
                # the resident read size, not lifetime throughput: a healthy
                # long-running server may emit more than 32 MiB over its life
                # without ever retaining those bytes in Pulsara.
                if len(chunk) > 4096:  # pragma: no cover - StreamReader contract
                    raise McpWireBoundExceeded("MCP stderr chunk exceeds its bound")
        except BaseException as exc:
            if self._closed:
                return
            # stderr is deliberately not retained or logged.  Crossing its
            # physical buffer contract is a slot failure, so wake the public
            # session and stop the exact process group rather than leaving a
            # silent failed reader beside a live child.
            with suppress(ProcessLookupError):
                os.killpg(self._process.pid, signal.SIGTERM)
            await self._offer_failure(exc, "MCP stderr reader cancelled")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.write_stream.aclose()
        process = self._process
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
                with suppress(BrokenPipeError, ConnectionResetError):
                    await process.stdin.wait_closed()
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        # Give stdout/stderr readers the process EOF before cancellation.  An
        # immediate cancel after wait can leave asyncio pipe transports pending
        # until the event loop is already closed.
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=1)
        else:
            pending = set()
        for task in pending:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._secret_values = ()
        await self.read_writer.aclose()


class _McpHttpClient(ProcessCredentialBoundMcpClient):
    """HTTP policy/credentials only; SDK owns MCP and SSE protocol behavior."""

    def __init__(
        self,
        transport_config,
        *,
        bounds,
        credential_boundary,
        config=None,
        observe_secrets=None,
        **kwargs,
    ):
        follow_redirects = kwargs.pop("follow_redirects", False)
        super().__init__(
            credential_boundary=credential_boundary,
            trust_env=False,
            follow_redirects=follow_redirects,
            **kwargs,
        )
        self.transport_config = transport_config
        self.bounds = bounds
        self.config = config
        self.observe_secrets = observe_secrets
        self.sessionful = False
        self.static_headers = {}
        if config is not None:
            from pulsara_agent.mcp_config import OAuthAuthorization

            if isinstance(config.auth, OAuthAuthorization):
                self.static_headers = dict(config.public_headers)
            else:
                self.static_headers, secrets = config.resolve_header_snapshot()
                if observe_secrets is not None:
                    observe_secrets(secrets)

    async def _send_single_request(self, request):
        from pulsara_agent.mcp_config import OAuthAuthorization
        from pulsara_agent.mcp_credentials import McpCredentialMissing

        if self.config is not None:
            if _http_origin(str(request.url)) != _http_origin(
                self.transport_config.endpoint
            ):
                raise ValueError("MCP credential destination changed")
            request.headers.update(self.static_headers)
            if isinstance(self.config.auth, OAuthAuthorization):
                if self.config.authorization_provider is None:
                    raise McpCredentialMissing()
                authorization, secrets = await self.config.authorization_provider()
                self.observe_secrets(secrets)
                request.headers["Authorization"] = authorization
        content = await request.aread()
        if len(content) > self.bounds.maximum_http_json_body_bytes:
            raise McpTransportOperationError(may_have_reached_server=False)
        pinned = await _enforce_http_network_policy(
            replace(self.transport_config, endpoint=str(request.url))
        )
        headers = dict(request.headers)
        headers["host"] = pinned.host_header
        physical = httpx2.Request(
            request.method,
            pinned.url,
            headers=headers,
            content=content,
            extensions={**request.extensions, **_http_request_extensions(pinned)},
        )
        response = await super()._send_single_request(physical)
        response.request = request  # Logical origin for SDK URLs and HTTP redirects.
        self.sessionful |= bool(response.headers.get("mcp-session-id"))
        if "text/event-stream" in response.headers.get("content-type", "").lower():
            return response
        # HTTPX2 owns decompression; bound the decoded body before SDK materializes
        # its RPC carrier. This is not a claim about decoder-internal allocations.
        try:
            body = await _bounded_aread(
                response, self.bounds.maximum_http_json_body_bytes
            )
            if response.status_code == 400 and content:
                import json

                try:
                    raw_request = json.loads(content)
                    raw = json.loads(body)
                except (ValueError, UnicodeError):
                    pass
                else:
                    normalized = _normalize_legacy_discovery_http_error(
                        raw,
                        request=raw_request,
                        response_status=400,
                        has_session_id=bool(response.headers.get("mcp-session-id")),
                    )
                    if normalized is not raw:
                        body = json.dumps(normalized).encode()
            result_headers = dict(response.headers)
            result_headers.pop("content-encoding", None)
            result_headers.pop("content-length", None)
            return httpx2.Response(
                response.status_code,
                headers=result_headers,
                content=bytes(body),
                request=request,
                extensions=response.extensions,
            )
        finally:
            await response.aclose()

    def _build_redirect_request(self, request, response):
        redirected = super()._build_redirect_request(request, response)
        if request.method not in {"GET", "HEAD"} and (
            _http_origin(str(request.url)) != _http_origin(str(redirected.url))
        ):
            raise ValueError("OAuth credential-bearing redirect changes origin")
        return redirected


def _http_origin(url):
    parsed = urlsplit(url)
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


class _SdkHttpTransport(_BoundedTransport):
    """Single task owns official transport contexts; no HTTP/SSE state machine."""

    def __init__(self, config, transport, *, credential_boundary, bounds):
        super().__init__(bounds)
        self.config = config
        self.transport_config = transport
        self.credential_boundary = credential_boundary
        self._stop = asyncio.Event()
        self._ready = asyncio.get_running_loop().create_future()
        self._owner = None
        self._client = None

    @property
    def sessionful(self):
        return isinstance(self.transport_config, LegacySseTransportConfig) or (
            self._client is not None and self._client.sessionful
        )

    def _observe_secrets(self, secrets):
        self._secret_values = tuple(dict.fromkeys((*self._secret_values, *secrets)))

    async def start(self):
        self._owner = asyncio.create_task(self._run(), name="mcp-sdk-http-owner")
        await asyncio.shield(self._ready)

    async def _run(self):
        from contextlib import asynccontextmanager
        from mcp.client.streamable_http import streamable_http_client
        from mcp.client.sse import sse_client

        private_context = _private_sdk_wire.set(True)
        try:
            async with _McpHttpClient(
                self.transport_config,
                bounds=self.bounds,
                credential_boundary=self.credential_boundary,
                config=self.config,
                observe_secrets=self._observe_secrets,
                timeout=httpx2.Timeout(connect=10, write=10, pool=10, read=None),
            ) as client:
                self._client = client

                @asynccontextmanager
                async def factory(**kwargs):
                    # The injected client is owned here, not by the SDK factory.
                    yield client

                context = (
                    sse_client(
                        self.transport_config.endpoint, httpx_client_factory=factory
                    )
                    if isinstance(self.transport_config, LegacySseTransportConfig)
                    else streamable_http_client(
                        self.transport_config.endpoint, http_client=client
                    )
                )
                async with context as (read, write):
                    async with anyio.create_task_group() as group:
                        group.start_soon(self._read_sdk_messages, read)
                        group.start_soon(self._forward_requests, write)
                        self._ready.set_result(None)
                        await self._stop.wait()
                        group.cancel_scope.cancel()
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            elif not self._closed:
                await self._offer_failure(
                    McpTransportOperationError(may_have_reached_server=True),
                    "MCP SDK transport failed",
                )
        finally:
            _private_sdk_wire.reset(private_context)
            await self.read_writer.aclose()

    async def _read_sdk_messages(self, read):
        from .wire import validate_json_shape

        async for message in read:
            if isinstance(message, Exception):
                await self._offer_failure(
                    McpProtocolConformanceError("MCP_RESPONSE_CARRIER_INVALID"),
                    "MCP SDK carrier invalid",
                )
                continue
            try:
                raw = message.message.model_dump(
                    by_alias=True, mode="json", exclude_unset=True
                )
                validate_json_shape(
                    raw,
                    maximum_nodes=self.bounds.maximum_wire_json_nodes,
                    maximum_depth=self.bounds.maximum_wire_json_depth,
                )
                await self.read_writer.send(self._decode_parsed(self._scrub(raw)))
            except (ValueError, TypeError):
                await self._offer_failure(
                    McpProtocolConformanceError("MCP_RESPONSE_CARRIER_INVALID"),
                    "MCP SDK carrier invalid",
                )

    async def _forward_requests(self, write):
        async with self.write_reader:
            async for message in self.write_reader:
                await write.send(message)

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._owner is not None:
            if not self._ready.done():
                self._owner.cancel()
            await asyncio.shield(self._owner)
            if self._ready.done() and not self._ready.cancelled():
                self._ready.exception()
        self._secret_values = ()
        await self.write_stream.aclose()


async def _bounded_aread(
    response: httpx2.Response,
    maximum: int,
) -> bytearray:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > maximum:
            raise McpWireBoundExceeded("MCP HTTP body exceeds the bound")
        body.extend(chunk)
    return body


def _http_request_extensions(endpoint: _PinnedHttpEndpoint) -> dict[str, object]:
    return (
        {"sni_hostname": endpoint.sni_hostname}
        if endpoint.sni_hostname is not None
        else {}
    )


async def _enforce_http_network_policy(
    transport: StreamableHttpTransportConfig,
) -> _PinnedHttpEndpoint:
    """Resolve, validate, and pin the exact address used by HTTPX."""

    parsed = urlsplit(transport.endpoint)
    host = parsed.hostname
    if host is None:
        raise ValueError("MCP HTTP endpoint has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            ),
            timeout=10,
        )
    except TimeoutError as exc:
        raise RuntimeError("MCP_HTTP_DNS_TIMEOUT") from exc
    if not addresses:
        raise RuntimeError("MCP_HTTP_DNS_EMPTY")
    resolved = frozenset(
        ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in addresses
    )
    localhost = all(address.is_loopback for address in resolved)
    allowed = (
        (localhost and transport.allow_http_localhost)
        or transport.network_policy is McpHttpNetworkPolicy.ALLOW_PRIVATE
        or all(address.is_global for address in resolved)
    )
    if not allowed:
        raise ValueError("MCP_HTTP_PRIVATE_NETWORK_DENIED")
    selected = sorted(resolved, key=lambda item: (item.version, item.packed))[0]
    pinned_host = f"[{selected}]" if selected.version == 6 else str(selected)
    explicit_port = parsed.port
    pinned_netloc = (
        f"{pinned_host}:{explicit_port}" if explicit_port is not None else pinned_host
    )
    logical_host = f"[{host}]" if ":" in host else host
    host_header = (
        f"{logical_host}:{explicit_port}" if explicit_port is not None else logical_host
    )
    return _PinnedHttpEndpoint(
        url=urlunsplit(
            (parsed.scheme, pinned_netloc, parsed.path, parsed.query, parsed.fragment)
        ),
        host_header=host_header,
        sni_hostname=host if parsed.scheme == "https" else None,
    )


class BoundedMcpSdkClient:
    """One official ClientSession with bounded physical transport ownership."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        workspace_root: Path,
        notification_callback: NotificationCallback,
        credential_boundary: ProcessCredentialBoundary,
        bounds: McpWireBounds = DEFAULT_MCP_WIRE_BOUNDS,
    ) -> None:
        self.config = config
        self._workspace_root = workspace_root
        self._notification_callback = notification_callback
        self._credential_boundary = credential_boundary
        self._bounds = bounds
        self._transport: _BoundedTransport | None = None
        self._session: ClientSession | None = None
        self._closed = False
        self._close_lock = asyncio.Lock()
        self.protocol_version = ""
        self.server_name = config.display_name
        self.server_instructions = ""
        self.advertised_capabilities: McpAdvertisedCapabilities | None = None
        self._allow_legacy_implicit_complete = False
        self._owner_task = None
        self._session_ready = None
        self._session_stop = asyncio.Event()

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCP SDK client is not open")
        return self._session

    @property
    def last_result_presence(self) -> McpWireResultPresence:
        if self._transport is None:
            return McpWireResultPresence(False, None)
        return self._transport.last_result_presence

    @property
    def uses_legacy_initialize(self) -> bool:
        """Whether this connection explicitly negotiated the legacy era."""

        return self._allow_legacy_implicit_complete

    @property
    def supports_bounded_stateless_parallelism(self) -> bool:
        """Prove the physical HTTP session is stateless after negotiation.

        Configuration is only an operator assertion.  A peer that returns an
        MCP session identity contradicts it and must stay on the serial lane.
        stdio is always sessionful for concurrency purposes.
        """

        return (
            isinstance(self._transport, _SdkHttpTransport)
            and self.config.supports_parallel_tool_calls
            and isinstance(self.config.transport, StreamableHttpTransportConfig)
            and self.config.transport.proved_stateless
            and not self._transport.sessionful
        )

    async def open(self) -> None:
        if self._owner_task is not None:
            raise RuntimeError("MCP SDK client was opened twice")
        self._session_ready = asyncio.get_running_loop().create_future()
        self._owner_task = asyncio.create_task(
            self._run_session(), name="mcp-sdk-session-owner"
        )
        try:
            await asyncio.shield(self._session_ready)
        except BaseException:
            await self.aclose()
            raise

    async def _run_session(self):
        try:
            await self._open_session()
            self._session_ready.set_result(None)
            await self._session_stop.wait()
        except BaseException as exc:
            if not self._session_ready.done():
                self._session_ready.set_exception(exc)
        finally:
            await self._close_resources()

    async def _open_session(self) -> None:
        if self._session is not None:
            raise RuntimeError("MCP SDK client was opened twice")
        transport_config = self.config.transport
        if isinstance(transport_config, StdioTransportConfig):
            transport: _BoundedTransport = _BoundedStdioTransport(
                transport_config,
                workspace_root=self._workspace_root,
                credential_boundary=self._credential_boundary,
                bounds=self._bounds,
                secret_resolver=self.config.secret_resolver,
            )
        else:
            transport = _SdkHttpTransport(
                self.config,
                transport_config,
                credential_boundary=self._credential_boundary,
                bounds=self._bounds,
            )
        self._transport = transport
        try:
            await transport.start()
            self._session = ClientSession(
                transport.read_stream,
                transport.write_stream,
                read_timeout_seconds=None,
                message_handler=self._handle_notification,
            )
            await self._session.__aenter__()
            try:
                result = await self._session.discover()
                if self.require_closed_result_type(result) != "complete":
                    raise RuntimeError("MCP discover cannot require input")
                self.protocol_version = self._session.protocol_version or ""
                info = self._session.server_info
                if info is not None:
                    self.server_name = info.name
                self.server_instructions = getattr(result, "instructions", "") or ""
                transport.enforce_closed_result_type = True
            except MCPError as modern_error:
                # MCP SDK 1.x FastMCP peers reject the MCP 2.x discover request
                # with either METHOD_NOT_FOUND or INVALID_PARAMS.  Only a
                # successful legacy initialize adopts the compatibility era;
                # every other discover/initialize fault remains visible.
                if modern_error.code not in {-32601, -32602}:
                    raise
                result = await self._session.initialize()
                self.protocol_version = result.protocol_version
                self.server_name = result.server_info.name
                self.server_instructions = result.instructions or ""
                self._allow_legacy_implicit_complete = True
                transport.allow_legacy_implicit_complete = True
                transport.enforce_closed_result_type = True
            capabilities = self._session.server_capabilities
            if capabilities is None:
                raise McpProtocolConformanceError("MCP_SERVER_CAPABILITIES_MISSING")
            self.advertised_capabilities = McpAdvertisedCapabilities(
                tools=capabilities.tools is not None,
                resources=capabilities.resources is not None,
                prompts=capabilities.prompts is not None,
            )
        except BaseException:
            raise

    async def _handle_notification(self, message: object) -> None:
        if isinstance(message, Exception):
            await self._notification_callback(
                "pulsara/protocol_conformance_failure"
                if isinstance(message, McpProtocolConformanceError)
                else "pulsara/transport_failure"
            )
            return
        payload = message.model_dump(by_alias=True, mode="json", exclude_none=True)
        method = str(payload.get("method", ""))
        if method in {
            "notifications/tools/list_changed",
            "notifications/resources/list_changed",
            "notifications/prompts/list_changed",
        }:
            await self._notification_callback(method)

    def require_closed_result_type(self, result: object | None = None) -> str:
        if result is None:
            presence = self.last_result_presence
        else:
            fields_set = getattr(result, "model_fields_set", frozenset())
            result_value = getattr(result, "result_type", None)
            presence = McpWireResultPresence(
                "result_type" in fields_set,
                result_value if isinstance(result_value, str) else None,
            )
        implicit_complete = (
            not presence.present and self._allow_legacy_implicit_complete
        )
        if implicit_complete:
            result_value = getattr(result, "result_type", None)
            if isinstance(result, types.InputRequiredResult) or result_value not in {
                None,
                "complete",
            }:
                raise McpProtocolConformanceError(
                    "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
                )
            effective_value = "complete"
        elif not presence.present or presence.value not in {
            "complete",
            "input_required",
        }:
            raise McpProtocolConformanceError("MCP_RESULT_TYPE_CONFORMANCE_FAILED")
        else:
            effective_value = presence.value
        result_value = getattr(result, "result_type", None)
        if (
            result is not None
            and not implicit_complete
            and result_value != effective_value
        ):
            raise McpProtocolConformanceError("MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION")
        if (
            effective_value == "input_required"
            and result is not None
            and not isinstance(result, types.InputRequiredResult)
        ) or (
            effective_value == "complete"
            and isinstance(result, types.InputRequiredResult)
        ):
            raise McpProtocolConformanceError("MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION")
        return effective_value

    async def aclose(self) -> None:
        self._session_stop.set()
        if self._owner_task is not None:
            if not self._session_ready.done():
                self._owner_task.cancel()
            await asyncio.shield(self._owner_task)
            if self._session_ready.done() and not self._session_ready.cancelled():
                self._session_ready.exception()

    async def _close_resources(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            # The transport is the physical request/process owner.  Close it
            # first so in-flight public ClientSession calls receive terminal I/O
            # and can leave before the SDK task group is joined.  _closed is
            # published only after the physical join, so concurrent close
            # callers wait for this same owner instead of returning early.
            if self._transport is not None:
                await self._transport.aclose()
            if self._session is not None:
                with suppress(BaseException):
                    await self._session.__aexit__(None, None, None)
            self._closed = True


__all__ = [
    "BoundedMcpSdkClient",
    "MCPError",
    "McpAdvertisedCapabilities",
    "McpProtocolConformanceError",
    "McpTransportOperationError",
    "McpWireResultPresence",
    "types",
]
