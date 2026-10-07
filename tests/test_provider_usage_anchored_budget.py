"""Provider usage preserves partial observations and calibrates actual wire inputs."""
from dataclasses import replace

import pytest

from pulsara_agent.llm.adapters.openai.events import (
    merge_response_usage_reports,
    transport_usage_report_from_mapping,
)
from pulsara_agent.llm.input import LLMMessage, LLMTextPart, MessageRole
from pulsara_agent.llm.request import ProviderInputUsageAnchor, MAXIMUM_PROVIDER_WIRE_INPUT_BYTES
from pulsara_agent.primitives.context import freeze_json, thaw_json
from pulsara_agent.conversation_kernel.direct_model import quote_provider_followup_wire_resources
from tests.test_stage2_direct_model import _port, _prepared_execution, _continuity_candidate


@pytest.mark.parametrize('value', [True, False, 1.0, '1', -1, 2**63, [], {}])
@pytest.mark.parametrize('wire,input_key,output_key', [
    ('openai_chat_completions', 'prompt_tokens', 'completion_tokens'),
    ('openai_responses', 'input_tokens', 'output_tokens'),
])
def test_bad_input_preserves_valid_output(value, wire, input_key, output_key):
    report = transport_usage_report_from_mapping({input_key: value, output_key: 7}, wire_api=wire)
    assert report.usage_status == 'partial'
    assert report.usage.input_tokens is None
    assert report.usage.output_tokens == 7
    assert report.usage.computed_total_tokens is None
    assert report.provider_diagnostics


@pytest.mark.parametrize('raw,status', [(None, 'missing'), ({}, 'missing'), ([], 'invalid'), ({'input_tokens': 'bad'}, 'invalid')])
def test_missing_invalid_are_distinct(raw, status):
    assert transport_usage_report_from_mapping(raw).usage_status == status


def test_total_and_breakdowns_are_independent_observations():
    report = transport_usage_report_from_mapping({
        'input_tokens': 3, 'output_tokens': 5, 'total_tokens': 999,
        'input_tokens_details': {'cached_tokens': 4},
        'output_tokens_details': {'reasoning_tokens': 6},
    })
    assert report.usage_status == 'reported'
    assert report.usage.reported_total_tokens == 999
    assert report.usage.computed_total_tokens == 8
    assert report.usage.cached_input_tokens is None
    assert report.usage.reasoning_output_tokens is None
    assert len(report.provider_diagnostics) == 3
    partial = transport_usage_report_from_mapping({'input_tokens_details': {'cached_tokens': 3}})
    assert partial.usage_status == 'partial'
    assert partial.usage.input_tokens is None
    assert partial.usage.cached_input_tokens == 3
    zero = transport_usage_report_from_mapping({'input_tokens': 0, 'output_tokens': 0})
    assert zero.usage_status == 'reported'
    assert zero.usage.computed_total_tokens == 0


def test_alias_priority_and_cumulative_carriers_do_not_add():
    first = transport_usage_report_from_mapping({'prompt_tokens': 9, 'input_tokens': 99}, wire_api='openai_chat_completions')
    assert first.usage.input_tokens == 9
    second = transport_usage_report_from_mapping({'completion_tokens': 3}, wire_api='openai_chat_completions')
    merged = merge_response_usage_reports(first, second)
    assert merged.usage_status == 'reported'
    assert merged.usage.computed_total_tokens == 12
    assert merge_response_usage_reports(merged, second).usage == merged.usage
    assert merge_response_usage_reports(merged, transport_usage_report_from_mapping({})).usage == merged.usage
    assert merge_response_usage_reports(merged, transport_usage_report_from_mapping([])).usage == merged.usage


def _anchor(request, reported=5, revision=1, nonce='epoch:test'):
    call = request.prepared_call.call
    plan = request.wire_input_plan
    return ProviderInputUsageAnchor(
        model_call_id=call.resolved_model_call_id, epoch_nonce=nonce,
        epoch_revision=revision, connection_id=call.binding.connection_id.value,
        target=call.target.fact, route_wire_profile_fingerprint=plan.route_wire_profile_fingerprint,
        reported_model_id='allowed-alias', reported_input_tokens=reported,
        raw_input_tokens=plan.quote.raw_final_wire_estimated_input_tokens,
        materialization=plan.materialization,
    )


@pytest.mark.parametrize('api', ['openai_chat_completions', 'openai_responses'])
@pytest.mark.parametrize('reported', [5, 20000])
def test_measurement_uses_reported_input_as_unique_budget(api, reported):
    port = _port(api=api)
    request, _ = _prepared_execution(port)
    try:
        prepared = request.prepared_call
        measurement = port.freeze_wire_measurement(
            call=prepared.call, compile_binding=prepared.compile_binding,
            native_projection_set=prepared.native_projection_set,
            semantic_input=request.compiled_input, replay_hydration=None,
            usage_anchor=_anchor(request, reported),
        )
        quote = measurement.quote
        assert quote.raw_final_wire_estimated_input_tokens == request.wire_input_plan.quote.raw_final_wire_estimated_input_tokens
        assert quote.budget_input_tokens == reported
        assert quote.budget_source == 'reported_input_anchor'
        assert quote.estimated_suffix_tokens == 0
        if reported < quote.effective_input_budget_tokens:
            plan = measurement.prepare_executable_plan()
            assert plan.quote.budget_input_tokens == reported
            with pytest.raises(ValueError, match='hard byte'):
                replace(plan, quote=replace(quote, final_wire_utf8_bytes=MAXIMUM_PROVIDER_WIRE_INPUT_BYTES + 1))
        else:
            assert reported > quote.effective_input_budget_tokens
            with pytest.raises(ValueError, match='input budget'):
                measurement.prepare_executable_plan()
    finally:
        request.surface_borrow.close()


@pytest.mark.parametrize('api', ['openai_chat_completions', 'openai_responses'])
def test_three_calls_with_middle_missing_keep_entire_assistant_suffix(api):
    port = _port(api=api)
    request, _ = _prepared_execution(port)
    try:
        anchor = _anchor(request)
        first_reply = LLMMessage(role=MessageRole.ASSISTANT, content=(LLMTextPart('first reply ' * 10),))
        middle_reply = LLMMessage(role=MessageRole.ASSISTANT, content=(LLMTextPart('middle reply ' * 12),))
        user = LLMMessage(role=MessageRole.USER, content=(LLMTextPart('next question'),))
        result = quote_provider_followup_wire_resources(
            request=request, actual_assistant_message=first_reply, provider_replay=None,
            bounded_suffix_messages=(user, middle_reply, user), usage_anchor=anchor,
        )
        assert result.budget_input_tokens == anchor.reported_input_tokens + result.raw_final_wire_estimated_input_tokens - anchor.raw_input_tokens
        shorter = quote_provider_followup_wire_resources(
            request=request, actual_assistant_message=first_reply, provider_replay=None,
            bounded_suffix_messages=(user,), usage_anchor=anchor,
        )
        assert result.budget_input_tokens > shorter.budget_input_tokens > anchor.reported_input_tokens
        assert request.wire_input_plan.quote.budget_source == 'heuristic'  # frozen old quote
    finally:
        request.surface_borrow.close()


def test_anchor_rejects_context_projection_and_prefix_drift():
    port = _port()
    request, _ = _prepared_execution(port)
    try:
        anchor = _anchor(request)
        kwargs = dict(connection_id=anchor.connection_id, target=anchor.target,
                      route_wire_profile_fingerprint=anchor.route_wire_profile_fingerprint,
                      raw_input_tokens=anchor.raw_input_tokens)
        projection = thaw_json(anchor.materialization.context_bearing_projection)
        projection['tool_choice'] = 'none'
        assert anchor.budget_for(materialization=replace(anchor.materialization, context_bearing_projection=freeze_json(projection)), **kwargs) is None
        assert anchor.budget_for(materialization=replace(anchor.materialization, ordered_input_items=()), **kwargs) is None
        with pytest.raises(ValueError, match='negative suffix'):
            anchor.budget_for(materialization=anchor.materialization, **{**kwargs, 'raw_input_tokens': anchor.raw_input_tokens - 1})
    finally:
        request.surface_borrow.close()


def test_owner_keeps_newest_anchor_and_rejects_other_epoch():
    port = _port()
    request, _ = _prepared_execution(port)
    try:
        owner, candidate = _continuity_candidate(request)
        execution = port.preflight_execution(request, append_candidate=candidate, install_authority=owner.install_authority)
        permit = owner.install(candidate=candidate, execution=execution)
        anchor = _anchor(request, revision=permit.epoch_revision, nonce=permit.epoch_nonce)
        owner.observe_usage_anchor(permit.scope, epoch_nonce=permit.epoch_nonce, epoch_revision=permit.epoch_revision, reported_model_id=anchor.reported_model_id, anchor=anchor)
        assert owner.current_usage_anchor(permit.scope) is anchor
        owner.observe_usage_anchor(permit.scope, epoch_nonce='expired', epoch_revision=permit.epoch_revision + 1, reported_model_id='other', anchor=replace(anchor, reported_input_tokens=999))
        assert owner.current_usage_anchor(permit.scope) is anchor
        owner.observe_usage_anchor(permit.scope, epoch_nonce=permit.epoch_nonce, epoch_revision=permit.epoch_revision, reported_model_id=None, anchor=None)
        assert owner.current_usage_anchor(permit.scope) is anchor
        owner.observe_usage_anchor(permit.scope, epoch_nonce=permit.epoch_nonce, epoch_revision=permit.epoch_revision, reported_model_id='new-alias', anchor=None)
        assert owner.current_usage_anchor(permit.scope) is None
    finally:
        request.surface_borrow.close()


def test_chat_breakdown_is_checked_after_last_parent_correction():
    from pulsara_agent.llm.adapters.openai.chat_completions import ChatCompletionAccumulator
    from pulsara_agent.llm.adapters.openai.events import ProviderLiveItemBuilder
    port = _port()
    request, _ = _prepared_execution(port)
    try:
        accumulator = ChatCompletionAccumulator(builder=ProviderLiveItemBuilder(), route_wire_profile=request.prepared_call.call.target.model_profile.route_wire_profile)
        accumulator.apply({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                           'usage': {'prompt_tokens': 5, 'prompt_tokens_details': {'cached_tokens': 7}}})
        accumulator.apply({'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}})
        assert accumulator.usage_report.usage.cached_input_tokens == 7
        assert accumulator.usage_report.usage.input_tokens == 10
        assert accumulator.usage_report.usage.computed_total_tokens == 12
        accumulator.apply({'choices': [], 'usage': {'prompt_tokens_details': {'cached_tokens': 11}}})
        assert accumulator.usage_report.usage.cached_input_tokens is None
    finally:
        request.surface_borrow.close()


@pytest.mark.parametrize('finish,error_type', [('stop', 'cancelled'), ('unknown_finish_reason', 'incomplete'), ('protocol_error', 'protocol')])
def test_cancellation_during_observation_preserves_original_execution_outcome(finish, error_type):
    import asyncio
    from pulsara_agent.conversation_kernel.direct_model import ProviderModelExecutionFailed, ProviderModelOutputIncomplete
    observed = []
    execution = None

    async def observer(request, report, permit, terminal_kind, anchor_allowed):
        assert execution.state == 'PHYSICALLY_CLOSED'
        observed.append((terminal_kind, anchor_allowed))
        raise asyncio.CancelledError()

    port = _port(usage_observer=observer)
    adapter = port._model_runtime.transport_registry(port._transport_timeout).get('openai_chat_completions')._adapter
    adapter._mock_chunks = [{'choices': [{'index': 1 if error_type == 'protocol' else 0, 'delta': {}, 'finish_reason': 'stop' if error_type == 'protocol' else finish}]}]
    request, _ = _prepared_execution(port)
    try:
        owner, candidate = _continuity_candidate(request)
        execution = port.preflight_execution(request, append_candidate=candidate, install_authority=owner.install_authority)
        permit = owner.install(candidate=candidate, execution=execution)

        async def collect():
            return [item async for item in execution.open_once(permit)]

        expected = {'cancelled': asyncio.CancelledError, 'protocol': ProviderModelExecutionFailed, 'incomplete': ProviderModelOutputIncomplete}[error_type]
        with pytest.raises(expected):
            asyncio.run(collect())
        assert len(observed) == 1
        assert observed[0][1] is (error_type != 'protocol')
    finally:
        request.surface_borrow.close()


@pytest.mark.parametrize('api', ['openai_chat_completions', 'openai_responses'])
def test_anchored_dispatch_keeps_full_sources_when_raw_quote_overestimates(api):
    from tests.test_catalog_final_wire_resources import _prepare_case, _skill_source, _measure, _mode
    from pulsara_agent.model_input.contracts import ContextRenderMode, ContextSourceKind
    # V3 folds ASCII at four bytes/token. Keep the raw quote above the
    # budget while the same measured prefix plus suffix remains admissible.
    coordinator, candidate, view, _ = _prepare_case(api, _skill_source(), budget=150_000, user_text='u'*400_000)
    plan = view.wire_input_plan
    call = candidate.call
    anchor = ProviderInputUsageAnchor(call.resolved_model_call_id, view.epoch_nonce, view.epoch_revision,
        call.binding.connection_id.value, call.target.fact, plan.route_wire_profile_fingerprint,
        'accepted-alias', 5, plan.quote.raw_final_wire_estimated_input_tokens, plan.materialization)
    coordinator._continuity.observe_usage_anchor(view.scope, epoch_nonce=view.epoch_nonce,
        epoch_revision=view.epoch_revision, reported_model_id=anchor.reported_model_id, anchor=anchor)
    decision = _measure(coordinator, candidate)
    assert decision.wire_input_plan is not None
    assert decision.quote.raw_final_wire_estimated_input_tokens > decision.quote.effective_input_budget_tokens
    assert decision.quote.budget_input_tokens < decision.quote.effective_input_budget_tokens
    assert _mode(decision.candidate, ContextSourceKind.SKILL_CATALOG) is ContextRenderMode.FULL
    assert decision.candidate is candidate
    assert decision.quote.anchor_reported_input_tokens == 5


def test_one_dispatch_floor_selection_uses_one_anchor_snapshot():
    from tests.test_catalog_final_wire_resources import _prepare_case, _skill_source, _measure, _mode
    from pulsara_agent.model_input.contracts import ContextRenderMode, ContextSourceKind
    coordinator, candidate, view, _ = _prepare_case('openai_chat_completions', _skill_source(), budget=150_000)
    plan = view.wire_input_plan
    call = candidate.call
    anchor = ProviderInputUsageAnchor(call.resolved_model_call_id, view.epoch_nonce, view.epoch_revision,
        call.binding.connection_id.value, call.target.fact, plan.route_wire_profile_fingerprint,
        'accepted-alias', 149_000, plan.quote.raw_final_wire_estimated_input_tokens, plan.materialization)
    owner = coordinator._continuity
    owner.observe_usage_anchor(view.scope, epoch_nonce=view.epoch_nonce, epoch_revision=view.epoch_revision,
        reported_model_id=anchor.reported_model_id, anchor=anchor)
    original = coordinator._freeze_candidate_wire_measurement
    observed = []

    async def freeze(candidate, **kwargs):
        measured = await original(candidate, **kwargs)
        observed.append(measured.quote.anchor_reported_input_tokens)
        owner.observe_usage_anchor(view.scope, epoch_nonce=view.epoch_nonce, epoch_revision=view.epoch_revision,
            reported_model_id=anchor.reported_model_id, anchor=replace(anchor, reported_input_tokens=5))
        return measured

    coordinator._freeze_candidate_wire_measurement = freeze
    decision = _measure(coordinator, candidate)
    assert decision.wire_input_plan is not None
    assert observed == [149_000, 149_000]
    assert _mode(decision.candidate, ContextSourceKind.SKILL_CATALOG) is ContextRenderMode.UNAVAILABLE_MINIMAL
    assert owner.current_usage_anchor(view.scope).reported_input_tokens == 5


def test_root_and_child_usage_anchors_are_independent_in_one_owner():
    from pulsara_agent.model_input.contracts import ModelInputScopeKind
    port = _port()
    root_request, _ = _prepared_execution(port)
    child_request, _ = _prepared_execution(port, scope_kind=ModelInputScopeKind.SUBAGENT_TASK, scope_subagent_task_id='subagent:test')
    try:
        owner, root_candidate = _continuity_candidate(root_request)
        root_execution = port.preflight_execution(root_request, append_candidate=root_candidate, install_authority=owner.install_authority)
        root_permit = owner.install(candidate=root_candidate, execution=root_execution)
        _, child_candidate = _continuity_candidate(child_request, owner=owner)
        child_execution = port.preflight_execution(child_request, append_candidate=child_candidate, install_authority=owner.install_authority)
        child_permit = owner.install(candidate=child_candidate, execution=child_execution)
        root_anchor = _anchor(root_request, reported=11, revision=root_permit.epoch_revision, nonce=root_permit.epoch_nonce)
        child_anchor = _anchor(child_request, reported=22, revision=child_permit.epoch_revision, nonce=child_permit.epoch_nonce)
        for permit, anchor in ((root_permit, root_anchor), (child_permit, child_anchor)):
            owner.observe_usage_anchor(permit.scope, epoch_nonce=permit.epoch_nonce, epoch_revision=permit.epoch_revision,
                reported_model_id=anchor.reported_model_id, anchor=anchor)
        assert owner.current_usage_anchor(root_permit.scope) is root_anchor
        assert owner.current_usage_anchor(child_permit.scope) is child_anchor
        owner.retire_terminal_subagent_scope(child_permit.scope)
        assert owner.current_usage_anchor(root_permit.scope) is root_anchor
        assert owner.current_usage_anchor(child_permit.scope) is None
    finally:
        root_request.surface_borrow.close()
        child_request.surface_borrow.close()


def test_storage_failure_does_not_remove_live_anchor_and_is_diagnosed():
    import asyncio
    from types import SimpleNamespace
    from pulsara_agent.conversation_kernel.host import KernelHostSession
    from pulsara_agent.ports.provider_stream import ProviderNormalizedTerminalKind
    port = _port()
    request, _ = _prepared_execution(port)
    try:
        owner, candidate = _continuity_candidate(request)
        execution = port.preflight_execution(request, append_candidate=candidate, install_authority=owner.install_authority)
        permit = owner.install(candidate=candidate, execution=execution)
        captured = []
        host = object.__new__(KernelHostSession)
        host._input_continuity = owner
        host._canonical_deadline = lambda: 100
        host.repository = SimpleNamespace(record_provider_call_usage=lambda *_args: None)
        host.extensions = SimpleNamespace(offer_operational_nowait=captured.append)

        async def fail_write(*_args, **_kwargs):
            raise TimeoutError('injected storage failure')

        host._io = SimpleNamespace(run=fail_write)
        report = transport_usage_report_from_mapping({'input_tokens': 11})
        asyncio.run(host._observe_provider_usage(request, report, permit, ProviderNormalizedTerminalKind.COMPLETED, True))
        assert owner.current_usage_anchor(permit.scope).reported_input_tokens == 11
        assert 'provider_usage_storage_failed' in captured[0].public_payload['diagnostic_codes']
        # Zero is a retained report with a diagnostic, not a replacement anchor.
        zero = transport_usage_report_from_mapping({'input_tokens': 0, 'output_tokens': 2})
        asyncio.run(host._observe_provider_usage(request, zero, permit, ProviderNormalizedTerminalKind.COMPLETED, True))
        assert owner.current_usage_anchor(permit.scope).reported_input_tokens == 11
        assert captured[1].public_payload['input_tokens'] == 0
        assert 'provider_input_zero_nonempty' in captured[1].public_payload['diagnostic_codes']
    finally:
        request.surface_borrow.close()


def test_chat_later_alias_carrier_updates_the_same_normalized_parent_field():
    from pulsara_agent.llm.adapters.openai.chat_completions import ChatCompletionAccumulator
    from pulsara_agent.llm.adapters.openai.events import ProviderLiveItemBuilder
    port = _port()
    request, _ = _prepared_execution(port)
    try:
        accumulator = ChatCompletionAccumulator(builder=ProviderLiveItemBuilder(), route_wire_profile=request.prepared_call.call.target.model_profile.route_wire_profile)
        accumulator.apply({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 9}})
        accumulator.apply({'choices': [], 'usage': {'input_tokens': 10, 'output_tokens': 2}})
        assert accumulator.usage_report.usage.input_tokens == 10
        assert accumulator.usage_report.usage.output_tokens == 2
        assert accumulator.usage_report.usage.computed_total_tokens == 12
    finally:
        request.surface_borrow.close()


def test_cold_successor_may_omit_optional_source_despite_old_epoch_head():
    from tests.test_catalog_final_wire_resources import _prepare_case, _measure
    from tests.test_round3_structured_model_input_compiler import _candidate
    from pulsara_agent.conversation_kernel.cold_epoch import CanonicalColdContinuationSeed
    from pulsara_agent.model_input.contracts import ContextSourceKind
    kind = ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD
    source = _candidate(kind, ('p' * 100_000,))
    coordinator, candidate, view, _ = _prepare_case('openai_chat_completions', source,
        initial_catalog=_candidate(kind, ('old preference',)), budget=50_000, user_text='u' * 140_000)
    assert any(head.source_kind is kind for head in view.source_heads)
    _, template, _, _ = _prepare_case('openai_chat_completions', source, cold=True, budget=100_000)
    append = coordinator._compiler.compile_new_epoch(candidate.compile_request, planning=candidate.planning)
    # Authority-free measurement of the already frozen successor basis. Actual
    # adoption and installation are covered separately by the real Host fixture.
    cold = replace(template.cold_semantic, compile_request=candidate.compile_request,
        seed=CanonicalColdContinuationSeed(candidate.canonical_read), planning=candidate.planning,
        compiled_result=append, prepared_call=candidate.prepared_call)
    candidate = replace(candidate, semantic_input=append.compiled_input, append_result=append, cold_semantic=cold)
    decision = _measure(coordinator, candidate)
    assert decision.wire_input_plan is not None
    assert decision.quote.budget_source == 'heuristic'
    preference = next(item for item in decision.candidate.semantic_input.source_decisions if item.source_kind is kind)
    assert not preference.included
    assert decision.quote.budget_input_tokens <= decision.quote.effective_input_budget_tokens


@pytest.mark.parametrize("api", ["openai_chat_completions", "openai_responses"])
def test_wire_memory_fallback_preserves_installed_state_and_steer_headroom(api):
    import asyncio
    from time import monotonic
    from tests.test_catalog_final_wire_resources import _prepare_case
    from pulsara_agent.conversation_kernel.context_sources import build_memory_context_source, replace_memory_context_sources
    from pulsara_agent.conversation_kernel.memory.dispatch import MemoryDispatchSupport
    from pulsara_agent.model_input.contracts import ContextSourceKind, ContextSourceAbsenceKind
    from pulsara_agent.model_input.continuity import SourceObservationPresence
    kind = ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD
    old = build_memory_context_source(kind=kind, texts=('old preference',))
    updated = build_memory_context_source(kind=kind, texts=('new preference ' * 500,))
    coordinator, candidate, view, _ = _prepare_case(api, updated,
        initial_catalog=old, budget=100_000)
    support = MemoryDispatchSupport(compiler=coordinator._compiler, io_owner=coordinator._io,
        memory_projection=None, input_reader=coordinator._input_reader, deadline_factory=None)
    coordinator._memory_support = support
    # Price the real UNAVAILABLE carrier; choose a fixture boundary at which the
    # admitted human suffix and reserved carrier fit, but optional VALUE does not.
    fallback_sources = replace_memory_context_sources(candidate.sources, (
        build_memory_context_source(kind=kind, texts=None, absence_kind=ContextSourceAbsenceKind.UNAVAILABLE),))
    fallback_request = replace(candidate.compile_request, sources=fallback_sources)
    fallback_append = coordinator._compiler.compile_installed_append(fallback_request, planning=candidate.planning)
    fallback_candidate = replace(candidate, sources=fallback_sources, compile_request=fallback_request,
        append_result=fallback_append, semantic_input=fallback_append.compiled_input)
    measured = asyncio.run(coordinator._freeze_candidate_wire_measurement(fallback_candidate, deadline=monotonic()+30))
    delta = measured.quote.raw_final_wire_estimated_input_tokens - view.wire_input_plan.quote.raw_final_wire_estimated_input_tokens
    measured.discard_materialization_to_quote()
    reservation = support.planning_preference_refresh_reservation(
        planning=candidate.planning, desired=updated, prepared_call=candidate.prepared_call, new_epoch=False)
    assert reservation is not None
    headroom = reservation.invalidation_input_token_ceiling
    plan, call = view.wire_input_plan, candidate.call
    anchor = ProviderInputUsageAnchor(call.resolved_model_call_id, view.epoch_nonce, view.epoch_revision,
        call.binding.connection_id.value, call.target.fact, plan.route_wire_profile_fingerprint,
        'alias', 100_000-delta, plan.quote.raw_final_wire_estimated_input_tokens, plan.materialization)
    coordinator._continuity.observe_usage_anchor(view.scope, epoch_nonce=view.epoch_nonce,
        epoch_revision=view.epoch_revision, reported_model_id='alias', anchor=anchor)
    decision = asyncio.run(coordinator.measure_prepared_wire_candidate(candidate,
        deadline=monotonic()+30, invalidation_reservations=(reservation,)))
    assert decision.wire_input_plan is not None  # steer remains admissible
    from pulsara_agent.conversation_kernel.provider_dispatch import _remaining_invalidation_input_tokens
    assert _remaining_invalidation_input_tokens(decision.candidate, (reservation,)) == 0
    assert headroom > 0
    assert decision.quote.budget_input_tokens == 100_000
    assert decision.quote.anchor_model_call_id == anchor.model_call_id
    absent = next(item for item in decision.candidate.sources.absent_facts if item.source_kind is kind)
    assert absent.absence_kind is ContextSourceAbsenceKind.UNAVAILABLE
    replacement = next(item for item in decision.candidate.append_result.source_heads if item.source_kind is kind)
    assert replacement.presence is SourceObservationPresence.UNAVAILABLE
    assert decision.candidate.compile_request.sources == decision.candidate.sources
    assert decision.candidate.semantic_input.messages[:len(view.messages)] == view.messages


def test_reserved_headroom_exhaustion_keeps_wire_admission_union_valid():
    import asyncio
    from time import monotonic
    from tests.test_catalog_final_wire_resources import _prepare_case, _skill_source
    coordinator, candidate, _, _ = _prepare_case('openai_chat_completions', _skill_source(1), budget=100_000)
    from types import SimpleNamespace
    from pulsara_agent.model_input.contracts import ContextSourceKind
    reservation = SimpleNamespace(source_kind=ContextSourceKind.MEMORY_RECALL,
        invalidation_input_token_ceiling=100_000)
    decision = asyncio.run(coordinator.measure_prepared_wire_candidate(candidate,
        deadline=monotonic()+30, invalidation_reservations=(reservation,)))
    # Wire input itself fits. The steer owner rejects its unsatisfied reservation
    # from this same quote, rather than an inconsistent None-plan union.
    assert decision.wire_input_plan is not None
    assert decision.quote.budget_input_tokens <= 100_000
    assert decision.quote.budget_input_tokens + 100_000 > 100_000
