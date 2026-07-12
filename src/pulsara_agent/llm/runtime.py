"""Resolved-target LLM runtime."""

from __future__ import annotations

from typing import AsyncIterator

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RunErrorEvent,
)
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.resolution import (
    ResolvedModelCall,
    ResolvedModelTarget,
    rebind_model_target,
    resolve_model_call,
    resolve_model_target,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.validation import validate_model_context_for_call
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ResolvedModelTargetFact,
)
from pulsara_agent.llm.errors import LLMTransportContractError


class LLMRuntime:
    def __init__(self, *, config: LLMConfig, registry: LLMTransportRegistry) -> None:
        self._config = config
        self._registry = registry

    def resolve_target(
        self,
        *,
        role: ModelRole,
        requested_options: LLMOptions | None = None,
    ) -> ResolvedModelTarget:
        return resolve_model_target(
            config=self._config,
            registry=self._registry,
            role=role,
            requested_options=requested_options,
        )

    def resolve_call(
        self,
        *,
        target: ResolvedModelTarget,
        purpose: ModelCallPurpose,
    ) -> ResolvedModelCall:
        return resolve_model_call(target=target, purpose=purpose)

    def rebind_target(self, fact: ResolvedModelTargetFact) -> ResolvedModelTarget:
        return rebind_model_target(
            config=self._config, registry=self._registry, fact=fact
        )

    def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        return self._stream_reply(
            call=call,
            context=context,
            event_context=event_context,
        )

    async def _stream_reply(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        validation = validate_model_context_for_call(call=call, context=context)
        yield ReplyStartEvent(**event_context.event_fields(), name="assistant")
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            resolved_call=call.fact,
            context_id=context.context_id or "",
            model_call_index=context.model_call_index,
        )
        usage_report: TransportUsageReport | None = None
        outcome = "completed"
        try:
            async for item in call.target.transport.stream(
                call=call,
                context=context,
                event_context=event_context,
            ):
                if isinstance(item, TransportUsageReport):
                    if usage_report is not None:
                        raise LLMTransportContractError(
                            "transport emitted more than one usage report",
                            reason_code="transport_usage_report_duplicate",
                        )
                    usage_report = item
                    continue
                if isinstance(
                    item,
                    (
                        ModelCallStartEvent,
                        ModelCallEndEvent,
                        ReplyStartEvent,
                        ReplyEndEvent,
                    ),
                ):
                    reason_code = {
                        ModelCallStartEvent: "transport_emitted_model_call_start",
                        ModelCallEndEvent: "transport_emitted_model_call_end",
                        ReplyStartEvent: "transport_emitted_reply_start",
                        ReplyEndEvent: "transport_emitted_reply_end",
                    }[type(item)]
                    raise LLMTransportContractError(
                        f"transport emitted forbidden lifecycle event: {item.type}",
                        reason_code=reason_code,
                    )
                if isinstance(item, RunErrorEvent):
                    outcome = "provider_error"
                yield item
        except Exception as exc:
            # Direct subsystem terminal facts still need the already completed
            # pre-send measurement when a transport fails before ModelCallEnd.
            exc.estimate = validation.estimate  # type: ignore[attr-defined]
            raise
        usage_report = usage_report or TransportUsageReport(
            usage_status="missing",
            usage=None,
        )
        yield ModelCallEndEvent(
            **event_context.event_fields(),
            resolved_model_call_id=call.fact.resolved_model_call_id,
            target_fingerprint=call.target.fact.target_fingerprint,
            reported_model_id=usage_report.reported_model_id,
            outcome=outcome,
            usage_status=usage_report.usage_status,
            usage=usage_report.usage,
            estimated_input_tokens=validation.estimate.total_input_tokens,
            diagnostics=usage_report.provider_diagnostics,
        )
        yield ReplyEndEvent(**event_context.event_fields())
