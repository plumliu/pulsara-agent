"""SDK-backed MCP v2 manager.

This module is the only production path that imports the official MCP SDK beta.
Everything above it speaks Pulsara-owned DTOs so SDK churn stays contained here.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import SplitResult, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import mcp_types as types
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from pulsara_agent.runtime.mcp.manager import McpClientManager
from pulsara_agent.primitives.mcp import McpServerLifecycleTimingFact
from pulsara_agent.runtime.mcp.types import (
    McpContentArtifact,
    McpDrainError,
    McpDiscoveredPrompt,
    McpDiscoveredResource,
    McpDiscoveredResourceTemplate,
    McpDiscoveredTool,
    McpInputRequestDTO,
    McpInputRequired,
    McpInputRequiredResolution,
    McpOriginalRequest,
    McpRequestSourceMethod,
    McpServerConfig,
    McpServerSnapshot,
    McpServerStatus,
    McpStdioConfig,
    McpStreamableHttpConfig,
    McpToolAnnotations,
    McpToolResult,
    event_safe_mcp_config_fingerprint,
    filter_mcp_tools,
    snapshot_semantic_fingerprint,
)


DEFAULT_MCP_MAX_PAGES = 20
DEFAULT_MCP_MAX_ITEMS = 2_000
DEFAULT_MCP_INPUT_REQUIRED_TIMEOUT_SECONDS = 300.0

_SAFE_AMBIENT_ENV = {
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TMPDIR",
    "USER",
}


@dataclass(slots=True)
class _SdkServerConnection:
    config: McpServerConfig
    client: Client
    http_client: httpx.AsyncClient | None = None
    close_requested: asyncio.Event | None = None
    owner_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _SdkOwnerStartError(RuntimeError):
    def __init__(
        self,
        cause: BaseException,
        *,
        close_requested: asyncio.Event,
        owner_task: asyncio.Task[None],
    ) -> None:
        self.cause = cause
        self.close_requested = close_requested
        self.owner_task = owner_task
        super().__init__(str(cause))


class SdkMcpConnectError(RuntimeError):
    """Connect failure whose partially started SDK owner still needs drain."""

    def __init__(self, connection: "SdkMcpConnection", cause: BaseException) -> None:
        self.connection = connection
        self.cause = cause
        super().__init__(f"{type(cause).__name__}: {cause}")


class SdkMcpConnectCancelled(asyncio.CancelledError):
    """Caller cancellation carrying the partial SDK connection cleanup owner."""

    def __init__(self, connection: "SdkMcpConnection") -> None:
        self.connection = connection
        super().__init__("MCP SDK connect cancelled")


@dataclass(slots=True)
class SdkMcpConnection:
    """Connected, not-yet-discovered per-server SDK owner."""

    _connection: _SdkServerConnection
    _closed: bool = False

    @classmethod
    async def connect(
        cls,
        config: McpServerConfig,
        *,
        timeout_seconds: float,
    ) -> "SdkMcpConnection":
        client, http_client = _build_sdk_client(config)
        try:
            close_requested, owner_task = await _start_sdk_client_owner(
                client,
                timeout_seconds=timeout_seconds,
            )
        except _SdkOwnerStartError as exc:
            connection = cls(
                _SdkServerConnection(
                    config=config,
                    client=client,
                    http_client=http_client,
                    close_requested=exc.close_requested,
                    owner_task=exc.owner_task,
                )
            )
            if isinstance(exc.cause, asyncio.CancelledError):
                raise SdkMcpConnectCancelled(connection) from exc.cause
            raise SdkMcpConnectError(connection, exc.cause) from exc.cause
        except BaseException:
            if http_client is not None:
                await _best_effort_sdk_close_step(http_client.aclose())
            raise
        return cls(
            _SdkServerConnection(
                config=config,
                client=client,
                http_client=http_client,
                close_requested=close_requested,
                owner_task=owner_task,
            )
        )

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        if self._closed:
            return
        await _close_sdk_connection(
            self._connection,
            timeout_seconds=timeout_seconds,
        )
        self._closed = True


@dataclass(slots=True)
class SdkMcpClientManager(McpClientManager):
    """Session-owned official Python MCP SDK v2 manager."""

    _snapshots: tuple[McpServerSnapshot, ...]
    _connections: dict[str, _SdkServerConnection]
    max_pages: int = DEFAULT_MCP_MAX_PAGES
    max_items: int = DEFAULT_MCP_MAX_ITEMS
    _closed: bool = False
    _active_tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @classmethod
    def from_connected_server(
        cls,
        *,
        connection: SdkMcpConnection,
        snapshot: McpServerSnapshot,
        max_pages: int = DEFAULT_MCP_MAX_PAGES,
        max_items: int = DEFAULT_MCP_MAX_ITEMS,
    ) -> "SdkMcpClientManager":
        raw = connection._connection
        connection._closed = True  # ownership moves into the manager
        return cls(
            _snapshots=(snapshot,),
            _connections={snapshot.server_id: raw},
            max_pages=max_pages,
            max_items=max_items,
        )

    @property
    def snapshots(self) -> tuple[McpServerSnapshot, ...]:
        return self._snapshots

    async def _discover_connected(
        self,
        connection: _SdkServerConnection,
        *,
        config_epoch: int,
        reconcile_attempt_id: str,
        discovery_generation: int,
        queued_at_utc: str,
        queued_monotonic: float,
        connect_started_at_utc: str,
        connect_ended_at_utc: str,
        connect_duration_seconds: float,
        discovery_started_at_utc: str,
        discovery_started_monotonic: float,
    ) -> tuple[McpServerSnapshot, int, int]:
        config = connection.config
        diagnostics: list[dict[str, Any]] = []
        metrics = {"request_count": 0, "page_count": 0}
        capabilities = getattr(connection.client, "server_capabilities", None)
        tools: list[Any] = []
        resources: list[Any] = []
        resource_templates: list[Any] = []
        prompts: list[Any] = []
        discovery_tasks: dict[str, asyncio.Task[list[Any]]] = {}
        async with asyncio.TaskGroup() as task_group:
            if capabilities is not None and getattr(capabilities, "tools", None) is not None:
                discovery_tasks["tools"] = task_group.create_task(
                    self._list_all(
                        "tools/list",
                        lambda cursor: connection.client.session.list_tools(
                            params=types.PaginatedRequestParams(cursor=cursor)
                        ),
                        diagnostics,
                        item_attr="tools",
                        metrics=metrics,
                    )
                )
            if capabilities is not None and getattr(capabilities, "resources", None) is not None:
                discovery_tasks["resources"] = task_group.create_task(
                    self._list_all(
                        "resources/list",
                        lambda cursor: connection.client.session.list_resources(
                            params=types.PaginatedRequestParams(cursor=cursor)
                        ),
                        diagnostics,
                        item_attr="resources",
                        metrics=metrics,
                    )
                )
                discovery_tasks["resource_templates"] = task_group.create_task(
                    self._list_all(
                        "resources/templates/list",
                        lambda cursor: connection.client.session.list_resource_templates(
                            params=types.PaginatedRequestParams(cursor=cursor)
                        ),
                        diagnostics,
                        item_attr="resource_templates",
                        metrics=metrics,
                    )
                )
            if capabilities is not None and getattr(capabilities, "prompts", None) is not None:
                discovery_tasks["prompts"] = task_group.create_task(
                    self._list_all(
                        "prompts/list",
                        lambda cursor: connection.client.session.list_prompts(
                            params=types.PaginatedRequestParams(cursor=cursor)
                        ),
                        diagnostics,
                        item_attr="prompts",
                        metrics=metrics,
                    )
                )
        tools = discovery_tasks.get("tools").result() if "tools" in discovery_tasks else []
        resources = (
            discovery_tasks.get("resources").result()
            if "resources" in discovery_tasks
            else []
        )
        resource_templates = (
            discovery_tasks.get("resource_templates").result()
            if "resource_templates" in discovery_tasks
            else []
        )
        prompts = (
            discovery_tasks.get("prompts").result()
            if "prompts" in discovery_tasks
            else []
        )
        ended_monotonic = time.monotonic()
        ended_at_utc = _utc_now()
        tool_facts = filter_mcp_tools(
            config,
            tuple(_tool_from_sdk(config.server_id, tool) for tool in tools),
        )
        resource_facts = tuple(_resource_from_sdk(config.server_id, resource) for resource in resources)
        template_facts = tuple(
            _resource_template_from_sdk(config.server_id, template)
            for template in resource_templates
        )
        prompt_facts = tuple(_prompt_from_sdk(config.server_id, prompt) for prompt in prompts)
        server_info = connection.client.server_info.model_dump(mode="json", by_alias=True)
        snapshot = McpServerSnapshot(
            snapshot_id=f"mcp_snapshot:{uuid4().hex}",
            server_id=config.server_id,
            config_epoch=config_epoch,
            event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
            snapshot_semantic_fingerprint=snapshot_semantic_fingerprint(
                server_id=config.server_id,
                status=McpServerStatus.READY,
                tools=tool_facts,
                resources=resource_facts,
                resource_templates=template_facts,
                prompts=prompt_facts,
                protocol_version=connection.client.protocol_version,
                server_info=server_info,
                instructions=connection.client.instructions,
            ),
            reconcile_attempt_id=reconcile_attempt_id,
            discovery_generation=discovery_generation,
            status=McpServerStatus.READY,
            required=config.required,
            tools=tool_facts,
            resources=resource_facts,
            resource_templates=template_facts,
            prompts=prompt_facts,
            protocol_version=connection.client.protocol_version,
            server_info=server_info,
            instructions=connection.client.instructions,
            diagnostics=tuple(diagnostics),
            timing=McpServerLifecycleTimingFact(
                queued_at_utc=queued_at_utc,
                connect_started_at_utc=connect_started_at_utc,
                connect_ended_at_utc=connect_ended_at_utc,
                discovery_started_at_utc=discovery_started_at_utc,
                discovery_ended_at_utc=ended_at_utc,
                completed_at_utc=ended_at_utc,
                connect_duration_seconds=connect_duration_seconds,
                discovery_duration_seconds=max(0.0, ended_monotonic - discovery_started_monotonic),
                total_duration_seconds=max(0.0, ended_monotonic - queued_monotonic),
            ),
        )
        return snapshot, metrics["request_count"], metrics["page_count"]

    async def _list_all(
        self,
        method: str,
        fetch: Callable[[str | None], Any],
        diagnostics: list[dict[str, Any]],
        *,
        item_attr: str,
        metrics: dict[str, int],
    ) -> list[Any]:
        items: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for page in range(1, self.max_pages + 1):
            metrics["request_count"] += 1
            metrics["page_count"] += 1
            result = await fetch(cursor)
            items.extend(list(getattr(result, item_attr)))
            next_cursor = getattr(result, "next_cursor", None)
            if len(items) > self.max_items:
                diagnostics.append(
                    {
                        "code": "mcp_pagination_item_limit",
                        "method": method,
                        "max_items": self.max_items,
                    }
                )
                return items[: self.max_items]
            if not next_cursor:
                return items
            if next_cursor in seen_cursors:
                raise RuntimeError(f"repeated MCP pagination cursor for {method}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError(f"MCP pagination exceeded max pages for {method}: {self.max_pages}")

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> McpToolResult | McpInputRequired:
        connection = self._require_connection(server_id)
        task = asyncio.create_task(
            self._call_tool_connected(connection, tool_name, dict(arguments), timeout_ms=timeout_ms)
        )
        self._active_tasks.add(task)
        try:
            return await task
        finally:
            self._active_tasks.discard(task)

    async def _call_tool_connected(
        self,
        connection: _SdkServerConnection,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int,
        input_responses: dict[str, dict[str, Any]] | None = None,
        request_state: str | None = None,
        round_count: int = 1,
    ) -> McpToolResult | McpInputRequired:
        async with connection.lock:
            result = await connection.client.session.call_tool(
                tool_name,
                arguments,
                read_timeout_seconds=timeout_ms / 1000,
                input_responses=input_responses,  # type: ignore[arg-type]
                request_state=request_state,
                allow_input_required=True,
                allow_claimed=True,
            )
        if isinstance(result, types.InputRequiredResult):
            return _input_required_from_sdk(
                result,
                server_id=connection.config.server_id,
                protocol_version=_safe_protocol_version(connection.client),
                original_request=McpOriginalRequest(
                    source_method=McpRequestSourceMethod.TOOL_CALL,
                    tool_name=tool_name,
                    arguments=arguments,
                ),
                round_count=round_count,
            )
        if not isinstance(result, types.CallToolResult):
            return McpToolResult(
                output=json.dumps(result.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2),
                metadata={"mcp_result_type": type(result).__name__},
            )
        return mcp_tool_result_from_sdk(result)

    async def read_resource(
        self,
        server_id: str,
        uri: str,
        *,
        timeout_ms: int,
    ) -> McpToolResult | McpInputRequired:
        connection = self._require_connection(server_id)
        async with connection.lock:
            result = await asyncio.wait_for(
                connection.client.session.read_resource(uri, allow_input_required=True),
                timeout=timeout_ms / 1000,
            )
        if isinstance(result, types.InputRequiredResult):
            return _input_required_from_sdk(
                result,
                server_id=server_id,
                protocol_version=_safe_protocol_version(connection.client),
                original_request=McpOriginalRequest(
                    source_method=McpRequestSourceMethod.RESOURCE_READ,
                    resource_uri=uri,
                ),
            )
        return mcp_read_resource_result_from_sdk(result)

    async def get_prompt(
        self,
        server_id: str,
        name: str,
        arguments: dict[str, str] | None = None,
        *,
        timeout_ms: int,
    ) -> McpToolResult | McpInputRequired:
        connection = self._require_connection(server_id)
        async with connection.lock:
            result = await asyncio.wait_for(
                connection.client.session.get_prompt(
                    name,
                    arguments,
                    allow_input_required=True,
                ),
                timeout=timeout_ms / 1000,
            )
        if isinstance(result, types.InputRequiredResult):
            return _input_required_from_sdk(
                result,
                server_id=server_id,
                protocol_version=_safe_protocol_version(connection.client),
                original_request=McpOriginalRequest(
                    source_method=McpRequestSourceMethod.PROMPT_GET,
                    prompt_name=name,
                    prompt_arguments=arguments,
                ),
            )
        return mcp_get_prompt_result_from_sdk(result)

    async def resume_suspended_request(
        self,
        *,
        server_id: str,
        original_request: McpOriginalRequest,
        request_state: str | None,
        resolution: McpInputRequiredResolution,
        timeout_ms: int,
    ) -> McpToolResult | McpInputRequired:
        if resolution.cancelled:
            return McpToolResult(
                output=f"MCP input-required interaction {resolution.interaction_id} was cancelled.",
                is_error=True,
                metadata={"mcp_cancelled": True},
            )
        connection = self._require_connection(server_id)
        source = original_request.source_method
        responses = _sdk_input_responses(resolution)
        if source is McpRequestSourceMethod.TOOL_CALL:
            if original_request.tool_name is None:
                raise RuntimeError("tools/call resume requires original tool name")
            return await self._call_tool_connected(
                connection,
                original_request.tool_name,
                dict(original_request.arguments or {}),
                timeout_ms=timeout_ms,
                input_responses=responses,
                request_state=request_state,
                round_count=resolution.round_count + 1,
            )
        if source is McpRequestSourceMethod.RESOURCE_READ:
            if original_request.resource_uri is None:
                raise RuntimeError("resources/read resume requires original resource uri")
            async with connection.lock:
                result = await asyncio.wait_for(
                    connection.client.session.read_resource(
                        original_request.resource_uri,
                        input_responses=responses,
                        request_state=request_state,
                        allow_input_required=True,
                    ),
                    timeout=timeout_ms / 1000,
                )
            if isinstance(result, types.InputRequiredResult):
                return _input_required_from_sdk(
                    result,
                    server_id=server_id,
                    protocol_version=_safe_protocol_version(connection.client),
                    original_request=original_request,
                    round_count=resolution.round_count + 1,
                )
            return mcp_read_resource_result_from_sdk(result)
        if source is McpRequestSourceMethod.PROMPT_GET:
            if original_request.prompt_name is None:
                raise RuntimeError("prompts/get resume requires original prompt name")
            async with connection.lock:
                result = await asyncio.wait_for(
                    connection.client.session.get_prompt(
                        original_request.prompt_name,
                        original_request.prompt_arguments,
                        input_responses=responses,
                        request_state=request_state,
                        allow_input_required=True,
                    ),
                    timeout=timeout_ms / 1000,
                )
            if isinstance(result, types.InputRequiredResult):
                return _input_required_from_sdk(
                    result,
                    server_id=server_id,
                    protocol_version=_safe_protocol_version(connection.client),
                    original_request=original_request,
                    round_count=resolution.round_count + 1,
                )
            return mcp_get_prompt_result_from_sdk(result)
        raise RuntimeError(f"unsupported MCP resume source method: {source}")

    def cancel_active(self) -> None:
        for task in tuple(self._active_tasks):
            task.cancel()

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        if self._closed:
            return
        deadline = time.monotonic() + timeout_seconds
        try:
            await asyncio.wait_for(
                self._close_lock.acquire(),
                timeout=max(0.001, deadline - time.monotonic()),
            )
        except TimeoutError as exc:
            raise McpDrainError("timed out waiting for MCP SDK close ownership") from exc
        try:
            if self._closed:
                return
            self.cancel_active()
            if self._active_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *tuple(self._active_tasks),
                            return_exceptions=True,
                        ),
                        timeout=max(0.001, deadline - time.monotonic()),
                    )
                except TimeoutError as exc:
                    raise McpDrainError("timed out draining active MCP SDK calls") from exc
            for server_id in tuple(self._connections):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpDrainError("MCP SDK close deadline expired")
                await self._close_connection(
                    server_id,
                    timeout_seconds=remaining,
                )
            self._closed = True
        finally:
            self._close_lock.release()

    async def _close_connection(
        self,
        server_id: str,
        *,
        timeout_seconds: float,
    ) -> None:
        connection = self._connections.get(server_id)
        if connection is None:
            return
        await _close_sdk_connection(connection, timeout_seconds=timeout_seconds)
        self._connections.pop(server_id, None)

    def _require_connection(self, server_id: str) -> _SdkServerConnection:
        if self._closed:
            raise RuntimeError("MCP SDK manager is closed")
        try:
            return self._connections[server_id]
        except KeyError as exc:
            raise KeyError(f"Unknown MCP server: {server_id}") from exc


async def discover_mcp_server(
    connection: SdkMcpConnection,
    *,
    config_epoch: int,
    reconcile_attempt_id: str,
    discovery_generation: int,
    queued_at_utc: str,
    queued_monotonic: float,
    connect_started_at_utc: str,
    connect_ended_at_utc: str,
    connect_duration_seconds: float,
    discovery_started_at_utc: str,
    discovery_started_monotonic: float,
    timeout_seconds: float,
    max_pages: int = DEFAULT_MCP_MAX_PAGES,
    max_items: int = DEFAULT_MCP_MAX_ITEMS,
) -> tuple[McpServerSnapshot, int, int]:
    """Discover one already-connected server under one absolute caller budget."""

    probe = SdkMcpClientManager(
        _snapshots=(),
        _connections={},
        max_pages=max_pages,
        max_items=max_items,
    )
    return await asyncio.wait_for(
        probe._discover_connected(
            connection._connection,
            config_epoch=config_epoch,
            reconcile_attempt_id=reconcile_attempt_id,
            discovery_generation=discovery_generation,
            queued_at_utc=queued_at_utc,
            queued_monotonic=queued_monotonic,
            connect_started_at_utc=connect_started_at_utc,
            connect_ended_at_utc=connect_ended_at_utc,
            connect_duration_seconds=connect_duration_seconds,
            discovery_started_at_utc=discovery_started_at_utc,
            discovery_started_monotonic=discovery_started_monotonic,
        ),
        timeout=max(0.001, timeout_seconds),
    )


def _build_sdk_client(config: McpServerConfig) -> tuple[Client, httpx.AsyncClient | None]:
    transport = config.transport
    if isinstance(transport, McpStdioConfig):
        env = _safe_child_env(dict(transport.env))
        params = StdioServerParameters(
            command=transport.command,
            args=list(transport.args),
            env=env,
            cwd=str(transport.cwd) if transport.cwd is not None else None,
        )
        return Client(
            stdio_client(params),
            cache=False,
            read_timeout_seconds=config.tool_timeout_ms / 1000,
        ), None
    if isinstance(transport, McpStreamableHttpConfig):
        headers = _http_headers(transport)
        timeout = httpx.Timeout(config.tool_timeout_ms / 1000)
        http_client = httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=transport.follow_redirects,
        )
        return Client(
            streamable_http_client(transport.url, http_client=http_client),
            cache=False,
            read_timeout_seconds=config.tool_timeout_ms / 1000,
        ), http_client
    raise TypeError(f"unsupported MCP transport: {type(transport).__name__}")


async def _close_sdk_connection(
    connection: _SdkServerConnection,
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    await _best_effort_sdk_close_step(
        asyncio.wait_for(
            _terminate_sdk_stdio_process(connection.client),
            timeout=max(0.001, min(1.0, deadline - time.monotonic())),
        )
    )
    if connection.close_requested is not None:
        connection.close_requested.set()
    if connection.owner_task is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise McpDrainError("MCP SDK owner close deadline expired")
        try:
            await asyncio.wait_for(
                asyncio.shield(connection.owner_task),
                timeout=remaining,
            )
        except TimeoutError as exc:
            raise McpDrainError("timed out draining MCP SDK owner task") from exc
        except asyncio.CancelledError:
            if not connection.owner_task.cancelled():
                raise
    if connection.http_client is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise McpDrainError("MCP HTTP client close deadline expired")
        try:
            await asyncio.wait_for(connection.http_client.aclose(), timeout=remaining)
        except TimeoutError as exc:
            raise McpDrainError("timed out closing MCP HTTP client") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _start_sdk_client_owner(
    client: Client,
    *,
    timeout_seconds: float,
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    """Enter and exit the SDK client context from one dedicated task.

    MCP SDK v2 beta streamable-http transports use anyio cancel scopes that must
    be exited by the same task that entered them.  Keeping a tiny owner task
    alive for the lifetime of the connection prevents close-time cancellation
    from leaking into HostCore/REPL teardown.
    """

    ready = asyncio.Event()
    close_requested = asyncio.Event()
    enter_error: BaseException | None = None

    async def owner() -> None:
        nonlocal enter_error
        try:
            await client.__aenter__()
        except BaseException as exc:
            enter_error = exc
            ready.set()
            return
        ready.set()
        try:
            await close_requested.wait()
        finally:
            await _best_effort_sdk_close_step(client.__aexit__(None, None, None))

    task = asyncio.create_task(owner(), name="pulsara-mcp-sdk-client-owner")
    try:
        await asyncio.wait_for(ready.wait(), timeout=timeout_seconds)
    except BaseException as exc:
        close_requested.set()
        task.cancel()
        raise _SdkOwnerStartError(
            exc,
            close_requested=close_requested,
            owner_task=task,
        ) from exc
    if enter_error is not None:
        with contextlib.suppress(BaseException):
            await task
        raise enter_error
    return close_requested, task


async def _terminate_sdk_stdio_process(client: Client) -> None:
    """Best-effort kill switch for SDK stdio transports.

    The SDK owns the actual subprocess and currently keeps it private inside an
    async-generator context manager. Pulsara keeps this private introspection
    inside the SDK facade so host/session shutdown still has a bounded off-ramp
    if a stdio server ignores normal stream closure.
    """

    process = _sdk_stdio_process(client)
    if process is None:
        return
    with contextlib.suppress(Exception):
        if getattr(process, "returncode", None) is None:
            process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=0.5)
    with contextlib.suppress(Exception):
        if getattr(process, "returncode", None) is None:
            process.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=0.5)


def _sdk_stdio_process(client: Client) -> Any | None:
    stack = getattr(client, "_exit_stack", None)
    callbacks = getattr(stack, "_exit_callbacks", ()) if stack is not None else ()
    for callback in callbacks:
        try:
            exit_func = callback[1]
            context_manager = exit_func.__self__
            generator = getattr(context_manager, "gen", None)
            frame = getattr(generator, "ag_frame", None)
            process = frame.f_locals.get("process") if frame is not None else None
        except Exception:
            continue
        if process is not None:
            return process
    return None


async def _best_effort_sdk_close_step(awaitable: Any) -> None:
    """Run one SDK close step without leaking internal cancel-scope shutdown.

    MCP SDK v2 transports may use cancellation as part of normal ``__aexit__``
    teardown.  Pulsara owns the host/session lifecycle above this facade, so an
    SDK-internal close cancellation must not poison the REPL task or make
    ``:close`` fail after the server has already been detached.
    """

    try:
        await awaitable
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        _clear_current_task_cancellation()
    except Exception:
        pass


def _clear_current_task_cancellation() -> None:
    task = asyncio.current_task()
    if task is None or not hasattr(task, "uncancel"):
        return
    while task.cancelling():
        task.uncancel()


def _safe_child_env(explicit_env: dict[str, str]) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _SAFE_AMBIENT_ENV}
    env.update({str(key): str(value) for key, value in explicit_env.items()})
    return env


def _http_headers(transport: McpStreamableHttpConfig) -> dict[str, str]:
    headers = dict(transport.headers)
    for header, env_var in transport.env_headers.items():
        value = os.getenv(env_var)
        if value:
            headers[header] = value
    if transport.bearer_token_env_var:
        token = os.getenv(transport.bearer_token_env_var)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            raise RuntimeError(f"missing bearer token env var {transport.bearer_token_env_var}")
    return headers


def _tool_from_sdk(server_id: str, tool: types.Tool) -> McpDiscoveredTool:
    annotations = tool.annotations
    return McpDiscoveredTool(
        server_id=server_id,
        name=tool.name,
        description=tool.description or tool.name,
        input_schema=dict(tool.input_schema or {}),
        annotations=McpToolAnnotations(
            read_only_hint=getattr(annotations, "read_only_hint", None) if annotations is not None else None,
            destructive_hint=getattr(annotations, "destructive_hint", None) if annotations is not None else None,
            open_world_hint=getattr(annotations, "open_world_hint", None) if annotations is not None else None,
            title=getattr(annotations, "title", None) if annotations is not None else None,
        ),
    )


def _resource_from_sdk(server_id: str, resource: types.Resource) -> McpDiscoveredResource:
    return McpDiscoveredResource(
        server_id=server_id,
        uri=resource.uri,
        name=resource.name,
        description=resource.description or "",
        mime_type=resource.mime_type,
        size=resource.size,
    )


def _resource_template_from_sdk(
    server_id: str,
    template: types.ResourceTemplate,
) -> McpDiscoveredResourceTemplate:
    return McpDiscoveredResourceTemplate(
        server_id=server_id,
        uri_template=template.uri_template,
        name=template.name,
        description=template.description or "",
        mime_type=template.mime_type,
    )


def _prompt_from_sdk(server_id: str, prompt: types.Prompt) -> McpDiscoveredPrompt:
    return McpDiscoveredPrompt(
        server_id=server_id,
        name=prompt.name,
        description=prompt.description or "",
        arguments=tuple(
            argument.model_dump(mode="json", by_alias=True, exclude_none=True)
            for argument in (prompt.arguments or ())
        ),
    )


def mcp_tool_result_from_sdk(result: types.CallToolResult) -> McpToolResult:
    output_parts: list[str] = []
    artifacts: list[McpContentArtifact] = []
    for index, item in enumerate(result.content):
        _append_content(item, output_parts, artifacts, role_prefix=f"content_{index}")
    if result.structured_content is not None:
        structured_text = json.dumps(result.structured_content, ensure_ascii=False, indent=2, sort_keys=True)
        output_parts.append(f"[structured_content]\n{structured_text}")
        artifacts.append(
            McpContentArtifact(
                role="structured_content",
                media_type="application/json",
                text=structured_text,
                metadata={"mcp_content_kind": "structured_content"},
            )
        )
    output = "\n\n".join(part for part in output_parts if part).strip()
    if not output:
        output = "[MCP tool returned non-text content; see artifacts/metadata.]"
    return McpToolResult(
        output=output,
        is_error=result.is_error,
        structured_content=result.structured_content,
        artifacts=tuple(artifacts),
        metadata={
            "mcp_result_type": "CallToolResult",
            "mcp_is_error": result.is_error,
            "mcp_content_count": len(result.content),
        },
    )


def mcp_read_resource_result_from_sdk(result: types.ReadResourceResult) -> McpToolResult:
    output_parts: list[str] = []
    artifacts: list[McpContentArtifact] = []
    for index, content in enumerate(result.contents):
        if isinstance(content, types.TextResourceContents):
            output_parts.append(f"[resource:{content.uri}]\n{content.text}")
            artifacts.append(
                McpContentArtifact(
                    role=f"resource_{index}",
                    media_type=content.mime_type or "text/plain; charset=utf-8",
                    text=content.text,
                    metadata={"uri": content.uri, "mcp_content_kind": "text_resource"},
                )
            )
        elif isinstance(content, types.BlobResourceContents):
            data = _decode_base64(content.blob)
            artifacts.append(
                McpContentArtifact(
                    role=f"resource_{index}",
                    media_type=content.mime_type or "application/octet-stream",
                    data=data,
                    metadata={"uri": content.uri, "mcp_content_kind": "blob_resource"},
                )
            )
            output_parts.append(f"[resource_blob:{content.uri}] {len(data)} bytes archived")
    return McpToolResult(
        output="\n\n".join(output_parts).strip() or "[MCP resource contained no model-visible text.]",
        artifacts=tuple(artifacts),
        metadata={"mcp_result_type": "ReadResourceResult", "mcp_content_count": len(result.contents)},
    )


def mcp_get_prompt_result_from_sdk(result: types.GetPromptResult) -> McpToolResult:
    payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return McpToolResult(
        output=text,
        artifacts=(
            McpContentArtifact(
                role="prompt",
                media_type="application/json",
                text=text,
                metadata={"mcp_content_kind": "prompt"},
            ),
        ),
        metadata={"mcp_result_type": "GetPromptResult", "mcp_message_count": len(result.messages)},
    )


def _append_content(
    item: types.ContentBlock,
    output_parts: list[str],
    artifacts: list[McpContentArtifact],
    *,
    role_prefix: str,
) -> None:
    if isinstance(item, types.TextContent):
        output_parts.append(item.text)
        return
    if isinstance(item, types.ImageContent):
        data = _decode_base64(item.data)
        artifacts.append(
            McpContentArtifact(
                role=f"{role_prefix}_image",
                media_type=item.mime_type,
                data=data,
                metadata={"mcp_content_kind": "image"},
            )
        )
        output_parts.append(f"[image:{item.mime_type}] {len(data)} bytes archived")
        return
    if isinstance(item, types.AudioContent):
        data = _decode_base64(item.data)
        artifacts.append(
            McpContentArtifact(
                role=f"{role_prefix}_audio",
                media_type=item.mime_type,
                data=data,
                metadata={"mcp_content_kind": "audio"},
            )
        )
        output_parts.append(f"[audio:{item.mime_type}] {len(data)} bytes archived")
        return
    if isinstance(item, types.ResourceLink):
        payload = item.model_dump(mode="json", by_alias=True, exclude_none=True)
        output_parts.append("[resource_link]\n" + json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if isinstance(item, types.EmbeddedResource):
        resource = item.resource
        if isinstance(resource, types.TextResourceContents):
            output_parts.append(f"[embedded_resource:{resource.uri}]\n{resource.text}")
            artifacts.append(
                McpContentArtifact(
                    role=f"{role_prefix}_embedded_resource",
                    media_type=resource.mime_type or "text/plain; charset=utf-8",
                    text=resource.text,
                    metadata={"uri": resource.uri, "mcp_content_kind": "embedded_text_resource"},
                )
            )
        else:
            data = _decode_base64(resource.blob)
            artifacts.append(
                McpContentArtifact(
                    role=f"{role_prefix}_embedded_resource",
                    media_type=resource.mime_type or "application/octet-stream",
                    data=data,
                    metadata={"uri": resource.uri, "mcp_content_kind": "embedded_blob_resource"},
                )
            )
            output_parts.append(f"[embedded_resource_blob:{resource.uri}] {len(data)} bytes archived")


def _sdk_input_responses(resolution: McpInputRequiredResolution) -> types.InputResponses | None:
    if resolution.cancelled:
        return None
    if not resolution.input_requests:
        return {str(key): dict(value) for key, value in resolution.responses.items()} or None  # type: ignore[return-value]
    requests_by_key = {request.key: request for request in resolution.input_requests}
    unexpected = sorted(set(resolution.responses).difference(requests_by_key))
    if unexpected:
        raise ValueError(f"unexpected MCP input response keys: {', '.join(unexpected)}")
    missing = sorted(set(requests_by_key).difference(resolution.responses))
    if missing:
        raise ValueError(f"missing MCP input response keys: {', '.join(missing)}")
    responses: dict[str, types.InputResponse] = {}
    for key, request in requests_by_key.items():
        responses[key] = _sdk_input_response_for_request(request, dict(resolution.responses[key]))
    return responses or None


def _sdk_input_response_for_request(request: McpInputRequestDTO, payload: dict[str, Any]) -> types.InputResponse:
    method = request.method
    if method == "elicitation/create":
        return _sdk_elicit_result(payload)
    if method == "roots/list":
        return _sdk_roots_result(payload)
    if method == "sampling/createMessage":
        return _sdk_sampling_result(payload)
    raise ValueError(f"unsupported MCP input request method: {method}")


def _sdk_elicit_result(payload: dict[str, Any]) -> types.ElicitResult:
    if "action" in payload:
        return types.ElicitResult.model_validate(payload)
    if "content" in payload:
        content = payload.get("content")
        if content is not None and not isinstance(content, dict):
            raise ValueError("elicitation content must be an object")
        return types.ElicitResult(action="accept", content=content)
    if "value" in payload:
        return types.ElicitResult(action="accept", content={"value": payload["value"]})
    return types.ElicitResult(action="accept", content=payload)


def _sdk_roots_result(payload: dict[str, Any]) -> types.ListRootsResult:
    if "roots" in payload:
        return types.ListRootsResult.model_validate(payload)
    return types.ListRootsResult.model_validate({"roots": [payload]})


def _sdk_sampling_result(payload: dict[str, Any]) -> types.CreateMessageResultWithTools:
    if "role" not in payload and "text" in payload:
        payload = {
            "role": "assistant",
            "content": {"type": "text", "text": str(payload["text"])},
            "model": str(payload.get("model") or "pulsara-user-provided"),
            "stopReason": payload.get("stopReason") or payload.get("stop_reason") or "endTurn",
        }
    return types.CreateMessageResultWithTools.model_validate(payload)


def _input_required_from_sdk(
    result: types.InputRequiredResult,
    *,
    server_id: str,
    protocol_version: str | None,
    original_request: McpOriginalRequest,
    round_count: int = 1,
) -> McpInputRequired:
    requests = []
    for key, request in (result.input_requests or {}).items():
        payload = request.model_dump(mode="json", by_alias=True, exclude_none=True)
        requests.append(
            McpInputRequestDTO(
                key=str(key),
                method=str(payload.get("method") or getattr(request, "method", "")),
                params=dict(payload.get("params") or {}),
            )
        )
    return McpInputRequired(
        interaction_id=f"mcp_input_required:{uuid4().hex}",
        server_id=server_id,
        protocol_version=protocol_version,
        request_state=result.request_state,
        input_requests=tuple(requests),
        original_request=original_request,
        round_count=round_count,
        deadline_monotonic=time.monotonic() + DEFAULT_MCP_INPUT_REQUIRED_TIMEOUT_SECONDS,
    )


def _decode_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value)
    except Exception:
        return value.encode("utf-8", errors="replace")


def _safe_protocol_version(client: Client) -> str | None:
    try:
        return client.protocol_version
    except Exception:
        return None


def _generation() -> int:
    return int(time.time() * 1000)


def _is_missing_auth(config: McpServerConfig, exc: Exception) -> bool:
    return isinstance(config.transport, McpStreamableHttpConfig) and "missing bearer token" in str(exc)


def _redact_diagnostic(message: str, config: McpServerConfig) -> str:
    transport = config.transport
    redacted = message
    if isinstance(transport, McpStreamableHttpConfig):
        redacted_url = _redact_url(transport.url)
        redacted = redacted.replace(transport.url, redacted_url)
        parsed = urlsplit(transport.url)
        if parsed.username or parsed.password:
            userinfo = parsed.netloc.rsplit("@", 1)[0]
            if userinfo:
                redacted = redacted.replace(userinfo, "<redacted-userinfo>")
        for value in transport.headers.values():
            if value:
                redacted = redacted.replace(value, "<redacted>")
        for env_var in [transport.bearer_token_env_var, *transport.env_headers.values()]:
            if env_var:
                token = os.getenv(env_var)
                if token:
                    redacted = redacted.replace(token, "<redacted>")
    if isinstance(transport, McpStdioConfig):
        for value in transport.env.values():
            if value:
                redacted = redacted.replace(value, "<redacted>")
    return redacted


def _redact_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    if parsed.username or parsed.password:
        host = f"<redacted-userinfo>@{host}"
    suffix = "<redacted-query-or-fragment>" if parsed.query or parsed.fragment else ""
    return urlunsplit(
        SplitResult(
            scheme=parsed.scheme,
            netloc=host,
            path=parsed.path,
            query="",
            fragment="",
        )
    ) + suffix
