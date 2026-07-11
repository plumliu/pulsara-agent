"""Session-owned MCP manager protocols and deterministic test managers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from pulsara_agent.runtime.mcp.types import (
    McpInputRequiredResolution,
    McpOriginalRequest,
    McpServerSnapshot,
)


class McpClientManager(Protocol):
    @property
    def snapshots(self) -> tuple[McpServerSnapshot, ...]:
        """Current session-scoped MCP server/tool snapshot."""

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> Any:
        """Execute an MCP tool via the manager-owned client."""

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        """Close clients/processes. Must be idempotent."""

    def cancel_active(self) -> None:
        """Best-effort cancellation signal for active MCP calls."""

    async def resume_suspended_request(
        self,
        *,
        server_id: str,
        original_request: McpOriginalRequest,
        request_state: str | None,
        resolution: McpInputRequiredResolution,
        timeout_ms: int,
    ) -> Any:
        """Resume a modern MCP InputRequiredResult through Pulsara-owned DTOs."""

McpToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


@dataclass(slots=True)
class MockMcpClientManager:
    """In-process MCP manager for capability/runtime tests.

    It owns no network/process resources, but it implements the same lifecycle
    contract as real managers: close is idempotent, active calls are tracked,
    and calls after close fail structurally.
    """

    _snapshots: tuple[McpServerSnapshot, ...]
    handlers: dict[tuple[str, str], McpToolHandler] = field(default_factory=dict)
    close_count: int = 0
    cancel_count: int = 0
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    _closed: bool = False
    _active_tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)

    @property
    def snapshots(self) -> tuple[McpServerSnapshot, ...]:
        return self._snapshots

    @property
    def closed(self) -> bool:
        return self._closed

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> Any:
        if self._closed:
            raise RuntimeError("MCP manager is closed")
        self.calls.append((server_id, tool_name, dict(arguments)))
        handler = self.handlers.get((server_id, tool_name))
        if handler is None:
            raise KeyError(f"Unknown MCP tool binding: {server_id}/{tool_name}")
        task = asyncio.create_task(_await_handler(handler, dict(arguments)))
        self._active_tasks.add(task)
        try:
            return await asyncio.wait_for(task, timeout=timeout_ms / 1000)
        finally:
            self._active_tasks.discard(task)

    def cancel_active(self) -> None:
        self.cancel_count += 1
        for task in tuple(self._active_tasks):
            task.cancel()

    async def resume_suspended_request(
        self,
        *,
        server_id: str,
        original_request: McpOriginalRequest,
        request_state: str | None,
        resolution: McpInputRequiredResolution,
        timeout_ms: int,
    ) -> Any:
        del request_state
        if resolution.cancelled:
            return {"cancelled": True, "interaction_id": resolution.interaction_id}
        if original_request.tool_name is None:
            raise RuntimeError("mock MCP manager only resumes tool calls")
        return await self.call_tool(
            server_id,
            original_request.tool_name,
            original_request.arguments or {},
            timeout_ms=timeout_ms,
        )

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_count += 1
        self.cancel_active()
        if self._active_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tuple(self._active_tasks), return_exceptions=True),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                pass


async def _await_handler(handler: McpToolHandler, arguments: dict[str, Any]) -> Any:
    result = handler(arguments)
    if hasattr(result, "__await__"):
        return await result  # type: ignore[misc]
    return result
