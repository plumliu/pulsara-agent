"""Round 5B compaction execution and canonical adoption coordinator."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field as dataclass_field, replace

from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal, Protocol
from uuid import uuid4


from time import monotonic


from pulsara_agent.conversation_kernel.assembler import (
    MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES,
)
from pulsara_agent.conversation_kernel.contracts import ConversationScopeKind


from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
)

from pulsara_agent.conversation_kernel.context_sources import (
    build_compaction_context_source,
)

from pulsara_agent.conversation_kernel.compaction.contracts import (
    COMPACTION_MODEL_CONTRACT,
    COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
    COMPACTION_SUMMARY_PROMPT_CONTRACT,
    CONTEXT_SNAPSHOT_CODEC,
    CONTEXT_SNAPSHOT_MEDIA_TYPE,
    CompactionCanonicalAdoptionFactoryInput,
    CompactionCanonicalWritePreconditions,
    CompactionActiveRequestLocation,
    CompactionContinuationMode,
    CompactionAttemptPhase,
    CompactionConfirmationKind,
    CompactionDisposition,
    CompactionOutcome,
    CompactionScope,
    CompactionSnapshotCarrier,
    CompactionTargetBranch,
    CompactionTrigger,
    ExpectedCompactionPredecessorRevision,
    FrozenCompactionCanonicalRead,
    FrozenCompactionActiveRequest,
    FrozenRetainedHistoricalRequest,
    FrozenCompactionSourceView,
    ProtectedTailSelectionFact,
    ProviderPrefixCutProof,
    RecentHumanMessageProof,
    PreparedCompactionCanonicalAdoption,
    manual_compaction_stable_suffix,
    build_prepared_compaction_canonical_adoption,
    canonical_compaction_range_digest,
    freeze_compaction_canonical_range,
    provider_input_item_canonical_expanded_bytes,
)

from pulsara_agent.conversation_kernel.compaction.model_call import (
    PreparedCompactionSummarySemantic,
    promote_compaction_summary_call,
    prepare_compaction_summary_repair_semantic,
    prepare_compaction_summary_semantic,
    prepare_destination_projection_summary_semantic,
)

from pulsara_agent.conversation_kernel.compaction.planner import (
    DestinationDialogueProjection,
    build_synthetic_compaction_dispatch_read,
    compaction_effective_history_has_image,
    CompactionPlanningError,
    CompactionReclaimUnavailable,
    NoSafeCompactionSummaryPrefix,
    crosses_compaction_resource_headroom,
    enumerate_destination_backbone_projections,
    enumerate_complete_tool_groups,
    enumerate_safe_summary_prefixes,
    freeze_compaction_continuation,
    freeze_compaction_source_view,
    freeze_destination_dialogue_projection_plan,
    freeze_tail_and_prefix,
    project_compaction_read_for_text_only_handover,
    project_destination_dialogue_for_text_only_handover,
    project_destination_dialogue_plan_for_text_only_handover,
    project_retained_request_for_text_only_handover,
    provider_dispatch_effective_history_has_image,
    recent_human_window_has_image,
    retain_destination_tool_evidence,
    select_recent_human_messages,
    should_trigger_compaction,
    validate_compaction_reclaim,
)

from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    compaction_summary_request,
    freeze_compaction_summary_output,
)

from pulsara_agent.conversation_kernel.compaction.runtime import (
    CompactionWriteReservation,
    HostCompactionRuntimeOwner,
    ManualCompactionRequest,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    NoContinuationProductEvidence,
    _issue_durable_turn_not_running_evidence,
    _issue_idle_target_no_continuation_evidence,
    _issue_post_compact_blocked_evidence,
    _issue_session_start_compact_blocked_evidence,
    _issue_successor_final_abandonment_evidence,
)

from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    FrozenCompactionRuntimeHandoff,
)

from pulsara_agent.conversation_kernel.cold_epoch import (
    AdoptedCompactionContinuationSeed,
    CanonicalColdContinuationSeed,
    CompactionDryProjectionSeed,
    _issue_adopted_compaction_continuation_seed,
)


from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
)

from pulsara_agent.conversation_kernel.contracts import (
    TurnStatus,
    WriterLease,
)


from pulsara_agent.conversation_kernel.io import KernelSessionIO

from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)


from pulsara_agent.conversation_kernel.workspace import SessionWorkspaceResolver
from pulsara_agent.conversation_kernel.memory.contracts import (
    MemoryUsePolicy,
)


from pulsara_agent.conversation_kernel.tool_contracts import (
    ToolSurfacePlanningPort,
)

from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelRepository,
    ConversationKernelConflict,
    StaleHostWriter,
)


from pulsara_agent.conversation_kernel.provider_dispatch import (
    KernelModelPort,
    PreparedCompactionCandidate,
    PreparedCompactionCandidateFamily,
    PreparedCompactionDryProjection,
    PendingEmptyCompactionDrySource,
    PreparedCompactionSourceDispatch,
    PreparedProviderDispatch,
    PreparedProviderHeadroomAdmission,
    PreparedProspectiveActiveRootInput,
    PreparedProspectiveRootCandidate,
    PreparedProspectiveRootCandidateFamily,
    PreparedProspectiveRootDispatch,
    PreparedProviderWireCandidate,
    PreparedWireMeasurementDecision,
    HandleFreeProviderWireObservation,
    ProviderDispatchCoordinator,
)


from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderInputReader,
)
from pulsara_agent.conversation_kernel.steer import (
    PreparedActiveRootInputAdmission,
    PreparedActiveRootInputCandidate,
    PreparedRootProviderInputCandidate,
)

from pulsara_agent.conversation_kernel.safe_point import (
    PreparedProviderInputHandle,
    ProviderSafePointCoordinator,
)


from pulsara_agent.model_input.compiler import (
    StructuredModelInputCompiler,
)


from pulsara_agent.model_input.contracts import (
    ContextBindingBaseKind,
    ContextSourceAbsentFact,
    ContextSourceAbsenceKind,
    ContextSourceCandidate,
    ContextSourceKind,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    ModelInputCompileFailureKind,
    PreparedProviderInputCut,
    StructuredModelInputCompileError,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
)

from pulsara_agent.model_input.continuity import (
    FrozenDirectSwitchAdmission,
    ProviderInputContinuityScope,
    provider_input_logical_bytes,
)
from pulsara_agent.llm.request import (
    MAXIMUM_PROVIDER_WIRE_INPUT_BYTES,
    FrozenProviderWireInputPlan,
    FrozenProviderWireInputQuote,
)
from pulsara_agent.llm.errors import ModelTargetCapabilityMismatch
from pulsara_agent.llm.input import content_has_image
from pulsara_agent.ports.provider_stream import (
    ProviderModelExecutionFailed,
    ProviderModelOutputIncomplete,
)
from pulsara_agent.conversation_kernel.direct_model import PreparedKernelModelTarget
from pulsara_agent.llm.frozen_target import FrozenEpochModelTargetBundle

from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.hooks.context import PendingHookContextReservation
from pulsara_agent.hooks.contracts import (
    GateDecision,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    PostCompactInput,
    PostCompactRef,
    PreCompactInput,
    PreCompactRef,
)
from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
from pulsara_agent.hooks.matcher import event_matcher_subject


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{sha256(chr(0).join(parts).encode()).hexdigest()}"


def _target_is_known_text_only(target: PreparedKernelModelTarget) -> bool:
    modalities = target.target.fact.input_modalities
    return modalities is not None and "text" in modalities and "image" not in modalities


@dataclass(frozen=True, slots=True)
class ValidatedCompactionWireTransition:
    phase: Literal["PRE_FULL", "POST_FULL"]
    source_quote: FrozenProviderWireInputQuote
    successor_quote: FrozenProviderWireInputQuote
    reclaim_tokens: int


class CompactionWireTransitionDrift(CompactionPlanningError):
    """The source and successor no longer share one exact planning authority."""


def validate_compaction_wire_transition(
    *,
    source_view: FrozenCompactionSourceView,
    source_candidate: PreparedProviderWireCandidate,
    successor_wire: PreparedWireMeasurementDecision,
    policy,
    force: bool,
    enforce_soft_target: bool,
    phase: Literal["PRE_FULL", "POST_FULL"],
) -> ValidatedCompactionWireTransition:
    """Exact structural join before the single numerical reclaim validator."""

    source_quote = source_view.provider_wire_quote
    successor_candidate = successor_wire.candidate
    successor_quote = successor_wire.quote
    successor_prepared_call = getattr(successor_candidate, "prepared_call", None)
    if successor_prepared_call is None:
        raise CompactionWireTransitionDrift(
            "compaction wire transition does not exact-join an installable successor"
        )
    source_call = source_candidate.prepared_call.call
    successor_call = successor_prepared_call.call
    source_binding = source_candidate.prepared_call.compile_binding
    successor_binding = successor_prepared_call.compile_binding
    source_identity = source_candidate.semantic_input.canonical_input_identity
    successor_identity = successor_candidate.semantic_input.canonical_input_identity
    if (
        phase not in {"PRE_FULL", "POST_FULL"}
        or source_candidate.canonical_read != source_view.canonical_dispatch_read
        or source_candidate.semantic_input.final_estimate.total_input_tokens
        != source_quote.semantic_estimated_input_tokens
        or successor_candidate.semantic_input.final_estimate.total_input_tokens
        != successor_quote.semantic_estimated_input_tokens
        or source_call.target.fact != successor_call.target.fact
        or source_binding.target_fact != source_call.target.fact
        or successor_binding.target_fact != successor_call.target.fact
        or source_call.target.model_profile.route_wire_profile
        != successor_call.target.model_profile.route_wire_profile
        or type(source_binding.estimator) is not type(successor_binding.estimator)
        or source_binding.estimator.fact != successor_binding.estimator.fact
        or source_quote.wire_api != successor_quote.wire_api
        or source_quote.wire_api
        != source_call.target.model_profile.route_wire_profile.wire_api
        or successor_quote.wire_api
        != successor_call.target.model_profile.route_wire_profile.wire_api
        or source_binding.estimator_fingerprint != source_quote.estimator_fingerprint
        or successor_binding.estimator_fingerprint
        != successor_quote.estimator_fingerprint
        or source_quote.estimator_fingerprint != successor_quote.estimator_fingerprint
        or source_binding.effective_input_budget_tokens
        != source_quote.effective_input_budget_tokens
        or successor_binding.effective_input_budget_tokens
        != successor_quote.effective_input_budget_tokens
        or source_quote.effective_input_budget_tokens
        != successor_quote.effective_input_budget_tokens
        or source_identity.session_id != successor_identity.session_id
        or source_identity.turn_id != successor_identity.turn_id
        or source_identity.conversation_scope_kind
        is not successor_identity.conversation_scope_kind
        or source_identity.scope_subagent_task_id
        != successor_identity.scope_subagent_task_id
        or source_identity
        != source_candidate.canonical_read.compile_snapshot.canonical_input.identity
        or successor_candidate.semantic_input.canonical_input_identity
        != successor_candidate.canonical_read.compile_snapshot.canonical_input.identity
        or not _compaction_cut_lineage_exactly_joins(
            source_candidate=source_candidate,
            successor_candidate=successor_candidate,
            phase=phase,
        )
    ):
        raise CompactionWireTransitionDrift(
            "compaction wire transition does not exact-join its target and cuts"
        )
    if successor_wire.wire_input_plan is None:
        if (
            successor_quote.final_wire_estimated_input_tokens
            > successor_quote.effective_input_budget_tokens
        ):
            raise CompactionPlanningError(
                "compaction successor exceeds its hard model budget"
            )
        if successor_quote.final_wire_utf8_bytes > MAXIMUM_PROVIDER_WIRE_INPUT_BYTES:
            raise CompactionPlanningError(
                "compaction successor exceeds its hard physical byte bound"
            )
        raise CompactionPlanningError(
            "compaction successor lacks executable final-wire admission"
        )
    if successor_quote.final_wire_utf8_bytes >= source_quote.final_wire_utf8_bytes:
        raise CompactionReclaimUnavailable(
            "compaction successor does not shrink exact final-wire bytes"
        )
    reclaim = validate_compaction_reclaim(
        source_tokens=source_quote.final_wire_estimated_input_tokens,
        successor_tokens=successor_quote.final_wire_estimated_input_tokens,
        hard_input_budget_tokens=successor_quote.effective_input_budget_tokens,
        policy=policy,
        force=force,
        enforce_soft_target=enforce_soft_target,
    )
    return ValidatedCompactionWireTransition(
        phase=phase,
        source_quote=source_quote,
        successor_quote=successor_quote,
        reclaim_tokens=reclaim,
    )


def validate_model_switch_wire_transition(
    *,
    source_view: FrozenCompactionSourceView,
    source_candidate: PreparedProviderWireCandidate,
    successor_wire: PreparedWireMeasurementDecision,
    destination_target: PreparedKernelModelTarget,
    policy,
    phase: Literal["PRE_FULL", "POST_FULL"],
) -> ValidatedCompactionWireTransition:
    """Join A's canonical source to one exact B successor admission.

    This validator is deliberately separate from ordinary same-target reclaim:
    A and B may have different profiles, wire APIs, estimators and budgets.  B's
    own trigger and hard bounds are the complete numerical admission rule.
    """

    successor_candidate = successor_wire.candidate
    source_prepared_call = getattr(source_candidate, "prepared_call", None)
    successor_prepared_call = getattr(successor_candidate, "prepared_call", None)
    if source_prepared_call is None or successor_prepared_call is None:
        raise CompactionWireTransitionDrift(
            "model-switch transition lacks an installable source or successor"
        )
    source_call = source_prepared_call.call
    source_binding = source_prepared_call.compile_binding
    source_view_binding = source_view.normal_compile_binding
    source_quote = source_view.provider_wire_quote
    source_semantic = source_candidate.semantic_input
    source_identity = source_candidate.semantic_input.canonical_input_identity
    successor_identity = successor_candidate.semantic_input.canonical_input_identity
    successor_call = successor_prepared_call.call
    successor_binding = successor_prepared_call.compile_binding
    quote = successor_wire.quote
    if (
        phase not in {"PRE_FULL", "POST_FULL"}
        or source_candidate.canonical_read != source_view.canonical_dispatch_read
        or source_identity
        != source_candidate.canonical_read.compile_snapshot.canonical_input.identity
        or source_binding != source_view_binding
        or type(source_binding.estimator) is not type(source_view_binding.estimator)
        or source_binding.estimator.fact != source_view_binding.estimator.fact
        or source_semantic.compile_binding_fingerprint
        != source_view_binding.binding_fingerprint
        or source_semantic.system_prompt != source_view.materialized_system_prompt()
        or source_semantic.messages != source_view.materialized_messages()
        or source_semantic.tools != source_view_binding.tool_surface.tool_specs
        or source_semantic.final_estimate
        != source_view.provider_projection.final_estimate
        or source_quote.semantic_estimated_input_tokens
        != source_semantic.final_estimate.total_input_tokens
        or source_quote.semantic_visual_image_tokens
        != source_semantic.final_estimate.visual_image_tokens
        or source_call.target.fact != source_view_binding.target_fact
        or source_quote.wire_api
        != source_call.target.model_profile.route_wire_profile.wire_api
        or source_quote.estimator_fingerprint
        != source_view_binding.estimator_fingerprint
        or source_quote.effective_input_budget_tokens
        != source_view_binding.effective_input_budget_tokens
        or successor_call.target is not destination_target.target
        or successor_call.target.fact != destination_target.target.fact
        or successor_call.binding != destination_target.call.binding
        or successor_binding.target_fact != destination_target.target.fact
        or successor_binding.estimator.fact
        != destination_target.target.token_estimator.fact
        or quote.estimator_fingerprint != successor_binding.estimator_fingerprint
        or quote.effective_input_budget_tokens
        != successor_binding.effective_input_budget_tokens
        or quote.wire_api
        != destination_target.target.model_profile.route_wire_profile.wire_api
        or source_identity.session_id != successor_identity.session_id
        or source_identity.turn_id != successor_identity.turn_id
        or source_identity.conversation_scope_kind
        is not successor_identity.conversation_scope_kind
        or source_identity.scope_subagent_task_id
        != successor_identity.scope_subagent_task_id
        or not _compaction_cut_lineage_exactly_joins(
            source_candidate=source_candidate,
            successor_candidate=successor_candidate,
            phase=phase,
        )
    ):
        raise CompactionWireTransitionDrift(
            "model-switch transition does not exact-join A, B and canonical cuts"
        )
    trigger = int(quote.effective_input_budget_tokens * policy.auto_trigger_ratio)
    if (
        successor_wire.wire_input_plan is None
        or quote.final_wire_estimated_input_tokens >= trigger
        or quote.final_wire_utf8_bytes > MAXIMUM_PROVIDER_WIRE_INPUT_BYTES
    ):
        raise CompactionReclaimUnavailable(
            "model-switch successor does not land below B trigger"
        )
    return ValidatedCompactionWireTransition(
        phase=phase,
        source_quote=source_quote,
        successor_quote=quote,
        # Cross-target token estimates are not numerically comparable.  This
        # shared carrier's reclaim field has no model-switch product meaning.
        reclaim_tokens=0,
    )


def _compaction_cut_lineage_exactly_joins(
    *,
    source_candidate: PreparedProviderWireCandidate,
    successor_candidate: PreparedProviderWireCandidate,
    phase: Literal["PRE_FULL", "POST_FULL"],
) -> bool:
    """Prove the successor is the one snapshot rewrite of the source cut."""

    source_read = source_candidate.canonical_read
    successor_read = successor_candidate.canonical_read
    source_facts = source_read.compile_snapshot
    successor_facts = successor_read.compile_snapshot
    source_input = source_facts.canonical_input
    successor_input = successor_facts.canonical_input
    source_identity = source_input.identity
    successor_identity = successor_input.identity
    source_binding = source_facts.context_binding_fact
    successor_binding = successor_facts.context_binding_fact
    boundary = successor_binding.source_through_sequence
    source_lineage_floor = (
        0
        if source_binding.base_kind is ContextBindingBaseKind.FULL_HISTORY
        else source_binding.source_through_sequence
    )
    if (
        successor_binding.base_kind is not ContextBindingBaseKind.SNAPSHOT
        or successor_binding.context_snapshot_id is None
        or successor_binding.revision_ordinal != source_binding.revision_ordinal + 1
        or not source_lineage_floor
        <= boundary
        <= source_identity.provider_input_through_sequence
        or successor_identity.session_id != source_identity.session_id
        or successor_identity.turn_id != source_identity.turn_id
        or successor_identity.initial_entry_id != source_identity.initial_entry_id
        or successor_identity.conversation_scope_kind
        is not source_identity.conversation_scope_kind
        or successor_identity.scope_subagent_task_id
        != source_identity.scope_subagent_task_id
        or successor_identity.context_binding_revision_id
        != successor_binding.binding_revision_id
        or successor_identity.provider_input_through_sequence
        < source_identity.provider_input_through_sequence
        or (
            phase == "PRE_FULL"
            and successor_identity.provider_input_through_sequence
            != source_identity.provider_input_through_sequence
        )
    ):
        return False
    suffix = tuple(
        item
        for item in source_input.items
        if item.source_entry_sequence is not None
        and item.source_entry_sequence > boundary
    )
    if not successor_input.items:
        return False
    snapshot, *successor_suffix = successor_input.items
    snapshot_carrier = snapshot.content
    if (
        snapshot.item_kind is not FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
        or not isinstance(snapshot_carrier, CompactionSnapshotCarrier)
        or snapshot.source_entry_id is not None
        or snapshot.source_entry_sequence != boundary
        or tuple(successor_suffix) != suffix
        or successor_input.canonical_expanded_bytes
        != snapshot_carrier.canonical_expanded_bytes
        + sum(provider_input_item_canonical_expanded_bytes(item) for item in suffix)
    ):
        return False
    retained_request_ids = frozenset(
        item.source_entry_id
        for item in suffix
        if item.item_kind is FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST
        and item.source_entry_id is not None
    )
    expected_closures = tuple(
        item
        for item in source_input.closures
        if item.assistant_entry_id in retained_request_ids
    )
    expected_late_outcomes = tuple(
        item
        for item in source_input.late_outcomes
        if item.result_entry_sequence > boundary
    )
    retained_entry_ids = frozenset(
        item.source_entry_id for item in suffix if item.source_entry_id is not None
    )
    expected_manifests = tuple(
        item
        for item in source_read.replay_manifest_cut.manifests
        if item.assistant_entry_id in retained_entry_ids
    )
    return (
        successor_input.closures == expected_closures
        and successor_input.late_outcomes == expected_late_outcomes
        and successor_read.replay_manifest_cut.manifests == expected_manifests
        and successor_facts.run_permission_snapshot
        == source_facts.run_permission_snapshot
        and successor_facts.plan_workflow_fact == source_facts.plan_workflow_fact
        and successor_facts.plan_handoff_fact == source_facts.plan_handoff_fact
        and successor_facts.previous_turn_outcome_fact
        == source_facts.previous_turn_outcome_fact
        and successor_facts.tool_observation_freshness_fact
        == source_facts.tool_observation_freshness_fact
    )


def _can_retry_compaction_with_smaller_tail(error: BaseException) -> bool:
    if isinstance(error, CompactionWireTransitionDrift):
        return False
    if isinstance(error, CompactionPlanningError):
        return True
    if not isinstance(error, StructuredModelInputCompileError):
        return False
    return error.kind in {
        ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED,
        ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED,
        ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE,
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET,
    }


_RECENT_RETRYABLE_PLANNING_FAILURES = frozenset(
    {
        "compaction successor exceeds its hard model budget",
        "compaction successor exceeds its hard physical byte bound",
        "compaction successor lacks executable final-wire admission",
        "compaction successor exceeds its post target",
    }
)


def _can_retry_compaction_with_smaller_recent(error: BaseException) -> bool:
    """Keep recent shrinking limited to resource and reclaim failures."""

    if isinstance(error, CompactionReclaimUnavailable):
        return True
    if isinstance(error, CompactionPlanningError):
        return str(error) in _RECENT_RETRYABLE_PLANNING_FAILURES
    if not isinstance(error, StructuredModelInputCompileError):
        return False
    return error.kind in {
        ModelInputCompileFailureKind.SOURCE_PHYSICAL_BOUND_EXCEEDED,
        ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED,
        ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED,
        ModelInputCompileFailureKind.STATEFUL_SOURCE_REPLACEMENT_OVER_BUDGET,
        ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.TOOL_SCHEMA_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE,
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET,
    }


def _ordinary_recent_suffixes(
    recent: tuple[RecentHumanMessageProof, ...],
) -> tuple[tuple[RecentHumanMessageProof, ...], ...]:
    """Try the selected window, then remove its oldest request each time."""

    return tuple(recent[offset:] for offset in range(len(recent) + 1))


def _rebase_pending_active_candidate(
    candidate: PreparedActiveRootInputCandidate,
    successor: FrozenCanonicalProviderDispatchRead,
) -> PreparedActiveRootInputCandidate:
    """Bind an unpublished suffix to one dry/adopted successor cut."""

    identity = successor.compile_snapshot.canonical_input.identity
    source = candidate.expected_provider_input_cut
    if (
        identity.session_id != source.session_id
        or identity.turn_id != source.turn_id
        or identity.conversation_scope_kind is not ModelInputScopeKind.ROOT
        or identity.scope_subagent_task_id is not None
        or identity.provider_input_through_sequence
        != source.provider_input_through_sequence
    ):
        raise StructuredModelInputCompileError(
            ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
        )
    return replace(
        candidate,
        expected_provider_input_cut=PreparedProviderInputCut(
            session_id=identity.session_id,
            turn_id=identity.turn_id,
            context_binding_revision_id=identity.context_binding_revision_id,
            provider_input_through_sequence=(identity.provider_input_through_sequence),
        ),
    )


def _can_enter_model_switch_tier_three(error: BaseException) -> bool:
    """Return whether Tier 2 ended in one closed, degradable B-fit outcome."""

    if isinstance(error, CompactionReclaimUnavailable):
        return True
    if isinstance(error, ModelTargetCapabilityMismatch):
        return True
    if not isinstance(error, StructuredModelInputCompileError):
        return False
    return error.kind in {
        ModelInputCompileFailureKind.SOURCE_PHYSICAL_BOUND_EXCEEDED,
        ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED,
        ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED,
        ModelInputCompileFailureKind.STATEFUL_SOURCE_REPLACEMENT_OVER_BUDGET,
        ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.TOOL_SCHEMA_EXCEEDS_BUDGET,
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE,
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET,
    }


@dataclass(frozen=True, slots=True)
class CompactionExecutionResult:
    outcome: CompactionOutcome
    successor_dispatch: PreparedProviderDispatch | None = dataclass_field(
        default=None, repr=False
    )
    prospective_root_dispatch: PreparedProspectiveRootDispatch | None = dataclass_field(
        default=None, repr=False
    )
    prospective_active_input: PreparedProspectiveActiveRootInput | None = (
        dataclass_field(default=None, repr=False)
    )
    active_continuation_blocked_reason: str | None = None
    model_switch_tier: Literal[2, 3] | None = None

    def __post_init__(self) -> None:
        if self.active_continuation_blocked_reason is not None and (
            self.outcome.disposition is not CompactionDisposition.COMPACTED
            or self.successor_dispatch is not None
            or self.prospective_root_dispatch is not None
            or self.prospective_active_input is not None
        ):
            raise ValueError("compaction continuation block carrier is invalid")
        if self.model_switch_tier is not None and (
            self.outcome.disposition is not CompactionDisposition.COMPACTED
            or (
                self.successor_dispatch is None
                and self.prospective_root_dispatch is None
                and self.prospective_active_input is None
            )
        ):
            raise ValueError("model-switch tier requires an installed successor")
        if (
            sum(
                value is not None
                for value in (
                    self.successor_dispatch,
                    self.prospective_root_dispatch,
                    self.prospective_active_input,
                )
            )
            > 1
        ):
            raise ValueError("compaction result has multiple continuation owners")


@dataclass(frozen=True, slots=True)
class _CompactionFencedRestart:
    """Stack-free restart request after one fenced attempt releases its owners."""

    maximum_retained_tool_groups: int | None
    pre_compact_dispatched: bool


@dataclass(frozen=True, slots=True)
class _ModelSwitchTier3Fallback:
    pre_compact_dispatched: bool


@dataclass(frozen=True, slots=True)
class _InstalledPrefixSummaryWinner:
    semantic: PreparedCompactionSummarySemantic = dataclass_field(repr=False)
    decision: PreparedWireMeasurementDecision = dataclass_field(repr=False)
    tail: ProtectedTailSelectionFact
    prefix: ProviderPrefixCutProof
    recent: tuple[RecentHumanMessageProof, ...]
    continuation: tuple[
        CompactionContinuationMode,
        FrozenCompactionActiveRequest | None,
    ]
    retained_tool_group_count: int


@dataclass(frozen=True, slots=True)
class _DestinationProjectionWinner:
    projection: DestinationDialogueProjection = dataclass_field(repr=False)
    semantic: PreparedCompactionSummarySemantic = dataclass_field(repr=False)
    decision: PreparedWireMeasurementDecision = dataclass_field(repr=False)
    tail: ProtectedTailSelectionFact
    prefix: ProviderPrefixCutProof
    recent: tuple[RecentHumanMessageProof, ...]
    continuation: tuple[
        CompactionContinuationMode,
        FrozenCompactionActiveRequest | None,
    ]


def _already_compact_result(turn_id: str) -> CompactionExecutionResult:
    return CompactionExecutionResult(
        CompactionOutcome(
            CompactionDisposition.NOT_NEEDED,
            turn_id,
            None,
            None,
            "CONTEXT_ALREADY_COMPACT",
        )
    )


class _PostAdoptionCompactionFailure(BaseException):
    """Carry an already-FULL compaction winner until its error is re-raised."""

    def __init__(self, outcome: CompactionOutcome, error: BaseException) -> None:
        if (
            outcome.disposition is not CompactionDisposition.COMPACTED
            or outcome.snapshot_id is None
            or outcome.revision_ordinal is None
        ):
            raise ValueError("post-adoption failure lacks its canonical winner")
        super().__init__(type(error).__name__)
        self.outcome = outcome
        self.error = error


_COMPACTION_ATTEMPT_TOKEN_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class CompactionAttemptToken:
    attempt_id: str
    session_id: str
    turn_id: str
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    trigger: CompactionTrigger

    def __init__(
        self,
        *,
        attempt_id: str,
        session_id: str,
        turn_id: str,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        trigger: CompactionTrigger,
        _seal: object,
    ) -> None:
        if _seal is not _COMPACTION_ATTEMPT_TOKEN_SEAL or not attempt_id:
            raise TypeError("compaction attempt token is coordinator-issued")
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "scope_kind", scope_kind)
        object.__setattr__(self, "scope_subagent_task_id", scope_subagent_task_id)
        object.__setattr__(self, "trigger", trigger)


def _new_compaction_attempt_token(
    *,
    session_id: str,
    turn_id: str,
    scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    trigger: CompactionTrigger,
) -> CompactionAttemptToken:
    return CompactionAttemptToken(
        attempt_id=f"compaction-attempt:{uuid4().hex}",
        session_id=session_id,
        turn_id=turn_id,
        scope_kind=scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        trigger=trigger,
        _seal=_COMPACTION_ATTEMPT_TOKEN_SEAL,
    )


@dataclass(frozen=True, slots=True)
class PreparedCompactSessionStartFacts:
    attempt_token: CompactionAttemptToken = dataclass_field(repr=False, compare=False)
    adopted_snapshot_id: str
    adopted_binding_revision_id: str
    scope: CompactionScope
    turn_id: str
    model_id: str
    permission_snapshot: FrozenRunPermissionSnapshot = dataclass_field(repr=False)
    deadline_monotonic: float = dataclass_field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self.scope.scope_kind is not ModelInputScopeKind.ROOT
            or self.scope.scope_subagent_task_id is not None
            or self.scope.turn_id != self.turn_id
            or self.attempt_token.session_id != self.scope.session_id
            or self.attempt_token.turn_id != self.turn_id
            or self.attempt_token.scope_kind is not ModelInputScopeKind.ROOT
            or self.attempt_token.scope_subagent_task_id is not None
            or not self.adopted_snapshot_id
            or not self.adopted_binding_revision_id
            or not self.model_id
            or not self.permission_snapshot.snapshot_id
            or self.deadline_monotonic <= 0
        ):
            raise ValueError("compact SessionStart facts do not exact-join")


@dataclass(frozen=True, slots=True)
class PreparedCompactSessionStart:
    proceed: bool
    reason: str | None = None
    reservation: PendingHookContextReservation | None = dataclass_field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.proceed and self.reservation is not None:
            raise ValueError("blocked compact SessionStart cannot own context")


class SessionStartCompactPort(Protocol):
    async def __call__(
        self, facts: PreparedCompactSessionStartFacts
    ) -> PreparedCompactSessionStart: ...


class SessionStartCompactBoundaryPort(Protocol):
    async def arm_compact_boundary(
        self,
        *,
        attempt_token: CompactionAttemptToken,
        adopted_snapshot_id: str,
        adopted_binding_revision_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class OrdinaryPrecompileDecision:
    """Unique ordinary admission plus its handle-free exact wire observation."""

    ordinary_admission: PreparedProviderHeadroomAdmission = dataclass_field(repr=False)
    reusable_wire_observation: HandleFreeProviderWireObservation = dataclass_field(
        repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class ModelSwitchDirectPrecompileDecision:
    """Tier-1 B candidate after consuming its exact precheck observation."""

    ordinary_admission: PreparedProviderHeadroomAdmission = dataclass_field(repr=False)
    direct_admission: FrozenDirectSwitchAdmission = dataclass_field(repr=False)
    wire_input_plan: FrozenProviderWireInputPlan = dataclass_field(repr=False)


@dataclass(frozen=True, slots=True)
class ModelSwitchCompactionTriggerCandidate:
    """Authority-free exact A/B values captured by the direct precheck."""

    source_target: FrozenEpochModelTargetBundle | None = dataclass_field(repr=False)
    destination_target: PreparedKernelModelTarget = dataclass_field(repr=False)
    trigger: CompactionTrigger = CompactionTrigger.MODEL_SWITCH_HANDOVER


@dataclass(frozen=True, slots=True)
class AutomaticCompactionTriggerCandidate:
    """Authority-free trigger fact; fenced execution recaptures all source truth."""

    trigger: CompactionTrigger
    reason: Literal["FINAL_WIRE_POLICY"] = "FINAL_WIRE_POLICY"

    def __post_init__(self) -> None:
        if self.trigger not in {
            CompactionTrigger.AUTO_ACTIVE_CONTEXT,
            CompactionTrigger.MID_TURN_FOLLOWUP,
        }:
            raise ValueError("automatic trigger candidate has a non-automatic trigger")


PrecompileCompactionDecision = (
    OrdinaryPrecompileDecision
    | AutomaticCompactionTriggerCandidate
    | ModelSwitchDirectPrecompileDecision
    | ModelSwitchCompactionTriggerCandidate
)


class CompactionCoordinator:
    """Own active/idle compaction planning, adoption and successor installation."""

    def __init__(
        self,
        *,
        owner: HostCompactionRuntimeOwner | None,
        repository: ConversationKernelRepository,
        writer_lease: WriterLease,
        model: KernelModelPort,
        tools: ToolSurfacePlanningPort,
        provider_dispatch: ProviderDispatchCoordinator,
        input_reader: CanonicalProviderInputReader,
        io_owner: KernelSessionIO,
        compiler: StructuredModelInputCompiler,
        continuity_owner: HostProviderInputContinuityOwner,
        safe_point: ProviderSafePointCoordinator,
        content_publisher: CanonicalContentPublisher,
        workspace_resolver: SessionWorkspaceResolver,
        deadline_factory: KernelExecutionDeadlineFactory,
        hook_dispatcher: KernelHookDispatcher | None = None,
        hook_root_scope: HookDispatchScopeRef | None = None,
    ) -> None:
        self._compaction_owner = owner
        self._repository = repository
        self._writer_lease = writer_lease
        self._model = model
        self._tools = tools
        self._provider_dispatch = provider_dispatch
        self._input_reader = input_reader
        self._io = io_owner
        self._compiler = compiler
        self._continuity = continuity_owner
        self._safe_point = safe_point
        self._content_publisher = content_publisher
        self._workspace_resolver = workspace_resolver
        self._deadlines = deadline_factory
        self._hook_dispatcher = hook_dispatcher
        self._hook_root_scope = hook_root_scope

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    async def take_manual(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        turn_id: str,
    ) -> ManualCompactionRequest | None:
        if (
            self._compaction_owner is None
            or not self._compaction_owner.policy.enabled
            or not self._compaction_owner.policy.manual_enabled
        ):
            return None
        return await self._compaction_owner.take_manual(
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            turn_id=turn_id,
        )

    async def prepare_precompile_admission(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        deadline: float,
    ) -> PreparedProviderHeadroomAdmission:
        return await self._provider_dispatch.prepare_headroom_admission(
            turn_id=turn_id,
            model_call_index=model_call_index,
            deadline=deadline,
        )

    def automatic_allowed(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> bool:
        return (
            self._compaction_owner is not None
            and self._compaction_owner.policy.enabled
            and self._compaction_owner.policy.automatic_enabled
            and (
                self._compaction_owner.automatic_allowed(
                    scope_kind=scope_kind,
                    scope_subagent_task_id=scope_subagent_task_id,
                )
            )
        )

    def precompile_needed(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        allow_model_switch: bool,
    ) -> bool:
        if self.automatic_allowed(
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
        ):
            return True
        if not allow_model_switch:
            return False
        # The first call must inspect canonical history even when no installed A
        # survives (cold restart/fork).  A known text-only B may need Tier 3 P.
        return True

    def prospective_root_crosses_automatic_threshold(
        self,
        prepared: PreparedProspectiveRootDispatch,
    ) -> bool:
        """Apply the existing soft trigger to one exact unpublished ROOT input."""

        if not self.automatic_allowed(
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        ):
            return False
        admission = prepared.admission
        compiled = prepared.append_result.compiled_input
        quote = admission.wire_quote
        if quote.final_wire_estimated_input_tokens >= int(
            quote.effective_input_budget_tokens
            * self._compaction_owner.policy.auto_trigger_ratio
        ):
            return True
        canonical = admission.canonical_read.compile_snapshot.canonical_input
        return crosses_compaction_resource_headroom(
            selected_item_count=len(canonical.items),
            selected_canonical_expanded_bytes=canonical.canonical_expanded_bytes,
            continuity_epoch_logical_bytes=provider_input_logical_bytes(
                system_prompt=compiled.system_prompt,
                tools=compiled.tools,
                messages=compiled.messages,
            ),
        )

    async def _resolved_workspace_id(self, *, deadline: float | None = None) -> str:
        return await self._workspace_resolver.resolve(
            deadline=self._canonical_deadline() if deadline is None else deadline
        )

    @staticmethod
    def _hook_trigger(trigger: CompactionTrigger) -> str:
        return "manual" if trigger is CompactionTrigger.MANUAL else "auto"

    async def _dispatch_pre_compact(
        self,
        *,
        dispatch: PreparedCompactionSourceDispatch,
        scope: HookDispatchScopeRef | None,
        attempt_token: CompactionAttemptToken,
        turn_id: str,
        trigger: CompactionTrigger,
        deadline: float,
    ) -> tuple[bool, str | None]:
        dispatcher = self._hook_dispatcher
        if dispatcher is None or scope is None:
            return True, None
        trigger_name = self._hook_trigger(trigger)
        public_input = PreCompactInput(
            session_id=self._writer_lease.guard.session_id,
            cwd=str(self._tools.snapshot_terminal_cwd()),
            model=dispatch.prepared_call.call.target.fact.model_id,
            turn_id=turn_id,
            trigger=trigger_name,
        )
        outcome = await dispatcher.dispatch(
            HookDispatchEnvelope(
                dispatcher.capture_view(),
                scope,
                public_input,
                PreCompactRef(
                    attempt_token,
                    attempt_token.scope_kind.value,
                    turn_id,
                    trigger_name,
                ),
                deadline,
            ),
            matcher_subject=event_matcher_subject(
                public_input.event_type, trigger=trigger_name
            ),
        )
        return outcome.decision is GateDecision.PROCEED, outcome.reason

    async def _dispatch_post_compact(
        self,
        *,
        model_id: str,
        cwd: str,
        scope: HookDispatchScopeRef | None,
        attempt_token: CompactionAttemptToken,
        candidate: PreparedCompactionCanonicalAdoption,
        turn_id: str,
        trigger: CompactionTrigger,
        deadline: float,
    ) -> tuple[bool, str | None]:
        dispatcher = self._hook_dispatcher
        if dispatcher is None or scope is None:
            return True, None
        trigger_name = self._hook_trigger(trigger)
        public_input = PostCompactInput(
            session_id=self._writer_lease.guard.session_id,
            cwd=cwd,
            model=model_id,
            turn_id=turn_id,
            trigger=trigger_name,
        )
        outcome = await dispatcher.dispatch(
            HookDispatchEnvelope(
                dispatcher.capture_view(),
                scope,
                public_input,
                PostCompactRef(
                    attempt_token,
                    candidate.snapshot.snapshot_id,
                    candidate.binding.binding_revision_id,
                ),
                deadline,
            ),
            matcher_subject=event_matcher_subject(
                public_input.event_type, trigger=trigger_name
            ),
        )
        return outcome.decision is GateDecision.PROCEED, outcome.reason

    async def execute_active(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        trigger: CompactionTrigger,
        force: bool,
        manual_request: ManualCompactionRequest | None,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        hook_scope: HookDispatchScopeRef | None = None,
        session_start_compact_port: SessionStartCompactPort | None = None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None = None,
    ) -> CompactionExecutionResult:
        return await self._execute_active(
            turn_id=turn_id,
            model_call_index=model_call_index,
            inherited_memory_use_policy=inherited_memory_use_policy,
            trigger=trigger,
            force=force,
            manual_request=manual_request,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            hook_scope=hook_scope,
            session_start_compact_port=session_start_compact_port,
            session_start_boundary_port=session_start_boundary_port,
            pending_active_candidate=None,
        )

    async def recover_pending_active_root_input(
        self,
        *,
        candidate: PreparedActiveRootInputCandidate,
        failure: BaseException,
        inherited_memory_use_policy: MemoryUsePolicy,
        admitted_writer: CompactionWriteReservation,
        hook_scope: HookDispatchScopeRef | None = None,
        session_start_compact_port: SessionStartCompactPort | None = None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None = None,
    ) -> PreparedProspectiveActiveRootInput:
        """Compact an active ROOT while keeping its candidate unpublished."""

        if not _can_retry_compaction_with_smaller_recent(failure):
            raise failure
        owner = self._compaction_owner
        if (
            owner is None
            or not owner.policy.enabled
            or not owner.policy.automatic_enabled
        ):
            raise failure
        cut = candidate.expected_provider_input_cut
        execution = await self._execute_active(
            turn_id=cut.turn_id,
            model_call_index=candidate.next_model_call_index,
            inherited_memory_use_policy=inherited_memory_use_policy,
            trigger=CompactionTrigger.AUTO_ACTIVE_CONTEXT,
            force=True,
            manual_request=None,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            hook_scope=hook_scope,
            session_start_compact_port=session_start_compact_port,
            session_start_boundary_port=session_start_boundary_port,
            pending_active_candidate=candidate,
            admitted_writer=admitted_writer,
        )
        prepared = execution.prospective_active_input
        if (
            execution.outcome.disposition is not CompactionDisposition.COMPACTED
            or prepared is None
        ):
            if prepared is not None:
                prepared.close()
            raise failure
        return prepared

    async def execute_model_switch_active(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        candidate: ModelSwitchCompactionTriggerCandidate,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        hook_scope: HookDispatchScopeRef | None = None,
        session_start_compact_port: SessionStartCompactPort | None = None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None = None,
    ) -> CompactionExecutionResult:
        owner = self._compaction_owner
        if owner is None or not owner.policy.enabled:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.MODEL_SWITCH_REQUIRES_COMPACTION
            )
        scope = CompactionScope(
            session_id=self._writer_lease.guard.session_id,
            workspace_id=await self._resolved_workspace_id(),
            turn_id=turn_id,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
        )
        token = _new_compaction_attempt_token(
            session_id=scope.session_id,
            turn_id=turn_id,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            trigger=CompactionTrigger.MODEL_SWITCH_HANDOVER,
        )

        async def operation() -> CompactionExecutionResult:
            return await self._execute_compaction_fenced(
                turn_id=turn_id,
                model_call_index=model_call_index,
                inherited_memory_use_policy=inherited_memory_use_policy,
                trigger=CompactionTrigger.MODEL_SWITCH_HANDOVER,
                force=True,
                expected_scope=scope,
                post_adoption_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
                stable_command_id=None,
                attempt_token=token,
                hook_scope=(
                    hook_scope
                    if hook_scope is not None
                    else self._hook_root_scope
                    if scope_kind is ModelInputScopeKind.ROOT
                    else None
                ),
                session_start_compact_port=session_start_compact_port,
                session_start_boundary_port=session_start_boundary_port,
                model_switch_candidate=candidate,
            )

        try:
            result = await owner.run_fenced(
                scope=scope,
                trigger=CompactionTrigger.MODEL_SWITCH_HANDOVER,
                operation=operation,
                attempt_id=token.attempt_id,
            )
            if (
                result.outcome.disposition is not CompactionDisposition.COMPACTED
                or result.successor_dispatch is None
            ):
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET
                )
            return result
        except _PostAdoptionCompactionFailure as failure:
            raise failure.error.with_traceback(failure.error.__traceback__)
        except StaleHostWriter:
            raise
        except asyncio.CancelledError:
            raise

    def prospective_root_model_switch_candidate(
        self, prepared: PreparedProspectiveRootDispatch
    ) -> ModelSwitchCompactionTriggerCandidate | None:
        """Require D4 handover when a text-only B would erase image history."""

        destination = prepared.prepared_target
        if not _target_is_known_text_only(destination):
            return None
        if not provider_dispatch_effective_history_has_image(
            prepared.admission.canonical_read
        ):
            return None
        scope = ProviderInputContinuityScope(
            session_id=prepared.admission.candidate.session_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        source = self._provider_dispatch.installed_source_target(
            scope=scope,
            destination=destination,
        )
        return ModelSwitchCompactionTriggerCandidate(
            source_target=source,
            destination_target=destination,
        )

    async def recover_pending_root_input(
        self,
        *,
        candidate: PreparedRootProviderInputCandidate,
        failure: BaseException,
        inherited_memory_use_policy: MemoryUsePolicy,
        admitted_writer: CompactionWriteReservation | None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None,
    ) -> PreparedProspectiveRootDispatch:
        """Compact an idle predecessor while keeping the ROOT input unpublished."""

        destination = self._provider_dispatch.prepare_prospective_root_target(candidate)
        if _target_is_known_text_only(destination) and content_has_image(
            candidate.unpublished_items[-1].content
        ):
            raise failure
        scope = ProviderInputContinuityScope(
            session_id=candidate.session_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        source = self._provider_dispatch.installed_source_target(
            scope=scope,
            destination=destination,
        )
        model_switch = (
            None
            if source is None
            else ModelSwitchCompactionTriggerCandidate(
                source_target=source,
                destination_target=destination,
            )
        )
        if source is None and _target_is_known_text_only(destination):
            if self._compaction_owner is None:
                raise failure
            prospective_read = await self._io.run(
                self._input_reader.read_prospective_root_dispatch,
                candidate,
                deadline_monotonic=(
                    monotonic() + self._compaction_owner.policy.planning_attempt_seconds
                ),
            )
            if provider_dispatch_effective_history_has_image(prospective_read):
                model_switch = ModelSwitchCompactionTriggerCandidate(
                    source_target=None,
                    destination_target=destination,
                )
        if model_switch is None and not _can_retry_compaction_with_smaller_recent(
            failure
        ):
            raise failure
        if model_switch is not None:
            failure = StructuredModelInputCompileError(
                ModelInputCompileFailureKind.MODEL_SWITCH_REQUIRES_COMPACTION
            )
        return await self._execute_pending_root_compaction(
            candidate=candidate,
            inherited_memory_use_policy=inherited_memory_use_policy,
            model_switch_candidate=model_switch,
            failure=failure,
            admitted_writer=admitted_writer,
            session_start_boundary_port=session_start_boundary_port,
        )

    async def execute_pending_root_model_handover(
        self,
        *,
        candidate: PreparedRootProviderInputCandidate,
        inherited_memory_use_policy: MemoryUsePolicy,
        model_switch_candidate: ModelSwitchCompactionTriggerCandidate,
        admitted_writer: CompactionWriteReservation | None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None,
    ) -> PreparedProspectiveRootDispatch:
        return await self._execute_pending_root_compaction(
            candidate=candidate,
            inherited_memory_use_policy=inherited_memory_use_policy,
            model_switch_candidate=model_switch_candidate,
            failure=StructuredModelInputCompileError(
                ModelInputCompileFailureKind.MODEL_SWITCH_REQUIRES_COMPACTION
            ),
            admitted_writer=admitted_writer,
            session_start_boundary_port=session_start_boundary_port,
        )

    async def _execute_pending_root_compaction(
        self,
        *,
        candidate: PreparedRootProviderInputCandidate,
        inherited_memory_use_policy: MemoryUsePolicy,
        model_switch_candidate: ModelSwitchCompactionTriggerCandidate | None,
        failure: BaseException,
        admitted_writer: CompactionWriteReservation | None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None,
    ) -> PreparedProspectiveRootDispatch:
        owner = self._compaction_owner
        if (
            owner is None
            or not owner.policy.enabled
            or (model_switch_candidate is None and not owner.policy.automatic_enabled)
        ):
            raise failure
        terminal_turn_id = await self._io.run(
            self._repository.read_latest_terminal_scope_turn_id,
            self._writer_lease.guard,
            scope_kind=ConversationScopeKind.ROOT,
            scope_subagent_task_id=None,
            deadline_monotonic=(monotonic() + owner.policy.planning_attempt_seconds),
        )
        if terminal_turn_id is None:
            raise failure
        if model_switch_candidate is not None:
            model_switch_candidate = replace(
                model_switch_candidate,
                destination_target=(
                    self._provider_dispatch.retarget_prepared_model_target(
                        model_switch_candidate.destination_target,
                        turn_id=terminal_turn_id,
                        model_call_index=1,
                    )
                ),
            )
        scope = CompactionScope(
            session_id=candidate.session_id,
            workspace_id=candidate.workspace_id,
            turn_id=terminal_turn_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        trigger = (
            CompactionTrigger.MODEL_SWITCH_HANDOVER
            if model_switch_candidate is not None
            else CompactionTrigger.AUTO_ACTIVE_CONTEXT
        )
        attempt_token = _new_compaction_attempt_token(
            session_id=scope.session_id,
            turn_id=terminal_turn_id,
            scope_kind=scope.scope_kind,
            scope_subagent_task_id=scope.scope_subagent_task_id,
            trigger=trigger,
        )

        async def operation() -> CompactionExecutionResult:
            return await self._execute_compaction_fenced(
                turn_id=terminal_turn_id,
                model_call_index=1,
                inherited_memory_use_policy=inherited_memory_use_policy,
                trigger=trigger,
                force=True,
                expected_scope=scope,
                post_adoption_branch=CompactionTargetBranch.IDLE_BASE_ONLY,
                stable_command_id=None,
                hook_scope=self._hook_root_scope,
                session_start_boundary_port=session_start_boundary_port,
                model_switch_candidate=model_switch_candidate,
                pending_root_candidate=candidate,
                attempt_token=attempt_token,
            )

        try:
            result = await owner.run_fenced(
                scope=scope,
                trigger=trigger,
                operation=operation,
                admitted_writer=admitted_writer,
                pending_root_turn_id=candidate.exact_turn_id,
                attempt_id=attempt_token.attempt_id,
            )
        except _PostAdoptionCompactionFailure as adopted:
            raise adopted.error.with_traceback(adopted.error.__traceback__)
        prepared = result.prospective_root_dispatch
        if (
            result.outcome.disposition is not CompactionDisposition.COMPACTED
            or prepared is None
        ):
            if prepared is not None:
                prepared.close()
            raise failure
        prepared.bind_model_switch_tier(result.model_switch_tier)
        return prepared

    async def prepare_precompile(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        trigger: CompactionTrigger,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        headroom_admission: PreparedProviderHeadroomAdmission,
        deadline: float,
        allow_model_switch: bool = False,
    ) -> PrecompileCompactionDecision | None:
        owner = self._compaction_owner
        if owner is None:
            headroom_admission.close()
            return None
        try:
            scope = CompactionScope(
                session_id=self._writer_lease.guard.session_id,
                workspace_id=await self._resolved_workspace_id(deadline=deadline),
                turn_id=turn_id,
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
            )
        except BaseException:
            headroom_admission.close()
            raise

        dispatch: PreparedCompactionSourceDispatch | None = None
        source_target: FrozenEpochModelTargetBundle | None = None
        headroom_consumed = False
        try:
            preflight = headroom_admission.preflight
            prepared_target = headroom_admission.prepared_target
            if allow_model_switch:
                source_target = self._provider_dispatch.installed_source_target(
                    scope=ProviderInputContinuityScope(
                        session_id=scope.session_id,
                        scope_kind=scope.scope_kind,
                        scope_subagent_task_id=scope.scope_subagent_task_id,
                    ),
                    destination=prepared_target,
                )
                if _target_is_known_text_only(prepared_target):
                    history_read = await self._io.run(
                        self._input_reader.read_frozen_compaction_cut,
                        headroom_admission.cut,
                        deadline_monotonic=deadline,
                    )
                    if (
                        history_read.scope != scope
                        or history_read.turn_status != "RUNNING"
                        or preflight.effective_materialization_lineage_floor
                        != history_read.safe_head_range.effective_materialization_lineage_floor
                    ):
                        raise CompactionPlanningError(
                            "model-switch history preflight changed at admission"
                        )
                    if compaction_effective_history_has_image(history_read):
                        if not owner.policy.enabled:
                            raise StructuredModelInputCompileError(
                                ModelInputCompileFailureKind.MODEL_SWITCH_REQUIRES_COMPACTION
                            )
                        headroom_admission.close()
                        return ModelSwitchCompactionTriggerCandidate(
                            source_target=source_target,
                            destination_target=prepared_target,
                        )
            transferred_handle = headroom_admission.take_handle()
            headroom_consumed = True
            dispatch = await self._provider_dispatch.prepare_compaction_source(
                turn_id=turn_id,
                model_call_index=model_call_index,
                inherited_memory_use_policy=inherited_memory_use_policy,
                deadline=deadline,
                existing_handle=transferred_handle,
                headroom_preflight_override=preflight,
                prepared_target_override=prepared_target,
            )
            canonical_read = await self._io.run(
                self._input_reader.read_frozen_compaction_cut,
                dispatch.cut,
                deadline_monotonic=deadline,
            )
            if canonical_read.scope != scope or canonical_read.turn_status != "RUNNING":
                raise CompactionPlanningError(
                    "precompile compaction target changed at admission"
                )
            canonical_range = canonical_read.safe_head_range
            if (
                preflight.effective_materialization_lineage_floor
                != canonical_range.effective_materialization_lineage_floor
            ):
                raise CompactionPlanningError(
                    "compaction headroom quote differs from its frozen source"
                )
            source_view = freeze_compaction_source_view(
                canonical_read=canonical_read,
                compile_binding=dispatch.prepared_call.compile_binding,
                semantic_projection=dispatch.projection,
                predecessor_epoch_view=dispatch.planning.predecessor_view,
                provider_wire_quote=dispatch.wire_quote,
                producer_wire_api=(
                    dispatch.prepared_call.call.target.model_profile.route_wire_profile.wire_api
                ),
            )
            if source_target is not None:
                quote = source_view.provider_wire_quote
                destination_trigger = int(
                    quote.effective_input_budget_tokens
                    * owner.policy.auto_trigger_ratio
                )
                if (
                    quote.final_wire_estimated_input_tokens < destination_trigger
                    and quote.final_wire_utf8_bytes <= MAXIMUM_PROVIDER_WIRE_INPUT_BYTES
                ):
                    wire_candidate = dispatch.wire_candidate
                    handle, measurement = dispatch.take_below_trigger_ownership()
                    dispatch = None
                    ordinary = PreparedProviderHeadroomAdmission(
                        handle, preflight, prepared_target
                    )
                    observation = HandleFreeProviderWireObservation(
                        candidate=wire_candidate,
                        measurement=measurement,
                    )
                    try:
                        wire_decision = await self._provider_dispatch.measure_prepared_wire_candidate(
                            wire_candidate,
                            deadline=deadline,
                            reusable_observation=observation,
                        )
                        if wire_decision.wire_input_plan is not None:
                            direct_admission, wire_input_plan = (
                                self._provider_dispatch.freeze_direct_switch_admission(
                                    candidate=wire_candidate,
                                    decision=wire_decision,
                                )
                            )
                            return ModelSwitchDirectPrecompileDecision(
                                ordinary_admission=ordinary,
                                direct_admission=direct_admission,
                                wire_input_plan=wire_input_plan,
                            )
                    except BaseException:
                        ordinary.close()
                        raise
                    ordinary.close()
                    if not owner.policy.enabled:
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.MODEL_SWITCH_REQUIRES_COMPACTION
                        )
                    return ModelSwitchCompactionTriggerCandidate(
                        source_target=source_target,
                        destination_target=prepared_target,
                    )
                dispatch.discard_wire_materialization_to_quote()
                dispatch.close()
                dispatch = None
                if not owner.policy.enabled:
                    raise StructuredModelInputCompileError(
                        ModelInputCompileFailureKind.MODEL_SWITCH_REQUIRES_COMPACTION
                    )
                return ModelSwitchCompactionTriggerCandidate(
                    source_target=source_target,
                    destination_target=prepared_target,
                )
            if not owner.policy.enabled or not owner.policy.automatic_enabled:
                wire_candidate = dispatch.wire_candidate
                handle, measurement = dispatch.take_below_trigger_ownership()
                dispatch = None
                ordinary = PreparedProviderHeadroomAdmission(
                    handle, preflight, prepared_target
                )
                observation = HandleFreeProviderWireObservation(
                    candidate=wire_candidate,
                    measurement=measurement,
                )
                return OrdinaryPrecompileDecision(
                    ordinary_admission=ordinary,
                    reusable_wire_observation=observation,
                )
            if not should_trigger_compaction(
                source_view=source_view,
                policy=owner.policy,
                force=False,
            ):
                wire_candidate = dispatch.wire_candidate
                handle, measurement = dispatch.take_below_trigger_ownership()
                dispatch = None
                ordinary: PreparedProviderHeadroomAdmission | None = None
                observation: HandleFreeProviderWireObservation | None = None
                try:
                    ordinary = PreparedProviderHeadroomAdmission(
                        handle,
                        preflight,
                        prepared_target,
                    )
                    observation = HandleFreeProviderWireObservation(
                        candidate=wire_candidate,
                        measurement=measurement,
                    )
                    return OrdinaryPrecompileDecision(
                        ordinary_admission=ordinary,
                        reusable_wire_observation=observation,
                    )
                except BaseException:
                    if observation is None:
                        measurement.discard_materialization_to_quote()
                    else:
                        observation.discard()
                    if ordinary is None:
                        handle.close()
                    else:
                        ordinary.close()
                    raise
            dispatch.discard_wire_materialization_to_quote()
            dispatch.close()
            dispatch = None
            return AutomaticCompactionTriggerCandidate(trigger=trigger)
        except (asyncio.CancelledError, StaleHostWriter):
            if dispatch is not None:
                dispatch.close()
            raise
        except StructuredModelInputCompileError as error:
            if dispatch is not None:
                dispatch.close()
            elif not headroom_consumed:
                headroom_admission.close()
            if source_target is not None or error.kind is (
                ModelInputCompileFailureKind.MODEL_SWITCH_REQUIRES_COMPACTION
            ):
                raise
            owner.record_automatic_failure(
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
            )
            return None
        except BaseException:
            if dispatch is not None:
                dispatch.close()
            elif not headroom_consumed:
                headroom_admission.close()
            if source_target is not None:
                raise
            owner.record_automatic_failure(
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
            )
            return None

    async def _execute_active(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        trigger: CompactionTrigger,
        force: bool,
        manual_request: ManualCompactionRequest | None,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        hook_scope: HookDispatchScopeRef | None,
        session_start_compact_port: SessionStartCompactPort | None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None,
        pending_active_candidate: PreparedActiveRootInputCandidate | None,
        admitted_writer: CompactionWriteReservation | None = None,
    ) -> CompactionExecutionResult:
        owner = self._compaction_owner
        if owner is None or not owner.policy.enabled:
            return CompactionExecutionResult(
                CompactionOutcome(
                    CompactionDisposition.NOT_NEEDED,
                    turn_id,
                    None,
                    None,
                    "COMPACTION_DISABLED",
                )
            )
        if manual_request is not None and (
            manual_request.scope_kind is not scope_kind
            or manual_request.scope_subagent_task_id != scope_subagent_task_id
        ):
            raise RuntimeError("manual compaction belongs to another scope")
        scope_task_id = scope_subagent_task_id
        attempt_token = _new_compaction_attempt_token(
            session_id=self._writer_lease.guard.session_id,
            turn_id=turn_id,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            trigger=trigger,
        )
        try:
            provisional_scope = CompactionScope(
                session_id=self._writer_lease.guard.session_id,
                workspace_id=await self._resolved_workspace_id(),
                turn_id=turn_id,
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_task_id,
            )
        except BaseException:
            raise

        async def operation() -> CompactionExecutionResult:
            return await self._execute_compaction_fenced(
                turn_id=turn_id,
                model_call_index=model_call_index,
                inherited_memory_use_policy=inherited_memory_use_policy,
                trigger=trigger,
                force=force,
                expected_scope=provisional_scope,
                post_adoption_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
                stable_command_id=(
                    None if manual_request is None else manual_request.command_id
                ),
                attempt_token=attempt_token,
                pre_compact_dispatched=False,
                hook_scope=(
                    hook_scope
                    if hook_scope is not None
                    else self._hook_root_scope
                    if scope_kind is ModelInputScopeKind.ROOT
                    else None
                ),
                session_start_compact_port=session_start_compact_port,
                session_start_boundary_port=session_start_boundary_port,
                pending_active_candidate=pending_active_candidate,
            )

        try:
            outcome = await owner.run_fenced(
                scope=provisional_scope,
                trigger=trigger,
                operation=operation,
                admitted_writer=admitted_writer,
                attempt_id=attempt_token.attempt_id,
            )
        except _PostAdoptionCompactionFailure as failure:
            if manual_request is not None:
                await asyncio.shield(
                    owner.settle_manual(manual_request, failure.outcome)
                )
            raise failure.error.with_traceback(failure.error.__traceback__)
        except asyncio.CancelledError:
            if manual_request is not None:
                await asyncio.shield(
                    owner.settle_manual(
                        manual_request,
                        CompactionOutcome(
                            CompactionDisposition.FAILED,
                            turn_id,
                            None,
                            None,
                            "COMPACTION_CANCELLED",
                        ),
                    )
                )
            raise
        except StaleHostWriter:
            if manual_request is not None:
                await owner.settle_manual(
                    manual_request,
                    CompactionOutcome(
                        CompactionDisposition.FAILED,
                        turn_id,
                        None,
                        None,
                        "WRITER_REPLACED",
                    ),
                )
            raise
        except BaseException:
            if pending_active_candidate is not None:
                raise
            execution = CompactionExecutionResult(
                CompactionOutcome(
                    CompactionDisposition.FAILED,
                    turn_id,
                    None,
                    None,
                    "COMPACTION_PLANNING_FAILED",
                )
            )
        else:
            execution = outcome
        outcome = execution.outcome
        if manual_request is not None:
            await owner.settle_manual(manual_request, outcome)
        elif outcome.disposition is CompactionDisposition.FAILED and not (
            outcome.public_code or ""
        ).startswith("HOOK_PRE_COMPACT_BLOCKED"):
            owner.record_automatic_failure(
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_task_id,
            )
        return execution

    async def measure_dispatch_wire(
        self,
        dispatch: PreparedProviderDispatch,
        *,
        deadline: float,
        reusable_observation: HandleFreeProviderWireObservation | None = None,
    ) -> PreparedWireMeasurementDecision:
        return await self._provider_dispatch.measure_prepared_wire_candidate(
            self._provider_dispatch.wire_candidate_for_dispatch(dispatch),
            deadline=deadline,
            reusable_observation=reusable_observation,
        )

    def wire_decision_crosses_automatic_threshold(
        self,
        dispatch: PreparedProviderDispatch,
        decision: PreparedWireMeasurementDecision,
    ) -> bool:
        owner = self._compaction_owner
        if (
            owner is None
            or not owner.policy.enabled
            or not owner.policy.automatic_enabled
        ):
            return False
        if decision.candidate != self._provider_dispatch.wire_candidate_for_dispatch(
            dispatch
        ):
            raise RuntimeError("wire decision belongs to another provider dispatch")
        compiled = dispatch.append_result.compiled_input
        quote = decision.quote
        budget = quote.effective_input_budget_tokens
        if quote.final_wire_estimated_input_tokens >= int(
            budget * owner.policy.auto_trigger_ratio
        ):
            return True
        canonical = dispatch.canonical_facts.canonical_input
        preflight = dispatch.compaction_headroom_preflight
        if preflight is not None:
            identity = canonical.identity
            if (
                preflight.session_id != identity.session_id
                or preflight.turn_id != identity.turn_id
                or preflight.context_binding_revision_id
                != identity.context_binding_revision_id
                or preflight.provider_input_through_sequence
                != identity.provider_input_through_sequence
                or preflight.scope_kind is not identity.conversation_scope_kind
                or preflight.scope_subagent_task_id != identity.scope_subagent_task_id
            ):
                raise RuntimeError(
                    "compaction headroom preflight differs from provider cut"
                )
            item_count = preflight.selected_item_count
            canonical_bytes = preflight.selected_canonical_expanded_bytes
        else:
            item_count = len(canonical.items)
            canonical_bytes = canonical.canonical_expanded_bytes
        return crosses_compaction_resource_headroom(
            selected_item_count=item_count,
            selected_canonical_expanded_bytes=canonical_bytes,
            continuity_epoch_logical_bytes=provider_input_logical_bytes(
                system_prompt=compiled.system_prompt,
                tools=compiled.tools,
                messages=compiled.messages,
            ),
        )

    async def _freeze_compaction_runtime_source(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        maximum_utf8_bytes: int,
        deadline: float,
    ) -> tuple[
        ContextSourceCandidate | ContextSourceAbsentFact,
        FrozenCompactionRuntimeHandoff | None,
    ]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("compaction runtime handoff deadline expired")
        handoff = await asyncio.wait_for(
            self._tools.freeze_compaction_runtime_handoff(
                conversation_scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
                maximum_utf8_bytes=maximum_utf8_bytes,
            ),
            timeout=remaining,
        )
        if handoff is None:
            source = build_compaction_context_source(
                kind=ContextSourceKind.COMPACTION_RUNTIME_HANDOFF,
                texts=None,
                absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            )
        else:
            source = build_compaction_context_source(
                kind=ContextSourceKind.COMPACTION_RUNTIME_HANDOFF,
                texts=(handoff.full_text, handoff.compact_text),
                domain_identity=handoff.source_fingerprint,
            )
        return source, handoff

    async def _select_installed_prefix_summary(
        self,
        *,
        dispatch: PreparedCompactionSourceDispatch,
        source_view: FrozenCompactionSourceView,
        compaction_read: FrozenCompactionCanonicalRead,
        policy,
        maximum_retained_tool_groups: int | None,
        target_branch: CompactionTargetBranch,
        force_zero_retained_groups: bool,
        summary_request: str,
        deadline: float,
    ) -> _InstalledPrefixSummaryWinner | None:
        groups = enumerate_complete_tool_groups(compaction_read)
        summary_call = self._model.resolve_compaction_summary_call(
            active_prepared_call=dispatch.prepared_call
        )
        maximum_retained = min(
            policy.maximum_retained_tool_groups,
            len(groups),
        )
        if force_zero_retained_groups:
            maximum_retained = 0
        if maximum_retained_tool_groups is not None:
            maximum_retained = min(
                maximum_retained,
                maximum_retained_tool_groups,
            )
        canonical = compaction_read.dispatch_read.compile_snapshot.canonical_input
        for retained_count in range(maximum_retained, -1, -1):
            if monotonic() >= deadline:
                raise TimeoutError("compaction planning deadline expired")
            try:
                safe_candidates = enumerate_safe_summary_prefixes(
                    source_view=source_view,
                    complete_tool_groups=groups,
                    retained_group_count=retained_count,
                    source_projection=dispatch.projection.projected_input,
                    deadline_monotonic=deadline,
                )
            except NoSafeCompactionSummaryPrefix:
                continue
            except (CompactionPlanningError, ValueError):
                if force_zero_retained_groups:
                    raise
                continue
            for tail, prefix in safe_candidates:
                tail_range = freeze_compaction_canonical_range(
                    scope=compaction_read.scope,
                    effective_materialization_lineage_floor=(
                        tail.source_through_sequence
                    ),
                    source_through_sequence=source_view.exact_safe_canonical_head,
                    ordered_items=canonical.items,
                    closures=canonical.closures,
                    late_outcomes=canonical.late_outcomes,
                )
                if tail_range.canonical_utf8_bytes > (
                    policy.maximum_retained_tail_utf8_bytes
                ):
                    continue
                continuation = freeze_compaction_continuation(
                    source_view=source_view,
                    target_branch=target_branch,
                    source_through_sequence=prefix.source_through_sequence,
                )
                active_request = continuation[1]
                recent = select_recent_human_messages(
                    canonical_read=compaction_read,
                    source_through_sequence=prefix.source_through_sequence,
                    policy=policy,
                    excluded_entry_id=(
                        None if active_request is None else active_request.entry_id
                    ),
                )
                semantic = prepare_compaction_summary_semantic(
                    call=summary_call,
                    source_view=source_view,
                    source_projection=dispatch.projection.projected_input,
                    prefix_proof=prefix,
                    native_projection_set=(
                        dispatch.tool_exposure_plan.direct_projection_set
                    ),
                    summary_request=summary_request,
                )
                decision = (
                    await self._provider_dispatch.measure_prepared_wire_candidate(
                        semantic,
                        deadline=deadline,
                    )
                )
                if decision.wire_input_plan is None:
                    continue
                return _InstalledPrefixSummaryWinner(
                    semantic=semantic,
                    decision=decision,
                    tail=tail,
                    prefix=prefix,
                    recent=recent,
                    continuation=continuation,
                    retained_tool_group_count=retained_count,
                )
        return None

    async def _select_destination_projection_summary(
        self,
        *,
        dispatch: PreparedCompactionSourceDispatch,
        source_view: FrozenCompactionSourceView,
        compaction_read: FrozenCompactionCanonicalRead,
        policy,
        summary_request: str,
        project_images_for_text_only: bool,
        target_branch: CompactionTargetBranch,
        deadline: float,
    ) -> _DestinationProjectionWinner | None:
        """Select the exact longest B-readable source under one deadline."""

        continuation = freeze_compaction_continuation(
            source_view=source_view,
            target_branch=target_branch,
            source_through_sequence=source_view.exact_safe_canonical_head,
        )
        active_request = continuation[1]
        if target_branch is CompactionTargetBranch.ACTIVE_INSTALLATION and (
            continuation[0] is not CompactionContinuationMode.RESUME_ACTIVE_TURN
            or active_request is None
            or active_request.location
            is not CompactionActiveRequestLocation.SNAPSHOT_EXACT
        ):
            raise CompactionPlanningError(
                "model-switch projection lacks an exact active request"
            )
        if target_branch is CompactionTargetBranch.IDLE_BASE_ONLY and (
            continuation[0] is not CompactionContinuationMode.AWAIT_NEXT_USER
            or active_request is not None
        ):
            raise CompactionPlanningError(
                "idle model-switch projection acquired an active request"
            )
        groups = enumerate_complete_tool_groups(compaction_read)
        tail, prefix = freeze_tail_and_prefix(
            source_view=source_view,
            complete_tool_groups=groups,
            retained_group_count=0,
        )
        if prefix.source_through_sequence != source_view.exact_safe_canonical_head:
            raise CompactionPlanningError(
                "model-switch projection does not cover the safe canonical head"
            )
        recent = select_recent_human_messages(
            canonical_read=compaction_read,
            source_through_sequence=prefix.source_through_sequence,
            policy=policy,
            excluded_entry_id=(
                None if active_request is None else active_request.entry_id
            ),
        )
        projection_plan = freeze_destination_dialogue_projection_plan(
            canonical_read=compaction_read,
            active_request=active_request,
        )
        if project_images_for_text_only:
            projection_plan = project_destination_dialogue_plan_for_text_only_handover(
                projection_plan
            )
        summary_call = self._model.resolve_compaction_summary_call(
            active_prepared_call=dispatch.prepared_call
        )
        trigger_tokens = int(
            dispatch.prepared_call.compile_binding.effective_input_budget_tokens
            * policy.auto_trigger_ratio
        )

        async def measure(
            projection: DestinationDialogueProjection,
        ) -> tuple[
            PreparedCompactionSummarySemantic,
            PreparedWireMeasurementDecision,
        ]:
            if monotonic() >= deadline:
                raise TimeoutError("destination projection planning deadline expired")
            semantic = prepare_destination_projection_summary_semantic(
                call=summary_call,
                canonical_source=compaction_read,
                compile_binding=dispatch.prepared_call.compile_binding,
                native_projection_set=(
                    dispatch.tool_exposure_plan.direct_projection_set
                ),
                source_projection=dispatch.projection.projected_input,
                projection=projection,
                active_request=active_request,
                summary_request=summary_request,
                resolved_trigger_tokens=trigger_tokens,
            )
            decision = await self._provider_dispatch.measure_prepared_wire_candidate(
                semantic,
                deadline=deadline,
            )
            return semantic, decision

        selected_projection: DestinationDialogueProjection | None = None
        selected_semantic: PreparedCompactionSummarySemantic | None = None
        selected_decision: PreparedWireMeasurementDecision | None = None
        for raw_projection in enumerate_destination_backbone_projections(
            projection_plan
        ):
            projection = (
                project_destination_dialogue_for_text_only_handover(raw_projection)
                if project_images_for_text_only
                else raw_projection
            )
            semantic, decision = await measure(projection)
            if (
                decision.wire_input_plan is not None
                and decision.quote.final_wire_estimated_input_tokens < trigger_tokens
            ):
                selected_projection = projection
                selected_semantic = semantic
                selected_decision = decision
                break
        if (
            selected_projection is None
            or selected_semantic is None
            or selected_decision is None
        ):
            return None

        while True:
            challengers: list[
                tuple[
                    tuple[int, int, int, int],
                    DestinationDialogueProjection,
                    PreparedCompactionSummarySemantic,
                    PreparedWireMeasurementDecision,
                ]
            ] = []
            for evidence in selected_projection.eligible_evidence:
                raw_projection = retain_destination_tool_evidence(
                    selected_projection,
                    evidence,
                )
                projection = (
                    project_destination_dialogue_for_text_only_handover(raw_projection)
                    if project_images_for_text_only
                    else raw_projection
                )
                semantic, decision = await measure(projection)
                if (
                    decision.wire_input_plan is None
                    or decision.quote.final_wire_estimated_input_tokens
                    >= trigger_tokens
                ):
                    continue
                challengers.append(
                    (
                        (
                            decision.quote.final_wire_estimated_input_tokens
                            - selected_decision.quote.final_wire_estimated_input_tokens,
                            decision.quote.final_wire_utf8_bytes
                            - selected_decision.quote.final_wire_utf8_bytes,
                            -(evidence.result_entry_sequence or -1),
                            evidence.outcome_ordinal,
                        ),
                        projection,
                        semantic,
                        decision,
                    )
                )
            if monotonic() >= deadline:
                raise TimeoutError("destination projection planning deadline expired")
            if not challengers:
                break
            _cost, selected_projection, selected_semantic, selected_decision = min(
                challengers,
                key=lambda item: item[0],
            )

        return _DestinationProjectionWinner(
            projection=selected_projection,
            semantic=selected_semantic,
            decision=selected_decision,
            tail=tail,
            prefix=prefix,
            recent=recent,
            continuation=continuation,
        )

    async def _execute_compaction_fenced(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        trigger: CompactionTrigger,
        force: bool,
        expected_scope: CompactionScope,
        post_adoption_branch: CompactionTargetBranch,
        stable_command_id: str | None,
        maximum_retained_tool_groups: int | None = None,
        attempt_token: CompactionAttemptToken | None = None,
        pre_compact_dispatched: bool = False,
        hook_scope: HookDispatchScopeRef | None = None,
        session_start_compact_port: SessionStartCompactPort | None = None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None = None,
        model_switch_candidate: ModelSwitchCompactionTriggerCandidate | None = None,
        pending_root_candidate: PreparedRootProviderInputCandidate | None = None,
        pending_active_candidate: PreparedActiveRootInputCandidate | None = None,
    ) -> CompactionExecutionResult:
        if attempt_token is None:
            attempt_token = _new_compaction_attempt_token(
                session_id=expected_scope.session_id,
                turn_id=turn_id,
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=expected_scope.scope_subagent_task_id,
                trigger=trigger,
            )
        current_maximum = maximum_retained_tool_groups
        current_pre_compact_dispatched = pre_compact_dispatched
        model_switch_tier: Literal[2, 3] | None = (
            None
            if model_switch_candidate is None
            else 2
            if model_switch_candidate.source_target is not None
            else 3
        )
        while True:
            result = await self._execute_compaction_fenced_once(
                turn_id=turn_id,
                model_call_index=model_call_index,
                inherited_memory_use_policy=inherited_memory_use_policy,
                trigger=trigger,
                force=force,
                expected_scope=expected_scope,
                post_adoption_branch=post_adoption_branch,
                stable_command_id=stable_command_id,
                maximum_retained_tool_groups=current_maximum,
                attempt_token=attempt_token,
                pre_compact_dispatched=current_pre_compact_dispatched,
                hook_scope=hook_scope,
                session_start_compact_port=session_start_compact_port,
                session_start_boundary_port=session_start_boundary_port,
                model_switch_candidate=model_switch_candidate,
                model_switch_tier=model_switch_tier,
                pending_root_candidate=pending_root_candidate,
                pending_active_candidate=pending_active_candidate,
            )
            if isinstance(result, _ModelSwitchTier3Fallback):
                owner = self._compaction_owner
                if owner is None:
                    raise RuntimeError("compaction restart lost its Host owner")
                owner.restart_preparation_phase(attempt_token=attempt_token)
                model_switch_tier = 3
                current_pre_compact_dispatched = result.pre_compact_dispatched
                continue
            if not isinstance(result, _CompactionFencedRestart):
                return result
            owner = self._compaction_owner
            if owner is None:
                raise RuntimeError("compaction restart lost its Host owner")
            owner.restart_preparation_phase(attempt_token=attempt_token)
            current_maximum = result.maximum_retained_tool_groups
            current_pre_compact_dispatched = result.pre_compact_dispatched

    async def _execute_compaction_fenced_once(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        trigger: CompactionTrigger,
        force: bool,
        expected_scope: CompactionScope,
        post_adoption_branch: CompactionTargetBranch,
        stable_command_id: str | None,
        maximum_retained_tool_groups: int | None,
        attempt_token: CompactionAttemptToken,
        pre_compact_dispatched: bool,
        hook_scope: HookDispatchScopeRef | None,
        session_start_compact_port: SessionStartCompactPort | None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None,
        model_switch_candidate: ModelSwitchCompactionTriggerCandidate | None,
        model_switch_tier: Literal[2, 3] | None,
        pending_root_candidate: PreparedRootProviderInputCandidate | None,
        pending_active_candidate: PreparedActiveRootInputCandidate | None,
    ) -> (
        CompactionExecutionResult | _CompactionFencedRestart | _ModelSwitchTier3Fallback
    ):
        owner = self._compaction_owner
        if owner is None:
            raise RuntimeError("compaction lacks its Host owner")
        if (pending_root_candidate is not None) != (
            post_adoption_branch is CompactionTargetBranch.IDLE_BASE_ONLY
        ) and pending_root_candidate is not None:
            raise ValueError("pending ROOT candidate requires idle compaction")
        if pending_active_candidate is not None and (
            post_adoption_branch is not CompactionTargetBranch.ACTIVE_INSTALLATION
            or pending_root_candidate is not None
            or expected_scope.scope_kind is not ModelInputScopeKind.ROOT
            or expected_scope.scope_subagent_task_id is not None
            or pending_active_candidate.expected_provider_input_cut.turn_id != turn_id
            or pending_active_candidate.expected_provider_input_cut.session_id
            != expected_scope.session_id
        ):
            raise ValueError("pending active ROOT candidate target is invalid")
        text_only_handover = (
            model_switch_candidate is not None
            and _target_is_known_text_only(model_switch_candidate.destination_target)
        )
        deadline = monotonic() + owner.policy.planning_attempt_seconds
        raw_handle: PreparedProviderInputHandle | None = None
        if model_switch_candidate is not None and model_switch_tier == 3:
            prepare_surface = getattr(
                self._tools, "prepare_tool_surface_safe_point", None
            )
            if prepare_surface is not None:
                prepare_surface()
            terminal_source = (
                post_adoption_branch is CompactionTargetBranch.IDLE_BASE_ONLY
            )
            freeze_operation = (
                self._safe_point.freeze_compaction_input
                if terminal_source
                else self._safe_point.freeze_provider_input
            )
            raw_handle = await self._io.run(
                freeze_operation,
                turn_id=turn_id,
                **({"allow_terminal": True} if terminal_source else {}),
                deadline_monotonic=deadline,
            )
            try:
                compaction_read = await self._io.run(
                    self._input_reader.read_frozen_compaction_cut,
                    raw_handle.cut,
                    deadline_monotonic=deadline,
                )
                projected_read = (
                    project_compaction_read_for_text_only_handover(compaction_read)
                    if text_only_handover
                    else compaction_read
                )
                transferred_handle = raw_handle
                raw_handle = None
                dispatch = await self._provider_dispatch.prepare_compaction_source(
                    turn_id=turn_id,
                    model_call_index=model_call_index,
                    inherited_memory_use_policy=inherited_memory_use_policy,
                    deadline=deadline,
                    existing_handle=transferred_handle,
                    prepared_target_override=(
                        model_switch_candidate.destination_target
                    ),
                    canonical_read_override=projected_read.dispatch_read,
                    expected_source_read=compaction_read.dispatch_read,
                    destination_projection_source=True,
                )
            finally:
                if raw_handle is not None:
                    raw_handle.close()
        else:
            source_target_override = None
            if model_switch_candidate is not None:
                if model_switch_candidate.source_target is None:
                    raise RuntimeError("Tier 2 lacks its installed source target")
                try:
                    source_target_override = (
                        self._provider_dispatch.prepare_installed_source_target(
                            scope=ProviderInputContinuityScope(
                                session_id=expected_scope.session_id,
                                scope_kind=expected_scope.scope_kind,
                                scope_subagent_task_id=(
                                    expected_scope.scope_subagent_task_id
                                ),
                            ),
                            source=model_switch_candidate.source_target,
                            turn_id=turn_id,
                            model_call_index=model_call_index,
                        )
                    )
                except ValueError:
                    return _ModelSwitchTier3Fallback(
                        pre_compact_dispatched=pre_compact_dispatched
                    )
            try:
                dispatch = await self._provider_dispatch.prepare_compaction_source(
                    turn_id=turn_id,
                    model_call_index=model_call_index,
                    inherited_memory_use_policy=inherited_memory_use_policy,
                    deadline=deadline,
                    allow_terminal_compaction=(
                        post_adoption_branch is CompactionTargetBranch.IDLE_BASE_ONLY
                    ),
                    prepared_target_override=source_target_override,
                )
            except StructuredModelInputCompileError as exc:
                if (
                    model_switch_candidate is not None
                    and model_switch_tier == 2
                    and _can_enter_model_switch_tier_three(exc)
                ):
                    return _ModelSwitchTier3Fallback(
                        pre_compact_dispatched=pre_compact_dispatched
                    )
                raise
            try:
                compaction_read = await self._io.run(
                    self._input_reader.read_frozen_compaction_cut,
                    dispatch.cut,
                    deadline_monotonic=deadline,
                )
                projected_read = compaction_read
            except BaseException:
                dispatch.close()
                raise
        try:
            source_quote = dispatch.discard_wire_materialization_to_quote()
            source_view = freeze_compaction_source_view(
                canonical_read=projected_read,
                compile_binding=dispatch.prepared_call.compile_binding,
                semantic_projection=dispatch.projection,
                predecessor_epoch_view=dispatch.planning.predecessor_view,
                provider_wire_quote=source_quote,
                producer_wire_api=(
                    dispatch.prepared_call.call.target.model_profile.route_wire_profile.wire_api
                ),
            )
        except BaseException:
            dispatch.close()
            raise
        dry_dispatch: PreparedCompactionDryProjection | None = None
        pending_empty_adoption: object | None = None
        prospective_root_dispatch: PreparedProspectiveRootDispatch | None = None
        candidate_family: PreparedCompactionCandidateFamily | None = None
        prospective_root_family: PreparedProspectiveRootCandidateFamily | None = None
        next_handle: PreparedProviderInputHandle | None = None
        source_handle_transferred = False
        try:
            expected_statuses = (
                {"RUNNING"}
                if post_adoption_branch is CompactionTargetBranch.ACTIVE_INSTALLATION
                else {"COMPLETED", "INTERRUPTED"}
            )
            if (
                compaction_read.scope != expected_scope
                or compaction_read.turn_status not in expected_statuses
            ):
                raise CompactionPlanningError("compaction target changed at admission")
            if not should_trigger_compaction(
                source_view=source_view,
                policy=owner.policy,
                force=force,
            ):
                return CompactionExecutionResult(
                    CompactionOutcome(
                        CompactionDisposition.NOT_NEEDED,
                        turn_id,
                        None,
                        None,
                        "BELOW_TRIGGER",
                    )
                )
            if not pre_compact_dispatched:
                pre_compact_dispatched = True
                proceed, reason = await self._dispatch_pre_compact(
                    dispatch=dispatch,
                    scope=hook_scope,
                    attempt_token=attempt_token,
                    turn_id=turn_id,
                    trigger=trigger,
                    deadline=deadline,
                )
                if not proceed:
                    return CompactionExecutionResult(
                        CompactionOutcome(
                            CompactionDisposition.FAILED,
                            turn_id,
                            None,
                            None,
                            "HOOK_PRE_COMPACT_BLOCKED"
                            + (f":{reason}" if reason else ""),
                        )
                    )
            summary_request = compaction_summary_request()
            # Installed-prefix summarization includes the whole previous base.
            # The destination tier may deliberately omit it to fit its budget.
            summary_includes_previous_base = True
            if model_switch_candidate is not None and model_switch_tier == 3:
                destination_winner = await self._select_destination_projection_summary(
                    dispatch=dispatch,
                    source_view=source_view,
                    compaction_read=compaction_read,
                    policy=owner.policy,
                    summary_request=summary_request,
                    project_images_for_text_only=text_only_handover,
                    target_branch=post_adoption_branch,
                    deadline=deadline,
                )
                if destination_winner is None:
                    return CompactionExecutionResult(
                        CompactionOutcome(
                            CompactionDisposition.FAILED,
                            turn_id,
                            None,
                            None,
                            "NO_EXECUTABLE_DESTINATION_PROJECTION",
                        )
                    )
                semantic = destination_winner.semantic
                selected_summary_decision = destination_winner.decision
                tail = destination_winner.tail
                prefix = destination_winner.prefix
                recent = destination_winner.recent
                selected_continuation = destination_winner.continuation
                selected_retained_count = 0
                summary_includes_previous_base = (
                    destination_winner.projection.prior_handoff is not None
                )
            else:
                installed_winner = await self._select_installed_prefix_summary(
                    dispatch=dispatch,
                    source_view=source_view,
                    compaction_read=compaction_read,
                    policy=owner.policy,
                    maximum_retained_tool_groups=maximum_retained_tool_groups,
                    target_branch=post_adoption_branch,
                    force_zero_retained_groups=(model_switch_candidate is not None),
                    summary_request=summary_request,
                    deadline=deadline,
                )
                if installed_winner is None:
                    if model_switch_candidate is not None and model_switch_tier == 2:
                        return _ModelSwitchTier3Fallback(
                            pre_compact_dispatched=pre_compact_dispatched
                        )
                    return CompactionExecutionResult(
                        CompactionOutcome(
                            CompactionDisposition.FAILED,
                            turn_id,
                            None,
                            None,
                            "NO_EXECUTABLE_SUMMARY_PREFIX",
                        )
                    )
                semantic = installed_winner.semantic
                selected_summary_decision = installed_winner.decision
                tail = installed_winner.tail
                prefix = installed_winner.prefix
                recent = installed_winner.recent
                selected_continuation = installed_winner.continuation
                selected_retained_count = installed_winner.retained_tool_group_count
            if selected_summary_decision.wire_input_plan is None:
                if model_switch_candidate is not None and model_switch_tier == 2:
                    return _ModelSwitchTier3Fallback(
                        pre_compact_dispatched=pre_compact_dispatched
                    )
                raise CompactionPlanningError(
                    "selected compaction summary is not executable"
                )
            successor_destination = (
                model_switch_candidate.destination_target.epoch_call_target
                if model_switch_candidate is not None
                else dispatch.prepared_call.epoch_call_target
            )
            promotion_authority = owner.issue_summary_promotion_authority(
                attempt_token=attempt_token,
                semantic=semantic,
                decision=selected_summary_decision,
                successor_destination=successor_destination,
                required_phase=CompactionAttemptPhase.PREPARING,
            )
            prepared_summary = promote_compaction_summary_call(
                semantic,
                decision=selected_summary_decision,
                model_runtime=self._model.model_runtime,
                timeout_policy=self._model.transport_timeout_policy,
                successor_destination=successor_destination,
                promotion_authority=promotion_authority,
            )
            owner.advance_phase(
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=expected_scope.scope_subagent_task_id,
                phase=CompactionAttemptPhase.STREAMING,
            )
            # The provider transport owns connect/write/read-idle watchdogs.
            # A progressing summary stream has no independent total deadline,
            # matching ordinary foreground model execution.
            try:
                raw = await prepared_summary.open_once()
            except (ProviderModelExecutionFailed, ProviderModelOutputIncomplete):
                if model_switch_candidate is not None and model_switch_tier == 2:
                    return _ModelSwitchTier3Fallback(
                        pre_compact_dispatched=pre_compact_dispatched
                    )
                raise
            if raw.tool_calls:
                # Tool calls are never dispatched.  One independent repair is
                # permitted; it appends a provider-valid ephemeral denial group
                # to the exact first request rather than replaying that request.
                owner.advance_phase(
                    scope_kind=expected_scope.scope_kind,
                    scope_subagent_task_id=expected_scope.scope_subagent_task_id,
                    phase=CompactionAttemptPhase.REPAIRING,
                )
                repair_deadline = monotonic() + owner.policy.planning_attempt_seconds
                repair_semantic = prepare_compaction_summary_repair_semantic(
                    semantic,
                    tool_calls=raw.tool_calls,
                )
                repair_decision = (
                    await self._provider_dispatch.measure_prepared_wire_candidate(
                        repair_semantic,
                        deadline=repair_deadline,
                    )
                )
                if repair_decision.wire_input_plan is None:
                    if model_switch_candidate is not None and model_switch_tier == 2:
                        return _ModelSwitchTier3Fallback(
                            pre_compact_dispatched=pre_compact_dispatched
                        )
                    raise CompactionPlanningError(
                        "summary repair exceeds final-wire admission"
                    )
                repair_authority = owner.issue_summary_promotion_authority(
                    attempt_token=attempt_token,
                    semantic=repair_semantic,
                    decision=repair_decision,
                    successor_destination=successor_destination,
                    required_phase=CompactionAttemptPhase.REPAIRING,
                )
                repair = promote_compaction_summary_call(
                    repair_semantic,
                    decision=repair_decision,
                    predecessor_summary_wire_plan=(prepared_summary.wire_input_plan),
                    model_runtime=self._model.model_runtime,
                    timeout_policy=self._model.transport_timeout_policy,
                    successor_destination=successor_destination,
                    promotion_authority=repair_authority,
                )
                try:
                    raw = await repair.open_once()
                except (
                    ProviderModelExecutionFailed,
                    ProviderModelOutputIncomplete,
                ):
                    if model_switch_candidate is not None and model_switch_tier == 2:
                        return _ModelSwitchTier3Fallback(
                            pre_compact_dispatched=pre_compact_dispatched
                        )
                    raise
                if raw.tool_calls:
                    if model_switch_candidate is not None and model_switch_tier == 2:
                        return _ModelSwitchTier3Fallback(
                            pre_compact_dispatched=pre_compact_dispatched
                        )
                    raise CompactionPlanningError(
                        "summary model attempted tools after one repair"
                    )
                del repair, repair_decision, repair_semantic
            del prepared_summary, selected_summary_decision, semantic
            try:
                summary = freeze_compaction_summary_output(
                    raw.text,
                    maximum_utf8_bytes=(MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES),
                )
            except ValueError:
                if model_switch_candidate is not None and model_switch_tier == 2:
                    return _ModelSwitchTier3Fallback(
                        pre_compact_dispatched=pre_compact_dispatched
                    )
                raise
            owner.advance_phase(
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=expected_scope.scope_subagent_task_id,
                phase=CompactionAttemptPhase.VALIDATED,
            )
            if model_switch_candidate is not None:
                if text_only_handover and (
                    model_switch_tier == 3 or recent_human_window_has_image(recent)
                ):
                    recent = ()
            retained_historical_requests = (
                ()
                if summary_includes_previous_base
                else (
                    ()
                    if compaction_read.snapshot_carrier is None
                    else compaction_read.snapshot_carrier.retained_historical_requests
                )
            )
            if text_only_handover and model_switch_tier == 3:
                retained_historical_requests = tuple(
                    project_retained_request_for_text_only_handover(item)
                    for item in retained_historical_requests
                )
            recent_candidates = (
                (tuple(recent),)
                if model_switch_candidate is not None
                else _ordinary_recent_suffixes(tuple(recent))
            )
            first_recent = recent_candidates[0]
            first_carrier = build_compaction_snapshot_carrier(
                summary=summary,
                recent_human_requests=tuple(
                    FrozenRetainedHistoricalRequest(
                        item_kind=FrozenProviderInputItemKind.USER,
                        input_origin=item.input_origin,
                        content=item.content,
                    )
                    for item in first_recent
                ),
                continuation_mode=selected_continuation[0],
                active_request=selected_continuation[1],
                retained_historical_requests=retained_historical_requests,
            )
            first_content = self._content_publisher.describe(
                workspace_id=compaction_read.scope.workspace_id,
                content=first_carrier.body,
                media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
                codec=CONTEXT_SNAPSHOT_CODEC,
            )
            initial_carrier_candidate = (first_carrier, first_content)
            del first_carrier, first_content
            successor_deadline = monotonic() + owner.policy.planning_attempt_seconds
            canonical = compaction_read.dispatch_read.compile_snapshot.canonical_input
            boundary_range = freeze_compaction_canonical_range(
                scope=compaction_read.scope,
                effective_materialization_lineage_floor=(
                    compaction_read.lineage_base.effective_materialization_lineage_floor
                ),
                source_through_sequence=prefix.source_through_sequence,
                ordered_items=canonical.items,
                closures=canonical.closures,
                late_outcomes=canonical.late_outcomes,
            )
            source_digest = canonical_compaction_range_digest(
                compaction_read.lineage_base, boundary_range
            )
            stable_suffix = (
                manual_compaction_stable_suffix(
                    session_id=expected_scope.session_id,
                    command_id=stable_command_id,
                )
                if stable_command_id is not None
                else _stable_id(
                    "compaction",
                    expected_scope.session_id,
                    expected_scope.turn_id,
                    trigger.value,
                    source_digest,
                ).rsplit(":", 1)[-1]
            )
            lineage = compaction_read.lineage_base
            preconditions = CompactionCanonicalWritePreconditions(
                scope=compaction_read.scope,
                expected_turn_status=compaction_read.turn_status,
                expected_safe_head=source_view.exact_safe_canonical_head,
                provider_safe=True,
            )
            (
                runtime_source,
                _runtime_handoff,
            ) = await self._freeze_compaction_runtime_source(
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                maximum_utf8_bytes=(owner.policy.maximum_runtime_handoff_utf8_bytes),
                deadline=successor_deadline,
            )
            owner.advance_phase(
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                phase=CompactionAttemptPhase.ADOPTION_PREPARED,
            )
            next_handle = dispatch.take_handle()
            source_handle_transferred = True
            for recent_index, selected_recent in enumerate(recent_candidates):
                # Dry planning carries only the deterministic body descriptor.
                # Actual body/images publish atomically inside canonical adoption.
                if recent_index == 0:
                    carrier, content = initial_carrier_candidate
                    initial_carrier_candidate = None
                else:
                    carrier = build_compaction_snapshot_carrier(
                        summary=summary,
                        recent_human_requests=tuple(
                            FrozenRetainedHistoricalRequest(
                                item_kind=FrozenProviderInputItemKind.USER,
                                input_origin=item.input_origin,
                                content=item.content,
                            )
                            for item in selected_recent
                        ),
                        continuation_mode=selected_continuation[0],
                        active_request=selected_continuation[1],
                        retained_historical_requests=retained_historical_requests,
                    )
                    content = self._content_publisher.describe(
                        workspace_id=compaction_read.scope.workspace_id,
                        content=carrier.body,
                        media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
                        codec=CONTEXT_SNAPSHOT_CODEC,
                    )
                candidate = build_prepared_compaction_canonical_adoption(
                    CompactionCanonicalAdoptionFactoryInput(
                        scope=compaction_read.scope,
                        target_branch=post_adoption_branch,
                        expected_turn_status=compaction_read.turn_status,
                        predecessor=ExpectedCompactionPredecessorRevision(
                            binding_revision_id=lineage.binding_revision_id,
                            revision_ordinal=lineage.binding_revision_ordinal,
                            base_kind=(
                                "FULL_HISTORY"
                                if lineage.snapshot_id is None
                                else "SNAPSHOT"
                            ),
                            context_snapshot_id=lineage.snapshot_id,
                            source_through_sequence=(
                                lineage.persisted_revision_genesis_marker
                            ),
                        ),
                        snapshot_id=f"context-snapshot:{stable_suffix}",
                        binding_revision_id=f"context-binding:{stable_suffix}",
                        event_id=f"event:{stable_suffix}",
                        source_through_sequence=prefix.source_through_sequence,
                        source_digest=source_digest,
                        snapshot_content=content,
                        snapshot_carrier=carrier,
                        compiler_contract=COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
                        prompt_contract=COMPACTION_SUMMARY_PROMPT_CONTRACT,
                        model_contract=COMPACTION_MODEL_CONTRACT,
                        occurred_at=datetime.now(timezone.utc),
                        actor_id=self._writer_lease.guard.writer_owner_id,
                    )
                )
                synthetic_read = build_synthetic_compaction_dispatch_read(
                    canonical_read=compaction_read,
                    source_through_sequence=prefix.source_through_sequence,
                    snapshot_id=candidate.snapshot.snapshot_id,
                    binding_revision_id=candidate.binding.binding_revision_id,
                    binding_revision_ordinal=candidate.binding.revision_ordinal,
                    snapshot_carrier=carrier,
                    snapshot_content_digest=content.digest,
                    snapshot_content_size=content.size,
                    snapshot_content_media_type=content.media_type,
                    snapshot_content_codec=content.codec,
                    snapshot_blob_id=getattr(content, "blob_id", None),
                )
                # Every candidate crosses the same cold-base assembly path.
                # Lifecycle continuation is interpreted only after adoption.
                seed = CompactionDryProjectionSeed(
                    dispatch_read=synthetic_read,
                    binding_rewrite_identity=candidate.binding.binding_revision_id,
                    protected_tail_selection_fingerprint=(
                        tail.protected_tail_selection_fingerprint
                    ),
                )
                dry_wire_candidate = None
                dry_wire_decision = None
                prepared_candidate: PreparedCompactionCandidate | None = None
                pending_prepared_candidate: PreparedCompactionCandidate | None = None
                pending_wire_decision: PreparedWireMeasurementDecision | None = None
                prepared_prospective: PreparedProspectiveRootCandidate | None = None
                try:
                    if candidate_family is None:
                        handle_for_prepare = next_handle
                        next_handle = None
                        if handle_for_prepare is None:
                            raise RuntimeError(
                                "compaction candidate family lost its safe-point handle"
                            )
                        candidate_family = await self._provider_dispatch.prepare_compaction_candidate_family(
                            turn_id=turn_id,
                            model_call_index=model_call_index,
                            inherited_memory_use_policy=(inherited_memory_use_policy),
                            deadline=successor_deadline,
                            canonical_read=synthetic_read,
                            expected_source_read=compaction_read.dispatch_read,
                            seed=seed,
                            existing_handle=handle_for_prepare,
                            source_replacements=(runtime_source,),
                            retained_skill_read=compaction_read,
                            prepared_target_override=(
                                None
                                if model_switch_candidate is None
                                else model_switch_candidate.destination_target
                            ),
                            dry_projection=True,
                            source_planning_basis=dispatch.planning,
                        )
                    prepared_candidate = (
                        await self._provider_dispatch.prepare_compaction_candidate(
                            candidate_family,
                            canonical_read=synthetic_read,
                            seed=seed,
                            deadline=successor_deadline,
                        )
                    )
                    dry_wire_candidate = prepared_candidate.wire_candidate
                    dry_wire_decision = (
                        await self._provider_dispatch.measure_prepared_wire_candidate(
                            dry_wire_candidate,
                            deadline=successor_deadline,
                        )
                    )
                    if model_switch_candidate is None:
                        validate_compaction_wire_transition(
                            source_view=source_view,
                            source_candidate=dispatch.wire_candidate,
                            successor_wire=dry_wire_decision,
                            policy=owner.policy,
                            force=force,
                            enforce_soft_target=True,
                            phase="PRE_FULL",
                        )
                    else:
                        validate_model_switch_wire_transition(
                            source_view=source_view,
                            source_candidate=dispatch.wire_candidate,
                            successor_wire=dry_wire_decision,
                            destination_target=(
                                model_switch_candidate.destination_target
                            ),
                            policy=owner.policy,
                            phase="PRE_FULL",
                        )
                    if pending_active_candidate is not None:
                        rebased_active = _rebase_pending_active_candidate(
                            pending_active_candidate,
                            synthetic_read,
                        )
                        prospective_active_read = self._provider_dispatch.build_prospective_active_root_dispatch_read(
                            synthetic_read,
                            rebased_active,
                            deadline=successor_deadline,
                        )
                        pending_prepared_candidate = (
                            await self._provider_dispatch.prepare_compaction_candidate(
                                candidate_family,
                                canonical_read=prospective_active_read,
                                seed=replace(
                                    seed, dispatch_read=prospective_active_read
                                ),
                                deadline=successor_deadline,
                            )
                        )
                        pending_wire_decision = await self._provider_dispatch.measure_prepared_wire_candidate(
                            pending_prepared_candidate.wire_candidate,
                            deadline=successor_deadline,
                        )
                        if pending_wire_decision.wire_input_plan is None:
                            raise StructuredModelInputCompileError(
                                ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED
                            )
                    if pending_root_candidate is not None:
                        rebased_candidate = replace(
                            pending_root_candidate,
                            context_base_kind=ContextBindingBaseKind.SNAPSHOT,
                            context_snapshot_id=candidate.snapshot.snapshot_id,
                            source_through_sequence=(
                                candidate.snapshot.source_through_sequence
                            ),
                        )
                        if prospective_root_family is None:
                            prospective_read = await self._io.run(
                                self._input_reader.build_prospective_root_dispatch_from_snapshot,
                                rebased_candidate,
                                snapshot_dispatch=synthetic_read,
                                deadline_monotonic=successor_deadline,
                            )
                            prospective_root_family = await self._provider_dispatch.prepare_prospective_root_candidate_family(
                                candidate=rebased_candidate,
                                inherited_memory_use_policy=(
                                    inherited_memory_use_policy
                                ),
                                deadline=successor_deadline,
                                canonical_read_override=prospective_read,
                                compaction_seed=replace(
                                    seed,
                                    dispatch_read=prospective_read,
                                ),
                                source_planning_basis=(candidate_family.planning_basis),
                            )
                        else:
                            prospective_read = await self._io.run(
                                self._input_reader.build_prospective_root_dispatch_sibling_from_snapshot,
                                rebased_candidate,
                                first_candidate=(
                                    prospective_root_family.first_candidate
                                ),
                                first_dispatch=(
                                    prospective_root_family.first_candidate_read
                                ),
                                snapshot_dispatch=synthetic_read,
                                deadline_monotonic=successor_deadline,
                            )
                        prepared_prospective = await self._provider_dispatch.prepare_prospective_root_candidate(
                            prospective_root_family,
                            candidate=rebased_candidate,
                            canonical_read=prospective_read,
                            deadline=successor_deadline,
                        )
                    dry_dispatch = (
                        self._provider_dispatch.bind_selected_compaction_dry_projection(
                            family=candidate_family,
                            selected=prepared_candidate,
                            decision=dry_wire_decision,
                        )
                    )
                    candidate_family = None
                    if isinstance(
                        dry_dispatch.result.basis.source,
                        PendingEmptyCompactionDrySource,
                    ):
                        pending_empty_adoption = self._safe_point.begin_empty_adoption(
                            continuity=self._continuity,
                            dry_projection=dry_dispatch,
                            attempt_token=attempt_token,
                            candidate=candidate,
                        )
                    if prepared_prospective is not None:
                        if prospective_root_family is None:
                            raise RuntimeError(
                                "prospective ROOT candidate lost its frozen family"
                            )
                        prospective_root_dispatch = self._provider_dispatch.bind_selected_prospective_root_candidate(
                            family=prospective_root_family,
                            selected=prepared_prospective,
                        )
                        prospective_root_family = None
                    prepared_prospective = None
                    break
                except CompactionWireTransitionDrift:
                    # Source/target authority changed.  Recent cannot repair it.
                    return _CompactionFencedRestart(
                        maximum_retained_tool_groups=maximum_retained_tool_groups,
                        pre_compact_dispatched=pre_compact_dispatched,
                    )
                except (
                    CompactionPlanningError,
                    StructuredModelInputCompileError,
                    ModelTargetCapabilityMismatch,
                ) as exc:
                    if (
                        model_switch_candidate is not None
                        and model_switch_tier == 2
                        and _can_enter_model_switch_tier_three(exc)
                    ):
                        return _ModelSwitchTier3Fallback(
                            pre_compact_dispatched=pre_compact_dispatched
                        )
                    has_smaller_recent = recent_index + 1 < len(recent_candidates)
                    if (
                        model_switch_candidate is None
                        and has_smaller_recent
                        and _can_retry_compaction_with_smaller_recent(exc)
                    ):
                        del (
                            dry_wire_candidate,
                            dry_wire_decision,
                            prepared_candidate,
                            pending_prepared_candidate,
                            pending_wire_decision,
                            candidate,
                            synthetic_read,
                            seed,
                            carrier,
                            content,
                        )
                        # The family retains one physical Tool borrow and the
                        # exact source observations while only recent changes.
                        continue
                    if (
                        selected_retained_count <= 0
                        or not _can_retry_compaction_with_smaller_tail(exc)
                    ):
                        if isinstance(exc, CompactionReclaimUnavailable):
                            return _already_compact_result(turn_id)
                        raise
                    # Recent is empty (or this is a one-candidate model switch).
                    # Only now restart at the next smaller protected tool tail.
                    return _CompactionFencedRestart(
                        maximum_retained_tool_groups=(selected_retained_count - 1),
                        pre_compact_dispatched=pre_compact_dispatched,
                    )
            else:  # pragma: no cover - every finite candidate exits explicitly
                raise AssertionError("compaction recent search produced no decision")
            # The winner keeps only its installed dry assembly.  Quote
            # materialization is no longer separately owned here.
            del dry_wire_decision, dry_wire_candidate
            if next_handle is not None:
                next_handle.close()
                next_handle = None
            owner.advance_phase(
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=expected_scope.scope_subagent_task_id,
                phase=CompactionAttemptPhase.SETTLING,
            )
            settlement_dispatch = dry_dispatch
            dry_dispatch = None
            settlement_operation = self._complete_compaction_settlement(
                turn_id=turn_id,
                model_call_index=model_call_index,
                inherited_memory_use_policy=inherited_memory_use_policy,
                force=force,
                expected_scope=expected_scope,
                target_branch=post_adoption_branch,
                candidate=candidate,
                preconditions=preconditions,
                dry_dispatch=settlement_dispatch,
                source_view=source_view,
                source_wire_candidate=dispatch.wire_candidate,
                protected_tail_selection_fingerprint=(
                    tail.protected_tail_selection_fingerprint
                ),
                compaction_read=compaction_read,
                trigger=trigger,
                attempt_token=attempt_token,
                hook_scope=hook_scope,
                hook_model_id=(
                    dispatch.prepared_call.call.target.fact.model_id
                    if model_switch_candidate is None
                    else model_switch_candidate.destination_target.target.fact.model_id
                ),
                hook_cwd=(
                    str(self._tools.snapshot_terminal_cwd())
                    if self._hook_dispatcher is not None
                    else ""
                ),
                session_start_compact_port=session_start_compact_port,
                session_start_boundary_port=session_start_boundary_port,
                model_switch_candidate=model_switch_candidate,
                model_switch_tier=model_switch_tier,
                prospective_root_dispatch=prospective_root_dispatch,
                pending_active_candidate=pending_active_candidate,
                pending_empty_adoption=pending_empty_adoption,
            )
            prospective_root_dispatch = None
            try:
                settlement = owner.start_settlement(
                    settlement_operation,
                    name=(
                        f"kernel-compaction-settlement:{candidate.snapshot.snapshot_id}"
                    ),
                )
            except BaseException:
                settlement_operation.close()
                if settlement_dispatch is not None:
                    settlement_dispatch.close()
                raise
            try:
                return await asyncio.shield(settlement)
            except asyncio.CancelledError:
                await settlement
                raise
        finally:
            if not source_handle_transferred:
                dispatch.close()
            if dry_dispatch is not None:
                dry_dispatch.close()
            if prospective_root_dispatch is not None:
                prospective_root_dispatch.close()
            if (
                candidate_family is not None
                and candidate_family.owns_resource_authority
            ):
                candidate_family.close()
            if (
                prospective_root_family is not None
                and prospective_root_family.owns_resources
            ):
                prospective_root_family.close()
            if next_handle is not None:
                next_handle.close()

    async def _complete_compaction_settlement(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        force: bool,
        expected_scope: CompactionScope,
        target_branch: CompactionTargetBranch,
        candidate: PreparedCompactionCanonicalAdoption,
        preconditions: CompactionCanonicalWritePreconditions,
        dry_dispatch: PreparedCompactionDryProjection | PreparedProviderDispatch | None,
        source_view: FrozenCompactionSourceView,
        source_wire_candidate: PreparedProviderWireCandidate,
        protected_tail_selection_fingerprint: str,
        compaction_read: FrozenCompactionCanonicalRead,
        trigger: CompactionTrigger,
        attempt_token: CompactionAttemptToken,
        hook_scope: HookDispatchScopeRef | None,
        hook_model_id: str,
        hook_cwd: str,
        session_start_compact_port: SessionStartCompactPort | None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None,
        model_switch_candidate: ModelSwitchCompactionTriggerCandidate | None,
        model_switch_tier: Literal[2, 3] | None,
        prospective_root_dispatch: PreparedProspectiveRootDispatch | None,
        pending_active_candidate: PreparedActiveRootInputCandidate | None,
        pending_empty_adoption: object | None,
    ) -> CompactionExecutionResult:
        """Drain canonical FULL and its exact process-local branch settlement."""

        owner = self._compaction_owner
        if owner is None:
            raise RuntimeError("compaction settlement lost its Host owner")
        continuity_scope = ProviderInputContinuityScope(
            session_id=expected_scope.session_id,
            scope_kind=expected_scope.scope_kind,
            scope_subagent_task_id=expected_scope.scope_subagent_task_id,
        )
        successor_dispatch: PreparedProviderDispatch | None = None
        prospective_active_input: PreparedProspectiveActiveRootInput | None = None
        adopted_outcome: CompactionOutcome | None = None
        no_continuation_retirement_started = False
        try:
            confirmation = await self._settle_compaction_adoption(
                candidate=candidate,
                preconditions=preconditions,
            )
            if confirmation.kind is CompactionConfirmationKind.NONE:
                if pending_empty_adoption is not None:
                    if not isinstance(dry_dispatch, PreparedCompactionDryProjection):
                        raise RuntimeError(
                            "pending Empty adoption lost its dry projection"
                        )
                    self._safe_point.settle_empty_adoption_none(
                        continuity=self._continuity,
                        dry_projection=dry_dispatch,
                        pending=pending_empty_adoption,
                    )
                    dry_dispatch = None
                raise CompactionPlanningError(
                    "compaction adoption remained uncommitted"
                )
            if confirmation.kind is CompactionConfirmationKind.CONFLICT:
                if pending_empty_adoption is not None:
                    if not isinstance(dry_dispatch, PreparedCompactionDryProjection):
                        raise RuntimeError(
                            "pending Empty adoption lost its dry projection"
                        )
                    self._safe_point.settle_empty_adoption_conflict(
                        continuity=self._continuity,
                        dry_projection=dry_dispatch,
                        pending=pending_empty_adoption,
                        confirmation=confirmation,
                    )
                    dry_dispatch = None
                raise ConversationKernelConflict(
                    "context compaction has a conflicting winner"
                )
            empty_adoption_resealed = pending_empty_adoption is not None
            if empty_adoption_resealed:
                if not isinstance(dry_dispatch, PreparedCompactionDryProjection):
                    raise RuntimeError("pending Empty adoption lost its dry projection")
                self._safe_point.settle_empty_adoption_full(
                    continuity=self._continuity,
                    dry_projection=dry_dispatch,
                    pending=pending_empty_adoption,
                    confirmation=confirmation,
                )
                dry_dispatch = None
                if prospective_root_dispatch is not None:
                    pre_adoption_root = prospective_root_dispatch
                    prospective_root_dispatch = None
                    rebased_candidate = pre_adoption_root.admission.candidate
                    rebased_read = pre_adoption_root.admission.canonical_read
                    pre_adoption_root.close()
                    prospective_root_dispatch = (
                        await self._provider_dispatch.prepare_prospective_root_input(
                            candidate=rebased_candidate,
                            inherited_memory_use_policy=inherited_memory_use_policy,
                            deadline=(
                                monotonic() + owner.policy.planning_attempt_seconds
                            ),
                            canonical_read_override=rebased_read,
                        )
                    )
            elif prospective_root_dispatch is not None:
                predecessor = self._continuity.current_cohort(continuity_scope)
                if predecessor is None:
                    raise RuntimeError(
                        "post-FULL prospective ROOT lacks its predecessor"
                    )
                prospective_seed = _issue_adopted_compaction_continuation_seed(
                    dispatch_read=prospective_root_dispatch.admission.canonical_read,
                    binding_rewrite_identity=(
                        candidate.binding.binding_revision_id
                    ),
                    protected_tail_selection_fingerprint=(
                        protected_tail_selection_fingerprint
                    ),
                    predecessor=predecessor,
                    destination=(
                        prospective_root_dispatch.prepared_call.epoch_call_target
                    ),
                    confirmation=confirmation,
                    attempt_token=attempt_token,
                    candidate=candidate,
                )
                prospective_root_dispatch.authorize_adopted_compaction_seed(
                    prospective_seed
                )
            post_hook_deadline = monotonic() + owner.policy.planning_attempt_seconds
            adopted_outcome = CompactionOutcome(
                CompactionDisposition.COMPACTED,
                turn_id,
                candidate.snapshot.snapshot_id,
                confirmation.revision_ordinal,
                (
                    "COMPACTED_CONTINUATION_UNAVAILABLE"
                    if target_branch is CompactionTargetBranch.ACTIVE_INSTALLATION
                    else "COMPACTED_POST_ADOPTION_FAILURE"
                ),
            )
            if (
                expected_scope.scope_kind is ModelInputScopeKind.ROOT
                and session_start_boundary_port is not None
            ):
                await session_start_boundary_port.arm_compact_boundary(
                    attempt_token=attempt_token,
                    adopted_snapshot_id=candidate.snapshot.snapshot_id,
                    adopted_binding_revision_id=(candidate.binding.binding_revision_id),
                )
            post_proceed, post_reason = await self._dispatch_post_compact(
                model_id=hook_model_id,
                cwd=hook_cwd,
                scope=hook_scope,
                attempt_token=attempt_token,
                candidate=candidate,
                turn_id=turn_id,
                trigger=trigger,
                deadline=post_hook_deadline,
            )
            if target_branch is CompactionTargetBranch.ACTIVE_INSTALLATION:
                if dry_dispatch is None and not empty_adoption_resealed:
                    raise RuntimeError("active compaction lost its dry assembly")
                base_deadline = monotonic() + owner.policy.planning_attempt_seconds
                status = await self._io.run(
                    self._repository.read_turn_status,
                    session_id=expected_scope.session_id,
                    turn_id=turn_id,
                    deadline_monotonic=base_deadline,
                )
                if status is not TurnStatus.RUNNING:
                    if not empty_adoption_resealed:
                        retiring_dispatch = dry_dispatch
                        dry_dispatch = None
                        no_continuation_retirement_started = True
                        self._retire_full_adoption_without_continuation(
                            scope=continuity_scope,
                            turn_id=turn_id,
                            attempt_token=attempt_token,
                            confirmation=confirmation,
                            candidate=candidate,
                            evidence=_issue_durable_turn_not_running_evidence(status),
                            dispatch=retiring_dispatch,
                        )
                    owner.reset_automatic_failures(
                        scope_kind=expected_scope.scope_kind,
                        scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                    )
                    return CompactionExecutionResult(
                        CompactionOutcome(
                            CompactionDisposition.COMPACTED,
                            turn_id,
                            candidate.snapshot.snapshot_id,
                            confirmation.revision_ordinal,
                            "HISTORICAL_COMPACTION_WINNER",
                        )
                    )
                if not post_proceed:
                    if not empty_adoption_resealed:
                        retiring_dispatch = dry_dispatch
                        dry_dispatch = None
                        no_continuation_retirement_started = True
                        self._retire_full_adoption_without_continuation(
                            scope=continuity_scope,
                            turn_id=turn_id,
                            attempt_token=attempt_token,
                            confirmation=confirmation,
                            candidate=candidate,
                            evidence=_issue_post_compact_blocked_evidence(
                                (post_proceed, post_reason)
                            ),
                            dispatch=retiring_dispatch,
                        )
                    owner.reset_automatic_failures(
                        scope_kind=expected_scope.scope_kind,
                        scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                    )
                    return CompactionExecutionResult(
                        CompactionOutcome(
                            CompactionDisposition.COMPACTED,
                            turn_id,
                            candidate.snapshot.snapshot_id,
                            confirmation.revision_ordinal,
                            "COMPACTED_CONTINUATION_BLOCKED",
                        ),
                        active_continuation_blocked_reason=(
                            post_reason or "PostCompact Hook blocked continuation"
                        ),
                    )
                rotation_handle = (
                    None
                    if empty_adoption_resealed
                    else dry_dispatch.take_handle_for_rotation()
                )
                dry_dispatch = None
                rotation_result: list[PreparedProviderInputHandle] = []

                def rotate_and_capture(
                    handle: PreparedProviderInputHandle,
                    *,
                    turn_id: str,
                    deadline_monotonic: float,
                ) -> PreparedProviderInputHandle:
                    successor = self._safe_point.rotate_provider_input(
                        handle,
                        turn_id=turn_id,
                        deadline_monotonic=deadline_monotonic,
                    )
                    rotation_result.append(successor)
                    return successor

                try:
                    if rotation_handle is None:
                        rotated = await self._io.run(
                            self._safe_point.freeze_compaction_input,
                            turn_id=turn_id,
                            allow_terminal=False,
                            deadline_monotonic=base_deadline,
                        )
                        rotation_result.append(rotated)
                    else:
                        rotated = await self._io.run(
                            rotate_and_capture,
                            rotation_handle,
                            turn_id=turn_id,
                            deadline_monotonic=base_deadline,
                        )
                except BaseException:
                    # KernelSessionIO drains a late/cancelled physical worker
                    # before raising.  Recover whichever exact handle the
                    # worker left active so ownership is never lost between
                    # the consumed dry dispatch and its successor.
                    if rotation_result:
                        rotation_result[0].close()
                    else:
                        if rotation_handle is not None:
                            rotation_handle.close()
                    raise
                rotated_owner: PreparedProviderInputHandle | None = rotated
                rebased_active: PreparedActiveRootInputCandidate | None = None
                try:
                    actual_read = await self._provider_dispatch.read_dispatch_read(
                        rotated_owner.cut, deadline=base_deadline
                    )
                    actual_facts = actual_read.compile_snapshot
                    actual_identity = actual_facts.canonical_input.identity
                    actual_binding = actual_facts.context_binding_fact
                    if (
                        actual_identity.turn_id != turn_id
                        or actual_identity.conversation_scope_kind
                        is not expected_scope.scope_kind
                        or actual_identity.scope_subagent_task_id
                        != expected_scope.scope_subagent_task_id
                        or actual_binding.binding_revision_id
                        != candidate.binding.binding_revision_id
                        or actual_binding.context_snapshot_id
                        != candidate.snapshot.snapshot_id
                    ):
                        raise ConversationKernelConflict(
                            "post-adoption cut does not name the adopted compaction winner"
                        )
                    (
                        current_runtime_source,
                        _current_runtime_handoff,
                    ) = await self._freeze_compaction_runtime_source(
                        scope_kind=expected_scope.scope_kind,
                        scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                        maximum_utf8_bytes=(
                            owner.policy.maximum_runtime_handoff_utf8_bytes
                        ),
                        deadline=base_deadline,
                    )
                    destination_epoch_target = (
                        model_switch_candidate.destination_target.epoch_call_target
                        if model_switch_candidate is not None
                        else source_wire_candidate.prepared_call.epoch_call_target
                    )
                    post_seed = (
                        CanonicalColdContinuationSeed(actual_read)
                        if empty_adoption_resealed
                        else self._safe_point.issue_adopted_compaction_continuation_seed(
                            handle=rotated_owner,
                            continuity=self._continuity,
                            confirmation=confirmation,
                            candidate=candidate,
                            attempt_token=attempt_token,
                            dispatch_read=actual_read,
                            destination=destination_epoch_target,
                            protected_tail_selection_fingerprint=(
                                protected_tail_selection_fingerprint
                            ),
                        )
                    )
                    prepare_handle = rotated_owner
                    rotated_owner = None
                    post_family: PreparedCompactionCandidateFamily | None = None
                    try:
                        transferred_prepare_handle = prepare_handle
                        prepare_handle = None
                        post_family = await self._provider_dispatch.prepare_compaction_candidate_family(
                            turn_id=turn_id,
                            model_call_index=model_call_index,
                            inherited_memory_use_policy=inherited_memory_use_policy,
                            deadline=base_deadline,
                            canonical_read=actual_read,
                            expected_source_read=actual_read,
                            seed=post_seed,
                            existing_handle=transferred_prepare_handle,
                            source_replacements=(current_runtime_source,),
                            retained_skill_read=compaction_read,
                            prepared_target_override=(
                                None
                                if model_switch_candidate is None
                                else model_switch_candidate.destination_target
                            ),
                        )
                        post_base_candidate = (
                            await self._provider_dispatch.prepare_compaction_candidate(
                                post_family,
                                canonical_read=actual_read,
                                seed=post_seed,
                                deadline=base_deadline,
                            )
                        )
                        post_base_decision = await self._provider_dispatch.measure_prepared_wire_candidate(
                            post_base_candidate.wire_candidate,
                            deadline=base_deadline,
                        )
                        if model_switch_candidate is None:
                            validate_compaction_wire_transition(
                                source_view=source_view,
                                source_candidate=source_wire_candidate,
                                successor_wire=post_base_decision,
                                policy=owner.policy,
                                force=force,
                                enforce_soft_target=True,
                                phase="POST_FULL",
                            )
                        else:
                            validate_model_switch_wire_transition(
                                source_view=source_view,
                                source_candidate=source_wire_candidate,
                                successor_wire=post_base_decision,
                                destination_target=(
                                    model_switch_candidate.destination_target
                                ),
                                policy=owner.policy,
                                phase="POST_FULL",
                            )
                        if post_base_decision.wire_input_plan is None:
                            raise CompactionPlanningError(
                                "post-adoption base lacks executable final-wire admission"
                            )
                        selected_post_candidate = post_base_candidate
                        selected_post_decision = post_base_decision
                        if pending_active_candidate is not None:
                            rebased_active = _rebase_pending_active_candidate(
                                pending_active_candidate,
                                actual_read,
                            )
                            prospective_active_read = self._provider_dispatch.build_prospective_active_root_dispatch_read(
                                actual_read,
                                rebased_active,
                                deadline=base_deadline,
                            )
                            selected_post_candidate = await self._provider_dispatch.prepare_compaction_candidate(
                                post_family,
                                canonical_read=prospective_active_read,
                                seed=(
                                    post_seed._with_dispatch_read(
                                        prospective_active_read
                                    )
                                    if isinstance(
                                        post_seed,
                                        AdoptedCompactionContinuationSeed,
                                    )
                                    else replace(
                                        post_seed,
                                        dispatch_read=prospective_active_read,
                                    )
                                ),
                                deadline=base_deadline,
                            )
                            selected_post_decision = await self._provider_dispatch.measure_prepared_wire_candidate(
                                selected_post_candidate.wire_candidate,
                                deadline=base_deadline,
                            )
                            if selected_post_decision.wire_input_plan is None:
                                raise StructuredModelInputCompileError(
                                    ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED
                                )
                        prepared_base = (
                            self._provider_dispatch.bind_selected_compaction_candidate(
                                family=post_family,
                                selected=selected_post_candidate,
                            )
                        )
                        post_family = None
                    finally:
                        if (
                            post_family is not None
                            and post_family.owns_execution_authority
                        ):
                            post_family.close()
                        if prepare_handle is not None:
                            prepare_handle.close()
                finally:
                    if rotated_owner is not None:
                        rotated_owner.close()
                if not isinstance(prepared_base, PreparedProviderDispatch):
                    raise RuntimeError("final compaction base is not installable")
                dry_dispatch = prepared_base
                compact_start: PreparedCompactSessionStart | None = None
                if expected_scope.scope_kind is ModelInputScopeKind.ROOT:
                    if session_start_compact_port is not None:
                        compact_start = await session_start_compact_port(
                            PreparedCompactSessionStartFacts(
                                attempt_token=attempt_token,
                                adopted_snapshot_id=candidate.snapshot.snapshot_id,
                                adopted_binding_revision_id=(
                                    candidate.binding.binding_revision_id
                                ),
                                scope=expected_scope,
                                turn_id=turn_id,
                                model_id=(
                                    dry_dispatch.prepared_call.call.target.fact.model_id
                                ),
                                permission_snapshot=(
                                    dry_dispatch.canonical_facts.run_permission_snapshot
                                ),
                                deadline_monotonic=base_deadline,
                            )
                        )
                        if not compact_start.proceed:
                            retiring_dispatch = dry_dispatch
                            dry_dispatch = None
                            no_continuation_retirement_started = True
                            self._retire_full_adoption_without_continuation(
                                scope=continuity_scope,
                                turn_id=turn_id,
                                attempt_token=attempt_token,
                                confirmation=confirmation,
                                candidate=candidate,
                                evidence=(
                                    _issue_session_start_compact_blocked_evidence(
                                        compact_start
                                    )
                                ),
                                dispatch=retiring_dispatch,
                            )
                            owner.reset_automatic_failures(
                                scope_kind=expected_scope.scope_kind,
                                scope_subagent_task_id=None,
                            )
                            return CompactionExecutionResult(
                                CompactionOutcome(
                                    CompactionDisposition.COMPACTED,
                                    turn_id,
                                    candidate.snapshot.snapshot_id,
                                    confirmation.revision_ordinal,
                                    "COMPACTED_CONTINUATION_BLOCKED",
                                ),
                                active_continuation_blocked_reason=(
                                    compact_start.reason
                                    or "compact SessionStart Hook blocked continuation"
                                ),
                            )
                elif session_start_compact_port is not None:
                    raise RuntimeError("child compaction received a ROOT start port")
                pending_start = (
                    None if compact_start is None else compact_start.reservation
                )
                selected_wire_decision = selected_post_decision
                hook_sibling = None
                hook_decision = None
                hook_probe_completed = False
                try:
                    hook_deadline = monotonic() + owner.policy.planning_attempt_seconds
                    try:
                        hook_sibling = (
                            await self._provider_dispatch.prepare_hook_context_sibling(
                                dry_dispatch,
                                model_call_index=model_call_index,
                                deadline=hook_deadline,
                            )
                        )
                        if hook_sibling is not None:
                            hook_decision = await self._provider_dispatch.measure_prepared_wire_candidate(
                                hook_sibling.candidate,
                                deadline=hook_deadline,
                            )
                            if (
                                model_switch_candidate is None
                                and pending_active_candidate is None
                            ):
                                validate_compaction_wire_transition(
                                    source_view=source_view,
                                    source_candidate=source_wire_candidate,
                                    successor_wire=hook_decision,
                                    policy=owner.policy,
                                    force=force,
                                    enforce_soft_target=True,
                                    phase="POST_FULL",
                                )
                            elif model_switch_candidate is not None:
                                validate_model_switch_wire_transition(
                                    source_view=source_view,
                                    source_candidate=source_wire_candidate,
                                    successor_wire=hook_decision,
                                    destination_target=(
                                        model_switch_candidate.destination_target
                                    ),
                                    policy=owner.policy,
                                    phase="POST_FULL",
                                )
                            if hook_decision.wire_input_plan is None:
                                raise CompactionPlanningError(
                                    "Hook sibling lacks executable final-wire admission"
                                )
                    except CompactionWireTransitionDrift:
                        # Target/cut/profile drift invalidates both siblings; it
                        # is not an optional Hook-only failure.
                        raise
                    except (
                        CompactionPlanningError,
                        StructuredModelInputCompileError,
                        TimeoutError,
                        ValueError,
                    ):
                        if hook_sibling is not None:
                            hook_sibling.retire()
                        hook_sibling = None
                        hook_decision = None
                    hook_probe_completed = True
                finally:
                    if (
                        not hook_probe_completed
                        and hook_sibling is not None
                        and hook_sibling.owns_reservation
                    ):
                        hook_sibling.retire()
                    if pending_start is not None:
                        pending_start.retire()
                if hook_sibling is not None:
                    assert hook_decision is not None
                    base_dispatch = dry_dispatch
                    try:
                        selected_dispatch = (
                            self._provider_dispatch.bind_selected_provider_dispatch(
                                base=base_dispatch,
                                sibling=hook_sibling,
                            )
                        )
                    except BaseException:
                        if hook_sibling.owns_reservation:
                            hook_sibling.retire()
                        if not base_dispatch.owns_execution_authority:
                            dry_dispatch = None
                        raise
                    dry_dispatch = selected_dispatch
                    selected_wire_decision = hook_decision
                if pending_active_candidate is None:
                    install_deadline = (
                        monotonic() + owner.policy.planning_attempt_seconds
                    )
                    prepared_wire = (
                        self._provider_dispatch.bind_prepared_executable_wire_input(
                            owner_dispatch=dry_dispatch,
                            decision=selected_wire_decision,
                            deadline=install_deadline,
                        )
                    )
                    await self._provider_dispatch.install_provider_open(
                        dispatch=dry_dispatch,
                        prepared_wire=prepared_wire,
                        turn_id=turn_id,
                        model_call_index=model_call_index,
                        deadline=install_deadline,
                    )
                    successor_dispatch = dry_dispatch
                    dry_dispatch = None
                    owner.advance_phase(
                        scope_kind=expected_scope.scope_kind,
                        scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                        phase=CompactionAttemptPhase.ACTIVE_EPOCH_INSTALLED,
                    )
                else:
                    if rebased_active is None:
                        raise RuntimeError(
                            "pending active compaction lost its rebased candidate"
                        )
                    prospective_active_input = PreparedProspectiveActiveRootInput(
                        admission=PreparedActiveRootInputAdmission(
                            candidate=rebased_active,
                            canonical_read=dry_dispatch.canonical_read,
                            semantic_input=dry_dispatch.append_result.compiled_input,
                            wire_quote=selected_wire_decision.quote,
                        ),
                        _dispatch=dry_dispatch,
                        wire_decision=selected_wire_decision,
                    )
                    dry_dispatch = None
            else:
                if prospective_root_dispatch is None:
                    if not empty_adoption_resealed:
                        retiring_dispatch = dry_dispatch
                        dry_dispatch = None
                        no_continuation_retirement_started = True
                        self._retire_full_adoption_without_continuation(
                            scope=continuity_scope,
                            turn_id=turn_id,
                            attempt_token=attempt_token,
                            confirmation=confirmation,
                            candidate=candidate,
                            evidence=_issue_idle_target_no_continuation_evidence(
                                target_branch
                            ),
                            dispatch=retiring_dispatch,
                        )
                owner.advance_phase(
                    scope_kind=expected_scope.scope_kind,
                    scope_subagent_task_id=(expected_scope.scope_subagent_task_id),
                    phase=CompactionAttemptPhase.IDLE_BASE_ADOPTED,
                )
            owner.reset_automatic_failures(
                scope_kind=expected_scope.scope_kind,
                scope_subagent_task_id=expected_scope.scope_subagent_task_id,
            )
            result = CompactionExecutionResult(
                CompactionOutcome(
                    CompactionDisposition.COMPACTED,
                    turn_id,
                    candidate.snapshot.snapshot_id,
                    confirmation.revision_ordinal,
                    "COMPACTED",
                ),
                successor_dispatch=(
                    successor_dispatch
                    if target_branch is CompactionTargetBranch.ACTIVE_INSTALLATION
                    else None
                ),
                prospective_root_dispatch=(
                    prospective_root_dispatch
                    if target_branch is CompactionTargetBranch.IDLE_BASE_ONLY
                    else None
                ),
                prospective_active_input=prospective_active_input,
                model_switch_tier=model_switch_tier,
            )
            prospective_root_dispatch = None
            prospective_active_input = None
            return result
        except BaseException as error:
            if adopted_outcome is None:
                raise
            if (
                not empty_adoption_resealed
                and successor_dispatch is None
                and not no_continuation_retirement_started
            ):
                retiring_dispatch = dry_dispatch
                dry_dispatch = None
                if prospective_root_dispatch is not None:
                    prospective_root_dispatch.close()
                    prospective_root_dispatch = None
                if prospective_active_input is not None:
                    prospective_active_input.close()
                    prospective_active_input = None
                no_continuation_retirement_started = True
                self._retire_full_adoption_without_continuation(
                    scope=continuity_scope,
                    turn_id=turn_id,
                    attempt_token=attempt_token,
                    confirmation=confirmation,
                    candidate=candidate,
                    evidence=_issue_successor_final_abandonment_evidence(error),
                    dispatch=retiring_dispatch,
                )
            raise _PostAdoptionCompactionFailure(adopted_outcome, error) from error
        finally:
            if dry_dispatch is not None:
                dry_dispatch.close()
            if prospective_root_dispatch is not None:
                prospective_root_dispatch.close()
            if prospective_active_input is not None:
                prospective_active_input.close()

    async def compact_idle_turn(
        self,
        *,
        turn_id: str,
        command_id: str,
        force: bool,
        scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
        scope_subagent_task_id: str | None = None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None = None,
    ) -> CompactionOutcome:
        """Compact the latest terminal exact-scope turn without a runner epoch."""

        owner = self._compaction_owner
        if owner is None or not owner.policy.enabled or not owner.policy.manual_enabled:
            return CompactionOutcome(
                CompactionDisposition.FAILED,
                turn_id,
                None,
                None,
                "COMPACTION_DISABLED",
            )
        scope = CompactionScope(
            session_id=self._writer_lease.guard.session_id,
            workspace_id=await self._resolved_workspace_id(),
            turn_id=turn_id,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
        )
        attempt_token = _new_compaction_attempt_token(
            session_id=scope.session_id,
            turn_id=turn_id,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            trigger=CompactionTrigger.MANUAL,
        )

        async def operation() -> CompactionExecutionResult:
            return await self._execute_compaction_fenced(
                turn_id=turn_id,
                model_call_index=1,
                inherited_memory_use_policy=MemoryUsePolicy.ENABLED,
                trigger=CompactionTrigger.MANUAL,
                force=force,
                expected_scope=scope,
                post_adoption_branch=CompactionTargetBranch.IDLE_BASE_ONLY,
                stable_command_id=command_id,
                hook_scope=(
                    self._hook_root_scope
                    if scope_kind is ModelInputScopeKind.ROOT
                    else None
                ),
                session_start_boundary_port=session_start_boundary_port,
                attempt_token=attempt_token,
            )

        try:
            execution = await owner.run_fenced(
                scope=scope,
                trigger=CompactionTrigger.MANUAL,
                operation=operation,
                attempt_id=attempt_token.attempt_id,
            )
            if execution.successor_dispatch is not None:
                execution.successor_dispatch.close()
                raise RuntimeError("idle compaction produced an active successor")
            return execution.outcome
        except _PostAdoptionCompactionFailure as failure:
            return failure.outcome
        except asyncio.CancelledError:
            raise
        except StaleHostWriter:
            raise
        except BaseException:
            return CompactionOutcome(
                CompactionDisposition.FAILED,
                turn_id,
                None,
                None,
                "COMPACTION_FAILED",
            )

    async def _settle_compaction_adoption(
        self,
        *,
        candidate,
        preconditions: CompactionCanonicalWritePreconditions,
    ):
        delay = 0.05
        while True:
            try:
                confirmation = await self._io.run(
                    self._repository.confirm_context_snapshot_adoption,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                raise
            except BaseException:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)
                continue
            if confirmation.kind is CompactionConfirmationKind.FULL:
                return confirmation
            if confirmation.kind is CompactionConfirmationKind.CONFLICT:
                return confirmation
            try:
                written = await self._io.run(
                    self._repository.adopt_context_snapshot,
                    self._writer_lease.guard,
                    candidate=candidate,
                    preconditions=preconditions,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except StaleHostWriter:
                raise
            except BaseException:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)
                continue
            if written.kind is CompactionConfirmationKind.FULL:
                return written
            if written.kind is CompactionConfirmationKind.CONFLICT:
                return written
            return written

    def _retire_full_adoption_without_continuation(
        self,
        *,
        scope: ProviderInputContinuityScope,
        turn_id: str,
        attempt_token: CompactionAttemptToken,
        confirmation: object,
        candidate: PreparedCompactionCanonicalAdoption,
        evidence: NoContinuationProductEvidence,
        dispatch: PreparedCompactionDryProjection | PreparedProviderDispatch | None,
    ) -> None:
        """Fence retries, release the exact preparation, then publish Empty."""

        predecessor = self._continuity.current_cohort(scope)
        if predecessor is None:
            raise RuntimeError(
                "installed no-continuation settlement lacks its predecessor"
            )
        fence = self._continuity.install_no_continuation_admission_fence(
            scope=scope,
            predecessor=predecessor,
            turn_id=turn_id,
            attempt_token=attempt_token,
            confirmation=confirmation,
            adopted_snapshot_id=candidate.snapshot.snapshot_id,
            adopted_binding_revision_id=candidate.binding.binding_revision_id,
        )
        self._safe_point.retire_full_adoption_without_continuation_and_arm_empty(
            continuity=self._continuity,
            fence=fence,
            evidence=evidence,
            dispatch=dispatch,
        )


__all__ = [
    "CompactionCoordinator",
    "CompactionExecutionResult",
    "CompactionWireTransitionDrift",
    "ValidatedCompactionWireTransition",
    "validate_compaction_wire_transition",
]
