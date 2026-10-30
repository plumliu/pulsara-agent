"""Compaction keeps the admitted model selection through its existing owners."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionDisposition,
    CompactionOutcome,
    CompactionScope,
    CompactionTrigger,
    build_prepared_manual_compaction_command,
)
from pulsara_agent.conversation_kernel.compaction.runtime import HostCompactionRuntimeOwner
from pulsara_agent.conversation_kernel.direct_model import (
    DirectKernelModelPort,
    KernelModelTargetPreparationRequest,
)
from pulsara_agent.llm.adapters.openai.chat_completions import build_chat_completions_payload
from pulsara_agent.llm.adapters.openai.responses import build_responses_payload
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.model_catalog import (
    ReasoningEffortChoices,
    ReasoningSelectableControls,
    ReasoningTokenBudgetRange,
    ReasoningToggle,
)
from pulsara_agent.llm.model_connections import (
    ReasoningBudgetSelection,
    ReasoningEffortSelection,
    ReasoningToggleSelection,
)
from pulsara_agent.llm.model_target import ModelReasoningSelectionInvalid
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import resolve_model_call
from pulsara_agent.model_input.contracts import ModelInputScopeKind, PreparedProviderInputCut
from pulsara_agent.primitives.model_call import ModelCallPurpose
from tests.support.model_config import test_model_binding, test_model_runtime

SESSION = 'session:336wrfwkamrr3up6kjtsgvgcte'
TURN = 'turn:klmmazz67vfgsfsvg6ceqgdsfu'
WORKSPACE = 'workspace:wdl6b3mtrkghwnvjo4an3ekbau'


def prepared_selection(wire_api='openai_chat_completions', *, toggle=False):
    controls = (
        ReasoningSelectableControls(toggle=ReasoningToggle())
        if toggle
        else ReasoningSelectableControls(effort=ReasoningEffortChoices(('low', 'high')))
    )
    runtime = test_model_runtime(wire_api=wire_api, reasoning=controls)
    binding = replace(
        test_model_binding(runtime),
        reasoning=ReasoningToggleSelection(False) if toggle else ReasoningEffortSelection('high'),
    )
    port = DirectKernelModelPort(model_runtime=runtime)
    request = KernelModelTargetPreparationRequest(
        session_id=SESSION, turn_id=TURN, model_call_index=1,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        maximum_input_tokens=None, binding=binding,
    )
    return runtime, port, request, port.prepare_target(request)


@pytest.mark.parametrize('wire_api', ('openai_chat_completions', 'openai_responses'))
@pytest.mark.parametrize('toggle', (False, True))
def test_summary_and_repair_wire_keep_nondefault_selection(wire_api, toggle):
    _runtime, port, request, prepared = prepared_selection(wire_api, toggle=toggle)
    call = port.resolve_compaction_summary_call(
        active_prepared_call=SimpleNamespace(call=prepared.call)
    )
    assert call.target is prepared.target
    assert call.binding is request.binding
    assert call.fact.purpose is ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
    assert call.resolved_model_call_id != prepared.call.resolved_model_call_id

    # Repair appends denial messages to the same call; neither request form
    # gets a fresh default selection at the wire boundary.
    first = (LLMMessage.user('Summarize the preceding history.'),)
    repaired = first + (
        LLMMessage.assistant_turn(tool_calls=()),
        LLMMessage.user('Return the summary without tools.'),
    )
    for messages in (first, repaired):
        context = LLMContext(
            messages=messages, context_id='summary-config',
            resolved_model_call_id=call.resolved_model_call_id, model_call_index=1,
        )
        payload = (
            build_chat_completions_payload(call=call, context=context)
            if wire_api == 'openai_chat_completions'
            else build_responses_payload(call=call, context=context)
        )
        if wire_api == 'openai_responses':
            assert payload['reasoning']['effort'] == ('none' if toggle else 'high')
        elif toggle:
            assert payload['extra_body']['reasoning'] == {'enabled': False}
        else:
            assert payload['reasoning_effort'] == 'high'


def test_summary_preserves_complete_typed_budget_selection():
    _runtime, port, request, prepared = prepared_selection()
    # The resolution boundary supports the full typed selection even when a
    # production wire adapter currently exposes only effort/toggle controls.
    target = replace(prepared.target, contract=replace(
        prepared.target.contract,
        reasoning=ReasoningSelectableControls(budget=ReasoningTokenBudgetRange(1024, 8192)),
    ))
    binding = replace(request.binding, reasoning=ReasoningBudgetSelection(4096))
    origin = resolve_model_call(target=target, binding=binding, purpose=ModelCallPurpose.AGENT_MODEL_LOOP)
    summary = port.resolve_compaction_summary_call(active_prepared_call=SimpleNamespace(call=origin))
    assert summary.target is target
    assert summary.binding is binding
    assert summary.binding.reasoning == ReasoningBudgetSelection(4096)


def test_snapshot_target_uses_the_captured_observation_without_catalog_reread(monkeypatch):
    runtime, port, request, expected = prepared_selection()
    snapshot = runtime.freeze_resolution_snapshot()

    def unavailable(*_args, **_kwargs):
        raise AssertionError('manual target must not resolve a second catalog/config cut')

    monkeypatch.setattr(type(runtime), 'selectable_catalog', unavailable)
    monkeypatch.setattr(type(runtime.catalog), 'selectable', unavailable)
    monkeypatch.setattr(type(runtime), 'connection', unavailable)
    captured = port.prepare_target_from_snapshot(request, resolution_snapshot=snapshot)
    assert captured.target.fact == expected.target.fact
    assert captured.call.binding is request.binding
    assert captured.target.contract is snapshot.validate(request.binding).target
    invalid = replace(request, binding=replace(request.binding, reasoning=ReasoningEffortSelection('invalid')))
    with pytest.raises(ModelReasoningSelectionInvalid):
        port.prepare_target_from_snapshot(invalid, resolution_snapshot=snapshot)


@pytest.mark.parametrize('toggle', (False, True))
def test_frozen_epoch_reborrow_keeps_request_reasoning(toggle):
    _runtime, port, request, prepared = prepared_selection(toggle=toggle)
    reborrowed = port.prepare_frozen_epoch_target(request, bundle=prepared.epoch_call_target.target_bundle)
    assert reborrowed.call.binding is request.binding
    assert reborrowed.target.fact == prepared.target.fact


def test_manual_identity_binds_selected_model_and_exact_source():
    _runtime, _port, request, _prepared = prepared_selection()
    cut = PreparedProviderInputCut(SESSION, TURN, 'binding:first', 12)
    values = dict(session_id=SESSION, command_id='command:manual', scope_kind=ModelInputScopeKind.ROOT,
                  target_turn_id=TURN, model_call_binding=request.binding, source_cut=cut)
    command = build_prepared_manual_compaction_command(**values)
    assert command == build_prepared_manual_compaction_command(**values)
    changed_binding = build_prepared_manual_compaction_command(**dict(values,
        model_call_binding=replace(request.binding, reasoning=ReasoningEffortSelection('low'))))
    changed_source = build_prepared_manual_compaction_command(**dict(values,
        source_cut=replace(cut, provider_input_through_sequence=13)))
    assert len({command.semantic_digest, changed_binding.semantic_digest, changed_source.semantic_digest}) == 3


def test_manual_runtime_joins_only_the_same_idle_operation_and_settles_once():
    async def run():
        _runtime, port, request, prepared = prepared_selection()
        owner = HostCompactionRuntimeOwner()
        fields = dict(command_id='command:manual', scope_kind=ModelInputScopeKind.ROOT,
                      target_turn_id=TURN, prepared_target=prepared)
        manual, future = await owner.request_manual(**fields)
        # A fresh ephemeral call ID does not change the captured operation.
        same, joined = await owner.request_manual(**dict(fields, prepared_target=port.prepare_target(request)))
        assert same is manual and joined is future
        with pytest.raises(RuntimeError, match='another manual'):
            await owner.request_manual(**dict(fields, command_id='command:other'))
        assert await owner.claim_manual_execution(manual)
        assert not await owner.claim_manual_execution(manual)
        outcome = CompactionOutcome(CompactionDisposition.COMPACTED, TURN, None, 1, 'COMPACTED')
        await owner.settle_manual(manual, outcome)
        assert future.result() is outcome
        cached = await owner.find_manual(command_id='command:manual', scope_kind=ModelInputScopeKind.ROOT)
        assert cached is not None and cached.result() is outcome
        await owner.aclose()
    asyncio.run(run())


def test_compaction_projection_reports_its_actual_scope():
    async def run():
        owner = HostCompactionRuntimeOwner()
        scope = CompactionScope(SESSION, WORKSPACE, TURN, ModelInputScopeKind.ROOT)
        task = asyncio.current_task()
        owner.install_fence_under_host_lock(scope=scope, trigger=CompactionTrigger.MANUAL,
                                          attempt_id='attempt', owner_task=task)
        assert owner.current_projection() == (True, 'MANUAL', 'PREPARING', 'ROOT')
        owner.remove_fence_under_host_lock(scope=scope, attempt_id='attempt', owner_task=task)
        assert owner.current_projection() == (False, None, None, None)
    asyncio.run(run())
