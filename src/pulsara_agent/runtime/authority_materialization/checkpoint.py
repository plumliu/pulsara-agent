"""Service-owned transcript checkpoint candidates and COW materialization."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Sequence, TypeVar

from pulsara_agent.primitives.authority_materialization import (
    ActivePhysicalReservationStateFact,
    AuthorityMaterializationLimits,
    CheckpointConsumerCauseFact,
    CheckpointDispatchBarrierFact,
    LedgerMaterializationAccountStateFact,
    LedgerMaterializationAccountTransitionFact,
    LedgerMaterializationConsumerHorizonFact,
    LedgerMaterializationTransitionCauseIdentityFact,
    PhysicalBurstContractFact,
    PhysicalOperationReservationFact,
    PhysicalOperationSettlementFact,
    TranscriptProjectionLiveAssemblyState,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.transcript_checkpoint import (
    CheckpointCancelledTerminalReferenceFact,
    CheckpointCommittedTerminalReferenceFact,
    CheckpointDispatchBarrierReleaseFact,
    CheckpointFailedTerminalReferenceFact,
    CheckpointRecoveredInterruptedTerminalReferenceFact,
    CheckpointCancellationReasonCode,
    CheckpointCancellationSource,
    CheckpointCancellationReasonRuleFact,
    CheckpointDiagnosticSanitizationContractFact,
    CheckpointFailureReasonCode,
    CheckpointFailureReasonStageRuleFact,
    CheckpointTerminalContractFact,
    CheckpointTerminalDiagnosticFact,
    CheckpointTerminalDiagnosticCode,
    TranscriptProjectionCheckpointCandidateFact,
)
from pulsara_agent.primitives.transcript_projection import (
    EmptyTranscriptProjectionCheckpointMaterializationFact,
    NonEmptyTranscriptProjectionCheckpointMaterializationFact,
    RunTranscriptSeedReferenceFact,
    RunTranscriptSeedSemanticFact,
    TranscriptProjectionCheckpointMaterializationFact,
    TranscriptProjectionScopeFact,
    TranscriptProjectionSemanticSourceFact,
)
from pulsara_agent.event import (
    AgentEvent,
    CheckpointDispatchBarrierInstalledEvent,
    CheckpointDispatchBarrierReleasedEvent,
    EventContext,
    PhysicalOperationReservationCreatedEvent,
    PhysicalOperationReservationSettledEvent,
    LedgerMaterializationConsumerHorizonAdvancedEvent,
    LedgerMaterializationGenerationAdvancedEvent,
    TranscriptProjectionCheckpointCommittedEvent,
    TranscriptProjectionCheckpointCancelledEvent,
    TranscriptProjectionCheckpointFailedEvent,
    TranscriptProjectionCheckpointRecoveredInterruptedEvent,
    TranscriptProjectionCheckpointIntentEvent,
)
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.runtime.authority_materialization.account import (
    LedgerMaterializationCoordinator,
    build_account_state,
    build_generation,
    deterministic_bookkeeping_charge,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
    TranscriptProjectionStateStore,
)
from pulsara_agent.runtime.authority_materialization.transcript_tree import (
    PreparedTranscriptProjectionMaterialization,
    TranscriptProjectionMaterializationContracts,
    prepare_transcript_projection_materialization,
)


TRANSCRIPT_CHECKPOINT_BUILD_CONTRACT_FINGERPRINT = context_fingerprint(
    "transcript-checkpoint-build-contract:v1",
    {
        "source": "incremental-stable-live-transcript-state:v1",
        "tree": "content-addressed-bounded-fanout-cow:v1",
        "identity": "semantic-source-separated-from-acceleration:v1",
    },
)


@dataclass(frozen=True, slots=True)
class PreparedTranscriptCheckpoint:
    candidate: TranscriptProjectionCheckpointCandidateFact
    materialization: PreparedTranscriptProjectionMaterialization
    prepared_at_monotonic: float


@dataclass(frozen=True, slots=True)
class InstalledTranscriptCheckpoint:
    prepared: PreparedTranscriptCheckpoint
    intent_event: TranscriptProjectionCheckpointIntentEvent
    reservation: PhysicalOperationReservationFact
    reservation_event: PhysicalOperationReservationCreatedEvent
    barrier: CheckpointDispatchBarrierFact
    barrier_event: CheckpointDispatchBarrierInstalledEvent
    stored_events: tuple[AgentEvent, ...]
    resulting_account_state: LedgerMaterializationAccountStateFact

    @property
    def checkpoint_id(self) -> str:
        return self.prepared.candidate.checkpoint_id

    @property
    def checkpoint_candidate_fingerprint(self) -> str:
        return self.prepared.candidate.candidate_fingerprint


@dataclass(frozen=True, slots=True)
class RestoredTranscriptCheckpointOwner:
    """Durable checkpoint owner reconstructed without artifact build inputs."""

    checkpoint_id: str
    checkpoint_candidate_fingerprint: str
    intent_event: TranscriptProjectionCheckpointIntentEvent
    reservation: PhysicalOperationReservationFact
    reservation_event: PhysicalOperationReservationCreatedEvent
    barrier: CheckpointDispatchBarrierFact
    barrier_event: CheckpointDispatchBarrierInstalledEvent
    stored_events: tuple[AgentEvent, ...]
    resulting_account_state: LedgerMaterializationAccountStateFact


CheckpointTerminalOwner = (
    InstalledTranscriptCheckpoint | RestoredTranscriptCheckpointOwner
)


_StoredCheckpointEventT = TypeVar("_StoredCheckpointEventT", bound=AgentEvent)


def _canonical_stored_event(
    stored_events: tuple[AgentEvent, ...],
    candidate: _StoredCheckpointEventT,
    expected_type: type[_StoredCheckpointEventT],
) -> _StoredCheckpointEventT:
    matches = tuple(event for event in stored_events if event.id == candidate.id)
    if len(matches) != 1 or not isinstance(matches[0], expected_type):
        raise RuntimeError("checkpoint commit returned an invalid canonical event")
    return matches[0]


@dataclass(frozen=True, slots=True)
class CommittedTranscriptCheckpoint:
    installed: InstalledTranscriptCheckpoint
    committed_event: TranscriptProjectionCheckpointCommittedEvent
    horizon_event: LedgerMaterializationConsumerHorizonAdvancedEvent
    generation_event: LedgerMaterializationGenerationAdvancedEvent | None
    settlement_event: PhysicalOperationReservationSettledEvent
    barrier_release_event: CheckpointDispatchBarrierReleasedEvent
    stored_events: tuple[AgentEvent, ...]
    resulting_account_state: LedgerMaterializationAccountStateFact


CheckpointNonSuccessTerminalEvent = (
    TranscriptProjectionCheckpointFailedEvent
    | TranscriptProjectionCheckpointCancelledEvent
    | TranscriptProjectionCheckpointRecoveredInterruptedEvent
)


@dataclass(frozen=True, slots=True)
class TerminatedTranscriptCheckpoint:
    installed: CheckpointTerminalOwner
    terminal_event: CheckpointNonSuccessTerminalEvent
    settlement_event: PhysicalOperationReservationSettledEvent
    barrier_release_event: CheckpointDispatchBarrierReleasedEvent
    stored_events: tuple[AgentEvent, ...]
    resulting_account_state: LedgerMaterializationAccountStateFact


def prepare_transcript_checkpoint_candidate(
    *,
    checkpoint_id: str,
    scope: TranscriptProjectionScopeFact,
    run_seed_semantic: RunTranscriptSeedSemanticFact,
    run_seed_reference: RunTranscriptSeedReferenceFact,
    materialization_consumer: LedgerMaterializationConsumerHorizonFact,
    account_state: LedgerMaterializationAccountStateFact,
    transcript_store: TranscriptProjectionStateStore,
    transcript_semantic_domain_contract_fingerprint: str,
    contracts: TranscriptProjectionMaterializationContracts,
    limits: AuthorityMaterializationLimits,
    previous_checkpoint_id: str | None = None,
    previously_reachable_artifact_ids: frozenset[str] = frozenset(),
) -> PreparedTranscriptCheckpoint:
    """Freeze a checkpoint without performing artifact or EventLog I/O."""

    live = transcript_store.snapshot()
    if not live.checkpointable:
        raise ValueError("transcript live assembly is not checkpointable")
    _validate_scope_and_consumer(
        scope=scope,
        consumer=materialization_consumer,
        account_state=account_state,
        live=live,
    )
    stable = live.stable_semantic_state
    entries = transcript_store.stable_entries()
    if len(entries) > limits.max_active_projection_entries:
        raise ValueError("transcript projection exceeds active entry limit")
    materialized = prepare_transcript_projection_materialization(
        runtime_session_id=scope.runtime_session_id,
        stable_entries=entries,
        normalized_transcript_fingerprint=stable.normalized_transcript_fingerprint,
        contracts=contracts,
        previously_reachable_artifact_ids=previously_reachable_artifact_ids,
    )
    artifact_bytes = sum(len(item.canonical_bytes) for item in materialized.artifacts)
    node_artifacts = tuple(
        item
        for item in materialized.artifacts
        if str(item.semantic_metadata.get("artifact_kind", "")).startswith(
            "transcript_projection_"
        )
    )
    leaf_artifacts = tuple(
        item
        for item in node_artifacts
        if item.semantic_metadata.get("artifact_kind")
        == "transcript_projection_leaf_node"
    )
    content_artifacts = tuple(
        item
        for item in materialized.artifacts
        if item.semantic_metadata.get("artifact_kind") == "normalized_message_content"
    )
    if len(leaf_artifacts) > limits.max_checkpoint_changed_leaves_per_operation:
        raise ValueError("checkpoint changed leaf count exceeds operation bound")
    if len(node_artifacts) > limits.max_checkpoint_changed_nodes_per_operation:
        raise ValueError("checkpoint changed artifact count exceeds operation bound")
    if (
        len(content_artifacts)
        > limits.max_changed_message_content_artifacts_per_operation
    ):
        raise ValueError("checkpoint changed message artifact count exceeds bound")
    if artifact_bytes > limits.max_checkpoint_total_artifact_bytes_per_operation:
        raise ValueError("checkpoint changed artifact bytes exceed operation bound")
    batches = (
        len(materialized.artifacts) + limits.max_checkpoint_nodes_per_artifact_batch - 1
    ) // limits.max_checkpoint_nodes_per_artifact_batch
    if batches > limits.max_checkpoint_artifact_batches_per_operation:
        raise ValueError("checkpoint artifact batches exceed operation bound")

    source = build_frozen_fact(
        TranscriptProjectionSemanticSourceFact,
        schema_version="transcript_projection_semantic_source.v1",
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            transcript_semantic_domain_contract_fingerprint
        ),
        semantic_source_event_count=stable.semantic_source_event_count,
        semantic_source_accumulator=stable.semantic_source_accumulator,
        resulting_state_fingerprint=stable.state_semantic_fingerprint,
    )
    root = materialized.root_manifest
    common = {
        "semantic_state_fingerprint": stable.state_semantic_fingerprint,
        "root_manifest_ref": materialized.root_reference,
        "tree_contract_fingerprint": contracts.tree.tree_contract_fingerprint,
        "total_entry_count": root.total_entry_count,
    }
    checkpoint_materialization: TranscriptProjectionCheckpointMaterializationFact
    if root.root_kind == "empty":
        checkpoint_materialization = build_frozen_fact(
            EmptyTranscriptProjectionCheckpointMaterializationFact,
            schema_version=(
                "empty_transcript_projection_checkpoint_materialization.v1"
            ),
            root_kind="empty",
            **common,
        )
    else:
        checkpoint_materialization = build_frozen_fact(
            NonEmptyTranscriptProjectionCheckpointMaterializationFact,
            schema_version=(
                "non_empty_transcript_projection_checkpoint_materialization.v1"
            ),
            root_kind="non_empty",
            tree_height=root.tree_height,
            **common,
        )
    candidate = build_frozen_fact(
        TranscriptProjectionCheckpointCandidateFact,
        schema_version="transcript_projection_checkpoint_candidate.v1",
        checkpoint_id=checkpoint_id,
        scope=scope,
        run_seed_semantic=run_seed_semantic,
        run_seed_reference=run_seed_reference,
        semantic_source=source,
        stable_semantic_state=stable,
        materialization=checkpoint_materialization,
        materialization_consumer_id=materialization_consumer.consumer_id,
        previous_checkpoint_id=previous_checkpoint_id,
        source_ledger_materialization_generation=(
            account_state.generation.ledger_materialization_generation
        ),
        source_consumer_horizon_revision=(
            account_state.generation.consumer_horizon_revision
        ),
        candidate_ledger_through_sequence=live.ledger_through_sequence,
        candidate_ledger_continuity_accumulator=(live.ledger_continuity_accumulator),
        build_contract_fingerprint=TRANSCRIPT_CHECKPOINT_BUILD_CONTRACT_FINGERPRINT,
    )
    return PreparedTranscriptCheckpoint(
        candidate=candidate,
        materialization=materialized,
        prepared_at_monotonic=monotonic(),
    )


def build_default_checkpoint_terminal_contract() -> CheckpointTerminalContractFact:
    sanitizer = build_frozen_fact(
        CheckpointDiagnosticSanitizationContractFact,
        schema_version="checkpoint_diagnostic_sanitization_contract.v2",
        contract_id="pulsara.checkpoint-diagnostic-sanitizer",
        contract_version="2",
        unicode_normalization="NFC",
        secret_key_normalization="casefold_strip_non_alnum",
        secret_key_tokens=(
            "api_key",
            "authorization",
            "cookie",
            "password",
            "secret",
            "token",
        ),
        secret_marker_tokens=(
            "bearer",
            "private_key",
            "secret",
            "token",
        ),
        max_token_characters=64,
        max_token_utf8_bytes=256,
        max_secret_key_tokens_total_utf8_bytes=4096,
        max_secret_marker_tokens_total_utf8_bytes=4096,
        max_all_secret_tokens_total_utf8_bytes=4096,
        url_userinfo_policy="remove",
        url_query_policy="remove",
        url_fragment_policy="remove",
        header_policy="remove_all",
        cookie_policy="remove_all",
        control_character_policy="replace_with_space",
        redaction_token="[redacted]",
        max_sanitization_passes=4,
        fixed_point_required=True,
        secret_safe_validation_required=True,
        max_output_characters=256,
        max_output_utf8_bytes=1024,
    )
    stage_map = {
        CheckpointFailureReasonCode.ARTIFACT_WRITE_FAILED: (
            "internal_node_write",
            "leaf_artifact_write",
            "message_artifact_write",
            "root_write",
        ),
        CheckpointFailureReasonCode.ARTIFACT_CONFIRMATION_FAILED: (
            "internal_node_confirmation",
            "leaf_artifact_confirmation",
            "message_artifact_confirmation",
            "root_confirmation",
        ),
        CheckpointFailureReasonCode.OPERATION_BOUND_EXCEEDED: (
            "checkpoint_precommit_validation",
        ),
        CheckpointFailureReasonCode.PRECOMMIT_CONTRACT_MISMATCH: (
            "checkpoint_precommit_validation",
        ),
    }
    diagnostic_map = {
        CheckpointFailureReasonCode.ARTIFACT_WRITE_FAILED: (
            CheckpointTerminalDiagnosticCode.ARTIFACT_IO_FAILURE,
        ),
        CheckpointFailureReasonCode.ARTIFACT_CONFIRMATION_FAILED: (
            CheckpointTerminalDiagnosticCode.ARTIFACT_CONFIRMATION_MISMATCH,
        ),
        CheckpointFailureReasonCode.OPERATION_BOUND_EXCEEDED: (
            CheckpointTerminalDiagnosticCode.OPERATION_BOUND_VIOLATION,
        ),
        CheckpointFailureReasonCode.PRECOMMIT_CONTRACT_MISMATCH: (
            CheckpointTerminalDiagnosticCode.PRECOMMIT_VALIDATION_MISMATCH,
        ),
    }
    failure_rules = tuple(
        build_frozen_fact(
            CheckpointFailureReasonStageRuleFact,
            schema_version="checkpoint_failure_reason_stage_rule.v1",
            reason_code=reason,
            allowed_failure_stages=tuple(sorted(stage_map[reason])),
            allowed_diagnostic_codes=tuple(
                sorted(diagnostic_map[reason], key=lambda item: item.value)
            ),
        )
        for reason in sorted(CheckpointFailureReasonCode, key=lambda item: item.value)
    )
    cancellation_reason = {
        "host_close": CheckpointCancellationReasonCode.HOST_CLOSE,
        "operation_deadline": CheckpointCancellationReasonCode.OPERATION_DEADLINE,
        "session_shutdown": CheckpointCancellationReasonCode.SESSION_SHUTDOWN,
        "user_stop": CheckpointCancellationReasonCode.USER_STOP,
    }
    cancellation_rules = tuple(
        build_frozen_fact(
            CheckpointCancellationReasonRuleFact,
            schema_version="checkpoint_cancellation_reason_rule.v1",
            cancellation_source=source,
            reason_code=reason,
        )
        for source, reason in sorted(cancellation_reason.items())
    )
    return build_frozen_fact(
        CheckpointTerminalContractFact,
        schema_version="checkpoint_terminal_contract.v1",
        contract_id="pulsara.transcript-checkpoint-terminal",
        contract_version="1",
        failure_rules=failure_rules,
        cancellation_rules=cancellation_rules,
        max_diagnostics=8,
        max_diagnostic_characters=256,
        max_diagnostic_utf8_bytes=1024,
        diagnostic_sanitization_contract=sanitizer,
    )


def install_checkpoint_barrier(
    *,
    coordinator: LedgerMaterializationCoordinator,
    context: EventContext,
    prepared: PreparedTranscriptCheckpoint,
    checkpoint_burst_contract: PhysicalBurstContractFact,
    terminal_contract: CheckpointTerminalContractFact,
    deadline_monotonic: float | None = None,
) -> InstalledTranscriptCheckpoint:
    """Atomically install intent, maintenance reservation, and producer barrier."""

    source = coordinator.store.snapshot()
    if source is None:
        raise ValueError("checkpoint barrier requires a bootstrapped account")
    candidate = prepared.candidate
    if (
        candidate.source_ledger_materialization_generation
        != source.generation.ledger_materialization_generation
        or candidate.source_consumer_horizon_revision
        != source.generation.consumer_horizon_revision
        or candidate.candidate_ledger_through_sequence != source.ledger_through_sequence
        or source.active_checkpoint_barrier is not None
        or source.active_reservations
    ):
        raise ValueError("checkpoint barrier source is stale or not drained")
    reservation_id = f"checkpoint_reservation:{candidate.checkpoint_id}"
    intent = coordinator._prepare_event(  # noqa: SLF001 - same subsystem owner
        TranscriptProjectionCheckpointIntentEvent(
            id=f"checkpoint_intent:{candidate.checkpoint_id}",
            **context.event_fields(),
            checkpoint_id=candidate.checkpoint_id,
            checkpoint_candidate_fingerprint=candidate.candidate_fingerprint,
            scope=candidate.scope,
            source_ledger_materialization_generation=(
                candidate.source_ledger_materialization_generation
            ),
            source_consumer_horizon_revision=(
                candidate.source_consumer_horizon_revision
            ),
            frozen_ledger_through_sequence=(
                candidate.candidate_ledger_through_sequence
            ),
            frozen_ledger_continuity_accumulator=(
                candidate.candidate_ledger_continuity_accumulator
            ),
            maintenance_reservation_id=reservation_id,
            intent_contract_fingerprint=context_fingerprint(
                "transcript-checkpoint-intent-contract:v1",
                terminal_contract.contract_fingerprint,
            ),
            terminal_contract=terminal_contract,
        )
    )
    intent_identity = stable_event_identity(
        intent, runtime_session_id=coordinator.runtime_session_id
    )
    initial_types = (
        "TRANSCRIPT_PROJECTION_CHECKPOINT_INTENT",
        "PHYSICAL_OPERATION_RESERVATION_CREATED",
        "CHECKPOINT_DISPATCH_BARRIER_INSTALLED",
    )
    initial_events = len(initial_types)
    initial_bytes = sum(
        deterministic_bookkeeping_charge(
            event_type, contract=coordinator.charge_contract
        ).charged_payload_bytes
        for event_type in initial_types
    )
    if (
        checkpoint_burst_contract.max_total_reserved_events < initial_events
        or checkpoint_burst_contract.max_total_reserved_payload_bytes < initial_bytes
    ):
        raise ValueError("checkpoint burst cannot cover barrier install batch")
    reservation = build_frozen_fact(
        PhysicalOperationReservationFact,
        schema_version="physical_operation_reservation.v2",
        reservation_id=reservation_id,
        runtime_session_id=coordinator.runtime_session_id,
        business_run_id=candidate.scope.run_id,
        business_window_id=candidate.scope.window_id,
        business_window_generation=candidate.scope.window_generation,
        owner_kind=checkpoint_burst_contract.operation_kind,
        owner_id=candidate.checkpoint_id,
        ledger_materialization_generation=(
            source.generation.ledger_materialization_generation
        ),
        consumer_horizon_revision=source.generation.consumer_horizon_revision,
        source_ledger_through_sequence=source.ledger_through_sequence,
        burst_contract_id=checkpoint_burst_contract.contract_id,
        burst_contract_version=checkpoint_burst_contract.contract_version,
        burst_contract_fingerprint=checkpoint_burst_contract.contract_fingerprint,
        physical_charge_contract_fingerprint=(
            coordinator.charge_contract.contract_fingerprint
        ),
        reserved_events=checkpoint_burst_contract.max_total_reserved_events,
        reserved_payload_bytes=(
            checkpoint_burst_contract.max_total_reserved_payload_bytes
        ),
        terminal_tail_reserved_events=(
            checkpoint_burst_contract.terminal_tail_reserved_events
        ),
        terminal_tail_reserved_payload_bytes=(
            checkpoint_burst_contract.terminal_tail_reserved_payload_bytes
        ),
    )
    barrier_id = f"checkpoint_barrier:{candidate.checkpoint_id}"
    barrier = build_frozen_fact(
        CheckpointDispatchBarrierFact,
        schema_version="checkpoint_dispatch_barrier.v2",
        barrier_id=barrier_id,
        runtime_session_id=coordinator.runtime_session_id,
        materialization_consumer_id=candidate.materialization_consumer_id,
        checkpoint_id=candidate.checkpoint_id,
        checkpoint_candidate_fingerprint=candidate.candidate_fingerprint,
        checkpoint_intent_event_identity=intent_identity,
        source_ledger_materialization_generation=(
            source.generation.ledger_materialization_generation
        ),
        source_consumer_horizon_revision=(source.generation.consumer_horizon_revision),
        frozen_ledger_through_sequence=source.ledger_through_sequence,
        frozen_ledger_continuity_accumulator=(
            candidate.candidate_ledger_continuity_accumulator
        ),
        maintenance_reservation_id=reservation_id,
        admitted_producer_generation=(
            source.generation.ledger_materialization_generation
        ),
        allowed_control_write_contract_fingerprint=context_fingerprint(
            "checkpoint-barrier-control-writes:v1",
            (
                "checkpoint_terminal",
                "horizon_advance",
                "generation_advance",
                "settlement",
                "barrier_release",
            ),
        ),
    )
    reservation_event_id = f"physical_reservation:{reservation_id}"
    barrier_event_id = f"checkpoint_barrier_installed:{candidate.checkpoint_id}"
    active = build_frozen_fact(
        ActivePhysicalReservationStateFact,
        schema_version="active_physical_reservation_state.v1",
        reservation_id=reservation_id,
        owner_kind=reservation.owner_kind,
        owner_id=candidate.checkpoint_id,
        lifecycle_status="active",
        reservation_fingerprint=reservation.reservation_fingerprint,
        suspension_fingerprint=None,
        reserved_events_total=reservation.reserved_events,
        reserved_payload_bytes_total=reservation.reserved_payload_bytes,
        charged_candidate_events_lifetime=0,
        charged_candidate_payload_bytes_lifetime=0,
        charged_wrapper_bytes_lifetime=0,
        charged_bookkeeping_events_lifetime=initial_events,
        charged_bookkeeping_bytes_lifetime=initial_bytes,
        charged_events_lifetime=initial_events,
        charged_payload_bytes_lifetime=initial_bytes,
        remaining_events=reservation.reserved_events - initial_events,
        remaining_payload_bytes=reservation.reserved_payload_bytes - initial_bytes,
        latest_reservation_event_id=reservation_event_id,
        latest_lifecycle_event_id=barrier_event_id,
        latest_charge_applied_event_id=None,
    )
    resulting = build_account_state(
        runtime_session_id=coordinator.runtime_session_id,
        generation=source.generation,
        ledger_through_sequence=source.ledger_through_sequence + initial_events,
        ledger_charged_payload_bytes_through=(
            source.ledger_charged_payload_bytes_through + initial_bytes
        ),
        active_reservations=(*source.active_reservations, active),
        active_checkpoint_barrier=barrier,
        latest_transition_event_ids=(
            reservation_event_id,
            barrier_event_id,
        ),
        reconciliation_required=False,
        reconciliation_reason_code=None,
    )
    cause = build_frozen_fact(
        LedgerMaterializationTransitionCauseIdentityFact,
        schema_version="ledger_materialization_transition_cause_identity.v1",
        cause_role="checkpoint_intent",
        event_identity=intent_identity,
    )
    transition = _checkpoint_transition(source, resulting, (cause,))
    reservation_event = coordinator._prepare_event(  # noqa: SLF001
        PhysicalOperationReservationCreatedEvent(
            id=reservation_event_id,
            **context.event_fields(),
            reservation=reservation,
            transition=transition,
            resulting_account_state_fingerprint=resulting.account_state_fingerprint,
        )
    )
    barrier_event = coordinator._prepare_event(  # noqa: SLF001
        CheckpointDispatchBarrierInstalledEvent(
            id=barrier_event_id,
            **context.event_fields(),
            barrier=barrier,
            transition=transition,
            resulting_account_state_fingerprint=resulting.account_state_fingerprint,
        )
    )
    stored = coordinator.commit_transition_batch(
        source=source,
        events=(intent, reservation_event, barrier_event),
        resulting=resulting,
        deadline_monotonic=deadline_monotonic,
    )
    return InstalledTranscriptCheckpoint(
        prepared=prepared,
        intent_event=_canonical_stored_event(
            stored, intent, TranscriptProjectionCheckpointIntentEvent
        ),
        reservation=reservation,
        reservation_event=_canonical_stored_event(
            stored, reservation_event, PhysicalOperationReservationCreatedEvent
        ),
        barrier=barrier,
        barrier_event=_canonical_stored_event(
            stored, barrier_event, CheckpointDispatchBarrierInstalledEvent
        ),
        stored_events=stored,
        resulting_account_state=resulting,
    )


def commit_checkpoint_success(
    *,
    coordinator: LedgerMaterializationCoordinator,
    context: EventContext,
    installed: InstalledTranscriptCheckpoint,
    terminal_contract: CheckpointTerminalContractFact,
    deadline_monotonic: float | None = None,
) -> CommittedTranscriptCheckpoint:
    """Commit checkpoint, horizon, settlement, and barrier release as one CAS."""

    source = coordinator.store.snapshot()
    if source is None or source.account_state_fingerprint != (
        installed.resulting_account_state.account_state_fingerprint
    ):
        raise ValueError("checkpoint terminal source account drifted")
    if source.active_checkpoint_barrier != installed.barrier:
        raise ValueError("checkpoint terminal lost its exact barrier")
    active = next(
        (
            item
            for item in source.active_reservations
            if item.reservation_id == installed.reservation.reservation_id
        ),
        None,
    )
    if active is None:
        raise ValueError("checkpoint terminal lost its maintenance reservation")
    candidate = installed.prepared.candidate
    committed_event = coordinator._prepare_event(  # noqa: SLF001
        TranscriptProjectionCheckpointCommittedEvent(
            id=f"checkpoint_committed:{candidate.checkpoint_id}",
            **context.event_fields(),
            created_at=installed.intent_event.created_at,
            checkpoint_id=candidate.checkpoint_id,
            checkpoint_candidate_fingerprint=candidate.candidate_fingerprint,
            checkpoint_intent_event_identity=stable_event_identity(
                installed.intent_event,
                runtime_session_id=coordinator.runtime_session_id,
            ),
            barrier_installed_event_identity=stable_event_identity(
                installed.barrier_event,
                runtime_session_id=coordinator.runtime_session_id,
            ),
            checkpoint=candidate,
            terminal_contract_id=terminal_contract.contract_id,
            terminal_contract_version=terminal_contract.contract_version,
            terminal_contract_fingerprint=terminal_contract.contract_fingerprint,
        )
    )
    committed_identity = stable_event_identity(
        committed_event, runtime_session_id=coordinator.runtime_session_id
    )
    old_horizon = next(
        (
            item
            for item in source.generation.consumer_horizons
            if item.consumer_id == candidate.materialization_consumer_id
        ),
        None,
    )
    if old_horizon is None:
        raise ValueError("checkpoint consumer is no longer active")
    if candidate.candidate_ledger_through_sequence <= old_horizon.through_sequence:
        raise ValueError("checkpoint does not advance its consumer horizon")
    install_bytes = sum(
        deterministic_bookkeeping_charge(
            event_type, contract=coordinator.charge_contract
        ).charged_payload_bytes
        for event_type in (
            "TRANSCRIPT_PROJECTION_CHECKPOINT_INTENT",
            "PHYSICAL_OPERATION_RESERVATION_CREATED",
            "CHECKPOINT_DISPATCH_BARRIER_INSTALLED",
        )
    )
    new_horizon = build_frozen_fact(
        LedgerMaterializationConsumerHorizonFact,
        schema_version="ledger_materialization_consumer_horizon.v1",
        runtime_session_id=old_horizon.runtime_session_id,
        consumer_kind=old_horizon.consumer_kind,
        consumer_id=old_horizon.consumer_id,
        business_run_id=old_horizon.business_run_id,
        business_window_id=old_horizon.business_window_id,
        business_window_generation=old_horizon.business_window_generation,
        through_sequence=candidate.candidate_ledger_through_sequence,
        ledger_event_count_through=candidate.candidate_ledger_through_sequence,
        ledger_charged_payload_bytes_through=(
            source.ledger_charged_payload_bytes_through - install_bytes
        ),
        ledger_continuity_accumulator=(
            candidate.candidate_ledger_continuity_accumulator
        ),
        consumer_contract_fingerprint=old_horizon.consumer_contract_fingerprint,
    )
    horizons = tuple(
        new_horizon if item.consumer_id == old_horizon.consumer_id else item
        for item in source.generation.consumer_horizons
    )
    provisional_generation = build_generation(
        source=source.generation,
        consumer_horizons=horizons,
        consumer_horizon_revision=(source.generation.consumer_horizon_revision + 1),
    )
    minimum_advanced = (
        provisional_generation.reclaimable_through_sequence
        > source.generation.reclaimable_through_sequence
    )
    resulting_generation = (
        build_generation(
            source=source.generation,
            consumer_horizons=horizons,
            materialization_generation=(
                source.generation.ledger_materialization_generation + 1
            ),
            consumer_horizon_revision=(source.generation.consumer_horizon_revision + 1),
        )
        if minimum_advanced
        else provisional_generation
    )
    terminal_types = [
        "TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED",
        "LEDGER_MATERIALIZATION_CONSUMER_HORIZON_ADVANCED",
        "PHYSICAL_OPERATION_RESERVATION_SETTLED",
        "CHECKPOINT_DISPATCH_BARRIER_RELEASED",
    ]
    if minimum_advanced:
        terminal_types.insert(2, "LEDGER_MATERIALIZATION_GENERATION_ADVANCED")
    terminal_charge_bytes = sum(
        deterministic_bookkeeping_charge(
            event_type, contract=coordinator.charge_contract
        ).charged_payload_bytes
        for event_type in terminal_types
    )
    terminal_charge_events = len(terminal_types)
    settlement_charge = deterministic_bookkeeping_charge(
        "PHYSICAL_OPERATION_RESERVATION_SETTLED",
        contract=coordinator.charge_contract,
    )
    before_settlement_events = terminal_charge_events - 1
    before_settlement_bytes = (
        terminal_charge_bytes - settlement_charge.charged_payload_bytes
    )
    if (
        terminal_charge_events > active.remaining_events
        or terminal_charge_bytes > active.remaining_payload_bytes
    ):
        raise ValueError("checkpoint terminal exceeds its maintenance reserve")
    horizon_event_id = f"ledger_consumer_horizon:{candidate.checkpoint_id}"
    generation_event_id = f"ledger_generation:{candidate.checkpoint_id}"
    settlement_event_id = f"physical_settlement:{installed.reservation.reservation_id}"
    release_event_id = f"checkpoint_barrier_released:{candidate.checkpoint_id}"
    transition_ids = [horizon_event_id, settlement_event_id, release_event_id]
    if minimum_advanced:
        transition_ids.append(generation_event_id)
    resulting = build_account_state(
        runtime_session_id=coordinator.runtime_session_id,
        generation=resulting_generation,
        ledger_through_sequence=(
            source.ledger_through_sequence + terminal_charge_events
        ),
        ledger_charged_payload_bytes_through=(
            source.ledger_charged_payload_bytes_through + terminal_charge_bytes
        ),
        active_reservations=tuple(
            item
            for item in source.active_reservations
            if item.reservation_id != installed.reservation.reservation_id
        ),
        active_checkpoint_barrier=None,
        latest_transition_event_ids=transition_ids,
        reconciliation_required=False,
        reconciliation_reason_code=None,
    )
    transition_cause = build_frozen_fact(
        LedgerMaterializationTransitionCauseIdentityFact,
        schema_version="ledger_materialization_transition_cause_identity.v1",
        cause_role="checkpoint_committed",
        event_identity=committed_identity,
    )
    transition = _checkpoint_transition(source, resulting, (transition_cause,))
    checkpoint_cause = build_frozen_fact(
        CheckpointConsumerCauseFact,
        schema_version="checkpoint_consumer_cause.v1",
        cause_kind="checkpoint",
        checkpoint_id=candidate.checkpoint_id,
        checkpoint_committed_event_identity=committed_identity,
        checkpoint_candidate_fingerprint=candidate.candidate_fingerprint,
    )
    horizon_event = coordinator._prepare_event(  # noqa: SLF001
        LedgerMaterializationConsumerHorizonAdvancedEvent(
            id=horizon_event_id,
            **context.event_fields(),
            created_at=installed.intent_event.created_at,
            previous_horizon=old_horizon,
            resulting_horizon=new_horizon,
            cause=checkpoint_cause,
            transition=transition,
            resulting_account_state_fingerprint=resulting.account_state_fingerprint,
        )
    )
    generation_event = (
        coordinator._prepare_event(  # noqa: SLF001
            LedgerMaterializationGenerationAdvancedEvent(
                id=generation_event_id,
                **context.event_fields(),
                created_at=installed.intent_event.created_at,
                previous_generation=source.generation,
                resulting_generation=resulting_generation,
                transition=transition,
                resulting_account_state_fingerprint=(
                    resulting.account_state_fingerprint
                ),
            )
        )
        if minimum_advanced
        else None
    )
    total_lifetime_events = active.charged_events_lifetime + terminal_charge_events
    total_lifetime_bytes = active.charged_payload_bytes_lifetime + terminal_charge_bytes
    settlement = build_frozen_fact(
        PhysicalOperationSettlementFact,
        schema_version="physical_operation_settlement.v2",
        reservation_id=installed.reservation.reservation_id,
        runtime_session_id=coordinator.runtime_session_id,
        business_run_id=installed.reservation.business_run_id,
        business_window_id=installed.reservation.business_window_id,
        business_window_generation=installed.reservation.business_window_generation,
        ledger_materialization_generation=(
            source.generation.ledger_materialization_generation
        ),
        consumer_horizon_revision=source.generation.consumer_horizon_revision,
        owner_kind=installed.reservation.owner_kind,
        owner_id=installed.reservation.owner_id,
        reservation_fingerprint=installed.reservation.reservation_fingerprint,
        predecessor_status="active",
        predecessor_lifecycle_event_id=active.latest_lifecycle_event_id,
        predecessor_reservation_state_fingerprint=active.state_fingerprint,
        burst_contract_fingerprint=installed.reservation.burst_contract_fingerprint,
        physical_charge_contract_fingerprint=(
            coordinator.charge_contract.contract_fingerprint
        ),
        predecessor_remaining_events=active.remaining_events,
        predecessor_remaining_payload_bytes=active.remaining_payload_bytes,
        terminal_batch_charge_before_settlement_events=before_settlement_events,
        terminal_batch_charge_before_settlement_payload_bytes=(before_settlement_bytes),
        settlement_event_charge_events=settlement_charge.event_count,
        settlement_event_charge_payload_bytes=(settlement_charge.charged_payload_bytes),
        charged_candidate_events=0,
        charged_candidate_payload_bytes=0,
        charged_wrapper_bytes=0,
        charged_bookkeeping_events=total_lifetime_events,
        charged_bookkeeping_bytes=total_lifetime_bytes,
        total_charged_events=total_lifetime_events,
        total_charged_payload_bytes=total_lifetime_bytes,
        terminal_outcome="completed",
        released_on_suspension_events_lifetime=0,
        released_on_suspension_payload_bytes_lifetime=0,
        released_on_settlement_events=(
            active.remaining_events
            - before_settlement_events
            - settlement_charge.event_count
        ),
        released_on_settlement_payload_bytes=(
            active.remaining_payload_bytes
            - before_settlement_bytes
            - settlement_charge.charged_payload_bytes
        ),
        resulting_reservation_state_fingerprint=context_fingerprint(
            "settled-physical-reservation-state:v1",
            installed.reservation.reservation_fingerprint,
        ),
    )
    settlement_event = coordinator._prepare_event(  # noqa: SLF001
        PhysicalOperationReservationSettledEvent(
            id=settlement_event_id,
            **context.event_fields(),
            created_at=installed.intent_event.created_at,
            settlement=settlement,
            transition=transition,
            resulting_account_state_fingerprint=resulting.account_state_fingerprint,
        )
    )
    terminal_reference = build_frozen_fact(
        CheckpointCommittedTerminalReferenceFact,
        schema_version="checkpoint_committed_terminal_ref.v1",
        release_outcome="checkpoint_committed",
        checkpoint_committed_event_identity=committed_identity,
        consumer_horizon_advanced_event_identity=stable_event_identity(
            horizon_event, runtime_session_id=coordinator.runtime_session_id
        ),
        generation_advanced_event_identity=(
            stable_event_identity(
                generation_event, runtime_session_id=coordinator.runtime_session_id
            )
            if generation_event is not None
            else None
        ),
    )
    release = build_frozen_fact(
        CheckpointDispatchBarrierReleaseFact,
        schema_version="checkpoint_dispatch_barrier_release.v1",
        barrier_id=installed.barrier.barrier_id,
        checkpoint_id=candidate.checkpoint_id,
        checkpoint_candidate_fingerprint=candidate.candidate_fingerprint,
        terminal_reference=terminal_reference,
        maintenance_settlement_event_identity=stable_event_identity(
            settlement_event, runtime_session_id=coordinator.runtime_session_id
        ),
    )
    release_event = coordinator._prepare_event(  # noqa: SLF001
        CheckpointDispatchBarrierReleasedEvent(
            id=release_event_id,
            **context.event_fields(),
            created_at=installed.intent_event.created_at,
            release=release,
            transition=transition,
            resulting_account_state_fingerprint=resulting.account_state_fingerprint,
        )
    )
    events: tuple[AgentEvent, ...] = (
        committed_event,
        horizon_event,
        *((generation_event,) if generation_event is not None else ()),
        settlement_event,
        release_event,
    )
    stored = coordinator.commit_transition_batch(
        source=source,
        events=events,
        resulting=resulting,
        deadline_monotonic=deadline_monotonic,
    )
    return CommittedTranscriptCheckpoint(
        installed=installed,
        committed_event=_canonical_stored_event(
            stored, committed_event, TranscriptProjectionCheckpointCommittedEvent
        ),
        horizon_event=_canonical_stored_event(
            stored, horizon_event, LedgerMaterializationConsumerHorizonAdvancedEvent
        ),
        generation_event=(
            _canonical_stored_event(
                stored,
                generation_event,
                LedgerMaterializationGenerationAdvancedEvent,
            )
            if generation_event is not None
            else None
        ),
        settlement_event=_canonical_stored_event(
            stored, settlement_event, PhysicalOperationReservationSettledEvent
        ),
        barrier_release_event=_canonical_stored_event(
            stored, release_event, CheckpointDispatchBarrierReleasedEvent
        ),
        stored_events=stored,
        resulting_account_state=resulting,
    )


def commit_checkpoint_failure(
    *,
    coordinator: LedgerMaterializationCoordinator,
    context: EventContext,
    installed: CheckpointTerminalOwner,
    terminal_contract: CheckpointTerminalContractFact,
    reason_code: CheckpointFailureReasonCode,
    diagnostics: Sequence[CheckpointTerminalDiagnosticFact] = (),
    deadline_monotonic: float | None = None,
) -> TerminatedTranscriptCheckpoint:
    """Commit a typed checkpoint failure and release its exact barrier owner."""

    _validate_failure_terminal(
        terminal_contract=terminal_contract,
        reason_code=reason_code,
        diagnostics=diagnostics,
    )
    terminal_event = coordinator._prepare_event(  # noqa: SLF001
        TranscriptProjectionCheckpointFailedEvent(
            id=f"checkpoint_failed:{installed.checkpoint_id}",
            **context.event_fields(),
            created_at=installed.intent_event.created_at,
            checkpoint_id=installed.checkpoint_id,
            checkpoint_candidate_fingerprint=(
                installed.checkpoint_candidate_fingerprint
            ),
            checkpoint_intent_event_identity=stable_event_identity(
                installed.intent_event,
                runtime_session_id=coordinator.runtime_session_id,
            ),
            barrier_installed_event_identity=stable_event_identity(
                installed.barrier_event,
                runtime_session_id=coordinator.runtime_session_id,
            ),
            terminal_contract_id=terminal_contract.contract_id,
            terminal_contract_version=terminal_contract.contract_version,
            terminal_contract_fingerprint=terminal_contract.contract_fingerprint,
            stable_reason_code=reason_code,
            diagnostics=tuple(diagnostics),
        )
    )
    terminal_reference = build_frozen_fact(
        CheckpointFailedTerminalReferenceFact,
        schema_version="checkpoint_failed_terminal_ref.v1",
        release_outcome="checkpoint_failed",
        checkpoint_failed_event_identity=stable_event_identity(
            terminal_event, runtime_session_id=coordinator.runtime_session_id
        ),
    )
    return _commit_checkpoint_non_success(
        coordinator=coordinator,
        context=context,
        installed=installed,
        terminal_event=terminal_event,
        terminal_reference=terminal_reference,
        terminal_outcome="runtime_error",
        deadline_monotonic=deadline_monotonic,
    )


def commit_checkpoint_cancellation(
    *,
    coordinator: LedgerMaterializationCoordinator,
    context: EventContext,
    installed: CheckpointTerminalOwner,
    terminal_contract: CheckpointTerminalContractFact,
    cancellation_source: CheckpointCancellationSource,
    reason_code: CheckpointCancellationReasonCode,
    deadline_monotonic: float | None = None,
) -> TerminatedTranscriptCheckpoint:
    """Commit a typed checkpoint cancellation and release its exact barrier owner."""

    matching = tuple(
        item
        for item in terminal_contract.cancellation_rules
        if item.cancellation_source == cancellation_source
    )
    if len(matching) != 1 or matching[0].reason_code is not reason_code:
        raise ValueError("checkpoint cancellation source/reason contract mismatch")
    terminal_event = coordinator._prepare_event(  # noqa: SLF001
        TranscriptProjectionCheckpointCancelledEvent(
            id=f"checkpoint_cancelled:{installed.checkpoint_id}",
            **context.event_fields(),
            created_at=installed.intent_event.created_at,
            checkpoint_id=installed.checkpoint_id,
            checkpoint_candidate_fingerprint=(
                installed.checkpoint_candidate_fingerprint
            ),
            checkpoint_intent_event_identity=stable_event_identity(
                installed.intent_event,
                runtime_session_id=coordinator.runtime_session_id,
            ),
            barrier_installed_event_identity=stable_event_identity(
                installed.barrier_event,
                runtime_session_id=coordinator.runtime_session_id,
            ),
            terminal_contract_id=terminal_contract.contract_id,
            terminal_contract_version=terminal_contract.contract_version,
            terminal_contract_fingerprint=terminal_contract.contract_fingerprint,
            cancellation_source=cancellation_source,
            stable_reason_code=reason_code,
        )
    )
    terminal_reference = build_frozen_fact(
        CheckpointCancelledTerminalReferenceFact,
        schema_version="checkpoint_cancelled_terminal_ref.v1",
        release_outcome="checkpoint_cancelled",
        checkpoint_cancelled_event_identity=stable_event_identity(
            terminal_event, runtime_session_id=coordinator.runtime_session_id
        ),
    )
    return _commit_checkpoint_non_success(
        coordinator=coordinator,
        context=context,
        installed=installed,
        terminal_event=terminal_event,
        terminal_reference=terminal_reference,
        terminal_outcome="cancelled",
        deadline_monotonic=deadline_monotonic,
    )


def commit_checkpoint_recovered_interrupted(
    *,
    coordinator: LedgerMaterializationCoordinator,
    context: EventContext,
    installed: CheckpointTerminalOwner,
    terminal_contract: CheckpointTerminalContractFact,
    reopen_ledger_high_water: int,
    deadline_monotonic: float | None = None,
) -> TerminatedTranscriptCheckpoint:
    """Repair Start-without-terminal using the stable recovered winner."""

    source = coordinator.store.snapshot()
    if source is None or reopen_ledger_high_water != source.ledger_through_sequence:
        raise ValueError("checkpoint recovery high-water drifted")
    terminal_event = coordinator._prepare_event(  # noqa: SLF001
        TranscriptProjectionCheckpointRecoveredInterruptedEvent(
            id=f"checkpoint_recovered_interrupted:{installed.checkpoint_id}",
            **context.event_fields(),
            created_at=installed.intent_event.created_at,
            checkpoint_id=installed.checkpoint_id,
            checkpoint_candidate_fingerprint=(
                installed.checkpoint_candidate_fingerprint
            ),
            checkpoint_intent_event_identity=stable_event_identity(
                installed.intent_event,
                runtime_session_id=coordinator.runtime_session_id,
            ),
            barrier_installed_event_identity=stable_event_identity(
                installed.barrier_event,
                runtime_session_id=coordinator.runtime_session_id,
            ),
            terminal_contract_id=terminal_contract.contract_id,
            terminal_contract_version=terminal_contract.contract_version,
            terminal_contract_fingerprint=terminal_contract.contract_fingerprint,
            reopen_ledger_high_water=reopen_ledger_high_water,
            stable_reason_code="checkpoint_recovered_interrupted",
        )
    )
    terminal_reference = build_frozen_fact(
        CheckpointRecoveredInterruptedTerminalReferenceFact,
        schema_version="checkpoint_recovered_interrupted_terminal_ref.v1",
        release_outcome="recovered_interrupted",
        checkpoint_recovered_interrupted_event_identity=stable_event_identity(
            terminal_event, runtime_session_id=coordinator.runtime_session_id
        ),
    )
    return _commit_checkpoint_non_success(
        coordinator=coordinator,
        context=context,
        installed=installed,
        terminal_event=terminal_event,
        terminal_reference=terminal_reference,
        terminal_outcome="recovered_interrupted",
        deadline_monotonic=deadline_monotonic,
    )


def _commit_checkpoint_non_success(
    *,
    coordinator: LedgerMaterializationCoordinator,
    context: EventContext,
    installed: CheckpointTerminalOwner,
    terminal_event: CheckpointNonSuccessTerminalEvent,
    terminal_reference: (
        CheckpointFailedTerminalReferenceFact
        | CheckpointCancelledTerminalReferenceFact
        | CheckpointRecoveredInterruptedTerminalReferenceFact
    ),
    terminal_outcome: str,
    deadline_monotonic: float | None,
) -> TerminatedTranscriptCheckpoint:
    source = coordinator.store.snapshot()
    if source is None or source.account_state_fingerprint != (
        installed.resulting_account_state.account_state_fingerprint
    ):
        raise ValueError("checkpoint terminal source account drifted")
    if source.active_checkpoint_barrier != installed.barrier:
        raise ValueError("checkpoint terminal lost its exact barrier")
    active = next(
        (
            item
            for item in source.active_reservations
            if item.reservation_id == installed.reservation.reservation_id
        ),
        None,
    )
    if active is None:
        raise ValueError("checkpoint terminal lost its maintenance reservation")
    terminal_type = str(terminal_event.type)
    terminal_types = (
        terminal_type,
        "PHYSICAL_OPERATION_RESERVATION_SETTLED",
        "CHECKPOINT_DISPATCH_BARRIER_RELEASED",
    )
    charges = tuple(
        deterministic_bookkeeping_charge(
            event_type, contract=coordinator.charge_contract
        )
        for event_type in terminal_types
    )
    total_events = sum(item.event_count for item in charges)
    total_bytes = sum(item.charged_payload_bytes for item in charges)
    if (
        total_events > active.remaining_events
        or total_bytes > active.remaining_payload_bytes
    ):
        raise ValueError("checkpoint terminal exceeds its maintenance reserve")
    settlement_charge = charges[1]
    before_settlement_events = charges[0].event_count + charges[2].event_count
    before_settlement_bytes = (
        charges[0].charged_payload_bytes + charges[2].charged_payload_bytes
    )
    settlement_event_id = f"physical_settlement:{installed.reservation.reservation_id}"
    release_event_id = f"checkpoint_barrier_released:{installed.checkpoint_id}"
    resulting = build_account_state(
        runtime_session_id=coordinator.runtime_session_id,
        generation=source.generation,
        ledger_through_sequence=source.ledger_through_sequence + total_events,
        ledger_charged_payload_bytes_through=(
            source.ledger_charged_payload_bytes_through + total_bytes
        ),
        active_reservations=tuple(
            item
            for item in source.active_reservations
            if item.reservation_id != installed.reservation.reservation_id
        ),
        active_checkpoint_barrier=None,
        latest_transition_event_ids=(
            settlement_event_id,
            release_event_id,
        ),
        reconciliation_required=False,
        reconciliation_reason_code=None,
    )
    terminal_identity = stable_event_identity(
        terminal_event, runtime_session_id=coordinator.runtime_session_id
    )
    transition_cause = build_frozen_fact(
        LedgerMaterializationTransitionCauseIdentityFact,
        schema_version="ledger_materialization_transition_cause_identity.v1",
        cause_role="checkpoint_terminal",
        event_identity=terminal_identity,
    )
    transition = _checkpoint_transition(source, resulting, (transition_cause,))
    total_lifetime_events = active.charged_events_lifetime + total_events
    total_lifetime_bytes = active.charged_payload_bytes_lifetime + total_bytes
    settlement = build_frozen_fact(
        PhysicalOperationSettlementFact,
        schema_version="physical_operation_settlement.v2",
        reservation_id=installed.reservation.reservation_id,
        runtime_session_id=coordinator.runtime_session_id,
        business_run_id=installed.reservation.business_run_id,
        business_window_id=installed.reservation.business_window_id,
        business_window_generation=installed.reservation.business_window_generation,
        ledger_materialization_generation=(
            source.generation.ledger_materialization_generation
        ),
        consumer_horizon_revision=source.generation.consumer_horizon_revision,
        owner_kind=installed.reservation.owner_kind,
        owner_id=installed.reservation.owner_id,
        reservation_fingerprint=installed.reservation.reservation_fingerprint,
        predecessor_status="active",
        predecessor_lifecycle_event_id=active.latest_lifecycle_event_id,
        predecessor_reservation_state_fingerprint=active.state_fingerprint,
        burst_contract_fingerprint=installed.reservation.burst_contract_fingerprint,
        physical_charge_contract_fingerprint=(
            coordinator.charge_contract.contract_fingerprint
        ),
        predecessor_remaining_events=active.remaining_events,
        predecessor_remaining_payload_bytes=active.remaining_payload_bytes,
        terminal_batch_charge_before_settlement_events=before_settlement_events,
        terminal_batch_charge_before_settlement_payload_bytes=(before_settlement_bytes),
        settlement_event_charge_events=settlement_charge.event_count,
        settlement_event_charge_payload_bytes=settlement_charge.charged_payload_bytes,
        charged_candidate_events=0,
        charged_candidate_payload_bytes=0,
        charged_wrapper_bytes=0,
        charged_bookkeeping_events=total_lifetime_events,
        charged_bookkeeping_bytes=total_lifetime_bytes,
        total_charged_events=total_lifetime_events,
        total_charged_payload_bytes=total_lifetime_bytes,
        terminal_outcome=terminal_outcome,
        released_on_suspension_events_lifetime=0,
        released_on_suspension_payload_bytes_lifetime=0,
        released_on_settlement_events=(active.remaining_events - total_events),
        released_on_settlement_payload_bytes=(
            active.remaining_payload_bytes - total_bytes
        ),
        resulting_reservation_state_fingerprint=context_fingerprint(
            "settled-physical-reservation-state:v1",
            installed.reservation.reservation_fingerprint,
        ),
    )
    settlement_event = coordinator._prepare_event(  # noqa: SLF001
        PhysicalOperationReservationSettledEvent(
            id=settlement_event_id,
            **context.event_fields(),
            created_at=installed.intent_event.created_at,
            settlement=settlement,
            transition=transition,
            resulting_account_state_fingerprint=resulting.account_state_fingerprint,
        )
    )
    release = build_frozen_fact(
        CheckpointDispatchBarrierReleaseFact,
        schema_version="checkpoint_dispatch_barrier_release.v1",
        barrier_id=installed.barrier.barrier_id,
        checkpoint_id=installed.checkpoint_id,
        checkpoint_candidate_fingerprint=(installed.checkpoint_candidate_fingerprint),
        terminal_reference=terminal_reference,
        maintenance_settlement_event_identity=stable_event_identity(
            settlement_event, runtime_session_id=coordinator.runtime_session_id
        ),
    )
    release_event = coordinator._prepare_event(  # noqa: SLF001
        CheckpointDispatchBarrierReleasedEvent(
            id=release_event_id,
            **context.event_fields(),
            created_at=installed.intent_event.created_at,
            release=release,
            transition=transition,
            resulting_account_state_fingerprint=resulting.account_state_fingerprint,
        )
    )
    events: tuple[AgentEvent, ...] = (
        terminal_event,
        settlement_event,
        release_event,
    )
    stored = coordinator.commit_transition_batch(
        source=source,
        events=events,
        resulting=resulting,
        deadline_monotonic=deadline_monotonic,
    )
    return TerminatedTranscriptCheckpoint(
        installed=installed,
        terminal_event=_canonical_stored_event(
            stored, terminal_event, type(terminal_event)
        ),
        settlement_event=_canonical_stored_event(
            stored, settlement_event, PhysicalOperationReservationSettledEvent
        ),
        barrier_release_event=_canonical_stored_event(
            stored, release_event, CheckpointDispatchBarrierReleasedEvent
        ),
        stored_events=stored,
        resulting_account_state=resulting,
    )


def _validate_failure_terminal(
    *,
    terminal_contract: CheckpointTerminalContractFact,
    reason_code: CheckpointFailureReasonCode,
    diagnostics: Sequence[CheckpointTerminalDiagnosticFact],
) -> None:
    if len(diagnostics) > terminal_contract.max_diagnostics:
        raise ValueError("checkpoint diagnostic count exceeds terminal contract")
    matching = tuple(
        item
        for item in terminal_contract.failure_rules
        if item.reason_code is reason_code
    )
    if len(matching) != 1:
        raise ValueError("checkpoint failure reason is absent from terminal contract")
    rule = matching[0]
    if any(
        diagnostic.failure_stage not in rule.allowed_failure_stages
        or diagnostic.diagnostic_code not in rule.allowed_diagnostic_codes
        for diagnostic in diagnostics
    ):
        raise ValueError("checkpoint failure diagnostic violates reason/stage matrix")


def _checkpoint_transition(
    source: LedgerMaterializationAccountStateFact,
    resulting: LedgerMaterializationAccountStateFact,
    causes: Sequence[LedgerMaterializationTransitionCauseIdentityFact],
) -> LedgerMaterializationAccountTransitionFact:
    return build_frozen_fact(
        LedgerMaterializationAccountTransitionFact,
        schema_version="ledger_materialization_account_transition.v2",
        runtime_session_id=source.runtime_session_id,
        source_generation=source.generation.ledger_materialization_generation,
        source_consumer_horizon_revision=source.generation.consumer_horizon_revision,
        result_generation=resulting.generation.ledger_materialization_generation,
        result_consumer_horizon_revision=(
            resulting.generation.consumer_horizon_revision
        ),
        before_account_state_fingerprint=source.account_state_fingerprint,
        after_account_state_fingerprint=resulting.account_state_fingerprint,
        cause_event_identities=tuple(
            sorted(causes, key=lambda item: item.event_identity.event_id)
        ),
        transition_contract_fingerprint=context_fingerprint(
            "ledger-materialization-checkpoint-transition-contract:v1",
            "barrier+maintenance-reservation+terminal-release",
        ),
    )


def _validate_scope_and_consumer(
    *,
    scope: TranscriptProjectionScopeFact,
    consumer: LedgerMaterializationConsumerHorizonFact,
    account_state: LedgerMaterializationAccountStateFact,
    live: TranscriptProjectionLiveAssemblyState,
) -> None:
    if (
        scope.runtime_session_id != account_state.runtime_session_id
        or consumer.runtime_session_id != scope.runtime_session_id
        or consumer.business_run_id != scope.run_id
        or consumer.business_window_id != scope.window_id
        or consumer.business_window_generation != scope.window_generation
    ):
        raise ValueError("checkpoint scope does not match materialization consumer")
    matching = tuple(
        item
        for item in account_state.generation.consumer_horizons
        if item.consumer_id == consumer.consumer_id
    )
    if matching != (consumer,):
        raise ValueError("checkpoint consumer is not active in the account")
    if live.ledger_through_sequence != account_state.ledger_through_sequence:
        raise ValueError("checkpoint source is stale relative to account high-water")


__all__ = [
    "CommittedTranscriptCheckpoint",
    "TerminatedTranscriptCheckpoint",
    "PreparedTranscriptCheckpoint",
    "InstalledTranscriptCheckpoint",
    "RestoredTranscriptCheckpointOwner",
    "TRANSCRIPT_CHECKPOINT_BUILD_CONTRACT_FINGERPRINT",
    "prepare_transcript_checkpoint_candidate",
    "build_default_checkpoint_terminal_contract",
    "commit_checkpoint_cancellation",
    "commit_checkpoint_failure",
    "commit_checkpoint_recovered_interrupted",
    "commit_checkpoint_success",
    "install_checkpoint_barrier",
]
