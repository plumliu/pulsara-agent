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
from pulsara_agent.runtime.mcp.types import (
    McpContentArtifact,
    McpInputRequired,
    McpInputRequiredResolution,
    McpOriginalRequest,
    McpRequestSourceMethod,
    McpToolResult,
    redact_mcp_error_message,
)
from pulsara_agent.tools.base import (
    ToolCall,
    ToolExecutionResult,
    ToolExecutionSuspended,
    ToolResultArtifactCandidate,
    ToolRuntimeContext,
)


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
        try:
            result = await self.client_manager.call_tool(
                self.server_id,
                self.original_tool_name,
                dict(call.arguments),
                timeout_ms=self.timeout_ms,
            )
        except Exception as exc:
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.ERROR,
                output=f"[MCP_ERROR] {type(exc).__name__}: {redact_mcp_error_message(exc)}",
            )
        if isinstance(result, McpInputRequired):
            payload = result.to_payload()
            payload.update(
                {
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "wrapper_tool_call_id": call.id,
                    "wrapper_tool_name": call.name,
                    "server_id": self.server_id,
                    "original_tool_name": self.original_tool_name,
                }
            )
            return ToolExecutionSuspended(
                tool_call_id=call.id,
                tool_name=call.name,
                interaction_kind="mcp_input_required",
                payload=payload,
            )
        normalized = _normalize_mcp_result(result)
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.ERROR if normalized.is_error else ToolResultState.SUCCESS,
            output=normalized.output,
            metadata={
                "provider_kind": "mcp",
                "server_id": self.server_id,
                "original_tool_name": self.original_tool_name,
                **normalized.metadata,
            },
            artifact_candidates=_artifact_candidates(normalized.artifacts),
        )

    async def resume_input_required(
        self,
        *,
        original_request: dict[str, Any],
        request_state: str | None,
        resolution: McpInputRequiredResolution,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult | ToolExecutionSuspended:
        del runtime_context
        result = await self.client_manager.resume_suspended_request(
            server_id=self.server_id,
            original_request=_original_request_from_payload(original_request),
            request_state=request_state,
            resolution=resolution,
            timeout_ms=self.timeout_ms,
        )
        if isinstance(result, McpInputRequired):
            payload = result.to_payload()
            payload.update(
                {
                    "tool_call_id": resolution.tool_call_id or "",
                    "tool_name": self.name,
                    "wrapper_tool_call_id": resolution.tool_call_id or "",
                    "wrapper_tool_name": self.name,
                    "server_id": self.server_id,
                    "original_tool_name": self.original_tool_name,
                }
            )
            return ToolExecutionSuspended(
                tool_call_id=str(payload.get("tool_call_id") or ""),
                tool_name=self.name,
                interaction_kind="mcp_input_required",
                payload=payload,
            )
        normalized = _normalize_mcp_result(result)
        tool_call_id = str(resolution.tool_call_id or "")
        return ToolExecutionResult(
            call_id=tool_call_id,
            tool_name=self.name,
            status=ToolResultState.ERROR if normalized.is_error else ToolResultState.SUCCESS,
            output=normalized.output,
            metadata={
                "provider_kind": "mcp",
                "server_id": self.server_id,
                "original_tool_name": self.original_tool_name,
                "mcp_input_required_interaction_id": resolution.interaction_id,
                **normalized.metadata,
            },
            artifact_candidates=_artifact_candidates(normalized.artifacts),
        )


def _normalize_mcp_result(result: Any) -> McpToolResult:
    if isinstance(result, McpToolResult):
        return result
    if isinstance(result, str):
        return McpToolResult(output=result)
    try:
        return McpToolResult(output=json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    except TypeError:
        return McpToolResult(output=str(result))


def _artifact_candidates(artifacts: tuple[McpContentArtifact, ...]) -> tuple[ToolResultArtifactCandidate, ...]:
    candidates = []
    for artifact in artifacts:
        candidates.append(
            ToolResultArtifactCandidate(
                role=artifact.role,
                media_type=artifact.media_type,
                text=artifact.text,
                data=artifact.data,
                metadata=artifact.metadata,
            )
        )
    return tuple(candidates)


def _original_request_from_payload(payload: dict[str, Any]) -> McpOriginalRequest:
    source = McpRequestSourceMethod(str(payload["source_method"]))
    arguments = payload.get("arguments")
    prompt_arguments = payload.get("prompt_arguments")
    return McpOriginalRequest(
        source_method=source,
        tool_name=str(payload["tool_name"]) if payload.get("tool_name") is not None else None,
        arguments=dict(arguments) if isinstance(arguments, dict) else None,
        resource_uri=str(payload["resource_uri"]) if payload.get("resource_uri") is not None else None,
        prompt_name=str(payload["prompt_name"]) if payload.get("prompt_name") is not None else None,
        prompt_arguments=dict(prompt_arguments) if isinstance(prompt_arguments, dict) else None,
    )
