"""Service-owned transcript checkpoint lifecycle and close drain."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Literal

from pulsara_agent.event import (
    CheckpointDispatchBarrierInstalledEvent,
    EventContext,
    PhysicalOperationReservationCreatedEvent,
    RunStartEvent,
    TranscriptProjectionCheckpointCommittedEvent,
    TranscriptProjectionCheckpointIntentEvent,
)
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.event_log.protocol import (
    RawStoredEventEnvelope,
    RawTranscriptDomainPrefixFact,
)
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.authority_materialization import (
    LedgerMaterializationAccountStateFact,
    LedgerMaterializationConsumerKind,
    LedgerWriteAdmissionClass,
    PhysicalOperationKind,
    TranscriptDomainSparseReadProofFact,
    TranscriptProjectionStableSemanticStateFact,
)
from pulsara_agent.runtime.authority_materialization.account import (
    MaterializationAccountCommitFailed,
    MaterializationAccountReconciliationRequired,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.transcript_checkpoint import (
    CheckpointCancellationReasonCode,
    CheckpointFailureReasonCode,
)
from pulsara_agent.primitives.transcript_projection import (
    CheckpointProjectionBaseFact,
    NormalizedMessageContentArtifactFact,
    ProjectionBaseCommonFact,
    ProjectionBaseSemanticIdentityFact,
    RunSeedProjectionBaseFact,
    RunTranscriptSeedReferenceFact,
    RunTranscriptSeedSemanticFact,
    TranscriptProjectionAccelerationFact,
    TranscriptProjectionBaseFact,
    TranscriptProjectionCheckpointMaterializationFact,
    TranscriptProjectionLeafEntryFact,
    TranscriptProjectionScopeFact,
    TranscriptProjectionSemanticSourceFact,
)
from pulsara_agent.runtime.authority_materialization.checkpoint import (
    CommittedTranscriptCheckpoint,
    InstalledTranscriptCheckpoint,
    PreparedTranscriptCheckpoint,
    RestoredTranscriptCheckpointOwner,
    TerminatedTranscriptCheckpoint,
    build_default_checkpoint_terminal_contract,
    commit_checkpoint_failure,
    commit_checkpoint_cancellation,
    commit_checkpoint_recovered_interrupted,
    commit_checkpoint_success,
    install_checkpoint_barrier,
    prepare_transcript_checkpoint_candidate,
)
from pulsara_agent.runtime.authority_materialization.transcript_tree import (
    prepare_authority_artifact_write_reservation,
    persist_prepared_transcript_projection_materialization,
)
from pulsara_agent.runtime.authority_materialization.transcript_hydrator import (
    TranscriptProjectionHydrationError,
    hydrate_run_transcript_seed,
)
from pulsara_agent.runtime.authority_materialization.transcript_restore import (
    RestoredTranscriptProjection,
    restore_transcript_projection,
)
from pulsara_agent.runtime.authority_materialization.contracts import (
    materialize_transcript_sparse_read_proof,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
)
from pulsara_agent.runtime.authority_materialization.cursor_resident_budget import (
    CursorResidentBudgetManager,
    CursorResidentHandle,
    process_cursor_resident_budget_manager,
)
from pulsara_agent.runtime.authority_materialization.evidence_cursor import (
    CursorAnchorCarrierIdentity,
    PersistentTranscriptSemanticEnvelopeVector,
    ProjectionEvidenceCursorOutcome,
    TranscriptProjectionCursorBaseIdentity,
    TranscriptProjectionDocumentResolver,
    TranscriptProjectionReducerEvidenceSnapshot,
    ValidatedCursorSnapshotFactory,
    ValidatedCursorUseToken,
    VerifiedTranscriptProjectionCursorSnapshot,
    build_checkpoint_cursor_base_identity,
    build_run_seed_cursor_base_identity,
)
from pulsara_agent.runtime.context_input.io_service import (
    ContextInputIoOperationHandle,
)

from .dispatch_barrier import CheckpointDrainToken

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


class TranscriptCheckpointBlocked(RuntimeError):
    """A durable checkpoint owner requires retry or reconciliation."""


@dataclass(slots=True)
class _CheckpointOwner:
    checkpoint_id: str
    context: EventContext
    prepared: PreparedTranscriptCheckpoint
    task: (
        asyncio.Task[
            CommittedTranscriptCheckpoint | TerminatedTranscriptCheckpoint | None
        ]
        | None
    ) = None
    installed: InstalledTranscriptCheckpoint | None = None
    committed_terminal: (
        CommittedTranscriptCheckpoint | TerminatedTranscriptCheckpoint | None
    ) = None
    pending_terminalization: Literal["success", "failure", "cancellation"] | None = None
    close_cancel_requested: bool = False
    artifact_operation: ContextInputIoOperationHandle[object] | None = None
    dispatch_drain: CheckpointDrainToken | None = None


@dataclass(frozen=True, slots=True)
class PreparedTranscriptProjectionEvidence:
    projection_base: TranscriptProjectionBaseFact
    semantic_source: TranscriptProjectionSemanticSourceFact
    domain_completeness_proof: TranscriptDomainSparseReadProofFact
    semantic_delta_events: tuple[RawStoredEventEnvelope, ...]
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...]
    document_registry: TranscriptProjectionDocumentResolver
    hydrated_message_contents: tuple[NormalizedMessageContentArtifactFact, ...]
    cursor_outcome: ProjectionEvidenceCursorOutcome


@dataclass(frozen=True, slots=True)
class _RunSeedProjectionAnchor:
    anchor_kind: Literal["run_seed"]
    carrier: CursorAnchorCarrierIdentity
    seed_semantic: RunTranscriptSeedSemanticFact
    seed_reference: RunTranscriptSeedReferenceFact


@dataclass(frozen=True, slots=True)
class _CheckpointProjectionAnchor:
    anchor_kind: Literal["checkpoint"]
    carrier: CursorAnchorCarrierIdentity
    checkpoint_candidate_fingerprint: str
    seed_semantic: RunTranscriptSeedSemanticFact
    seed_reference: RunTranscriptSeedReferenceFact
    stable_semantic_state: TranscriptProjectionStableSemanticStateFact
    scope: TranscriptProjectionScopeFact
    checkpoint_id: str
    checkpoint_committed_event_id: str
    checkpoint_committed_event_sequence: int
    checkpoint_candidate_ledger_through_sequence: int
    checkpoint_candidate_ledger_continuity_accumulator: str
    checkpoint_materialization: TranscriptProjectionCheckpointMaterializationFact
    previous_checkpoint_id: str | None
    ledger_materialization_generation: int
    consumer_horizon_revision: int
    build_contract_fingerprint: str


_ProjectionAnchor = _RunSeedProjectionAnchor | _CheckpointProjectionAnchor


@dataclass(slots=True, weakref_slot=True)
class TranscriptProjectionCheckpointService:
    runtime_session: RuntimeSession
    _owners: dict[str, _CheckpointOwner] = field(
        default_factory=dict, init=False, repr=False
    )
    _checkpoint_owner_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _cursor_advance_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _anchor_state_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _projection_anchor_generation: int = field(default=0, init=False, repr=False)
    _verified_evidence_cursor_handle: CursorResidentHandle | None = field(
        default=None, init=False, repr=False
    )
    _cursor_resident_budget: CursorResidentBudgetManager = field(
        default_factory=process_cursor_resident_budget_manager,
        init=False,
        repr=False,
    )
    _reachable_artifact_ids: frozenset[str] = field(
        default_factory=frozenset, init=False, repr=False
    )
    _latest_checkpoint_id: str | None = field(default=None, init=False, repr=False)
    _projection_anchor: _ProjectionAnchor | None = field(
        default=None, init=False, repr=False
    )
    _prepared_run_seed_artifacts: dict[str, frozenset[str]] = field(
        default_factory=dict, init=False, repr=False
    )
    _active_run_context: EventContext | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        restored = self.runtime_session.transcript_projection_restore
        if restored.active_run_start is not None:
            self._active_run_context = EventContext(
                run_id=restored.active_run_start.run_id,
                turn_id=restored.active_run_start.turn_id,
                reply_id=restored.active_run_start.reply_id,
            )
        self._reachable_artifact_ids = restored.reachable_artifact_ids
        if restored.base_kind == "checkpoint":
            self._latest_checkpoint_id = restored.base_id
        if (
            restored.projection_base is not None
            and restored.anchor_carrier_event is not None
        ):
            self._projection_anchor = _anchor_from_restored_base(
                restored.projection_base,
                carrier_event=restored.anchor_carrier_event,
            )
            self._projection_anchor_generation = 1
            self._seed_cursor_from_restored(restored)
        account = self.runtime_session.materialization_account_store.snapshot()
        if account is not None and account.active_checkpoint_barrier is not None:
            self._recover_interrupted_checkpoint(account)

    def _recover_interrupted_checkpoint(
        self,
        account: LedgerMaterializationAccountStateFact,
    ) -> None:
        """Terminalize one durable barrier before the reopened session is usable."""

        barrier = account.active_checkpoint_barrier
        if barrier is None:
            return
        active = tuple(
            item
            for item in account.active_reservations
            if item.reservation_id == barrier.maintenance_reservation_id
        )
        if len(active) != 1:
            raise TranscriptCheckpointBlocked(
                "checkpoint recovery has no unique maintenance reservation"
            )
        reservation_state = active[0]
        event_ids = (
            barrier.checkpoint_intent_event_identity.event_id,
            reservation_state.latest_reservation_event_id,
            f"checkpoint_barrier_installed:{barrier.checkpoint_id}",
        )
        deadline = monotonic() + (
            self.runtime_session.authority_materialization_contracts.limits.checkpoint_operation_timeout_seconds
        )
        snapshot = self.runtime_session.event_log.read_raw_events_by_id_snapshot(
            event_ids,
            deadline_monotonic=deadline,
        )
        if snapshot.through_sequence != account.ledger_through_sequence or len(
            snapshot.events
        ) != len(event_ids):
            raise TranscriptCheckpointBlocked(
                "checkpoint recovery installation facts are incomplete or stale"
            )
        decoded = tuple(
            raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY) for raw in snapshot.events
        )
        by_id = {event.id: event for event in decoded}
        intent = by_id.get(event_ids[0])
        reservation_event = by_id.get(event_ids[1])
        barrier_event = by_id.get(event_ids[2])
        if not isinstance(intent, TranscriptProjectionCheckpointIntentEvent):
            raise TranscriptCheckpointBlocked(
                "checkpoint recovery intent reference has the wrong event type"
            )
        if not isinstance(reservation_event, PhysicalOperationReservationCreatedEvent):
            raise TranscriptCheckpointBlocked(
                "checkpoint recovery reservation reference has the wrong event type"
            )
        if not isinstance(barrier_event, CheckpointDispatchBarrierInstalledEvent):
            raise TranscriptCheckpointBlocked(
                "checkpoint recovery barrier reference has the wrong event type"
            )
        reservation = reservation_event.reservation
        if (
            intent.checkpoint_id != barrier.checkpoint_id
            or intent.checkpoint_candidate_fingerprint
            != barrier.checkpoint_candidate_fingerprint
            or intent.maintenance_reservation_id != reservation.reservation_id
            or barrier_event.barrier != barrier
            or reservation.reservation_id != reservation_state.reservation_id
            or reservation.reservation_fingerprint
            != reservation_state.reservation_fingerprint
            or stable_event_identity(
                intent,
                runtime_session_id=self.runtime_session.runtime_session_id,
            )
            != barrier.checkpoint_intent_event_identity
            or reservation_event.resulting_account_state_fingerprint
            != account.account_state_fingerprint
            or barrier_event.resulting_account_state_fingerprint
            != account.account_state_fingerprint
        ):
            raise TranscriptCheckpointBlocked(
                "checkpoint recovery durable owner identity drifted"
            )
        restored = RestoredTranscriptCheckpointOwner(
            checkpoint_id=barrier.checkpoint_id,
            checkpoint_candidate_fingerprint=(barrier.checkpoint_candidate_fingerprint),
            intent_event=intent,
            reservation=reservation,
            reservation_event=reservation_event,
            barrier=barrier,
            barrier_event=barrier_event,
            stored_events=tuple(sorted(decoded, key=lambda item: item.sequence or 0)),
            resulting_account_state=account,
        )
        context = EventContext(
            run_id=intent.run_id,
            turn_id=intent.turn_id,
            reply_id=intent.reply_id,
        )
        def recover() -> TerminatedTranscriptCheckpoint:
            terminated = commit_checkpoint_recovered_interrupted(
                coordinator=self.runtime_session.materialization_coordinator,
                context=context,
                installed=restored,
                terminal_contract=intent.terminal_contract,
                reopen_ledger_high_water=account.ledger_through_sequence,
                deadline_monotonic=deadline,
            )
            self.runtime_session.checkpoint_dispatch_barrier_coordinator.release_after_terminal(
                checkpoint_id=barrier.checkpoint_id
            )
            self.runtime_session.accept_authority_materialization_transition(
                terminated.stored_events
            )
            return terminated

        try:
            self.runtime_session.event_write_service.execute_blocking(
                recover,
                deadline_monotonic=deadline,
                admission_class=LedgerWriteAdmissionClass.RECONCILIATION_CONTROL,
                checkpoint_id=barrier.checkpoint_id,
            )
        except MaterializationAccountReconciliationRequired as exc:
            self.runtime_session.checkpoint_dispatch_barrier_coordinator.latch_reconciliation(
                checkpoint_id=barrier.checkpoint_id,
                checkpoint_candidate_fingerprint=(
                    barrier.checkpoint_candidate_fingerprint
                ),
            )
            self.runtime_session.latch_event_commit_outcome_unknown()
            raise TranscriptCheckpointBlocked(
                "checkpoint recovered-interrupted terminalization is untrusted"
            ) from exc
        except BaseException as exc:
            raise TranscriptCheckpointBlocked(
                "checkpoint recovered-interrupted terminalization failed"
            ) from exc

    def prepare_run_seed_artifacts(
        self,
        *,
        run_id: str,
        artifact_ids: frozenset[str],
    ) -> None:
        with self._anchor_state_lock:
            if run_id in self._prepared_run_seed_artifacts:
                if self._prepared_run_seed_artifacts[run_id] != artifact_ids:
                    raise TranscriptCheckpointBlocked(
                        "prepared run-seed artifacts changed before RunStart commit"
                    )
                return
            self._prepared_run_seed_artifacts[run_id] = artifact_ids

    def discard_prepared_run_seed(self, run_id: str) -> None:
        with self._anchor_state_lock:
            self._prepared_run_seed_artifacts.pop(run_id, None)

    def adopt_committed_run_seed(self, run_start: RunStartEvent) -> None:
        if run_start.sequence is None:
            raise TranscriptCheckpointBlocked(
                "run-seed adoption requires a committed RunStart"
            )
        seed_reference = run_start.run_transcript_seed_reference
        if (
            seed_reference.source_runtime_session_id
            != self.runtime_session.runtime_session_id
        ):
            raise TranscriptCheckpointBlocked("run-seed ledger attribution drifted")
        with self._anchor_state_lock:
            artifacts = self._prepared_run_seed_artifacts.pop(run_start.run_id, None)
        if artifacts is None:
            try:
                hydrated = hydrate_run_transcript_seed(
                    archive=self.runtime_session.archive,
                    runtime_session_id=self.runtime_session.runtime_session_id,
                    seed_semantic=run_start.run_transcript_seed_semantic,
                    seed_reference=seed_reference,
                    contracts=(
                        self.runtime_session.transcript_projection_materialization_contracts
                    ),
                    deadline_monotonic=(
                        monotonic()
                        + self.runtime_session.authority_materialization_contracts.limits.operation_timeout_seconds
                    ),
                )
            except (KeyError, TranscriptProjectionHydrationError) as exc:
                raise TranscriptCheckpointBlocked(
                    "committed run seed cannot be hydrated"
                ) from exc
            artifacts = hydrated.reachable_artifact_ids
        carrier = _carrier_identity(
            run_start,
            runtime_session_id=self.runtime_session.runtime_session_id,
        )
        anchor = _RunSeedProjectionAnchor(
            anchor_kind="run_seed",
            carrier=carrier,
            seed_semantic=run_start.run_transcript_seed_semantic,
            seed_reference=seed_reference,
        )
        retired: CursorResidentHandle | None
        with self._anchor_state_lock:
            retired = self._verified_evidence_cursor_handle
            self._verified_evidence_cursor_handle = None
            self._projection_anchor_generation += 1
            self._projection_anchor = anchor
            self._reachable_artifact_ids = artifacts
            self._latest_checkpoint_id = None
            self._active_run_context = EventContext(
                run_id=run_start.run_id,
                turn_id=run_start.turn_id,
                reply_id=run_start.reply_id,
            )
        if retired is not None:
            self._cursor_resident_budget.retire(retired)

    async def checkpoint_for_admission(
        self,
        *,
        operation_kind: PhysicalOperationKind,
    ) -> CommittedTranscriptCheckpoint | TerminatedTranscriptCheckpoint | None:
        """Force the active transcript consumer forward before one dispatch.

        The caller must re-resolve physical capacity after this returns. A
        successful transcript checkpoint may still leave another consumer as
        the minimum reclaimable horizon.
        """

        if self.runtime_session.physical_dispatch_capacity(operation_kind) > 0:
            return None
        with self._anchor_state_lock:
            anchor = self._projection_anchor
            context = self._active_run_context
        if anchor is None or context is None:
            raise TranscriptCheckpointBlocked(
                "physical headroom recovery has no active transcript owner"
            )
        return await self.checkpoint_if_needed(
            context=context,
            run_seed_semantic=anchor.seed_semantic,
            run_seed_reference=anchor.seed_reference,
            force_for_admission=True,
        )

    async def prepare_projection_evidence(
        self,
        *,
        requested_through_sequence: int,
    ) -> PreparedTranscriptProjectionEvidence:
        limits = self.runtime_session.authority_materialization_contracts.limits
        deadline = monotonic() + limits.operation_timeout_seconds
        async with self._cursor_advance_lock:
            reducer_snapshot = (
                self.runtime_session.transcript_projection_state_store.evidence_snapshot()
            )
            live = reducer_snapshot.live_state
            if live.ledger_through_sequence < requested_through_sequence:
                raise TranscriptCheckpointBlocked(
                    "transcript projection reducer trails requested high-water"
                )
            with self._anchor_state_lock:
                anchor = self._projection_anchor
                generation = self._projection_anchor_generation
                handle = self._verified_evidence_cursor_handle
            if anchor is None:
                raise TranscriptCheckpointBlocked(
                    "active transcript projection has no adopted seed/checkpoint base"
                )
            base_sequence = _anchor_base_sequence(anchor)
            if requested_through_sequence < base_sequence:
                raise TranscriptCheckpointBlocked(
                    "requested transcript projection precedes its adopted base"
                )
            if anchor.carrier.committed_sequence > requested_through_sequence:
                return await self._prepare_exact_projection_evidence(
                    requested_through_sequence=requested_through_sequence,
                    outcome=ProjectionEvidenceCursorOutcome.EXACT_RESTORE_ANCHOR_CHANGED,
                    deadline_monotonic=deadline,
                )
            if live.ledger_through_sequence > requested_through_sequence:
                return await self._prepare_exact_projection_evidence(
                    requested_through_sequence=requested_through_sequence,
                    outcome=ProjectionEvidenceCursorOutcome.EXACT_RESTORE_LIVE_STORE_AHEAD,
                    deadline_monotonic=deadline,
                )

            binding = self.runtime_session.authority_materialization_contracts.event_domain
            if handle is not None:
                lease = self._cursor_resident_budget.borrow(handle)
                if lease is not None:
                    with lease as cursor:
                        try:
                            base_identity = _cursor_base_identity(
                                anchor=anchor,
                                base_prefix=cursor.delta_before,
                                runtime_session_id=(
                                    self.runtime_session.runtime_session_id
                                ),
                                registry_fingerprint=(
                                    binding.contract.registry_contract_fingerprint
                                ),
                            )
                            token = ValidatedCursorSnapshotFactory.validate_for_use(
                                cursor,
                                active_generation=generation,
                                active_base_identity=base_identity,
                                event_domain_binding=binding,
                                reducer_snapshot=reducer_snapshot,
                            )
                        except ValueError:
                            self._discard_cursor(handle)
                        else:
                            if cursor.verified_through_sequence == requested_through_sequence:
                                return self._prepared_from_cursor(
                                    cursor=cursor,
                                    reducer_snapshot=reducer_snapshot,
                                    outcome=(
                                        ProjectionEvidenceCursorOutcome.SAME_HIGH_WATER_HIT
                                    ),
                                )
                            if cursor.verified_through_sequence < requested_through_sequence:
                                return await self._extend_cursor(
                                    token=token,
                                    anchor=anchor,
                                    generation=generation,
                                    reducer_snapshot=reducer_snapshot,
                                    requested_through_sequence=(
                                        requested_through_sequence
                                    ),
                                    deadline_monotonic=deadline,
                                )
                            return await self._prepare_exact_projection_evidence(
                                requested_through_sequence=requested_through_sequence,
                                outcome=(
                                    ProjectionEvidenceCursorOutcome.EXACT_RESTORE_REQUESTED_BEHIND
                                ),
                                deadline_monotonic=deadline,
                            )
            return await self._prepare_full_range_and_seed_cursor(
                anchor=anchor,
                generation=generation,
                reducer_snapshot=reducer_snapshot,
                requested_through_sequence=requested_through_sequence,
                deadline_monotonic=deadline,
            )

    async def _extend_cursor(
        self,
        *,
        token: ValidatedCursorUseToken,
        anchor: _ProjectionAnchor,
        generation: int,
        reducer_snapshot: TranscriptProjectionReducerEvidenceSnapshot,
        requested_through_sequence: int,
        deadline_monotonic: float,
    ) -> PreparedTranscriptProjectionEvidence:
        cursor = token.cursor
        limits = self.runtime_session.authority_materialization_contracts.limits
        after_sequence = cursor.verified_through_sequence
        delta = await self.runtime_session.context_input_io_service.execute(
            operation_name="transcript-projection-evidence-delta-read",
            operation=lambda: self.runtime_session.event_log.read_transcript_domain_delta(
                after_sequence=after_sequence,
                through_sequence=requested_through_sequence,
                max_events=limits.max_unreclaimable_ledger_events,
                max_payload_bytes=limits.max_unreclaimable_charged_payload_bytes,
                registry_contract_fingerprint=(
                    self.runtime_session.authority_materialization_contracts.event_domain.contract.registry_contract_fingerprint
                ),
                deadline_monotonic=deadline_monotonic,
            ),
            deadline_monotonic=deadline_monotonic,
        )
        latest_snapshot = (
            self.runtime_session.transcript_projection_state_store.evidence_snapshot()
        )
        if latest_snapshot.live_state.ledger_through_sequence != requested_through_sequence:
            return await self._prepare_exact_projection_evidence(
                requested_through_sequence=requested_through_sequence,
                outcome=ProjectionEvidenceCursorOutcome.EXACT_RESTORE_LIVE_STORE_AHEAD,
                deadline_monotonic=deadline_monotonic,
            )
        next_base = _projection_base_from_anchor(
            anchor=anchor,
            delta_before=cursor.delta_before,
            delta_after=delta.after,
            registry_contract_fingerprint=delta.registry_contract_fingerprint,
            requested_through_sequence=requested_through_sequence,
        )
        candidate = ValidatedCursorSnapshotFactory.build_from_validated_previous(
            previous=token,
            new_delta=delta,
            next_projection_base=next_base,
            reducer_snapshot=latest_snapshot,
            event_domain_binding=(
                self.runtime_session.authority_materialization_contracts.event_domain
            ),
            max_payload_bytes=limits.max_unreclaimable_charged_payload_bytes,
        )
        installed = self._install_cursor(
            candidate,
            expected_anchor=anchor,
            expected_generation=generation,
        )
        if not installed and not self._anchor_matches(
            anchor=anchor,
            generation=generation,
        ):
            return await self._prepare_exact_projection_evidence(
                requested_through_sequence=requested_through_sequence,
                outcome=ProjectionEvidenceCursorOutcome.EXACT_RESTORE_ANCHOR_CHANGED,
                deadline_monotonic=deadline_monotonic,
            )
        return self._prepared_from_cursor(
            cursor=candidate,
            reducer_snapshot=latest_snapshot,
            outcome=(
                ProjectionEvidenceCursorOutcome.DELTA_EXTENSION
                if installed
                else ProjectionEvidenceCursorOutcome.RESIDENT_ADMISSION_REJECTED
            ),
        )

    async def _prepare_full_range_and_seed_cursor(
        self,
        *,
        anchor: _ProjectionAnchor,
        generation: int,
        reducer_snapshot: TranscriptProjectionReducerEvidenceSnapshot,
        requested_through_sequence: int,
        deadline_monotonic: float,
    ) -> PreparedTranscriptProjectionEvidence:
        limits = self.runtime_session.authority_materialization_contracts.limits
        delta = await self.runtime_session.context_input_io_service.execute(
            operation_name="transcript-projection-evidence-read",
            operation=lambda: self.runtime_session.event_log.read_transcript_domain_delta(
                after_sequence=_anchor_base_sequence(anchor),
                through_sequence=requested_through_sequence,
                max_events=limits.max_unreclaimable_ledger_events,
                max_payload_bytes=limits.max_unreclaimable_charged_payload_bytes,
                registry_contract_fingerprint=(
                    self.runtime_session.authority_materialization_contracts.event_domain.contract.registry_contract_fingerprint
                ),
                deadline_monotonic=deadline_monotonic,
            ),
            deadline_monotonic=deadline_monotonic,
        )
        live = reducer_snapshot.live_state
        stable = live.stable_semantic_state
        if (
            stable.semantic_source_event_count != delta.after.semantic_event_count
            or stable.semantic_source_accumulator != delta.after.semantic_accumulator
        ):
            return await self._prepare_exact_projection_evidence(
                requested_through_sequence=requested_through_sequence,
                outcome=ProjectionEvidenceCursorOutcome.EXACT_RESTORE_CURSOR_MISMATCH,
                deadline_monotonic=deadline_monotonic,
            )
        semantic_source = _semantic_source_from_state(
            stable_state=stable,
            registry_fingerprint=delta.registry_contract_fingerprint,
        )
        projection_base = _projection_base_from_anchor(
            anchor=anchor,
            delta_before=delta.before,
            delta_after=delta.after,
            registry_contract_fingerprint=delta.registry_contract_fingerprint,
            requested_through_sequence=requested_through_sequence,
        )
        vector = PersistentTranscriptSemanticEnvelopeVector.build(
            delta.semantic_events,
            max_payload_bytes=limits.max_unreclaimable_charged_payload_bytes,
        )
        base_identity = _cursor_base_identity(
            anchor=anchor,
            base_prefix=delta.before,
            runtime_session_id=self.runtime_session.runtime_session_id,
            registry_fingerprint=delta.registry_contract_fingerprint,
        )
        candidate = ValidatedCursorSnapshotFactory.build(
            generation=generation,
            base_identity=base_identity,
            projection_base=projection_base,
            base_prefix=delta.before,
            through_prefix=delta.after,
            semantic_envelopes=vector,
            semantic_source=semantic_source,
            domain_completeness_proof=materialize_transcript_sparse_read_proof(
                delta,
                binding=(
                    self.runtime_session.authority_materialization_contracts.event_domain
                ),
            ),
            reducer_snapshot=reducer_snapshot,
            event_domain_binding=(
                self.runtime_session.authority_materialization_contracts.event_domain
            ),
        )
        installed = self._install_cursor(
            candidate,
            expected_anchor=anchor,
            expected_generation=generation,
        )
        if not installed and not self._anchor_matches(
            anchor=anchor,
            generation=generation,
        ):
            return await self._prepare_exact_projection_evidence(
                requested_through_sequence=requested_through_sequence,
                outcome=ProjectionEvidenceCursorOutcome.EXACT_RESTORE_ANCHOR_CHANGED,
                deadline_monotonic=deadline_monotonic,
            )
        return self._prepared_from_cursor(
            cursor=candidate,
            reducer_snapshot=reducer_snapshot,
            outcome=(
                ProjectionEvidenceCursorOutcome.SEEDED_FROM_STARTUP_RESTORE
                if installed
                else ProjectionEvidenceCursorOutcome.RESIDENT_ADMISSION_REJECTED
            ),
        )

    async def _prepare_exact_projection_evidence(
        self,
        *,
        requested_through_sequence: int,
        outcome: ProjectionEvidenceCursorOutcome,
        deadline_monotonic: float,
    ) -> PreparedTranscriptProjectionEvidence:
        limits = self.runtime_session.authority_materialization_contracts.limits
        restored = await self.runtime_session.context_input_io_service.execute(
            operation_name="transcript-projection-evidence-exact-restore",
            operation=lambda: restore_transcript_projection(
                event_log=self.runtime_session.event_log,
                archive=self.runtime_session.archive,
                runtime_session_id=self.runtime_session.runtime_session_id,
                requested_through_sequence=requested_through_sequence,
                event_domain_binding=(
                    self.runtime_session.authority_materialization_contracts.event_domain
                ),
                materialization_contracts=(
                    self.runtime_session.transcript_projection_materialization_contracts
                ),
                limits=limits,
                deadline_monotonic=deadline_monotonic,
            ),
            deadline_monotonic=deadline_monotonic,
        )
        if restored.projection_base is None:
            raise TranscriptCheckpointBlocked(
                "exact transcript projection restore has no durable base"
            )
        snapshot = restored.state_store.evidence_snapshot()
        return PreparedTranscriptProjectionEvidence(
            projection_base=restored.projection_base,
            semantic_source=restored.semantic_source,
            domain_completeness_proof=restored.domain_completeness_proof,
            semantic_delta_events=restored.semantic_delta_events,
            stable_entries=snapshot.stable_entries,
            document_registry=restored.document_registry.freeze_references(
                snapshot.required_projection_references
            ),
            hydrated_message_contents=restored.hydrated_message_contents,
            cursor_outcome=outcome,
        )

    def _prepared_from_cursor(
        self,
        *,
        cursor: VerifiedTranscriptProjectionCursorSnapshot,
        reducer_snapshot: TranscriptProjectionReducerEvidenceSnapshot,
        outcome: ProjectionEvidenceCursorOutcome,
    ) -> PreparedTranscriptProjectionEvidence:
        documents = (
            self.runtime_session.transcript_projection_document_registry.freeze_references(
                reducer_snapshot.required_projection_references
            )
        )
        return PreparedTranscriptProjectionEvidence(
            projection_base=cursor.projection_base,
            semantic_source=cursor.semantic_source,
            domain_completeness_proof=cursor.domain_completeness_proof,
            semantic_delta_events=cursor.semantic_envelopes.materialize(),
            stable_entries=reducer_snapshot.stable_entries,
            document_registry=documents,
            hydrated_message_contents=(
                self.runtime_session.transcript_projection_restore.hydrated_message_contents
            ),
            cursor_outcome=outcome,
        )

    def _seed_cursor_from_restored(
        self,
        restored: RestoredTranscriptProjection,
    ) -> None:
        anchor = self._projection_anchor
        projection_base = restored.projection_base
        if anchor is None or projection_base is None:
            return
        snapshot = (
            self.runtime_session.transcript_projection_state_store.evidence_snapshot()
        )
        limits = self.runtime_session.authority_materialization_contracts.limits
        try:
            vector = PersistentTranscriptSemanticEnvelopeVector.build(
                restored.semantic_delta_events,
                max_payload_bytes=limits.max_unreclaimable_charged_payload_bytes,
            )
            base_identity = _cursor_base_identity(
                anchor=anchor,
                base_prefix=restored.delta_before,
                runtime_session_id=self.runtime_session.runtime_session_id,
                registry_fingerprint=(
                    self.runtime_session.authority_materialization_contracts.event_domain.contract.registry_contract_fingerprint
                ),
            )
            candidate = ValidatedCursorSnapshotFactory.build(
                generation=self._projection_anchor_generation,
                base_identity=base_identity,
                projection_base=projection_base,
                base_prefix=restored.delta_before,
                through_prefix=restored.delta_after,
                semantic_envelopes=vector,
                semantic_source=restored.semantic_source,
                domain_completeness_proof=restored.domain_completeness_proof,
                reducer_snapshot=snapshot,
                event_domain_binding=(
                    self.runtime_session.authority_materialization_contracts.event_domain
                ),
            )
        except ValueError:
            return
        self._install_cursor(
            candidate,
            expected_anchor=anchor,
            expected_generation=self._projection_anchor_generation,
        )

    def _install_cursor(
        self,
        candidate: VerifiedTranscriptProjectionCursorSnapshot,
        *,
        expected_anchor: _ProjectionAnchor,
        expected_generation: int,
    ) -> bool:
        with self._anchor_state_lock:
            if (
                self._projection_anchor != expected_anchor
                or self._projection_anchor_generation != expected_generation
            ):
                return False
            replaces = self._verified_evidence_cursor_handle
        reservation = self._cursor_resident_budget.prepare_admission(
            owner_runtime_session_id=self.runtime_session.runtime_session_id,
            anchor_generation=expected_generation,
            candidate=candidate,
            replaces=replaces,
            eviction_callback=self._evict_cursor,
        )
        if reservation is None:
            return False
        should_abort = False
        with self._anchor_state_lock:
            if (
                self._projection_anchor != expected_anchor
                or self._projection_anchor_generation != expected_generation
            ):
                should_abort = True
            else:
                self._verified_evidence_cursor_handle = reservation.provisional_handle
        if should_abort:
            self._cursor_resident_budget.abort_admission(reservation)
            return False
        try:
            handle = self._cursor_resident_budget.commit_admission(reservation)
        except BaseException:
            with self._anchor_state_lock:
                if (
                    self._verified_evidence_cursor_handle
                    == reservation.provisional_handle
                ):
                    self._verified_evidence_cursor_handle = None
            self._cursor_resident_budget.abort_admission(reservation)
            return False
        should_retire = False
        with self._anchor_state_lock:
            if (
                self._projection_anchor != expected_anchor
                or self._projection_anchor_generation != expected_generation
                or self._verified_evidence_cursor_handle
                != reservation.provisional_handle
            ):
                if (
                    self._verified_evidence_cursor_handle
                    == reservation.provisional_handle
                ):
                    self._verified_evidence_cursor_handle = None
                should_retire = True
            elif handle != reservation.provisional_handle:
                self._verified_evidence_cursor_handle = None
                should_retire = True
        if should_retire:
            self._cursor_resident_budget.retire(handle)
            return False
        return True

    def _anchor_matches(
        self,
        *,
        anchor: _ProjectionAnchor,
        generation: int,
    ) -> bool:
        with self._anchor_state_lock:
            return (
                self._projection_anchor == anchor
                and self._projection_anchor_generation == generation
            )

    def _discard_cursor(self, handle: CursorResidentHandle) -> None:
        with self._anchor_state_lock:
            if self._verified_evidence_cursor_handle != handle:
                return
            self._verified_evidence_cursor_handle = None
        self._cursor_resident_budget.retire(handle)

    def _evict_cursor(self, resident_entry_id: str) -> bool:
        with self._anchor_state_lock:
            handle = self._verified_evidence_cursor_handle
            if handle is None or handle.resident_entry_id != resident_entry_id:
                return False
            self._verified_evidence_cursor_handle = None
            return True

    async def projection_delta_minimum_sequence(self) -> int:
        """Return the first ledger sequence not covered by the adopted base."""

        with self._anchor_state_lock:
            anchor = self._projection_anchor
        if anchor is None:
            raise TranscriptCheckpointBlocked(
                "active transcript projection has no adopted seed/checkpoint base"
            )
        base_sequence = (
            anchor.seed_reference.source_ledger_through_sequence
            if isinstance(anchor, _RunSeedProjectionAnchor)
            else anchor.checkpoint_candidate_ledger_through_sequence
        )
        return base_sequence + 1

    async def checkpoint_if_needed(
        self,
        *,
        context: EventContext,
        run_seed_semantic: RunTranscriptSeedSemanticFact,
        run_seed_reference: RunTranscriptSeedReferenceFact,
        force_for_admission: bool = False,
    ) -> CommittedTranscriptCheckpoint | TerminatedTranscriptCheckpoint | None:
        subscriber_deadline = monotonic() + (
            self.runtime_session.authority_materialization_contracts.limits.checkpoint_operation_timeout_seconds
        )
        account = self.runtime_session.materialization_account_store.snapshot()
        if account is None:
            return None
        limits = self.runtime_session.authority_materialization_contracts.limits
        if not force_for_admission and (
            account.used_since_reclaimable_events < limits.soft_reclaim_pressure_events
            and account.used_since_reclaimable_payload_bytes
            < limits.soft_reclaim_pressure_payload_bytes
        ):
            return None
        if (
            account.reconciliation_required
            or account.active_checkpoint_barrier is not None
        ):
            raise TranscriptCheckpointBlocked(
                "transcript checkpoint account is not dispatchable"
            )
        if account.active_reservations:
            if force_for_admission:
                raise TranscriptCheckpointBlocked(
                    "physical headroom recovery is blocked by active reservations"
                )
            return None
        live = self.runtime_session.transcript_projection_state_store.snapshot()
        if not live.checkpointable:
            if force_for_admission:
                raise TranscriptCheckpointBlocked(
                    "physical headroom recovery requires a checkpointable transcript"
                )
            return None
        consumers = tuple(
            item
            for item in account.generation.consumer_horizons
            if item.consumer_kind is LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW
            and item.business_run_id == context.run_id
        )
        if len(consumers) != 1:
            raise TranscriptCheckpointBlocked(
                "active run has no unique transcript materialization consumer"
            )
        consumer = consumers[0]
        if consumer.through_sequence >= live.ledger_through_sequence:
            return None
        if (
            consumer.business_window_id is None
            or consumer.business_window_generation is None
        ):
            raise TranscriptCheckpointBlocked(
                "transcript consumer has incomplete business attribution"
            )
        scope = build_frozen_fact(
            TranscriptProjectionScopeFact,
            schema_version="transcript_projection_scope.v1",
            runtime_session_id=self.runtime_session.runtime_session_id,
            run_id=context.run_id,
            window_id=consumer.business_window_id,
            window_generation=consumer.business_window_generation,
        )
        digest = context_fingerprint(
            "transcript-checkpoint-id:v1",
            {
                "runtime_session_id": self.runtime_session.runtime_session_id,
                "consumer_id": consumer.consumer_id,
                "through_sequence": live.ledger_through_sequence,
                "state": live.stable_semantic_state.state_semantic_fingerprint,
            },
        ).removeprefix("sha256:")
        # 160 bits remain collision-resistant while leaving room for the
        # reservation/event prefixes under the durable 128-character bound.
        checkpoint_id = f"transcript_checkpoint:{digest[:40]}"
        with self._anchor_state_lock:
            previous_checkpoint_id = self._latest_checkpoint_id
            reachable_artifact_ids = self._reachable_artifact_ids
        prepared = prepare_transcript_checkpoint_candidate(
            checkpoint_id=checkpoint_id,
            scope=scope,
            run_seed_semantic=run_seed_semantic,
            run_seed_reference=run_seed_reference,
            materialization_consumer=consumer,
            account_state=account,
            transcript_store=self.runtime_session.transcript_projection_state_store,
            transcript_semantic_domain_contract_fingerprint=(
                self.runtime_session.authority_materialization_contracts.event_domain.contract.registry_contract_fingerprint
            ),
            contracts=self.runtime_session.transcript_projection_materialization_contracts,
            limits=limits,
            previous_checkpoint_id=previous_checkpoint_id,
            previously_reachable_artifact_ids=reachable_artifact_ids,
        )
        async with self._checkpoint_owner_lock:
            existing = self._owners.get(checkpoint_id)
            if existing is None:
                existing = _CheckpointOwner(
                    checkpoint_id=checkpoint_id,
                    context=context,
                    prepared=prepared,
                )
                self._owners[checkpoint_id] = existing
                self._start_owner_task(existing)
            elif existing.task is None or (
                existing.task.done()
                and (
                    existing.pending_terminalization is not None
                    or existing.committed_terminal is not None
                    or existing.dispatch_drain is not None
                )
            ):
                self._start_owner_task(existing)
            task = existing.task
        if task is None:
            raise TranscriptCheckpointBlocked("checkpoint owner has no execution task")
        try:
            remaining = subscriber_deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            result = await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except TimeoutError as exc:
            raise TranscriptCheckpointBlocked(
                "checkpoint caller deadline expired; service owner remains active"
            ) from exc
        except BaseException:
            async with self._checkpoint_owner_lock:
                if (
                    self._owners.get(checkpoint_id) is existing
                    and existing.installed is None
                    and existing.pending_terminalization is None
                    and existing.dispatch_drain is None
                    and existing.task is task
                    and task.done()
                ):
                    self._owners.pop(checkpoint_id, None)
            raise
        async with self._checkpoint_owner_lock:
            if self._owners.get(checkpoint_id) is existing:
                self._owners.pop(checkpoint_id, None)
        return result

    def _start_owner_task(
        self,
        owner: _CheckpointOwner,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        if owner.task is not None and not owner.task.done():
            return
        owner.task = asyncio.create_task(
            self._run_owner(owner=owner, deadline_monotonic=deadline_monotonic)
        )

    async def _run_owner(
        self,
        *,
        owner: _CheckpointOwner,
        deadline_monotonic: float | None,
    ) -> CommittedTranscriptCheckpoint | TerminatedTranscriptCheckpoint | None:
        prepared = owner.prepared
        context = owner.context
        operation_deadline = monotonic() + (
            self.runtime_session.authority_materialization_contracts.limits.checkpoint_operation_timeout_seconds
        )
        deadline = (
            operation_deadline
            if deadline_monotonic is None
            else min(operation_deadline, deadline_monotonic)
        )
        terminal_contract = build_default_checkpoint_terminal_contract()
        burst_contract = self.runtime_session.authority_materialization_contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.CHECKPOINT_COMMIT
        ).contract
        installed = owner.installed
        committed_terminal = owner.committed_terminal
        if committed_terminal is not None:

            def handoff_committed_terminal() -> None:
                self.runtime_session.accept_authority_materialization_transition(
                    committed_terminal.stored_events
                )

            await self.runtime_session.event_write_service.execute(
                handoff_committed_terminal,
                deadline_monotonic=deadline,
                admission_class=LedgerWriteAdmissionClass.RECONCILIATION_CONTROL,
                checkpoint_id=owner.checkpoint_id,
            )
            if isinstance(committed_terminal, CommittedTranscriptCheckpoint):
                await self._adopt_committed_checkpoint(committed_terminal)
            owner.committed_terminal = None
            owner.pending_terminalization = None
            return committed_terminal
        if installed is None:
            if owner.close_cancel_requested:
                return None

            gate = self.runtime_session.checkpoint_dispatch_barrier_coordinator
            if owner.dispatch_drain is None:
                owner.dispatch_drain = gate.begin_checkpoint_drain(
                    checkpoint_id=owner.checkpoint_id,
                    checkpoint_candidate_fingerprint=(
                        prepared.candidate.candidate_fingerprint
                    ),
                )
            drain_token = owner.dispatch_drain
            await asyncio.to_thread(
                gate.wait_until_drained,
                drain_token,
                deadline_monotonic=deadline,
            )
            if owner.close_cancel_requested:
                gate.abort_before_install(drain_token)
                owner.dispatch_drain = None
                return None

            drained_account = self.runtime_session.materialization_account_store.snapshot()
            candidate = prepared.candidate
            if (
                drained_account is None
                or drained_account.active_reservations
                or drained_account.ledger_through_sequence
                != candidate.candidate_ledger_through_sequence
                or drained_account.generation.ledger_materialization_generation
                != candidate.source_ledger_materialization_generation
                or drained_account.generation.consumer_horizon_revision
                != candidate.source_consumer_horizon_revision
            ):
                gate.abort_before_install(drain_token)
                owner.dispatch_drain = None
                return None

            def install() -> InstalledTranscriptCheckpoint:
                installed_owner = install_checkpoint_barrier(
                    coordinator=self.runtime_session.materialization_coordinator,
                    context=context,
                    prepared=prepared,
                    checkpoint_burst_contract=burst_contract,
                    terminal_contract=terminal_contract,
                    deadline_monotonic=deadline,
                )
                gate.mark_durable_active(drain_token, installed_owner.barrier)
                owner.installed = installed_owner
                self.runtime_session.accept_authority_materialization_transition(
                    installed_owner.stored_events
                )
                return installed_owner

            try:
                installed = await self.runtime_session.event_write_service.execute(
                    install,
                    deadline_monotonic=deadline,
                    admission_class=(
                        LedgerWriteAdmissionClass.CHECKPOINT_BARRIER_CONTROL
                    ),
                    checkpoint_id=owner.checkpoint_id,
                )
            except MaterializationAccountCommitFailed:
                gate.abort_before_install(drain_token)
                owner.dispatch_drain = None
                raise
            except MaterializationAccountReconciliationRequired:
                gate.latch_reconciliation(
                    checkpoint_id=owner.checkpoint_id,
                    checkpoint_candidate_fingerprint=candidate.candidate_fingerprint,
                )
                self.runtime_session.latch_event_commit_outcome_unknown()
                raise
            owner.installed = installed
        terminal_contract = installed.intent_event.terminal_contract
        if (
            owner.pending_terminalization is None
            and owner.close_cancel_requested
            and owner.artifact_operation is None
        ):
            owner.pending_terminalization = "cancellation"
        if owner.pending_terminalization is None:
            artifact_write_reservation = prepare_authority_artifact_write_reservation(
                operation_id=prepared.candidate.checkpoint_id,
                owner_kind="checkpoint_materialization",
                artifacts=prepared.materialization.artifacts,
                limits=(
                    self.runtime_session.authority_materialization_contracts.limits
                ),
                absolute_deadline_monotonic=deadline,
            )
            try:
                if owner.artifact_operation is None:
                    owner.artifact_operation = await self.runtime_session.context_input_io_service.start_owned(
                        operation_name="transcript-checkpoint-materialization",
                        operation=lambda: (
                            persist_prepared_transcript_projection_materialization(
                                prepared.materialization,
                                write_reservation=artifact_write_reservation,
                                limits=(
                                    self.runtime_session.authority_materialization_contracts.limits
                                ),
                                archive=self.runtime_session.archive,
                                runtime_session_id=self.runtime_session.runtime_session_id,
                                run_id=context.run_id,
                                deadline_monotonic=deadline,
                            )
                        ),
                        deadline_monotonic=deadline,
                    )
                await owner.artifact_operation.wait_physical_completion()
            except asyncio.CancelledError:
                # The service-owned physical handle remains authoritative. A
                # retry rejoins it before selecting any durable terminal fact.
                raise
            except BaseException:
                owner.artifact_operation = None
                owner.pending_terminalization = (
                    "cancellation" if owner.close_cancel_requested else "failure"
                )
            else:
                owner.artifact_operation = None
                owner.pending_terminalization = (
                    "cancellation" if owner.close_cancel_requested else "success"
                )

        if owner.pending_terminalization == "cancellation":

            def cancel() -> TerminatedTranscriptCheckpoint:
                terminated = commit_checkpoint_cancellation(
                    coordinator=self.runtime_session.materialization_coordinator,
                    context=context,
                    installed=installed,
                    terminal_contract=terminal_contract,
                    cancellation_source="host_close",
                    reason_code=CheckpointCancellationReasonCode.HOST_CLOSE,
                    deadline_monotonic=deadline,
                )
                self.runtime_session.checkpoint_dispatch_barrier_coordinator.release_after_terminal(
                    checkpoint_id=owner.checkpoint_id
                )
                owner.committed_terminal = terminated
                self.runtime_session.accept_authority_materialization_transition(
                    terminated.stored_events
                )
                return terminated

            try:
                terminated = await self.runtime_session.event_write_service.execute(
                    cancel,
                    deadline_monotonic=deadline,
                    admission_class=(
                        LedgerWriteAdmissionClass.CHECKPOINT_BARRIER_CONTROL
                    ),
                    checkpoint_id=owner.checkpoint_id,
                )
            except MaterializationAccountReconciliationRequired:
                self._latch_checkpoint_reconciliation(owner)
                raise
            owner.committed_terminal = None
            owner.pending_terminalization = None
            return terminated

        if owner.pending_terminalization == "failure":

            def fail() -> TerminatedTranscriptCheckpoint:
                terminated = commit_checkpoint_failure(
                    coordinator=self.runtime_session.materialization_coordinator,
                    context=context,
                    installed=installed,
                    terminal_contract=terminal_contract,
                    reason_code=CheckpointFailureReasonCode.ARTIFACT_WRITE_FAILED,
                    diagnostics=(),
                    deadline_monotonic=deadline,
                )
                self.runtime_session.checkpoint_dispatch_barrier_coordinator.release_after_terminal(
                    checkpoint_id=owner.checkpoint_id
                )
                owner.committed_terminal = terminated
                self.runtime_session.accept_authority_materialization_transition(
                    terminated.stored_events
                )
                return terminated

            try:
                terminated = await self.runtime_session.event_write_service.execute(
                    fail,
                    deadline_monotonic=deadline,
                    admission_class=(
                        LedgerWriteAdmissionClass.CHECKPOINT_BARRIER_CONTROL
                    ),
                    checkpoint_id=owner.checkpoint_id,
                )
            except MaterializationAccountReconciliationRequired:
                self._latch_checkpoint_reconciliation(owner)
                raise
            owner.committed_terminal = None
            owner.pending_terminalization = None
            return terminated

        def succeed() -> CommittedTranscriptCheckpoint:
            committed = commit_checkpoint_success(
                coordinator=self.runtime_session.materialization_coordinator,
                context=context,
                installed=installed,
                terminal_contract=terminal_contract,
                deadline_monotonic=deadline,
            )
            self.runtime_session.checkpoint_dispatch_barrier_coordinator.release_after_terminal(
                checkpoint_id=owner.checkpoint_id
            )
            owner.committed_terminal = committed
            self.runtime_session.accept_authority_materialization_transition(
                committed.stored_events
            )
            return committed

        try:
            committed = await self.runtime_session.event_write_service.execute(
                succeed,
                deadline_monotonic=deadline,
                admission_class=(
                    LedgerWriteAdmissionClass.CHECKPOINT_BARRIER_CONTROL
                ),
                checkpoint_id=owner.checkpoint_id,
            )
        except MaterializationAccountReconciliationRequired:
            self._latch_checkpoint_reconciliation(owner)
            raise
        await self._adopt_committed_checkpoint(committed)
        owner.committed_terminal = None
        owner.pending_terminalization = None
        return committed

    def _latch_checkpoint_reconciliation(self, owner: _CheckpointOwner) -> None:
        self.runtime_session.checkpoint_dispatch_barrier_coordinator.latch_reconciliation(
            checkpoint_id=owner.checkpoint_id,
            checkpoint_candidate_fingerprint=(
                owner.prepared.candidate.candidate_fingerprint
            ),
        )
        self.runtime_session.latch_event_commit_outcome_unknown()

    async def _adopt_committed_checkpoint(
        self,
        committed: CommittedTranscriptCheckpoint,
    ) -> None:
        prepared = committed.installed.prepared
        candidate = prepared.candidate
        if committed.committed_event.sequence is None:
            raise TranscriptCheckpointBlocked(
                "committed transcript checkpoint lacks a sequence"
            )
        generation = committed.resulting_account_state.generation
        anchor = _CheckpointProjectionAnchor(
                anchor_kind="checkpoint",
                carrier=_carrier_identity(
                    committed.committed_event,
                    runtime_session_id=self.runtime_session.runtime_session_id,
                ),
                checkpoint_candidate_fingerprint=candidate.candidate_fingerprint,
                seed_semantic=candidate.run_seed_semantic,
                seed_reference=candidate.run_seed_reference,
                stable_semantic_state=candidate.stable_semantic_state,
                scope=candidate.scope,
                checkpoint_id=candidate.checkpoint_id,
                checkpoint_committed_event_id=committed.committed_event.id,
                checkpoint_committed_event_sequence=(
                    committed.committed_event.sequence
                ),
                checkpoint_candidate_ledger_through_sequence=(
                    candidate.candidate_ledger_through_sequence
                ),
                checkpoint_candidate_ledger_continuity_accumulator=(
                    candidate.candidate_ledger_continuity_accumulator
                ),
                checkpoint_materialization=candidate.materialization,
                previous_checkpoint_id=candidate.previous_checkpoint_id,
                ledger_materialization_generation=(
                    generation.ledger_materialization_generation
                ),
                consumer_horizon_revision=generation.consumer_horizon_revision,
                build_contract_fingerprint=candidate.build_contract_fingerprint,
            )
        with self._anchor_state_lock:
            retired = self._verified_evidence_cursor_handle
            self._verified_evidence_cursor_handle = None
            self._projection_anchor_generation += 1
            self._projection_anchor = anchor
            self._reachable_artifact_ids = self._reachable_artifact_ids | frozenset(
                item.artifact_id for item in prepared.materialization.artifacts
            )
            self._latest_checkpoint_id = candidate.checkpoint_id
        if retired is not None:
            self._cursor_resident_budget.retire(retired)

    async def request_close_cancellation(self) -> None:
        """Request typed cancellation without abandoning physical artifact I/O."""

        async with self._checkpoint_owner_lock:
            for owner in self._owners.values():
                if owner.pending_terminalization is None:
                    owner.close_cancel_requested = True

    async def drain_pending(self, *, deadline_monotonic: float) -> None:
        while True:
            async with self._checkpoint_owner_lock:
                owners = tuple(self._owners.values())
                if not owners:
                    return
                for owner in owners:
                    if owner.task is None or (
                        owner.task.done()
                        and (
                            owner.pending_terminalization is not None
                            or owner.committed_terminal is not None
                            or owner.artifact_operation is not None
                            or owner.dispatch_drain is not None
                        )
                    ):
                        self._start_owner_task(
                            owner,
                            deadline_monotonic=deadline_monotonic,
                        )
                tasks = tuple(owner.task for owner in owners if owner.task is not None)
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TimeoutError("transcript checkpoint drain timed out")
            done, _ = await asyncio.wait(tasks, timeout=remaining)
            if not done:
                raise TimeoutError("transcript checkpoint drain timed out")
            async with self._checkpoint_owner_lock:
                for item in owners:
                    task = item.task
                    if task is None or not task.done():
                        continue
                    if task.cancelled() or task.exception() is not None:
                        if (
                            item.pending_terminalization is not None
                            or item.committed_terminal is not None
                            or item.dispatch_drain is not None
                        ):
                            continue
                        task.result()
                    if self._owners.get(item.checkpoint_id) is item:
                        self._owners.pop(item.checkpoint_id, None)
            await asyncio.sleep(0)

    def close_if_idle(self) -> None:
        if self._owners:
            raise TranscriptCheckpointBlocked(
                "transcript checkpoint owner is still active"
            )
        with self._anchor_state_lock:
            handle = self._verified_evidence_cursor_handle
            self._verified_evidence_cursor_handle = None
        if handle is not None:
            self._cursor_resident_budget.retire(handle)

    @property
    def pending_count(self) -> int:
        return len(self._owners)


def _anchor_from_restored_base(
    base: TranscriptProjectionBaseFact,
    *,
    carrier_event: RunStartEvent | TranscriptProjectionCheckpointCommittedEvent,
) -> _ProjectionAnchor:
    common = base.common
    if isinstance(base, RunSeedProjectionBaseFact):
        if not isinstance(carrier_event, RunStartEvent):
            raise TranscriptCheckpointBlocked("run-seed anchor carrier is unavailable")
        return _RunSeedProjectionAnchor(
            anchor_kind="run_seed",
            carrier=_carrier_identity(
                carrier_event,
                runtime_session_id=common.run_seed_reference.source_runtime_session_id,
            ),
            seed_semantic=common.run_seed_semantic,
            seed_reference=common.run_seed_reference,
        )
    if not isinstance(carrier_event, TranscriptProjectionCheckpointCommittedEvent):
        raise TranscriptCheckpointBlocked("checkpoint anchor carrier is unavailable")
    acceleration = base.checkpoint_acceleration
    return _CheckpointProjectionAnchor(
        anchor_kind="checkpoint",
        carrier=_carrier_identity(
            carrier_event,
            runtime_session_id=common.run_seed_reference.source_runtime_session_id,
        ),
        checkpoint_candidate_fingerprint=(
            carrier_event.checkpoint_candidate_fingerprint
        ),
        seed_semantic=common.run_seed_semantic,
        seed_reference=common.run_seed_reference,
        stable_semantic_state=common.stable_semantic_state,
        scope=acceleration.scope,
        checkpoint_id=acceleration.checkpoint_id,
        checkpoint_committed_event_id=(acceleration.checkpoint_committed_event_id),
        checkpoint_committed_event_sequence=(
            acceleration.checkpoint_committed_event_sequence
        ),
        checkpoint_candidate_ledger_through_sequence=(
            acceleration.checkpoint_candidate_ledger_through_sequence
        ),
        checkpoint_candidate_ledger_continuity_accumulator=(
            acceleration.checkpoint_candidate_ledger_continuity_accumulator
        ),
        checkpoint_materialization=base.checkpoint_materialization,
        previous_checkpoint_id=acceleration.previous_checkpoint_id,
        ledger_materialization_generation=(
            acceleration.ledger_materialization_generation
        ),
        consumer_horizon_revision=acceleration.consumer_horizon_revision,
        build_contract_fingerprint=acceleration.build_contract_fingerprint,
    )


def _carrier_identity(
    event: RunStartEvent | TranscriptProjectionCheckpointCommittedEvent,
    *,
    runtime_session_id: str,
) -> CursorAnchorCarrierIdentity:
    if event.sequence is None:
        raise TranscriptCheckpointBlocked("cursor anchor carrier is not committed")
    if isinstance(event, RunStartEvent):
        kind = "run_start"
    elif isinstance(event, TranscriptProjectionCheckpointCommittedEvent):
        kind = "transcript_checkpoint_committed"
    else:
        raise TranscriptCheckpointBlocked("unsupported cursor anchor carrier")
    return CursorAnchorCarrierIdentity(
        stable_event_identity=stable_event_identity(
            event,
            runtime_session_id=runtime_session_id,
        ),
        committed_sequence=event.sequence,
        carrier_kind=kind,
    )


def _anchor_base_sequence(anchor: _ProjectionAnchor) -> int:
    if isinstance(anchor, _RunSeedProjectionAnchor):
        return anchor.seed_reference.source_ledger_through_sequence
    return anchor.checkpoint_candidate_ledger_through_sequence


def _cursor_base_identity(
    *,
    anchor: _ProjectionAnchor,
    base_prefix,
    runtime_session_id: str,
    registry_fingerprint: str,
) -> TranscriptProjectionCursorBaseIdentity:
    if base_prefix.through_sequence != _anchor_base_sequence(anchor):
        raise ValueError("cursor anchor/base prefix high-water drifted")
    if isinstance(anchor, _RunSeedProjectionAnchor):
        return build_run_seed_cursor_base_identity(
            runtime_session_id=runtime_session_id,
            carrier=anchor.carrier,
            run_seed_semantic_fingerprint=(
                anchor.seed_semantic.seed_semantic_fingerprint
            ),
            run_seed_reference_fingerprint=(
                anchor.seed_reference.reference_fingerprint
            ),
            stable_state_semantic_fingerprint=(
                anchor.seed_semantic.prior_stable_semantic_state.state_semantic_fingerprint
            ),
            base_prefix=base_prefix,
            event_domain_registry_contract_fingerprint=registry_fingerprint,
            reducer_contract_fingerprint=(
                TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
            ),
        )
    return build_checkpoint_cursor_base_identity(
        runtime_session_id=runtime_session_id,
        carrier=anchor.carrier,
        run_seed_semantic_fingerprint=anchor.seed_semantic.seed_semantic_fingerprint,
        run_seed_reference_fingerprint=anchor.seed_reference.reference_fingerprint,
        stable_state_semantic_fingerprint=(
            anchor.stable_semantic_state.state_semantic_fingerprint
        ),
        checkpoint_id=anchor.checkpoint_id,
        checkpoint_candidate_fingerprint=anchor.checkpoint_candidate_fingerprint,
        checkpoint_materialization_fingerprint=(
            anchor.checkpoint_materialization.materialization_fingerprint
        ),
        previous_checkpoint_id=anchor.previous_checkpoint_id,
        ledger_materialization_generation=anchor.ledger_materialization_generation,
        consumer_horizon_revision=anchor.consumer_horizon_revision,
        checkpoint_build_contract_fingerprint=anchor.build_contract_fingerprint,
        base_prefix=base_prefix,
        event_domain_registry_contract_fingerprint=registry_fingerprint,
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
    )


def _semantic_source_from_state(
    *,
    stable_state: TranscriptProjectionStableSemanticStateFact,
    registry_fingerprint: str,
) -> TranscriptProjectionSemanticSourceFact:
    return build_frozen_fact(
        TranscriptProjectionSemanticSourceFact,
        schema_version="transcript_projection_semantic_source.v1",
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=registry_fingerprint,
        semantic_source_event_count=stable_state.semantic_source_event_count,
        semantic_source_accumulator=stable_state.semantic_source_accumulator,
        resulting_state_fingerprint=stable_state.state_semantic_fingerprint,
    )


def _projection_base_from_anchor(
    *,
    anchor: _ProjectionAnchor,
    delta_before: RawTranscriptDomainPrefixFact,
    delta_after: RawTranscriptDomainPrefixFact,
    registry_contract_fingerprint: str,
    requested_through_sequence: int,
) -> TranscriptProjectionBaseFact:
    base_state = (
        anchor.seed_semantic.prior_stable_semantic_state
        if isinstance(anchor, _RunSeedProjectionAnchor)
        else anchor.stable_semantic_state
    )
    semantic_identity = build_frozen_fact(
        ProjectionBaseSemanticIdentityFact,
        schema_version="projection_base_semantic_identity.v2",
        run_seed_semantic_fingerprint=(anchor.seed_semantic.seed_semantic_fingerprint),
        stable_state_semantic_fingerprint=(base_state.state_semantic_fingerprint),
    )
    common = build_frozen_fact(
        ProjectionBaseCommonFact,
        schema_version="projection_base_common.v2",
        run_seed_semantic=anchor.seed_semantic,
        run_seed_reference=anchor.seed_reference,
        stable_semantic_state=base_state,
        semantic_identity=semantic_identity,
    )
    if isinstance(anchor, _RunSeedProjectionAnchor):
        return build_frozen_fact(
            RunSeedProjectionBaseFact,
            schema_version="run_seed_projection_base.v2",
            base_kind="run_seed",
            common=common,
        )
    acceleration = build_frozen_fact(
        TranscriptProjectionAccelerationFact,
        schema_version="transcript_projection_acceleration.v1",
        scope=anchor.scope,
        checkpoint_id=anchor.checkpoint_id,
        checkpoint_committed_event_id=(anchor.checkpoint_committed_event_id),
        checkpoint_committed_event_sequence=(
            anchor.checkpoint_committed_event_sequence
        ),
        checkpoint_candidate_ledger_through_sequence=(
            anchor.checkpoint_candidate_ledger_through_sequence
        ),
        checkpoint_candidate_ledger_continuity_accumulator=(
            anchor.checkpoint_candidate_ledger_continuity_accumulator
        ),
        checkpoint_artifact_ref=(anchor.checkpoint_materialization.root_manifest_ref),
        previous_checkpoint_id=anchor.previous_checkpoint_id,
        ledger_materialization_generation=(anchor.ledger_materialization_generation),
        consumer_horizon_revision=anchor.consumer_horizon_revision,
        delta_from_sequence=(anchor.checkpoint_candidate_ledger_through_sequence + 1),
        delta_through_sequence=requested_through_sequence,
        delta_event_count=(
            requested_through_sequence
            - anchor.checkpoint_candidate_ledger_through_sequence
        ),
        delta_payload_bytes=(
            delta_after.ledger_payload_bytes - delta_before.ledger_payload_bytes
        ),
        ledger_through_sequence=requested_through_sequence,
        ledger_continuity_accumulator=delta_after.ledger_continuity_accumulator,
        event_domain_registry_contract_fingerprint=(
            registry_contract_fingerprint
        ),
        build_contract_fingerprint=anchor.build_contract_fingerprint,
    )
    return build_frozen_fact(
        CheckpointProjectionBaseFact,
        schema_version="checkpoint_projection_base.v2",
        base_kind="checkpoint",
        common=common,
        checkpoint_acceleration=acceleration,
        checkpoint_materialization=anchor.checkpoint_materialization,
    )


__all__ = [
    "PreparedTranscriptProjectionEvidence",
    "TranscriptCheckpointBlocked",
    "TranscriptProjectionCheckpointService",
]
