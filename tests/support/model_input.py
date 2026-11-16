"""Current canonical and owner-authentic provider-input fixtures."""

from __future__ import annotations
from pulsara_agent.identity import derive_id
from dataclasses import fields, replace
from datetime import datetime, timedelta, tzinfo
import json
from pathlib import Path
from types import SimpleNamespace
from pulsara_agent.capability.provider import SkillProjectionOutput
from pulsara_agent.capability.contracts import (
    CapabilityKind,
    CapabilitySourceKind,
    CapabilitySourceRefreshMode,
    CapabilitySourceSnapshotDisposition,
    EmptyCapabilityEpochPredecessor,
    FrozenMcpCapabilityProjectionInput,
    FrozenSkillProjectionInput,
    ToolCapabilityOrigin,
    capability_identity,
    capability_source_ref,
    capability_source_registration,
    freeze_capability_source_snapshot,
    freeze_tool_capability_fact,
)
from pulsara_agent.capability.planner import KernelToolCapabilityPlanner
from pulsara_agent.capability.registry import (
    freeze_capability_dispatch_cut_and_views,
    freeze_capability_registration_set,
    freeze_capability_registry_snapshot,
    freeze_tool_planning_input,
)
from pulsara_agent.capability.local_skills import LooseSkillDefinitionProducer
from pulsara_agent.capability.resolver import (
    CompleteEffectiveSkillCatalogInspection,
)
from pulsara_agent.conversation_kernel.context_sources import (
    KernelContextSourceCollector,
)
from pulsara_agent.conversation_kernel.capability_composition import (
    issue_skill_catalog_source_snapshot,
)
from pulsara_agent.conversation_kernel.direct_model import (
    DirectKernelModelPort,
    KernelModelPreparationRequest,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
)
from pulsara_agent.conversation_kernel.provider_dispatch import (
    _semantic_projection_from_compiled,
    prepared_append_candidate,
)
from pulsara_agent.llm.input import (
    LLMTextPart,
    join_text_content,
)
from pulsara_agent.llm.adapters.openai.function_tools import (
    freeze_openai_native_tool_eligibility,
    materialize_openai_native_tool_projection_set,
)
from pulsara_agent.llm.provider import RouteWireProfile
from pulsara_agent.llm.provider_replay import (
    ProviderAssistantReplayFragment,
    provider_replay_id,
)
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.contracts import (
    ApprovedPlanMaterializationFact,
    AcceptedAssistantDisposition,
    CapabilityActivationSubjectKind,
    CanonicalInputOriginKind,
    CanonicalModelInputIdentity,
    CanonicalModelInputSnapshot,
    CollectedContextSources,
    ContextBudgetClass,
    ContextChannel,
    ContextSourceCandidate,
    ContextSourceKind,
    ContextBindingBaseKind,
    FrozenContextBindingCompileFact,
    FrozenCanonicalCompileSnapshot,
    FrozenModelToolSurface,
    FrozenPlanHandoffCompileFact,
    FrozenPreviousTurnOutcomeCompileFact,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    PreviousTurnOutcomeKind,
    ModelInputTokenEstimator,
    ProviderToolResultContextMetadata,
    StructuredModelInputCompileRequest,
    canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint,
    provider_input_item_text,
    canonical_compile_snapshot_fingerprint,
    context_binding_compile_fact_fingerprint,
    approved_plan_materialization_fingerprint,
    plan_handoff_compile_fact_fingerprint,
    previous_turn_outcome_fingerprint,
    provider_input_item_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    FULL_HISTORY_CONTEXT_BASE_IDENTITY,
    NewTriggerAnchor,
    ProcessLocalCanonicalFrontier,
    ProviderInputContinuityScope,
)
from pulsara_agent.conversation_kernel.cold_epoch import CanonicalColdContinuationSeed
from pulsara_agent.model_input.provider_replay import (
    FrozenDurableProviderReplayManifestCut,
    freeze_provider_replay_manifest,
    freeze_provider_replay_manifest_cut,
    freeze_selected_provider_replay_hydration,
    select_compatible_provider_replay_manifests,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import (
    PlanHandoffKind,
    PlanWorkflowStatus,
    extract_plan_draft,
)
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from pulsara_agent.primitives.tool_observation import (
    FrozenToolObservationTimingFact,
    ToolObservationDurationDisposition,
    ToolObservationOrigin,
    tool_observation_timing_fingerprint,
)
from tests.support.model_config import test_model_binding, test_model_runtime
from tests.support.round3 import (
    StructuredToolPort,
    prepare_test_model_call,
)
from pulsara_agent.model_input.continuity import (
    RuntimeObservationItem,
    encode_runtime_observation,
)

_PREPARED_MODEL_CALLS = {}


def _test_round9_capability_views(
    surface: FrozenModelToolSurface,
    *,
    skill_projection: FrozenSkillProjectionInput,
    wire_api: str = "openai_chat_completions",
):
    builtin_source = capability_source_ref(
        CapabilitySourceKind.BUILTIN_REGISTRY, "test-round3-builtins"
    )
    builtin_registration = capability_source_registration(
        source=builtin_source,
        refresh_mode=CapabilitySourceRefreshMode.IMMUTABLE,
        source_contract_fingerprint=context_fingerprint(
            "test:round3-builtin-contract:v1", "sealed"
        ),
    )
    builtin_facts = tuple(
        freeze_tool_capability_fact(
            identity=capability_identity(
                kind=CapabilityKind.TOOL,
                source=builtin_source,
                stable_name=spec.name,
            ),
            origin=ToolCapabilityOrigin.BUILTIN,
            canonical_tool_spec=spec,
        )
        for spec in surface.tool_specs
    )
    builtin_snapshot = freeze_capability_source_snapshot(
        registration=builtin_registration,
        conversation_scope_kind=surface.conversation_scope_kind,
        disposition=CapabilitySourceSnapshotDisposition.COMPLETE,
        facts=builtin_facts,
    )
    skill_registration = skill_projection.source_snapshot.registration
    registration_set = freeze_capability_registration_set(
        conversation_scope_kind=surface.conversation_scope_kind,
        builtin_registration=builtin_registration,
        mcp_registrations=(),
        local_skill_catalog_registration=skill_registration,
    )
    registry = freeze_capability_registry_snapshot(
        registration_set=registration_set,
        source_snapshots=(builtin_snapshot, skill_projection.source_snapshot),
    )
    native = freeze_openai_native_tool_eligibility(
        conversation_scope_kind=surface.conversation_scope_kind,
        wire_api=wire_api,
        tool_facts=registry.tool_facts,
    )
    tool_input = freeze_tool_planning_input(
        predecessor=EmptyCapabilityEpochPredecessor(0),
        native_wire=native,
        mcp=FrozenMcpCapabilityProjectionInput(
            conversation_scope_kind=surface.conversation_scope_kind,
            source_snapshots=(),
        ),
    )
    _parent, tool_view, skill_view = freeze_capability_dispatch_cut_and_views(
        conversation_scope_kind=surface.conversation_scope_kind,
        registry=registry,
        tools=tool_input,
        skills=skill_projection,
    )
    planner = KernelToolCapabilityPlanner()
    selection = planner.select(view=tool_view)
    projection_set = materialize_openai_native_tool_projection_set(
        conversation_scope_kind=surface.conversation_scope_kind,
        wire_api=wire_api,
        tool_versions=selection.direct_tool_versions,
        tool_specs=selection.direct_tool_surface.tool_specs,
        eligibility=native,
    )
    plan = planner.finalize(
        selection=selection,
        direct_projection_set=projection_set,
    )
    assert plan.direct_tool_surface == surface
    return plan, skill_view


def _collect_context_sources(
    collector: KernelContextSourceCollector,
    *,
    activation_subject: CapabilityActivationSubjectKind | None,
    activation_text: str,
    tool_surface: FrozenModelToolSurface,
    canonical_facts: FrozenCanonicalCompileSnapshot,
) -> CollectedContextSources:
    owner = collector.freeze_skill_capability_source_snapshot(
        conversation_scope_kind=tool_surface.conversation_scope_kind,
    )
    projection = collector.freeze_skill_capability_projection_input(owner)
    tool_plan, skill_view = _test_round9_capability_views(
        tool_surface,
        skill_projection=projection,
    )
    return collector.collect(
        activation_subject=activation_subject,
        activation_text=activation_text,
        skill_reference_surfaces=(activation_text,),
        tool_surface=tool_surface,
        canonical_facts=canonical_facts,
        tool_exposure_plan=tool_plan,
        skill_dispatch_view=skill_view,
        skill_owner_snapshot=owner,
        new_context=True,
    )


def _candidate(
    kind: ContextSourceKind,
    text: str,
    *,
    channel=None,
    placement=10,
    budget=ContextBudgetClass.MUST_KEEP,
):
    if not isinstance(text, str):
        raise TypeError("source fixture requires its one final string")
    if kind is ContextSourceKind.BASE_SYSTEM:
        return ContextSourceCandidate(
            kind, channel or ContextChannel.SYSTEM, text, 0, budget
        )
    if kind in {
        ContextSourceKind.SKILL_CATALOG,
        ContextSourceKind.COMPACTION_RUNTIME_HANDOFF,
    }:
        return ContextSourceCandidate(
            kind, channel or ContextChannel.CONTEXT_MESSAGE, text, placement, budget
        )
    message = encode_runtime_observation(
        source_kind=kind, body=(RuntimeObservationItem(text),)
    )
    return ContextSourceCandidate(
        kind,
        channel or ContextChannel.RUNTIME_OBSERVATION,
        join_text_content(message.content),
        placement,
        budget,
    )


def _sources(
    *candidates: ContextSourceCandidate, hook_context=None, observation_state=None
):
    if not any(
        item.source_kind is ContextSourceKind.BASE_SYSTEM for item in candidates
    ):
        candidates = (_candidate(ContextSourceKind.BASE_SYSTEM, "BASE"), *candidates)
    return CollectedContextSources(
        tuple(candidates),
        hook_context=hook_context,
        observation_state=observation_state,
    )


def _snapshot(
    *items: FrozenProviderInputItem,
    scope: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    turn_id: str = "turn:klmmazz67vfgsfsvg6ceqgdsfu",
    canonical_expanded_bytes: int | None = None,
) -> CanonicalModelInputSnapshot:
    initial_entry_id = next(
        (
            item.source_entry_id
            for item in items
            if item.source_turn_id == turn_id and item.source_entry_id is not None
        ),
        "entry:x6eevt4jzgsks3d5pdcognxexm",
    )
    identity = CanonicalModelInputIdentity(
        session_id="session:336wrfwkamrr3up6kjtsgvgcte",
        turn_id=turn_id,
        initial_entry_id=initial_entry_id,
        context_binding_revision_id="binding:rpcv6lnowmsx57p73m43mjmbdm",
        provider_input_through_sequence=max(
            (item.source_entry_sequence or 0 for item in items), default=0
        ),
        conversation_scope_kind=scope,
        identity_fingerprint=canonical_model_input_identity_fingerprint(
            session_id="session:336wrfwkamrr3up6kjtsgvgcte",
            turn_id=turn_id,
            initial_entry_id=initial_entry_id,
            context_binding_revision_id="binding:rpcv6lnowmsx57p73m43mjmbdm",
            provider_input_through_sequence=max(
                (item.source_entry_sequence or 0 for item in items), default=0
            ),
            conversation_scope_kind=scope,
        ),
    )
    logical_bytes = (
        sum(len(provider_input_item_text(item).encode("utf-8")) for item in items)
        if canonical_expanded_bytes is None
        else canonical_expanded_bytes
    )
    fingerprint = canonical_model_input_snapshot_fingerprint(
        identity=identity,
        items=tuple(items),
        canonical_expanded_bytes=logical_bytes,
        closures=(),
        late_outcomes=(),
    )
    return CanonicalModelInputSnapshot(
        identity=identity,
        items=tuple(items),
        canonical_expanded_bytes=logical_bytes,
        snapshot_fingerprint=fingerprint,
    )


def _user(
    text: str,
    *,
    sequence: int = 1,
    turn_id: str = "turn:klmmazz67vfgsfsvg6ceqgdsfu",
    origin: CanonicalInputOriginKind = CanonicalInputOriginKind.HUMAN_MESSAGE,
):
    return FrozenProviderInputItem(
        FrozenProviderInputItemKind.USER,
        derive_id("entry", "test-fixture", f"entry:{sequence}"),
        sequence,
        turn_id,
        (LLMTextPart(text),),
        input_origin=origin,
    )


def _tool_result(body: str, *, sequence: int, turn_id: str):
    return FrozenProviderInputItem(
        FrozenProviderInputItemKind.TOOL_RESULT,
        derive_id("entry", "test-fixture", f"entry:{sequence}"),
        sequence,
        turn_id,
        (LLMTextPart(body),),
        tool_call_id=f"call:{sequence}",
        tool_request_entry_id=derive_id(
            "entry", "test-fixture", f"entry:request:{sequence}"
        ),
        tool_result_context=ProviderToolResultContextMetadata(
            session_id="session:336wrfwkamrr3up6kjtsgvgcte",
            result_id=derive_id("result", "test-fixture", f"result:{sequence}"),
            result_entry_id=derive_id("entry", "test-fixture", f"entry:{sequence}"),
            result_state="SUCCESS",
            output_unavailability_reason=None,
            timing=_tool_timing(turn_id),
        ),
        tool_result_body_text=body,
    )


def _tool_timing(turn_id: str) -> FrozenToolObservationTimingFact:
    values = {
        "source_turn_ref": turn_id,
        "observed_at_utc": "2026-08-14T03:04:05.123456Z",
        "observation_duration_microseconds": 2_123_456,
        "duration_disposition": ToolObservationDurationDisposition.MEASURED,
        "tool_reported_duration_microseconds": None,
        "observation_origin": ToolObservationOrigin.BUILTIN,
    }
    provisional = FrozenToolObservationTimingFact.__new__(
        FrozenToolObservationTimingFact
    )
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "fact_fingerprint", "")
    return FrozenToolObservationTimingFact(
        **values,
        fact_fingerprint=tool_observation_timing_fingerprint(provisional),
    )


def _prepared_request(
    snapshot: CanonicalModelInputSnapshot,
    sources: CollectedContextSources,
    *,
    budget: int = 100_000,
    tool_names: tuple[str, ...] = (),
    canonical_facts: FrozenCanonicalCompileSnapshot | None = None,
    route_wire_profile: RouteWireProfile | None = None,
) -> StructuredModelInputCompileRequest:
    tools = StructuredToolPort(object(), tool_names=tool_names)
    prepared_surface = tools.snapshot_tool_surface(
        conversation_scope_kind=snapshot.identity.conversation_scope_kind,
    )
    skill_source = capability_source_ref(
        CapabilitySourceKind.LOCAL_SKILL_CATALOG,
        "test-round3-request-skills",
    )
    skill_registration = capability_source_registration(
        source=skill_source,
        refresh_mode=CapabilitySourceRefreshMode.SAFE_POINT_REFRESHABLE,
        source_contract_fingerprint=context_fingerprint(
            "test:round3-request-skill-contract:v1", "empty"
        ),
    )
    skill_snapshot = freeze_capability_source_snapshot(
        registration=skill_registration,
        conversation_scope_kind=snapshot.identity.conversation_scope_kind,
        disposition=CapabilitySourceSnapshotDisposition.COMPLETE,
        facts=(),
    )
    skill_root_policy = LooseSkillDefinitionProducer(
        user_product_skills_root=Path.cwd() / ".test-r3-user-product",
        user_agents_skills_root=Path.cwd() / ".test-r3-user-agents",
    ).prepare_root_policy(Path.cwd())
    skill_inspection = CompleteEffectiveSkillCatalogInspection(
        root_policy=skill_root_policy,
        winners=(),
        readable_skills=(),
        candidate_issues=(),
    )
    tool_plan, _skill_view = _test_round9_capability_views(
        prepared_surface.model_surface,
        skill_projection=FrozenSkillProjectionInput(
            source_snapshot=skill_snapshot,
            inspection=skill_inspection,
        ),
        wire_api=(
            "openai_chat_completions"
            if route_wire_profile is None
            else route_wire_profile.wire_api
        ),
    )
    prepared_surface = replace(
        prepared_surface,
        capability_exposure_plan=tool_plan,
    )
    model_runtime = test_model_runtime(
        api_key="sk-fixture-secret",
        base_url="https://example.invalid/v1",
        model_id="test-pro",
        wire_api=(
            "openai_chat_completions"
            if route_wire_profile is None
            else route_wire_profile.wire_api
        ),
        route_wire_profile=route_wire_profile,
    )
    model = DirectKernelModelPort(model_runtime=model_runtime)
    prepared = prepare_test_model_call(
        model,
        KernelModelPreparationRequest(
            session_id=snapshot.identity.session_id,
            turn_id=snapshot.identity.turn_id,
            model_call_index=1,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
            maximum_input_tokens=max(budget, 1),
            binding=test_model_binding(model_runtime),
            tool_surface=prepared_surface,
        ),
    )
    binding = prepared.compile_binding
    _PREPARED_MODEL_CALLS[binding.call_fact.resolved_model_call_id] = (model, prepared)
    canonical_facts = canonical_facts or _canonical_facts(snapshot)
    return StructuredModelInputCompileRequest(
        context_id="context:maoywbssk6zspli7b2bnu3pala",
        model_call_index=1,
        canonical_input=snapshot,
        canonical_facts=canonical_facts,
        compile_binding=binding,
        sources=sources,
    )


def _canonical_facts(
    snapshot: CanonicalModelInputSnapshot | None = None,
) -> FrozenCanonicalCompileSnapshot:
    canonical = snapshot or _snapshot()
    binding = _context_binding_fact(canonical)
    permission = build_run_permission_snapshot(
        snapshot_id="permission:53mvfpeaityebpvslz3hltgfuy",
        requested_mode=PermissionMode.BYPASS_PERMISSIONS,
        effective_mode=PermissionMode.BYPASS_PERMISSIONS,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
    )
    provisional = FrozenCanonicalCompileSnapshot.__new__(FrozenCanonicalCompileSnapshot)
    object.__setattr__(provisional, "canonical_input", canonical)
    object.__setattr__(provisional, "plan_mode_original_input", None)
    object.__setattr__(provisional, "context_binding_fact", binding)
    object.__setattr__(provisional, "run_permission_snapshot", permission)
    object.__setattr__(provisional, "plan_workflow_fact", None)
    object.__setattr__(provisional, "plan_handoff_fact", None)
    object.__setattr__(provisional, "approved_plan_materialization_fact", None)
    object.__setattr__(provisional, "previous_turn_outcome_fact", None)
    object.__setattr__(provisional, "canonical_read_cut_fingerprint", "")
    return FrozenCanonicalCompileSnapshot(
        canonical_input=canonical,
        context_binding_fact=binding,
        run_permission_snapshot=permission,
        plan_workflow_fact=None,
        plan_handoff_fact=None,
        approved_plan_materialization_fact=None,
        previous_turn_outcome_fact=None,
        canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
            provisional
        ),
    )


def _context_binding_fact(
    snapshot: CanonicalModelInputSnapshot,
) -> FrozenContextBindingCompileFact:
    values = {
        "binding_revision_id": snapshot.identity.context_binding_revision_id,
        "revision_ordinal": 0,
        "base_kind": ContextBindingBaseKind.FULL_HISTORY,
        "context_snapshot_id": None,
        "source_through_sequence": 0,
        "context_base_semantic_identity": FULL_HISTORY_CONTEXT_BASE_IDENTITY,
    }
    provisional = FrozenContextBindingCompileFact.__new__(
        FrozenContextBindingCompileFact
    )
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "fact_fingerprint", "")
    return FrozenContextBindingCompileFact(
        **values,
        fact_fingerprint=context_binding_compile_fact_fingerprint(provisional),
    )


def _append_frontier(
    request: StructuredModelInputCompileRequest,
) -> ProcessLocalCanonicalFrontier:
    snapshot = request.canonical_input
    return ProcessLocalCanonicalFrontier(
        latest_context_binding_revision_id=(
            request.canonical_facts.context_binding_fact.binding_revision_id
        ),
        context_base_semantic_identity=(
            request.canonical_facts.context_binding_fact.context_base_semantic_identity
        ),
        through_sequence=snapshot.identity.provider_input_through_sequence,
        ordered_item_fingerprints=tuple(
            provider_input_item_fingerprint(item) for item in snapshot.items
        ),
    )


def _append_anchor(
    request: StructuredModelInputCompileRequest,
) -> NewTriggerAnchor:
    item = request.canonical_input.items[-1]
    fingerprint = provider_input_item_fingerprint(item)
    return NewTriggerAnchor(
        source_entry_id=item.source_entry_id or "",
        provider_input_item_fingerprint=fingerprint,
        provider_group_boundary_fingerprint=context_fingerprint(
            "pulsara:provider-group-boundary:v1",
            {
                "entry_id": item.source_entry_id,
                "item": fingerprint,
                "sequence": item.source_entry_sequence,
            },
        ),
    )


def _prepare_append_candidate(
    *,
    compiler: StructuredModelInputCompiler,
    owner: HostProviderInputContinuityOwner,
    request: StructuredModelInputCompileRequest,
    dispatch_anchor=None,
    replay_manifest_cut: FrozenDurableProviderReplayManifestCut | None = None,
    replay_fragments: tuple[ProviderAssistantReplayFragment, ...] = (),
):
    scope = ProviderInputContinuityScope(
        session_id=request.canonical_input.identity.session_id,
        scope_kind=request.canonical_input.identity.conversation_scope_kind,
    )
    planning = owner.freeze_planning_input(
        scope=scope,
        canonical_frontier=_append_frontier(request),
        dispatch_anchor=(
            _append_anchor(request) if dispatch_anchor is None else dispatch_anchor
        ),
    )
    result = (
        compiler.compile_new_epoch(request, planning=planning)
        if planning.predecessor_view is None
        else compiler.compile_installed_append(request, planning=planning)
    )
    model, prepared_call = _PREPARED_MODEL_CALLS[
        request.compile_binding.call_fact.resolved_model_call_id
    ]
    replay_hydration = None
    if replay_manifest_cut is not None:
        replay_target = DirectKernelModelPort.replay_target(prepared_call)
        selected, placements = select_compatible_provider_replay_manifests(
            manifest_cut=replay_manifest_cut,
            compiled_input=result.compiled_input,
            replay_target=replay_target,
        )
        replay_hydration = freeze_selected_provider_replay_hydration(
            manifest_cut=replay_manifest_cut,
            compiled_input=result.compiled_input,
            replay_target=replay_target,
            selected_manifests=selected,
            selected_placements=placements,
            fragments=replay_fragments,
        )
    wire_input_plan = model.plan_wire_input(
        prepared_call=prepared_call,
        compiled_input=result.compiled_input,
        predecessor_view=planning.predecessor_view,
        replay_hydration=replay_hydration,
    )
    tool_exposure_plan = prepared_call.tool_surface.capability_exposure_plan
    assert tool_exposure_plan is not None
    preparation_basis = SimpleNamespace(
        call_target=prepared_call.epoch_call_target,
        canonical_read=SimpleNamespace(),
        capability_dispatch_cut=SimpleNamespace(),
    )
    transition = owner.issue_transition(
        planning=planning,
        preparation_basis=preparation_basis,
        call_target=prepared_call.epoch_call_target,
        seed=(
            CanonicalColdContinuationSeed(dispatch_read=SimpleNamespace())
            if planning.predecessor_view is None
            else None
        ),
        semantic_projection=_semantic_projection_from_compiled(result.compiled_input),
        wire_input_plan=wire_input_plan,
    )
    candidate = prepared_append_candidate(
        planning=planning,
        transition=transition,
        call_target=prepared_call.epoch_call_target,
        compiled_result=result,
        wire_input_plan=wire_input_plan,
        tool_exposure_plan=tool_exposure_plan,
        preparation_basis=preparation_basis,
    )
    return result, candidate


def _compile_and_install_append(
    *,
    compiler: StructuredModelInputCompiler,
    owner: HostProviderInputContinuityOwner,
    request: StructuredModelInputCompileRequest,
    dispatch_anchor=None,
    replay_manifest_cut: FrozenDurableProviderReplayManifestCut | None = None,
    replay_fragments: tuple[ProviderAssistantReplayFragment, ...] = (),
):
    result, candidate = _prepare_append_candidate(
        compiler=compiler,
        owner=owner,
        request=request,
        dispatch_anchor=dispatch_anchor,
        replay_manifest_cut=replay_manifest_cut,
        replay_fragments=replay_fragments,
    )
    owner.register(candidate)
    owner.install(candidate=candidate, execution=object())
    view = owner.current_view(candidate.scope)
    assert view is not None
    return result, view


def _replay_manifest_cut(
    snapshot: CanonicalModelInputSnapshot,
    fragment: ProviderAssistantReplayFragment,
    *,
    wire_api: str,
) -> FrozenDurableProviderReplayManifestCut:
    replay_id = provider_replay_id(
        session_id=snapshot.identity.session_id,
        assistant_entry_id=fragment.assistant_entry_id,
        wire_api=wire_api,
    )
    manifest = freeze_provider_replay_manifest(
        replay_id=replay_id,
        assistant_entry_id=fragment.assistant_entry_id,
        wire_api=wire_api,
        codec_kind=fragment.codec_kind.value,
        provider_replay_contract_fingerprint=(
            fragment.provider_replay_contract_fingerprint
        ),
        replay_target_fingerprint=fragment.replay_target_fingerprint,
        public_projection_fingerprint=fragment.public_projection_fingerprint,
        payload_digest=fragment.payload_digest,
        payload_size=fragment.payload_size,
        item_count=fragment.item_count,
        fragment_fingerprint=fragment.fragment_fingerprint,
    )
    scope = ProviderInputContinuityScope(
        session_id=snapshot.identity.session_id,
        scope_kind=snapshot.identity.conversation_scope_kind,
    )
    return freeze_provider_replay_manifest_cut(
        session_id=snapshot.identity.session_id,
        scope=scope,
        context_binding_revision_id=(snapshot.identity.context_binding_revision_id),
        provider_input_through_sequence=(
            snapshot.identity.provider_input_through_sequence
        ),
        manifests=(manifest,),
    )


def _permission_snapshot():
    return build_run_permission_snapshot(
        snapshot_id="permission:53mvfpeaityebpvslz3hltgfuy",
        requested_mode=PermissionMode.BYPASS_PERMISSIONS,
        effective_mode=PermissionMode.BYPASS_PERMISSIONS,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
    )


def _round7_previous_fact(
    facts: FrozenCanonicalCompileSnapshot,
) -> FrozenPreviousTurnOutcomeCompileFact:
    identity = facts.canonical_input.identity
    values = {
        "session_id": identity.session_id,
        "workspace_id": "workspace:wdl6b3mtrkghwnvjo4an3ekbau",
        "current_turn_id": identity.turn_id,
        "current_scope_kind": identity.conversation_scope_kind,
        "predecessor_turn_id": "turn:mcie4zq2f6b4g2mm2ogvtx2uwq",
        "predecessor_initial_entry_sequence": 1,
        "predecessor_terminal_at_utc": "2026-08-14T03:04:05.123456Z",
        "outcome_kind": PreviousTurnOutcomeKind.EXECUTION_FAILED,
        "accepted_assistant_disposition": (AcceptedAssistantDisposition.NONE_ACCEPTED),
        "accepted_assistant_entry_count": 0,
        "definitely_not_dispatched_tool_count": 0,
        "outcome_unknown_tool_count": 0,
        "bounded_tool_name_samples": (),
        "user_input_preserved": True,
        "canonical_entries_preserved": True,
        "terminal_reason": "FOREGROUND_EXECUTION_INTERRUPTED",
        "terminal_public_detail": None,
    }
    provisional = FrozenPreviousTurnOutcomeCompileFact.__new__(
        FrozenPreviousTurnOutcomeCompileFact
    )
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "fact_fingerprint", "")
    return FrozenPreviousTurnOutcomeCompileFact(
        **values,
        fact_fingerprint=previous_turn_outcome_fingerprint(provisional),
    )


def _with_round7_previous(
    facts: FrozenCanonicalCompileSnapshot,
    previous: FrozenPreviousTurnOutcomeCompileFact | None,
) -> FrozenCanonicalCompileSnapshot:
    values = {
        field.name: getattr(facts, field.name)
        for field in fields(FrozenCanonicalCompileSnapshot)
        if field.name != "canonical_read_cut_fingerprint"
    }
    values["previous_turn_outcome_fact"] = previous
    provisional = FrozenCanonicalCompileSnapshot.__new__(FrozenCanonicalCompileSnapshot)
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "canonical_read_cut_fingerprint", "")
    return FrozenCanonicalCompileSnapshot(
        **values,
        canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
            provisional
        ),
    )


def _approved_plan_compile_facts(
    *,
    workflow_id: str = "plan:ff5em4o3lofovbbj5jawnllhku",
    interaction_id: str = "interaction:draft",
    transition_digest: str = "sha256:" + "2" * 64,
) -> tuple[FrozenCanonicalCompileSnapshot, str]:
    plan = "PLAN_SENTINEL_EXACTLY_ONCE"
    arguments = freeze_json({"plan": plan})
    assert isinstance(arguments, FrozenJsonObjectFact)
    continuation = FrozenProviderInputItem(
        FrozenProviderInputItemKind.PLAN_CONTINUATION,
        "entry:ali6tyzqiwe3pcr33e2lleswoq",
        2,
        "turn:kd5kt7ezlfyprg4wjsc7fmkvem",
        (
            LLMTextPart(
                json.dumps(
                    {
                        "pulsara_plan_continuation": {
                            "status": "APPROVED",
                            "transition": "APPROVED_PLAN",
                        }
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        ),
        input_origin=CanonicalInputOriginKind.PLAN_CONTINUATION,
    )
    snapshot = _snapshot(continuation, turn_id="turn:kd5kt7ezlfyprg4wjsc7fmkvem")
    handoff_values = {
        "session_id": "session:336wrfwkamrr3up6kjtsgvgcte",
        "workspace_id": "workspace:wdl6b3mtrkghwnvjo4an3ekbau",
        "target_turn_id": "turn:kd5kt7ezlfyprg4wjsc7fmkvem",
        "carrier_entry_id": "entry:ali6tyzqiwe3pcr33e2lleswoq",
        "carrier_entry_sequence": 2,
        "workflow_id": workflow_id,
        "workflow_ordinal": 1,
        "workflow_revision_at_transition": 4,
        "interaction_id": interaction_id,
        "handoff_kind": PlanHandoffKind.APPROVED_PLAN,
        "workflow_status": PlanWorkflowStatus.APPROVED,
        "resume_permission_mode": PermissionMode.ACCEPT_EDITS,
        "transition_semantic_digest": transition_digest,
    }
    provisional_handoff = FrozenPlanHandoffCompileFact.__new__(
        FrozenPlanHandoffCompileFact
    )
    for name, value in handoff_values.items():
        object.__setattr__(provisional_handoff, name, value)
    object.__setattr__(provisional_handoff, "fact_fingerprint", "")
    handoff = FrozenPlanHandoffCompileFact(
        **handoff_values,
        fact_fingerprint=plan_handoff_compile_fact_fingerprint(provisional_handoff),
    )
    extracted = extract_plan_draft(
        interaction_id=interaction_id,
        assistant_entry_id="entry:iucvuktd36hivez7ibvwjqibn4",
        tool_call_id="call:exit",
        request_semantic_digest="sha256:" + "3" * 64,
        arguments=arguments,
    )
    approved_values = {
        "session_id": "session:336wrfwkamrr3up6kjtsgvgcte",
        "workspace_id": "workspace:wdl6b3mtrkghwnvjo4an3ekbau",
        "target_turn_id": "turn:kd5kt7ezlfyprg4wjsc7fmkvem",
        "workflow_id": workflow_id,
        "interaction_id": interaction_id,
        "assistant_entry_id": "entry:iucvuktd36hivez7ibvwjqibn4",
        "tool_call_id": "call:exit",
        "request_semantic_digest": "sha256:" + "3" * 64,
        "content_identity": extracted.identity,
        "exact_plan_utf8": extracted.exact_plan_utf8,
    }
    provisional_approved = ApprovedPlanMaterializationFact.__new__(
        ApprovedPlanMaterializationFact
    )
    for name, value in approved_values.items():
        object.__setattr__(provisional_approved, name, value)
    object.__setattr__(provisional_approved, "fact_fingerprint", "")
    approved = ApprovedPlanMaterializationFact(
        **approved_values,
        fact_fingerprint=approved_plan_materialization_fingerprint(
            provisional_approved
        ),
    )
    permission = build_run_permission_snapshot(
        snapshot_id="permission:cg5nthre3ofnd6pibptx4ahtoq",
        requested_mode=PermissionMode.ACCEPT_EDITS,
        effective_mode=PermissionMode.ACCEPT_EDITS,
        admission_source=RunPermissionAdmissionSource.RUNTIME_PLAN_CONTINUATION,
        inherited_from_turn_id="turn:qb63tb3eshpbkryzmq6wzhzt2u",
    )
    provisional = FrozenCanonicalCompileSnapshot.__new__(FrozenCanonicalCompileSnapshot)
    binding = _context_binding_fact(snapshot)
    object.__setattr__(provisional, "canonical_input", snapshot)
    object.__setattr__(provisional, "plan_mode_original_input", None)
    object.__setattr__(provisional, "context_binding_fact", binding)
    object.__setattr__(provisional, "run_permission_snapshot", permission)
    object.__setattr__(provisional, "plan_workflow_fact", None)
    object.__setattr__(provisional, "plan_handoff_fact", handoff)
    object.__setattr__(provisional, "approved_plan_materialization_fact", approved)
    object.__setattr__(provisional, "previous_turn_outcome_fact", None)
    object.__setattr__(provisional, "canonical_read_cut_fingerprint", "")
    facts = FrozenCanonicalCompileSnapshot(
        canonical_input=snapshot,
        context_binding_fact=binding,
        run_permission_snapshot=permission,
        plan_workflow_fact=None,
        plan_handoff_fact=handoff,
        approved_plan_materialization_fact=approved,
        previous_turn_outcome_fact=None,
        canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
            provisional
        ),
    )
    return facts, plan


class _CountingEstimator:
    def __init__(self, delegate: ModelInputTokenEstimator) -> None:
        self._delegate = delegate
        self.fact = delegate.fact
        self.calls = 0

    def __getattr__(self, name):
        self.calls += 1
        raise AssertionError(
            f"structural compiler must not ask its budget estimator for {name}"
        )


class _Capability:
    def __init__(self) -> None:
        self.inputs: list[tuple[tuple[str, ...], frozenset[str]]] = []
        self._owner_authenticity = object()

    def resolve_projection(self, *, user_input: str, available_tool_names):
        self.inputs.append((user_input, available_tool_names))
        return SkillProjectionOutput()

    def freeze_owner_snapshot(
        self,
        *,
        conversation_scope_kind,
        deadline_monotonic=None,
    ):
        del deadline_monotonic
        source = capability_source_ref(
            CapabilitySourceKind.LOCAL_SKILL_CATALOG,
            "test-round3-skill-catalog",
        )
        registration = capability_source_registration(
            source=source,
            refresh_mode=CapabilitySourceRefreshMode.SAFE_POINT_REFRESHABLE,
            source_contract_fingerprint=context_fingerprint(
                "test:round3-skill-source-contract:v1", "empty"
            ),
        )
        snapshot = freeze_capability_source_snapshot(
            registration=registration,
            conversation_scope_kind=conversation_scope_kind,
            disposition=CapabilitySourceSnapshotDisposition.COMPLETE,
            facts=(),
        )
        root_policy = LooseSkillDefinitionProducer(
            user_product_skills_root=Path.cwd() / ".test-r3-user-product",
            user_agents_skills_root=Path.cwd() / ".test-r3-user-agents",
        ).prepare_root_policy(Path.cwd())
        return issue_skill_catalog_source_snapshot(
            conversation_scope_kind=conversation_scope_kind,
            source_snapshot=snapshot,
            inspection=CompleteEffectiveSkillCatalogInspection(
                root_policy=root_policy,
                winners=(),
                readable_skills=(),
                candidate_issues=(),
            ),
            owner_authenticity=self._owner_authenticity,
        )

    def freeze_projection_input(self, owner):
        if owner.owner_authenticity is not self._owner_authenticity:
            raise ValueError("foreign test Skill owner")
        return FrozenSkillProjectionInput(
            source_snapshot=owner.source_snapshot,
            inspection=owner.inspection,
        )

    def activation_context(self, *, user_input: str):
        return SimpleNamespace(user_input=user_input)

    def compose(self, *, view, owner, activation_subject):
        if owner.owner_authenticity is not self._owner_authenticity:
            raise ValueError("foreign test Skill owner")
        output = self.resolve_projection(
            user_input=activation_subject.user_input,
            available_tool_names=frozenset(),
        )
        return output


class _MutableUnkeyedTimezone(tzinfo):
    def __init__(self, offset: timedelta) -> None:
        self.offset = offset

    def utcoffset(self, _value: datetime | None) -> timedelta:
        return self.offset

    def dst(self, _value: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, _value: datetime | None) -> str:
        return "dynamic-unkeyed"
