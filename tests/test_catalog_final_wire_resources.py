"""Real compiler/adapter selection with frozen reads; no database or provider calls."""

from __future__ import annotations
from tests.support.images import bound_test_image

import asyncio
from dataclasses import replace
from io import BytesIO
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.mcp_config import LocalConfiguredMcpRuntimeSource
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
    _render_mcp_catalog,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
)
from pulsara_agent.conversation_kernel.mcp.contracts import (
    McpServerCatalogEntry,
    McpServerState,
    build_catalog_snapshot,
)
from pulsara_agent.conversation_kernel.provider_dispatch import (
    PreparedProviderWireCandidate,
    ProviderDispatchCoordinator,
)
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMTextPart,
    frozen_tool_result_public_text,
)
from pulsara_agent.llm.provider import RouteWireProfile
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.contracts import (
    ContextRenderMode,
    ContextSourceKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputCompileFailureKind,
    ProviderToolCall,
    StructuredModelInputCompileError,
)
from pulsara_agent.model_input.continuity import (
    NoNewTriggerAnchor,
    ProviderInputContinuityScope,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
    freeze_provider_replay_manifest_cut,
)
from pulsara_agent.primitives.context import context_fingerprint, freeze_json
from pulsara_agent.primitives.tool_result_projection import (
    classify_tool_result_delivery,
)
from tests.support.round3 import new_test_provider_input_continuity_owner
from tests.test_round3_structured_model_input_compiler import (
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
    return _candidate(ContextSourceKind.SKILL_CATALOG, (text, ""))


def _mcp_source():
    # Use the production bounded renderer at all three existing variant bounds.
    servers = tuple(
        McpServerCatalogEntry(
            runtime_source=LocalConfiguredMcpRuntimeSource(),
            server_id=f"server-{index:03d}",
            display_name=f"Server {index}",
            status=McpServerState.READY,
            required=False,
            exposed_tool_count=0,
            discovered_tool_count=0,
            resource_count=1,
            resource_template_count=0,
            prompt_count=0,
            bounded_tool_name_overview=(),
            sanitized_instructions="",
            stable_failure_category=None,
            tool_surface_semantic_fingerprint=None,
            catalog_semantic_fingerprint=f"catalog:{index}",
            scope_subagents=True,
        )
        for index in range(100)
    )
    catalog = build_catalog_snapshot(owner_epoch="epoch:aaaaaaaaaaaaaaaaaaaaaaaaaa", catalog_revision=1, entries=servers)
    return _candidate(
        ContextSourceKind.MCP_CATALOG,
        _render_mcp_catalog(catalog, SimpleNamespace(routes=()), ()),
    )


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
    content = FrozenPromptContent((LLMTextPart("Image loaded."), image))
    public_text = frozen_tool_result_public_text(content)
    results = tuple(
        replace(
            _tool_result(text, sequence=index + 3, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu', artifact=False),
            tool_call_id=call.tool_call_id,
            tool_request_entry_id=assistant.source_entry_id,
            tool_call_ordinal=index,
            tool_call_arguments=call.arguments,
            content=content.parts if index else (LLMTextPart(text),),
            tool_result_delivery=classify_tool_result_delivery(
                tool_name=call.tool_name,
                result_state="SUCCESS",
                has_image_attachment=bool(index),
            ),
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
    model, prepared = _PREPARED_MODEL_CALLS[initial.compile_binding.binding_fingerprint]
    # The old helper copies an equal binding; production owns the exact instance.
    initial = replace(initial, compile_binding=prepared.compile_binding)
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
    request = replace(
        initial,
        canonical_input=snapshot,
        canonical_facts=_canonical_facts(snapshot),
        sources=_sources(source, *extra_sources),
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
            request.sources.candidates,
            request.sources.absent_facts,
            (),
            request.sources.registry_fingerprint,
            exposure,
            skill_view,
            # Owner acquisition is outside this frozen-input compiler/wire test.
            None,
        )
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


def _mode(candidate, kind):
    return next(
        item.selected_mode
        for item in candidate.semantic_input.source_decisions
        if item.source_kind is kind
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


@pytest.mark.parametrize("api", _WIRE_APIS)
@pytest.mark.parametrize(
    "cold", (False, True), ids=("same-epoch", "cold-minimum-history")
)
@pytest.mark.parametrize("mixed", (False, True), ids=("catalog", "catalog-and-image"))
def test_actual_wire_degrades_large_skill_catalog_and_preserves_mandatory_input(
    api, cold, mixed
):
    # Escaped provider JSON exceeds the budget even for this smaller source text.
    budget = 78_000 if mixed else 64_000
    coordinator, candidate, view, image = _prepare_case(
        api,
        _skill_source(),
        budget=budget,
        cold=cold,
        mixed=mixed,
        initial_catalog=None if cold else _skill_source(1),
    )
    original = candidate.semantic_input
    raw = coordinator._model.freeze_wire_measurement(
        call=candidate.call,
        compile_binding=candidate.compile_binding,
        native_projection_set=candidate.native_projection_set,
        semantic_input=original,
        replay_hydration=None,
    )
    assert sum(len(p.text) for m in original.messages for p in m.content if isinstance(p, LLMTextPart)) > 0
    assert raw.quote.raw_final_wire_estimated_input_tokens > budget
    raw.discard_materialization_to_quote()

    decision = _measure(coordinator, candidate)

    assert decision.wire_input_plan is not None
    selected = decision.candidate
    assert (
        _mode(selected, ContextSourceKind.SKILL_CATALOG)
        is ContextRenderMode.UNAVAILABLE_MINIMAL
    )
    assert selected.sources is candidate.sources
    assert selected.canonical_read is candidate.canonical_read
    assert selected.planning is candidate.planning
    assert decision.quote.raw_final_wire_estimated_input_tokens <= budget
    if view is not None:
        assert selected.semantic_input.system_prompt == view.system_prompt
        assert selected.semantic_input.tools == view.tools
        assert selected.semantic_input.messages[: len(view.messages)] == view.messages
        installed_wire = view.wire_input_plan.materialization
        selected_wire = decision.wire_input_plan.materialization
        assert selected_wire.root_policy_value == installed_wire.root_policy_value
        assert selected_wire.tool_items == installed_wire.tool_items
        assert (
            selected_wire.ordered_input_items[: len(installed_wire.ordered_input_items)]
            == installed_wire.ordered_input_items
        )
    else:
        assert selected.cold_semantic.compiled_result is selected.append_result
        bound = KernelColdEpochInputAssembler(coordinator._compiler).bind_prepared_wire(
            selected.cold_semantic,
            wire_input_plan=decision.wire_input_plan,
            deadline_monotonic=monotonic() + 30,
        )
        assert bound.compiled_input is selected.semantic_input
    if image is not None:
        assert (
            decision.quote.final_wire_visual_image_tokens
            == raw.quote.final_wire_visual_image_tokens
            > 0
        )
        images = tuple(
            part
            for message in selected.semantic_input.messages
            for part in message.content
            if isinstance(part, LLMImagePart)
        )
        assert images == (image,)
        assert (
            selected.canonical_read.compile_snapshot.canonical_input.items
            == candidate.canonical_read.compile_snapshot.canonical_input.items
        )


@pytest.mark.parametrize("api", _WIRE_APIS)
def test_catalog_that_fits_actual_wire_keeps_full_and_original_candidate(api):
    coordinator, candidate, _, _ = _prepare_case(api, _skill_source(1))

    decision = _measure(coordinator, candidate)

    assert decision.wire_input_plan is not None
    assert decision.candidate is candidate
    assert _mode(candidate, ContextSourceKind.SKILL_CATALOG) is ContextRenderMode.FULL




@pytest.mark.parametrize("api", _WIRE_APIS)
def test_smallest_catalog_cannot_hide_mandatory_final_wire_exhaustion(api):
    # Newlines fit semantic text but expand when serialized into final-wire JSON.
    coordinator, candidate, _, _ = _prepare_case(
        api,
        _skill_source(),
        cold=True,
        budget=100_000,
        user_text="\n" * 220_000,
    )

    decision = _measure(coordinator, candidate)

    assert decision.wire_input_plan is None
    assert decision.quote.raw_final_wire_estimated_input_tokens > 100_000
    _assert_typed_budget_rejection(coordinator, decision)
    assert (
        _mode(decision.candidate, ContextSourceKind.SKILL_CATALOG)
        is ContextRenderMode.UNAVAILABLE_MINIMAL
    )


@pytest.mark.parametrize("api", _WIRE_APIS)
@pytest.mark.parametrize("user_bytes,admitted", ((28_000, True), (40_000, False)))
def test_mcp_remeasures_each_legal_variant_and_rejects_if_reference_cannot_fit(
    api, user_bytes, admitted
):
    coordinator, candidate, _, _ = _prepare_case(
        api,
        _mcp_source(),
        cold=True,
        budget=16_000,
        user_text="\n" * user_bytes,
    )
    observed_modes = []

    def hydrate(**kwargs):
        observed_modes.append(
            next(
                item.selected_mode
                for item in kwargs["compiled_input"].source_decisions
                if item.source_kind is ContextSourceKind.MCP_CATALOG
            )
        )
        return None

    coordinator._input_reader.hydrate_selected_provider_replays = hydrate

    decision = _measure(coordinator, candidate)

    assert (decision.wire_input_plan is not None) is admitted
    assert observed_modes[-1] is (ContextRenderMode.REF_ONLY if admitted else None)
    if admitted:
        assert observed_modes == [
            ContextRenderMode.FULL,
            ContextRenderMode.COMPACT,
            ContextRenderMode.REF_ONLY,
        ]
    else:
        assert decision.quote.raw_final_wire_estimated_input_tokens > 16_000
        _assert_typed_budget_rejection(coordinator, decision)


class _RecordedHookReservation:
    def __init__(self):
        self.retire_calls = 0

    def retire(self):
        self.retire_calls += 1
        assert self.retire_calls == 1


class _RecordedDispatchAuthority:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        assert self.close_calls == 1


def _prepared_hook_case(api, *, catalog_count=230):
    from pulsara_agent.conversation_kernel.provider_dispatch import PreparedProviderDispatch

    coordinator, initial, _, _ = _prepare_case(
        api, _skill_source(catalog_count), cold=True, budget=64_000
    )
    authority = _RecordedDispatchAuthority()
    reservation = _RecordedHookReservation()
    retired_plans = []
    continuity = coordinator._continuity

    class RecordingContinuity:
        def __getattr__(self, name):
            return getattr(continuity, name)

        def abort_planning(self, planning):
            retired_plans.append(planning)
            return continuity.abort_planning(planning)

    coordinator._continuity = RecordingContinuity()
    coordinator._cold_epoch_assembler = KernelColdEpochInputAssembler(coordinator._compiler)
    hook = _candidate(ContextSourceKind.HOOK_CONTEXT, ("Hook context", "Hook context"))
    coordinator._context_source_collector = SimpleNamespace(
        freeze_hook_context_source=lambda **_: (hook, reservation)
    )
    base = PreparedProviderDispatch(
        _execution_authority=authority,
        _continuity_owner=coordinator._continuity,
        canonical_read=initial.canonical_read,
        canonical_facts=initial.canonical_read.compile_snapshot,
        planning=initial.planning,
        prepared_call=initial.prepared_call,
        capability_dispatch_cut=initial.cold_semantic.capability_dispatch_cut,
        tool_exposure_plan=initial.tool_exposure_plan,
        sources=initial.sources,
        compile_request=initial.compile_request,
        append_result=initial.append_result,
        memory_context=initial.memory_context,
        cold_semantic=initial.cold_semantic,
        non_trigger_sources=initial.cold_semantic.non_trigger_sources,
    )
    sibling = asyncio.run(coordinator.prepare_hook_context_sibling(
        base, model_call_index=1, deadline=monotonic() + 30
    ))
    assert sibling is not None
    assert _mode(sibling.candidate, ContextSourceKind.HOOK_CONTEXT) is ContextRenderMode.FULL
    return coordinator, base, sibling, authority, reservation, retired_plans


@pytest.mark.parametrize("api", _WIRE_APIS)
def test_hook_sibling_binds_measured_catalog_winner_and_transfers_authority(api):
    coordinator, base, sibling, authority, reservation, retired = _prepared_hook_case(api)
    original = sibling.candidate
    decision = _measure(coordinator, original)
    assert decision.wire_input_plan is not None
    assert decision.candidate is not original
    assert _mode(original, ContextSourceKind.SKILL_CATALOG) is ContextRenderMode.FULL
    assert _mode(decision.candidate, ContextSourceKind.SKILL_CATALOG) is ContextRenderMode.UNAVAILABLE_MINIMAL
    # Existing degradation order omits this optional Hook before the catalog.
    assert _mode(decision.candidate, ContextSourceKind.HOOK_CONTEXT) is None
    assert decision.quote.raw_final_wire_estimated_input_tokens <= 64_000

    # Compaction's owner selects the measured sibling before the production
    # dispatch owner transfers the unique physical authority and reservation.
    sibling.candidate = decision.candidate
    selected = coordinator.bind_selected_provider_dispatch(base=base, sibling=sibling)
    assert selected.append_result is decision.candidate.append_result
    assert selected.cold_semantic is decision.candidate.cold_semantic
    assert selected.sources is decision.candidate.sources
    assert selected.append_result.compiled_input is decision.candidate.semantic_input
    assert not base.owns_execution_authority and selected.owns_execution_authority
    assert not sibling.owns_reservation and selected.has_hook_context_reservation
    assert authority.close_calls == reservation.retire_calls == 0
    with pytest.raises(RuntimeError, match="already consumed"):
        sibling.take_reservation()
    selected.close()
    assert authority.close_calls == reservation.retire_calls == 1
    assert retired == [selected.planning]
    with pytest.raises(RuntimeError, match="authority is consumed"):
        selected.close()



@pytest.mark.parametrize("api", _WIRE_APIS)
def test_hook_sibling_that_fits_remains_in_the_bound_wire_input(api):
    coordinator, base, sibling, authority, reservation, retired = _prepared_hook_case(
        api, catalog_count=1
    )
    original = sibling.candidate
    decision = _measure(coordinator, original)
    assert decision.wire_input_plan is not None and decision.candidate is original
    assert _mode(decision.candidate, ContextSourceKind.HOOK_CONTEXT) is ContextRenderMode.FULL
    assert "Hook context" in repr(decision.wire_input_plan.materialization.ordered_input_items)
    sibling.candidate = decision.candidate
    selected = coordinator.bind_selected_provider_dispatch(base=base, sibling=sibling)
    assert selected.append_result.compiled_input is decision.candidate.semantic_input
    assert not base.owns_execution_authority and selected.has_hook_context_reservation
    selected.close()
    assert authority.close_calls == reservation.retire_calls == 1
    assert retired == [selected.planning]

def test_hook_sibling_binding_rejects_mismatched_cut_and_retires_reservation():
    coordinator, base, sibling, authority, reservation, retired = _prepared_hook_case(
        "openai_chat_completions"
    )
    base.tool_exposure_plan = object()
    with pytest.raises(StructuredModelInputCompileError) as raised:
        coordinator.bind_selected_provider_dispatch(base=base, sibling=sibling)
    assert raised.value.kind is ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
    assert not sibling.owns_reservation and reservation.retire_calls == 1
    assert base.owns_execution_authority and authority.close_calls == 0
    base.close()
    assert authority.close_calls == 1 and retired == [base.planning]


def test_hook_sibling_binding_failure_closes_transferred_authority_and_reservation(monkeypatch):
    import pulsara_agent.conversation_kernel.provider_dispatch as dispatch_module

    coordinator, base, sibling, authority, reservation, retired = _prepared_hook_case(
        "openai_chat_completions"
    )
    def fail_final(**_kwargs):
        raise RuntimeError("injected final dispatch construction failure")

    monkeypatch.setattr(dispatch_module, "PreparedProviderDispatch", fail_final)
    with pytest.raises(RuntimeError, match="injected final dispatch construction failure"):
        coordinator.bind_selected_provider_dispatch(base=base, sibling=sibling)
    assert not base.owns_execution_authority and not sibling.owns_reservation
    assert authority.close_calls == reservation.retire_calls == 1
    assert not retired  # No dispatch was constructed to own continuity cleanup.


@pytest.mark.parametrize("api", _WIRE_APIS)
@pytest.mark.parametrize("cold", (False, True))
def test_catalog_wire_retry_omits_optional_source_only_after_wire_feedback(api, cold):
    kind = ContextSourceKind.MEMORY_WRITE_HINT
    coordinator, candidate, _, _ = _prepare_case(
        api,
        _skill_source(),
        cold=cold,
        user_text="u" * 148_000,
        extra_sources=(_candidate(kind, ("h" * 1_024,)),),
    )
    original = next(
        item
        for item in candidate.semantic_input.source_decisions
        if item.source_kind is kind
    )
    assert original.included
    assert original.selected_mode is ContextRenderMode.FULL
    assert _mode(candidate, ContextSourceKind.SKILL_CATALOG) is ContextRenderMode.FULL

    decision = _measure(coordinator, candidate)

    assert decision.wire_input_plan is not None
    selected = next(
        item
        for item in decision.candidate.semantic_input.source_decisions
        if item.source_kind is kind
    )
    assert not selected.included
    assert selected.selected_mode is None


@pytest.mark.parametrize("request_owner", ("candidate", "cold-assembly"))
@pytest.mark.parametrize(
    "field,value", (("context_id", 'context:slooqntuoemzed5ha7sta3saq4'), ("model_call_index", 2))
)
def test_cold_wire_candidate_rejects_mixed_compilation_basis(
    request_owner, field, value
):
    _, candidate, _, _ = _prepare_case(
        "openai_chat_completions",
        _skill_source(),
        cold=True,
    )
    different_request = replace(candidate.compile_request, **{field: value})

    with pytest.raises(ValueError):
        if request_owner == "candidate":
            replace(candidate, compile_request=different_request)
        else:
            replace(
                candidate,
                cold_semantic=replace(
                    candidate.cold_semantic,
                    compile_request=different_request,
                ),
            )


@pytest.mark.parametrize("cold", (False, True))
@pytest.mark.parametrize("failure_site", ("floor-selection", "recompile"))
def test_catalog_selection_failure_consumes_real_measurement_exactly_once(
    monkeypatch, cold, failure_site
):
    coordinator, candidate, _, _ = _prepare_case(
        "openai_chat_completions",
        _skill_source(),
        cold=cold,
        budget=64_000,
    )
    measurements = []
    discard_counts = []
    freeze = coordinator._model.freeze_wire_measurement

    def capture(**kwargs):
        measurement = freeze(**kwargs)
        index = len(measurements)
        measurements.append(measurement)
        discard_counts.append(0)
        discard = measurement.discard_materialization_to_quote

        def count_discard():
            discard_counts[index] += 1
            return discard()

        monkeypatch.setattr(
            measurement, "discard_materialization_to_quote", count_discard
        )
        return measurement

    def fail(*_args, **_kwargs):
        raise LookupError("injected catalog selection failure")

    monkeypatch.setattr(coordinator._model, "freeze_wire_measurement", capture)
    method = (
        "next_wire_render_floors"
        if failure_site == "floor-selection"
        else "compile_new_epoch"
        if cold
        else "compile_installed_append"
    )
    monkeypatch.setattr(coordinator._compiler, method, fail)

    # A second discard would replace this original exception with RuntimeError.
    with pytest.raises(LookupError, match="injected catalog selection failure"):
        _measure(coordinator, candidate)

    assert discard_counts == [1]
    with pytest.raises(RuntimeError, match="already consumed"):
        measurements[0].prepare_executable_plan()


@pytest.mark.postgres
def test_compaction_installs_the_actual_measured_hook_sibling_winner(
    stage2_migrated_postgres_database,
):
    """The real compaction owner must adopt the candidate returned by measurement."""
    from pulsara_agent.conversation_kernel.compaction.contracts import (
        CompactionDisposition, ResolvedCompactionPolicy,
    )
    from pulsara_agent.conversation_kernel.compaction.runtime import HostCompactionRuntimeOwner
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
    from pulsara_agent.identity import random_id
    from tests.support.model_config import frozen_test_prompt, test_model_resolution_snapshot
    from tests.support.postgres import verified_postgres_provider
    from tests.support.round3 import StructuredToolPort
    from tests.test_stage2_conversation_runner import (
        _AssertingTool, _LimitedCompactionScriptedModel,
        _PostCompactionHookContextCollector, _acquire_bound_host_writer,
        _record_automatic_compaction, _text_stream,
    )

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, workspace_id = random_id("session"), random_id("workspace")
    lease = _acquire_bound_host_writer(
        repository, session_id=session_id, workspace_id=workspace_id,
        writer_owner_id=random_id("host"), lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    collector = _PostCompactionHookContextCollector("Hook payload\n" + "\n" * 110_000)
    model = _LimitedCompactionScriptedModel(
        [_text_stream("history " + "x" * 80_000), _text_stream("final answer")],
        "A concise handoff after compaction.",
    )
    owner = HostCompactionRuntimeOwner(policy=ResolvedCompactionPolicy(
        automatic_enabled=False, auto_trigger_ratio=0.05,
        post_compaction_target_ratio=0.04, minimum_reclaim_tokens=1,
    ))
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository, writer_lease=lease, model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(), context_source_collector=collector,
        compaction_owner=owner, workspace_id=workspace_id,
    )
    dispatch = runner._provider_dispatch
    prepare, measure, bind = (dispatch.prepare_hook_context_sibling,
                              dispatch.measure_prepared_wire_candidate,
                              dispatch.bind_selected_provider_dispatch)
    observed = {}

    async def record_prepare(*args, **kwargs):
        sibling = await prepare(*args, **kwargs)
        if sibling is not None:
            observed["sibling"] = sibling
            observed["original"] = sibling.candidate
        return sibling

    async def record_measure(candidate, **kwargs):
        decision = await measure(candidate, **kwargs)
        if candidate is observed.get("original"):
            observed["decision"] = decision
        return decision

    def record_bind(*, base, sibling):
        decision = observed["decision"]
        assert sibling is observed["sibling"]
        assert decision.candidate is not observed["original"]
        assert sibling.candidate is decision.candidate
        selected = bind(base=base, sibling=sibling)
        assert selected.append_result is decision.candidate.append_result
        observed["selected"] = selected
        return selected

    async def exercise():
        try:
            await runner.run_turn(frozen_test_prompt("first question"))
            dispatch.prepare_hook_context_sibling = record_prepare
            dispatch.measure_prepared_wire_candidate = record_measure
            dispatch.bind_selected_provider_dispatch = record_bind
            outcomes = _record_automatic_compaction(runner)
            result = await runner.run_turn(frozen_test_prompt("second question"))
            return result, outcomes[-1]
        finally:
            await owner.aclose()

    result, outcome = asyncio.run(exercise())
    assert result.final_text == "final answer"
    assert outcome.disposition is CompactionDisposition.COMPACTED
    assert observed["decision"].wire_input_plan is not None
    assert _mode(observed["original"], ContextSourceKind.HOOK_CONTEXT) is ContextRenderMode.FULL
    assert _mode(observed["decision"].candidate, ContextSourceKind.HOOK_CONTEXT) is not ContextRenderMode.FULL
    actual = model.requests[-1].wire_input_plan.materialization
    measured = observed["decision"].wire_input_plan.materialization
    assert actual.root_policy_value == measured.root_policy_value
    assert actual.tool_items == measured.tool_items
    assert actual.ordered_input_items == measured.ordered_input_items
    assert model.requests[-1].compiled_input.messages == observed["decision"].candidate.semantic_input.messages
    assert tuple(item.retire_calls for item in collector.reservations) == (1,)
