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
from typing import Any, Callable
from urllib.parse import SplitResult, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import mcp_types as types
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from pulsara_agent.runtime.mcp.manager import McpClientManager
from pulsara_agent.runtime.mcp.types import (
    McpContentArtifact,
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
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class SdkMcpClientManager(McpClientManager):
    """Session-owned official Python MCP SDK v2 manager."""

    _snapshots: tuple[McpServerSnapshot, ...]
    _connections: dict[str, _SdkServerConnection]
    max_pages: int = DEFAULT_MCP_MAX_PAGES
    max_items: int = DEFAULT_MCP_MAX_ITEMS
    _closed: bool = False
    _active_tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)

    @classmethod
    async def start(
        cls,
        configs: tuple[McpServerConfig, ...],
        *,
        max_pages: int = DEFAULT_MCP_MAX_PAGES,
        max_items: int = DEFAULT_MCP_MAX_ITEMS,
    ) -> "SdkMcpClientManager":
        manager = cls(_snapshots=(), _connections={}, max_pages=max_pages, max_items=max_items)
        snapshots: list[McpServerSnapshot] = []
        for config in configs:
            snapshots.append(await manager._connect_one(config))
        manager._snapshots = tuple(snapshots)
        return manager

    @property
    def snapshots(self) -> tuple[McpServerSnapshot, ...]:
        return self._snapshots

    async def refresh(self) -> tuple[McpServerSnapshot, ...]:
        snapshots: list[McpServerSnapshot] = []
        for connection in tuple(self._connections.values()):
            snapshots.append(await self._discover_connected(connection))
        self._snapshots = tuple(snapshots)
        return self._snapshots

    async def _connect_one(self, config: McpServerConfig) -> McpServerSnapshot:
        if not config.enabled:
            return McpServerSnapshot(config=config, status=McpServerStatus.DISABLED)
        try:
            client, http_client = _build_sdk_client(config)
            await asyncio.wait_for(client.__aenter__(), timeout=config.startup_timeout_ms / 1000)
            connection = _SdkServerConnection(config=config, client=client, http_client=http_client)
            self._connections[config.server_id] = connection
            return await self._discover_connected(connection)
        except Exception as exc:
            await self._close_connection(config.server_id)
            status = McpServerStatus.NEEDS_AUTH if _is_missing_auth(config, exc) else McpServerStatus.FAILED
            return McpServerSnapshot(
                config=config,
                status=status,
                message=_redact_diagnostic(f"{type(exc).__name__}: {exc}", config),
            )

    async def _discover_connected(self, connection: _SdkServerConnection) -> McpServerSnapshot:
        config = connection.config
        diagnostics: list[dict[str, Any]] = []
        try:
            tools = await self._list_all(
                "tools/list",
                lambda cursor: connection.client.session.list_tools(
                    params=types.PaginatedRequestParams(cursor=cursor)
                ),
                diagnostics,
                item_attr="tools",
            )
            resources = await self._list_optional(
                "resources/list",
                lambda cursor: connection.client.session.list_resources(
                    params=types.PaginatedRequestParams(cursor=cursor)
                ),
                diagnostics,
                config=config,
                item_attr="resources",
            )
            resource_templates = await self._list_optional(
                "resources/templates/list",
                lambda cursor: connection.client.session.list_resource_templates(
                    params=types.PaginatedRequestParams(cursor=cursor)
                ),
                diagnostics,
                config=config,
                item_attr="resource_templates",
            )
            prompts = await self._list_optional(
                "prompts/list",
                lambda cursor: connection.client.session.list_prompts(
                    params=types.PaginatedRequestParams(cursor=cursor)
                ),
                diagnostics,
                config=config,
                item_attr="prompts",
            )
            return McpServerSnapshot(
                config=config,
                status=McpServerStatus.READY,
                tools=tuple(_tool_from_sdk(config.server_id, tool) for tool in tools),
                resources=tuple(_resource_from_sdk(config.server_id, resource) for resource in resources),
                resource_templates=tuple(
                    _resource_template_from_sdk(config.server_id, template)
                    for template in resource_templates
                ),
                prompts=tuple(_prompt_from_sdk(config.server_id, prompt) for prompt in prompts),
                generation=_generation(),
                protocol_version=connection.client.protocol_version,
                server_info=connection.client.server_info.model_dump(mode="json", by_alias=True),
                instructions=connection.client.instructions,
                diagnostics=tuple(diagnostics),
            )
        except Exception as exc:
            return McpServerSnapshot(
                config=config,
                status=McpServerStatus.DEGRADED,
                message=_redact_diagnostic(f"{type(exc).__name__}: {exc}", config),
                generation=_generation(),
                protocol_version=_safe_protocol_version(connection.client),
                diagnostics=tuple(diagnostics),
            )

    async def _list_all(
        self,
        method: str,
        fetch: Callable[[str | None], Any],
        diagnostics: list[dict[str, Any]],
        *,
        item_attr: str,
    ) -> list[Any]:
        items: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for page in range(1, self.max_pages + 1):
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

    async def _list_optional(
        self,
        method: str,
        fetch: Callable[[str | None], Any],
        diagnostics: list[dict[str, Any]],
        *,
        config: McpServerConfig,
        item_attr: str,
    ) -> list[Any]:
        try:
            return await self._list_all(method, fetch, diagnostics, item_attr=item_attr)
        except Exception as exc:
            diagnostics.append(
                {
                    "code": "mcp_optional_method_unavailable",
                    "method": method,
                    "error_type": type(exc).__name__,
                    "message": _redact_diagnostic(str(exc), config),
                }
            )
            return []

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
        self._closed = True
        self.cancel_active()
        if self._active_tasks:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(
                    asyncio.gather(*tuple(self._active_tasks), return_exceptions=True),
                    timeout=timeout_seconds,
                )
            _clear_current_task_cancellation()
        close_tasks = tuple(self._close_connection(server_id) for server_id in tuple(self._connections))
        if close_tasks:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(
                    asyncio.gather(*close_tasks, return_exceptions=True),
                    timeout=timeout_seconds,
                )
            _clear_current_task_cancellation()

    async def _close_connection(self, server_id: str) -> None:
        connection = self._connections.pop(server_id, None)
        if connection is None:
            return
        await _best_effort_sdk_close_step(_terminate_sdk_stdio_process(connection.client))
        await _best_effort_sdk_close_step(connection.client.__aexit__(None, None, None))
        if connection.http_client is not None:
            await _best_effort_sdk_close_step(connection.http_client.aclose())

    def _require_connection(self, server_id: str) -> _SdkServerConnection:
        if self._closed:
            raise RuntimeError("MCP SDK manager is closed")
        try:
            return self._connections[server_id]
        except KeyError as exc:
            raise KeyError(f"Unknown MCP server: {server_id}") from exc


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
