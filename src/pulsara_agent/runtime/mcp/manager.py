"""Session-owned MCP manager protocols and deterministic test managers."""

from __future__ import annotations

from typing import Any, Protocol

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
