"""MCP tool execution adapters.

The adapter is intentionally thin: it owns no MCP client or server process and
only delegates to the session-owned ``McpClientManager``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime.mcp.manager import McpClientManager
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult, ToolExecutionSuspended, ToolRuntimeContext


@dataclass(frozen=True, slots=True)
class McpCapabilityTool:
    name: str
    description: str
    parameters: dict[str, Any]
    server_id: str
    original_tool_name: str
    client_manager: McpClientManager
    timeout_ms: int
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult | ToolExecutionSuspended:
        del runtime_context
        elicitation = call.arguments.get("__mcp_elicitation__")
        if isinstance(elicitation, dict):
            request_id = str(elicitation.get("request_id") or "")
            if not request_id:
                raise ValueError("__mcp_elicitation__.request_id is required")
            return ToolExecutionSuspended(
                tool_call_id=call.id,
                tool_name=call.name,
                interaction_kind="mcp_elicitation",
                payload={
                    "interaction_id": f"mcp_elicitation:{request_id}",
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "server_id": self.server_id,
                    "request_id": request_id,
                    "prompt": str(elicitation.get("prompt") or "MCP server requested input."),
                    "schema": dict(elicitation.get("schema") or {}),
                },
            )
        result = await self.client_manager.call_tool(
            self.server_id,
            self.original_tool_name,
            dict(call.arguments),
            timeout_ms=self.timeout_ms,
        )
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=_format_mcp_result(result),
            metadata={
                "provider_kind": "mcp",
                "server_id": self.server_id,
                "original_tool_name": self.original_tool_name,
            },
        )

    async def resume_elicitation(
        self,
        *,
        request_id: str,
        answer: dict[str, Any],
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        del runtime_context
        result = await self.client_manager.respond_elicitation(self.server_id, request_id, answer)
        return ToolExecutionResult(
            call_id=answer["tool_call_id"],
            tool_name=self.name,
            status=ToolResultState.SUCCESS,
            output=_format_mcp_result(result),
            metadata={
                "provider_kind": "mcp",
                "server_id": self.server_id,
                "original_tool_name": self.original_tool_name,
                "mcp_elicitation_request_id": request_id,
            },
        )


def _format_mcp_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    except TypeError:
        return str(result)
