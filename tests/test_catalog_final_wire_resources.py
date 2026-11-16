"""Exact final-wire admission preserves complete mandatory input and installed prefixes."""

from __future__ import annotations


from tests.support.images import bound_test_image


import asyncio


from dataclasses import replace


from io import BytesIO


from time import monotonic


from types import SimpleNamespace


import pytest




from PIL import Image


from pulsara_agent.capability.contracts import (
    FrozenSkillCapabilityDispatchView,
    LocalSkillRootKind,
)


from pulsara_agent.capability.render import render_catalog_prompt


from pulsara_agent.capability.types import LooseSkillOrigin, ResolvedSkillCatalogEntry


from pulsara_agent.conversation_kernel.cold_epoch import (
    CanonicalColdContinuationSeed,
    KernelColdEpochInputAssembler,
)


from pulsara_agent.conversation_kernel.compaction.contracts import (
    provider_input_item_canonical_expanded_bytes,
)


from pulsara_agent.conversation_kernel.context_sources import (
    FrozenNonTriggerContextSources,
)


from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
)




from pulsara_agent.conversation_kernel.provider_dispatch import (
    PreparedProviderWireCandidate,
    ProviderDispatchCoordinator,
)




from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMTextPart,
    LLMMessage,
)


from pulsara_agent.llm.provider import RouteWireProfile


from pulsara_agent.model_input.compiler import StructuredModelInputCompiler


from pulsara_agent.model_input.contracts import (
    ContextSourceKind,
    ContextBudgetClass,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputCompileFailureKind,
    ProviderToolCall,
    StructuredModelInputCompileError,
)


from pulsara_agent.model_input.continuity import (
    NoNewTriggerAnchor,
    InstalledObservationState,
    ProviderInputContinuityScope,
)


from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
    freeze_provider_replay_manifest_cut,
)


from pulsara_agent.primitives.context import context_fingerprint, freeze_json




from tests.support.round3 import new_test_provider_input_continuity_owner


from tests.support.model_input import (
    _PREPARED_MODEL_CALLS,
    _append_anchor,
    _append_frontier,
    _candidate,
    _canonical_facts,
    _compile_and_install_append,
    _prepared_request,
    _snapshot,
    _sources,
    _tool_result,
    _user,
)


_WIRE_APIS = ("openai_chat_completions", "openai_responses")


class _InlineIO:
    async def run(self, operation, *args, **kwargs):
        return operation(*args, **kwargs)


def _dispatch_read(request):
    identity = request.canonical_input.identity
    manifest = freeze_provider_replay_manifest_cut(
        session_id=identity.session_id,
        scope=ProviderInputContinuityScope(
            identity.session_id,
            identity.conversation_scope_kind,
        ),
        context_binding_revision_id=identity.context_binding_revision_id,
        provider_input_through_sequence=identity.provider_input_through_sequence,
        manifests=(),
    )
    return FrozenCanonicalProviderDispatchRead(
        request.canonical_facts,
        manifest,
        context_fingerprint(
            "pulsara.canonical-provider-dispatch-read:v1",
            {
                "compile": request.canonical_facts.canonical_read_cut_fingerprint,
                "replay_manifest_cut": manifest.cut_fingerprint,
            },
        ),
    )


def _skill_source(count=230):
    text = render_catalog_prompt(
        tuple(
            ResolvedSkillCatalogEntry(
                name=f"skill-{index}",
                description="x" * 1_000,
                skill_path=f"/test/skills/{index}/SKILL.md",
                origin=LooseSkillOrigin(LocalSkillRootKind.WORKSPACE_AGENTS),
            )
            for index in range(count)
        )
    )
    assert text is not None
    assert len(text.encode()) < 384 * 1024
    return _candidate(ContextSourceKind.SKILL_CATALOG, text)


def _mixed_results():
    encoded = BytesIO()
    Image.new("RGB", (3_500, 3_500), (11, 23, 41)).save(encoded, "PNG")
    image = bound_test_image("image/png", encoded.getvalue(), 3_500, 3_500)
    arguments = (
        freeze_json({"action": "SET_PLUGIN_ENABLED"}),
        freeze_json({"target": {"kind": "PATH", "path": "plot.png"}}),
    )
    calls = tuple(
        ProviderToolCall(f"call:{index}", name, args)
        for index, name, args in zip(
            (0, 1), ("manage_capability", "view_image"), arguments, strict=True
        )
    )
    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        'entry:5qidalqvkujk5ykzzzfpwie2qy',
        2,
        'turn:klmmazz67vfgsfsvg6ceqgdsfu',
        (),
        tool_calls=calls,
    )
    content = FrozenPromptContent((LLMTextPart("image result"), image))
    public_text = "image result"
    results = tuple(
        replace(
            _tool_result(text, sequence=index + 3, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu'),
            tool_call_id=call.tool_call_id,
            tool_request_entry_id=assistant.source_entry_id,
            tool_call_ordinal=index,
            tool_call_arguments=call.arguments,
            tool_call_name=call.tool_name,
            content=content.parts if index else (LLMTextPart(text),),
        )
        for index, call, text in zip(
            (0, 1), calls, ("Enabled.", public_text), strict=True
        )
    )
    return (assistant, *results), image


def _prepare_case(
    api,
    source,
    *,
    cold=False,
    budget=100_000,
    mixed=False,
    user_text="Continue.",
    extra_sources=(),
    initial_catalog=None,
    tool_items=(),
    tool_names=None,
):
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    initial = _prepared_request(
        _snapshot(_user(user_text)),
        _sources(*(() if initial_catalog is None else (initial_catalog,))),
        budget=budget,
        tool_names=tool_names if tool_names is not None else (("manage_capability", "view_image") if mixed else ()),
        route_wire_profile=RouteWireProfile(wire_api=api),
    )
    model, prepared = _PREPARED_MODEL_CALLS[initial.compile_binding.call_fact.resolved_model_call_id]
    view = None
    if not cold:
        _, view = _compile_and_install_append(
            compiler=compiler, owner=owner, request=initial
        )
    items = initial.canonical_input.items
    image = None
    if mixed:
        results, image = _mixed_results()
        items += results
    items += tool_items
    snapshot = _snapshot(
        *items,
        canonical_expanded_bytes=sum(
            provider_input_item_canonical_expanded_bytes(item) for item in items
        ),
    )
    same_as_initial=source==initial_catalog
    if not cold and source.source_kind is ContextSourceKind.SKILL_CATALOG:
        source=_candidate(ContextSourceKind.SKILL_CATALOG_CHANGES,source.text)
    current_sources=_sources(*extra_sources) if not cold and same_as_initial else _sources(source,*extra_sources)
    request = replace(
        initial,
        canonical_input=snapshot,
        canonical_facts=_canonical_facts(snapshot),
        sources=current_sources,
    )
    scope = ProviderInputContinuityScope(
        'session:336wrfwkamrr3up6kjtsgvgcte', snapshot.identity.conversation_scope_kind
    )
    planning = owner.freeze_planning_input(
        scope=scope,
        canonical_frontier=_append_frontier(request),
        dispatch_anchor=_append_anchor(request) if cold else NoNewTriggerAnchor(None),
    )
    read = _dispatch_read(request)
    exposure = prepared.tool_surface.capability_exposure_plan
    assert exposure is not None
    semantic = None
    if cold:
        tool_view = exposure.dispatch_view
        cut = tool_view.parent_dispatch_cut
        skill_view = FrozenSkillCapabilityDispatchView(cut, cut.registry.skill_facts)
        non_trigger = FrozenNonTriggerContextSources(
            candidates=request.sources.candidates,diagnostics=(),tool_exposure_plan=exposure,
            skill_dispatch_view=skill_view,skill_owner_snapshot=None,mcp_owner_snapshot=None,
            canonical_facts=request.canonical_facts,previous_state=InstalledObservationState(),
            new_context=True,active_compaction=False)
        semantic = KernelColdEpochInputAssembler(compiler).prepare_semantic(
            seed=CanonicalColdContinuationSeed(read),
            compile_request=request,
            planning=planning,
            prepared_call=prepared,
            capability_dispatch_cut=cut,
            tool_view=tool_view,
            skill_view=skill_view,
            tool_exposure_plan=exposure,
            non_trigger_sources=non_trigger,
            replay_target=model.replay_target_for_resolved_call(prepared.call),
            deadline_monotonic=monotonic() + 30,
        )
        append = semantic.compiled_result
    else:
        append = compiler.compile_installed_append(request, planning=planning)
    candidate = PreparedProviderWireCandidate(
        canonical_read=read,
        semantic_input=append.compiled_input,
        prepared_call=prepared,
        native_projection_set=prepared.native_projection_set,
        compile_request=request,
        planning=planning,
        append_result=append,
        cold_semantic=semantic,
        sources=request.sources,
        tool_exposure_plan=exposure,
        memory_context=FrozenModelCallMemoryContext(),
    )
    coordinator = object.__new__(ProviderDispatchCoordinator)
    coordinator._continuity = owner
    coordinator._model = model
    coordinator._compiler = compiler
    coordinator._io = _InlineIO()
    coordinator._input_reader = SimpleNamespace(
        hydrate_selected_provider_replays=lambda **_kwargs: None
    )
    return coordinator, candidate, view, image


def _measure(coordinator, candidate):
    return asyncio.run(
        coordinator.measure_prepared_wire_candidate(
            candidate, deadline=monotonic() + 30
        )
    )


def _assert_typed_budget_rejection(coordinator, decision):
    with pytest.raises(StructuredModelInputCompileError) as raised:
        coordinator.bind_prepared_executable_wire_input(
            owner_dispatch=SimpleNamespace(direct_switch_admission=None),
            decision=decision,
            deadline=monotonic() + 30,
        )
    assert (
        raised.value.kind
        is ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET
    )



@pytest.mark.parametrize("api",_WIRE_APIS)
def test_complete_mandatory_catalog_is_not_degraded_at_final_wire_boundary(api):
    coordinator,candidate,_,_=_prepare_case(api,_skill_source(),cold=True,budget=64_000)
    original=candidate.semantic_input.messages
    decision=_measure(coordinator,candidate)
    assert decision.wire_input_plan is None
    assert decision.candidate.semantic_input.messages==original
    assert decision.quote.budget_input_tokens>decision.quote.effective_input_budget_tokens
    _assert_typed_budget_rejection(coordinator,decision)


@pytest.mark.parametrize("api",_WIRE_APIS)
@pytest.mark.parametrize("cold",(False,True))
def test_exact_wire_admission_keeps_the_installed_prefix_and_complete_notice(api,cold):
    coordinator,candidate,view,_=_prepare_case(api,_skill_source(1),cold=cold)
    decision=_measure(coordinator,candidate)
    assert decision.wire_input_plan is not None
    assert decision.candidate is candidate
    assert all(item.included for item in candidate.semantic_input.source_decisions)
    if view is not None:
        assert candidate.semantic_input.system_prompt==view.system_prompt
        assert candidate.semantic_input.tools==view.tools
        assert candidate.semantic_input.messages[:len(view.messages)]==view.messages
        assert decision.wire_input_plan.materialization.ordered_input_items[:len(view.wire_input_plan.materialization.ordered_input_items)]==view.wire_input_plan.materialization.ordered_input_items


@pytest.mark.parametrize("api",_WIRE_APIS)
def test_actual_wire_can_omit_only_the_complete_optional_user_action(api):
    action=_candidate(ContextSourceKind.USER_ACTIONS,"u"*500_000,budget=ContextBudgetClass.OPTIONAL)
    coordinator,candidate,view,_=_prepare_case(api,_candidate(ContextSourceKind.TURN_CONTEXT,"frozen permission"),extra_sources=(action,))
    decision=_measure(coordinator,candidate)
    assert decision.wire_input_plan is not None
    selected={item.source_kind:item.included for item in decision.candidate.semantic_input.source_decisions}
    assert selected[ContextSourceKind.TURN_CONTEXT]
    assert not selected[ContextSourceKind.USER_ACTIONS]
    assert decision.candidate.semantic_input.messages[:len(view.messages)]==view.messages


@pytest.mark.parametrize("api",_WIRE_APIS)
def test_hook_is_a_separate_optional_message_at_actual_wire_admission(api):
    coordinator,candidate,_,_=_prepare_case(api,_skill_source(1),cold=True)
    hook=LLMMessage.user('{"pulsara_hook_context":[{"event":"Stop","text":"'+"h"*500_000+'"}]}')
    sources=replace(candidate.sources,hook_context=hook)
    request=replace(candidate.compile_request,sources=sources)
    append=coordinator._compiler.compile_new_epoch(request,planning=candidate.planning)
    candidate=replace(candidate,compile_request=request,sources=sources,append_result=append,
        semantic_input=append.compiled_input,cold_semantic=replace(candidate.cold_semantic,compile_request=request,compiled_result=append))
    assert append.compiled_input.messages[-1]==hook
    decision=_measure(coordinator,candidate)
    assert decision.wire_input_plan is not None
    assert hook not in decision.candidate.semantic_input.messages
    assert all(item.included for item in decision.candidate.semantic_input.source_decisions)
