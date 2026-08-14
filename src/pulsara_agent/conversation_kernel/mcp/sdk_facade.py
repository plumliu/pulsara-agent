"""Official MCP SDK session over Pulsara-owned bounded framing transports."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import signal
import socket
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpx
from mcp import ClientSession
import mcp_types as types
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

from pulsara_agent.mcp_config import (
    McpHttpNetworkPolicy,
    McpServerConfig,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
)

from .wire import (
    DEFAULT_MCP_WIRE_BOUNDS,
    McpWireBoundExceeded,
    McpWireBounds,
    bounded_json_loads,
    result_type_presence,
)


NotificationCallback = Callable[[str], Awaitable[None]]


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


class _SlotByteBudget:
    """Slot-local reservation for concurrently retained HTTP wire bytes."""

    __slots__ = ("maximum", "used")

    def __init__(self, maximum: int) -> None:
        if maximum <= 0:
            raise ValueError("MCP slot byte budget must be positive")
        self.maximum = maximum
        self.used = 0

    def reserve(self, amount: int) -> None:
        if amount < 0:
            raise ValueError("MCP slot byte reservation cannot be negative")
        if self.used + amount > self.maximum:
            raise McpWireBoundExceeded(
                "MCP concurrent transport buffers exceed the slot bound"
            )
        self.used += amount

    def release(self, amount: int) -> None:
        if amount < 0 or amount > self.used:
            raise RuntimeError("MCP slot byte reservation settlement conflicts")
        self.used -= amount


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
        self._closed = False

    async def start(self) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError

    def _decode(
        self, data: bytes | bytearray, *, maximum_bytes: int
    ) -> SessionMessage:
        try:
            raw = bounded_json_loads(
                data,
                maximum_bytes=maximum_bytes,
                maximum_nodes=self.bounds.maximum_wire_json_nodes,
                maximum_depth=self.bounds.maximum_wire_json_depth,
            )
            present, value = result_type_presence(raw)
            self.last_result_presence = McpWireResultPresence(present, value)
            if (
                self.enforce_closed_result_type
                and isinstance(raw, dict)
                and "result" in raw
                and (not present or value not in {"complete", "input_required"})
            ):
                raise McpProtocolConformanceError(
                    "MCP_RESULT_TYPE_CONFORMANCE_FAILED"
                )
            message = types.jsonrpc_message_adapter.validate_python(raw)
        except McpProtocolConformanceError:
            raise
        except BaseException as exc:
            # A peer frame is already physically present.  Malformed JSON,
            # shape overflow and SDK carrier validation are therefore exact
            # protocol failures, never evidence of an unknown remote effect.
            raise McpProtocolConformanceError(
                "MCP_RESPONSE_CARRIER_INVALID"
            ) from exc
        return SessionMessage(message)

    def _encode(self, value: SessionMessage) -> bytes:
        data = value.message.model_dump_json(
            by_alias=True, exclude_none=True
        ).encode("utf-8")
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
        bounds: McpWireBounds,
    ) -> None:
        super().__init__(bounds)
        self._config = config
        self._workspace_root = workspace_root
        self._process: asyncio.subprocess.Process | None = None
        self._tasks: tuple[asyncio.Task[object], ...] = ()
        self._closed = False

    async def start(self) -> None:
        cwd = self._workspace_root
        if self._config.cwd is not None:
            candidate = (self._workspace_root / self._config.cwd).resolve()
            try:
                candidate.relative_to(self._workspace_root)
            except ValueError as exc:
                raise ValueError("MCP stdio cwd escapes the workspace") from exc
            cwd = candidate
        environment = {
            key: value
            for key in ("HOME", "LANG", "LC_ALL", "LOGNAME", "PATH", "TMPDIR", "USER")
            if (value := os.environ.get(key)) is not None
        }
        environment.update(dict(self._config.environment))
        for target, reference in self._config.secret_environment_refs:
            value = os.environ.get(reference)
            if value is None:
                raise ValueError("MCP stdio secret environment reference is unavailable")
            environment[target] = value
        self._process = await asyncio.create_subprocess_exec(
            self._config.command,
            *self._config.args,
            cwd=str(cwd),
            env=environment,
            limit=self.bounds.maximum_stdio_frame_bytes + 1,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
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
                            McpTransportOperationError(
                                may_have_reached_server=True
                            ),
                            "MCP stdio peer closed stdout",
                        )
                        await self.read_writer.aclose()
                    return
                if len(frame) > self.bounds.maximum_stdio_frame_bytes:
                    raise McpProtocolConformanceError(
                        "MCP_RESPONSE_CARRIER_INVALID"
                    )
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
                        McpTransportOperationError(
                            may_have_reached_server=False
                        ),
                        "MCP stdio encode failed",
                    )
                    return
                try:
                    self._process.stdin.write(payload)
                    await self._process.stdin.drain()
                except BaseException:
                    await self._offer_failure(
                        McpTransportOperationError(
                            may_have_reached_server=True
                        ),
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
        await self.read_writer.aclose()


class _BoundedHttpTransport(_BoundedTransport):
    def __init__(
        self,
        config: McpServerConfig,
        transport: StreamableHttpTransportConfig,
        *,
        bounds: McpWireBounds,
    ) -> None:
        super().__init__(bounds)
        self._config = config
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._writer_task: asyncio.Task[object] | None = None
        self._listener_task: asyncio.Task[object] | None = None
        self._request_tasks: set[asyncio.Task[None]] = set()
        maximum_requests = (
            config.stateless_http_max_in_flight
            if transport.proved_stateless and config.supports_parallel_tool_calls
            else 1
        )
        self._request_lane = asyncio.Semaphore(maximum_requests)
        self._parallel_requests = maximum_requests > 1
        self._session_id: str | None = None
        self._endpoint: _PinnedHttpEndpoint | None = None
        self._byte_budget = _SlotByteBudget(
            bounds.maximum_buffered_transport_bytes_per_slot
        )
        self._closed = False

    @property
    def sessionful(self) -> bool:
        return self._session_id is not None

    async def start(self) -> None:
        self._endpoint = await _enforce_http_network_policy(self._transport)
        headers = self._config.resolved_headers()
        self._client = httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(connect=10, write=10, pool=10, read=None),
        )
        self._writer_task = asyncio.create_task(
            self._writer(), name=f"mcp-http-writer:{self._config.server_id}"
        )

    async def _writer(self) -> None:
        async with self.write_reader:
            async for message in self.write_reader:
                if not self._parallel_requests:
                    await self._send_message(message)
                    continue
                await self._request_lane.acquire()
                task = asyncio.create_task(
                    self._send_message(message),
                    name=f"mcp-http-request:{self._config.server_id}",
                )
                self._request_tasks.add(task)
                task.add_done_callback(self._request_done)

    def _request_done(self, task: asyncio.Task[None]) -> None:
        self._request_tasks.discard(task)
        self._request_lane.release()
        if not task.cancelled():
            with suppress(BaseException):
                task.result()

    async def _send_message(self, message: SessionMessage) -> None:
        assert self._client is not None and self._endpoint is not None
        try:
            payload = self._encode(message)
        except BaseException:
            await self._offer_failure(
                McpTransportOperationError(may_have_reached_server=False),
                "MCP HTTP encode failed",
            )
            return
        try:
            self._byte_budget.reserve(len(payload))
        except McpWireBoundExceeded:
            await self._offer_failure(
                McpTransportOperationError(may_have_reached_server=False),
                "MCP HTTP request exceeds aggregate slot memory",
            )
            return
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Host": self._endpoint.host_header,
        }
        if message.metadata is not None and getattr(message.metadata, "headers", None):
            headers.update(message.metadata.headers or {})
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            async with self._client.stream(
                "POST",
                self._endpoint.url,
                content=payload,
                headers=headers,
                extensions=_http_request_extensions(self._endpoint),
            ) as response:
                response.raise_for_status()
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self._session_id = session_id
                    self._ensure_listener()
                if response.status_code == 202:
                    return
                try:
                    await self._consume_response(response)
                except McpProtocolConformanceError:
                    raise
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise McpProtocolConformanceError(
                        "MCP_RESPONSE_CARRIER_INVALID"
                    ) from exc
        except McpProtocolConformanceError as exc:
            # The HTTP response was received and decoded far enough to prove a
            # peer conformance failure.  Preserve that exact settlement instead
            # of converting it into may-have-written ambiguity.
            await self._offer_failure(exc, "MCP HTTP response invalid")
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            may_have_reached = not isinstance(
                exc, httpx.ConnectError | httpx.PoolTimeout
            )
            await self._offer_failure(
                McpTransportOperationError(
                    may_have_reached_server=may_have_reached
                ),
                "MCP HTTP request failed",
            )
        finally:
            self._byte_budget.release(len(payload))

    def _ensure_listener(self) -> None:
        if self._listener_task is None or self._listener_task.done():
            self._listener_task = asyncio.create_task(
                self._listener(), name=f"mcp-http-listener:{self._config.server_id}"
            )

    async def _listener(self) -> None:
        assert (
            self._client is not None
            and self._session_id is not None
            and self._endpoint is not None
        )
        try:
            async with self._client.stream(
                "GET",
                self._endpoint.url,
                headers={
                    "Accept": "text/event-stream",
                    "Mcp-Session-Id": self._session_id,
                    "Host": self._endpoint.host_header,
                },
                extensions=_http_request_extensions(self._endpoint),
            ) as response:
                if response.status_code in {404, 405}:
                    return
                response.raise_for_status()
                await self._consume_sse(response)
        except BaseException as exc:
            await self._offer_failure(exc, "MCP HTTP listener cancelled")

    async def _consume_response(self, response: httpx.Response) -> None:
        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            await self._consume_sse(response)
            return
        data, reserved = await _bounded_aread(
            response,
            self.bounds.maximum_http_json_body_bytes,
            budget=self._byte_budget,
        )
        try:
            await self.read_writer.send(
                self._decode(
                    data,
                    maximum_bytes=self.bounds.maximum_http_json_body_bytes,
                )
            )
        finally:
            self._byte_budget.release(reserved)

    async def _consume_sse(self, response: httpx.Response) -> None:
        data = bytearray()
        line_buffer = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                if len(line_buffer) + len(chunk) > (
                    self.bounds.maximum_sse_event_data_bytes + 4096
                ):
                    raise McpWireBoundExceeded("MCP SSE line exceeds the bound")
                self._byte_budget.reserve(len(chunk))
                line_buffer.extend(chunk)
                while (newline := line_buffer.find(b"\n")) >= 0:
                    line_length = newline
                    self._byte_budget.reserve(line_length)
                    line = bytes(line_buffer[:newline]).removesuffix(b"\r")
                    del line_buffer[: newline + 1]
                    self._byte_budget.release(newline + 1)
                    try:
                        if not line:
                            if data:
                                await self.read_writer.send(
                                    self._decode(
                                        data,
                                        maximum_bytes=(
                                            self.bounds.maximum_sse_event_data_bytes
                                        ),
                                    )
                                )
                                self._byte_budget.release(len(data))
                                data.clear()
                            continue
                        if line.startswith(b"data:"):
                            value = line[5:].lstrip()
                            addition = len(value) + (1 if data else 0)
                            self._byte_budget.reserve(addition)
                            if data:
                                data.extend(b"\n")
                            data.extend(value)
                            if len(data) > self.bounds.maximum_sse_event_data_bytes:
                                raise McpWireBoundExceeded(
                                    "MCP SSE event exceeds the bound"
                                )
                    finally:
                        self._byte_budget.release(line_length)
            if line_buffer or data:
                raise ValueError("MCP SSE stream ended with an incomplete event")
        finally:
            self._byte_budget.release(len(line_buffer) + len(data))

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.write_stream.aclose()
        tasks = tuple(
            task for task in (self._writer_task, self._listener_task) if task is not None
        )
        tasks += tuple(self._request_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
        await self.read_writer.aclose()


async def _bounded_aread(
    response: httpx.Response,
    maximum: int,
    *,
    budget: _SlotByteBudget,
) -> tuple[bytearray, int]:
    body = bytearray()
    total = 0
    reserved = 0
    try:
        async for chunk in response.aiter_bytes():
            if total + len(chunk) > maximum:
                raise McpWireBoundExceeded("MCP HTTP body exceeds the bound")
            budget.reserve(len(chunk))
            reserved += len(chunk)
            total += len(chunk)
            body.extend(chunk)
        return body, total
    except BaseException:
        budget.release(reserved)
        raise


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
        f"{logical_host}:{explicit_port}"
        if explicit_port is not None
        else logical_host
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
        bounds: McpWireBounds = DEFAULT_MCP_WIRE_BOUNDS,
    ) -> None:
        self.config = config
        self._workspace_root = workspace_root
        self._notification_callback = notification_callback
        self._bounds = bounds
        self._transport: _BoundedTransport | None = None
        self._session: ClientSession | None = None
        self._closed = False
        self._close_lock = asyncio.Lock()
        self.protocol_version = ""
        self.server_name = config.display_name
        self.server_instructions = ""
        self.advertised_capabilities: McpAdvertisedCapabilities | None = None

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
    def supports_bounded_stateless_parallelism(self) -> bool:
        """Prove the physical HTTP session is stateless after negotiation.

        Configuration is only an operator assertion.  A peer that returns an
        MCP session identity contradicts it and must stay on the serial lane.
        stdio is always sessionful for concurrency purposes.
        """

        return (
            isinstance(self._transport, _BoundedHttpTransport)
            and self.config.supports_parallel_tool_calls
            and isinstance(self.config.transport, StreamableHttpTransportConfig)
            and self.config.transport.proved_stateless
            and not self._transport.sessionful
        )

    async def open(self) -> None:
        if self._session is not None:
            raise RuntimeError("MCP SDK client was opened twice")
        transport_config = self.config.transport
        if isinstance(transport_config, StdioTransportConfig):
            transport: _BoundedTransport = _BoundedStdioTransport(
                transport_config,
                workspace_root=self._workspace_root,
                bounds=self._bounds,
            )
        else:
            transport = _BoundedHttpTransport(
                self.config, transport_config, bounds=self._bounds
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
                # A legacy initialize fallback is allowed only for a peer that
                # explicitly lacks modern discover; other faults stay visible.
                if modern_error.code != -32601:
                    raise
                result = await self._session.initialize()
                self.protocol_version = result.protocol_version
                self.server_name = result.server_info.name
                self.server_instructions = result.instructions or ""
                transport.enforce_closed_result_type = True
            capabilities = self._session.server_capabilities
            if capabilities is None:
                raise McpProtocolConformanceError(
                    "MCP_SERVER_CAPABILITIES_MISSING"
                )
            self.advertised_capabilities = McpAdvertisedCapabilities(
                tools=capabilities.tools is not None,
                resources=capabilities.resources is not None,
                prompts=capabilities.prompts is not None,
            )
        except BaseException:
            await self.aclose()
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
        if (
            not presence.present
            or presence.value not in {"complete", "input_required"}
        ):
            raise McpProtocolConformanceError(
                "MCP_RESULT_TYPE_CONFORMANCE_FAILED"
            )
        result_value = getattr(result, "result_type", None)
        if result is not None and result_value != presence.value:
            raise McpProtocolConformanceError(
                "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
            )
        if (
            presence.value == "input_required"
            and result is not None
            and not isinstance(result, types.InputRequiredResult)
        ) or (
            presence.value == "complete"
            and isinstance(result, types.InputRequiredResult)
        ):
            raise McpProtocolConformanceError(
                "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
            )
        return presence.value

    async def aclose(self) -> None:
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
    "McpAdvertisedCapabilities",
    "McpProtocolConformanceError",
    "McpTransportOperationError",
    "McpWireResultPresence",
    "types",
]
