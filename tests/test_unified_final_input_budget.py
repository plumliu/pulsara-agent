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


@pytest.mark.parametrize(
    "api,project",
    [
        ("openai_chat_completions", chat_semantic_wire_group),
        ("openai_responses", responses_semantic_wire_group),
    ],
)
def test_memory_suffix_quote_prices_only_actual_wire_items(api, project):
    from pulsara_agent.conversation_kernel.context_sources import (
        build_memory_context_source,
    )
    from pulsara_agent.conversation_kernel.memory.dispatch import MemoryDispatchSupport
    from pulsara_agent.model_input.contracts import (
        ContextSourceKind,
        ContextSourceAbsenceKind,
    )
    from pulsara_agent.model_input.continuity import (
        ProcessLocalSourceHead,
        SourceObservationPresence,
        SourceObservationLifecycle,
        encode_runtime_observation,
    )

    port = _port(api=api)
    request, _ = _prepared_execution(port)
    prepared = request.prepared_call
    support = object.__new__(MemoryDispatchSupport)
    prior = ProcessLocalSourceHead(
        ContextSourceKind.MEMORY_RECALL,
        SourceObservationPresence.VALUE,
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        'turn:klmmazz67vfgsfsvg6ceqgdsfu',
        1,
    )
    try:
        for kind in (
            ContextSourceKind.MEMORY_RECALL,
            ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
        ):
            invalidations = []
            for absence, presence, lifecycle in (
                (
                    ContextSourceAbsenceKind.EXPLICIT_EMPTY,
                    SourceObservationPresence.CLEARED,
                    SourceObservationLifecycle.CLEARED,
                ),
                (
                    ContextSourceAbsenceKind.UNAVAILABLE,
                    SourceObservationPresence.UNAVAILABLE,
                    SourceObservationLifecycle.UNAVAILABLE,
                ),
            ):
                desired = build_memory_context_source(
                    kind=kind, texts=None, absence_kind=absence
                )
                message = encode_runtime_observation(
                    source_kind=kind,
                    trust_class=desired.trust_class,
                    lifecycle=lifecycle,
                    presence=presence,
                    contract_version=desired.source_contract_version,
                    body="",
                )
                invalidations.append(message)
                wire = tuple(project(message))
                expected = prepared.compile_binding.estimator.estimate_ordered_wire_json_components(
                    ordered_input_items=wire,
                    ordered_input_sources=(message,) * len(wire),
                )
                assert (
                    quote_provider_suffix_input_tokens(
                        call=prepared.call, messages=(message,)
                    )
                    == expected.total_input_tokens
                )
            full = build_memory_context_source(
                kind=kind,
                texts=("full memory", "compact", "reference")
                if kind is ContextSourceKind.MEMORY_RECALL
                else ("full memory",),
            )
            reservation = support._memory_invalidation_reservation(
                source_kind=kind,
                prior=replace(prior, source_kind=kind),
                desired=full,
                prepared_call=prepared,
            )
            assert reservation.invalidation_input_token_ceiling == max(
                quote_provider_suffix_input_tokens(call=prepared.call, messages=(m,))
                for m in invalidations
            )
            assert not hasattr(reservation, "full_input_token_cost")
    finally:
        request.surface_borrow.close()


def test_canonical_identity_goldens_survive_removal_of_semantic_token_fields():
    from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
    from pulsara_agent.model_input.contracts import (
        ContextSourceKind,
        compiled_message_placements_fingerprint,
    )
    from pulsara_agent.model_input.continuity import (
        ProcessLocalSourceHead,
        SourceObservationPresence,
    )
    from pulsara_agent.conversation_kernel.context_sources import (
        build_memory_context_source,
    )
    from pulsara_agent.conversation_kernel.memory.dispatch import MemoryDispatchSupport
    from pulsara_agent.conversation_kernel.subagents.contracts import (
        build_parent_context_call_subject,
        parent_context_call_subject_identity_digest,
    )
    from tests.support.unified_budget_identity_cases import identity_request

    request = identity_request()
    compiled = StructuredModelInputCompiler().compile(request)
    # Unified identity fixture: retained complete semantic proofs remain deterministic.
    assert (
        compiled.compiled_semantic_fingerprint
        == "sha256:8da1339f6700d4547a05633bd0dd6c9e7b0709b6e03f0f8167baee8d12a84aa3"
    )
    port = _port()
    execution, _ = _prepared_execution(port)
    prepared = SimpleNamespace(
        compile_binding=request.compile_binding, call=execution.prepared_call.call
    )
    support = object.__new__(MemoryDispatchSupport)
    prior = ProcessLocalSourceHead(
        ContextSourceKind.MEMORY_RECALL,
        SourceObservationPresence.VALUE,
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        'turn:klmmazz67vfgsfsvg6ceqgdsfu',
        1,
    )
    try:
        reservation = support._memory_invalidation_reservation(
            source_kind=prior.source_kind,
            prior=prior,
            desired=build_memory_context_source(
                kind=prior.source_kind, texts=("记忆正文", "简版", "ref")
            ),
            prepared_call=prepared,
        )
        assert reservation.prior_semantic_fingerprint == prior.semantic_fingerprint
        assert reservation.desired_presence is SourceObservationPresence.VALUE
        assert reservation.invalidation_input_token_ceiling > 0
        assert reservation.invalidation_epoch_bytes_ceiling > 0
        subject = build_parent_context_call_subject(
            session_id='session:336wrfwkamrr3up6kjtsgvgcte',
            caller_turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu',
            provider_input_cut_fingerprint="sha256:" + "3" * 64,
            continuity_epoch_nonce='epoch:77qvnnqag4wpxnkz63b2qc5s7u',
            continuity_epoch_revision=1,
            compiled_semantic_input_fingerprint=compiled.compiled_semantic_fingerprint,
            compiled_message_placements_fingerprint=compiled_message_placements_fingerprint(
                compiled.message_placements
            ),
            ordered_eligible_units=(),
        )
        assert (
            parent_context_call_subject_identity_digest(subject)
            == "sha256:b433f66c6a5c8fe18aba5a0c69e4d8e2a89676bbac0f6ebcbfae8c1b18cb6b5a"
        )
    finally:
        execution.surface_borrow.close()


def test_cold_and_installed_append_identity_goldens():
    from tests.support.unified_budget_identity_cases import append_identity_digests

    assert append_identity_digests() == (
        "sha256:29698143b9310a2366c0e5d183bff1e15dcf3a80d21d6cefc111c0c49faa3844",
        "sha256:e46cf6d997f7aa3a3a049b7455561d8c29a943ffdafa0a020040b7af6333b1c7",
    )


@pytest.mark.parametrize(
    "api,raw,wire_bytes",
    [
        ("openai_chat_completions", 244, 916),
        ("openai_responses", 237, 887),
    ],
)
def test_final_wire_quote_uses_short_refs_and_preserves_estimator_identity(api, raw, wire_bytes):
    request, _ = _prepared_execution(_port(api=api))
    try:
        quote = request.wire_input_plan.quote
        assert quote.raw_final_wire_estimated_input_tokens == raw
        assert quote.final_wire_utf8_bytes == wire_bytes
        assert (
            quote.estimator_fingerprint
            == "sha256:b6e8c5d3896481f7e9feee52e953982a48363218776283e31122a698d2137606"
        )
    finally:
        request.surface_borrow.close()


@pytest.mark.parametrize('api', ['openai_chat_completions', 'openai_responses'])
@pytest.mark.parametrize('anchored', [False, True])
@pytest.mark.parametrize('kind_name', ['MEMORY_RECALL', 'MEMORY_RESPONSE_PREFERENCE_HEAD'])
@pytest.mark.parametrize('changed', [False, True])
def test_replaced_memory_is_paid_once_at_exact_wire_boundary(api, anchored, kind_name, changed):
    from time import monotonic
    from pulsara_agent.conversation_kernel.context_sources import build_memory_context_source, replace_memory_context_sources
    from pulsara_agent.conversation_kernel.memory.dispatch import MemoryDispatchSupport
    from pulsara_agent.conversation_kernel.provider_dispatch import _remaining_invalidation_input_tokens
    from pulsara_agent.llm.request import ProviderInputUsageAnchor
    from pulsara_agent.model_input.contracts import ContextSourceKind, ContextSourceAbsenceKind
    from tests.test_catalog_final_wire_resources import _prepare_case

    kind = ContextSourceKind[kind_name]
    count = 3 if kind is ContextSourceKind.MEMORY_RECALL else 1
    old = build_memory_context_source(kind=kind, texts=('old memory',) * count)
    updated = build_memory_context_source(kind=kind, texts=('new desired memory',) * count) if changed else old

    def prepare(budget):
        coordinator, candidate, view, _ = _prepare_case(api, updated, initial_catalog=old, budget=budget)
        support = MemoryDispatchSupport(compiler=coordinator._compiler, io_owner=coordinator._io,
            memory_projection=None, input_reader=coordinator._input_reader, deadline_factory=None)
        coordinator._memory_support = support
        # Recall planning precedes retrieval, so its desired fact is a placeholder.
        desired = build_memory_context_source(kind=kind, texts=('',) * count) if count == 3 else updated
        reservation = support._memory_invalidation_reservation(source_kind=kind,
            prior=next(head for head in view.source_heads if head.source_kind is kind),
            desired=desired, prepared_call=candidate.prepared_call)
        return coordinator, candidate, view, reservation

    coordinator, candidate, view, reservation = prepare(100_000)
    measurement = asyncio.run(coordinator._freeze_candidate_wire_measurement(candidate, deadline=monotonic()+30))
    raw = measurement.quote.raw_final_wire_estimated_input_tokens
    measurement.discard_materialization_to_quote()
    if anchored:
        plan, call = view.wire_input_plan, candidate.call
        delta = raw - plan.quote.raw_final_wire_estimated_input_tokens
        anchor = ProviderInputUsageAnchor(call.resolved_model_call_id, view.epoch_nonce, view.epoch_revision,
            call.binding.connection_id.value, call.target.fact, plan.route_wire_profile_fingerprint,
            'alias', 100_000-delta, plan.quote.raw_final_wire_estimated_input_tokens, plan.materialization)
        coordinator._continuity.observe_usage_anchor(view.scope, epoch_nonce=view.epoch_nonce,
            epoch_revision=view.epoch_revision, reported_model_id='alias', anchor=anchor)
    else:
        coordinator, candidate, view, reservation = prepare(raw)
    # If no actual value was resolved and the compiler leaves the prior head
    # installed, the invalidation obligation must still reserve its wire suffix.
    unresolved = replace_memory_context_sources(candidate.sources, (
        build_memory_context_source(kind=kind, texts=None,
            absence_kind=ContextSourceAbsenceKind.NOT_APPLICABLE),))
    unresolved_request = replace(candidate.compile_request, sources=unresolved)
    unresolved_append = coordinator._compiler.compile_installed_append(
        unresolved_request, planning=candidate.planning)
    unresolved_candidate = replace(candidate, sources=unresolved,
        compile_request=unresolved_request, append_result=unresolved_append,
        semantic_input=unresolved_append.compiled_input)
    assert _remaining_invalidation_input_tokens(unresolved_candidate, (reservation,)) == reservation.invalidation_input_token_ceiling
    assert reservation.invalidation_input_token_ceiling > 0
    assert _remaining_invalidation_input_tokens(candidate, (reservation,)) == 0
    decision = asyncio.run(coordinator.measure_prepared_wire_candidate(candidate,
        deadline=monotonic()+30, invalidation_reservations=(reservation,)))
    assert decision.wire_input_plan is not None
    assert decision.quote.budget_input_tokens == decision.quote.effective_input_budget_tokens
    assert decision.candidate.sources == candidate.sources
    assert decision.candidate.semantic_input.messages == candidate.semantic_input.messages
    assert any(('new desired memory' if changed else 'old memory') in str(message.content) for message in decision.candidate.semantic_input.messages)
    assert decision.candidate.semantic_input.messages[:len(view.messages)] == view.messages


@pytest.mark.parametrize('api', ['openai_chat_completions', 'openai_responses'])
@pytest.mark.parametrize('kind_name', ['MEMORY_RECALL', 'MEMORY_RESPONSE_PREFERENCE_HEAD'])
def test_cold_memory_planning_does_not_inherit_old_reservation(api, kind_name):
    from pulsara_agent.conversation_kernel.context_sources import build_memory_context_source
    from pulsara_agent.conversation_kernel.memory.dispatch import MemoryDispatchSupport
    from pulsara_agent.model_input.contracts import ContextSourceKind
    from tests.test_catalog_final_wire_resources import _prepare_case

    kind = ContextSourceKind[kind_name]
    count = 3 if kind is ContextSourceKind.MEMORY_RECALL else 1
    old = build_memory_context_source(kind=kind, texts=('old',) * count)
    desired = build_memory_context_source(kind=kind, texts=('replacement',) * count)
    coordinator, candidate, view, _ = _prepare_case(api, desired, initial_catalog=old)
    support = MemoryDispatchSupport(compiler=coordinator._compiler, io_owner=coordinator._io,
        memory_projection=None, input_reader=coordinator._input_reader, deadline_factory=None)
    preference = desired if count == 1 else build_memory_context_source(
        kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD, texts=('preference',))
    recall = desired if count == 3 else build_memory_context_source(
        kind=ContextSourceKind.MEMORY_RECALL, texts=('recall',) * 3)
    assert any(head.source_kind is kind for head in view.source_heads)
    kwargs = dict(planning=candidate.planning, prepared_preference=preference,
        recall_desired=recall, prepared_call=candidate.prepared_call)
    assert any(item is not None for item in support.planning_reservations(**kwargs, new_epoch=False))
    assert support.planning_reservations(**kwargs, new_epoch=True) == (None, None)
    assert support.planning_preference_refresh_reservation(planning=candidate.planning,
        desired=preference, prepared_call=candidate.prepared_call, new_epoch=True) is None


@pytest.mark.parametrize('api', ['openai_chat_completions', 'openai_responses'])
def test_wire_overflow_does_not_attribute_small_required_items_to_failure(api):
    from tests.test_catalog_final_wire_resources import _prepare_case, _measure, _assert_typed_budget_rejection
    from tests.test_round3_structured_model_input_compiler import _candidate
    from pulsara_agent.model_input.contracts import ContextSourceKind
    # Large fixed SYSTEM dominates the input; a valid image and FULL_REQUIRED
    # result coexist. Their presence cannot identify the cause of aggregate overflow.
    coordinator, candidate, _, _ = _prepare_case(api,
        _candidate(ContextSourceKind.BASE_SYSTEM, ('fixed SYSTEM ' * 15_000,)),
        mixed=True, cold=True, budget=20_000)
    decision = _measure(coordinator, candidate)
    assert decision.wire_input_plan is None
    _assert_typed_budget_rejection(coordinator, decision)
