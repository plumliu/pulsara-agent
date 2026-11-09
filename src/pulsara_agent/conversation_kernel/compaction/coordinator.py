"""Round 5B compaction execution and canonical adoption coordinator."""

from __future__ import annotations

from pulsara_agent.conversation_kernel.compaction.admission import (
    CompactionAdmissionOperations,
    _target_is_known_text_only as _target_is_known_text_only,
)
from pulsara_agent.conversation_kernel.compaction.execution_context import (
    CompactionContextOperations,
)
from pulsara_agent.conversation_kernel.compaction.execution_contracts import (
    AutomaticCompactionTriggerCandidate as AutomaticCompactionTriggerCandidate,
    CompactionAttemptToken as CompactionAttemptToken,
    CompactionExecutionResult as CompactionExecutionResult,
    ModelSwitchCompactionTriggerCandidate as ModelSwitchCompactionTriggerCandidate,
    ModelSwitchDirectPrecompileDecision as ModelSwitchDirectPrecompileDecision,
    OrdinaryPrecompileDecision as OrdinaryPrecompileDecision,
    PrecompileCompactionDecision as PrecompileCompactionDecision,
    PreparedCompactSessionStart as PreparedCompactSessionStart,
    PreparedCompactSessionStartFacts as PreparedCompactSessionStartFacts,
    SessionStartCompactBoundaryPort as SessionStartCompactBoundaryPort,
    SessionStartCompactPort as SessionStartCompactPort,
    _COMPACTION_ATTEMPT_TOKEN_SEAL as _COMPACTION_ATTEMPT_TOKEN_SEAL,
    _CompactionFencedRestart as _CompactionFencedRestart,
    _PendingToolBatchAdoption as _PendingToolBatchAdoption,
    _PendingToolBatchSource as _PendingToolBatchSource,
    _PostAdoptionCompactionFailure as _PostAdoptionCompactionFailure,
    _SummaryWinner as _SummaryWinner,
    _already_compact_result as _already_compact_result,
    _new_compaction_attempt_token as _new_compaction_attempt_token,
)

from pulsara_agent.identity import derive_id


from pulsara_agent.conversation_kernel.interruption import summary_interruption_suffix

from pulsara_agent.conversation_kernel.workspace import WorkspaceExecutionGate

import asyncio

from dataclasses import dataclass, field as dataclass_field, replace

from datetime import datetime, timezone
from typing import Callable, Literal


from time import monotonic


from pulsara_agent.conversation_kernel.assembler import (
    MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES,
)


from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
)


from pulsara_agent.conversation_kernel.compaction.contracts import (
    COMPACTION_MODEL_CONTRACT,
    COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
    COMPACTION_SUMMARY_PROMPT_CONTRACT,
    CONTEXT_SNAPSHOT_CODEC,
    CONTEXT_SNAPSHOT_MEDIA_TYPE,
    CompactionCanonicalAdoptionFactoryInput,
    CompactionCanonicalWritePreconditions,
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
    FrozenCompactionSourceView,
    PreparedCompactionCanonicalAdoption,
    manual_compaction_snapshot_id,
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
)

from pulsara_agent.conversation_kernel.compaction.planner import (
    build_synthetic_compaction_dispatch_read,
    CompactionPlanningError,
    CompactionReclaimUnavailable,
    NoSafeCompactionSummaryPrefix,
    enumerate_destination_backbone_projections,
    enumerate_complete_tool_groups,
    enumerate_safe_summary_prefixes,
    freeze_compaction_continuation,
    freeze_compaction_source_view,
    freeze_destination_dialogue_projection_plan,
    minimum_pending_image_tool_groups,
    project_destination_dialogue_plan_for_text_only_handover,
    retain_destination_tool_evidence,
    select_recent_human_messages,
    select_retained_prefix_entry_ids,
    pending_image_evidence_start_sequence,
    unconsumed_request_start_sequence,
    should_trigger_compaction,
)

from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    compaction_summary_request,
    freeze_compaction_summary_output,
)

from pulsara_agent.conversation_kernel.compaction.runtime import (
    CompactionWriteReservation,
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    NoContinuationProductEvidence,
    _issue_durable_turn_not_running_evidence,
    _issue_idle_target_no_continuation_evidence,
    _issue_post_compact_blocked_evidence,
    _issue_session_start_compact_blocked_evidence,
    _issue_successor_final_abandonment_evidence,
)


from pulsara_agent.conversation_kernel.cold_epoch import (
    AdoptedCompactionContinuationSeed,
    CanonicalColdContinuationSeed,
    CompactionDryProjectionSeed,
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
    PreparedProspectiveActiveRootInput,
    PreparedProviderWireCandidate,
    PreparedWireMeasurementDecision,
    ProviderDispatchCoordinator,
)


from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderInputReader,
)
from pulsara_agent.conversation_kernel.steer import (
    PreparedActiveRootInputAdmission,
    PreparedActiveRootInputCandidate,
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
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    ModelInputCompileFailureKind,
    PreparedProviderInputCut,
    FrozenCompiledModelInput,
    StructuredModelInputCompileError,
)
from pulsara_agent.llm.request import FrozenProviderWireInputPlan
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
)

from pulsara_agent.model_input.continuity import (
    ProviderInputContinuityScope,
)
from pulsara_agent.llm.request import (
    MAXIMUM_PROVIDER_WIRE_INPUT_BYTES,
    FrozenProviderWireInputQuote,
)
from pulsara_agent.llm.errors import ModelTargetCapabilityMismatch
from pulsara_agent.llm.input import content_has_image
from pulsara_agent.ports.provider_stream import (
    ProviderModelExecutionFailed,
    ProviderModelOutputIncomplete,
)
from pulsara_agent.conversation_kernel.direct_model import (
    KernelModelTargetPreparationRequest,
    PreparedKernelModelCall,
    PreparedKernelModelTarget,
)
from pulsara_agent.llm.frozen_target import (
    FrozenEpochModelCallTarget,
)

from pulsara_agent.hooks.contracts import (
    HookDispatchScopeRef,
)
from pulsara_agent.hooks.dispatcher import KernelHookDispatcher


@dataclass(frozen=True, slots=True)
class ValidatedCompactionWireTransition:
    phase: Literal["PRE_FULL", "POST_FULL"]
    source_quote: FrozenProviderWireInputQuote
    successor_quote: FrozenProviderWireInputQuote
    reclaim_tokens: int


class CompactionWireTransitionDrift(CompactionPlanningError):
    """The source and successor no longer share one exact planning authority."""


class CompactionSuccessorHardAdmissionError(CompactionPlanningError):
    """A measured successor failed a concrete model or wire hard boundary."""


def validate_compaction_wire_transition(
    *,
    source_view: FrozenCompactionSourceView,
    source_candidate: PreparedProviderWireCandidate,
    successor_wire: PreparedWireMeasurementDecision,
    phase: Literal["PRE_FULL", "POST_FULL"],
    destination_target: PreparedKernelModelTarget | None = None,
    failed_wire_quote: FrozenProviderWireInputQuote | None = None,
    require_token_reclaim: bool = True,
) -> ValidatedCompactionWireTransition:
    """Join one canonical rewrite, admit its full wire, then check its benefit.

    Runtime observations added after adoption do not repeat PRE_FULL's ordinary
    gain test. A 413 retry retains its original byte baseline through final send.
    """

    source_quote = source_view.provider_wire_quote
    successor_candidate = successor_wire.candidate
    successor_quote = successor_wire.quote
    source_prepared_call = getattr(source_candidate, "prepared_call", None)
    successor_prepared_call = getattr(successor_candidate, "prepared_call", None)
    if source_prepared_call is None or successor_prepared_call is None:
        raise CompactionWireTransitionDrift(
            "compaction wire transition lacks an installable source or successor"
        )
    source_call = source_prepared_call.call
    successor_call = successor_prepared_call.call
    source_binding = source_prepared_call.compile_binding
    successor_binding = successor_prepared_call.compile_binding
    source_semantic = source_candidate.semantic_input
    source_identity = source_semantic.canonical_input_identity
    successor_identity = successor_candidate.semantic_input.canonical_input_identity
    same_target = (
        source_call.target.fact == successor_call.target.fact
        and source_call.binding == successor_call.binding
        and source_call.target.model_profile.route_wire_profile
        == successor_call.target.model_profile.route_wire_profile
    )
    same_budget = (
        source_quote.effective_input_budget_tokens
        == successor_quote.effective_input_budget_tokens
    )
    if (
        phase not in {"PRE_FULL", "POST_FULL"}
        or source_candidate.canonical_read != source_view.canonical_dispatch_read
        or source_binding != source_view.normal_compile_binding
        or source_identity
        != source_candidate.canonical_read.compile_snapshot.canonical_input.identity
        or successor_identity
        != successor_candidate.canonical_read.compile_snapshot.canonical_input.identity
        or source_semantic.compile_binding_fingerprint
        != source_binding.binding_fingerprint
        or source_semantic.system_prompt != source_view.materialized_system_prompt()
        or source_semantic.messages != source_view.materialized_messages()
        or source_semantic.tools != source_binding.tool_surface.tool_specs
        or source_binding.target_fact != source_call.target.fact
        or successor_binding.target_fact != successor_call.target.fact
        or source_quote.wire_api
        != source_call.target.model_profile.route_wire_profile.wire_api
        or successor_quote.wire_api
        != successor_call.target.model_profile.route_wire_profile.wire_api
        or source_quote.estimator_fingerprint != source_binding.estimator_fingerprint
        or successor_quote.estimator_fingerprint
        != successor_binding.estimator_fingerprint
        or source_quote.effective_input_budget_tokens
        != source_binding.effective_input_budget_tokens
        or successor_quote.effective_input_budget_tokens
        != successor_binding.effective_input_budget_tokens
        or (destination_target is None and (not same_target or not same_budget))
        or (
            destination_target is not None
            and (
                successor_call.target is not destination_target.target
                or successor_call.binding != destination_target.call.binding
                or successor_binding.estimator.fact
                != destination_target.target.token_estimator.fact
            )
        )
        or (
            same_target
            and same_budget
            and (
                type(source_binding.estimator) is not type(successor_binding.estimator)
                or source_binding.estimator.fact != successor_binding.estimator.fact
                or source_quote.estimator_fingerprint
                != successor_quote.estimator_fingerprint
            )
        )
        or (
            failed_wire_quote is not None
            and (
                not same_target
                or not same_budget
                or failed_wire_quote.wire_api != source_quote.wire_api
                or failed_wire_quote.estimator_fingerprint
                != source_quote.estimator_fingerprint
                or failed_wire_quote.effective_input_budget_tokens
                != source_quote.effective_input_budget_tokens
            )
        )
        or not _compaction_cut_lineage_exactly_joins(
            source_candidate=source_candidate,
            successor_candidate=successor_candidate,
            phase=phase,
        )
    ):
        raise CompactionWireTransitionDrift(
            "compaction wire transition does not exact-join its target and cuts"
        )
    if (
        successor_quote.budget_input_tokens
        > successor_quote.effective_input_budget_tokens
    ):
        raise CompactionSuccessorHardAdmissionError(
            "compaction successor exceeds its hard model budget"
        )
    if successor_quote.final_wire_utf8_bytes > MAXIMUM_PROVIDER_WIRE_INPUT_BYTES:
        raise CompactionSuccessorHardAdmissionError(
            "compaction successor exceeds its hard physical byte bound"
        )
    if successor_wire.wire_input_plan is None:
        raise CompactionSuccessorHardAdmissionError(
            "compaction successor lacks executable final-wire admission"
        )
    reclaim = 0
    if failed_wire_quote is not None:
        if (
            successor_quote.final_wire_utf8_bytes
            >= failed_wire_quote.final_wire_utf8_bytes
        ):
            raise CompactionReclaimUnavailable(
                "compaction successor does not shrink failed request final-wire bytes"
            )
    elif phase == "PRE_FULL" and same_target and same_budget and require_token_reclaim:
        if source_quote.final_wire_utf8_bytes > MAXIMUM_PROVIDER_WIRE_INPUT_BYTES:
            if (
                successor_quote.final_wire_utf8_bytes
                >= source_quote.final_wire_utf8_bytes
            ):
                raise CompactionReclaimUnavailable(
                    "compaction successor does not shrink hard-boundary final-wire bytes"
                )
        else:
            reclaim = (
                source_quote.raw_final_wire_estimated_input_tokens
                - successor_quote.raw_final_wire_estimated_input_tokens
            )
            if reclaim <= 0:
                raise CompactionReclaimUnavailable(
                    "compaction successor does not reduce estimated input tokens"
                )
    return ValidatedCompactionWireTransition(
        phase=phase,
        source_quote=source_quote,
        successor_quote=successor_quote,
        reclaim_tokens=reclaim,
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
        or (
            not source_lineage_floor
            <= boundary
            <= source_identity.provider_input_through_sequence
        )
        or (successor_identity.session_id != source_identity.session_id)
        or (successor_identity.turn_id != source_identity.turn_id)
        or (successor_identity.initial_entry_id != source_identity.initial_entry_id)
        or (
            successor_identity.conversation_scope_kind
            is not source_identity.conversation_scope_kind
        )
        or (
            successor_identity.context_binding_revision_id
            != successor_binding.binding_revision_id
        )
        or (
            successor_identity.provider_input_through_sequence
            < source_identity.provider_input_through_sequence
        )
        or (
            phase == "PRE_FULL"
            and successor_identity.provider_input_through_sequence
            != source_identity.provider_input_through_sequence
        )
    ):
        return False
    if not successor_input.items:
        return False
    snapshot, *successor_items = successor_input.items
    snapshot_carrier = snapshot.content
    if (
        snapshot.item_kind is not FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
        or not isinstance(snapshot_carrier, CompactionSnapshotCarrier)
        or snapshot.source_entry_id is not None
        or snapshot.source_entry_sequence != boundary
    ):
        return False
    references = snapshot_carrier.retained_entry_ids
    reference_ids = set(references)
    prefix = tuple(
        item
        for item in source_input.items
        if item.item_kind is not FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
        and item.source_entry_id in reference_ids
        and item.source_entry_sequence is not None
        and item.source_entry_sequence <= boundary
    )
    if tuple(item.source_entry_id for item in prefix) != references:
        return False
    from pulsara_agent.model_input.contracts import validate_compaction_request_shape

    try:
        for item in prefix:
            validate_compaction_request_shape(
                item_kind=item.item_kind,
                input_origin=item.input_origin,
                active=False,
                has_image=content_has_image(item.content),
            )
    except (TypeError, ValueError):
        return False
    suffix = tuple(
        item
        for item in source_input.items
        if item.item_kind is not FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
        and item.source_entry_sequence is not None
        and item.source_entry_sequence > boundary
    )
    expected_items = prefix + suffix
    if tuple(successor_items[: len(expected_items)]) != expected_items:
        return False
    added_items = tuple(successor_items[len(expected_items) :])
    source_ceiling = source_identity.provider_input_through_sequence
    if added_items and (
        phase != "POST_FULL"
        or any(
            item.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
            or item.source_entry_sequence is None
            or item.source_entry_sequence <= source_ceiling
            for item in added_items
        )
    ):
        return False
    if (
        snapshot_carrier.continuation_mode
        is CompactionContinuationMode.RESUME_ACTIVE_TURN
        and (
            snapshot_carrier.active_request_entry_id != source_identity.initial_entry_id
            or sum(
                item.source_entry_id == snapshot_carrier.active_request_entry_id
                for item in successor_items
            )
            != 1
        )
    ):
        return False
    if successor_input.canonical_expanded_bytes != (
        snapshot_carrier.canonical_expanded_bytes
        + sum(
            provider_input_item_canonical_expanded_bytes(item)
            for item in successor_items
        )
    ):
        return False
    retained_request_ids = frozenset(
        item.source_entry_id
        for item in expected_items
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
    retained_ids = frozenset(
        item.source_entry_id
        for item in expected_items
        if item.source_entry_id is not None
    )
    expected_manifests = tuple(
        item
        for item in source_read.replay_manifest_cut.manifests
        if item.assistant_entry_id in retained_ids
    )
    return (
        tuple(
            item
            for item in successor_input.closures
            if item.target_provider_input_through_sequence <= source_ceiling
        )
        == expected_closures
        and tuple(
            item
            for item in successor_input.late_outcomes
            if item.result_entry_sequence <= source_ceiling
        )
        == expected_late_outcomes
        and tuple(
            item
            for item in successor_read.replay_manifest_cut.manifests
            if item.assistant_entry_id in retained_ids
        )
        == expected_manifests
        and (
            phase == "POST_FULL"
            or (
                successor_input.closures == expected_closures
                and successor_input.late_outcomes == expected_late_outcomes
                and successor_read.replay_manifest_cut.manifests == expected_manifests
            )
        )
        and successor_facts.run_permission_snapshot
        == source_facts.run_permission_snapshot
        and successor_facts.plan_workflow_fact == source_facts.plan_workflow_fact
        and successor_facts.plan_handoff_fact == source_facts.plan_handoff_fact
        and successor_facts.previous_turn_outcome_fact
        == source_facts.previous_turn_outcome_fact
    )


def _can_retry_compaction_with_smaller_tail(error: BaseException) -> bool:
    """Only a successor's reducible hard admission can expand summary scope."""

    if isinstance(error, CompactionWireTransitionDrift):
        return False
    if isinstance(error, CompactionSuccessorHardAdmissionError):
        return True
    if isinstance(error, CompactionPlanningError):
        return False
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


def _summary_source_lifecycle_suffix(
    *,
    semantic: PreparedCompactionSummarySemantic,
    canonical_read: FrozenCompactionCanonicalRead,
) -> str:
    """Quote exact lifecycle facts belonging to this full or projected source."""
    if (
        canonical_read.source_interruption is None
        and not canonical_read.imported_interruptions
        and not canonical_read.executed_history_interruptions
    ):
        return ""
    proof = semantic.source_proof
    canonical = (
        proof.source_view.canonical_dispatch_read.compile_snapshot.canonical_input
    )
    sequences_by_entry = {
        item.source_entry_id: item.source_entry_sequence
        for item in canonical.items
        if item.source_entry_id is not None
    }
    indices = (
        proof.source_message_indices
        if proof.projection is None
        else proof.reference_message_indices
    )
    selected_sequences = set()
    for index in indices:
        placement = proof.source_projection.message_placements[index]
        if placement.origin_entry_id is not None:
            selected_sequences.add(sequences_by_entry.get(placement.origin_entry_id))
        attachment = placement.tool_attachment_source
        if attachment is not None:
            selected_sequences.update(
                member.source.source_entry_sequence for member in attachment.members
            )

    def included(notice):
        sequence = notice.display_after_entry_sequence
        if sequence > proof.read_through_sequence:
            return False
        # P(H) may omit old body while carrying its exact canonical lifecycle
        # metadata. Raw R facts still follow actual referenced message rows.
        return sequence in selected_sequences or (
            proof.projection is not None
            and sequence <= proof.prefix_proof.source_through_sequence
        )

    return summary_interruption_suffix(
        canonical_read.source_interruption,
        tuple(n for n in canonical_read.imported_interruptions if included(n)),
        tuple(n for n in canonical_read.executed_history_interruptions if included(n)),
    )


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
        or (
            identity.provider_input_through_sequence
            != source.provider_input_through_sequence
        )
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


async def _join_compaction_settlement(
    task: asyncio.Task[CompactionExecutionResult],
) -> CompactionExecutionResult:
    """Drain admitted adoption and retire any returned continuation on stop."""
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        except BaseException:
            break
    try:
        result = task.result()
    except BaseException:
        if cancellation is not None:
            raise cancellation
        raise
    if cancellation is not None:
        # The worker may have returned the only physical owner after
        # FULL. Drain that result before surfacing any repeated stop.
        if result.successor_dispatch is not None:
            result.successor_dispatch.close(error=cancellation)
        if result.prospective_active_input is not None:
            result.prospective_active_input.close(error=cancellation)
        raise cancellation
    return result


@dataclass(slots=True)
class PendingToolBatchCompaction:
    """One disposable summary/dry-prefix owner, never an installed request.

    Tools still settle against their original epoch.  Only finalize(), after
    the exact batch is provider-safe, can adopt the prepared prefix and bind
    its complete real suffix.  Closing before that point changes no canonical
    history and never leaves an executable synthetic tool closure behind.
    """

    prepared_call: PreparedKernelModelCall = dataclass_field(repr=False)
    compiled_input: FrozenCompiledModelInput = dataclass_field(repr=False)
    wire_input_plan: FrozenProviderWireInputPlan = dataclass_field(repr=False)
    _coordinator: CompactionCoordinator = dataclass_field(repr=False)
    _adoption: _PendingToolBatchAdoption | None = dataclass_field(repr=False)

    def close(self) -> None:
        adoption, self._adoption = self._adoption, None
        if adoption is not None:
            adoption.dry_dispatch.close()

    async def finalize(self) -> CompactionExecutionResult:
        adoption, self._adoption = self._adoption, None
        if adoption is None:
            raise RuntimeError("pending tool-batch compaction is already consumed")
        return await self._coordinator._finalize_pending_tool_batch(adoption)


class CompactionCoordinator(CompactionAdmissionOperations, CompactionContextOperations):
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
        workspace_gate: WorkspaceExecutionGate | None = None,
        session_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
        presentation_notice_sink: Callable[[str], None] | None = None,
    ) -> None:
        self._presentation_notice_sink = presentation_notice_sink
        self._workspace_gate = workspace_gate
        self._session_scope_kind = session_kind
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

    async def _resolved_workspace_id(self, *, deadline: float | None = None) -> str:
        return await self._workspace_resolver.resolve(
            deadline=self._canonical_deadline() if deadline is None else deadline
        )

    async def execute_active(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        trigger: CompactionTrigger,
        force: bool,
        scope_kind: ModelInputScopeKind,
        hook_scope: HookDispatchScopeRef | None = None,
        session_start_compact_port: SessionStartCompactPort | None = None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None = None,
        first_call_history_through_sequence: int | None = None,
        failed_wire_quote: FrozenProviderWireInputQuote | None = None,
    ) -> CompactionExecutionResult:
        return await self._execute_active(
            failed_wire_quote=failed_wire_quote,
            turn_id=turn_id,
            model_call_index=model_call_index,
            inherited_memory_use_policy=inherited_memory_use_policy,
            trigger=trigger,
            force=force,
            scope_kind=scope_kind,
            hook_scope=hook_scope,
            session_start_compact_port=session_start_compact_port,
            session_start_boundary_port=session_start_boundary_port,
            pending_active_candidate=None,
            first_call_history_through_sequence=first_call_history_through_sequence,
        )

    async def prepare_pending_tool_batch(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        closed_source_cut: PreparedProviderInputCut,
        assistant_entry_id: str,
        ordered_tool_call_ids: tuple[str, ...],
        hook_scope: HookDispatchScopeRef | None = None,
        session_start_compact_port: SessionStartCompactPort | None = None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None = None,
    ) -> PendingToolBatchCompaction | None:
        """Summarize a closed prefix without adopting across unfinished tools."""

        owner = self._compaction_owner
        if (
            owner is None
            or not owner.policy.enabled
            or not owner.policy.automatic_enabled
            or not owner.automatic_allowed(scope_kind=self._session_scope_kind)
        ):
            return None
        if (
            closed_source_cut.session_id != self._writer_lease.guard.session_id
            or closed_source_cut.turn_id != turn_id
            or not assistant_entry_id
            or not ordered_tool_call_ids
            or len(set(ordered_tool_call_ids)) != len(ordered_tool_call_ids)
        ):
            raise ValueError("pending tool-batch compaction source is invalid")
        scope = CompactionScope(
            session_id=closed_source_cut.session_id,
            workspace_id=await self._resolved_workspace_id(),
            turn_id=turn_id,
            scope_kind=self._session_scope_kind,
        )
        token = _new_compaction_attempt_token(
            session_id=scope.session_id,
            turn_id=turn_id,
            scope_kind=scope.scope_kind,
            trigger=CompactionTrigger.MID_TURN_FOLLOWUP,
        )

        prepared_pending: PendingToolBatchCompaction | None = None

        async def operation():
            nonlocal prepared_pending
            result = await self._execute_compaction_fenced(
                turn_id=turn_id,
                model_call_index=model_call_index,
                inherited_memory_use_policy=inherited_memory_use_policy,
                trigger=CompactionTrigger.MID_TURN_FOLLOWUP,
                force=True,
                expected_scope=scope,
                post_adoption_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
                stable_command_id=None,
                attempt_token=token,
                hook_scope=hook_scope
                if hook_scope is not None
                else self._hook_root_scope,
                session_start_compact_port=session_start_compact_port,
                session_start_boundary_port=session_start_boundary_port,
                pending_tool_batch=_PendingToolBatchSource(
                    closed_source_cut,
                    assistant_entry_id,
                    ordered_tool_call_ids,
                ),
            )
            if isinstance(result, PendingToolBatchCompaction):
                prepared_pending = result
            return result

        try:
            result = await owner.run_fenced(
                scope=scope,
                trigger=token.trigger,
                operation=operation,
                attempt_id=token.attempt_id,
            )
        except BaseException as error:
            if prepared_pending is not None:
                prepared_pending.close()
            if isinstance(error, Exception) and not isinstance(error, StaleHostWriter):
                owner.record_automatic_failure(scope_kind=scope.scope_kind)
            raise
        if (
            isinstance(result, CompactionExecutionResult)
            and result.outcome.disposition is CompactionDisposition.FAILED
            and not (result.outcome.public_code or "").startswith(
                "HOOK_PRE_COMPACT_BLOCKED"
            )
        ):
            owner.record_automatic_failure(scope_kind=scope.scope_kind)
        return result if isinstance(result, PendingToolBatchCompaction) else None

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

        if not _can_retry_compaction_with_smaller_tail(failure):
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
            scope_kind=self._session_scope_kind,
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
        hook_scope: HookDispatchScopeRef | None = None,
        session_start_compact_port: SessionStartCompactPort | None = None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None = None,
        first_call_history_through_sequence: int | None = None,
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
        )
        token = _new_compaction_attempt_token(
            session_id=scope.session_id,
            turn_id=turn_id,
            scope_kind=scope_kind,
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
                    hook_scope if hook_scope is not None else self._hook_root_scope
                ),
                session_start_compact_port=session_start_compact_port,
                session_start_boundary_port=session_start_boundary_port,
                model_switch_candidate=candidate,
                first_call_history_through_sequence=first_call_history_through_sequence,
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

    async def _execute_active(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        trigger: CompactionTrigger,
        force: bool,
        scope_kind: ModelInputScopeKind,
        hook_scope: HookDispatchScopeRef | None,
        session_start_compact_port: SessionStartCompactPort | None,
        session_start_boundary_port: SessionStartCompactBoundaryPort | None,
        pending_active_candidate: PreparedActiveRootInputCandidate | None,
        admitted_writer: CompactionWriteReservation | None = None,
        first_call_history_through_sequence: int | None = None,
        failed_wire_quote: FrozenProviderWireInputQuote | None = None,
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
        if not self.automatic_allowed(scope_kind=scope_kind):
            return CompactionExecutionResult(
                CompactionOutcome(
                    CompactionDisposition.NOT_NEEDED,
                    turn_id,
                    None,
                    None,
                    "AUTOMATIC_COMPACTION_UNAVAILABLE",
                )
            )
        attempt_token = _new_compaction_attempt_token(
            session_id=self._writer_lease.guard.session_id,
            turn_id=turn_id,
            scope_kind=scope_kind,
            trigger=trigger,
        )
        try:
            provisional_scope = CompactionScope(
                session_id=self._writer_lease.guard.session_id,
                workspace_id=await self._resolved_workspace_id(),
                turn_id=turn_id,
                scope_kind=scope_kind,
            )
        except BaseException:
            raise

        async def operation() -> CompactionExecutionResult:
            return await self._execute_compaction_fenced(
                failed_wire_quote=failed_wire_quote,
                turn_id=turn_id,
                model_call_index=model_call_index,
                inherited_memory_use_policy=inherited_memory_use_policy,
                trigger=trigger,
                force=force,
                expected_scope=provisional_scope,
                post_adoption_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
                stable_command_id=None,
                attempt_token=attempt_token,
                pre_compact_dispatched=False,
                hook_scope=(
                    hook_scope if hook_scope is not None else self._hook_root_scope
                ),
                session_start_compact_port=session_start_compact_port,
                session_start_boundary_port=session_start_boundary_port,
                pending_active_candidate=pending_active_candidate,
                first_call_history_through_sequence=first_call_history_through_sequence,
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
            raise failure.error.with_traceback(failure.error.__traceback__)
        except (asyncio.CancelledError, StaleHostWriter):
            raise
        except Exception:
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
        if execution.outcome.disposition is CompactionDisposition.FAILED and not (
            execution.outcome.public_code or ""
        ).startswith("HOOK_PRE_COMPACT_BLOCKED"):
            owner.record_automatic_failure(scope_kind=scope_kind)
        return execution

    async def _select_summary(
        self,
        *,
        dispatch: PreparedCompactionSourceDispatch,
        source_view: FrozenCompactionSourceView,
        compaction_read: FrozenCompactionCanonicalRead,
        policy,
        maximum_retained_tool_groups: int | None,
        target_branch: CompactionTargetBranch,
        summary_request: str,
        deadline: float,
        first_call_history_through_sequence: int | None = None,
    ) -> _SummaryWinner | None:
        """Freeze R once, admit full Q first, then only local-hard P(H)+raw R."""
        canonical = compaction_read.dispatch_read.compile_snapshot.canonical_input
        groups = enumerate_complete_tool_groups(compaction_read)
        mandatory = minimum_pending_image_tool_groups(
            source_view=source_view,
            complete_tool_groups=groups,
        )
        read_through = source_view.exact_safe_canonical_head
        if first_call_history_through_sequence is not None:
            read_through = min(read_through, first_call_history_through_sequence)
        pending_image = pending_image_evidence_start_sequence(
            canonical,
            consumed_through_sequence=source_view.latest_assistant_input_through_sequence,
        )
        if pending_image is not None:
            read_through = min(read_through, pending_image - 1)
        if target_branch is CompactionTargetBranch.ACTIVE_INSTALLATION:
            new_request_start = unconsumed_request_start_sequence(
                canonical,
                consumed_through_sequence=source_view.latest_assistant_input_through_sequence,
            )
            if new_request_start is not None:
                read_through = min(read_through, new_request_start - 1)
        mandatory = max(
            mandatory,
            sum(group.assistant_entry_sequence > read_through for group in groups),
        )
        # Mandatory current batches are N, in addition to the optional historical R.
        optional = min(
            policy.maximum_retained_tool_groups, max(0, len(groups) - mandatory)
        )
        maximum = optional + mandatory
        if maximum_retained_tool_groups is not None:
            maximum = min(maximum, max(mandatory, maximum_retained_tool_groups))
        call = self._model.resolve_compaction_summary_call(
            active_prepared_call=dispatch.prepared_call
        )
        modalities = dispatch.prepared_call.call.target.fact.input_modalities
        text_only = modalities is not None and "image" not in modalities

        async def measure(prefix, retained_ids, projection=None):
            if monotonic() >= deadline:
                raise TimeoutError("compaction planning deadline expired")
            arguments = dict(
                call=call,
                source_view=source_view,
                source_projection=dispatch.projection.projected_input,
                prefix_proof=prefix,
                native_projection_set=dispatch.tool_exposure_plan.direct_projection_set,
                read_through_sequence=read_through,
                retained_entry_ids=retained_ids,
                projection=projection,
            )
            semantic = prepare_compaction_summary_semantic(
                **arguments,
                summary_request=summary_request,
            )
            lifecycle_suffix = _summary_source_lifecycle_suffix(
                semantic=semantic,
                canonical_read=compaction_read,
            )
            if lifecycle_suffix:
                semantic = prepare_compaction_summary_semantic(
                    **arguments,
                    summary_request=summary_request + lifecycle_suffix,
                )
            try:
                decision = (
                    await self._provider_dispatch.measure_prepared_wire_candidate(
                        semantic, deadline=deadline
                    )
                )
            except ModelTargetCapabilityMismatch:
                return semantic, None
            return semantic, decision

        full_attempted = False
        for retained_count in range(maximum, mandatory - 1, -1):
            try:
                candidates = enumerate_safe_summary_prefixes(
                    source_view=source_view,
                    complete_tool_groups=groups,
                    retained_group_count=retained_count,
                    source_projection=dispatch.projection.projected_input,
                    deadline_monotonic=deadline,
                    maximum_source_through_sequence=read_through,
                )
            except NoSafeCompactionSummaryPrefix:
                continue
            if not candidates:
                continue
            # No token-pruned arbitrary cut search: longest whole-batch replacement.
            tail, prefix = candidates[0]
            continuation = freeze_compaction_continuation(
                source_view=source_view,
                target_branch=target_branch,
                source_through_sequence=prefix.source_through_sequence,
            )
            recent = select_recent_human_messages(
                canonical_read=compaction_read,
                source_through_sequence=prefix.source_through_sequence,
                policy=policy,
                excluded_entry_id=continuation[1],
            )
            if text_only:
                recent = tuple(
                    item for item in recent if not content_has_image(item.content.parts)
                )
            retained_ids = select_retained_prefix_entry_ids(
                canonical_read=compaction_read,
                source_through_sequence=prefix.source_through_sequence,
                recent=recent,
                active_request_entry_id=continuation[1],
            )
            if not full_attempted:
                full_attempted = True
                semantic, decision = await measure(prefix, retained_ids)
                if decision is not None and decision.wire_input_plan is not None:
                    return _SummaryWinner(
                        semantic,
                        decision,
                        tail,
                        prefix,
                        retained_ids,
                        continuation,
                        retained_count,
                    )
            # Full Q is invariant under optional-R reductions. Only P(H)+raw R
            # can become smaller by moving a whole optional batch from R to H.
            plan = freeze_destination_dialogue_projection_plan(
                canonical_read=compaction_read,
                active_request_entry_id=continuation[1],
                source_through_sequence=prefix.source_through_sequence,
                excluded_entry_ids=retained_ids,
            )
            if text_only:
                plan = project_destination_dialogue_plan_for_text_only_handover(plan)
            selected = None
            for projection in enumerate_destination_backbone_projections(plan):
                projected_ids = retained_ids
                if (
                    projection.prior_handoff is None
                    and compaction_read.snapshot_carrier is not None
                ):
                    projected_ids = select_retained_prefix_entry_ids(
                        canonical_read=compaction_read,
                        source_through_sequence=prefix.source_through_sequence,
                        recent=recent,
                        active_request_entry_id=continuation[1],
                        inherited_entry_ids=compaction_read.snapshot_carrier.retained_entry_ids,
                    )
                semantic, decision = await measure(prefix, projected_ids, projection)
                if decision is not None and decision.wire_input_plan is not None:
                    selected = (projection, semantic, decision, projected_ids)
                    break
            if selected is None:
                continue
            projection, semantic, decision, retained_ids = selected
            # Preserve as much old tool evidence as exact local admission permits.
            while True:
                challengers = []
                for evidence in projection.eligible_evidence:
                    challenger = retain_destination_tool_evidence(projection, evidence)
                    candidate_semantic, candidate_decision = await measure(
                        prefix, retained_ids, challenger
                    )
                    if (
                        candidate_decision is None
                        or candidate_decision.wire_input_plan is None
                    ):
                        continue
                    cost = (
                        candidate_decision.quote.budget_input_tokens
                        - decision.quote.budget_input_tokens,
                        candidate_decision.quote.final_wire_utf8_bytes
                        - decision.quote.final_wire_utf8_bytes,
                        -(evidence.result_entry_sequence or -1),
                        evidence.outcome_ordinal,
                    )
                    challengers.append(
                        (cost, challenger, candidate_semantic, candidate_decision)
                    )
                if not challengers:
                    break
                _, projection, semantic, decision = min(
                    challengers, key=lambda item: item[0]
                )
            return _SummaryWinner(
                semantic,
                decision,
                tail,
                prefix,
                retained_ids,
                continuation,
                retained_count,
            )
        return None

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
        destination_projection_target: PreparedKernelModelTarget | None = None,
        first_call_history_through_sequence: int | None = None,
        pending_active_candidate: PreparedActiveRootInputCandidate | None = None,
        pending_tool_batch: _PendingToolBatchSource | None = None,
        failed_wire_quote: FrozenProviderWireInputQuote | None = None,
    ) -> CompactionExecutionResult | PendingToolBatchCompaction:
        if attempt_token is None:
            attempt_token = _new_compaction_attempt_token(
                session_id=expected_scope.session_id,
                turn_id=turn_id,
                scope_kind=expected_scope.scope_kind,
                trigger=trigger,
            )
        current_maximum = maximum_retained_tool_groups
        current_pre_compact_dispatched = pre_compact_dispatched
        while True:
            result = await self._execute_compaction_fenced_once(
                failed_wire_quote=failed_wire_quote,
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
                destination_projection_target=(
                    model_switch_candidate.destination_target
                    if model_switch_candidate is not None
                    else destination_projection_target
                ),
                first_call_history_through_sequence=first_call_history_through_sequence,
                pending_active_candidate=pending_active_candidate,
                pending_tool_batch=pending_tool_batch,
            )
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
        destination_projection_target: PreparedKernelModelTarget | None,
        first_call_history_through_sequence: int | None,
        pending_active_candidate: PreparedActiveRootInputCandidate | None,
        pending_tool_batch: _PendingToolBatchSource | None,
        failed_wire_quote: FrozenProviderWireInputQuote | None = None,
    ) -> (
        CompactionExecutionResult
        | _CompactionFencedRestart
        | PendingToolBatchCompaction
    ):
        owner = self._compaction_owner
        if owner is None:
            raise RuntimeError("compaction lacks its Host owner")
        if pending_active_candidate is not None and (
            post_adoption_branch is not CompactionTargetBranch.ACTIVE_INSTALLATION
            or (pending_active_candidate.expected_provider_input_cut.turn_id != turn_id)
            or (
                pending_active_candidate.expected_provider_input_cut.session_id
                != expected_scope.session_id
            )
        ):
            raise ValueError("pending active ROOT candidate target is invalid")
        projection_target = destination_projection_target
        deadline = monotonic() + owner.policy.planning_attempt_seconds
        require_token_reclaim = trigger is not CompactionTrigger.MODEL_SWITCH_HANDOVER
        if post_adoption_branch is CompactionTargetBranch.IDLE_BASE_ONLY:
            if projection_target is None:
                raise CompactionPlanningError(
                    "idle compaction lacks its captured target"
                )
            cohort = self._continuity.current_cohort(
                ProviderInputContinuityScope(
                    session_id=expected_scope.session_id,
                    scope_kind=expected_scope.scope_kind,
                )
            )
            if cohort is not None:
                require_token_reclaim = (
                    cohort.target_bundle
                    == projection_target.epoch_call_target.target_bundle
                )
            else:
                terminal_binding = await self._io.run(
                    self._repository.read_turn_model_call_binding,
                    self._writer_lease.guard,
                    turn_id=turn_id,
                    deadline_monotonic=deadline,
                )
                require_token_reclaim = (
                    terminal_binding == projection_target.call.binding
                )
        raw_handle: PreparedProviderInputHandle | None = None
        if pending_tool_batch is not None:
            if projection_target is not None or pending_active_candidate is not None:
                raise ValueError(
                    "pending tool-batch compaction cannot change destination"
                )
            raw_handle = await self._provider_dispatch._run_preparation_handle(
                self._safe_point.freeze_pending_tool_batch_compaction,
                closed_source_cut=pending_tool_batch.closed_source_cut,
                assistant_entry_id=pending_tool_batch.assistant_entry_id,
                ordered_tool_call_ids=pending_tool_batch.ordered_tool_call_ids,
                deadline=deadline,
            )
            try:
                compaction_read = await self._io.run(
                    self._input_reader.read_frozen_compaction_cut,
                    raw_handle.cut,
                    deadline_monotonic=deadline,
                )
                transferred_handle, raw_handle = raw_handle, None
                dispatch = await self._provider_dispatch.prepare_compaction_source(
                    turn_id=turn_id,
                    model_call_index=model_call_index,
                    inherited_memory_use_policy=inherited_memory_use_policy,
                    deadline=deadline,
                    existing_handle=transferred_handle,
                    canonical_read_override=compaction_read.dispatch_read,
                    expected_source_read=compaction_read.dispatch_read,
                    independent_auxiliary_source=True,
                )
                projected_read = compaction_read
            finally:
                if raw_handle is not None:
                    raw_handle.close()
        elif projection_target is not None:
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
            raw_handle = await self._provider_dispatch._run_preparation_handle(
                freeze_operation,
                turn_id=turn_id,
                **({"allow_terminal": True} if terminal_source else {}),
                deadline=deadline,
            )
            try:
                compaction_read = await self._io.run(
                    self._input_reader.read_frozen_compaction_cut,
                    raw_handle.cut,
                    deadline_monotonic=deadline,
                )
                projected_read = compaction_read
                transferred_handle = raw_handle
                raw_handle = None
                dispatch = await self._provider_dispatch.prepare_compaction_source(
                    turn_id=turn_id,
                    model_call_index=model_call_index,
                    inherited_memory_use_policy=inherited_memory_use_policy,
                    deadline=deadline,
                    existing_handle=transferred_handle,
                    prepared_target_override=projection_target,
                    canonical_read_override=projected_read.dispatch_read,
                    expected_source_read=compaction_read.dispatch_read,
                    independent_auxiliary_source=True,
                )
            finally:
                if raw_handle is not None:
                    raw_handle.close()
        else:
            source_target_override = None
            scope = ProviderInputContinuityScope(
                session_id=expected_scope.session_id,
                scope_kind=expected_scope.scope_kind,
            )
            cohort = self._continuity.current_cohort(scope)
            if cohort is not None:
                binding = await self._io.run(
                    self._repository.read_turn_model_call_binding,
                    self._writer_lease.guard,
                    turn_id=turn_id,
                    deadline_monotonic=deadline,
                )
                source_target_override = (
                    self._provider_dispatch.prepare_installed_source_target(
                        scope=scope,
                        source=cohort.target_bundle,
                        turn_id=turn_id,
                        model_call_index=model_call_index,
                        binding_override=binding,
                    )
                )
            dispatch = await self._provider_dispatch.prepare_compaction_source(
                turn_id=turn_id,
                model_call_index=model_call_index,
                inherited_memory_use_policy=inherited_memory_use_policy,
                deadline=deadline,
                allow_terminal_compaction=(
                    post_adoption_branch is CompactionTargetBranch.IDLE_BASE_ONLY
                ),
                prepared_target_override=source_target_override,
                independent_auxiliary_source=True,
            )
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
            # Ordinary compaction keeps the exact source target throughout dry
            # successor validation and adoption. Settings edits are handled by
            # the next ordinary safe-point's destination handover.
            ordinary_destination_target = self._model.prepare_resolved_target(
                KernelModelTargetPreparationRequest(
                    session_id=expected_scope.session_id,
                    turn_id=turn_id,
                    model_call_index=model_call_index,
                    purpose=dispatch.prepared_call.call.fact.purpose,
                    maximum_input_tokens=dispatch.prepared_call.compile_binding.effective_input_budget_tokens,
                    binding=dispatch.prepared_call.call.binding,
                ),
                target=dispatch.prepared_call.call.target,
                binding=dispatch.prepared_call.call.binding,
            )
            source_quote = dispatch.discard_wire_materialization_to_quote()
            # A hard-boundary recovery must admit every legal successor, even
            # when its mandatory current suffix cannot reach a proactive target.
            # The final wire and model hard budgets are still checked below.
            force = force or (
                source_quote.budget_input_tokens
                > source_quote.effective_input_budget_tokens
                or source_quote.final_wire_utf8_bytes
                > MAXIMUM_PROVIDER_WIRE_INPUT_BYTES
            )
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
        candidate_family: PreparedCompactionCandidateFamily | None = None
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
            winner = await self._select_summary(
                dispatch=dispatch,
                source_view=source_view,
                compaction_read=compaction_read,
                policy=owner.policy,
                maximum_retained_tool_groups=maximum_retained_tool_groups,
                summary_request=summary_request,
                target_branch=post_adoption_branch,
                deadline=deadline,
                first_call_history_through_sequence=first_call_history_through_sequence,
            )
            if winner is None:
                return CompactionExecutionResult(
                    CompactionOutcome(
                        CompactionDisposition.FAILED,
                        turn_id,
                        None,
                        None,
                        "NO_EXECUTABLE_SUMMARY_SOURCE",
                    )
                )
            source_projected = winner.semantic.source_proof.projection is not None
            semantic = winner.semantic
            selected_summary_decision = winner.decision
            tail, prefix = winner.tail, winner.prefix
            selected_continuation = winner.continuation
            selected_retained_count = winner.retained_tool_group_count
            retained_entry_ids = winner.retained_entry_ids
            if selected_summary_decision.wire_input_plan is None:
                raise CompactionPlanningError(
                    "selected compaction summary is not executable"
                )
            successor_destination = (
                projection_target.epoch_call_target
                if projection_target is not None
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
                phase=CompactionAttemptPhase.STREAMING,
            )
            # The provider transport owns connect/write/read-idle watchdogs.
            # A progressing summary stream has no independent total deadline,
            # matching ordinary foreground model execution.
            try:
                if self._workspace_gate is not None:
                    await self._workspace_gate.wait_available()
                raw = await prepared_summary.open_once()
            except (ProviderModelExecutionFailed, ProviderModelOutputIncomplete):
                raise
            if raw.tool_calls:
                # Tool calls are never dispatched.  One independent repair is
                # permitted; it appends a provider-valid ephemeral denial group
                # to the exact first request rather than replaying that request.
                owner.advance_phase(
                    scope_kind=expected_scope.scope_kind,
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
                    if self._workspace_gate is not None:
                        await self._workspace_gate.wait_available()
                    raw = await repair.open_once()
                except (
                    ProviderModelExecutionFailed,
                    ProviderModelOutputIncomplete,
                ):
                    raise
                if raw.tool_calls:
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
                raise
            owner.advance_phase(
                scope_kind=expected_scope.scope_kind,
                phase=CompactionAttemptPhase.VALIDATED,
            )
            # R is frozen before summary. A different H/R cut requires a new S.
            carrier = build_compaction_snapshot_carrier(
                summary=summary,
                retained_entry_ids=retained_entry_ids,
                continuation_mode=selected_continuation[0],
                active_request_entry_id=selected_continuation[1],
            )
            content = self._content_publisher.describe(
                workspace_id=compaction_read.scope.workspace_id,
                content=carrier.body,
                media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
                codec=CONTEXT_SNAPSHOT_CODEC,
            )
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
            snapshot_id = (
                manual_compaction_snapshot_id(
                    session_id=expected_scope.session_id,
                    command_id=stable_command_id,
                )
                if stable_command_id is not None
                else derive_id(
                    "snapshot",
                    "compact",
                    expected_scope.session_id,
                    expected_scope.turn_id,
                    trigger.value,
                    source_digest,
                )
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
                scope=expected_scope,
                deadline=successor_deadline,
            )
            owner.advance_phase(
                scope_kind=expected_scope.scope_kind,
                phase=CompactionAttemptPhase.ADOPTION_PREPARED,
            )
            next_handle = dispatch.take_handle()
            source_handle_transferred = True
            dry_wire_candidate = None
            dry_wire_decision = None
            prepared_candidate: PreparedCompactionCandidate | None = None
            pending_prepared_candidate = None
            pending_wire_decision = None
            try:
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
                                if lineage.persisted_context_snapshot_id is None
                                else "SNAPSHOT"
                            ),
                            context_snapshot_id=lineage.persisted_context_snapshot_id,
                            source_through_sequence=(
                                lineage.persisted_revision_genesis_marker
                            ),
                        ),
                        snapshot_id=snapshot_id,
                        binding_revision_id=derive_id(
                            "binding",
                            "revision",
                            expected_scope.turn_id,
                            str(lineage.binding_revision_ordinal + 1),
                        ),
                        event_id=derive_id(
                            "event",
                            "accepted",
                            expected_scope.session_id,
                            "COMPACTION_ADOPTED",
                            "CONTEXT_BINDING_REVISION",
                            derive_id(
                                "binding",
                                "revision",
                                expected_scope.turn_id,
                                str(lineage.binding_revision_ordinal + 1),
                            ),
                            "",
                        ),
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
                            ordinary_destination_target
                            if projection_target is None
                            else projection_target
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
                dry_wire_candidate = dry_wire_decision.candidate
                prepared_candidate = replace(
                    prepared_candidate, wire_candidate=dry_wire_candidate
                )
                validate_compaction_wire_transition(
                    source_view=source_view,
                    source_candidate=dispatch.wire_candidate,
                    successor_wire=dry_wire_decision,
                    phase="PRE_FULL",
                    require_token_reclaim=require_token_reclaim,
                    destination_target=projection_target or ordinary_destination_target,
                    failed_wire_quote=failed_wire_quote,
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
                            seed=replace(seed, dispatch_read=prospective_active_read),
                            deadline=successor_deadline,
                        )
                    )
                    pending_wire_decision = (
                        await self._provider_dispatch.measure_prepared_wire_candidate(
                            pending_prepared_candidate.wire_candidate,
                            deadline=successor_deadline,
                        )
                    )
                    pending_prepared_candidate = replace(
                        pending_prepared_candidate,
                        wire_candidate=pending_wire_decision.candidate,
                    )
                    if pending_wire_decision.wire_input_plan is None:
                        raise StructuredModelInputCompileError(
                            ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED
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
                    if pending_tool_batch is not None:
                        raise CompactionPlanningError(
                            "pending tool batch lost its installed source epoch"
                        )
                    pending_empty_adoption = self._safe_point.begin_empty_adoption(
                        continuity=self._continuity,
                        dry_projection=dry_dispatch,
                        attempt_token=attempt_token,
                        candidate=candidate,
                    )
            except CompactionWireTransitionDrift:
                # Re-capture exact authority after source/target drift.
                return _CompactionFencedRestart(
                    maximum_retained_tool_groups=maximum_retained_tool_groups,
                    pre_compact_dispatched=pre_compact_dispatched,
                )
            except (
                CompactionPlanningError,
                StructuredModelInputCompileError,
                ModelTargetCapabilityMismatch,
            ) as exc:
                if selected_retained_count <= minimum_pending_image_tool_groups(
                    source_view=source_view,
                    complete_tool_groups=enumerate_complete_tool_groups(
                        compaction_read
                    ),
                ) or not _can_retry_compaction_with_smaller_tail(exc):
                    if failed_wire_quote is None and isinstance(
                        exc, CompactionReclaimUnavailable
                    ):
                        return _already_compact_result(turn_id)
                    raise
                # A genuine successor hard-boundary miss may move the next
                # optional whole batch into H and regenerate its summary.
                return _CompactionFencedRestart(
                    maximum_retained_tool_groups=(selected_retained_count - 1),
                    pre_compact_dispatched=pre_compact_dispatched,
                )
            if pending_tool_batch is not None:
                if (
                    dry_dispatch is None
                    or prepared_candidate is None
                    or dry_wire_decision is None
                    or dry_wire_decision.wire_input_plan is None
                    or prepared_candidate.wire_candidate.append_result is None
                ):
                    raise RuntimeError("pending tool batch lost its dry prefix quote")
                pending = PendingToolBatchCompaction(
                    prepared_call=prepared_candidate.wire_candidate.prepared_call,
                    compiled_input=prepared_candidate.wire_candidate.append_result.compiled_input,
                    wire_input_plan=dry_wire_decision.wire_input_plan,
                    _coordinator=self,
                    _adoption=_PendingToolBatchAdoption(
                        dry_dispatch=dry_dispatch,
                        candidate=candidate,
                        source_read=compaction_read,
                        source=pending_tool_batch,
                        destination_target=ordinary_destination_target,
                        scope=expected_scope,
                        model_call_index=model_call_index,
                        memory_use_policy=inherited_memory_use_policy,
                        attempt_token=attempt_token,
                        hook_scope=hook_scope,
                        session_start_compact_port=session_start_compact_port,
                        session_start_boundary_port=session_start_boundary_port,
                        source_projected=source_projected,
                    ),
                )
                dry_dispatch = None
                return pending
            # The winner keeps only its installed dry assembly.  Quote
            # materialization is no longer separately owned here.
            del dry_wire_decision, dry_wire_candidate
            if next_handle is not None:
                next_handle.close()
                next_handle = None
            owner.advance_phase(
                scope_kind=expected_scope.scope_kind,
                phase=CompactionAttemptPhase.SETTLING,
            )
            settlement_dispatch = dry_dispatch
            dry_dispatch = None
            settlement_operation = self._complete_compaction_settlement(
                source_projected=source_projected,
                failed_wire_quote=failed_wire_quote,
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
                    if projection_target is None
                    else projection_target.target.fact.model_id
                ),
                hook_cwd=(
                    str(self._tools.snapshot_workspace_root())
                    if self._hook_dispatcher is not None
                    else ""
                ),
                session_start_compact_port=session_start_compact_port,
                session_start_boundary_port=session_start_boundary_port,
                destination_projection_target=projection_target,
                ordinary_destination_target=ordinary_destination_target,
                pending_active_candidate=pending_active_candidate,
                pending_empty_adoption=pending_empty_adoption,
            )
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
            return await _join_compaction_settlement(settlement)
        finally:
            if not source_handle_transferred:
                dispatch.close()
            if dry_dispatch is not None:
                dry_dispatch.close()
            if (
                candidate_family is not None
                and candidate_family.owns_resource_authority
            ):
                candidate_family.close()
            if next_handle is not None:
                next_handle.close()

    async def _finalize_pending_tool_batch(
        self,
        pending: _PendingToolBatchAdoption,
    ) -> CompactionExecutionResult:
        """Revalidate the real complete suffix before adopting its old prefix."""

        owner = self._compaction_owner
        if owner is None:
            pending.dry_dispatch.close()
            raise RuntimeError("pending tool-batch compaction lost its Host owner")
        pending_dry: PreparedCompactionDryProjection | None = pending.dry_dispatch
        completed_execution: CompactionExecutionResult | None = None

        async def operation() -> CompactionExecutionResult:
            nonlocal pending_dry, completed_execution
            deadline = monotonic() + owner.policy.planning_attempt_seconds
            handle: PreparedProviderInputHandle | None = None
            source_dispatch: PreparedCompactionSourceDispatch | None = None
            family: PreparedCompactionCandidateFamily | None = None
            final_dry: PreparedCompactionDryProjection | None = None
            try:
                await self._io.run(
                    self._repository.require_pending_tool_batch_compaction_safe,
                    self._writer_lease.guard,
                    closed_source_cut=pending.source.closed_source_cut,
                    assistant_entry_id=pending.source.assistant_entry_id,
                    ordered_tool_call_ids=pending.source.ordered_tool_call_ids,
                    deadline_monotonic=deadline,
                )
                if pending_dry is None:
                    raise RuntimeError("pending tool-batch dry owner is consumed")
                handle = pending_dry.take_handle_for_rotation()
                pending_dry = None
                transferred_handle, handle = handle, None
                handle = await self._provider_dispatch._run_preparation_handle(
                    self._safe_point.rotate_provider_input,
                    transferred_handle,
                    turn_id=pending.scope.turn_id,
                    deadline=deadline,
                    abandoned_handle=transferred_handle,
                )
                actual_read = await self._io.run(
                    self._input_reader.read_frozen_compaction_cut,
                    handle.cut,
                    deadline_monotonic=deadline,
                )
                if (
                    actual_read.scope != pending.scope
                    or actual_read.turn_status != "RUNNING"
                    or actual_read.lineage_base != pending.source_read.lineage_base
                ):
                    raise ConversationKernelConflict(
                        "pending tool-batch source lineage changed"
                    )
                transferred_handle, handle = handle, None
                source_dispatch = (
                    await self._provider_dispatch.prepare_compaction_source(
                        turn_id=pending.scope.turn_id,
                        model_call_index=pending.model_call_index,
                        inherited_memory_use_policy=pending.memory_use_policy,
                        deadline=deadline,
                        existing_handle=transferred_handle,
                        prepared_target_override=pending.destination_target,
                        canonical_read_override=actual_read.dispatch_read,
                        expected_source_read=actual_read.dispatch_read,
                    )
                )
                source_quote = source_dispatch.discard_wire_materialization_to_quote()
                source_view = freeze_compaction_source_view(
                    canonical_read=actual_read,
                    compile_binding=source_dispatch.prepared_call.compile_binding,
                    semantic_projection=source_dispatch.projection,
                    predecessor_epoch_view=source_dispatch.planning.predecessor_view,
                    provider_wire_quote=source_quote,
                    producer_wire_api=source_dispatch.prepared_call.call.target.model_profile.route_wire_profile.wire_api,
                )
                groups = enumerate_complete_tool_groups(actual_read)
                prefix_options = enumerate_safe_summary_prefixes(
                    source_view=source_view,
                    complete_tool_groups=groups,
                    retained_group_count=0,
                    source_projection=source_dispatch.projection.projected_input,
                    deadline_monotonic=deadline,
                    maximum_source_through_sequence=pending.candidate.snapshot.source_through_sequence,
                )
                selected = next(
                    (
                        tail
                        for tail, prefix in prefix_options
                        if prefix.source_through_sequence
                        == pending.candidate.snapshot.source_through_sequence
                    ),
                    None,
                )
                if selected is None:
                    raise ConversationKernelConflict(
                        "prepared summary prefix is no longer a safe boundary"
                    )
                candidate = pending.candidate
                content = candidate.snapshot.content
                synthetic_read = build_synthetic_compaction_dispatch_read(
                    canonical_read=actual_read,
                    source_through_sequence=candidate.snapshot.source_through_sequence,
                    snapshot_id=candidate.snapshot.snapshot_id,
                    binding_revision_id=candidate.binding.binding_revision_id,
                    binding_revision_ordinal=candidate.binding.revision_ordinal,
                    snapshot_carrier=candidate.snapshot.carrier,
                    snapshot_content_digest=content.digest,
                    snapshot_content_size=content.size,
                    snapshot_content_media_type=content.media_type,
                    snapshot_content_codec=content.codec,
                    snapshot_blob_id=getattr(content, "blob_id", None),
                )
                seed = CompactionDryProjectionSeed(
                    dispatch_read=synthetic_read,
                    binding_rewrite_identity=candidate.binding.binding_revision_id,
                    protected_tail_selection_fingerprint=selected.protected_tail_selection_fingerprint,
                )
                runtime_source, _ = await self._freeze_compaction_runtime_source(
                    scope=pending.scope,
                    deadline=deadline,
                )
                handle = source_dispatch.take_handle()
                source_wire_candidate = source_dispatch.wire_candidate
                planning = source_dispatch.planning
                source_dispatch = None
                transferred_handle, handle = handle, None
                family = (
                    await self._provider_dispatch.prepare_compaction_candidate_family(
                        turn_id=pending.scope.turn_id,
                        model_call_index=pending.model_call_index,
                        inherited_memory_use_policy=pending.memory_use_policy,
                        deadline=deadline,
                        canonical_read=synthetic_read,
                        expected_source_read=actual_read.dispatch_read,
                        seed=seed,
                        existing_handle=transferred_handle,
                        source_replacements=(runtime_source,),
                        retained_skill_read=actual_read,
                        prepared_target_override=pending.destination_target,
                        dry_projection=True,
                        source_planning_basis=planning,
                    )
                )
                prepared = await self._provider_dispatch.prepare_compaction_candidate(
                    family,
                    canonical_read=synthetic_read,
                    seed=seed,
                    deadline=deadline,
                )
                decision = (
                    await self._provider_dispatch.measure_prepared_wire_candidate(
                        prepared.wire_candidate,
                        deadline=deadline,
                    )
                )
                prepared = replace(prepared, wire_candidate=decision.candidate)
                validate_compaction_wire_transition(
                    source_view=source_view,
                    source_candidate=source_wire_candidate,
                    successor_wire=decision,
                    phase="PRE_FULL",
                    destination_target=pending.destination_target,
                )
                if decision.wire_input_plan is None:
                    raise CompactionPlanningError(
                        "complete tool-batch successor is not admissible"
                    )
                final_dry = (
                    self._provider_dispatch.bind_selected_compaction_dry_projection(
                        family=family,
                        selected=prepared,
                        decision=decision,
                    )
                )
                family = None
                owner.advance_phase(
                    scope_kind=pending.scope.scope_kind,
                    phase=CompactionAttemptPhase.SETTLING,
                )
                settlement_dry, final_dry = final_dry, None
                settlement_operation = self._complete_compaction_settlement(
                    source_projected=pending.source_projected,
                    turn_id=pending.scope.turn_id,
                    model_call_index=pending.model_call_index,
                    inherited_memory_use_policy=pending.memory_use_policy,
                    force=True,
                    expected_scope=pending.scope,
                    target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
                    candidate=candidate,
                    preconditions=CompactionCanonicalWritePreconditions(
                        scope=pending.scope,
                        expected_turn_status="RUNNING",
                        expected_safe_head=actual_read.safe_head_range.source_through_sequence,
                        provider_safe=True,
                    ),
                    dry_dispatch=settlement_dry,
                    source_view=source_view,
                    source_wire_candidate=source_wire_candidate,
                    protected_tail_selection_fingerprint=selected.protected_tail_selection_fingerprint,
                    compaction_read=actual_read,
                    trigger=pending.attempt_token.trigger,
                    attempt_token=pending.attempt_token,
                    hook_scope=pending.hook_scope,
                    hook_model_id=pending.destination_target.target.fact.model_id,
                    hook_cwd=str(self._tools.snapshot_workspace_root())
                    if self._hook_dispatcher is not None
                    else "",
                    session_start_compact_port=pending.session_start_compact_port,
                    session_start_boundary_port=pending.session_start_boundary_port,
                    destination_projection_target=None,
                    ordinary_destination_target=pending.destination_target,
                    pending_active_candidate=None,
                    pending_empty_adoption=None,
                )
                try:
                    settlement = owner.start_settlement(
                        settlement_operation,
                        name=f"kernel-compaction-settlement:{candidate.snapshot.snapshot_id}",
                    )
                except BaseException:
                    settlement_operation.close()
                    settlement_dry.close()
                    raise
                completed_execution = await _join_compaction_settlement(settlement)
                return completed_execution
            finally:
                if handle is not None:
                    handle.close()
                if source_dispatch is not None:
                    source_dispatch.close()
                if family is not None and family.owns_resource_authority:
                    family.close()
                if final_dry is not None:
                    final_dry.close()

        try:
            return await owner.run_fenced(
                scope=pending.scope,
                trigger=pending.attempt_token.trigger,
                operation=operation,
                attempt_id=pending.attempt_token.attempt_id,
            )
        except _PostAdoptionCompactionFailure as failure:
            raise failure.error.with_traceback(failure.error.__traceback__)
        except BaseException as error:
            # FULL may already have returned its unique successor before the
            # Host fence-removal await is cancelled.  That exact owner must not
            # disappear with the waiter after canonical adoption.
            if (
                completed_execution is not None
                and completed_execution.successor_dispatch is not None
            ):
                completed_execution.successor_dispatch.close(error=error)
            if isinstance(error, Exception) and not isinstance(error, StaleHostWriter):
                owner.record_automatic_failure(scope_kind=pending.scope.scope_kind)
            raise
        finally:
            if pending_dry is not None:
                pending_dry.close()

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
        destination_projection_target: PreparedKernelModelTarget | None,
        ordinary_destination_target: PreparedKernelModelTarget,
        pending_active_candidate: PreparedActiveRootInputCandidate | None,
        pending_empty_adoption: object | None,
        failed_wire_quote: FrozenProviderWireInputQuote | None = None,
        source_projected: bool = False,
    ) -> CompactionExecutionResult:
        """Drain canonical FULL and its exact process-local branch settlement."""

        owner = self._compaction_owner
        if owner is None:
            raise RuntimeError("compaction settlement lost its Host owner")
        continuity_scope = ProviderInputContinuityScope(
            session_id=expected_scope.session_id,
            scope_kind=expected_scope.scope_kind,
        )
        successor_dispatch: PreparedProviderDispatch | None = None
        prospective_active_input: PreparedProspectiveActiveRootInput | None = None
        adopted_outcome: CompactionOutcome | None = None
        empty_adoption_resealed = False
        no_continuation_retirement_started = False
        source_identity = (
            compaction_read.dispatch_read.compile_snapshot.canonical_input.identity
        )
        source_cut = PreparedProviderInputCut(
            session_id=source_identity.session_id,
            turn_id=source_identity.turn_id,
            context_binding_revision_id=(source_identity.context_binding_revision_id),
            provider_input_through_sequence=(
                source_identity.provider_input_through_sequence
            ),
        )
        if isinstance(dry_dispatch, PreparedProviderDispatch):
            dry_destination = dry_dispatch.prepared_call.epoch_call_target
        else:
            if dry_dispatch is None:
                raise RuntimeError("compaction settlement lost its dry destination")
            dry_destination = dry_dispatch.result.basis.destination
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
                        confirmation=confirmation,
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
            if source_projected and self._presentation_notice_sink is not None:
                try:
                    self._presentation_notice_sink(
                        "为适配当前模型的输入限制，本次摘要省略了部分较早历史或工具正文；原始记录仍保留。"
                    )
                except Exception:
                    # Presentation is advisory; FULL and physical settlement
                    # remain owned by this operation if its UI sink is gone.
                    pass
            post_hook_deadline = monotonic() + owner.policy.planning_attempt_seconds
            if session_start_boundary_port is not None:
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
                            cut=source_cut,
                            destination=dry_destination,
                        )
                    owner.reset_automatic_failures(
                        scope_kind=expected_scope.scope_kind,
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
                            cut=source_cut,
                            destination=dry_destination,
                        )
                    owner.reset_automatic_failures(
                        scope_kind=expected_scope.scope_kind,
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
                if rotation_handle is None:
                    rotated = await self._provider_dispatch._run_preparation_handle(
                        self._safe_point.freeze_compaction_input,
                        turn_id=turn_id,
                        allow_terminal=False,
                        deadline=base_deadline,
                    )
                else:
                    rotated = await self._provider_dispatch._run_preparation_handle(
                        self._safe_point.rotate_provider_input,
                        rotation_handle,
                        turn_id=turn_id,
                        deadline=base_deadline,
                        abandoned_handle=rotation_handle,
                    )
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
                        or actual_binding.binding_revision_id
                        != candidate.binding.binding_revision_id
                        or (
                            actual_binding.context_snapshot_id
                            != candidate.snapshot.snapshot_id
                        )
                    ):
                        raise ConversationKernelConflict(
                            "post-adoption cut does not name the adopted compaction winner"
                        )
                    (
                        current_runtime_source,
                        _current_runtime_handoff,
                    ) = await self._freeze_compaction_runtime_source(
                        scope=expected_scope,
                        deadline=base_deadline,
                    )
                    destination_epoch_target = (
                        destination_projection_target.epoch_call_target
                        if destination_projection_target is not None
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
                                ordinary_destination_target
                                if destination_projection_target is None
                                else destination_projection_target
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
                        post_base_candidate = replace(
                            post_base_candidate,
                            wire_candidate=post_base_decision.candidate,
                        )
                        validate_compaction_wire_transition(
                            source_view=source_view,
                            source_candidate=source_wire_candidate,
                            successor_wire=post_base_decision,
                            phase="POST_FULL",
                            destination_target=destination_projection_target
                            or ordinary_destination_target,
                            failed_wire_quote=failed_wire_quote,
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
                            selected_post_candidate = replace(
                                selected_post_candidate,
                                wire_candidate=selected_post_decision.candidate,
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
                if session_start_compact_port is not None:
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
                                cut=source_cut,
                                destination=dry_destination,
                            )
                            owner.reset_automatic_failures(
                                scope_kind=expected_scope.scope_kind,
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
                            hook_sibling.candidate = hook_decision.candidate
                            if pending_active_candidate is None:
                                validate_compaction_wire_transition(
                                    source_view=source_view,
                                    source_candidate=source_wire_candidate,
                                    successor_wire=hook_decision,
                                    phase="POST_FULL",
                                    destination_target=destination_projection_target
                                    or ordinary_destination_target,
                                    failed_wire_quote=failed_wire_quote,
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
                if not empty_adoption_resealed:

                    def abandon_continuation(error: BaseException) -> None:
                        self._retire_full_adoption_without_continuation(
                            scope=continuity_scope,
                            turn_id=turn_id,
                            attempt_token=attempt_token,
                            confirmation=confirmation,
                            candidate=candidate,
                            evidence=_issue_successor_final_abandonment_evidence(error),
                            dispatch=None,
                            cut=source_cut,
                            destination=dry_destination,
                        )

                    dry_dispatch.install_continuation_abandonment(abandon_continuation)
                    # Further resource retirement belongs to that exact dispatch.
                    no_continuation_retirement_started = True
                if pending_active_candidate is None:
                    successor_dispatch = dry_dispatch
                    dry_dispatch = None
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
                        cut=source_cut,
                        destination=dry_destination,
                    )
                owner.advance_phase(
                    scope_kind=expected_scope.scope_kind,
                    phase=CompactionAttemptPhase.IDLE_BASE_ADOPTED,
                )
            owner.reset_automatic_failures(
                scope_kind=expected_scope.scope_kind,
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
                prospective_active_input=prospective_active_input,
                successor_wire_decision=(
                    selected_wire_decision if successor_dispatch is not None else None
                ),
            )
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
                    cut=source_cut,
                    destination=dry_destination,
                )
            raise _PostAdoptionCompactionFailure(adopted_outcome, error) from error
        finally:
            if dry_dispatch is not None:
                dry_dispatch.close()
            if prospective_active_input is not None:
                prospective_active_input.close()

    async def compact_idle_turn(
        self,
        *,
        turn_id: str,
        command_id: str,
        prepared_target: PreparedKernelModelTarget,
        scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
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
        )
        attempt_token = _new_compaction_attempt_token(
            session_id=scope.session_id,
            turn_id=turn_id,
            scope_kind=scope_kind,
            trigger=CompactionTrigger.MANUAL,
        )

        async def operation() -> CompactionExecutionResult:
            return await self._execute_compaction_fenced(
                turn_id=turn_id,
                model_call_index=1,
                inherited_memory_use_policy=MemoryUsePolicy.ENABLED,
                trigger=CompactionTrigger.MANUAL,
                force=True,
                expected_scope=scope,
                post_adoption_branch=CompactionTargetBranch.IDLE_BASE_ONLY,
                stable_command_id=command_id,
                hook_scope=(self._hook_root_scope),
                session_start_boundary_port=session_start_boundary_port,
                attempt_token=attempt_token,
                destination_projection_target=prepared_target,
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
            except asyncio.CancelledError:
                raise
            except Exception:
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
            except (ConversationKernelConflict, ValueError, TypeError):
                # A deterministic rejection is not an uncertain commit. Confirm
                # the exact candidate once before ending this attempt.
                return await self._io.run(
                    self._repository.confirm_context_snapshot_adoption,
                    candidate=candidate,
                    deadline_monotonic=self._canonical_deadline(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
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
        cut: PreparedProviderInputCut,
        destination: FrozenEpochModelCallTarget,
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
            candidate=candidate,
            cut=cut,
            destination=destination,
            adopted_snapshot_id=candidate.snapshot.snapshot_id,
            adopted_binding_revision_id=candidate.binding.binding_revision_id,
        )

        def verify_physical_release() -> None:
            self._tools.assert_no_tool_surface_borrows(
                scope_kind=scope.scope_kind,
            )
            self._model.model_runtime.assert_no_provider_borrows(
                session_id=scope.session_id,
                turn_id=turn_id,
            )

        self._safe_point.retire_full_adoption_without_continuation_and_arm_empty(
            continuity=self._continuity,
            fence=fence,
            evidence=evidence,
            dispatch=dispatch,
            verify_physical_release=verify_physical_release,
        )


__all__ = [
    "CompactionCoordinator",
    "PendingToolBatchCompaction",
    "CompactionExecutionResult",
    "CompactionWireTransitionDrift",
    "ValidatedCompactionWireTransition",
    "validate_compaction_wire_transition",
]
