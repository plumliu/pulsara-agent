"""Pure planning for append-only canonical provider-input generations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ProviderInputAppendCommittedEvent,
    ProviderInputGenerationClosedEvent,
    ProviderInputGenerationRolloverResolvedEvent,
    ProviderInputGenerationStartedEvent,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.estimator import estimate_model_context_for_call
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.primitives.context import (
    ContextSectionCandidate,
    context_fingerprint,
)
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.context_source import (
    CapabilityToolCatalogRootFact,
    ContextSourceId,
    GenerationRootLifecycleFact,
    LedgerAuthorityHorizonFact,
    RuntimeClockProposalPayloadFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.provider_input import (
    CanonicalProviderInputPlanFact,
    CommittedProviderInputGenerationCoreStateFact,
    CommittedProviderInputReferenceFact,
    ContextInputManifestProjectionReferenceFact,
    InitialGenerationCommitGuardFact,
    OneShotGenerationScopeFact,
    ProviderInputAppendBatchReferenceFact,
    ProviderInputAppendSemanticFact,
    ProviderAcceptedContinuationProjectionJoinFact,
    ProviderInputClockHeadFact,
    ProviderInputCommittedSourceHeadFact,
    ProviderInputContinuationMaterializationProofFact,
    ProviderInputGenerationCompatibilityFact,
    ProviderInputGenerationFact,
    ProviderInputGenerationRootReferenceFact,
    ProviderInputGenerationRootSemanticFact,
    ProviderInputGenerationScopeBindingFact,
    ProviderInputReplayBindingIdentityFact,
    ProviderInputPreparationOwnershipFact,
    ProviderInputPhysicalPolicyFailureReason,
    ProviderInputPendingContinuationFact,
    ProviderInputDispatchBarrierIdentityFact,
    ProviderAuxiliaryFrameRebaseAuthorityFact,
    ProviderCompatibilityChangeAuthorityFact,
    ProviderSystemRootChangeAuthorityFact,
    ProviderToolCatalogChangeAuthorityFact,
    ProviderInputRolloverIntentFact,
    ProviderInputRolloverReason,
    ProviderInputRolloverRequestFact,
    ProviderInputSemanticIdentityFact,
    ProviderInvocationContextFramePlacementFact,
    ProviderInvocationContextFrameSemanticFact,
    ProviderTranscriptDeltaCommitProofFact,
    ProviderTranscriptFrontierFact,
    PreparedProviderInputPlanFact,
    ProviderVisibleInputCompatibilityFact,
    PreparedProviderInputAppendCandidateFact,
    SessionProviderInputContinuityScopeFact,
    ExistingAppendCommitGuardFact,
    RolloverGenerationCommitGuardFact,
    DirectStableMessageSourceAttributionFact,
    CompactionReplacementSummarySourceAttributionFact,
    ProviderLongHorizonRewriteRolloverAuthorityFact,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.runtime.context_engine.types import CompiledContext
from pulsara_agent.runtime.context_input.live import PreparedLiveContextSnapshot
from pulsara_agent.runtime.provider_input.materialization import (
    LOWERING_CONTRACT_ID,
    LOWERING_CONTRACT_FINGERPRINT,
    LOWERING_CONTRACT_VERSION,
    RecursivelyImmutableProviderInputCarrier,
    append_carrier,
    freeze_message_unit,
    freeze_ordered_transcript_unit,
    freeze_provider_message_fragment,
    freeze_tool_unit,
    hydrate_carrier,
    message_semantic_fingerprint,
    ordered_transcript_unit_source_event_refs,
    tool_fragment_semantic_fingerprint,
    tool_semantic_fingerprint,
)
from pulsara_agent.runtime.provider_input.causal import (
    CONTINUATION_JOIN_CONTRACT_FINGERPRINT,
    ProviderInputPhysicalPolicyError,
    build_default_resolved_causal_physical_policy,
    validate_projection,
)
from pulsara_agent.runtime.provider_input.continuation import (
    PreparedProviderInputContinuationMaterialization,
)
from pulsara_agent.runtime.provider_input.store import (
    ProviderInputGenerationSnapshot,
    ProviderInputResidentGeneration,
)
from pulsara_agent.runtime.provider_input.vector import (
    PreparedProviderInputArtifact,
    VECTOR_CONTRACT_FINGERPRINT,
    append_provider_input_vector,
    prepared_json_artifact,
    prepare_ledger_horizon_set,
    prepare_provider_input_vector,
    prepare_replay_binding_set,
    provider_input_artifact_namespace,
)
from pulsara_agent.llm.terminal_projection import stable_event_identity


PROVIDER_INPUT_ROOT_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-input-generation-root-contract:v1",
    {
        "root": "system-prompt-and-tool-catalog",
        "append": "transcript-frontier-and-context-source-revisions",
    },
)
PROVIDER_INPUT_APPEND_ORDERING_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-input-append-ordering-contract:v2",
    (
        "ordered-transcript-strict-suffix",
        "accepted-continuation-exact-join",
        "typed-context-frame-placement",
    ),
)
PROVIDER_INPUT_FRAMING_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-input-framing-contract:v1",
    "system-tools-ordered-message-vector",
)
PROVIDER_CONTEXT_FRAME_INSERTION_POLICY_ID = "pulsara.provider-context-frame"
PROVIDER_CONTEXT_FRAME_INSERTION_POLICY_VERSION = "1"
PROVIDER_CONTEXT_FRAME_INSERTION_POLICY_FINGERPRINT = context_fingerprint(
    "provider-context-frame-insertion-policy:v1",
    ("before-new-current-user", "otherwise-after-new-transcript-tail"),
)


@dataclass(frozen=True, slots=True)
class PreparedProviderInputStartBundle:
    prepared_candidate: PreparedProviderInputAppendCandidateFact
    companion_events: tuple[AgentEvent, ...]
    committed_reference: CommittedProviderInputReferenceFact
    carrier: RecursivelyImmutableProviderInputCarrier
    resident: ProviderInputResidentGeneration
    artifacts: tuple[PreparedProviderInputArtifact, ...]
    prepared_plan: PreparedProviderInputPlanFact | None = None

    @property
    def resulting_core_state(self) -> CommittedProviderInputGenerationCoreStateFact:
        append = next(
            event
            for event in self.companion_events
            if isinstance(event, ProviderInputAppendCommittedEvent)
        )
        return append.resulting_core_state

    @property
    def is_one_shot(self) -> bool:
        return (
            self.prepared_candidate.append_batch_reference.generation.scope.scope_kind
            == "one_shot"
        )


@dataclass(frozen=True, slots=True)
class PreparedProviderInputPlanningBundle:
    """Generation-neutral physical plan created before manifest persistence."""

    prepared_plan: PreparedProviderInputPlanFact
    canonical_plan: CanonicalProviderInputPlanFact
    carrier: RecursivelyImmutableProviderInputCarrier
    resident: ProviderInputResidentGeneration
    artifacts: tuple[PreparedProviderInputArtifact, ...]


def build_one_shot_generation_close_event(
    *,
    bundle: PreparedProviderInputStartBundle,
    event_context: EventContext,
    created_at=None,
) -> ProviderInputGenerationClosedEvent:
    if not bundle.is_one_shot:
        raise ValueError("session-window generation cannot use one-shot close")
    core = bundle.resulting_core_state
    payload = {
        name: getattr(core, name)
        for name in core.__class__.model_fields
        if name not in {"schema_version", "core_state_fingerprint"}
    }
    payload.update(status="closed", reconciliation_reason=None)
    closed = build_frozen_fact(
        CommittedProviderInputGenerationCoreStateFact,
        schema_version="committed_provider_input_generation_core_state.v1",
        **payload,
    )
    event_fields = event_context.event_fields()
    if created_at is not None:
        event_fields["created_at"] = created_at
    return ProviderInputGenerationClosedEvent(
        id=f"provider_input_generation_closed:{core.generation.generation_id}",
        **event_fields,
        generation_id=core.generation.generation_id,
        generation_fingerprint=core.generation.generation_fingerprint,
        final_revision=core.revision,
        final_prefix_fingerprint=core.committed_prefix_fingerprint,
        final_vector_root=core.unit_vector_root,
        close_reason="one_shot_terminal",
        successor_generation_id=None,
        unconsumed_continuation_fingerprint=None,
        predecessor_core_state_fingerprint=core.core_state_fingerprint,
        resulting_closed_core_state=closed,
    )


def build_session_generation_close_event(
    *,
    core: CommittedProviderInputGenerationCoreStateFact,
    event_context: EventContext,
) -> ProviderInputGenerationClosedEvent:
    if not isinstance(
        core.generation.scope, SessionProviderInputContinuityScopeFact
    ):
        raise ValueError("one-shot generation cannot use session close")
    if core.status != "open":
        raise ValueError("session close requires an open provider generation")
    if core.awaiting_control_disposition is not None:
        raise ValueError("session close cannot discard an unresolved model disposition")
    closed = _copy_core_state(
        core,
        status="closed",
        awaiting_control_disposition=None,
        accepted_but_not_appended_continuation=None,
        reconciliation_reason=None,
    )
    pending = core.accepted_but_not_appended_continuation
    return ProviderInputGenerationClosedEvent(
        id=f"provider_input_generation_closed:{core.generation.generation_id}:session",
        **event_context.event_fields(),
        generation_id=core.generation.generation_id,
        generation_fingerprint=core.generation.generation_fingerprint,
        final_revision=core.revision,
        final_prefix_fingerprint=core.committed_prefix_fingerprint,
        final_vector_root=core.unit_vector_root,
        close_reason="session_close",
        successor_generation_id=None,
        unconsumed_continuation_fingerprint=(
            pending.continuation_fingerprint if pending is not None else None
        ),
        predecessor_core_state_fingerprint=core.core_state_fingerprint,
        resulting_closed_core_state=closed,
    )


def plan_one_shot_provider_input(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
    generation_snapshot: ProviderInputGenerationSnapshot,
    event_context: EventContext,
    runtime_session_id: str,
    operation_kind: str,
    operation_id: str,
    attempt_index: int,
) -> PreparedProviderInputStartBundle:
    """Build an exact one-call generation without a synthetic context window."""

    artifact_namespace = provider_input_artifact_namespace(runtime_session_id)

    scope = build_frozen_fact(
        OneShotGenerationScopeFact,
        schema_version="one_shot_generation_scope.v1",
        operation_kind=operation_kind,
        operation_id=operation_id,
        attempt_index=attempt_index,
    )
    if generation_snapshot.scope_binding.scope_fingerprint != scope.scope_fingerprint:
        raise ValueError("one-shot provider snapshot belongs to another scope")
    if (
        generation_snapshot.core_state is not None
        or generation_snapshot.preparation_attribution is not None
        or generation_snapshot.scope_binding.active_generation_id is not None
        or generation_snapshot.scope_binding.active_preparation_id is not None
    ):
        raise ValueError("one-shot provider scope is already owned")

    horizons: tuple[LedgerAuthorityHorizonFact, ...] = ()
    horizon_set = prepare_ledger_horizon_set(
        horizons, artifact_namespace=artifact_namespace
    )
    replay_bindings = (_provider_lowering_replay_binding(),)
    replay_set = prepare_replay_binding_set(
        replay_bindings, artifact_namespace=artifact_namespace
    )
    compatibility = _one_shot_compatibility(call=call, context=context)
    tool_semantics = tuple(tool_semantic_fingerprint(item) for item in context.tools)
    tool_catalog_root = build_frozen_fact(
        CapabilityToolCatalogRootFact,
        schema_version="capability_tool_catalog_root.v1",
        capability_snapshot_semantic_fingerprint=context_fingerprint(
            "one-shot-capability-snapshot:v1", tool_semantics
        ),
        ordered_descriptor_fingerprints=(),
        ordered_tool_spec_fingerprints=tool_semantics,
        tool_catalog_contract_fingerprint=context_fingerprint(
            "one-shot-tool-catalog-contract:v1", "ordered-tool-specs"
        ),
        authority_horizons=horizons,
    )
    root_units = []
    if context.system_prompt:
        root_units.append(
            freeze_message_unit(
                LLMMessage.system(context.system_prompt),
                unit_kind="context_source",
                owner_semantic_fingerprint=context_fingerprint(
                    "one-shot-system-owner:v1", context.system_prompt
                ),
                authority_horizons=horizons,
                estimated_tokens=0,
                required_replay_bindings=replay_bindings,
            )
        )
    root_units.extend(
        freeze_tool_unit(
            tool,
            authority_horizons=horizons,
            estimated_tokens=0,
            required_replay_bindings=replay_bindings,
        )
        for tool in context.tools
    )
    root_units_tuple = tuple(root_units)
    root_vector = prepare_provider_input_vector(
        root_units_tuple, artifact_namespace=artifact_namespace
    )
    root_semantic = build_frozen_fact(
        ProviderInputGenerationRootSemanticFact,
        schema_version="provider_input_generation_root_semantic.v1",
        root_unit_count=len(root_units_tuple),
        root_ordered_unit_accumulator=root_vector.root_reference.ordered_unit_accumulator,
        root_unit_vector_semantic_fingerprint=(
            root_vector.root_reference.vector_semantic_fingerprint
        ),
        root_lowering_contract_fingerprint=LOWERING_CONTRACT_FINGERPRINT,
        tool_catalog_root_semantic_fingerprint=(
            compatibility.tool_catalog_semantic_fingerprint
        ),
    )
    generation_id = _generation_id(
        scope.scope_fingerprint,
        compatibility,
        root_semantic_fingerprint=root_semantic.root_semantic_fingerprint,
    )
    call_lane = {
        "direct_model_call": "direct_one_shot",
        "window_summarizer": "window_summarizer",
        "governance_model_call": "governance_one_shot",
    }[operation_kind]
    generation = build_frozen_fact(
        ProviderInputGenerationFact,
        schema_version="provider_input_generation.v1",
        generation_id=generation_id,
        call_lane=call_lane,
        scope=scope,
        compatibility=compatibility,
        predecessor_generation_id=None,
        predecessor_generation_fingerprint=None,
        rollover_reason=None,
    )
    root_artifact = prepared_json_artifact(
        "provider-input-generation-root",
        {
            "schema_version": "provider_input_generation_root_artifact.v1",
            "generation": generation.model_dump(mode="json"),
            "root_semantic": root_semantic.model_dump(mode="json"),
            "root_vector": root_vector.root_reference.model_dump(mode="json"),
        },
        artifact_namespace=artifact_namespace,
        contract_fingerprint=PROVIDER_INPUT_ROOT_CONTRACT_FINGERPRINT,
        metadata_kind="provider_input_generation_root",
    )
    root_reference = build_frozen_fact(
        ProviderInputGenerationRootReferenceFact,
        schema_version="provider_input_generation_root_reference.v1",
        generation=generation,
        root_semantic=root_semantic,
        tool_catalog_root=tool_catalog_root,
        initial_unit_vector_root=root_vector.root_reference,
        authority_horizon_set=horizon_set.reference,
        replay_binding_set=replay_set.reference,
        root_artifact_reference=root_artifact.artifact_reference,
    )
    prefix_before = context_fingerprint(
        "provider-input-prefix:v1",
        {
            "provider_visible_compatibility_fingerprint": (
                compatibility.provider_visible.semantic_fingerprint
            ),
            "generation_root_semantic_fingerprint": (
                root_semantic.root_semantic_fingerprint
            ),
        },
    )
    empty_frontier = _empty_transcript_frontier(horizon_set.reference)
    genesis = build_frozen_fact(
        CommittedProviderInputGenerationCoreStateFact,
        schema_version="committed_provider_input_generation_core_state.v1",
        generation=generation,
        root_reference=root_reference,
        status="open",
        revision=0,
        next_append_index=1,
        committed_prefix_fingerprint=prefix_before,
        unit_count=len(root_units_tuple),
        unit_vector_root=root_vector.root_reference,
        committed_authority_horizon_set=horizon_set.reference,
        replay_binding_set=replay_set.reference,
        transcript_frontier=empty_frontier,
        committed_source_heads=(),
        clock_head=None,
        awaiting_control_disposition=None,
        accepted_but_not_appended_continuation=None,
        reconciliation_reason=None,
    )

    append_units = tuple(
        freeze_message_unit(
            message,
            unit_kind="context_source",
            owner_semantic_fingerprint=message_semantic_fingerprint(message),
            authority_horizons=horizons,
            estimated_tokens=0,
            required_replay_bindings=replay_bindings,
        )
        for message in context.messages
    )
    if not append_units:
        raise ValueError("one-shot provider input requires at least one message")
    if len(append_units) > 512:
        raise ValueError("one-shot provider input exceeds append unit bound")
    all_units = (*root_units_tuple, *append_units)
    vector = append_provider_input_vector(
        root_vector.state,
        append_units,
        artifact_namespace=artifact_namespace,
    )
    append_semantic = build_frozen_fact(
        ProviderInputAppendSemanticFact,
        schema_version="provider_input_append_semantic.v1",
        ordered_unit_semantic_fingerprints=tuple(
            item.attribution.semantic.semantic_fingerprint for item in append_units
        ),
        append_ordering_contract_fingerprint=(
            PROVIDER_INPUT_APPEND_ORDERING_CONTRACT_FINGERPRINT
        ),
    )
    append_artifact = prepared_json_artifact(
        "provider-input-append",
        {
            "schema_version": "provider_input_append_artifact.v1",
            "generation_id": generation_id,
            "append_index": 1,
            "units": tuple(item.model_dump(mode="json") for item in append_units),
        },
        artifact_namespace=artifact_namespace,
        contract_fingerprint=PROVIDER_INPUT_APPEND_ORDERING_CONTRACT_FINGERPRINT,
        metadata_kind="provider_input_append",
    )
    _validate_append_artifact_physical_bound(
        canonical_text=append_artifact.canonical_text,
        max_canonical_bytes=(
            build_default_resolved_causal_physical_policy().max_append_candidate_canonical_bytes
        ),
    )
    prefix_after = context_fingerprint(
        "provider-input-prefix:v1",
        {
            "predecessor_prefix_fingerprint": prefix_before,
            "append_semantic_fingerprint": append_semantic.semantic_fingerprint,
        },
    )
    append_reference = build_frozen_fact(
        ProviderInputAppendBatchReferenceFact,
        schema_version="provider_input_append_batch_reference.v1",
        generation=generation,
        expected_generation_revision=0,
        append_index=1,
        authority_horizons=horizons,
        append_semantic=append_semantic,
        batch_artifact_reference=append_artifact.artifact_reference,
        changed_vector_node_refs=vector.changed_node_references,
        resulting_unit_vector_root=vector.root_reference,
        resulting_authority_horizon_set=horizon_set.reference,
        new_replay_bindings=replay_bindings,
        resulting_replay_binding_set=replay_set.reference,
        predecessor_prefix_fingerprint=prefix_before,
        resulting_prefix_fingerprint=prefix_after,
    )
    resulting_core = build_frozen_fact(
        CommittedProviderInputGenerationCoreStateFact,
        schema_version="committed_provider_input_generation_core_state.v1",
        generation=generation,
        root_reference=root_reference,
        status="open",
        revision=1,
        next_append_index=2,
        committed_prefix_fingerprint=prefix_after,
        unit_count=len(all_units),
        unit_vector_root=vector.root_reference,
        committed_authority_horizon_set=horizon_set.reference,
        replay_binding_set=replay_set.reference,
        transcript_frontier=empty_frontier,
        committed_source_heads=(),
        clock_head=None,
        awaiting_control_disposition=None,
        accepted_but_not_appended_continuation=None,
        reconciliation_reason=None,
    )
    carrier = hydrate_carrier(all_units)
    identity = _provider_input_identity(
        carrier=carrier, vector_root=vector.root_reference
    )
    plan = build_frozen_fact(
        CanonicalProviderInputPlanFact,
        schema_version="canonical_provider_input_plan.v1",
        resolved_model_call_fact=call.fact,
        generation_root_reference=root_reference,
        resulting_prefix_fingerprint=prefix_after,
        resulting_generation_revision=1,
        unit_vector_root=vector.root_reference,
        authority_horizon_set=horizon_set.reference,
        replay_binding_set=replay_set.reference,
        provider_input_semantic_identity=identity,
    )
    predecessor_binding = generation_snapshot.scope_binding
    preparation_id = _preparation_id(
        scope_fingerprint=scope.scope_fingerprint,
        generation_id=generation_id,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        append_index=1,
        ownership_kind="initial_start",
    )
    prepared_binding = build_frozen_fact(
        ProviderInputGenerationScopeBindingFact,
        schema_version="provider_input_generation_scope_binding.v1",
        scope_fingerprint=scope.scope_fingerprint,
        active_generation_id=None,
        latest_closed_generation_id=None,
        active_preparation_id=preparation_id,
    )
    append_event_id = (
        f"provider_input_append:{generation_id}:1:{call.fact.resolved_model_call_id}"
    )
    model_start_event_id = f"model_call_start:{call.fact.resolved_model_call_id}"
    generation_start_event_id = f"provider_input_generation_started:{generation_id}"
    companion_ids = (generation_start_event_id, append_event_id)
    ownership = build_frozen_fact(
        ProviderInputPreparationOwnershipFact,
        schema_version="provider_input_preparation_ownership.v1",
        preparation_id=preparation_id,
        ownership_kind="initial_start",
        generation_id=generation_id,
        scope_fingerprint=scope.scope_fingerprint,
        expected_predecessor_scope_binding_fingerprint=(
            predecessor_binding.binding_fingerprint
        ),
        resulting_scope_binding_fingerprint=prepared_binding.binding_fingerprint,
        expected_committed_core_state_fingerprint=None,
        expected_revision=0,
        append_batch_reference_fingerprint=append_reference.reference_fingerprint,
        provider_input_plan_fingerprint=plan.plan_fingerprint,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        stable_companion_event_ids=companion_ids,
    )
    guard = build_frozen_fact(
        InitialGenerationCommitGuardFact,
        schema_version="initial_generation_commit_guard.v1",
        new_generation_id=generation_id,
        new_generation_fingerprint=generation.generation_fingerprint,
        new_root_reference_fingerprint=root_reference.reference_fingerprint,
        expected_scope_binding_fingerprint=prepared_binding.binding_fingerprint,
        expected_preparation_ownership_fingerprint=ownership.ownership_fingerprint,
        expected_authority_horizon_set_reference_fingerprint=(
            horizon_set.reference.reference_fingerprint
        ),
        expected_revision=0,
        resolved_model_call_id=call.fact.resolved_model_call_id,
    )
    prepared_candidate = build_frozen_fact(
        PreparedProviderInputAppendCandidateFact,
        schema_version="prepared_provider_input_append_candidate.v2",
        candidate_kind="one_shot",
        generation_id=generation_id,
        preparation_ownership=ownership,
        expected_committed_core_state_fingerprint=None,
        append_batch_reference=append_reference,
        provider_input_plan=plan,
        prepared_plan=None,
        manifest_projection_reference=None,
        rollover_request=None,
        stable_companion_event_ids=companion_ids,
        generation_commit_guard=guard,
    )
    append_event = ProviderInputAppendCommittedEvent(
        id=append_event_id,
        **event_context.event_fields(),
        append_kind="one_shot",
        generation_id=generation_id,
        generation_fingerprint=generation.generation_fingerprint,
        expected_revision=0,
        resulting_revision=1,
        append_batch_reference=append_reference,
        consumed_preparation_id=preparation_id,
        consumed_preparation_ownership_fingerprint=ownership.ownership_fingerprint,
        consumed_pending_continuation_fingerprint=None,
        continuation_materialization_proof=None,
        manifest_projection_reference=None,
        causal_validation=None,
        frame_placement=None,
        transcript_delta_proof=None,
        prepared_provider_input_candidate_fingerprint=None,
        predecessor_core_state_fingerprint=genesis.core_state_fingerprint,
        resulting_core_state=resulting_core,
        authority_horizons=horizons,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        expected_model_start_event_id=model_start_event_id,
    )
    companions: tuple[AgentEvent, ...] = (
        ProviderInputGenerationStartedEvent(
            id=generation_start_event_id,
            **event_context.event_fields(),
            generation=generation,
            root_reference=root_reference,
            initial_vector_root=root_vector.root_reference,
            initial_prefix_fingerprint=prefix_before,
            authority_horizon_set=horizon_set.reference,
            expected_initial_append_event_id=append_event_id,
            expected_model_start_event_id=model_start_event_id,
            genesis_core_state=genesis,
        ),
        append_event,
    )
    reference = build_frozen_fact(
        CommittedProviderInputReferenceFact,
        schema_version="committed_provider_input_reference.v2",
        reference_kind="one_shot",
        generation_id=generation_id,
        committed_generation_revision=1,
        resulting_generation_core_state_fingerprint=resulting_core.core_state_fingerprint,
        append_committed_event_identity=stable_event_identity(
            append_event, runtime_session_id=runtime_session_id
        ),
        resulting_prefix_fingerprint=prefix_after,
        resulting_unit_vector_root=vector.root_reference,
        authority_horizon_set=horizon_set.reference,
        replay_binding_set=replay_set.reference,
        provider_input_plan_fingerprint=plan.plan_fingerprint,
        manifest_projection_reference_fingerprint=None,
        causal_validation_fingerprint=None,
        transcript_frontier_fingerprint=None,
    )
    artifacts = _unique_artifacts(
        (
            *root_vector.artifacts,
            *vector.artifacts,
            *horizon_set.artifacts,
            *replay_set.artifacts,
            root_artifact,
            append_artifact,
        )
    )
    resident = ProviderInputResidentGeneration(
        units=vector.state.units,
        vector_state=vector.state,
        carrier=carrier,
        authority_horizons=horizons,
        replay_bindings=replay_bindings,
        reachable_artifact_ids=frozenset(
            item.artifact_reference.artifact_id for item in artifacts
        ),
    )
    return PreparedProviderInputStartBundle(
        prepared_candidate=prepared_candidate,
        companion_events=companions,
        committed_reference=reference,
        carrier=carrier,
        resident=resident,
        artifacts=artifacts,
    )


def plan_provider_input_append(
    *,
    call: ResolvedModelCall,
    compiled_context: CompiledContext,
    prepared_context_input: PreparedLiveContextSnapshot,
    generation_snapshot: ProviderInputGenerationSnapshot,
    event_context: EventContext,
    runtime_session_id: str,
    rollover_from: ProviderInputGenerationSnapshot | None = None,
    rollover_intent: ProviderInputRolloverIntentFact | None = None,
    pending_continuation_materialization: (
        PreparedProviderInputContinuationMaterialization | None
    ) = None,
    manifest_projection_reference: (
        ContextInputManifestProjectionReferenceFact | None
    ) = None,
) -> PreparedProviderInputStartBundle | PreparedProviderInputPlanningBundle:
    """Plan one initial or ordinary append without storage or mutable state."""

    artifact_namespace = provider_input_artifact_namespace(runtime_session_id)

    scope = build_session_provider_input_continuity_scope(
        runtime_session_id=runtime_session_id,
        prepared_context_input=prepared_context_input,
    )
    if generation_snapshot.scope_binding.scope_fingerprint != scope.scope_fingerprint:
        raise ValueError("provider generation snapshot belongs to another scope")
    if generation_snapshot.preparation_attribution is not None:
        raise ValueError("provider generation already has an active preparation")
    if (rollover_from is None) != (rollover_intent is None):
        raise ValueError("provider rollover predecessor/intent must be paired")

    selected_candidates = _selected_compiled_source_candidates(
        compiled_context=compiled_context,
        prepared_context_input=prepared_context_input,
    )
    current_horizons = _merge_horizons(
        (
            *(
                horizon
                for candidate in selected_candidates
                for horizon in candidate.attribution.authority_horizons
            ),
            *prepared_context_input.invocation.fact.capability_tool_catalog_root.authority_horizons,
        )
    )
    horizons = current_horizons
    horizon_set = prepare_ledger_horizon_set(
        horizons, artifact_namespace=artifact_namespace
    )
    current_replay_bindings = _ordered_replay_bindings(
        (
            _provider_lowering_replay_binding(),
            *(
                _context_source_replay_binding(candidate)
                for candidate in selected_candidates
            ),
        )
    )
    replay_set = prepare_replay_binding_set(
        current_replay_bindings, artifact_namespace=artifact_namespace
    )
    compatibility = _compatibility(call, compiled_context)
    rollover_predecessor = (
        rollover_from.core_state if rollover_from is not None else None
    )
    if rollover_from is not None and (
        rollover_predecessor is None
        or rollover_predecessor.status != "open"
        or rollover_from.preparation_attribution is not None
        or rollover_predecessor.awaiting_control_disposition is not None
    ):
        raise ValueError("provider rollover predecessor is not at a safe point")
    predecessor = None if rollover_from is not None else generation_snapshot.core_state
    initial = predecessor is None
    ordered_projection = compiled_context.prepared_ordered_transcript_projection
    if ordered_projection is None:
        raise ValueError("provider append lacks ordered transcript projection")
    policy = compiled_context.provider_causal_physical_policy
    if policy.provider_input_vector_contract_fingerprint != VECTOR_CONTRACT_FINGERPRINT:
        raise ProviderInputPhysicalPolicyError(
            ProviderInputPhysicalPolicyFailureReason.PROVIDER_INPUT_PHYSICAL_POLICY_UNSATISFIED,
            "provider input physical policy does not bind the active vector contract",
        )
    if not initial and predecessor.generation.compatibility != compatibility:
        raise ProviderInputRolloverPlanningRequired(
            _compatibility_rollover_intent(
                predecessor=predecessor,
                resulting=compatibility,
                scope_fingerprint=scope.scope_fingerprint,
                projection_identity_fingerprint=(
                    ordered_projection.identity.identity_fingerprint
                ),
                resolved_model_call_id=call.fact.resolved_model_call_id,
            )
        )
    if not initial:
        _validate_generation_root_sources(
            selected_candidates=selected_candidates,
            predecessor=predecessor,
        )
    if not initial and predecessor.status != "open":
        raise ValueError("provider generation is not open")
    if not initial and predecessor.awaiting_control_disposition is not None:
        raise ValueError("provider generation still awaits control disposition")
    pending_continuation = (
        rollover_predecessor.accepted_but_not_appended_continuation
        if rollover_predecessor is not None
        else predecessor.accepted_but_not_appended_continuation
        if predecessor is not None
        else None
    )

    if predecessor is not None and not _projection_extends_frontier(
        projection=ordered_projection.projection,
        frontier=predecessor.transcript_frontier,
    ):
        raise ProviderInputRolloverPlanningRequired(
            _long_horizon_rollover_intent(
                predecessor=predecessor,
                scope_fingerprint=scope.scope_fingerprint,
                ordered_projection=ordered_projection,
            )
        )

    if predecessor is not None and _removed_dynamic_source_keys(
        predecessor=predecessor,
        selected_candidates=selected_candidates,
    ):
        raise ProviderInputRolloverPlanningRequired(
            _auxiliary_frame_rebase_rollover_intent(
                predecessor_snapshot=generation_snapshot,
                selected_candidates=selected_candidates,
                scope_fingerprint=scope.scope_fingerprint,
                ordered_projection=ordered_projection,
            )
        )

    if initial:
        root_units = _root_units(
            compiled_context=compiled_context,
            required_replay_bindings=current_replay_bindings,
            prepared_context_input=prepared_context_input,
        )
        if len(root_units) > policy.max_generation_root_units:
            raise ProviderInputPhysicalPolicyError(
                ProviderInputPhysicalPolicyFailureReason.PROVIDER_INPUT_APPEND_UNIT_BOUND_EXCEEDED,
                "provider generation root exceeds resolved unit bound",
            )
        horizons = _merge_horizons(
            horizon
            for unit in root_units
            for horizon in unit.attribution.authority_horizons
        )
        horizon_set = prepare_ledger_horizon_set(
            horizons,
            artifact_namespace=artifact_namespace,
        )
        root_vector = prepare_provider_input_vector(
            root_units, artifact_namespace=artifact_namespace
        )
        root_semantic = build_frozen_fact(
            ProviderInputGenerationRootSemanticFact,
            schema_version="provider_input_generation_root_semantic.v1",
            root_unit_count=len(root_units),
            root_ordered_unit_accumulator=(
                root_vector.root_reference.ordered_unit_accumulator
            ),
            root_unit_vector_semantic_fingerprint=(
                root_vector.root_reference.vector_semantic_fingerprint
            ),
            root_lowering_contract_fingerprint=LOWERING_CONTRACT_FINGERPRINT,
            # Tool-catalog horizons are attribution, not provider semantics.
            tool_catalog_root_semantic_fingerprint=(
                compatibility.tool_catalog_semantic_fingerprint
            ),
        )
        identity_predecessor_generation_id = (
            rollover_predecessor.generation.generation_id
            if rollover_predecessor is not None
            else generation_snapshot.scope_binding.latest_closed_generation_id
        )
        generation_id = _generation_id(
            scope.scope_fingerprint,
            compatibility,
            identity_predecessor_generation_id=(
                identity_predecessor_generation_id
            ),
            root_semantic_fingerprint=root_semantic.root_semantic_fingerprint,
        )
        generation = build_frozen_fact(
            ProviderInputGenerationFact,
            schema_version="provider_input_generation.v1",
            generation_id=generation_id,
            call_lane=(
                "subagent"
                if prepared_context_input.invocation.fact.run_entry.run_entry_kind
                == "subagent"
                else "main_agent"
            ),
            scope=scope,
            compatibility=compatibility,
            predecessor_generation_id=(
                rollover_predecessor.generation.generation_id
                if rollover_predecessor is not None
                else None
            ),
            predecessor_generation_fingerprint=(
                rollover_predecessor.generation.generation_fingerprint
                if rollover_predecessor is not None
                else None
            ),
            rollover_reason=(rollover_intent.reason if rollover_intent else None),
        )
        root_artifact = prepared_json_artifact(
            "provider-input-generation-root",
            {
                "schema_version": "provider_input_generation_root_artifact.v1",
                "generation": generation.model_dump(mode="json"),
                "root_semantic": root_semantic.model_dump(mode="json"),
                "root_vector": root_vector.root_reference.model_dump(mode="json"),
            },
            artifact_namespace=artifact_namespace,
            contract_fingerprint=PROVIDER_INPUT_ROOT_CONTRACT_FINGERPRINT,
            metadata_kind="provider_input_generation_root",
        )
        root_reference = build_frozen_fact(
            ProviderInputGenerationRootReferenceFact,
            schema_version="provider_input_generation_root_reference.v1",
            generation=generation,
            root_semantic=root_semantic,
            tool_catalog_root=(
                prepared_context_input.invocation.fact.capability_tool_catalog_root
            ),
            initial_unit_vector_root=root_vector.root_reference,
            authority_horizon_set=horizon_set.reference,
            replay_binding_set=replay_set.reference,
            root_artifact_reference=root_artifact.artifact_reference,
        )
        prefix_before = context_fingerprint(
            "provider-input-prefix:v1",
            {
                "provider_visible_compatibility_fingerprint": (
                    compatibility.provider_visible.semantic_fingerprint
                ),
                "generation_root_semantic_fingerprint": (
                    root_semantic.root_semantic_fingerprint
                ),
            },
        )
        empty_frontier = _empty_transcript_frontier(horizon_set.reference)
        genesis = build_frozen_fact(
            CommittedProviderInputGenerationCoreStateFact,
            schema_version="committed_provider_input_generation_core_state.v1",
            generation=generation,
            root_reference=root_reference,
            status="open",
            revision=0,
            next_append_index=1,
            committed_prefix_fingerprint=prefix_before,
            unit_count=len(root_units),
            unit_vector_root=root_vector.root_reference,
            committed_authority_horizon_set=horizon_set.reference,
            replay_binding_set=replay_set.reference,
            transcript_frontier=empty_frontier,
            committed_source_heads=(),
            clock_head=None,
            awaiting_control_disposition=None,
            accepted_but_not_appended_continuation=None,
            reconciliation_reason=None,
        )
        previous_units = root_units
        previous_vector_state = root_vector.state
        previous_reachable = frozenset(
            item.artifact_reference.artifact_id for item in root_vector.artifacts
        )
        expected_revision = 0
        predecessor_core_fingerprint = genesis.core_state_fingerprint
    else:
        assert predecessor is not None
        generation = predecessor.generation
        generation_id = generation.generation_id
        root_reference = predecessor.root_reference
        prefix_before = predecessor.committed_prefix_fingerprint
        genesis = None
        resident = generation_snapshot.resident
        if resident is None:
            raise ProviderInputResidentRestoreRequired(generation_id)
        previous_units = resident.units
        previous_vector_state = resident.vector_state
        previous_reachable = resident.reachable_artifact_ids
        expected_revision = predecessor.revision
        predecessor_core_fingerprint = predecessor.core_state_fingerprint
        horizons = _merge_horizons((*current_horizons, *resident.authority_horizons))
        horizon_set = prepare_ledger_horizon_set(
            horizons, artifact_namespace=artifact_namespace
        )

    (
        transcript_units,
        transcript_owner_fingerprints,
        continuation_transcript_unit_index,
        continuation_projection_join,
    ) = _new_transcript_units(
        compiled_context=compiled_context,
        horizons=horizons,
        previous_units=previous_units,
        initial=initial,
        required_replay_bindings=current_replay_bindings,
        pending=pending_continuation,
        pending_materialization=pending_continuation_materialization,
    )
    source_units, changed_candidates = _changed_source_units(
        compiled_context=compiled_context,
        predecessor=predecessor,
        initial=initial,
        provider_lowering_binding=_provider_lowering_replay_binding(),
    )
    append_units, frame_placement = _arrange_transcript_delta_and_frame(
        compiled_context=compiled_context,
        transcript_units=transcript_units,
        frame_units=source_units,
        changed_candidates=changed_candidates,
        previous_unit_count=len(previous_units),
        generation_id=generation_id,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        model_call_index=compiled_context.llm_context.model_call_index,
    )
    if not append_units:
        raise ValueError("provider input plan produced an empty append")
    if len(transcript_units) > policy.max_transcript_delta_units_per_append:
        raise ProviderInputPhysicalPolicyError(
            ProviderInputPhysicalPolicyFailureReason.PROVIDER_INPUT_APPEND_UNIT_BOUND_EXCEEDED,
            "provider transcript delta exceeds resolved physical policy",
        )
    if len(source_units) > policy.max_context_frame_units_per_append:
        raise ProviderInputPhysicalPolicyError(
            ProviderInputPhysicalPolicyFailureReason.PROVIDER_INPUT_APPEND_UNIT_BOUND_EXCEEDED,
            "provider context frame exceeds resolved physical policy",
        )
    if not initial and len(append_units) > policy.max_append_units:
        raise ProviderInputPhysicalPolicyError(
            ProviderInputPhysicalPolicyFailureReason.PROVIDER_INPUT_APPEND_UNIT_BOUND_EXCEEDED,
            "provider input append exceeds resolved 512-unit bound",
        )
    if initial and len(previous_units) + len(append_units) > (
        policy.max_initial_generation_units
    ):
        raise ProviderInputPhysicalPolicyError(
            ProviderInputPhysicalPolicyFailureReason.PROVIDER_INPUT_APPEND_UNIT_BOUND_EXCEEDED,
            "initial provider generation exceeds resolved unit bound",
        )

    horizons = _merge_horizons(
        horizon
        for unit in (*previous_units, *append_units)
        for horizon in unit.attribution.authority_horizons
    )
    horizon_set = prepare_ledger_horizon_set(
        horizons,
        artifact_namespace=artifact_namespace,
    )

    previous_replay_bindings = () if initial else resident.replay_bindings
    replay_bindings = _ordered_replay_bindings(
        (*previous_replay_bindings, *current_replay_bindings)
    )
    replay_set = prepare_replay_binding_set(
        replay_bindings, artifact_namespace=artifact_namespace
    )
    previous_binding_fingerprints = {
        item.identity_fingerprint for item in previous_replay_bindings
    }
    new_replay_bindings = tuple(
        item
        for item in replay_bindings
        if item.identity_fingerprint not in previous_binding_fingerprints
    )
    vector = append_provider_input_vector(
        previous_vector_state,
        append_units,
        artifact_namespace=artifact_namespace,
    )
    all_units = vector.state.units
    append_semantic = build_frozen_fact(
        ProviderInputAppendSemanticFact,
        schema_version="provider_input_append_semantic.v1",
        ordered_unit_semantic_fingerprints=tuple(
            item.attribution.semantic.semantic_fingerprint for item in append_units
        ),
        append_ordering_contract_fingerprint=(
            PROVIDER_INPUT_APPEND_ORDERING_CONTRACT_FINGERPRINT
        ),
    )
    append_artifact = prepared_json_artifact(
        "provider-input-append",
        {
            "schema_version": "provider_input_append_artifact.v1",
            "generation_id": generation_id,
            "append_index": expected_revision + 1,
            "units": tuple(item.model_dump(mode="json") for item in append_units),
        },
        artifact_namespace=artifact_namespace,
        contract_fingerprint=PROVIDER_INPUT_APPEND_ORDERING_CONTRACT_FINGERPRINT,
        metadata_kind="provider_input_append",
    )
    _validate_append_artifact_physical_bound(
        canonical_text=append_artifact.canonical_text,
        max_canonical_bytes=policy.max_append_candidate_canonical_bytes,
    )
    prefix_after = context_fingerprint(
        "provider-input-prefix:v1",
        {
            "predecessor_prefix_fingerprint": prefix_before,
            "append_semantic_fingerprint": append_semantic.semantic_fingerprint,
        },
    )
    append_reference = build_frozen_fact(
        ProviderInputAppendBatchReferenceFact,
        schema_version="provider_input_append_batch_reference.v1",
        generation=generation,
        expected_generation_revision=expected_revision,
        append_index=expected_revision + 1,
        authority_horizons=horizons,
        append_semantic=append_semantic,
        batch_artifact_reference=append_artifact.artifact_reference,
        changed_vector_node_refs=vector.changed_node_references,
        resulting_unit_vector_root=vector.root_reference,
        resulting_authority_horizon_set=horizon_set.reference,
        new_replay_bindings=new_replay_bindings,
        resulting_replay_binding_set=replay_set.reference,
        predecessor_prefix_fingerprint=prefix_before,
        resulting_prefix_fingerprint=prefix_after,
    )

    frontier = _transcript_frontier(
        compiled_context=compiled_context,
        predecessor=(
            predecessor.transcript_frontier if predecessor is not None else None
        ),
        committed_count=len(
            compiled_context.prepared_ordered_transcript_projection.projection.ordered_units
        ),
        authority_horizon_set=horizon_set.reference,
    )
    causal_validation = validate_projection(
        projection=ordered_projection.projection,
        identity=ordered_projection.identity,
        policy=policy,
    )
    if causal_validation.status != "valid":
        raise ValueError("provider transcript causal validation failed")
    predecessor_frontier = (
        predecessor.transcript_frontier
        if predecessor is not None
        else _empty_transcript_frontier(horizon_set.reference)
    )
    delta_first = predecessor_frontier.committed_transcript_unit_count
    delta_units = ordered_projection.projection.ordered_units[delta_first:]
    delta_last = delta_first + len(delta_units) - 1
    transcript_delta_proof = build_frozen_fact(
        ProviderTranscriptDeltaCommitProofFact,
        schema_version="provider_transcript_delta_commit_proof.v1",
        projection_identity_fingerprint=ordered_projection.identity.identity_fingerprint,
        predecessor_frontier_fingerprint=(
            predecessor_frontier.provider_semantic_frontier_fingerprint
        ),
        delta_first_projection_index=delta_first if delta_units else None,
        delta_last_projection_index=delta_last if delta_units else None,
        ordered_delta_wire_accumulator=_ordered_fingerprint_accumulator(
            "provider-ordered-transcript-wire:v2",
            tuple(
                item.wire_semantic.wire_semantic_fingerprint for item in delta_units
            ),
        ),
        ordered_delta_causal_accumulator=_ordered_fingerprint_accumulator(
            "provider-ordered-transcript-causal:v2",
            tuple(item.unit_causal_semantic_fingerprint for item in delta_units),
        ),
        continuation_joins=(
            (continuation_projection_join,)
            if continuation_projection_join is not None
            else ()
        ),
        resulting_frontier=frontier,
        resolved_causal_physical_policy_fingerprint=policy.policy_fingerprint,
    )
    source_heads = _source_heads(
        selected_candidates=selected_candidates,
        predecessor=predecessor,
        changed_candidates=changed_candidates,
        available_source_units=(
            *(root_units if initial else ()),
            *source_units,
        ),
        append_index=expected_revision + 1,
    )
    clock_head = _clock_head(
        selected_candidates=selected_candidates,
        predecessor=predecessor,
        append_index=expected_revision + 1,
    )
    resulting_core = build_frozen_fact(
        CommittedProviderInputGenerationCoreStateFact,
        schema_version="committed_provider_input_generation_core_state.v1",
        generation=generation,
        root_reference=root_reference,
        status="open",
        revision=expected_revision + 1,
        next_append_index=expected_revision + 2,
        committed_prefix_fingerprint=prefix_after,
        unit_count=len(all_units),
        unit_vector_root=vector.root_reference,
        committed_authority_horizon_set=horizon_set.reference,
        replay_binding_set=replay_set.reference,
        transcript_frontier=frontier,
        committed_source_heads=source_heads,
        clock_head=clock_head,
        awaiting_control_disposition=None,
        accepted_but_not_appended_continuation=None,
        reconciliation_reason=None,
    )
    continuation_proof = _continuation_materialization_proof(
        pending=pending_continuation,
        predecessor_frontier=(
            rollover_predecessor.transcript_frontier
            if rollover_predecessor is not None
            else predecessor.transcript_frontier
            if predecessor is not None
            else None
        ),
        resulting_frontier=resulting_core.transcript_frontier,
        previous_unit_count=len(previous_units),
        append_units=append_units,
        continuation_append_unit_index=(
            next(
                index
                for index, unit in enumerate(append_units)
                if unit is transcript_units[continuation_transcript_unit_index]
            )
            if continuation_transcript_unit_index is not None
            else None
        ),
    )
    carrier = (
        hydrate_carrier(all_units)
        if initial
        else append_carrier(resident.carrier, tuple(append_units))
    )
    _validate_planned_input_budget(
        call=call,
        carrier=carrier,
        template=compiled_context.llm_context,
        retained_generation=predecessor is not None and rollover_from is None,
    )
    identity = build_frozen_fact(
        ProviderInputSemanticIdentityFact,
        schema_version="provider_input_semantic_identity.v1",
        input_unit_count=len(all_units),
        ordered_unit_accumulator=vector.root_reference.ordered_unit_accumulator,
        unit_vector_semantic_fingerprint=(
            vector.root_reference.vector_semantic_fingerprint
        ),
        system_instruction_fingerprint=context_fingerprint(
            "provider-input-system-prompt:v1", carrier.system_prompt
        ),
        tool_catalog_fingerprint=context_fingerprint(
            "provider-input-tool-catalog:v1",
            tuple(
                tool_fragment_semantic_fingerprint(item)
                for item in carrier.ordered_tool_fragments
            ),
        ),
        provider_message_sequence_fingerprint=context_fingerprint(
            "provider-input-message-sequence:v1",
            tuple(
                message_semantic_fingerprint(item) for item in carrier.ordered_messages
            ),
        ),
    )
    plan = build_frozen_fact(
        CanonicalProviderInputPlanFact,
        schema_version="canonical_provider_input_plan.v1",
        resolved_model_call_fact=call.fact,
        generation_root_reference=root_reference,
        resulting_prefix_fingerprint=prefix_after,
        resulting_generation_revision=expected_revision + 1,
        unit_vector_root=vector.root_reference,
        authority_horizon_set=horizon_set.reference,
        replay_binding_set=replay_set.reference,
        provider_input_semantic_identity=identity,
    )

    prepared_plan = build_frozen_fact(
        PreparedProviderInputPlanFact,
        schema_version="prepared_provider_input_plan.v1",
        plan_kind=(
            "rollover_initial_append"
            if rollover_predecessor is not None
            else "initial_generation"
            if initial
            else "existing_generation_append"
        ),
        resolved_model_call_id=call.fact.resolved_model_call_id,
        continuity_scope_fingerprint=scope.scope_fingerprint,
        target_generation_id=generation_id,
        predecessor_core_state_fingerprint=(
            rollover_predecessor.core_state_fingerprint
            if rollover_predecessor is not None
            else predecessor.core_state_fingerprint
            if predecessor is not None
            else None
        ),
        ordered_transcript_projection_identity=ordered_projection.identity,
        causal_validation=causal_validation,
        frame_placement=frame_placement,
        transcript_delta_proof=transcript_delta_proof,
        rollover_intent=rollover_intent,
        resulting_unit_vector_root_fingerprint=(
            vector.root_reference.reference_fingerprint
        ),
        resolved_causal_physical_policy_fingerprint=policy.policy_fingerprint,
    )

    artifacts = _unique_artifacts(
        (
            *(root_vector.artifacts if initial else ()),
            *vector.artifacts,
            *horizon_set.artifacts,
            *replay_set.artifacts,
            *((root_artifact,) if initial else ()),
            append_artifact,
        )
    )
    reachable = frozenset(
        {
            *previous_reachable,
            *(item.artifact_reference.artifact_id for item in artifacts),
        }
    )
    resident = ProviderInputResidentGeneration(
        units=all_units,
        vector_state=vector.state,
        carrier=carrier,
        authority_horizons=horizons,
        replay_bindings=replay_bindings,
        reachable_artifact_ids=reachable,
    )
    planning_bundle = PreparedProviderInputPlanningBundle(
        prepared_plan=prepared_plan,
        canonical_plan=plan,
        carrier=carrier,
        resident=resident,
        artifacts=artifacts,
    )
    if manifest_projection_reference is None:
        return planning_bundle
    if (
        manifest_projection_reference.projection_identity
        != ordered_projection.identity
        or manifest_projection_reference.context_id != compiled_context.context_id
    ):
        raise ValueError("provider plan manifest projection reference drifted")
    rollover_request = None
    if rollover_intent is not None:
        request_id = "provider-input-rollover-request:" + context_fingerprint(
            "provider-input-rollover-request-id:v1",
            (
                rollover_intent.intent_fingerprint,
                manifest_projection_reference.reference_fingerprint,
            ),
        ).removeprefix("sha256:")
        rollover_request = build_frozen_fact(
            ProviderInputRolloverRequestFact,
            schema_version="provider_input_rollover_request.v1",
            rollover_request_id=request_id,
            intent=rollover_intent,
            manifest_projection_reference=manifest_projection_reference,
        )

    preparation_id = _preparation_id(
        scope_fingerprint=scope.scope_fingerprint,
        generation_id=generation_id,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        append_index=expected_revision + 1,
        ownership_kind=(
            "rollover_start"
            if rollover_predecessor is not None
            else "initial_start"
            if initial
            else "existing_append"
        ),
    )
    predecessor_binding = generation_snapshot.scope_binding
    resulting_preparation_binding = build_frozen_fact(
        ProviderInputGenerationScopeBindingFact,
        schema_version="provider_input_generation_scope_binding.v1",
        scope_fingerprint=scope.scope_fingerprint,
        active_generation_id=predecessor_binding.active_generation_id,
        latest_closed_generation_id=predecessor_binding.latest_closed_generation_id,
        active_preparation_id=preparation_id,
    )
    append_event_id = (
        f"provider_input_append:{generation_id}:{expected_revision + 1}:"
        f"{call.fact.resolved_model_call_id}"
    )
    model_start_event_id = f"model_call_start:{call.fact.resolved_model_call_id}"
    generation_start_event_id = f"provider_input_generation_started:{generation_id}"
    old_close_event_id = (
        f"provider_input_generation_closed:{rollover_predecessor.generation.generation_id}:"
        f"rollover:{generation_id}"
        if rollover_predecessor is not None
        else None
    )
    rollover_event_id = (
        "provider_input_generation_rollover:"
        f"{rollover_request.request_fingerprint.removeprefix('sha256:')}"
        if rollover_request is not None
        else None
    )
    companion_ids = (
        (
            old_close_event_id,
            rollover_event_id,
            generation_start_event_id,
            append_event_id,
        )
        if rollover_predecessor is not None
        else (generation_start_event_id, append_event_id)
        if initial
        else (append_event_id,)
    )
    ownership = build_frozen_fact(
        ProviderInputPreparationOwnershipFact,
        schema_version="provider_input_preparation_ownership.v1",
        preparation_id=preparation_id,
        ownership_kind=(
            "rollover_start"
            if rollover_predecessor is not None
            else "initial_start"
            if initial
            else "existing_append"
        ),
        generation_id=generation_id,
        scope_fingerprint=scope.scope_fingerprint,
        expected_predecessor_scope_binding_fingerprint=(
            predecessor_binding.binding_fingerprint
        ),
        resulting_scope_binding_fingerprint=(
            resulting_preparation_binding.binding_fingerprint
        ),
        expected_committed_core_state_fingerprint=(
            rollover_predecessor.core_state_fingerprint
            if rollover_predecessor is not None
            else None
            if initial
            else predecessor_core_fingerprint
        ),
        expected_revision=expected_revision,
        append_batch_reference_fingerprint=append_reference.reference_fingerprint,
        provider_input_plan_fingerprint=plan.plan_fingerprint,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        stable_companion_event_ids=companion_ids,
    )
    if rollover_predecessor is not None:
        assert rollover_from is not None
        assert rollover_request is not None
        barrier = build_frozen_fact(
            ProviderInputDispatchBarrierIdentityFact,
            schema_version="provider_input_dispatch_barrier_identity.v1",
            barrier_id=(
                f"provider-input-rollover-barrier:"
                f"{rollover_predecessor.generation.generation_id}:{generation_id}"
            ),
            scope_fingerprint=(rollover_predecessor.generation.scope.scope_fingerprint),
            old_generation_id=rollover_predecessor.generation.generation_id,
            installed_at_core_revision=rollover_predecessor.revision,
            attempt_id=call.fact.resolved_model_call_id,
        )
        guard = build_frozen_fact(
            RolloverGenerationCommitGuardFact,
            schema_version="rollover_generation_commit_guard.v1",
            old_generation_id=rollover_predecessor.generation.generation_id,
            expected_old_core_state_fingerprint=(
                rollover_predecessor.core_state_fingerprint
            ),
            expected_old_revision=rollover_predecessor.revision,
            expected_old_prefix_fingerprint=(
                rollover_predecessor.committed_prefix_fingerprint
            ),
            old_scope_fingerprint=(
                rollover_predecessor.generation.scope.scope_fingerprint
            ),
            expected_old_scope_binding_fingerprint=(
                resulting_preparation_binding.binding_fingerprint
                if rollover_predecessor.generation.scope.scope_fingerprint
                == scope.scope_fingerprint
                else rollover_from.scope_binding.binding_fingerprint
            ),
            new_generation_id=generation_id,
            new_generation_fingerprint=generation.generation_fingerprint,
            new_root_reference_fingerprint=root_reference.reference_fingerprint,
            new_scope_fingerprint=scope.scope_fingerprint,
            expected_new_scope_binding_fingerprint=(
                resulting_preparation_binding.binding_fingerprint
            ),
            expected_preparation_ownership_fingerprint=(
                ownership.ownership_fingerprint
            ),
            rollover_authority_horizon_set_reference_fingerprint=(
                horizon_set.reference.reference_fingerprint
            ),
            rollover_request_fingerprint=rollover_request.request_fingerprint,
            dispatch_barrier_identity=barrier,
            resolved_model_call_id=call.fact.resolved_model_call_id,
        )
    elif initial:
        guard = build_frozen_fact(
            InitialGenerationCommitGuardFact,
            schema_version="initial_generation_commit_guard.v1",
            new_generation_id=generation_id,
            new_generation_fingerprint=generation.generation_fingerprint,
            new_root_reference_fingerprint=root_reference.reference_fingerprint,
            expected_scope_binding_fingerprint=(
                resulting_preparation_binding.binding_fingerprint
            ),
            expected_preparation_ownership_fingerprint=(
                ownership.ownership_fingerprint
            ),
            expected_authority_horizon_set_reference_fingerprint=(
                horizon_set.reference.reference_fingerprint
            ),
            expected_revision=0,
            resolved_model_call_id=call.fact.resolved_model_call_id,
        )
    else:
        assert predecessor is not None
        guard = build_frozen_fact(
            ExistingAppendCommitGuardFact,
            schema_version="existing_append_commit_guard.v1",
            generation_id=generation_id,
            expected_committed_core_state_fingerprint=(
                predecessor.core_state_fingerprint
            ),
            expected_preparation_ownership_fingerprint=(
                ownership.ownership_fingerprint
            ),
            expected_revision=predecessor.revision,
            expected_committed_prefix_fingerprint=(
                predecessor.committed_prefix_fingerprint
            ),
            expected_transcript_frontier_fingerprint=(
                predecessor.transcript_frontier.provider_semantic_frontier_fingerprint
            ),
            expected_awaiting_disposition_fingerprint=(
                predecessor.awaiting_control_disposition.awaiting_fingerprint
                if predecessor.awaiting_control_disposition is not None
                else None
            ),
            expected_pending_continuation_fingerprint=(
                predecessor.accepted_but_not_appended_continuation.continuation_fingerprint
                if predecessor.accepted_but_not_appended_continuation is not None
                else None
            ),
            expected_scope_binding_fingerprint=(
                resulting_preparation_binding.binding_fingerprint
            ),
            resolved_model_call_id=call.fact.resolved_model_call_id,
        )
    prepared_candidate = build_frozen_fact(
        PreparedProviderInputAppendCandidateFact,
        schema_version="prepared_provider_input_append_candidate.v2",
        candidate_kind="compiled_manifest",
        generation_id=generation_id,
        preparation_ownership=ownership,
        expected_committed_core_state_fingerprint=(
            rollover_predecessor.core_state_fingerprint
            if rollover_predecessor is not None
            else None
            if initial
            else predecessor_core_fingerprint
        ),
        append_batch_reference=append_reference,
        provider_input_plan=plan,
        prepared_plan=prepared_plan,
        manifest_projection_reference=manifest_projection_reference,
        rollover_request=rollover_request,
        stable_companion_event_ids=companion_ids,
        generation_commit_guard=guard,
    )

    append_event = ProviderInputAppendCommittedEvent(
        id=append_event_id,
        **event_context.event_fields(),
        append_kind="compiled_manifest",
        generation_id=generation_id,
        generation_fingerprint=generation.generation_fingerprint,
        expected_revision=expected_revision,
        resulting_revision=expected_revision + 1,
        append_batch_reference=append_reference,
        consumed_preparation_id=preparation_id,
        consumed_preparation_ownership_fingerprint=ownership.ownership_fingerprint,
        consumed_pending_continuation_fingerprint=(
            pending_continuation.continuation_fingerprint
            if pending_continuation is not None
            else None
        ),
        continuation_materialization_proof=continuation_proof,
        manifest_projection_reference=manifest_projection_reference,
        causal_validation=causal_validation,
        frame_placement=frame_placement,
        transcript_delta_proof=transcript_delta_proof,
        prepared_provider_input_candidate_fingerprint=(
            prepared_candidate.candidate_fingerprint
        ),
        predecessor_core_state_fingerprint=predecessor_core_fingerprint,
        resulting_core_state=resulting_core,
        authority_horizons=horizons,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        expected_model_start_event_id=model_start_event_id,
    )
    companions: tuple[AgentEvent, ...]
    if rollover_predecessor is not None:
        assert genesis is not None
        assert old_close_event_id is not None
        assert rollover_event_id is not None
        assert rollover_request is not None
        old_closed_core = _copy_core_state(
            rollover_predecessor,
            status="closed",
            awaiting_control_disposition=None,
            accepted_but_not_appended_continuation=None,
            reconciliation_reason=None,
        )
        close_event = ProviderInputGenerationClosedEvent(
            id=old_close_event_id,
            **event_context.event_fields(),
            generation_id=rollover_predecessor.generation.generation_id,
            generation_fingerprint=(
                rollover_predecessor.generation.generation_fingerprint
            ),
            final_revision=rollover_predecessor.revision,
            final_prefix_fingerprint=(
                rollover_predecessor.committed_prefix_fingerprint
            ),
            final_vector_root=rollover_predecessor.unit_vector_root,
            close_reason="rollover",
            successor_generation_id=generation_id,
            unconsumed_continuation_fingerprint=(
                rollover_predecessor.accepted_but_not_appended_continuation.continuation_fingerprint
                if rollover_predecessor.accepted_but_not_appended_continuation
                is not None
                else None
            ),
            predecessor_core_state_fingerprint=(
                rollover_predecessor.core_state_fingerprint
            ),
            resulting_closed_core_state=old_closed_core,
        )
        rollover_event = ProviderInputGenerationRolloverResolvedEvent(
            id=rollover_event_id,
            **event_context.event_fields(),
            old_generation_id=rollover_predecessor.generation.generation_id,
            old_generation_fingerprint=(
                rollover_predecessor.generation.generation_fingerprint
            ),
            old_final_core_state_fingerprint=old_closed_core.core_state_fingerprint,
            new_generation=generation,
            new_root_reference=root_reference,
            rollover_request=rollover_request,
            authority_horizon_set=horizon_set.reference,
            expected_old_close_event_id=old_close_event_id,
            expected_new_start_event_id=generation_start_event_id,
            expected_initial_append_event_id=append_event_id,
            expected_model_start_event_id=model_start_event_id,
        )
        companions = (
            close_event,
            rollover_event,
            ProviderInputGenerationStartedEvent(
                id=generation_start_event_id,
                **event_context.event_fields(),
                generation=generation,
                root_reference=root_reference,
                initial_vector_root=root_vector.root_reference,
                initial_prefix_fingerprint=prefix_before,
                authority_horizon_set=horizon_set.reference,
                expected_initial_append_event_id=append_event_id,
                expected_model_start_event_id=model_start_event_id,
                genesis_core_state=genesis,
            ),
            append_event,
        )
    elif initial:
        assert genesis is not None
        companions = (
            ProviderInputGenerationStartedEvent(
                id=generation_start_event_id,
                **event_context.event_fields(),
                generation=generation,
                root_reference=root_reference,
                initial_vector_root=root_vector.root_reference,
                initial_prefix_fingerprint=prefix_before,
                authority_horizon_set=horizon_set.reference,
                expected_initial_append_event_id=append_event_id,
                expected_model_start_event_id=model_start_event_id,
                genesis_core_state=genesis,
            ),
            append_event,
        )
    else:
        companions = (append_event,)
    reference = build_frozen_fact(
        CommittedProviderInputReferenceFact,
        schema_version="committed_provider_input_reference.v2",
        reference_kind="compiled_manifest",
        generation_id=generation_id,
        committed_generation_revision=expected_revision + 1,
        resulting_generation_core_state_fingerprint=(
            resulting_core.core_state_fingerprint
        ),
        append_committed_event_identity=stable_event_identity(
            append_event, runtime_session_id=runtime_session_id
        ),
        resulting_prefix_fingerprint=prefix_after,
        resulting_unit_vector_root=vector.root_reference,
        authority_horizon_set=horizon_set.reference,
        replay_binding_set=replay_set.reference,
        provider_input_plan_fingerprint=plan.plan_fingerprint,
        manifest_projection_reference_fingerprint=(
            manifest_projection_reference.reference_fingerprint
        ),
        causal_validation_fingerprint=causal_validation.result_fingerprint,
        transcript_frontier_fingerprint=(
            frontier.provider_semantic_frontier_fingerprint
        ),
    )
    # Force evaluation so accidental attribution loss cannot hide behind an
    # unused local while building the transcript frontier.
    _ = transcript_owner_fingerprints
    return PreparedProviderInputStartBundle(
        prepared_candidate=prepared_candidate,
        companion_events=companions,
        committed_reference=reference,
        carrier=carrier,
        resident=resident,
        artifacts=artifacts,
        prepared_plan=prepared_plan,
    )


class ProviderInputRolloverPlanningRequired(RuntimeError):
    def __init__(self, intent: ProviderInputRolloverIntentFact) -> None:
        super().__init__(intent.reason.value)
        self.intent = intent


class ProviderInputRolloverRequired(RuntimeError):
    """Post-manifest dispatch signal carrying one fully joined request."""

    def __init__(self, request: ProviderInputRolloverRequestFact) -> None:
        super().__init__(request.intent.reason.value)
        self.request = request


def _compatibility_rollover_intent(
    *,
    predecessor: CommittedProviderInputGenerationCoreStateFact,
    resulting: ProviderInputGenerationCompatibilityFact,
    scope_fingerprint: str,
    projection_identity_fingerprint: str,
    resolved_model_call_id: str,
) -> ProviderInputRolloverIntentFact:
    previous = predecessor.generation.compatibility
    common = {
        "predecessor_generation_id": predecessor.generation.generation_id,
        "predecessor_core_state_fingerprint": predecessor.core_state_fingerprint,
        "ordered_projection_identity_fingerprint": projection_identity_fingerprint,
    }
    if previous.provider_visible != resulting.provider_visible:
        reason = ProviderInputRolloverReason.PROVIDER_VISIBLE_COMPATIBILITY_CHANGED
        authority = build_frozen_fact(
            ProviderCompatibilityChangeAuthorityFact,
            schema_version="provider_compatibility_change_authority.v1",
            **common,
            previous_provider_visible_compatibility_fingerprint=(
                previous.provider_visible.semantic_fingerprint
            ),
            resulting_provider_visible_compatibility_fingerprint=(
                resulting.provider_visible.semantic_fingerprint
            ),
            resolved_model_call_id=resolved_model_call_id,
        )
    elif (
        previous.system_instruction_semantic_fingerprint
        != resulting.system_instruction_semantic_fingerprint
    ):
        reason = ProviderInputRolloverReason.SYSTEM_ROOT_SEMANTIC_CHANGED
        authority = build_frozen_fact(
            ProviderSystemRootChangeAuthorityFact,
            schema_version="provider_system_root_change_authority.v1",
            **common,
            previous_system_root_semantic_fingerprint=(
                previous.system_instruction_semantic_fingerprint
            ),
            resulting_system_root_semantic_fingerprint=(
                resulting.system_instruction_semantic_fingerprint
            ),
        )
    elif (
        previous.tool_catalog_semantic_fingerprint
        != resulting.tool_catalog_semantic_fingerprint
    ):
        reason = ProviderInputRolloverReason.TOOL_CATALOG_SEMANTIC_CHANGED
        authority = build_frozen_fact(
            ProviderToolCatalogChangeAuthorityFact,
            schema_version="provider_tool_catalog_change_authority.v1",
            **common,
            previous_tool_catalog_semantic_fingerprint=(
                previous.tool_catalog_semantic_fingerprint
            ),
            resulting_tool_catalog_semantic_fingerprint=(
                resulting.tool_catalog_semantic_fingerprint
            ),
        )
    else:
        raise ValueError("provider compatibility changed without visible component drift")
    return build_frozen_fact(
        ProviderInputRolloverIntentFact,
        schema_version="provider_input_rollover_intent.v1",
        continuity_scope_fingerprint=scope_fingerprint,
        predecessor_generation_id=predecessor.generation.generation_id,
        reason=reason,
        authority=authority,
        authority_fingerprint=authority.authority_fingerprint,
    )


def _projection_extends_frontier(*, projection, frontier) -> bool:
    count = frontier.committed_transcript_unit_count
    if count > len(projection.ordered_units):
        return False
    prefix = projection.ordered_units[:count]
    return (
        frontier.committed_ordered_wire_semantic_accumulator
        == _ordered_fingerprint_accumulator(
            "provider-ordered-transcript-wire:v2",
            tuple(item.wire_semantic.wire_semantic_fingerprint for item in prefix),
        )
        and frontier.committed_ordered_causal_semantic_accumulator
        == _ordered_fingerprint_accumulator(
            "provider-ordered-transcript-causal:v2",
            tuple(item.unit_causal_semantic_fingerprint for item in prefix),
        )
    )


def _long_horizon_rollover_intent(
    *,
    predecessor: CommittedProviderInputGenerationCoreStateFact,
    scope_fingerprint: str,
    ordered_projection,
) -> ProviderInputRolloverIntentFact:
    summaries = tuple(
        item
        for item in ordered_projection.projection.ordered_units
        if isinstance(
            item.source_attribution,
            CompactionReplacementSummarySourceAttributionFact,
        )
    )
    if len(summaries) != 1:
        raise ValueError(
            "transcript prefix changed without one confirmed compaction authority"
        )
    summary = summaries[0]
    authority = build_frozen_fact(
        ProviderLongHorizonRewriteRolloverAuthorityFact,
        schema_version="provider_long_horizon_rewrite_rollover_authority.v1",
        predecessor_generation_id=predecessor.generation.generation_id,
        predecessor_core_state_fingerprint=predecessor.core_state_fingerprint,
        ordered_projection_identity_fingerprint=(
            ordered_projection.identity.identity_fingerprint
        ),
        rewrite_authority_reference=(
            summary.source_attribution.rewrite_authority_reference
        ),
        resulting_transcript_projection_semantic_fingerprint=(
            ordered_projection.projection.projection_semantic_fingerprint
        ),
    )
    return build_frozen_fact(
        ProviderInputRolloverIntentFact,
        schema_version="provider_input_rollover_intent.v1",
        continuity_scope_fingerprint=scope_fingerprint,
        predecessor_generation_id=predecessor.generation.generation_id,
        reason=ProviderInputRolloverReason.EXPLICIT_LONG_HORIZON_REWRITE,
        authority=authority,
        authority_fingerprint=authority.authority_fingerprint,
    )


def _removed_dynamic_source_keys(
    *,
    predecessor: CommittedProviderInputGenerationCoreStateFact,
    selected_candidates: tuple[ContextSectionCandidate, ...],
) -> tuple[tuple[ContextSourceId, str, str], ...]:
    selected = {
        (
            item.source_id,
            item.source_instance_id,
            item.attribution.semantic.candidate_key,
        )
        for item in selected_candidates
    }
    return tuple(
        sorted(
            (
                (item.source_id, item.source_instance_id, item.candidate_key)
                for item in predecessor.committed_source_heads
                if item.committed_append_index > 0
                and item.source_id is not ContextSourceId.RUNTIME_CLOCK
                and (
                    item.source_id,
                    item.source_instance_id,
                    item.candidate_key,
                )
                not in selected
            ),
            key=lambda item: (item[0].value, item[1], item[2]),
        )
    )


def _auxiliary_frame_rebase_rollover_intent(
    *,
    predecessor_snapshot: ProviderInputGenerationSnapshot,
    selected_candidates: tuple[ContextSectionCandidate, ...],
    scope_fingerprint: str,
    ordered_projection,
) -> ProviderInputRolloverIntentFact:
    predecessor = predecessor_snapshot.core_state
    resident = predecessor_snapshot.resident
    if predecessor is None or resident is None:
        raise ValueError("auxiliary frame rebase requires restored predecessor state")
    if not _projection_extends_frontier(
        projection=ordered_projection.projection,
        frontier=predecessor.transcript_frontier,
    ):
        raise ValueError("auxiliary frame rebase cannot rewrite transcript history")
    frames = predecessor_snapshot.frame_placements
    if not frames:
        raise ValueError("auxiliary frame rebase lacks durable frame attribution")
    retained_count = predecessor.transcript_frontier.committed_transcript_unit_count
    retained_units = ordered_projection.projection.ordered_units[:retained_count]
    retained_frontier = build_frozen_fact(
        ProviderTranscriptFrontierFact,
        schema_version="provider_transcript_frontier.v2",
        committed_transcript_unit_count=retained_count,
        committed_ordered_wire_semantic_accumulator=(
            _ordered_fingerprint_accumulator(
                "provider-ordered-transcript-wire:v2",
                tuple(
                    item.wire_semantic.wire_semantic_fingerprint
                    for item in retained_units
                ),
            )
        ),
        committed_ordered_causal_semantic_accumulator=(
            _ordered_fingerprint_accumulator(
                "provider-ordered-transcript-causal:v2",
                tuple(
                    item.unit_causal_semantic_fingerprint
                    for item in retained_units
                ),
            )
        ),
        stable_transcript_prefix_fingerprint=context_fingerprint(
            "provider-stable-transcript-prefix:v2",
            tuple(
                item.causal_placement.source.source_semantic_fingerprint
                for item in retained_units
            ),
        ),
    )
    if (
        retained_frontier.provider_semantic_frontier_fingerprint
        != predecessor.transcript_frontier.provider_semantic_frontier_fingerprint
    ):
        raise ValueError("auxiliary frame rebase retained prefix proof drifted")
    root_owner_fingerprints = {
        item.candidate_semantic_fingerprint
        for item in predecessor.committed_source_heads
        if item.committed_append_index == 0
    }
    dropped_units = tuple(
        item
        for item in resident.units
        if item.attribution.semantic.unit_kind in {"context_source", "runtime_clock"}
        and item.attribution.owner_semantic_fingerprint
        not in root_owner_fingerprints
    )
    if not dropped_units:
        raise ValueError("auxiliary frame rebase has no droppable provider units")
    previous_source_head_set = context_fingerprint(
        "provider-committed-source-head-set:v1",
        tuple(item.head_fingerprint for item in predecessor.committed_source_heads),
    )
    resulting_source_head_set = context_fingerprint(
        "provider-selected-source-head-set:v1",
        tuple(
            sorted(
                (
                    item.source_id.value,
                    item.source_instance_id,
                    item.attribution.semantic.candidate_key,
                    item.attribution.semantic.source_revision.revision_fingerprint,
                    item.semantic_fingerprint,
                )
                for item in selected_candidates
                if item.source_id is not ContextSourceId.RUNTIME_CLOCK
            )
        ),
    )
    dropped_ranges = tuple(
        sorted(
            set(
            context_fingerprint(
                "provider-auxiliary-frame-range:v1",
                (
                    item.frame_id,
                    item.first_vector_ordinal,
                    item.last_vector_ordinal,
                    item.ordered_source_unit_range_accumulator,
                ),
            )
            for item in frames
            )
        )
    )
    rebase_contract = context_fingerprint(
        "provider-auxiliary-frame-rebase-contract:v1",
        {
            "retained_transcript": "wire-and-causal-prefix-exact",
            "dropped_units": "non-root-context-frames-only",
            "replacement": "current-compiler-selected-sources",
        },
    )
    authority = build_frozen_fact(
        ProviderAuxiliaryFrameRebaseAuthorityFact,
        schema_version="provider_auxiliary_frame_rebase_authority.v1",
        predecessor_generation_id=predecessor.generation.generation_id,
        predecessor_core_state_fingerprint=predecessor.core_state_fingerprint,
        ordered_projection_identity_fingerprint=(
            ordered_projection.identity.identity_fingerprint
        ),
        dropped_frame_fact_fingerprints=tuple(
            sorted({item.frame_fact_fingerprint for item in frames})
        ),
        dropped_unit_range_fingerprints=dropped_ranges,
        dropped_unit_accumulator=_ordered_fingerprint_accumulator(
            "provider-auxiliary-frame-dropped-units:v1",
            tuple(
                item.attribution.semantic.semantic_fingerprint
                for item in dropped_units
            ),
        ),
        previous_source_head_set_fingerprint=previous_source_head_set,
        resulting_source_head_set_fingerprint=resulting_source_head_set,
        retained_transcript_unit_count=retained_count,
        predecessor_transcript_frontier_fingerprint=(
            predecessor.transcript_frontier.provider_semantic_frontier_fingerprint
        ),
        resulting_retained_transcript_prefix_fingerprint=(
            retained_frontier.provider_semantic_frontier_fingerprint
        ),
        budget_decision_fingerprint=context_fingerprint(
            "provider-auxiliary-frame-rebase-decision:v1",
            {
                "reason": "committed_dynamic_source_absent",
                "removed_source_keys": tuple(
                    (
                        source_id.value,
                        source_instance_id,
                        candidate_key,
                    )
                    for source_id, source_instance_id, candidate_key in (
                        _removed_dynamic_source_keys(
                            predecessor=predecessor,
                            selected_candidates=selected_candidates,
                        )
                    )
                ),
            },
        ),
        rebase_contract_fingerprint=rebase_contract,
    )
    return build_frozen_fact(
        ProviderInputRolloverIntentFact,
        schema_version="provider_input_rollover_intent.v1",
        continuity_scope_fingerprint=scope_fingerprint,
        predecessor_generation_id=predecessor.generation.generation_id,
        reason=ProviderInputRolloverReason.AUXILIARY_FRAME_REBASE,
        authority=authority,
        authority_fingerprint=authority.authority_fingerprint,
    )


def build_session_provider_input_continuity_scope(
    *,
    runtime_session_id: str,
    prepared_context_input: PreparedLiveContextSnapshot,
) -> SessionProviderInputContinuityScopeFact:
    run_entry = prepared_context_input.invocation.fact.run_entry
    call_lane = "subagent" if run_entry.run_entry_kind == "subagent" else "main_agent"
    subagent_id = (
        getattr(run_entry.run_entry, "subagent_run_id", None)
        if call_lane == "subagent"
        else None
    )
    cohort = context_fingerprint(
        "provider-input-session-continuity-cohort:v1",
        (runtime_session_id, call_lane, subagent_id),
    )
    return build_frozen_fact(
        SessionProviderInputContinuityScopeFact,
        schema_version="session_provider_input_continuity_scope.v1",
        runtime_session_id=runtime_session_id,
        call_lane=call_lane,
        subagent_id=subagent_id,
        compatibility_cohort_fingerprint=cohort,
    )


def _validate_planned_input_budget(
    *,
    call: ResolvedModelCall,
    carrier: RecursivelyImmutableProviderInputCarrier,
    template: LLMContext,
    retained_generation: bool,
) -> None:
    estimate = estimate_model_context_for_call(
        call=call,
        context=carrier.to_llm_context(template),
    )
    if estimate.total_input_tokens <= call.target.context_budget.input_budget_tokens:
        return
    if retained_generation:
        raise ProviderInputPhysicalPolicyError(
            ProviderInputPhysicalPolicyFailureReason.PROVIDER_INPUT_PHYSICAL_POLICY_UNSATISFIED,
            "retained provider prefix exceeds budget without rewrite authority",
        )
    raise ValueError(
        "fresh provider-input generation exceeds the resolved input budget"
    )


class ProviderInputResidentRestoreRequired(RuntimeError):
    def __init__(self, generation_id: str) -> None:
        self.generation_id = generation_id
        super().__init__(f"provider input generation {generation_id} requires restore")


def _continuation_materialization_proof(
    *,
    pending: ProviderInputPendingContinuationFact | None,
    predecessor_frontier: ProviderTranscriptFrontierFact | None,
    resulting_frontier: ProviderTranscriptFrontierFact,
    previous_unit_count: int,
    append_units,
    continuation_append_unit_index: int | None,
) -> ProviderInputContinuationMaterializationProofFact | None:
    if pending is None:
        if continuation_append_unit_index is not None:
            raise ValueError("continuation unit exists without pending authority")
        return None
    if predecessor_frontier is None:
        raise ValueError("pending continuation lacks predecessor transcript frontier")
    if continuation_append_unit_index is None:
        raise ValueError(
            "accepted continuation has no exact appended terminal transcript units"
        )
    selected_indices = (continuation_append_unit_index,)
    selected = tuple(append_units[index] for index in selected_indices)
    ordinals = tuple(previous_unit_count + index for index in selected_indices)
    semantics = tuple(
        item.attribution.semantic.semantic_fingerprint for item in selected
    )
    materializations = tuple(item.materialization_fingerprint for item in selected)
    owners = tuple(item.attribution.owner_semantic_fingerprint for item in selected)
    range_accumulator = context_fingerprint(
        "provider-input-continuation-unit-range:v1",
        tuple(zip(ordinals, semantics, materializations, owners, strict=True)),
    )
    return build_frozen_fact(
        ProviderInputContinuationMaterializationProofFact,
        schema_version="provider_input_continuation_materialization_proof.v1",
        pending_continuation_fingerprint=pending.continuation_fingerprint,
        terminal_projection_reference=pending.terminal_projection_reference,
        predecessor_transcript_frontier_fingerprint=(
            predecessor_frontier.provider_semantic_frontier_fingerprint
        ),
        resulting_transcript_frontier_fingerprint=(
            resulting_frontier.provider_semantic_frontier_fingerprint
        ),
        appended_unit_ordinals=ordinals,
        ordered_appended_unit_semantic_fingerprints=semantics,
        ordered_appended_unit_materialization_fingerprints=materializations,
        ordered_appended_unit_owner_semantic_fingerprints=owners,
        appended_unit_range_accumulator=range_accumulator,
    )


def _provider_lowering_replay_binding() -> ProviderInputReplayBindingIdentityFact:
    return build_frozen_fact(
        ProviderInputReplayBindingIdentityFact,
        schema_version="provider_input_replay_binding_identity.v1",
        binding_kind="provider_lowering",
        contract_id=LOWERING_CONTRACT_ID,
        contract_version=LOWERING_CONTRACT_VERSION,
        schema_or_contract_fingerprint=LOWERING_CONTRACT_FINGERPRINT,
    )


def _context_source_replay_binding(
    candidate: ContextSectionCandidate,
) -> ProviderInputReplayBindingIdentityFact:
    attribution = candidate.attribution
    return build_frozen_fact(
        ProviderInputReplayBindingIdentityFact,
        schema_version="provider_input_replay_binding_identity.v1",
        binding_kind="context_source",
        contract_id=attribution.source_contract_id,
        contract_version=attribution.source_contract_version,
        schema_or_contract_fingerprint=attribution.source_contract_fingerprint,
    )


def _ordered_replay_bindings(
    bindings: Iterable[ProviderInputReplayBindingIdentityFact],
) -> tuple[ProviderInputReplayBindingIdentityFact, ...]:
    return tuple(
        sorted(
            {item.identity_fingerprint: item for item in bindings}.values(),
            key=lambda item: item.identity_fingerprint,
        )
    )


def _validate_append_artifact_physical_bound(
    *,
    canonical_text: str,
    max_canonical_bytes: int,
) -> None:
    if max_canonical_bytes <= 0:
        raise ValueError("provider append canonical-byte bound must be positive")
    if len(canonical_text.encode("utf-8")) > max_canonical_bytes:
        raise ProviderInputPhysicalPolicyError(
            ProviderInputPhysicalPolicyFailureReason.PROVIDER_INPUT_APPEND_BYTE_BOUND_EXCEEDED,
            "provider input append exceeds resolved canonical-byte bound",
        )


def _compatibility(
    call: ResolvedModelCall, compiled_context: CompiledContext
) -> ProviderInputGenerationCompatibilityFact:
    target = call.fact.target
    provider_visible = build_frozen_fact(
        ProviderVisibleInputCompatibilityFact,
        schema_version="provider_visible_input_compatibility.v1",
        requested_model_identity=target.model_id,
        provider_api_kind=target.api,
        adapter_input_contract_id=target.transport_binding_id,
        adapter_input_contract_version=target.transport_contract_version,
        adapter_input_contract_fingerprint=target.provider_request_shape_fingerprint,
        tool_order_contract_fingerprint=context_fingerprint(
            "provider-tool-order-contract:v1", "ordered-as-resolved"
        ),
        transcript_lowering_contract_fingerprint=(
            compiled_context.transcript_provider_projection.rendering_contract.lowering_order_contract.contract_fingerprint
        ),
        context_source_lowering_contract_fingerprint=LOWERING_CONTRACT_FINGERPRINT,
        provider_input_framing_contract_fingerprint=(
            PROVIDER_INPUT_FRAMING_CONTRACT_FINGERPRINT
        ),
    )
    return build_frozen_fact(
        ProviderInputGenerationCompatibilityFact,
        schema_version="provider_input_generation_compatibility.v1",
        provider_visible=provider_visible,
        system_instruction_semantic_fingerprint=context_fingerprint(
            "provider-input-system-prompt:v1",
            compiled_context.llm_context.system_prompt,
        ),
        tool_catalog_semantic_fingerprint=context_fingerprint(
            "provider-input-tool-catalog:v1",
            tuple(
                tool_semantic_fingerprint(item)
                for item in compiled_context.llm_context.tools
            ),
        ),
    )


def _one_shot_compatibility(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
) -> ProviderInputGenerationCompatibilityFact:
    target = call.fact.target
    provider_visible = build_frozen_fact(
        ProviderVisibleInputCompatibilityFact,
        schema_version="provider_visible_input_compatibility.v1",
        requested_model_identity=target.model_id,
        provider_api_kind=target.api,
        adapter_input_contract_id=target.transport_binding_id,
        adapter_input_contract_version=target.transport_contract_version,
        adapter_input_contract_fingerprint=target.provider_request_shape_fingerprint,
        tool_order_contract_fingerprint=context_fingerprint(
            "provider-tool-order-contract:v1", "ordered-as-resolved"
        ),
        transcript_lowering_contract_fingerprint=context_fingerprint(
            "one-shot-transcript-lowering-contract:v1", "ordered-direct-messages"
        ),
        context_source_lowering_contract_fingerprint=LOWERING_CONTRACT_FINGERPRINT,
        provider_input_framing_contract_fingerprint=(
            PROVIDER_INPUT_FRAMING_CONTRACT_FINGERPRINT
        ),
    )
    return build_frozen_fact(
        ProviderInputGenerationCompatibilityFact,
        schema_version="provider_input_generation_compatibility.v1",
        provider_visible=provider_visible,
        system_instruction_semantic_fingerprint=context_fingerprint(
            "provider-input-system-prompt:v1", context.system_prompt
        ),
        tool_catalog_semantic_fingerprint=context_fingerprint(
            "provider-input-tool-catalog:v1",
            tuple(tool_semantic_fingerprint(item) for item in context.tools),
        ),
    )


def _empty_transcript_frontier(
    authority_horizon_set,
) -> ProviderTranscriptFrontierFact:
    del authority_horizon_set
    return build_frozen_fact(
        ProviderTranscriptFrontierFact,
        schema_version="provider_transcript_frontier.v2",
        committed_transcript_unit_count=0,
        committed_ordered_wire_semantic_accumulator=_ordered_fingerprint_accumulator(
            "provider-ordered-transcript-wire:v2", ()
        ),
        committed_ordered_causal_semantic_accumulator=_ordered_fingerprint_accumulator(
            "provider-ordered-transcript-causal:v2", ()
        ),
        stable_transcript_prefix_fingerprint=context_fingerprint(
            "provider-stable-transcript-prefix:v2", ()
        ),
    )


def _provider_input_identity(
    *,
    carrier: RecursivelyImmutableProviderInputCarrier,
    vector_root,
) -> ProviderInputSemanticIdentityFact:
    return build_frozen_fact(
        ProviderInputSemanticIdentityFact,
        schema_version="provider_input_semantic_identity.v1",
        input_unit_count=vector_root.unit_count,
        ordered_unit_accumulator=vector_root.ordered_unit_accumulator,
        unit_vector_semantic_fingerprint=vector_root.vector_semantic_fingerprint,
        system_instruction_fingerprint=context_fingerprint(
            "provider-input-system-prompt:v1", carrier.system_prompt
        ),
        tool_catalog_fingerprint=context_fingerprint(
            "provider-input-tool-catalog:v1",
            tuple(
                tool_fragment_semantic_fingerprint(item)
                for item in carrier.ordered_tool_fragments
            ),
        ),
        provider_message_sequence_fingerprint=context_fingerprint(
            "provider-input-message-sequence:v1",
            tuple(
                message_semantic_fingerprint(item) for item in carrier.ordered_messages
            ),
        ),
    )


def _preparation_id(
    *,
    scope_fingerprint: str,
    generation_id: str,
    resolved_model_call_id: str,
    append_index: int,
    ownership_kind: str,
) -> str:
    digest = context_fingerprint(
        "provider-input-preparation-id:v1",
        {
            "scope_fingerprint": scope_fingerprint,
            "generation_id": generation_id,
            "resolved_model_call_id": resolved_model_call_id,
            "append_index": append_index,
            "ownership_kind": ownership_kind,
        },
    ).removeprefix("sha256:")
    return f"provider-input-preparation:{digest}"


def _selected_compiled_source_candidates(
    *,
    compiled_context: CompiledContext,
    prepared_context_input: PreparedLiveContextSnapshot,
) -> tuple[ContextSectionCandidate, ...]:
    """Adopt only compiler-accepted source fragments as provider truth."""

    prepared_by_fingerprint = {
        entry.candidate.semantic_fingerprint: entry.candidate
        for entry in prepared_context_input.prepared_candidates.entries
    }
    if len(prepared_by_fingerprint) != len(
        prepared_context_input.prepared_candidates.entries
    ):
        raise ValueError("prepared ContextSource semantic identities are duplicated")
    selected: list[ContextSectionCandidate] = []
    for fragment in compiled_context.provider_source_fragments:
        candidate = prepared_by_fingerprint.get(fragment.owner_semantic_fingerprint)
        if candidate is None or candidate != fragment.candidate:
            raise ValueError("compiled provider source has no exact prepared owner")
        selected.append(candidate)
    identities = tuple(item.semantic_fingerprint for item in selected)
    if len(identities) != len(set(identities)):
        raise ValueError("compiled provider source owner is duplicated")
    return tuple(selected)


def _root_units(
    *,
    compiled_context: CompiledContext,
    required_replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...],
    prepared_context_input: PreparedLiveContextSnapshot,
):
    units = []
    for fragment in compiled_context.provider_source_fragments:
        candidate = fragment.candidate
        if fragment.provider_lane != "system_prompt":
            continue
        if not isinstance(
            candidate.attribution.semantic.lifecycle,
            GenerationRootLifecycleFact,
        ):
            raise ValueError(
                "mutable system source cannot be appended without typed root authority"
            )
        units.append(
            freeze_message_unit(
                fragment.message,
                unit_kind="context_source",
                owner_semantic_fingerprint=candidate.semantic_fingerprint,
                authority_horizons=candidate.attribution.authority_horizons,
                estimated_tokens=fragment.estimated_tokens,
                source_event_refs=candidate.attribution.source_event_refs,
                source_artifact_refs=candidate.attribution.source_artifact_refs,
                required_replay_bindings=_ordered_replay_bindings(
                    (
                        _provider_lowering_replay_binding(),
                        _context_source_replay_binding(candidate),
                    )
                ),
            )
        )
    if "\n\n".join(
        fragment.message.content[0]
        for fragment in compiled_context.provider_source_fragments
        if fragment.provider_lane == "system_prompt"
    ) != (compiled_context.llm_context.system_prompt or ""):
        raise ValueError("provider system fragment lowering drifted")
    materialized_tools = prepared_context_input.invocation.materialized_tool_specs
    if len(materialized_tools) != len(compiled_context.llm_context.tools):
        raise ValueError("compiled tool catalog/materialization count drifted")
    tool_horizons = prepared_context_input.invocation.fact.capability_tool_catalog_root.authority_horizons
    for tool, materialized in zip(
        compiled_context.llm_context.tools,
        materialized_tools,
        strict=True,
    ):
        attribution = materialized.fact.descriptor_render_attribution
        if attribution.descriptor_id != materialized.fact.descriptor_id:
            raise ValueError("compiled tool descriptor attribution drifted")
        units.append(
            freeze_tool_unit(
                tool,
                authority_horizons=tool_horizons,
                estimated_tokens=0,
                source_event_refs=(
                    ContextEventReferenceFact(
                        runtime_session_id=attribution.owner_runtime_session_id,
                        event_id=attribution.descriptor_source_event_id,
                        sequence=attribution.descriptor_source_sequence,
                        event_type="CAPABILITY_EXPOSURE_RESOLVED",
                        payload_fingerprint=(
                            attribution.descriptor_source_payload_fingerprint
                        ),
                    ),
                ),
                required_replay_bindings=required_replay_bindings,
            )
        )
    for fragment in compiled_context.provider_source_fragments:
        candidate = fragment.candidate
        intent_kind = candidate.attribution.semantic.lowering_intent.intent_kind
        if (
            not isinstance(
                candidate.attribution.semantic.lifecycle,
                GenerationRootLifecycleFact,
            )
            or fragment.provider_lane == "system_prompt"
        ):
            continue
        if intent_kind != "leading_context":
            raise ValueError(
                "non-leading generation-root source cannot be placed before history"
            )
        units.append(
            freeze_message_unit(
                fragment.message,
                unit_kind="context_source",
                owner_semantic_fingerprint=candidate.semantic_fingerprint,
                authority_horizons=candidate.attribution.authority_horizons,
                estimated_tokens=fragment.estimated_tokens,
                source_event_refs=candidate.attribution.source_event_refs,
                source_artifact_refs=candidate.attribution.source_artifact_refs,
                required_replay_bindings=_ordered_replay_bindings(
                    (
                        _provider_lowering_replay_binding(),
                        _context_source_replay_binding(candidate),
                    )
                ),
            )
        )
    return tuple(units)


def _horizons_for_refs(
    refs: tuple[ContextEventReferenceFact, ...],
    available: tuple[LedgerAuthorityHorizonFact, ...],
) -> tuple[LedgerAuthorityHorizonFact, ...]:
    owners = {ref.runtime_session_id for ref in refs}
    selected = tuple(
        horizon for horizon in available if horizon.runtime_session_id in owners
    )
    if {item.runtime_session_id for item in selected} != owners:
        raise ValueError("provider unit source refs lack canonical ledger horizons")
    return selected


def _new_transcript_units(
    *,
    compiled_context: CompiledContext,
    horizons: tuple[LedgerAuthorityHorizonFact, ...],
    previous_units,
    initial: bool,
    required_replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...],
    pending: ProviderInputPendingContinuationFact | None,
    pending_materialization: PreparedProviderInputContinuationMaterialization | None,
):
    prepared = compiled_context.prepared_ordered_transcript_projection
    if prepared is None:
        raise ValueError("compiled context lacks ordered transcript projection")
    projection = prepared.projection
    ordered_units = projection.ordered_units
    owner_fingerprints = tuple(
        item.unit_causal_semantic_fingerprint for item in ordered_units
    )
    previous_transcript = tuple(
        item
        for item in previous_units
        if item.attribution.semantic.unit_kind == "transcript_message"
    )
    if not initial:
        if len(previous_transcript) > len(ordered_units):
            raise ValueError("transcript frontier moved backwards without rewrite authority")
        for index, prior in enumerate(previous_transcript):
            if (
                prior.attribution.owner_semantic_fingerprint
                != owner_fingerprints[index]
                or prior.canonical_provider_fragment
                != ordered_units[index].wire_semantic.provider_message
            ):
                raise ValueError(
                    "transcript frontier changed without confirmed rewrite authority"
                )
    start = 0 if initial else len(previous_transcript)
    new_units = tuple(
        freeze_ordered_transcript_unit(
            ordered_units[index],
            authority_horizons=_horizons_for_refs(
                ordered_transcript_unit_source_event_refs(ordered_units[index]),
                horizons,
            ),
            estimated_tokens=0,
            required_replay_bindings=required_replay_bindings,
        )
        for index in range(start, len(ordered_units))
    )
    if pending is None:
        if pending_materialization is not None:
            raise ValueError("continuation materialization lacks pending authority")
        return new_units, owner_fingerprints, None, None
    if (
        pending_materialization is None
        or pending_materialization.pending_continuation_fingerprint
        != pending.continuation_fingerprint
        or pending_materialization.terminal_projection_reference_fingerprint
        != pending.terminal_projection_reference.reference_fingerprint
        or pending_materialization.provider_message_semantic_fingerprint
        != message_semantic_fingerprint(pending_materialization.provider_message)
    ):
        raise ValueError("pending continuation materialization identity drifted")
    matching_projection_indices = tuple(
        index
        for index, ordered in enumerate(ordered_units)
        if isinstance(
            ordered.source_attribution,
            DirectStableMessageSourceAttributionFact,
        )
        and ordered.source_attribution.stable_leaf_reference.entry_fact_fingerprint
        == pending_materialization.matched_stable_entry_fact_fingerprint
        and ordered.source_attribution.stable_leaf_reference.entry_semantic_fingerprint
        == pending_materialization.matched_stable_entry_semantic_fingerprint
        and ordered.wire_semantic.provider_message
        == freeze_provider_message_fragment(pending_materialization.provider_message)
    )
    if len(matching_projection_indices) != 1:
        raise ValueError(
            "accepted continuation does not match one exact ordered projection unit "
            f"(matches={matching_projection_indices!r}, "
            f"stable_fact={pending_materialization.matched_stable_entry_fact_fingerprint}, "
            f"projection_sources={tuple((item.source_attribution.source_kind, getattr(getattr(item.source_attribution, 'stable_leaf_reference', None), 'entry_fact_fingerprint', None), item.invocation_attribution.invocation_classification) for item in ordered_units)!r})"
        )
    matched_projection_index = matching_projection_indices[0]
    if matched_projection_index < start:
        raise ValueError("pending continuation is already inside committed frontier")
    continuation_index = matched_projection_index - start
    matched = ordered_units[matched_projection_index]
    join = build_frozen_fact(
        ProviderAcceptedContinuationProjectionJoinFact,
        schema_version="provider_accepted_continuation_projection_join.v1",
        resolved_model_call_id=pending.resolved_model_call_id,
        reply_id=pending_materialization.reply_id,
        terminal_projection_reference=pending.terminal_projection_reference,
        accepted_disposition_event_reference=pending.accepted_disposition_event_ref,
        ordered_projection_identity_fingerprint=prepared.identity.identity_fingerprint,
        matched_projection_index=matched_projection_index,
        matched_unit_causal_semantic_fingerprint=(
            matched.unit_causal_semantic_fingerprint
        ),
        continuation_join_contract_fingerprint=(
            CONTINUATION_JOIN_CONTRACT_FINGERPRINT
        ),
    )
    return new_units, owner_fingerprints, continuation_index, join


def _arrange_transcript_delta_and_frame(
    *,
    compiled_context: CompiledContext,
    transcript_units,
    frame_units,
    changed_candidates: tuple[ContextSectionCandidate, ...],
    previous_unit_count: int,
    generation_id: str,
    resolved_model_call_id: str,
    model_call_index: int | None,
):
    """Insert one invocation frame without changing transcript delta order."""

    prepared = compiled_context.prepared_ordered_transcript_projection
    if prepared is None:
        raise ValueError("ordered transcript projection is required")
    projection_units = prepared.projection.ordered_units
    delta_start = len(projection_units) - len(transcript_units)
    if delta_start < 0:
        raise ValueError("provider transcript delta exceeds ordered projection")
    current_offsets = tuple(
        index - delta_start
        for index, item in enumerate(projection_units)
        if index >= delta_start
        and item.invocation_attribution.invocation_classification == "current_user"
    )
    if len(current_offsets) > 1:
        raise ValueError("provider transcript delta has multiple current-user units")
    if not frame_units:
        return tuple(transcript_units), None
    if model_call_index is None:
        raise ValueError("compiled provider context lacks model-call index")
    if current_offsets:
        offset = current_offsets[0]
        before = tuple(transcript_units[:offset])
        after = tuple(transcript_units[offset:])
        insertion_kind = "before_new_current_user"
        absolute_index = delta_start + offset
        preceding = (
            projection_units[absolute_index - 1]
            .causal_placement.node_identity.node_identity_fingerprint
            if absolute_index > 0
            else None
        )
        following = projection_units[
            absolute_index
        ].causal_placement.node_identity.node_identity_fingerprint
        append_units = (*before, *frame_units, *after)
        frame_offset = len(before)
    else:
        append_units = (*transcript_units, *frame_units)
        insertion_kind = "after_new_transcript_tail"
        preceding = (
            projection_units[-1]
            .causal_placement.node_identity.node_identity_fingerprint
            if projection_units
            else None
        )
        following = None
        frame_offset = len(transcript_units)
    wire_fingerprints = tuple(
        item.attribution.semantic.provider_content_semantic_fingerprint
        for item in frame_units
    )
    source_head_set_fingerprint = context_fingerprint(
        "provider-invocation-frame-source-head-set:v1",
        tuple(
            (
                item.source_id.value,
                item.source_instance_id,
                item.attribution.semantic.candidate_key,
                item.attribution.semantic.source_revision,
                item.semantic_fingerprint,
            )
            for item in changed_candidates
        ),
    )
    semantic = build_frozen_fact(
        ProviderInvocationContextFrameSemanticFact,
        schema_version="provider_invocation_context_frame_semantic.v1",
        ordered_source_unit_wire_fingerprints=wire_fingerprints,
        source_head_set_fingerprint=source_head_set_fingerprint,
    )
    first_ordinal = previous_unit_count + frame_offset
    frame_id_digest = context_fingerprint(
        "provider-invocation-context-frame-id:v1",
        (
            generation_id,
            resolved_model_call_id,
            semantic.frame_semantic_fingerprint,
            insertion_kind,
            preceding,
            following,
        ),
    ).removeprefix("sha256:")
    placement = build_frozen_fact(
        ProviderInvocationContextFramePlacementFact,
        schema_version="provider_invocation_context_frame_placement.v1",
        semantic=semantic,
        insertion_kind=insertion_kind,
        preceding_transcript_node_identity_fingerprint=preceding,
        following_transcript_node_identity_fingerprint=following,
        insertion_policy_id=PROVIDER_CONTEXT_FRAME_INSERTION_POLICY_ID,
        insertion_policy_version=PROVIDER_CONTEXT_FRAME_INSERTION_POLICY_VERSION,
        insertion_policy_fingerprint=(
            PROVIDER_CONTEXT_FRAME_INSERTION_POLICY_FINGERPRINT
        ),
        frame_id=f"provider-input-frame:{frame_id_digest[:32]}",
        generation_id=generation_id,
        resolved_model_call_id=resolved_model_call_id,
        model_call_index=model_call_index,
        first_vector_ordinal=first_ordinal,
        last_vector_ordinal=first_ordinal + len(frame_units) - 1,
        ordered_source_unit_range_accumulator=_ordered_fingerprint_accumulator(
            "provider-invocation-context-frame-wire:v1", wire_fingerprints
        ),
    )
    return tuple(append_units), placement


def _changed_source_units(
    *,
    compiled_context: CompiledContext,
    predecessor: CommittedProviderInputGenerationCoreStateFact | None,
    initial: bool,
    provider_lowering_binding: ProviderInputReplayBindingIdentityFact,
):
    previous = {
        (item.source_id, item.source_instance_id, item.candidate_key): item
        for item in (predecessor.committed_source_heads if predecessor else ())
    }
    changed_fragments = tuple(
        fragment
        for fragment in compiled_context.provider_source_fragments
        for candidate in (fragment.candidate,)
        if candidate.lowering_kind != "system_instruction"
        and not isinstance(
            candidate.attribution.semantic.lifecycle,
            GenerationRootLifecycleFact,
        )
        and (
            initial
            or previous.get(
                (
                    candidate.source_id,
                    candidate.source_instance_id,
                    candidate.attribution.semantic.candidate_key,
                )
            )
            is None
            or previous[
                (
                    candidate.source_id,
                    candidate.source_instance_id,
                    candidate.attribution.semantic.candidate_key,
                )
            ].canonical_source_revision
            != candidate.attribution.semantic.source_revision
        )
    )
    if not changed_fragments:
        return (), ()
    changed_fragments = tuple(
        sorted(
            changed_fragments,
            key=lambda item: _source_append_sort_key(item.candidate),
        )
    )
    units = []
    for fragment in changed_fragments:
        candidate = fragment.candidate
        required_replay_bindings = _ordered_replay_bindings(
            (
                provider_lowering_binding,
                _context_source_replay_binding(candidate),
            )
        )
        units.append(
            freeze_message_unit(
                fragment.message,
                unit_kind=(
                    "runtime_clock"
                    if candidate.source_id is ContextSourceId.RUNTIME_CLOCK
                    else "context_source"
                ),
                owner_semantic_fingerprint=candidate.semantic_fingerprint,
                authority_horizons=candidate.attribution.authority_horizons,
                estimated_tokens=fragment.estimated_tokens,
                source_event_refs=candidate.attribution.source_event_refs,
                source_artifact_refs=candidate.attribution.source_artifact_refs,
                required_replay_bindings=required_replay_bindings,
            )
        )
    return tuple(units), tuple(item.candidate for item in changed_fragments)


def _source_append_sort_key(candidate: ContextSectionCandidate):
    lane_order = {
        "leading_context": 0,
        "paired_observation": 1,
        "trailing_observation": 2,
        "status_observation": 3,
    }
    return (
        1 if candidate.source_id is ContextSourceId.RUNTIME_CLOCK else 0,
        lane_order.get(
            candidate.attribution.semantic.lowering_intent.intent_kind,
            99,
        ),
        candidate.priority,
        candidate.source_id.value,
        candidate.source_instance_id,
        candidate.attribution.semantic.candidate_key,
    )


def _validate_generation_root_sources(
    *,
    selected_candidates: tuple[ContextSectionCandidate, ...],
    predecessor: CommittedProviderInputGenerationCoreStateFact,
) -> None:
    previous = {
        (item.source_id, item.source_instance_id, item.candidate_key): item
        for item in predecessor.committed_source_heads
    }
    for candidate in selected_candidates:
        if not isinstance(
            candidate.attribution.semantic.lifecycle,
            GenerationRootLifecycleFact,
        ):
            continue
        key = (
            candidate.source_id,
            candidate.source_instance_id,
            candidate.attribution.semantic.candidate_key,
        )
        head = previous.get(key)
        if (
            head is None
            or head.canonical_source_revision
            != candidate.attribution.semantic.source_revision
            or head.candidate_semantic_fingerprint != candidate.semantic_fingerprint
        ):
            raise ValueError(
                "provider generation-root ContextSource changed without typed authority"
            )


def _transcript_frontier(
    *,
    compiled_context: CompiledContext,
    predecessor: ProviderTranscriptFrontierFact | None,
    committed_count: int,
    authority_horizon_set,
):
    del authority_horizon_set
    prepared = compiled_context.prepared_ordered_transcript_projection
    if prepared is None:
        raise ValueError("ordered transcript projection is required")
    units = prepared.projection.ordered_units[:committed_count]
    wire = tuple(item.wire_semantic.wire_semantic_fingerprint for item in units)
    causal = tuple(item.unit_causal_semantic_fingerprint for item in units)
    stable_sources = tuple(
        item.causal_placement.source.source_semantic_fingerprint for item in units
    )
    if predecessor is not None and (
        predecessor.committed_transcript_unit_count > committed_count
        or predecessor.committed_ordered_wire_semantic_accumulator
        != _ordered_fingerprint_accumulator(
            "provider-ordered-transcript-wire:v2",
            wire[: predecessor.committed_transcript_unit_count],
        )
        or predecessor.committed_ordered_causal_semantic_accumulator
        != _ordered_fingerprint_accumulator(
            "provider-ordered-transcript-causal:v2",
            causal[: predecessor.committed_transcript_unit_count],
        )
    ):
        raise ValueError("transcript frontier prefix changed after rollover planning")
    return build_frozen_fact(
        ProviderTranscriptFrontierFact,
        schema_version="provider_transcript_frontier.v2",
        committed_transcript_unit_count=committed_count,
        committed_ordered_wire_semantic_accumulator=_ordered_fingerprint_accumulator(
            "provider-ordered-transcript-wire:v2", wire
        ),
        committed_ordered_causal_semantic_accumulator=_ordered_fingerprint_accumulator(
            "provider-ordered-transcript-causal:v2", causal
        ),
        stable_transcript_prefix_fingerprint=context_fingerprint(
            "provider-stable-transcript-prefix:v2", stable_sources
        ),
    )


def _ordered_fingerprint_accumulator(domain: str, values) -> str:
    accumulator = context_fingerprint(f"{domain}:empty", ())
    for value in values:
        accumulator = context_fingerprint(f"{domain}:step", (accumulator, value))
    return accumulator


def _source_heads(
    *,
    selected_candidates: tuple[ContextSectionCandidate, ...],
    predecessor: CommittedProviderInputGenerationCoreStateFact | None,
    changed_candidates: tuple[ContextSectionCandidate, ...],
    available_source_units,
    append_index: int,
):
    previous = {
        (item.source_id, item.source_instance_id, item.candidate_key): item
        for item in (predecessor.committed_source_heads if predecessor else ())
    }
    changed_keys = {
        (
            item.source_id,
            item.source_instance_id,
            item.attribution.semantic.candidate_key,
        )
        for item in changed_candidates
    }
    source_units_by_owner = {
        item.attribution.owner_semantic_fingerprint: (
            item.attribution.semantic.semantic_fingerprint
        )
        for item in available_source_units
    }
    for candidate in selected_candidates:
        if candidate.source_id is ContextSourceId.RUNTIME_CLOCK:
            continue
        key = (
            candidate.source_id,
            candidate.source_instance_id,
            candidate.attribution.semantic.candidate_key,
        )
        if key in previous and key not in changed_keys:
            continue
        if isinstance(
            candidate.attribution.semantic.lifecycle,
            GenerationRootLifecycleFact,
        ):
            try:
                appended = source_units_by_owner[candidate.semantic_fingerprint]
            except KeyError as exc:
                raise ValueError(
                    "generation-root source lacks its exact root unit"
                ) from exc
            committed_index = 0
        elif key in changed_keys:
            try:
                appended = source_units_by_owner[candidate.semantic_fingerprint]
            except KeyError as exc:
                raise ValueError(
                    "changed ContextSource lacks its exact provider unit"
                ) from exc
            committed_index = append_index
        else:
            continue
        previous[key] = build_frozen_fact(
            ProviderInputCommittedSourceHeadFact,
            schema_version="provider_input_committed_source_head.v1",
            source_id=candidate.source_id,
            source_instance_id=candidate.source_instance_id,
            candidate_key=candidate.attribution.semantic.candidate_key,
            canonical_source_revision=candidate.attribution.semantic.source_revision,
            candidate_semantic_fingerprint=candidate.semantic_fingerprint,
            appended_unit_semantic_fingerprint=appended,
            committed_append_index=committed_index,
        )
    return tuple(
        previous[key]
        for key in sorted(previous, key=lambda item: (item[0].value, item[1], item[2]))
    )


def _clock_head(
    *,
    selected_candidates: tuple[ContextSectionCandidate, ...],
    predecessor: CommittedProviderInputGenerationCoreStateFact | None,
    append_index: int,
):
    clock = next(
        (
            item
            for item in selected_candidates
            if item.source_id is ContextSourceId.RUNTIME_CLOCK
        ),
        None,
    )
    if clock is None:
        return predecessor.clock_head if predecessor else None
    payload = clock.attribution.semantic.payload
    if not isinstance(payload, RuntimeClockProposalPayloadFact):
        raise ValueError("runtime clock source has an invalid semantic payload")
    if (
        predecessor is not None
        and predecessor.clock_head is not None
        and (
            predecessor.clock_head.observation_semantic_fingerprint
            == clock.semantic_fingerprint
        )
    ):
        return predecessor.clock_head
    return build_frozen_fact(
        ProviderInputClockHeadFact,
        schema_version="provider_input_clock_head.v1",
        observation_semantic_fingerprint=clock.semantic_fingerprint,
        observed_at_utc=payload.observed_at_utc,
        committed_append_index=append_index,
    )


def _merge_horizons(
    horizons: Iterable[LedgerAuthorityHorizonFact],
) -> tuple[LedgerAuthorityHorizonFact, ...]:
    by_owner: dict[str, LedgerAuthorityHorizonFact] = {}
    for horizon in horizons:
        current = by_owner.get(horizon.runtime_session_id)
        if current is None or horizon.through_sequence > current.through_sequence:
            by_owner[horizon.runtime_session_id] = horizon
        elif (
            horizon.through_sequence == current.through_sequence and horizon != current
        ):
            raise ValueError("same ledger horizon has conflicting continuity proof")
    return tuple(by_owner[key] for key in sorted(by_owner))


def _copy_core_state(
    core: CommittedProviderInputGenerationCoreStateFact,
    **updates,
) -> CommittedProviderInputGenerationCoreStateFact:
    payload = {
        name: getattr(core, name)
        for name in core.__class__.model_fields
        if name not in {"schema_version", "core_state_fingerprint"}
    }
    payload.update(updates)
    return build_frozen_fact(
        CommittedProviderInputGenerationCoreStateFact,
        schema_version="committed_provider_input_generation_core_state.v1",
        **payload,
    )


def _generation_id(
    scope_fingerprint: str,
    compatibility,
    *,
    identity_predecessor_generation_id: str | None = None,
    root_semantic_fingerprint: str | None = None,
) -> str:
    digest = context_fingerprint(
        "provider-input-generation-id:v1",
        [
            scope_fingerprint,
            compatibility.compatibility_fingerprint,
            identity_predecessor_generation_id,
            root_semantic_fingerprint,
        ],
    ).removeprefix("sha256:")
    return f"provider-input-generation:{digest[:32]}"


def _unique_artifacts(artifacts):
    by_id: dict[str, PreparedProviderInputArtifact] = {}
    for artifact in artifacts:
        current = by_id.get(artifact.artifact_reference.artifact_id)
        if current is not None and current != artifact:
            raise ValueError("provider input artifact ID collision")
        by_id[artifact.artifact_reference.artifact_id] = artifact
    return tuple(by_id[key] for key in sorted(by_id))


__all__ = [
    "PreparedProviderInputStartBundle",
    "ProviderInputResidentRestoreRequired",
    "ProviderInputRolloverRequired",
    "plan_provider_input_append",
]
