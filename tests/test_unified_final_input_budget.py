"""Final-input budgeting uses adapter projections at every provider-open seam."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.direct_model import (
    quote_provider_suffix_input_tokens,
)
from pulsara_agent.llm.adapters.openai.chat_completions import (
    OpenAIChatCompletionsTransport,
    chat_semantic_wire_group,
)
from pulsara_agent.llm.adapters.openai.responses import (
    OpenAIResponsesTransport,
    responses_semantic_wire_group,
)
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.errors import (
    ModelInputBudgetUnavailable,
    ModelContextIdentityMismatch,
)
from pulsara_agent.llm.input import LLMMessage, ToolSpec
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.primitives.context import thaw_json
from tests.test_stage2_direct_model import _port, _prepared_execution
from tests.test_provider_usage_anchored_budget import _anchor


class _Endpoint:
    def __init__(self):
        self.payloads = []

    async def create(self, **kwargs):
        self.payloads.append(kwargs)

        async def stream():
            if False:
                yield None

        return stream()


@pytest.mark.parametrize(
    "api,transport_type,key",
    [
        (
            "openai_chat_completions",
            OpenAIChatCompletionsTransport,
            "max_completion_tokens",
        ),
        ("openai_responses", OpenAIResponsesTransport, "max_output_tokens"),
    ],
)
@pytest.mark.parametrize(
    "plan_mode", ["no_plan", "raw_plan", "low_anchor", "high_anchor"]
)
def test_sdk_open_uses_wire_budget_and_resolves_output(
    api, transport_type, key, plan_mode
):
    from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
    from tests.support.model_config import test_model_runtime, test_model_limits

    port = DirectKernelModelPort(
        model_runtime=test_model_runtime(
            wire_api=api,
            model_id="test-pro",
            api_key="sk-fixture-secret",
            base_url="https://example.invalid/v1",
            limits=test_model_limits(
                total_context_tokens=256000,
                max_input_tokens=250000,
                max_output_tokens=32000,
                default_output_tokens=32000,
                input_safety_margin_tokens=1000,
            ),
        )
    )
    request, _ = _prepared_execution(port, maximum_input_tokens=249000)
    call = request.prepared_call.call
    endpoint = _Endpoint()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=endpoint), responses=endpoint
    )
    transport = transport_type(
        settings=port._model_runtime.settings,
        timeout_policy=OpenAITransportTimeoutPolicy(1, 1, 1, 1, None),
        _client=client,
    )
    plan = request.wire_input_plan
    if plan_mode.endswith("anchor"):
        reported = (
            1
            if plan_mode == "low_anchor"
            else call.target.context_budget.input_budget_tokens - 1
        )
        plan = port.freeze_wire_measurement(
            call=call,
            compile_binding=request.prepared_call.compile_binding,
            native_projection_set=request.prepared_call.native_projection_set,
            semantic_input=request.compiled_input,
            replay_hydration=None,
            usage_anchor=_anchor(request, reported),
        ).prepare_executable_plan()
    context = LLMContext(
        context_id=request.compiled_input.context_id,
        resolved_model_call_id=call.resolved_model_call_id,
        model_call_index=1,
        system_prompt=request.compiled_input.system_prompt,
        messages=request.compiled_input.messages,
        tools=tuple(
            ToolSpec(t.name, t.description, thaw_json(t.parameters))
            for t in request.compiled_input.tools
        ),
        provider_wire_input_plan=None if plan_mode == "no_plan" else plan,
    )

    async def run(value):
        return [item async for item in transport.stream(call=call, context=value)]

    try:
        asyncio.run(run(context))
        assert len(endpoint.payloads) == 1
        budget = plan.quote.budget_input_tokens
        assert endpoint.payloads[0][key] == min(
            call.target.context_budget.effective_output_tokens,
            call.target.limits.max_output_tokens,
            call.target.limits.total_context_tokens
            - budget
            - call.target.context_budget.safety_margin_tokens,
        )
        endpoint.payloads.clear()
        huge = replace(
            context,
            provider_wire_input_plan=None,
            messages=(
                LLMMessage.user(
                    '"\\' * (call.target.context_budget.input_budget_tokens + 1)
                ),
            ),
        )
        with pytest.raises(ModelInputBudgetUnavailable):
            asyncio.run(run(huge))
        assert endpoint.payloads == []
        with pytest.raises(ModelContextIdentityMismatch):
            asyncio.run(
                run(
                    replace(
                        context,
                        provider_wire_input_plan=None,
                        resolved_model_call_id='call:ebxtqqdu4uvdqy2yfhvm543c7e',
                    )
                )
            )
        assert endpoint.payloads == []
    finally:
        request.surface_borrow.close()














@pytest.mark.parametrize('api', ['openai_chat_completions', 'openai_responses'])
def test_wire_overflow_does_not_attribute_small_required_items_to_failure(api):
    from tests.test_catalog_final_wire_resources import _prepare_case, _measure, _assert_typed_budget_rejection
    from tests.support.model_input import _candidate
    from pulsara_agent.model_input.contracts import ContextSourceKind
    # Large fixed SYSTEM dominates the input; a valid image and complete native
    # result coexist. Their presence cannot identify the cause of aggregate overflow.
    coordinator, candidate, _, _ = _prepare_case(api,
        _candidate(ContextSourceKind.BASE_SYSTEM, 'fixed SYSTEM ' * 15_000),
        mixed=True, cold=True, budget=20_000)
    decision = _measure(coordinator, candidate)
    assert decision.wire_input_plan is None
    _assert_typed_budget_rejection(coordinator, decision)


@pytest.mark.parametrize("api,project", [
    ("openai_chat_completions", chat_semantic_wire_group),
    ("openai_responses", responses_semantic_wire_group),
])
def test_memory_suffix_quote_prices_only_actual_wire_items(api, project):
    from pulsara_agent.model_input.contracts import ContextSourceKind
    from pulsara_agent.model_input.continuity import RuntimeObservationItem, encode_runtime_observation
    port = _port(api=api)
    request, _ = _prepared_execution(port)
    try:
        for body in (
            (RuntimeObservationItem("当前没有匹配的记忆。"),),
            (RuntimeObservationItem("本次无法读取记忆，请依据当前输入继续。"),),
            (RuntimeObservationItem("完整 response preferences"), RuntimeObservationItem("完整 recall")),
        ):
            message = encode_runtime_observation(source_kind=ContextSourceKind.MEMORY_RECALL, body=body)
            wire = tuple(project(message))
            expected = request.prepared_call.compile_binding.estimator.estimate_ordered_wire_json_components(
                ordered_input_items=wire, ordered_input_sources=(message,) * len(wire))
            assert quote_provider_suffix_input_tokens(call=request.prepared_call.call, messages=(message,)) == expected.total_input_tokens
    finally:
        request.surface_borrow.close()


@pytest.mark.parametrize('api', ['openai_chat_completions', 'openai_responses'])
@pytest.mark.parametrize('anchored', [False, True])
@pytest.mark.parametrize('changed', [False, True])
def test_memory_net_delta_is_paid_once_at_exact_wire_boundary(api, anchored, changed):
    from time import monotonic
    from pulsara_agent.conversation_kernel.context_sources import observation_source
    from pulsara_agent.model_input.continuity import RuntimeObservationItem
    from pulsara_agent.model_input.contracts import ContextSourceKind
    from pulsara_agent.llm.request import ProviderInputUsageAnchor
    from tests.test_catalog_final_wire_resources import _prepare_case, _measure
    kind = ContextSourceKind.MEMORY_RECALL
    old = observation_source(kind, (RuntimeObservationItem('old memory'),), 60)
    updated = observation_source(kind, (RuntimeObservationItem('new desired memory'),), 60) if changed else old

    def prepare(budget):
        return _prepare_case(api, updated, initial_catalog=old, budget=budget)[:3]

    coordinator, candidate, view = prepare(100_000)
    measured = asyncio.run(coordinator._freeze_candidate_wire_measurement(candidate, deadline=monotonic()+30))
    raw = measured.quote.raw_final_wire_estimated_input_tokens
    measured.discard_materialization_to_quote()
    if anchored:
        plan, call = view.wire_input_plan, candidate.call
        delta = raw-plan.quote.raw_final_wire_estimated_input_tokens
        anchor = ProviderInputUsageAnchor(call.resolved_model_call_id, view.epoch_nonce, view.epoch_revision,
            call.binding.connection_id.value, call.target.fact, plan.route_wire_profile_fingerprint,
            'alias', 100_000-delta, plan.quote.raw_final_wire_estimated_input_tokens, plan.materialization)
        coordinator._continuity.observe_usage_anchor(view.scope, epoch_nonce=view.epoch_nonce,
            epoch_revision=view.epoch_revision, reported_model_id='alias', anchor=anchor)
    else:
        coordinator, candidate, view = prepare(raw)
    decision = _measure(coordinator, candidate)
    assert decision.wire_input_plan is not None
    assert decision.quote.budget_input_tokens == decision.quote.effective_input_budget_tokens
    assert decision.candidate is candidate
    assert decision.candidate.semantic_input.messages[:len(view.messages)] == view.messages
    suffix = decision.candidate.semantic_input.messages[len(view.messages):]
    assert len(suffix) == int(changed)
    if changed:
        assert 'new desired memory' in suffix[0].content[0].text
    if not anchored and not changed:
        # The already installed prefix is the entire input. Verify the exact
        # physical wire boundary without trying to install that same prefix
        # into an artificially smaller earlier call budget.
        with pytest.raises(ValueError, match="provider wire plan exceeds the input budget"):
            replace(decision.wire_input_plan, quote=replace(decision.quote,
                effective_input_budget_tokens=decision.quote.effective_input_budget_tokens-1))
        return
    if anchored:
        coordinator._continuity.observe_usage_anchor(view.scope, epoch_nonce=view.epoch_nonce,
            epoch_revision=view.epoch_revision, reported_model_id='alias',
            anchor=replace(anchor, reported_input_tokens=anchor.reported_input_tokens+1))
    else:
        coordinator, candidate, _ = prepare(raw-1)
    rejected = _measure(coordinator, candidate)
    assert rejected.wire_input_plan is None
    assert rejected.quote.budget_input_tokens == rejected.quote.effective_input_budget_tokens+1
    assert rejected.candidate.sources == candidate.sources
