"""Shared collection for direct subsystem model calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pulsara_agent.event import (
    ModelCallEndEvent,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.execution import ModelStreamExecutionHandle
from pulsara_agent.primitives.model_call import (
    ModelCallDiagnosticFact,
    ModelTokenUsageFact,
    ResolvedModelCallFact,
)


class DirectModelCallCollectionError(RuntimeError):
    def __init__(self, message: str, *, partial_text: str = "") -> None:
        super().__init__(message)
        self.partial_text = partial_text


@dataclass(frozen=True, slots=True)
class DirectModelCallResult:
    text: str
    resolved_call: ResolvedModelCallFact
    estimated_input_tokens: int
    outcome: Literal["completed", "provider_error"]
    error: ModelCallDiagnosticFact | None
    usage_status: Literal["reported", "missing"]
    usage: ModelTokenUsageFact | None
    reported_model_id: str | None


async def collect_direct_model_call_handle(
    handle: ModelStreamExecutionHandle,
    *,
    expected_call: ResolvedModelCall,
) -> DirectModelCallResult:
    """Materialize one direct result from the session-owned durable worker."""

    completion = await handle.wait_completed()
    if completion.terminal_outcome == "reconciliation_blocked":
        raise DirectModelCallCollectionError(
            "direct model result is blocked by stream reconciliation"
        )
    if completion.terminal_outcome == "rejected_before_start":
        # Validation runs inside the service-owned worker. The handle retains
        # the original typed error (including its token estimate), so direct
        # subsystems must observe that contract rather than a lossy wrapper.
        await handle.wait_result()
        raise DirectModelCallCollectionError(
            "rejected model call completed without its validation error"
        )
    result = await handle.wait_result()
    model_ends = tuple(
        event
        for event in completion.committed_events
        if isinstance(event, ModelCallEndEvent)
        and event.resolved_model_call_id
        == expected_call.fact.resolved_model_call_id
    )
    if len(model_ends) != 1:
        raise DirectModelCallCollectionError(
            "direct model result lacks one matching canonical end"
        )
    model_end = model_ends[0]
    if result.resolved_model_call_id != expected_call.fact.resolved_model_call_id:
        raise DirectModelCallCollectionError(
            "direct model result call identity mismatch"
        )
    if model_end.target_fingerprint != expected_call.target.fact.target_fingerprint:
        raise DirectModelCallCollectionError(
            "direct model result target fingerprint mismatch"
        )
    if result.terminal_outcome in {"cancelled", "runtime_error"}:
        raise DirectModelCallCollectionError(
            f"direct model call ended with {result.terminal_outcome}",
            partial_text=result.combined_text,
        )
    error = None
    if result.terminal_outcome == "provider_error":
        provider_error = result.provider_errors[0]
        error = ModelCallDiagnosticFact(
            code=provider_error.code.value,
            message=provider_error.message,
        )
    return DirectModelCallResult(
        text=result.combined_text,
        resolved_call=expected_call.fact,
        estimated_input_tokens=model_end.estimated_input_tokens,
        outcome=result.terminal_outcome,
        error=error,
        usage_status=result.usage_status,
        usage=result.usage,
        reported_model_id=result.reported_model_id,
    )
