"""Result envelopes, invocation budgets, and admitted operation settlement."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import monotonic
from typing import TypeVar
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolBusinessValue
from .content_parts import ResultPart
from pulsara_agent.primitives.tool_observation import (
    PhysicalToolObservationSupplement,
    ToolObservationOrigin,
    normalize_observation_duration,
)
from pulsara_agent.conversation_kernel.tool_surface import McpEffectKind
from .tool_contracts import KernelToolResult
from .mcp.supervisor import (
    McpDispatchAdmissionPermit,
    McpDispatchPermitState,
    McpKnownToolResult,
)


_McpOperationResult = TypeVar("_McpOperationResult")


async def _await_mcp_operation(
    task: asyncio.Task[_McpOperationResult],
    *,
    permit: McpDispatchAdmissionPermit | None = None,
) -> tuple[_McpOperationResult | None, bool]:
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
        content=ToolBusinessValue(content=(ResultPart("text", "Turn 已停止，此 MCP 操作未派发。"),)),
        caller_cancelled_while_running=True,
        effect_class="read_only" if effect_kind is McpEffectKind.READ_ONLY else "unknown_effect",
    )


def _kernel_tool_state(status: ToolResultState) -> str:
    return {
        ToolResultState.SUCCESS: "SUCCESS", ToolResultState.ERROR: "APPLICATION_ERROR",
        ToolResultState.INTERRUPTED: "CANCELLED", ToolResultState.DENIED: "APPLICATION_ERROR",
        ToolResultState.RUNNING: "SUCCESS",
    }[status]






async def _kernel_result_from_mcp_known(
    known: McpKnownToolResult,
    *,
    caller_cancelled: bool,
    effect_kind: McpEffectKind,
    physical_observation: PhysicalToolObservationSupplement,
    image_validator=None,
    deadline_monotonic: float,
) -> KernelToolResult:
    from .mcp.content import adapt_known_result
    # The remote result is already known. Local image validation belongs to
    # its settlement, so Stop must join it just as it joins a dispatched call.
    adaptation = asyncio.create_task(
        adapt_known_result(known, validator=image_validator, deadline=deadline_monotonic),
        name="mcp-known-result-adaptation",
    )
    adapted, adaptation_cancelled = await _await_mcp_operation(adaptation)
    assert adapted is not None
    business, images, state = adapted
    return KernelToolResult(
        state="APPLICATION_ERROR" if state == "ERROR" else state,
        content=business,
        pending_images=images,
        remote_identity=known.remote_identity,
        remote_identity_verification=known.remote_identity_verification,
        caller_cancelled_while_running=caller_cancelled or adaptation_cancelled,
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
