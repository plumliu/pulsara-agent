"""Thin model-facing MCP tool binding.

Live managers, leases, protocol state, and resume ownership belong to the
runtime MCP execution port. This module intentionally imports no runtime code.
"""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.mcp import (
    McpInvocationOwner,
    McpToolCompletedOutcome,
    McpToolExecutionPort,
    McpToolRejectedOutcome,
    McpToolSuspendedOutcome,
    build_mcp_tool_execution_request,
)
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolExecutionResult,
    ToolExecutionSuspended,
    ToolRuntimeContext,
)
from pulsara_agent.ports.tool_registry import McpToolBindingContract


@dataclass(frozen=True, slots=True)
class McpCapabilityTool:
    binding: McpToolBindingContract
    execution_port: McpToolExecutionPort
    timeout_ms: int

    def __post_init__(self) -> None:
        if self.timeout_ms < 1:
            raise ValueError("MCP tool timeout must be positive")

    @property
    def name(self) -> str:
        return self.binding.tool_name

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult | ToolExecutionSuspended:
        if call.name != self.binding.tool_name:
            raise ValueError("MCP tool call/binding identity mismatch")
        owner = McpInvocationOwner(
            runtime_session_id=runtime_context.runtime_session_id,
            run_id=runtime_context.event_context.run_id,
            tool_call_id=call.id,
            event_context=runtime_context.event_context,
        )
        outcome = await self.execution_port.execute(
            build_mcp_tool_execution_request(
                owner=owner,
                exposed_tool_name=self.binding.tool_name,
                original_tool_name=self.binding.original_tool_name,
                binding=self.binding,
                arguments=call.arguments,
                timeout_ms=self.timeout_ms,
            )
        )
        if isinstance(outcome, McpToolCompletedOutcome):
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=outcome.result_state,
                output=outcome.normalized_output,
                metadata=outcome.normalized_metadata,
                artifact_candidates=outcome.artifact_candidates,
                display_payload=outcome.frozen_display_payload,
                semantics_input=outcome.semantics_input,
            )
        if isinstance(outcome, McpToolSuspendedOutcome):
            return ToolExecutionSuspended(
                tool_call_id=call.id,
                tool_name=call.name,
                interaction_kind="mcp_input_required",
                mcp_pending_handle=outcome.pending_handle,
            )
        if not isinstance(outcome, McpToolRejectedOutcome):
            raise TypeError("MCP execution port returned an unknown outcome")
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.ERROR,
            output=(
                f"[MCP_ERROR:{outcome.error_code.value}] {outcome.sanitized_message}"
            ),
            metadata={
                "provider_kind": "mcp",
                "mcp_reject_code": outcome.error_code.value,
                "retryable_in_same_live_owner": (outcome.retryable_in_same_live_owner),
            },
        )


__all__ = ["McpCapabilityTool"]
