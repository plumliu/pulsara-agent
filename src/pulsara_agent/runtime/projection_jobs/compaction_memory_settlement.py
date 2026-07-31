"""RuntimeSession-owned atomic settlement for extraction RESULT_READY facts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from time import monotonic
from typing import Callable, Sequence, cast

from psycopg.types.json import Jsonb

from pulsara_agent.event import (
    AgentEvent,
    ContextCompactionMemoryExtractionCompletedEvent,
)
from pulsara_agent.event_log.protocol import RawStoredEventEnvelope
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    FrozenEventWriteCandidate,
    decode_event_write_candidate,
)
from pulsara_agent.event_log.serialization import stable_event_identity
from pulsara_agent.memory.compaction.settlement_support import (
    CompactionMemoryExtractionSettlementOutcome,
    CompactionMemoryExtractionSettlementWriteAttempt,
    PostgresCandidateProjectionOutbox,
    build_settlement_write_attempt,
    validate_result_candidate_outbox_plan,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    GovernanceStoredEventReferenceFact,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    build_bounded_runtime_failure_diagnostic,
)
from pulsara_agent.projection_jobs.compaction_memory import (
    CompactionMemoryModelResultAttributionFact,
    CompactionMemoryExtractionResultCandidateFact,
)
from pulsara_agent.projection_jobs.contracts import (
    CompactionMemoryExtractionProjectionResultReceiptFact,
    DurableProjectionCommitConfirmation,
    DurableProjectionJobOperationalStateFact,
    DurableProjectionJobStatus,
    DurableProjectionKind,
    DurableProjectionResultReceiptReferenceFact,
    DurableProjectionTargetHeadFact,
    build_projection_fact,
    durable_result_receipt_reference,
)
from pulsara_agent.runtime.projection_jobs.postgres_repository import (
    PostgresDurableProjectionRepository,
)
from pulsara_agent.blocking_executor import projection_maintenance_executor
from pulsara_agent.runtime.session import EventCommitError, RuntimeSession


async def _run_projection(operation, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        projection_maintenance_executor(),
        partial(operation, *args, **kwargs),
    )


def _thaw_event_candidate(
    candidate: CompactionMemoryExtractionResultCandidateFact,
) -> ContextCompactionMemoryExtractionCompletedEvent:
    frozen = candidate.producer_event_candidate
    event = decode_event_write_candidate(
        FrozenEventWriteCandidate(
            event_id=frozen.event_id,
            event_type=frozen.event_type,
            event_schema_version=frozen.event_schema_version,
            event_schema_fingerprint=frozen.event_schema_fingerprint,
            event_domain_contract_fingerprint=(
                frozen.event_domain_contract_fingerprint
            ),
            canonical_payload_bytes=(
                frozen.canonical_unsequenced_payload_utf8.encode("utf-8")
            ),
            payload_fingerprint=frozen.canonical_payload_sha256,
        )
    )
    if not isinstance(event, ContextCompactionMemoryExtractionCompletedEvent):
        raise TypeError("extraction result candidate decoded to the wrong event")
    return event


def _stored_reference(
    *, event: AgentEvent, runtime_session_id: str
) -> GovernanceStoredEventReferenceFact:
    envelope = RawStoredEventEnvelope.from_stored_event(
        event=event,
        runtime_session_id=runtime_session_id,
        schema_registry=DEFAULT_EVENT_SCHEMA_REGISTRY,
    )
    return build_frozen_fact(
        GovernanceStoredEventReferenceFact,
        schema_version="governance_stored_event_reference.v1",
        stable_identity=stable_event_identity(
            event,
            runtime_session_id=runtime_session_id,
        ),
        sequence=envelope.sequence,
        stored_envelope_fingerprint=envelope.envelope_fingerprint,
    )


@dataclass(slots=True)
class CompactionMemoryExtractionResultTransactionCompanion:
    runtime_session_id: str
    repository: PostgresDurableProjectionRepository
    outbox: PostgresCandidateProjectionOutbox
    result_candidate: CompactionMemoryExtractionResultCandidateFact
    settlement_generation: int
    receipt_reference: DurableProjectionResultReceiptReferenceFact | None = field(
        default=None,
        init=False,
    )
    target_head_revision: int | None = field(default=None, init=False)

    def apply_postgres(
        self,
        cursor,
        stored_events: Sequence[AgentEvent],
    ) -> None:
        events = tuple(
            event
            for event in stored_events
            if event.id == self.result_candidate.completed_event_id
        )
        if len(events) != 1 or not isinstance(
            events[0], ContextCompactionMemoryExtractionCompletedEvent
        ):
            raise ValueError("extraction settlement lacks exact stored result event")
        stored_event = events[0]
        expected_event = _thaw_event_candidate(self.result_candidate)
        if stable_event_identity(
            stored_event,
            runtime_session_id=self.runtime_session_id,
        ) != stable_event_identity(
            expected_event,
            runtime_session_id=self.runtime_session_id,
        ):
            raise ValueError("stored extraction result differs from RESULT_READY")

        row = cursor.execute(
            """
            SELECT * FROM durable_projection_jobs
            WHERE job_id = %s
            FOR UPDATE
            """,
            (self.result_candidate.job_id,),
        ).fetchone()
        if row is None:
            raise ValueError("extraction settlement job disappeared")
        state = self.repository._state_from_row(row)
        job_candidate = self.repository._candidate_from_row(row)
        if (
            state.status is not DurableProjectionJobStatus.SETTLEMENT_WRITING
            or state.settlement_generation != self.settlement_generation
            or job_candidate.job_semantic.job_id != self.result_candidate.job_id
            or job_candidate.job_semantic.target_key != self.result_candidate.target_key
            or job_candidate.candidate_fingerprint
            != self.result_candidate.result_owner.job_candidate_fingerprint
        ):
            raise ValueError("extraction settlement job CAS failed")
        result_row = cursor.execute(
            """
            SELECT candidate_payload, candidate_fingerprint
            FROM compaction_memory_extraction_result_candidates
            WHERE result_candidate_id = %s AND job_id = %s
            """,
            (
                self.result_candidate.result_candidate_id,
                self.result_candidate.job_id,
            ),
        ).fetchone()
        if result_row is None:
            raise ValueError("extraction RESULT_READY row disappeared")
        observed_candidate = (
            CompactionMemoryExtractionResultCandidateFact.model_validate(
                result_row["candidate_payload"]
            )
        )
        if (
            observed_candidate != self.result_candidate
            or str(result_row["candidate_fingerprint"])
            != self.result_candidate.result_candidate_fingerprint
        ):
            raise ValueError("extraction RESULT_READY row drifted")

        head = self.repository._read_head_in_connection(
            cursor,
            projection_kind=DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION,
            target_key=self.result_candidate.target_key,
            lock=True,
        )
        if (
            (head.head_fingerprint if head is not None else None)
            != self.result_candidate.expected_target_head_fingerprint
            or self.result_candidate.intended_target_head_revision
            != (1 if head is None else head.head_revision + 1)
        ):
            raise ValueError("extraction target single-assignment authority changed")

        self._validate_budget_authority(cursor, stored_event)
        rows = validate_result_candidate_outbox_plan(
            runtime_session_id=self.runtime_session_id,
            candidate=self.result_candidate,
            event=stored_event,
        )
        completed_reference = _stored_reference(
            event=stored_event,
            runtime_session_id=self.runtime_session_id,
        )
        receipt = build_frozen_fact(
            CompactionMemoryExtractionProjectionResultReceiptFact,
            schema_version=(
                "compaction_memory_extraction_projection_result_receipt.v1"
            ),
            receipt_kind="compaction_memory_extraction",
            receipt_id=self.result_candidate.receipt_id,
            projection_kind=DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION,
            job_id=self.result_candidate.job_id,
            target_key=self.result_candidate.target_key,
            source_request_event_reference=(
                job_candidate.job_semantic.source_event_reference
            ),
            completed_event_reference=completed_reference,
            completed_result_semantic_fingerprint=(
                self.result_candidate.result_semantic_fingerprint
            ),
            target_head_revision=(self.result_candidate.intended_target_head_revision),
            outbox_item_count=self.result_candidate.candidate_outbox_plan.item_count,
            outbox_item_accumulator=(
                self.result_candidate.candidate_outbox_plan.ordered_item_accumulator
            ),
            permanent_automatic_omission_count=(
                self.result_candidate.permanent_automatic_omission_count
            ),
            permanent_automatic_omission_semantic_accumulator=(
                self.result_candidate.permanent_automatic_omission_semantic_accumulator
            ),
            permanent_automatic_omission_attribution_accumulator=(
                self.result_candidate.permanent_automatic_omission_attribution_accumulator
            ),
        )
        receipt_reference = durable_result_receipt_reference(receipt)
        resulting_head = cast(
            DurableProjectionTargetHeadFact,
            build_projection_fact(
                DurableProjectionTargetHeadFact,
                schema_version="durable_projection_target_head.v1",
                projection_kind=DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION,
                target_key=self.result_candidate.target_key,
                applied_source_sequence=(
                    job_candidate.job_semantic.source_event_reference.sequence
                ),
                applied_source_event_reference_fingerprint=(
                    job_candidate.job_semantic.source_event_reference.reference_fingerprint
                ),
                applied_result_receipt_reference=receipt_reference,
                head_revision=self.result_candidate.intended_target_head_revision,
            ),
        )
        inserted = cursor.execute(
            """
            INSERT INTO durable_projection_result_receipts (
                receipt_id, receipt_kind, projection_kind, target_key,
                candidate_source_sequence, effective_source_sequence,
                result_semantic_fingerprint, receipt_payload,
                receipt_fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (receipt_id) DO NOTHING
            RETURNING receipt_id
            """,
            (
                receipt.receipt_id,
                receipt.receipt_kind,
                receipt.projection_kind.value,
                receipt.target_key,
                receipt.source_request_event_reference.sequence,
                receipt.source_request_event_reference.sequence,
                receipt.completed_result_semantic_fingerprint,
                Jsonb(receipt.model_dump(mode="json")),
                receipt.receipt_fingerprint,
            ),
        ).fetchone()
        if inserted is None:
            existing = cursor.execute(
                """
                SELECT receipt_payload, receipt_fingerprint
                FROM durable_projection_result_receipts
                WHERE receipt_id = %s
                """,
                (receipt.receipt_id,),
            ).fetchone()
            if existing is None or (
                CompactionMemoryExtractionProjectionResultReceiptFact.model_validate(
                    existing["receipt_payload"]
                )
                != receipt
                or str(existing["receipt_fingerprint"]) != receipt.receipt_fingerprint
            ):
                raise ValueError("extraction result receipt identity conflict")
        self.repository._write_target_head(
            cursor,
            expected=head,
            resulting=resulting_head,
        )
        self.outbox.insert_with_cursor(cursor, rows=rows)
        next_state = cast(
            DurableProjectionJobOperationalStateFact,
            build_projection_fact(
                DurableProjectionJobOperationalStateFact,
                schema_version="durable_projection_job_operational_state.v1",
                status=DurableProjectionJobStatus.SUCCEEDED,
                state_revision=state.state_revision + 1,
                repair_generation=state.repair_generation,
                attempt_count=state.attempt_count,
                dispatch_attempt_count=state.dispatch_attempt_count,
                settlement_generation=state.settlement_generation,
                lease_generation=state.lease_generation,
                lease_owner_id=None,
                lease_expires_at=None,
                next_attempt_at=None,
                last_failure=None,
                result_receipt_reference=receipt_reference,
            ),
        )
        self.repository._write_job_state(
            cursor,
            job_id=self.result_candidate.job_id,
            state=next_state,
        )
        self.receipt_reference = receipt_reference
        self.target_head_revision = resulting_head.head_revision

    def _validate_budget_authority(
        self,
        cursor,
        event: ContextCompactionMemoryExtractionCompletedEvent,
    ) -> None:
        attribution = event.occurrence_attribution.outcome_attribution
        rows = tuple(
            cursor.execute(
                """
                SELECT reservation_payload, reservation_fingerprint, status
                FROM background_derived_work_budget_reservations
                WHERE extraction_job_id = %s
                FOR SHARE
                """,
                (self.result_candidate.job_id,),
            ).fetchall()
        )
        if not isinstance(attribution, CompactionMemoryModelResultAttributionFact):
            if rows:
                raise ValueError("no-call extraction result has a budget reservation")
            return
        if len(rows) != 1:
            raise ValueError("model extraction result lacks one budget reservation")
        reservation = attribution.background_budget_reservation
        if (
            rows[0]["reservation_payload"] != reservation.model_dump(mode="json")
            or str(rows[0]["reservation_fingerprint"])
            != reservation.reservation_fingerprint
            or str(rows[0]["status"]) != "settled"
        ):
            raise ValueError("model extraction budget reservation drifted")
        settlement = cursor.execute(
            """
            SELECT settlement_payload, settlement_fingerprint
            FROM background_derived_work_budget_settlements
            WHERE reservation_id = %s
            """,
            (reservation.reservation_id,),
        ).fetchone()
        expected = attribution.background_budget_settlement
        if (
            settlement is None
            or settlement["settlement_payload"] != expected.model_dump(mode="json")
            or str(settlement["settlement_fingerprint"])
            != expected.settlement_fingerprint
        ):
            raise ValueError("model extraction budget settlement drifted")

    def apply_in_memory(self, stored_events: Sequence[AgentEvent]) -> None:
        del stored_events
        raise TypeError("durable extraction settlement requires PostgreSQL")


@dataclass(slots=True)
class RuntimeSessionCompactionMemoryExtractionSettlementPort:
    runtime_session: RuntimeSession
    repository: PostgresDurableProjectionRepository
    outbox: PostgresCandidateProjectionOutbox
    on_result_full: Callable[[], None] | None = None
    _owned_tasks: set[asyncio.Task] = field(default_factory=set, init=False)

    async def commit_result(
        self,
        *,
        result_candidate: CompactionMemoryExtractionResultCandidateFact,
        write_attempt: CompactionMemoryExtractionSettlementWriteAttempt,
    ) -> CompactionMemoryExtractionSettlementOutcome:
        task = asyncio.create_task(
            self._commit_result_owned(
                result_candidate=result_candidate,
                write_attempt=write_attempt,
            ),
            name=f"pulsara-extraction-settlement:{result_candidate.job_id}",
        )
        self._owned_tasks.add(task)
        task.add_done_callback(self._retire_owned_task)
        return await asyncio.shield(task)

    async def _commit_result_owned(
        self,
        *,
        result_candidate: CompactionMemoryExtractionResultCandidateFact,
        write_attempt: CompactionMemoryExtractionSettlementWriteAttempt,
    ) -> CompactionMemoryExtractionSettlementOutcome:
        identity = write_attempt.identity
        if (
            not write_attempt.active
            or identity.result_candidate_id != result_candidate.result_candidate_id
            or identity.result_candidate_fingerprint
            != result_candidate.result_candidate_fingerprint
        ):
            raise ValueError("extraction settlement write attempt is stale")
        event = _thaw_event_candidate(result_candidate)
        companion = CompactionMemoryExtractionResultTransactionCompanion(
            runtime_session_id=self.runtime_session.runtime_session_id,
            repository=self.repository,
            outbox=self.outbox,
            result_candidate=result_candidate,
            settlement_generation=identity.settlement_generation,
        )
        try:
            result = await self.runtime_session.write_events_with_deadline(
                (event,),
                deadline_monotonic=identity.deadline_monotonic,
                transaction_companion=companion,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            resolved = self.runtime_session.resolved_event_write_outcome(error)
            if resolved.status == "full" and resolved.result is not None:
                result = resolved.result
            else:
                (
                    confirmation,
                    receipt_reference,
                    head_revision,
                ) = await _run_projection(
                    self.repository.confirm_compaction_memory_settlement,
                    result_candidate,
                    deadline_monotonic=min(
                        identity.deadline_monotonic,
                        monotonic() + 10.0,
                    ),
                )
                if confirmation is DurableProjectionCommitConfirmation.FULL:
                    write_attempt.consume()
                    assert receipt_reference is not None
                    assert head_revision is not None
                    self._notify_result_full()
                    return _outcome(
                        result_candidate=result_candidate,
                        settlement_generation=identity.settlement_generation,
                        confirmation="full",
                        receipt_reference=receipt_reference,
                        target_head_revision=head_revision,
                        publication_status="unavailable",
                        reconciliation_required=True,
                    )
                reconciliation = resolved.status == "unknown" or confirmation in {
                    DurableProjectionCommitConfirmation.CONFLICT,
                    DurableProjectionCommitConfirmation.UNRESOLVED,
                }
                diagnostic = build_bounded_runtime_failure_diagnostic(
                    error=error,
                    redaction_profile_id="durable_projection_job_error.v1",
                )
                await _run_projection(
                    self.repository.defer_compaction_memory_settlement,
                    result_candidate=result_candidate,
                    settlement_generation=identity.settlement_generation,
                    failure=diagnostic,
                    delay_seconds=1.0,
                    reconciliation_required=reconciliation,
                    deadline_monotonic=monotonic() + 10.0,
                )
                write_attempt.consume()
                return _outcome(
                    result_candidate=result_candidate,
                    settlement_generation=identity.settlement_generation,
                    confirmation="unresolved" if reconciliation else "none",
                    receipt_reference=None,
                    target_head_revision=None,
                    publication_status="not_applicable",
                    reconciliation_required=reconciliation,
                )
        write_attempt.consume()
        if (
            companion.receipt_reference is None
            or companion.target_head_revision is None
        ):
            confirmation, receipt_reference, head_revision = await _run_projection(
                self.repository.confirm_compaction_memory_settlement,
                result_candidate,
                deadline_monotonic=min(
                    identity.deadline_monotonic,
                    monotonic() + 10.0,
                ),
            )
            if confirmation is not DurableProjectionCommitConfirmation.FULL:
                raise EventCommitError(
                    "extraction settlement committed without its companion receipt",
                    commit_outcome="unknown",
                    deadline_monotonic=identity.deadline_monotonic,
                )
            assert receipt_reference is not None
            assert head_revision is not None
            companion.receipt_reference = receipt_reference
            companion.target_head_revision = head_revision
        publication = result.publication_status
        self._notify_result_full()
        return _outcome(
            result_candidate=result_candidate,
            settlement_generation=identity.settlement_generation,
            confirmation="full",
            receipt_reference=companion.receipt_reference,
            target_head_revision=companion.target_head_revision,
            publication_status=publication,
            reconciliation_required=result.reconciliation_required,
        )

    def _notify_result_full(self) -> None:
        callback = self.on_result_full
        if callback is None:
            return
        try:
            callback()
        except Exception:
            # The durable outbox remains the recovery authority for a missed wake.
            return

    def _retire_owned_task(self, task: asyncio.Task) -> None:
        self._owned_tasks.discard(task)
        if task.cancelled():
            return
        task.exception()

    async def drain(self, *, deadline_monotonic: float) -> None:
        pending = tuple(self._owned_tasks)
        if not pending:
            return
        _done, active = await asyncio.wait(
            pending,
            timeout=max(0.0, deadline_monotonic - monotonic()),
        )
        if active:
            raise TimeoutError("extraction settlement physical writes did not drain")


def _outcome(
    *,
    result_candidate: CompactionMemoryExtractionResultCandidateFact,
    settlement_generation: int,
    confirmation: str,
    receipt_reference: DurableProjectionResultReceiptReferenceFact | None,
    target_head_revision: int | None,
    publication_status: str,
    reconciliation_required: bool,
) -> CompactionMemoryExtractionSettlementOutcome:
    event = _thaw_event_candidate(result_candidate)
    runtime_session_id = (
        event.occurrence_attribution.request_event_reference.stable_identity.runtime_session_id
    )
    payload = {
        "confirmation": confirmation,
        "result_candidate_id": result_candidate.result_candidate_id,
        "result_candidate_fingerprint": (result_candidate.result_candidate_fingerprint),
        "settlement_generation": settlement_generation,
        "producer_event_identity": stable_event_identity(
            event,
            runtime_session_id=runtime_session_id,
        ),
        "result_receipt_reference": receipt_reference,
        "target_head_revision": target_head_revision,
        "publication_status": publication_status,
        "runtime_session_ledger_reconciliation_required": (reconciliation_required),
    }
    return CompactionMemoryExtractionSettlementOutcome(
        **payload,
        outcome_fingerprint=context_fingerprint(
            "compaction-memory-extraction-settlement-outcome:v1",
            payload,
        ),
    )


__all__ = [
    "CompactionMemoryExtractionResultTransactionCompanion",
    "RuntimeSessionCompactionMemoryExtractionSettlementPort",
    "build_settlement_write_attempt",
]
