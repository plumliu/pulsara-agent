"""Shared collection for direct subsystem model calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Literal

from pulsara_agent.event import (
    AgentEvent,
    ModelCallEndEvent,
    ReplyEndEvent,
    RunErrorEvent,
    TextBlockDeltaEvent,
)
from pulsara_agent.llm.errors import (
    ModelContextIdentityMismatch,
    ModelInputBudgetExceeded,
    ModelInputEstimateMismatch,
    ModelTargetBindingMismatch,
    ModelTargetCapabilityMismatch,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
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


async def collect_direct_model_call(
    events: AsyncIterator[AgentEvent],
    *,
    expected_call: ResolvedModelCall,
) -> DirectModelCallResult:
    parts: list[str] = []
    run_error: RunErrorEvent | None = None
    model_end: ModelCallEndEvent | None = None
    reply_end_seen = False
    try:
        async for event in events:
            if isinstance(event, TextBlockDeltaEvent):
                parts.append(event.delta)
            elif isinstance(event, RunErrorEvent):
                if run_error is not None:
                    raise DirectModelCallCollectionError(
                        "direct model stream emitted multiple RunErrorEvent values",
                        partial_text="".join(parts),
                    )
                run_error = event
            elif isinstance(event, ModelCallEndEvent):
                if model_end is not None:
                    raise DirectModelCallCollectionError(
                        "direct model stream emitted multiple ModelCallEndEvent values",
                        partial_text="".join(parts),
                    )
                if (
                    event.resolved_model_call_id
                    != expected_call.fact.resolved_model_call_id
                ):
                    raise DirectModelCallCollectionError(
                        "direct model end call identity mismatch",
                        partial_text="".join(parts),
                    )
                if (
                    event.target_fingerprint
                    != expected_call.target.fact.target_fingerprint
                ):
                    raise DirectModelCallCollectionError(
                        "direct model end target fingerprint mismatch",
                        partial_text="".join(parts),
                    )
                model_end = event
            elif isinstance(event, ReplyEndEvent):
                reply_end_seen = True
    except asyncio.CancelledError:
        raise
    except (
        ModelInputBudgetExceeded,
        ModelInputEstimateMismatch,
        ModelContextIdentityMismatch,
        ModelTargetBindingMismatch,
        ModelTargetCapabilityMismatch,
    ):
        # These are pre-start validation rejections owned by LLMRuntime.  The
        # subsystem needs the stable type/reason code to write its own terminal
        # failure fact; treating them as an interrupted transport stream would
        # misclassify a provider call that never began.
        raise
    except DirectModelCallCollectionError:
        raise
    except Exception as exc:
        wrapped = DirectModelCallCollectionError(
            f"direct model transport failed before canonical end: {type(exc).__name__}",
            partial_text="".join(parts),
        )
        estimate = getattr(exc, "estimate", None)
        if estimate is not None:
            wrapped.estimate = estimate  # type: ignore[attr-defined]
        raise wrapped from exc
    text = "".join(parts)
    if model_end is None or not reply_end_seen:
        raise DirectModelCallCollectionError(
            "direct model stream ended without canonical end lifecycle",
            partial_text=text,
        )
    if run_error is not None and model_end.outcome != "provider_error":
        raise DirectModelCallCollectionError(
            "RunErrorEvent was not closed by provider_error ModelCallEndEvent",
            partial_text=text,
        )
    if run_error is None and model_end.outcome != "completed":
        raise DirectModelCallCollectionError(
            "provider_error model end is missing RunErrorEvent",
            partial_text=text,
        )
    error = (
        ModelCallDiagnosticFact(
            code=run_error.code[:96] or "provider_error",
            message=run_error.message[:512],
        )
        if run_error is not None
        else None
    )
    return DirectModelCallResult(
        text=text,
        resolved_call=expected_call.fact,
        estimated_input_tokens=model_end.estimated_input_tokens,
        outcome=model_end.outcome,
        error=error,
        usage_status=model_end.usage_status,
        usage=model_end.usage,
        reported_model_id=model_end.reported_model_id,
    )
