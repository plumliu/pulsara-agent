"""Repository contracts and deterministic in-memory projection state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable, cast

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.runtime.projection_jobs.contracts import (
    DurableProjectionAppliedResultReceiptFact,
    DurableProjectionArtifactResultDocumentReferenceFact,
    DurableProjectionCanonicalMutationReferenceFact,
    DurableProjectionCommitConfirmation,
    DurableProjectionFailureKind,
    DurableProjectionGraphResultDocumentReferenceFact,
    DurableProjectionJobCandidateFact,
    DurableProjectionJobOperationalStateFact,
    DurableProjectionJobStatus,
    DurableProjectionKind,
    DurableProjectionResultDocumentReferenceFact,
    DurableProjectionResultReceiptFact,
    DurableProjectionSeedCommitCandidateFact,
    DurableProjectionSeedCommitOutcome,
    DurableProjectionSeedFailureCommitCandidateFact,
    DurableProjectionSeedFailureFact,
    DurableProjectionSeedFailureResolutionFact,
    DurableProjectionSeedRepairActionFact,
    DurableProjectionSeedStateFact,
    DurableProjectionSeedWriteCandidate,
    DurableProjectionSettlementOutcome,
    DurableProjectionSupersededResultReceiptFact,
    DurableProjectionTargetAuthorityConflictFact,
    DurableProjectionTargetHeadFact,
    DurableProjectionTargetUpdatePolicy,
    DurableProjectionRepairActionFact,
    LeasedDurableProjectionJob,
    PreparedDurableProjectionArtifactDocumentFact,
    PreparedDurableProjectionGraphDocumentFact,
    PreparedDurableProjectionGraphRelationFact,
    PreparedDurableProjectionResultFact,
    ProjectionJobResultOwnerFact,
    build_projection_fact,
    durable_result_receipt_reference,
)
from pulsara_agent.runtime.projection_jobs.seeder import (
    build_seed_failure_resolution,
    build_seed_repair_action,
)


@dataclass(frozen=True, slots=True)
class DurableProjectionJobRecord:
    candidate: DurableProjectionJobCandidateFact
    state: DurableProjectionJobOperationalStateFact


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def initial_job_state() -> DurableProjectionJobOperationalStateFact:
    return cast(
        DurableProjectionJobOperationalStateFact,
        build_projection_fact(
            DurableProjectionJobOperationalStateFact,
            schema_version="durable_projection_job_operational_state.v1",
            status=DurableProjectionJobStatus.PENDING,
            state_revision=0,
            repair_generation=0,
            attempt_count=0,
            lease_generation=0,
            lease_owner_id=None,
            lease_expires_at=None,
            next_attempt_at=None,
            last_failure=None,
            result_receipt_reference=None,
        ),
    )


class InMemoryDurableProjectionRepository:
    """A lock-linearized reference implementation used by tests and local mode."""

    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._lock = RLock()
        self._jobs: dict[str, DurableProjectionJobRecord] = {}
        self._seed_states: dict[
            tuple[str, DurableProjectionKind], DurableProjectionSeedStateFact
        ] = {}
        self._seed_failures: dict[str, DurableProjectionSeedFailureFact] = {}
        self._seed_repairs: dict[
            str, DurableProjectionSeedRepairActionFact
        ] = {}
        self._seed_resolutions: dict[
            str, DurableProjectionSeedFailureResolutionFact
        ] = {}
        self._receipts: dict[str, DurableProjectionResultReceiptFact] = {}
        self._heads: dict[
            tuple[DurableProjectionKind, str], DurableProjectionTargetHeadFact
        ] = {}
        self._conflicts: dict[str, DurableProjectionTargetAuthorityConflictFact] = {}
        self._target_leases: dict[
            tuple[DurableProjectionKind, str], str
        ] = {}

    def install_seed_state(self, state: DurableProjectionSeedStateFact) -> None:
        key = (state.runtime_session_id, state.projection_kind)
        with self._lock:
            existing = self._seed_states.get(key)
            if existing is not None and existing != state:
                raise ValueError("projection seed state already has different authority")
            self._seed_states[key] = state

    def read_seed_state(
        self, runtime_session_id: str, projection_kind: DurableProjectionKind
    ) -> DurableProjectionSeedStateFact | None:
        with self._lock:
            return self._seed_states.get((runtime_session_id, projection_kind))

    def commit(
        self,
        *,
        candidate: DurableProjectionSeedWriteCandidate,
        admission_guard: object | None = None,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionSeedCommitOutcome:
        del admission_guard
        del deadline_monotonic
        if isinstance(candidate, DurableProjectionSeedFailureCommitCandidateFact):
            return self._commit_seed_failure(candidate)
        state = candidate.resulting_seed_state
        key = (state.runtime_session_id, state.projection_kind)
        with self._lock:
            current = self._seed_states.get(key)
            if current != candidate.expected_seed_state:
                return self._seed_outcome(
                    confirmation=DurableProjectionCommitConfirmation.NONE,
                    candidate=candidate,
                    committed_state=None,
                    committed_failure=None,
                    committed_resolution=None,
                    committed_jobs=(),
                )
            active_failures = tuple(
                failure
                for failure in self._seed_failures.values()
                if (
                    failure.runtime_session_id == state.runtime_session_id
                    and failure.projection_kind is state.projection_kind
                    and failure.failure_id not in self._seed_resolutions
                )
            )
            if len(active_failures) > 1:
                return self._seed_outcome(
                    confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                    candidate=candidate,
                    committed_state=None,
                    committed_failure=None,
                    committed_resolution=None,
                    committed_jobs=(),
                )
            resolution = None
            if active_failures:
                failure = active_failures[0]
                repair = self._seed_repairs.get(failure.failure_id)
                if (
                    repair is None
                    or candidate.repaired_seed_failure_fingerprint
                    != failure.failure_fingerprint
                    or candidate.seed_repair_action_fingerprint
                    != repair.action_fingerprint
                ):
                    return self._seed_outcome(
                        confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                        candidate=candidate,
                        committed_state=None,
                        committed_failure=None,
                        committed_resolution=None,
                        committed_jobs=(),
                    )
                resolution = build_seed_failure_resolution(
                    failure=failure,
                    repair_action=repair,
                    resulting_state=state,
                )
            elif candidate.repaired_seed_failure_fingerprint is not None:
                failure = next(
                    (
                        item
                        for item in self._seed_failures.values()
                        if item.failure_fingerprint
                        == candidate.repaired_seed_failure_fingerprint
                    ),
                    None,
                )
                if (
                    failure is None
                    or self._seed_resolutions.get(failure.failure_id)
                    is None
                ):
                    return self._seed_outcome(
                        confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                        candidate=candidate,
                        committed_state=None,
                        committed_failure=None,
                        committed_resolution=None,
                        committed_jobs=(),
                    )
                resolution = self._seed_resolutions[failure.failure_id]
            expected_accumulator = current.admitted_job_candidate_accumulator
            for job in candidate.ordered_job_candidates:
                expected_accumulator = context_fingerprint(
                    "durable-projection-admitted-job-candidate-accumulator:v1",
                    {
                        "previous_accumulator": expected_accumulator,
                        "job_candidate_fingerprint": job.candidate_fingerprint,
                    },
                )
            if (
                state.through_sequence != candidate.scan_horizon.through_sequence
                or state.ledger_continuity_accumulator
                != candidate.scan_horizon.ledger_continuity_accumulator
                or state.ledger_payload_prefix_bytes
                != candidate.scan_horizon.ledger_payload_prefix_bytes
                or state.transcript_semantic_prefix_count
                != candidate.scan_horizon.transcript_semantic_prefix_count
                or state.transcript_semantic_prefix_accumulator
                != candidate.scan_horizon.transcript_semantic_prefix_accumulator
                or state.admitted_job_candidate_count
                != current.admitted_job_candidate_count
                + len(candidate.ordered_job_candidates)
                or state.admitted_job_candidate_accumulator
                != expected_accumulator
                or candidate.source_event_count
                < len(candidate.ordered_job_candidates)
            ):
                return self._seed_outcome(
                    confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                    candidate=candidate,
                    committed_state=None,
                    committed_failure=None,
                    committed_resolution=None,
                    committed_jobs=(),
                )
            committed: list[str] = []
            for job in candidate.ordered_job_candidates:
                if (
                    job.job_semantic.projection_kind is not state.projection_kind
                    or job.job_semantic.source_event_reference.runtime_session_id
                    != state.runtime_session_id
                    or job.job_semantic.source_event_reference.sequence
                    > candidate.scan_horizon.through_sequence
                ):
                    return self._seed_outcome(
                        confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                        candidate=candidate,
                        committed_state=None,
                        committed_failure=None,
                        committed_resolution=None,
                        committed_jobs=(),
                    )
                existing = self._jobs.get(job.job_semantic.job_id)
                if existing is not None and existing.candidate != job:
                    return self._seed_outcome(
                        confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                        candidate=candidate,
                        committed_state=None,
                        committed_failure=None,
                        committed_resolution=None,
                        committed_jobs=(),
                    )
                if existing is None:
                    self._jobs[job.job_semantic.job_id] = DurableProjectionJobRecord(
                        candidate=job,
                        state=initial_job_state(),
                    )
                committed.append(job.job_semantic.job_id)
            self._seed_states[key] = state
            if resolution is not None:
                self._seed_resolutions[failure.failure_id] = resolution
            return self._seed_outcome(
                confirmation=DurableProjectionCommitConfirmation.FULL,
                candidate=candidate,
                committed_state=state.state_fingerprint,
                committed_failure=None,
                committed_resolution=(
                    resolution.resolution_fingerprint
                    if resolution is not None
                    else None
                ),
                committed_jobs=tuple(committed),
            )

    def commit_seed(
        self,
        candidate: DurableProjectionSeedCommitCandidateFact,
        *,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionSeedCommitOutcome:
        """Compatibility alias for the DPJ0 in-memory fixture."""

        return self.commit(
            candidate=candidate,
            deadline_monotonic=deadline_monotonic,
        )

    def _commit_seed_failure(
        self,
        candidate: DurableProjectionSeedFailureCommitCandidateFact,
    ) -> DurableProjectionSeedCommitOutcome:
        key = (candidate.runtime_session_id, candidate.projection_kind)
        with self._lock:
            current = self._seed_states.get(key)
            current_fingerprint = current.state_fingerprint if current else None
            if current_fingerprint != candidate.expected_seed_state_fingerprint:
                return self._seed_outcome(
                    confirmation=DurableProjectionCommitConfirmation.NONE,
                    candidate=candidate,
                    committed_state=None,
                    committed_failure=None,
                    committed_resolution=None,
                    committed_jobs=(),
                )
            active_failures = tuple(
                failure
                for failure in self._seed_failures.values()
                if (
                    failure.runtime_session_id == candidate.runtime_session_id
                    and failure.projection_kind is candidate.projection_kind
                    and failure.failure_id not in self._seed_resolutions
                )
            )
            if active_failures:
                active = active_failures[0]
                return self._seed_outcome(
                    confirmation=(
                        DurableProjectionCommitConfirmation.FULL
                        if len(active_failures) == 1
                        and active == candidate.failure
                        else DurableProjectionCommitConfirmation.CONFLICT
                    ),
                    candidate=candidate,
                    committed_state=None,
                    committed_failure=(
                        candidate.failure.failure_fingerprint
                        if len(active_failures) == 1
                        and active == candidate.failure
                        else None
                    ),
                    committed_resolution=None,
                    committed_jobs=(),
                )
            existing = self._seed_failures.get(candidate.failure.failure_id)
            if existing is not None and existing != candidate.failure:
                return self._seed_outcome(
                    confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                    candidate=candidate,
                    committed_state=None,
                    committed_failure=None,
                    committed_resolution=None,
                    committed_jobs=(),
                )
            self._seed_failures[candidate.failure.failure_id] = candidate.failure
            return self._seed_outcome(
                confirmation=DurableProjectionCommitConfirmation.FULL,
                candidate=candidate,
                committed_state=None,
                committed_failure=candidate.failure.failure_fingerprint,
                committed_resolution=None,
                committed_jobs=(),
            )

    def repair_seed_failure(
        self,
        *,
        failure_id: str,
        action: str,
        operator_authority_id: str,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionSeedRepairActionFact:
        del deadline_monotonic
        with self._lock:
            failure = self._seed_failures.get(failure_id)
            if failure is None or failure_id in self._seed_resolutions:
                raise KeyError(failure_id)
            state = self._seed_states.get(
                (failure.runtime_session_id, failure.projection_kind)
            )
            if state is None:
                raise ValueError("projection seed repair state is absent")
            latest = self._seed_repairs.get(failure_id)
            if latest is not None:
                authority_ids = tuple(
                    item.authority_id
                    for item in latest.authority_references
                    if item.authority_kind == "source_authority_repair"
                )
                if latest.action == action and authority_ids == (
                    operator_authority_id,
                ):
                    return latest
            repair = build_seed_repair_action(
                failure=failure,
                expected_state=state,
                action=action,
                operator_authority_id=operator_authority_id,
                repair_generation=(
                    latest.repair_generation + 1 if latest is not None else 1
                ),
                predecessor_repair_action_fingerprint=(
                    latest.action_fingerprint if latest is not None else None
                ),
            )
            self._seed_repairs[failure_id] = repair
            return repair

    def _seed_outcome(
        self,
        *,
        confirmation: DurableProjectionCommitConfirmation,
        candidate: DurableProjectionSeedWriteCandidate,
        committed_state: str | None,
        committed_failure: str | None,
        committed_resolution: str | None,
        committed_jobs: tuple[str, ...],
    ) -> DurableProjectionSeedCommitOutcome:
        return cast(
            DurableProjectionSeedCommitOutcome,
            build_projection_fact(
                DurableProjectionSeedCommitOutcome,
                schema_version="durable_projection_seed_commit_outcome.v1",
                confirmation=confirmation,
                attempted_candidate_fingerprint=candidate.candidate_fingerprint,
                committed_seed_state_fingerprint=committed_state,
                committed_seed_failure_fingerprint=committed_failure,
                committed_seed_failure_resolution_fingerprint=(
                    committed_resolution
                ),
                committed_job_ids=committed_jobs,
                failure=None,
            ),
        )

    def read_job(self, job_id: str) -> DurableProjectionJobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def claim_due(
        self,
        *,
        owner_id: str,
        limit: int,
        now: datetime | None = None,
    ) -> tuple[LeasedDurableProjectionJob, ...]:
        effective_now = now or self._clock()
        if effective_now.tzinfo is None:
            raise ValueError("projection claim clock must be timezone-aware")
        with self._lock:
            self._reclaim_expired(effective_now)
            eligible = [
                record
                for record in self._jobs.values()
                if record.state.status is DurableProjectionJobStatus.PENDING
                or (
                    record.state.status is DurableProjectionJobStatus.RETRY_WAIT
                    and record.state.next_attempt_at is not None
                    and record.state.next_attempt_at <= effective_now
                )
            ]
            selected: list[DurableProjectionJobRecord] = []
            grouped: dict[
                tuple[DurableProjectionKind, str],
                list[DurableProjectionJobRecord],
            ] = {}
            for record in eligible:
                key = (
                    record.candidate.job_semantic.projection_kind,
                    record.candidate.job_semantic.target_key,
                )
                grouped.setdefault(key, []).append(record)
            for key in sorted(grouped, key=lambda item: (item[0].value, item[1])):
                if key in self._target_leases:
                    continue
                records = grouped[key]
                policy = records[0].candidate.job_semantic.handler_contract.target_update_policy
                records.sort(
                    key=lambda item: (
                        item.candidate.job_semantic.source_event_reference.sequence,
                        item.candidate.job_semantic.job_id,
                    ),
                    reverse=(
                        policy
                        is DurableProjectionTargetUpdatePolicy.FULL_REPLACEMENT
                    ),
                )
                selected.append(records[0])
                if len(selected) >= limit:
                    break
            leases: list[LeasedDurableProjectionJob] = []
            for record in selected:
                candidate = record.candidate
                state = record.state
                lease_generation = state.lease_generation + 1
                expires = effective_now + timedelta(
                    seconds=candidate.delivery_policy.retry_policy.lease_duration_seconds
                )
                next_state = cast(
                    DurableProjectionJobOperationalStateFact,
                    build_projection_fact(
                        DurableProjectionJobOperationalStateFact,
                        schema_version="durable_projection_job_operational_state.v1",
                        status=DurableProjectionJobStatus.LEASED,
                        state_revision=state.state_revision + 1,
                        repair_generation=state.repair_generation,
                        attempt_count=state.attempt_count + 1,
                        lease_generation=lease_generation,
                        lease_owner_id=owner_id,
                        lease_expires_at=expires,
                        next_attempt_at=None,
                        last_failure=state.last_failure,
                        result_receipt_reference=None,
                    ),
                )
                self._jobs[candidate.job_semantic.job_id] = DurableProjectionJobRecord(
                    candidate=candidate,
                    state=next_state,
                )
                target_key = (
                    candidate.job_semantic.projection_kind,
                    candidate.job_semantic.target_key,
                )
                self._target_leases[target_key] = candidate.job_semantic.job_id
                lease = cast(
                    LeasedDurableProjectionJob,
                    build_projection_fact(
                        LeasedDurableProjectionJob,
                        schema_version="leased_durable_projection_job.v1",
                        job=candidate.job_semantic,
                        job_candidate_fingerprint=candidate.candidate_fingerprint,
                        activation_fingerprint=candidate.activation_fingerprint,
                        seed_contract_fingerprint=candidate.seed_contract_fingerprint,
                        delivery_policy=candidate.delivery_policy,
                        canonical_mutation_surface_plan=(
                            candidate.canonical_mutation_surface_plan
                        ),
                        expected_state_revision=next_state.state_revision,
                        repair_generation=next_state.repair_generation,
                        attempt_count=next_state.attempt_count,
                        lease_generation=lease_generation,
                        lease_owner_id=owner_id,
                        lease_expires_at=expires,
                    ),
                )
                head = self._heads.get(target_key)
                if (
                    candidate.job_semantic.handler_contract.target_update_policy
                    is DurableProjectionTargetUpdatePolicy.SINGLE_ASSIGNMENT
                    and head is not None
                ):
                    if (
                        head.applied_source_event_reference_fingerprint
                        == candidate.job_semantic.source_event_reference.reference_fingerprint
                    ):
                        receipt = self._read_applied_head_receipt(head)
                        self._terminalize(
                            lease=lease,
                            record=self._jobs[candidate.job_semantic.job_id],
                            status=DurableProjectionJobStatus.SUCCEEDED,
                            receipt=receipt,
                        )
                    else:
                        self._install_target_conflict(
                            lease=lease,
                            record=self._jobs[candidate.job_semantic.job_id],
                            existing=head,
                            reason=(
                                "single-assignment target already has "
                                "distinct source authority"
                            ),
                        )
                    continue
                leases.append(lease)
            return tuple(leases)

    def _reclaim_expired(self, now: datetime) -> None:
        for job_id, record in tuple(self._jobs.items()):
            state = record.state
            if (
                state.status is not DurableProjectionJobStatus.LEASED
                or state.lease_expires_at is None
                or state.lease_expires_at > now
            ):
                continue
            candidate = record.candidate
            self._target_leases.pop(
                (
                    candidate.job_semantic.projection_kind,
                    candidate.job_semantic.target_key,
                ),
                None,
            )
            self._jobs[job_id] = DurableProjectionJobRecord(
                candidate=candidate,
                state=cast(
                    DurableProjectionJobOperationalStateFact,
                    build_projection_fact(
                        DurableProjectionJobOperationalStateFact,
                        schema_version="durable_projection_job_operational_state.v1",
                        status=DurableProjectionJobStatus.PENDING,
                        state_revision=state.state_revision + 1,
                        repair_generation=state.repair_generation,
                        attempt_count=state.attempt_count,
                        lease_generation=state.lease_generation,
                        lease_owner_id=None,
                        lease_expires_at=None,
                        next_attempt_at=None,
                        last_failure=state.last_failure,
                        result_receipt_reference=None,
                    ),
                ),
            )

    def settle_success(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        prepared: PreparedDurableProjectionResultFact,
    ) -> DurableProjectionSettlementOutcome:
        with self._lock:
            record = self._validated_lease(lease)
            job = record.candidate.job_semantic
            if not isinstance(prepared.result_owner, ProjectionJobResultOwnerFact):
                return self._conflict_outcome(lease, "prepared owner is not a job")
            if (
                prepared.result_owner.job_id != job.job_id
                or prepared.result_owner.job_semantic_fingerprint
                != job.job_semantic_fingerprint
                or prepared.result_owner.job_candidate_fingerprint
                != record.candidate.candidate_fingerprint
                or prepared.result_owner.source_event_reference_fingerprint
                != job.source_event_reference.reference_fingerprint
                or prepared.result_semantic.projection_kind is not job.projection_kind
            ):
                return self._conflict_outcome(lease, "prepared result owner drifted")
            key = (job.projection_kind, job.target_key)
            head = self._heads.get(key)
            policy = job.handler_contract.target_update_policy
            source_sequence = job.source_event_reference.sequence
            if policy is DurableProjectionTargetUpdatePolicy.SINGLE_ASSIGNMENT:
                if head is not None:
                    receipt = self._read_applied_head_receipt(head)
                    if (
                        head.applied_source_event_reference_fingerprint
                        == job.source_event_reference.reference_fingerprint
                        and receipt.result_semantic.result_semantic_fingerprint
                        == prepared.result_semantic.result_semantic_fingerprint
                    ):
                        return self._terminalize(
                            lease=lease,
                            record=record,
                            status=DurableProjectionJobStatus.SUCCEEDED,
                            receipt=receipt,
                        )
                    return self._install_target_conflict(
                        lease=lease,
                        record=record,
                        existing=head,
                        reason="single-assignment target already has distinct authority",
                    )
            elif (
                head is not None
                and source_sequence < head.applied_source_sequence
            ):
                applied = self._read_applied_head_receipt(head)
                superseded = self._superseded_receipt(
                    lease=lease, prepared=prepared, effective=applied
                )
                self._insert_receipt(superseded)
                return self._terminalize(
                    lease=lease,
                    record=record,
                    status=DurableProjectionJobStatus.SUPERSEDED,
                    receipt=superseded,
                )
            elif (
                head is not None
                and source_sequence == head.applied_source_sequence
            ):
                receipt = self._read_applied_head_receipt(head)
                if (
                    head.applied_source_event_reference_fingerprint
                    != job.source_event_reference.reference_fingerprint
                    or receipt.result_semantic.result_semantic_fingerprint
                    != prepared.result_semantic.result_semantic_fingerprint
                ):
                    return self._install_target_conflict(
                        lease=lease,
                        record=record,
                        existing=head,
                        reason="same target sequence has distinct result authority",
                        candidate_result_semantic_fingerprint=(
                            prepared.result_semantic.result_semantic_fingerprint
                        ),
                    )
                return self._terminalize(
                    lease=lease,
                    record=record,
                    status=DurableProjectionJobStatus.SUCCEEDED,
                    receipt=receipt,
                )
            applied = self._applied_receipt(
                lease=lease,
                prepared=prepared,
                head_revision=(head.head_revision + 1 if head else 1),
            )
            self._insert_receipt(applied)
            reference = durable_result_receipt_reference(applied)
            new_head = cast(
                DurableProjectionTargetHeadFact,
                build_projection_fact(
                    DurableProjectionTargetHeadFact,
                    schema_version="durable_projection_target_head.v1",
                    projection_kind=job.projection_kind,
                    target_key=job.target_key,
                    applied_source_event_reference_fingerprint=(
                        job.source_event_reference.reference_fingerprint
                    ),
                    applied_source_sequence=source_sequence,
                    applied_result_receipt_reference=reference,
                    head_revision=applied.target_head_revision,
                ),
            )
            self._heads[key] = new_head
            return self._terminalize(
                lease=lease,
                record=record,
                status=DurableProjectionJobStatus.SUCCEEDED,
                receipt=applied,
            )

    def settle_failure(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        failure_kind: DurableProjectionFailureKind,
        error: BaseException,
        now: datetime | None = None,
    ) -> DurableProjectionSettlementOutcome:
        from pulsara_agent.primitives.runtime_event_vocabulary import (
            build_bounded_runtime_failure_diagnostic,
        )

        effective_now = now or self._clock()
        diagnostic = build_bounded_runtime_failure_diagnostic(
            error=error,
            redaction_profile_id="durable_projection_job_error.v1",
        )
        non_retryable = failure_kind in {
            DurableProjectionFailureKind.SOURCE_AUTHORITY_CONFLICT,
            DurableProjectionFailureKind.TARGET_AUTHORITY_CONFLICT,
            DurableProjectionFailureKind.REPOSITORY_AUTHORITY_CONFLICT,
            DurableProjectionFailureKind.HISTORICAL_DECODER_UNAVAILABLE,
            DurableProjectionFailureKind.HANDLER_CONTRACT_MISMATCH,
            DurableProjectionFailureKind.PROJECTION_INPUT_OVERSIZE,
            DurableProjectionFailureKind.PROJECTION_OUTPUT_OVERSIZE,
            DurableProjectionFailureKind.ATTEMPTS_EXHAUSTED,
            DurableProjectionFailureKind.RESULT_IDENTITY_CONFLICT,
            DurableProjectionFailureKind.EXTERNAL_SURFACE_CONTRACT_MISMATCH,
        }
        with self._lock:
            record = self._validated_lease(lease)
            state = record.state
            policy = record.candidate.delivery_policy.retry_policy
            exhausted = state.attempt_count >= policy.maximum_attempts
            status = (
                DurableProjectionJobStatus.DEAD_LETTER
                if non_retryable or exhausted
                else DurableProjectionJobStatus.RETRY_WAIT
            )
            next_attempt = None
            if status is DurableProjectionJobStatus.RETRY_WAIT:
                multiplier = 2 ** max(0, state.attempt_count - 1)
                delay_ms = min(
                    policy.maximum_delay_milliseconds,
                    policy.base_delay_milliseconds * multiplier,
                )
                next_attempt = effective_now + timedelta(
                    milliseconds=delay_ms
                )
            next_state = cast(
                DurableProjectionJobOperationalStateFact,
                build_projection_fact(
                    DurableProjectionJobOperationalStateFact,
                    schema_version="durable_projection_job_operational_state.v1",
                    status=status,
                    state_revision=state.state_revision + 1,
                    repair_generation=state.repair_generation,
                    attempt_count=state.attempt_count,
                    lease_generation=state.lease_generation,
                    lease_owner_id=None,
                    lease_expires_at=None,
                    next_attempt_at=next_attempt,
                    last_failure=diagnostic,
                    result_receipt_reference=None,
                ),
            )
            self._jobs[lease.job.job_id] = DurableProjectionJobRecord(
                candidate=record.candidate,
                state=next_state,
            )
            self._target_leases.pop(
                (lease.job.projection_kind, lease.job.target_key), None
            )
            confirmation = (
                DurableProjectionCommitConfirmation.CONFLICT
                if non_retryable
                else DurableProjectionCommitConfirmation.FULL
            )
            return cast(
                DurableProjectionSettlementOutcome,
                build_projection_fact(
                    DurableProjectionSettlementOutcome,
                    schema_version="durable_projection_settlement_outcome.v1",
                    confirmation=confirmation,
                    job_id=lease.job.job_id,
                    attempted_lease_fingerprint=lease.lease_fingerprint,
                    resulting_status=next_state.status,
                    resulting_state_revision=next_state.state_revision,
                    resulting_repair_generation=(
                        next_state.repair_generation
                    ),
                    result_receipt_reference=None,
                    failure=diagnostic,
                ),
            )

    def release_lease(
        self,
        lease: LeasedDurableProjectionJob,
    ) -> DurableProjectionJobOperationalStateFact:
        """Return a cancelled physical attempt to the same durable owner."""

        with self._lock:
            record = self._validated_lease(lease)
            state = record.state
            next_state = cast(
                DurableProjectionJobOperationalStateFact,
                build_projection_fact(
                    DurableProjectionJobOperationalStateFact,
                    schema_version="durable_projection_job_operational_state.v1",
                    status=DurableProjectionJobStatus.PENDING,
                    state_revision=state.state_revision + 1,
                    repair_generation=state.repair_generation,
                    attempt_count=state.attempt_count,
                    lease_generation=state.lease_generation,
                    lease_owner_id=None,
                    lease_expires_at=None,
                    next_attempt_at=None,
                    last_failure=state.last_failure,
                    result_receipt_reference=None,
                ),
            )
            self._jobs[lease.job.job_id] = DurableProjectionJobRecord(
                candidate=record.candidate,
                state=next_state,
            )
            self._target_leases.pop(
                (lease.job.projection_kind, lease.job.target_key), None
            )
            return next_state

    def apply_repair(
        self,
        action: DurableProjectionRepairActionFact,
    ) -> DurableProjectionJobOperationalStateFact:
        with self._lock:
            record = self._jobs.get(action.job_id)
            if record is None:
                raise KeyError(action.job_id)
            state = record.state
            if (
                state.status is not DurableProjectionJobStatus.DEAD_LETTER
                or state.state_revision != action.expected_state_revision
                or record.candidate.job_semantic.job_semantic_fingerprint
                != action.expected_job_semantic_fingerprint
                or state.repair_generation
                != action.expected_repair_generation
                or action.resulting_repair_generation
                != state.repair_generation + 1
            ):
                raise ValueError("projection repair CAS failed")
            next_state = cast(
                DurableProjectionJobOperationalStateFact,
                build_projection_fact(
                    DurableProjectionJobOperationalStateFact,
                    schema_version="durable_projection_job_operational_state.v1",
                    status=DurableProjectionJobStatus.PENDING,
                    state_revision=state.state_revision + 1,
                    repair_generation=action.resulting_repair_generation,
                    attempt_count=0,
                    lease_generation=state.lease_generation,
                    lease_owner_id=None,
                    lease_expires_at=None,
                    next_attempt_at=None,
                    last_failure=None,
                    result_receipt_reference=None,
                ),
            )
            self._jobs[action.job_id] = DurableProjectionJobRecord(
                candidate=record.candidate,
                state=next_state,
            )
            return next_state

    def _validated_lease(
        self, lease: LeasedDurableProjectionJob
    ) -> DurableProjectionJobRecord:
        record = self._jobs.get(lease.job.job_id)
        if record is None:
            raise ValueError("leased projection job is absent")
        state = record.state
        if (
            record.candidate.candidate_fingerprint
            != lease.job_candidate_fingerprint
            or state.status is not DurableProjectionJobStatus.LEASED
            or state.state_revision != lease.expected_state_revision
            or state.lease_generation != lease.lease_generation
            or state.lease_owner_id != lease.lease_owner_id
        ):
            raise ValueError("projection job lease is stale")
        return record

    def _applied_receipt(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        prepared: PreparedDurableProjectionResultFact,
        head_revision: int,
    ) -> DurableProjectionAppliedResultReceiptFact:
        job = lease.job
        documents = tuple(self._document_reference(item) for item in prepared.ordered_documents)
        mutations = tuple(
            cast(
                DurableProjectionCanonicalMutationReferenceFact,
                build_projection_fact(
                    DurableProjectionCanonicalMutationReferenceFact,
                    schema_version="durable_projection_canonical_mutation_reference.v1",
                    mutation_id=item.mutation_id,
                    mutation_semantic_fingerprint=(
                        item.mutation_semantic.mutation_semantic_fingerprint
                    ),
                    ordered_surface_delivery_identity_fingerprints=tuple(
                        context_fingerprint(
                            "canonical-mutation-surface-delivery-identity:v1",
                            {
                                "mutation_id": item.mutation_id,
                                "surface": surface.value,
                                "surface_plan_fingerprint": (
                                    item.surface_plan_fingerprint
                                ),
                            },
                        )
                        for surface in item.requested_surfaces
                    ),
                ),
            )
            for item in prepared.canonical_mutation_candidates
        )
        receipt_id = "projection-result-receipt:" + context_fingerprint(
            "durable-projection-applied-result-receipt-id:v1",
            {
                "projection_kind": job.projection_kind.value,
                "target_key": job.target_key,
                "source_event_reference_fingerprint": (
                    job.source_event_reference.reference_fingerprint
                ),
                "result_semantic_fingerprint": (
                    prepared.result_semantic.result_semantic_fingerprint
                ),
            },
        )
        return cast(
            DurableProjectionAppliedResultReceiptFact,
            build_projection_fact(
                DurableProjectionAppliedResultReceiptFact,
                schema_version="durable_projection_applied_result_receipt.v1",
                receipt_kind="applied",
                receipt_id=receipt_id,
                result_owner=prepared.result_owner,
                result_semantic=prepared.result_semantic,
                target_key=job.target_key,
                source_event_reference_fingerprint=(
                    job.source_event_reference.reference_fingerprint
                ),
                source_sequence=job.source_event_reference.sequence,
                target_head_revision=head_revision,
                result_document_references=documents,
                canonical_mutation_references=mutations,
            ),
        )

    def _superseded_receipt(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        prepared: PreparedDurableProjectionResultFact,
        effective: DurableProjectionAppliedResultReceiptFact,
    ) -> DurableProjectionSupersededResultReceiptFact:
        effective_ref = durable_result_receipt_reference(effective)
        receipt_id = "projection-result-receipt:" + context_fingerprint(
            "durable-projection-superseded-result-receipt-id:v1",
            {
                "projection_kind": lease.job.projection_kind.value,
                "target_key": lease.job.target_key,
                "candidate_owner_fingerprint": prepared.result_owner.owner_fingerprint,
                "candidate_source_event_reference_fingerprint": (
                    lease.job.source_event_reference.reference_fingerprint
                ),
                "effective_applied_receipt_fingerprint": (
                    effective.receipt_fingerprint
                ),
            },
        )
        return cast(
            DurableProjectionSupersededResultReceiptFact,
            build_projection_fact(
                DurableProjectionSupersededResultReceiptFact,
                schema_version="durable_projection_superseded_result_receipt.v1",
                receipt_kind="superseded",
                receipt_id=receipt_id,
                candidate_result_owner=prepared.result_owner,
                projection_kind=lease.job.projection_kind,
                target_key=lease.job.target_key,
                candidate_source_event_reference_fingerprint=(
                    lease.job.source_event_reference.reference_fingerprint
                ),
                candidate_source_sequence=(
                    lease.job.source_event_reference.sequence
                ),
                effective_applied_result_receipt_reference=effective_ref,
                target_head_revision=effective.target_head_revision,
            ),
        )

    @staticmethod
    def _document_reference(
        document: (
            PreparedDurableProjectionArtifactDocumentFact
            | PreparedDurableProjectionGraphDocumentFact
            | PreparedDurableProjectionGraphRelationFact
        ),
    ) -> DurableProjectionResultDocumentReferenceFact:
        if isinstance(document, PreparedDurableProjectionArtifactDocumentFact):
            return cast(
                DurableProjectionArtifactResultDocumentReferenceFact,
                build_projection_fact(
                    DurableProjectionArtifactResultDocumentReferenceFact,
                    schema_version="durable_projection_artifact_result_document_reference.v1",
                    document_kind="artifact",
                    semantic_document_id=document.semantic_document_id,
                    document_semantic_fingerprint=(
                        document.document_semantic_fingerprint
                    ),
                    media_type=document.media_type,
                    content_codec_contract_fingerprint=(
                        document.content_codec_contract_fingerprint
                    ),
                    metadata_contract_fingerprint=(
                        document.metadata_contract_fingerprint
                    ),
                    artifact_reference=document.artifact_reference,
                ),
            )
        if isinstance(document, PreparedDurableProjectionGraphDocumentFact):
            return cast(
                DurableProjectionGraphResultDocumentReferenceFact,
                build_projection_fact(
                    DurableProjectionGraphResultDocumentReferenceFact,
                    schema_version="durable_projection_graph_result_document_reference.v1",
                    document_kind="graph_document",
                    graph_id=document.graph_id,
                    semantic_document_id=document.semantic_document_id,
                    graph_document_type=document.graph_document_type,
                    document_semantic_fingerprint=(
                        document.document_semantic_fingerprint
                    ),
                    canonical_json_sha256=document.canonical_json_sha256,
                    canonical_json_utf8_bytes=document.canonical_json_utf8_bytes,
                    jsonld_codec_contract_fingerprint=(
                        document.jsonld_codec_contract_fingerprint
                    ),
                ),
            )
        return document.relation_reference

    def _insert_receipt(self, receipt: DurableProjectionResultReceiptFact) -> None:
        existing = self._receipts.get(receipt.receipt_id)
        if existing is not None and existing != receipt:
            raise ValueError("projection result receipt identity conflict")
        self._receipts[receipt.receipt_id] = receipt

    def _read_applied_head_receipt(
        self,
        head: DurableProjectionTargetHeadFact,
    ) -> DurableProjectionAppliedResultReceiptFact:
        reference = head.applied_result_receipt_reference
        receipt = self.read_receipt(reference.receipt_id)
        if (
            not isinstance(receipt, DurableProjectionAppliedResultReceiptFact)
            or receipt.receipt_fingerprint != reference.receipt_fingerprint
            or durable_result_receipt_reference(receipt) != reference
            or receipt.target_key != head.target_key
            or receipt.source_sequence != head.applied_source_sequence
            or receipt.source_event_reference_fingerprint
            != head.applied_source_event_reference_fingerprint
            or receipt.target_head_revision != head.head_revision
        ):
            raise ValueError("projection target head receipt rebind failed")
        return receipt

    def _terminalize(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        record: DurableProjectionJobRecord,
        status: DurableProjectionJobStatus,
        receipt: DurableProjectionResultReceiptFact,
    ) -> DurableProjectionSettlementOutcome:
        reference = durable_result_receipt_reference(receipt)
        state = record.state
        next_state = cast(
            DurableProjectionJobOperationalStateFact,
            build_projection_fact(
                DurableProjectionJobOperationalStateFact,
                schema_version="durable_projection_job_operational_state.v1",
                status=status,
                state_revision=state.state_revision + 1,
                repair_generation=state.repair_generation,
                attempt_count=state.attempt_count,
                lease_generation=state.lease_generation,
                lease_owner_id=None,
                lease_expires_at=None,
                next_attempt_at=None,
                last_failure=None,
                result_receipt_reference=reference,
            ),
        )
        self._jobs[lease.job.job_id] = DurableProjectionJobRecord(
            candidate=record.candidate,
            state=next_state,
        )
        self._target_leases.pop(
            (lease.job.projection_kind, lease.job.target_key), None
        )
        return cast(
            DurableProjectionSettlementOutcome,
            build_projection_fact(
                DurableProjectionSettlementOutcome,
                schema_version="durable_projection_settlement_outcome.v1",
                confirmation=DurableProjectionCommitConfirmation.FULL,
                job_id=lease.job.job_id,
                attempted_lease_fingerprint=lease.lease_fingerprint,
                resulting_status=status,
                resulting_state_revision=next_state.state_revision,
                resulting_repair_generation=next_state.repair_generation,
                result_receipt_reference=reference,
                failure=None,
            ),
        )

    def _install_target_conflict(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        record: DurableProjectionJobRecord,
        existing: DurableProjectionTargetHeadFact,
        reason: str,
        candidate_result_semantic_fingerprint: str | None = None,
    ) -> DurableProjectionSettlementOutcome:
        from pulsara_agent.primitives.runtime_event_vocabulary import (
            build_bounded_runtime_failure_diagnostic,
        )

        diagnostic = build_bounded_runtime_failure_diagnostic(
            error=ValueError(reason),
            redaction_profile_id="execution_evidence_projection_error.v1",
        )
        policy = lease.job.handler_contract.target_update_policy
        distinct_source = (
            existing.applied_source_event_reference_fingerprint
            != lease.job.source_event_reference.reference_fingerprint
        )
        conflict_kind = (
            "distinct_source_for_single_assignment"
            if (
                policy
                is DurableProjectionTargetUpdatePolicy.SINGLE_ASSIGNMENT
                and distinct_source
            )
            else "same_source_different_result"
        )
        if conflict_kind == "distinct_source_for_single_assignment":
            candidate_result_semantic_fingerprint = None
        elif candidate_result_semantic_fingerprint is None:
            candidate_result_semantic_fingerprint = context_fingerprint(
                "durable-projection-conflicting-result-unknown:v1",
                lease.job.job_semantic_fingerprint,
            )
        conflict_id = "projection-target-conflict:" + context_fingerprint(
            "durable-projection-target-authority-conflict-id:v1",
            {
                "projection_kind": lease.job.projection_kind.value,
                "target_key": lease.job.target_key,
                "conflict_kind": conflict_kind,
                "candidate_source_event_reference_fingerprint": (
                    lease.job.source_event_reference.reference_fingerprint
                ),
                "candidate_result_semantic_fingerprint": (
                    candidate_result_semantic_fingerprint
                ),
                "existing_head_fingerprint": existing.head_fingerprint,
                "existing_applied_result_receipt_fingerprint": (
                    existing.applied_result_receipt_reference.receipt_fingerprint
                ),
                "handler_contract_fingerprint": (
                    lease.job.handler_contract.contract_fingerprint
                ),
            },
        )
        conflict = cast(
            DurableProjectionTargetAuthorityConflictFact,
            build_projection_fact(
                DurableProjectionTargetAuthorityConflictFact,
                schema_version="durable_projection_target_authority_conflict.v1",
                conflict_id=conflict_id,
                projection_kind=lease.job.projection_kind,
                target_key=lease.job.target_key,
                target_update_policy=policy,
                conflict_kind=conflict_kind,
                candidate_source_event_reference_fingerprint=(
                    lease.job.source_event_reference.reference_fingerprint
                ),
                candidate_source_sequence=(
                    lease.job.source_event_reference.sequence
                ),
                candidate_result_semantic_fingerprint=(
                    candidate_result_semantic_fingerprint
                ),
                existing_head_fingerprint=existing.head_fingerprint,
                existing_applied_result_receipt_reference=(
                    existing.applied_result_receipt_reference
                ),
                handler_contract_fingerprint=(
                    lease.job.handler_contract.contract_fingerprint
                ),
            ),
        )
        self._conflicts[conflict_id] = conflict
        state = record.state
        next_state = cast(
            DurableProjectionJobOperationalStateFact,
            build_projection_fact(
                DurableProjectionJobOperationalStateFact,
                schema_version="durable_projection_job_operational_state.v1",
                status=DurableProjectionJobStatus.DEAD_LETTER,
                state_revision=state.state_revision + 1,
                repair_generation=state.repair_generation,
                attempt_count=state.attempt_count,
                lease_generation=state.lease_generation,
                lease_owner_id=None,
                lease_expires_at=None,
                next_attempt_at=None,
                last_failure=diagnostic,
                result_receipt_reference=None,
            ),
        )
        self._jobs[lease.job.job_id] = DurableProjectionJobRecord(
            candidate=record.candidate, state=next_state
        )
        self._target_leases.pop(
            (lease.job.projection_kind, lease.job.target_key), None
        )
        return cast(
            DurableProjectionSettlementOutcome,
            build_projection_fact(
                DurableProjectionSettlementOutcome,
                schema_version="durable_projection_settlement_outcome.v1",
                confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                job_id=lease.job.job_id,
                attempted_lease_fingerprint=lease.lease_fingerprint,
                resulting_status=DurableProjectionJobStatus.DEAD_LETTER,
                resulting_state_revision=next_state.state_revision,
                resulting_repair_generation=next_state.repair_generation,
                result_receipt_reference=None,
                failure=diagnostic,
            ),
        )

    def _conflict_outcome(
        self, lease: LeasedDurableProjectionJob, reason: str
    ) -> DurableProjectionSettlementOutcome:
        record = self._validated_lease(lease)
        head = self._heads.get((lease.job.projection_kind, lease.job.target_key))
        if head is not None:
            return self._install_target_conflict(
                lease=lease, record=record, existing=head, reason=reason
            )
        raise ValueError(reason)

    def read_receipt(self, receipt_id: str) -> DurableProjectionResultReceiptFact:
        with self._lock:
            try:
                return self._receipts[receipt_id]
            except KeyError as exc:
                raise KeyError(f"projection result receipt is absent: {receipt_id}") from exc

    def read_head(
        self, projection_kind: DurableProjectionKind, target_key: str
    ) -> DurableProjectionTargetHeadFact | None:
        with self._lock:
            return self._heads.get((projection_kind, target_key))

    def conflicts(self) -> tuple[DurableProjectionTargetAuthorityConflictFact, ...]:
        with self._lock:
            return tuple(self._conflicts[key] for key in sorted(self._conflicts))

    def jobs(self) -> tuple[DurableProjectionJobRecord, ...]:
        with self._lock:
            return tuple(self._jobs[key] for key in sorted(self._jobs))


__all__ = [
    "DurableProjectionJobRecord",
    "InMemoryDurableProjectionRepository",
    "initial_job_state",
]
