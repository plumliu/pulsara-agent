"""Central model-target fixtures for hard-cut runtime tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import AsyncIterator

from pulsara_agent.event import AgentEvent, EventContext
from pulsara_agent.llm.config import LLMConfig, ModelSlotConfig
from pulsara_agent.llm.adapters.mock import MockTransport
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.provider import ProviderProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.llm.resolution import resolve_model_call, resolve_model_target
from pulsara_agent.primitives.model_call import (
    ContextBudgetReportEvent,
    CompactionTargetEstimateFact,
    ModelCallPurpose,
    ModelContextLimits,
    ResolvedModelCallFact,
    ResolvedModelTargetFact,
    ModelTokenUsageFact,
)


def compaction_completed_contract_fields(
    *,
    estimated_tokens_before: int = 10_000,
    estimated_tokens_after: int = 100,
) -> dict[str, object]:
    target = test_resolved_target_fact()
    summarizer = test_resolved_call_fact(
        purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
    )
    summary_actual = min(estimated_tokens_after, 50)
    target_estimate = CompactionTargetEstimateFact(
        estimate_scope="transcript_only",
        basis_context_id=None,
        target_fingerprint=target.target_fingerprint,
        non_transcript_baseline_tokens=None,
        transcript_tokens_before=estimated_tokens_before,
        estimated_tokens_before=estimated_tokens_before,
        summary_tokens_reserved=max(summary_actual, 256),
        retained_transcript_tokens=estimated_tokens_after - summary_actual,
        protected_transcript_tokens=0,
        summary_tokens_actual=summary_actual,
        transcript_tokens_after=estimated_tokens_after,
        estimated_tokens_after=estimated_tokens_after,
        predicted_post_target_reached=None,
    )
    return {
        "target_model_target": target,
        "target_input_budget_tokens": target.context_budget.input_budget_tokens,
        "post_compaction_target_tokens": max(
            1, target.context_budget.input_budget_tokens // 2
        ),
        "target_estimate": target_estimate,
        "summarizer_call": summarizer,
        "summarizer_context_id": "context:test-compaction",
        "summarizer_input_estimated_tokens": 64,
        "summarizer_input_budget_tokens": summarizer.target.context_budget.input_budget_tokens,
        "summarizer_usage_status": "missing",
        "summarizer_usage": None,
        "summarizer_estimated_input_tokens": 64,
        "summarizer_reported_model_id": None,
        "predicted_post_target_reached": None,
    }


def compaction_started_contract_fields(
    *,
    estimated_tokens_before: int = 10_000,
) -> dict[str, object]:
    target = test_resolved_target_fact()
    summarizer = test_resolved_call_fact(
        purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
    )
    target_estimate = CompactionTargetEstimateFact(
        estimate_scope="transcript_only",
        basis_context_id=None,
        target_fingerprint=target.target_fingerprint,
        non_transcript_baseline_tokens=None,
        transcript_tokens_before=estimated_tokens_before,
        estimated_tokens_before=estimated_tokens_before,
        summary_tokens_reserved=256,
        retained_transcript_tokens=0,
        protected_transcript_tokens=0,
        summary_tokens_actual=None,
        transcript_tokens_after=None,
        estimated_tokens_after=None,
        predicted_post_target_reached=None,
    )
    return {
        "target_model_target": target,
        "target_input_budget_tokens": target.context_budget.input_budget_tokens,
        "post_compaction_target_tokens": max(
            1, target.context_budget.input_budget_tokens // 2
        ),
        "target_estimate": target_estimate,
        "summarizer_call": summarizer,
        "summarizer_context_id": "context:test-compaction",
        "summarizer_input_estimated_tokens": 64,
        "summarizer_input_budget_tokens": summarizer.target.context_budget.input_budget_tokens,
    }


def compaction_failed_contract_fields() -> dict[str, object]:
    target = test_resolved_target_fact()
    return {
        "target_model_target": target,
        "target_input_budget_tokens": target.context_budget.input_budget_tokens,
        "post_compaction_target_tokens": max(
            1, target.context_budget.input_budget_tokens // 2
        ),
        "failure_stage": "planning",
    }


def context_compiled_contract_fields(
    *,
    estimated_tokens: int = 123,
    tools_estimated_tokens: int = 42,
    status: str = "compiled",
    non_transcript_baseline_tokens: int | None = None,
    resolved_call: ResolvedModelCallFact | None = None,
) -> dict[str, object]:
    call = resolved_call or test_resolved_call_fact()
    target = call.target
    baseline = (
        estimated_tokens - max(0, estimated_tokens // 3)
        if non_transcript_baseline_tokens is None
        else non_transcript_baseline_tokens
    )
    transcript = estimated_tokens - baseline
    if transcript < 0:
        raise ValueError("non-transcript baseline exceeds estimated token total")
    sections = max(0, estimated_tokens - tools_estimated_tokens)
    budget = ContextBudgetReportEvent(
        target_fingerprint=target.target_fingerprint,
        resolved_model_call_id=call.resolved_model_call_id,
        measurement_stage="final_payload",
        total_context_tokens=target.limits.total_context_tokens,
        max_input_tokens=target.limits.max_input_tokens,
        max_output_tokens=target.limits.max_output_tokens,
        effective_output_tokens=target.context_budget.effective_output_tokens,
        safety_margin_tokens=target.context_budget.safety_margin_tokens,
        input_budget_tokens=target.context_budget.input_budget_tokens,
        sections_estimated_tokens=sections,
        tools_estimated_tokens=tools_estimated_tokens,
        envelope_estimated_tokens=3,
        allocation_estimated_tokens=sections + tools_estimated_tokens,
        final_payload_estimated_tokens=estimated_tokens,
        non_transcript_baseline_tokens=baseline,
        transcript_estimated_tokens=transcript,
        estimator=target.token_estimator,
    )
    return {
        "status": status,
        "compile_attempt_index": 1,
        "context_retry_index": 0,
        "resolved_call": call,
        "budget": budget,
    }


def model_call_start_fields(
    *,
    context_id: str = "context:test",
    model_call_index: int = 1,
    resolved_call: ResolvedModelCallFact | None = None,
) -> dict[str, object]:
    return {
        "resolved_call": resolved_call or test_resolved_call_fact(),
        "context_id": context_id,
        "model_call_index": model_call_index,
    }


def model_call_end_fields(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_input_tokens: int | None = None,
    resolved_call: ResolvedModelCallFact | None = None,
) -> dict[str, object]:
    call = resolved_call or test_resolved_call_fact()
    usage = ModelTokenUsageFact(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )
    return {
        "resolved_model_call_id": call.resolved_model_call_id,
        "target_fingerprint": call.target.target_fingerprint,
        "reported_model_id": call.target.model_id,
        "outcome": "completed",
        "usage_status": "reported",
        "usage": usage,
        "estimated_input_tokens": (
            input_tokens if estimated_input_tokens is None else estimated_input_tokens
        ),
    }


async def run_agent_task(agent, user_input: str, **kwargs):
    """Invoke the production hard-cut API with an explicitly resolved run target."""

    kwargs.setdefault("run_model_target", agent.resolve_run_model_target())
    return await agent.run_task(user_input, **kwargs)


def stream_agent_task(agent, user_input: str, **kwargs):
    """Return the production stream with an explicitly resolved run target."""

    kwargs.setdefault("run_model_target", agent.resolve_run_model_target())
    return agent.stream_task(user_input, **kwargs)


@dataclass(frozen=True, slots=True)
class _ContractOnlyTransport:
    api: str
    binding_id: str = "test.contract_only"
    contract_version: str = "v1"

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        if False:
            yield  # pragma: no cover


def test_model_limits(
    *,
    total_context_tokens: int = 256_000,
    max_input_tokens: int = 256_000,
    max_output_tokens: int = 8_192,
    default_output_tokens: int = 8_000,
    input_safety_margin_tokens: int = 64_000,
) -> ModelContextLimits:
    return ModelContextLimits(
        total_context_tokens=total_context_tokens,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        default_output_tokens=default_output_tokens,
        input_safety_margin_tokens=input_safety_margin_tokens,
    )


def test_model_slot(
    model_id: str,
    *,
    limits: ModelContextLimits | None = None,
) -> ModelSlotConfig:
    return ModelSlotConfig(model_id=model_id, limits=limits or test_model_limits())


def test_llm_config(
    *,
    api_key: str,
    base_url: str,
    pro_model: str,
    flash_model: str,
    api: str = "openai_responses",
    provider: str = "custom",
    provider_profile: ProviderProfile | None = None,
    retry: LLMRetryConfig = LLMRetryConfig(),
    openai_sdk_max_retries: int | None = None,
    pro_limits: ModelContextLimits | None = None,
    flash_limits: ModelContextLimits | None = None,
) -> LLMConfig:
    """Build a production-shaped config while keeping terse test call sites."""

    return LLMConfig(
        api_key=api_key,
        base_url=base_url,
        pro=test_model_slot(pro_model, limits=pro_limits),
        flash=test_model_slot(flash_model, limits=flash_limits),
        api=api,
        provider=provider,
        provider_profile=provider_profile,
        retry=retry,
        openai_sdk_max_retries=openai_sdk_max_retries,
    )


def test_resolved_target_fact(
    *,
    model_id: str = "test-pro",
    role: ModelRole = ModelRole.PRO,
    limits: ModelContextLimits | None = None,
) -> ResolvedModelTargetFact:
    config = test_llm_config(
        api_key="test-key",
        base_url="https://example.test/v1",
        pro_model=model_id if role is ModelRole.PRO else "test-pro",
        flash_model=model_id if role is ModelRole.FLASH else "test-flash",
        api="mock",
        pro_limits=limits,
        flash_limits=limits,
    )
    registry = LLMTransportRegistry()
    registry.register(MockTransport(text="test"))
    return resolve_model_target(
        config=config,
        registry=registry,
        role=role,
        requested_options=None,
    ).fact


def test_resolved_call_fact(
    *,
    purpose: ModelCallPurpose = ModelCallPurpose.AGENT_MODEL_LOOP,
) -> ResolvedModelCallFact:
    config = test_llm_config(
        api_key="test-key",
        base_url="https://example.test/v1",
        pro_model="test-pro",
        flash_model="test-flash",
        api="mock",
    )
    registry = LLMTransportRegistry()
    registry.register(MockTransport(text="test"))
    role = (
        ModelRole.PRO
        if purpose is ModelCallPurpose.AGENT_MODEL_LOOP
        else ModelRole.FLASH
    )
    target = resolve_model_target(
        config=config,
        registry=registry,
        role=role,
        requested_options=None,
    )
    return resolve_model_call(target=target, purpose=purpose).fact


def test_resolved_call(
    *,
    purpose: ModelCallPurpose = ModelCallPurpose.AGENT_MODEL_LOOP,
    limits: ModelContextLimits | None = None,
    options: LLMOptions | None = None,
    provider_profile: ProviderProfile | None = None,
):
    """Return a runtime call for component tests that do not own an LLM runtime."""

    config = test_llm_config(
        api_key="test-key",
        base_url="https://example.test/v1",
        pro_model="test-pro",
        flash_model="test-flash",
        api="mock",
        provider_profile=provider_profile,
        pro_limits=limits,
        flash_limits=limits,
    )
    role = (
        ModelRole.PRO
        if purpose is ModelCallPurpose.AGENT_MODEL_LOOP
        else ModelRole.FLASH
    )
    return resolve_test_call(config, role=role, purpose=purpose, options=options)


def resolve_test_call(
    config: LLMConfig,
    *,
    role: ModelRole = ModelRole.PRO,
    options: LLMOptions | None = None,
    transport=None,
    purpose: ModelCallPurpose = ModelCallPurpose.AGENT_MODEL_LOOP,
):
    registry = LLMTransportRegistry()
    registry.register(transport or _ContractOnlyTransport(api=config.api))
    target = resolve_model_target(
        config=config,
        registry=registry,
        role=role,
        requested_options=options,
    )
    return resolve_model_call(target=target, purpose=purpose)


def bind_test_context(
    call,
    context: LLMContext,
    *,
    context_id: str | None = None,
    model_call_index: int | None = None,
) -> LLMContext:
    index = model_call_index
    if index is None and call.fact.context_mode == "compiled":
        index = context.model_call_index if context.model_call_index is not None else 1
    bound = replace(
        context,
        context_id=context_id or context.context_id or "context:test",
        resolved_model_call_id=call.fact.resolved_model_call_id,
        target_fingerprint=call.target.fact.target_fingerprint,
        model_call_index=index,
    )
    if (
        call.fact.context_mode == "compiled"
        and bound.compiler_estimated_input_tokens is None
    ):
        bound = replace(
            bound,
            compiler_estimated_input_tokens=(
                call.target.token_estimator.estimate_context(bound).total_input_tokens
            ),
        )
    return bound


def test_llm_context(**kwargs) -> LLMContext:
    """Build a structurally complete context before a test binds its real call."""

    kwargs.setdefault("context_id", "context:test-unbound")
    kwargs.setdefault("resolved_model_call_id", f"model_call:{'0' * 32}")
    kwargs.setdefault("target_fingerprint", f"sha256:{'0' * 64}")
    kwargs.setdefault("model_call_index", None)
    return LLMContext(**kwargs)


test_model_limits.__test__ = False
test_model_slot.__test__ = False
test_llm_config.__test__ = False
test_resolved_target_fact.__test__ = False
test_resolved_call_fact.__test__ = False
test_resolved_call.__test__ = False
resolve_test_call.__test__ = False
bind_test_context.__test__ = False
test_llm_context.__test__ = False
model_call_start_fields.__test__ = False
model_call_end_fields.__test__ = False
context_compiled_contract_fields.__test__ = False
compaction_completed_contract_fields.__test__ = False
compaction_started_contract_fields.__test__ = False
compaction_failed_contract_fields.__test__ = False
run_agent_task.__test__ = False
stream_agent_task.__test__ = False
