"""Result envelopes, invocation budgets, and admitted operation settlement."""

from __future__ import annotations

import asyncio
import json
from pulsara_agent.identity import derive_id
from datetime import datetime, timezone
from time import monotonic
from pulsara_agent.message import ToolResultState
from pulsara_agent.primitives.tool_result_budget import (
    ToolResultPageBudget,
    ToolResultPageBoundary,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceFormatHint
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.tool_observation import (
    PhysicalToolObservationSupplement,
    ToolObservationOrigin,
    normalize_observation_duration,
)
from pulsara_agent.conversation_kernel.tool_surface import McpEffectKind
from .tool_contracts import KernelToolInvocationContext, KernelToolResult
from .mcp.supervisor import (
    McpDispatchAdmissionPermit,
    McpDispatchPermitState,
    McpKnownToolResult,
)


async def _await_mcp_operation(
    task: asyncio.Task[McpKnownToolResult],
    *,
    permit: McpDispatchAdmissionPermit | None = None,
) -> tuple[McpKnownToolResult | None, bool]:
    """Cancel an undispatched lane wait; join any physical call to settlement."""

    caller_cancelled = False
    cancelled_before_dispatch = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            caller_cancelled = True
            if (permit is not None and not cancelled_before_dispatch
                    and permit.state in {McpDispatchPermitState.ADMITTED, McpDispatchPermitState.ATTEMPT_ACCEPTED}):
                # State changes and cancellation occur on the same event loop.
                # Once LANE_ACQUIRED, the executor enters the physical call
                # without yielding and its result must be joined instead.
                cancelled_before_dispatch = True
                task.cancel()
            continue
        except BaseException:
            break
    if cancelled_before_dispatch and task.cancelled():
        # A task cancelled before its first step cannot run its own cleanup.
        if permit is not None and permit.state is not McpDispatchPermitState.RELEASED:
            permit.release()
        return None, True
    return task.result(), caller_cancelled


def _cancelled_mcp_before_dispatch(effect_kind):
    return KernelToolResult(
        state="CANCELLED_BEFORE_DISPATCH",
        content="Turn 已停止，此 MCP 操作未派发。".encode("utf-8"),
        caller_cancelled_while_running=True,
        effect_class="read_only" if effect_kind is McpEffectKind.READ_ONLY else "unknown_effect",
    )


def _kernel_tool_state(status: ToolResultState) -> str:
    return {
        ToolResultState.SUCCESS: "SUCCESS", ToolResultState.ERROR: "APPLICATION_ERROR",
        ToolResultState.INTERRUPTED: "CANCELLED", ToolResultState.DENIED: "APPLICATION_ERROR",
        ToolResultState.RUNNING: "SUCCESS",
    }[status]


def _invocation_page_budget(context: KernelToolInvocationContext) -> ToolResultPageBudget:
    budget = getattr(context, "page_budget", None)
    if budget is not None:
        return budget
    if context.surface_borrow.code_cell_id is not None:
        return ToolResultPageBudget(tool_call_id=context.tool_call_id)
    raise ToolResultPageBoundary("native invocation has no allocated result budget")


def _no_effect_budget_result() -> KernelToolResult:
    facts = {"error": "OUTPUT_PAGE_BOUNDARY", "physical_outcome": "NO_EFFECT", "retry_performed": False}
    return KernelToolResult(state="APPLICATION_ERROR", content=canonical_json_bytes(facts),
        required_facts=facts, public_format=ToolOutputSourceFormatHint.JSON, effect_class="read_only")


def _kernel_result_from_mcp_known(
    known: McpKnownToolResult,
    *,
    caller_cancelled: bool,
    effect_kind: McpEffectKind,
    physical_observation: PhysicalToolObservationSupplement,
) -> KernelToolResult:
    return KernelToolResult(public_format=ToolOutputSourceFormatHint.JSON,
        state=known.state,
        content=known.content,
        remote_identity=known.remote_identity,
        remote_identity_verification=known.remote_identity_verification,
        caller_cancelled_while_running=caller_cancelled,
        effect_class=(
            "read_only" if effect_kind is McpEffectKind.READ_ONLY else "unknown_effect"
        ),
        physical_observation=physical_observation,
    )


def _freeze_physical_observation(
    started_at_monotonic: float,
    origin: ToolObservationOrigin,
) -> PhysicalToolObservationSupplement:
    elapsed = normalize_observation_duration(
        max(0, int((monotonic() - started_at_monotonic) * 1_000_000))
    )
    return PhysicalToolObservationSupplement(
        observed_at=datetime.now(timezone.utc),
        elapsed_microseconds=elapsed,
        observation_origin_kind=origin,
    )


def _mcp_unavailable_reason(reason: str) -> str:
    return {
        "REMOVED": "The server no longer lists this tool.",
        "SOURCE_UNAVAILABLE": "The server's current tool list is unavailable.",
        "DIRTY_FENCED": "The server's tool list is being refreshed.",
        "SCOPE_INVISIBLE": "This tool is not available to this agent.",
        "NATIVE_CONTRACT_REQUIRES_META": "This tool's previous entry can no longer be used.",
        "DESCRIPTOR_OVERBOUND": "The tool definition is too large to inspect, so this tool cannot be called.",
    }.get(reason, "The tool is not currently available.")


def _mcp_unavailable_inspection_message(reason: str, server_id: str) -> str:
    target = json.dumps({"kind": "MCP_SERVER", "runtime_server_id": derive_id("mcp-server", "runtime-target", server_id)}, ensure_ascii=False)
    return _mcp_unavailable_reason(reason) + (
        f" If this blocks the task, check the server with query_capabilities target={target}."
    )


def _mcp_unavailable_direct_message(reason: str, target, provider_tool_name: str) -> str:
    inspect_target = json.dumps({"kind": "MCP_TOOL", "server_id": target.server_id,
        "tool_name": provider_tool_name}, ensure_ascii=False)
    if reason == "NATIVE_CONTRACT_REQUIRES_META":
        return (
            "This direct tool entry has been replaced; this call was not dispatched. "
            f"Use query_capabilities with target={inspect_target} and follow invocation.mode. "
            "For META, use the returned input_schema and invocation.target with use_new_mcp_tool."
        )
    return (
        "This call was not dispatched. " + _mcp_unavailable_reason(reason)
        + f" Use query_capabilities with target={inspect_target} for its current availability."
    )
