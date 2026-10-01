"""Real compiler/adapter selection with frozen reads; no database or provider calls."""

from __future__ import annotations

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
            identity.scope_subagent_task_id,
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
                location=f"/test/skills/{index}/SKILL.md",
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
    catalog = build_catalog_snapshot(owner_epoch=1, catalog_revision=1, entries=servers)
    return _candidate(
        ContextSourceKind.MCP_CATALOG,
        _render_mcp_catalog(catalog, SimpleNamespace(routes=())),
    )


def _mixed_results():
    encoded = BytesIO()
    Image.new("RGB", (3_500, 3_500), (11, 23, 41)).save(encoded, "PNG")
    image = LLMImagePart("image/png", encoded.getvalue(), 3_500, 3_500)
    arguments = (
        freeze_json({"action": "SET_PLUGIN_ENABLED"}),
        freeze_json({"path": "plot.png"}),
    )
    calls = tuple(
        ProviderToolCall(f"call:{index}", name, args)
        for index, name, args in zip(
            (0, 1), ("manage_capability", "view_image"), arguments, strict=True
        )
    )
    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        "entry:assistant",
        2,
        "turn:test",
        (),
        tool_calls=calls,
    )
    content = FrozenPromptContent((LLMTextPart("Image loaded."), image))
    public_text = frozen_tool_result_public_text(content)
    results = tuple(
        replace(
            _tool_result(text, sequence=index + 3, turn_id="turn:test", artifact=False),
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
        "session:test", snapshot.identity.conversation_scope_kind, None
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
    coordinator, candidate, view, image = _prepare_case(
        api,
        _skill_source(),
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
    assert original.final_estimate.total_input_tokens < 100_000
    assert raw.quote.final_wire_estimated_input_tokens > 100_000
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
    assert decision.quote.final_wire_estimated_input_tokens <= 100_000
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
@pytest.mark.parametrize(
    "budget,expected",
    ((12_000, ContextRenderMode.COMPACT), (4_000, ContextRenderMode.REF_ONLY)),
)
def test_mcp_catalog_follows_existing_full_compact_reference_variants(
    api, budget, expected
):
    coordinator, candidate, _, _ = _prepare_case(
        api, _mcp_source(), cold=True, budget=budget
    )

    decision = _measure(coordinator, candidate)

    assert decision.wire_input_plan is not None
    assert _mode(decision.candidate, ContextSourceKind.MCP_CATALOG) is expected
    assert decision.quote.final_wire_estimated_input_tokens <= budget


@pytest.mark.parametrize("api", _WIRE_APIS)
def test_smallest_catalog_cannot_hide_mandatory_final_wire_exhaustion(api):
    coordinator, candidate, _, _ = _prepare_case(
        api,
        _skill_source(),
        cold=True,
        budget=100_000,
        user_text="u" * 220_000,
    )

    decision = _measure(coordinator, candidate)

    assert decision.wire_input_plan is None
    assert decision.quote.final_wire_estimated_input_tokens > 100_000
    _assert_typed_budget_rejection(coordinator, decision)
    assert (
        _mode(decision.candidate, ContextSourceKind.SKILL_CATALOG)
        is ContextRenderMode.UNAVAILABLE_MINIMAL
    )


@pytest.mark.parametrize("api", _WIRE_APIS)
@pytest.mark.parametrize("user_bytes,admitted", ((24_000, True), (30_000, False)))
def test_mcp_remeasures_each_legal_variant_and_rejects_if_reference_cannot_fit(
    api, user_bytes, admitted
):
    coordinator, candidate, _, _ = _prepare_case(
        api,
        _mcp_source(),
        cold=True,
        budget=16_000,
        user_text="u" * user_bytes,
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
    assert observed_modes[-1] is ContextRenderMode.REF_ONLY
    if admitted:
        assert observed_modes == [
            ContextRenderMode.FULL,
            ContextRenderMode.COMPACT,
            ContextRenderMode.REF_ONLY,
        ]
    else:
        assert decision.quote.final_wire_estimated_input_tokens > 16_000
        _assert_typed_budget_rejection(coordinator, decision)


def test_pending_root_hook_binds_the_measured_catalog_winner():
    original = object()
    winner = object()
    expected = object()
    sibling = SimpleNamespace(candidate=original, owns_reservation=False)
    prepared = SimpleNamespace(
        admission=SimpleNamespace(candidate=SimpleNamespace(exact_turn_id="turn:test")),
        owns_resources=False,
    )

    async def prepare_hook(base, **_kwargs):
        assert base is prepared
        return sibling

    async def measure(candidate, **_kwargs):
        assert candidate is original
        return SimpleNamespace(candidate=winner, wire_input_plan=object())

    def bind(*, base, sibling, decision):
        assert base is prepared
        assert sibling.candidate is decision.candidate
        return expected

    runner = object.__new__(ConversationKernelRunner)
    runner._session_start_boundary = SimpleNamespace(has_pending=False)
    runner._provider_dispatch = SimpleNamespace(
        prepare_hook_context_sibling=prepare_hook,
        measure_prepared_wire_candidate=measure,
        bind_selected_prospective_root_dispatch=bind,
    )
    runner._planning_deadline = lambda: monotonic() + 30
    intent = SimpleNamespace(require_exact=lambda **_kwargs: None)

    assert (
        asyncio.run(runner._attach_pending_root_hook_context(prepared, intent))
        is expected
    )


@pytest.mark.parametrize("api", _WIRE_APIS)
@pytest.mark.parametrize("cold", (False, True))
def test_catalog_wire_retry_preserves_already_omitted_optional_source(api, cold):
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
    assert not original.included
    assert original.selected_mode is None
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
    "field,value", (("context_id", "context:other"), ("model_call_index", 2))
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
