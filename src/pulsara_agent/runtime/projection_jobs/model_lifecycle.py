"""Atomic D3 job and background-budget companions for model lifecycle commits."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence, cast

from psycopg.types.json import Jsonb

from pulsara_agent.event import (
    AgentEvent,
    ContextCompactionMemoryExtractionRequestedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
)
from pulsara_agent.event_log.serialization import (
    freeze_event_write_candidate,
    payload_sha256,
)
from pulsara_agent.runtime.projection_jobs.compaction_budget import (
    DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
    reserve_background_budget,
    settle_background_budget,
)
from pulsara_agent.ports.model_lifecycle import (
    BackgroundModelCallAdmissionLease,
)
from pulsara_agent.ports.model_lifecycle import (
    ModelLifecycleTransactionCompanionIdentityFact,
)
from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.compaction import (
    BackgroundDerivedWorkBudgetAccountFact,
    BackgroundDerivedWorkBudgetReservationFact,
    BackgroundDerivedWorkBudgetSettlementFact,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.projection_jobs.contracts import (
    CompactionMemoryExtractionJobDeferralFact,
    DurableProjectionJobOperationalStateFact,
    DurableProjectionJobStatus,
    DurableProjectionTargetExecutionLeaseFact,
    LeasedDurableProjectionJob,
    build_projection_fact,
)
from pulsara_agent.projection_jobs.compaction_memory_policy import (
    compaction_memory_delivery_policy_from_request,
)


def _identity(
    *,
    phase: str,
    resolved_model_call_id: str,
    stable_primary_event_id: str,
    external_owner_reference_fingerprint: str,
    stable_candidate_fingerprint: str,
) -> ModelLifecycleTransactionCompanionIdentityFact:
    payload = {
        "companion_kind": "durable_derived_model_job",
        "phase": phase,
        "purpose": ModelCallPurpose.COMPACTION_MEMORY_EXTRACTION,
        "resolved_model_call_id": resolved_model_call_id,
        "stable_primary_event_id": stable_primary_event_id,
        "external_owner_reference_fingerprint": (external_owner_reference_fingerprint),
        "stable_candidate_fingerprint": stable_candidate_fingerprint,
    }
    return ModelLifecycleTransactionCompanionIdentityFact(
        **payload,
        companion_fingerprint=context_fingerprint(
            "model-lifecycle-transaction-companion:v1", payload
        ),
    )


def _state_from_row(row) -> DurableProjectionJobOperationalStateFact:
    deferral = (
        CompactionMemoryExtractionJobDeferralFact.model_validate(
            row["compaction_memory_deferral"]
        )
        if row["compaction_memory_deferral"] is not None
        else None
    )
    return cast(
        DurableProjectionJobOperationalStateFact,
        build_projection_fact(
            DurableProjectionJobOperationalStateFact,
            schema_version="durable_projection_job_operational_state.v1",
            status=DurableProjectionJobStatus(str(row["status"])),
            state_revision=int(row["state_revision"]),
            repair_generation=int(row["repair_generation"]),
            attempt_count=int(row["attempt_count"]),
            dispatch_attempt_count=int(row["dispatch_attempt_count"]),
            settlement_generation=int(row["settlement_generation"]),
            lease_generation=int(row["lease_generation"]),
            lease_owner_id=(
                str(row["lease_owner_id"])
                if row["lease_owner_id"] is not None
                else None
            ),
            lease_expires_at=row["lease_expires_at"],
            next_attempt_at=row["next_attempt_at"],
            last_failure=row["last_failure"],
            compaction_memory_deferral=deferral,
            result_receipt_reference=row["result_receipt_reference"],
        ),
    )


def _write_state(cursor, *, job_id: str, state) -> None:
    changed = cursor.execute(
        """
        UPDATE durable_projection_jobs
        SET status = %s,
            state_revision = %s,
            repair_generation = %s,
            attempt_count = %s,
            dispatch_attempt_count = %s,
            settlement_generation = %s,
            lease_generation = %s,
            lease_owner_id = %s,
            lease_expires_at = %s,
            next_attempt_at = %s,
            last_failure = %s,
            result_receipt_reference = %s,
            compaction_memory_deferral = %s,
            state_fingerprint = %s,
            updated_at = now()
        WHERE job_id = %s
        """,
        (
            state.status.value,
            state.state_revision,
            state.repair_generation,
            state.attempt_count,
            state.dispatch_attempt_count,
            state.settlement_generation,
            state.lease_generation,
            state.lease_owner_id,
            state.lease_expires_at,
            state.next_attempt_at,
            Jsonb(state.last_failure.model_dump(mode="json"))
            if state.last_failure is not None
            else None,
            Jsonb(state.result_receipt_reference.model_dump(mode="json"))
            if state.result_receipt_reference is not None
            else None,
            Jsonb(state.compaction_memory_deferral.model_dump(mode="json"))
            if state.compaction_memory_deferral is not None
            else None,
            state.state_fingerprint,
            job_id,
        ),
    ).rowcount
    if changed != 1:
        raise ValueError("background model job state update lost its authority")


def _read_account(cursor, runtime_session_id: str):
    row = cursor.execute(
        """
        SELECT policy_payload, policy_fingerprint, account_payload,
               account_fingerprint, account_revision
        FROM background_derived_work_budget_accounts
        WHERE runtime_session_id = %s
        FOR UPDATE
        """,
        (runtime_session_id,),
    ).fetchone()
    if row is None:
        raise ValueError("background budget genesis is absent from session bootstrap")
    account = BackgroundDerivedWorkBudgetAccountFact.model_validate(
        row["account_payload"]
    )
    if (
        account.account_fingerprint != str(row["account_fingerprint"])
        or account.account_revision != int(row["account_revision"])
        or account.policy_fingerprint != str(row["policy_fingerprint"])
        or row["policy_payload"]
        != DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY.model_dump(mode="json")
    ):
        raise ValueError("background budget account row drifted")
    return account


def _write_account(cursor, account: BackgroundDerivedWorkBudgetAccountFact) -> None:
    policy = DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY
    changed = cursor.execute(
        """
        UPDATE background_derived_work_budget_accounts
        SET policy_payload = %s,
            policy_fingerprint = %s,
            account_revision = %s,
            account_payload = %s,
            account_fingerprint = %s,
            updated_at = now()
        WHERE runtime_session_id = %s
        """,
        (
            Jsonb(policy.model_dump(mode="json")),
            policy.policy_fingerprint,
            account.account_revision,
            Jsonb(account.model_dump(mode="json")),
            account.account_fingerprint,
            account.runtime_session_id,
        ),
    ).rowcount
    if changed != 1:
        raise ValueError("background budget account update lost bootstrap authority")


def _read_exact_extraction_request(cursor, lease: LeasedDurableProjectionJob):
    source = lease.job.source_event_reference
    row = cursor.execute(
        """
        SELECT id, session_id, run_id, turn_id, reply_id, sequence,
               event_type, event_schema_version, event_schema_fingerprint,
               event_domain_contract_fingerprint, payload
        FROM agent_events
        WHERE session_id = %s AND id = %s
        FOR SHARE
        """,
        (source.runtime_session_id, source.event_id),
    ).fetchone()
    if row is None:
        raise ValueError("background Start source Request disappeared")
    request = ContextCompactionMemoryExtractionRequestedEvent.model_validate(
        row["payload"]
    )
    frozen = freeze_event_write_candidate(request.model_copy(update={"sequence": None}))
    comparisons = {
        "event_id": str(row["id"]) == source.event_id,
        "runtime_session_id": str(row["session_id"]) == source.runtime_session_id,
        "run_id": str(row["run_id"]) == source.run_id,
        "turn_id": str(row["turn_id"]) == source.turn_id,
        "reply_id": str(row["reply_id"]) == source.reply_id,
        "sequence": int(row["sequence"]) == source.sequence,
        "event_type": str(row["event_type"]) == source.event_type,
        "event_schema_version": (
            str(row["event_schema_version"]) == source.event_schema_version
            and frozen.event_schema_version == source.event_schema_version
        ),
        "event_schema_fingerprint": (
            str(row["event_schema_fingerprint"]) == source.event_schema_fingerprint
            and frozen.event_schema_fingerprint == source.event_schema_fingerprint
        ),
        "event_domain_contract_fingerprint": (
            str(row["event_domain_contract_fingerprint"])
            == source.event_domain_contract_fingerprint
            and frozen.event_domain_contract_fingerprint
            == source.event_domain_contract_fingerprint
        ),
        "payload_fingerprint": (
            payload_sha256(canonical_json_bytes(row["payload"]))
            == source.payload_fingerprint
        ),
    }
    mismatches = tuple(name for name, matches in comparisons.items() if not matches)
    if mismatches:
        raise ValueError(
            "background Start source Request authority drifted: "
            + ", ".join(mismatches)
        )
    expected_delivery = compaction_memory_delivery_policy_from_request(
        request.extraction_policy
    )
    if expected_delivery != lease.delivery_policy:
        raise ValueError("background Start Request/job delivery policy drifted")
    return request


@dataclass(slots=True)
class CompactionMemoryExtractionModelStartCompanion:
    lease: LeasedDurableProjectionJob
    reservation: BackgroundDerivedWorkBudgetReservationFact
    admission_lease: BackgroundModelCallAdmissionLease
    identity: ModelLifecycleTransactionCompanionIdentityFact
    resulting_state: DurableProjectionJobOperationalStateFact | None = field(
        default=None, init=False
    )
    resulting_account: BackgroundDerivedWorkBudgetAccountFact | None = field(
        default=None, init=False
    )

    def apply_postgres(self, cursor, stored_events: Sequence[AgentEvent]) -> None:
        starts = tuple(
            event
            for event in stored_events
            if isinstance(event, ModelCallStartEvent)
            and event.id == self.identity.stable_primary_event_id
        )
        if len(starts) != 1:
            raise ValueError("background Start companion lacks exact ModelCallStart")
        start = starts[0]
        request = _read_exact_extraction_request(cursor, self.lease)
        self.admission_lease.validate_model_start(
            resolved_model_call_id=start.resolved_call.resolved_model_call_id
        )
        attribution = start.compaction_memory_extraction_input_attribution
        if attribution is None:
            raise ValueError("background Start lacks extraction attribution")
        attribution_checks = {
            "purpose": (
                start.resolved_call.purpose
                is ModelCallPurpose.COMPACTION_MEMORY_EXTRACTION
            ),
            "budget_reservation": (
                attribution.background_budget_reservation == self.reservation
            ),
            "job_id": attribution.extraction_job_id == self.lease.job.job_id,
            "request_event_id": (
                attribution.request_event_reference.stable_identity.event_id
                == request.id
            ),
            "request_sequence": (
                attribution.request_event_reference.sequence
                == self.lease.job.source_event_reference.sequence
            ),
            "request_envelope": (
                attribution.request_event_reference.stored_envelope_fingerprint
                == self.lease.job.source_event_reference.stored_envelope_fingerprint
            ),
            "model_target": (
                request.extraction_policy.model_target.target_fingerprint
                == start.resolved_call.target.target_fingerprint
            ),
        }
        attribution_mismatches = tuple(
            name for name, matches in attribution_checks.items() if not matches
        )
        if attribution_mismatches:
            raise ValueError(
                "background Start event attribution drifted: "
                + ", ".join(attribution_mismatches)
            )
        row = cursor.execute(
            "SELECT * FROM durable_projection_jobs WHERE job_id = %s FOR UPDATE",
            (self.lease.job.job_id,),
        ).fetchone()
        if row is None:
            raise ValueError("background Start job disappeared")
        state = _state_from_row(row)
        target = cursor.execute(
            """
            SELECT lease_payload, lease_fingerprint
            FROM durable_projection_target_execution_leases
            WHERE projection_kind = %s AND target_key = %s
              AND owner_job_id = %s AND lease_generation = %s
              AND lease_owner_id = %s AND lease_expires_at > clock_timestamp()
            FOR UPDATE
            """,
            (
                self.lease.job.projection_kind.value,
                self.lease.job.target_key,
                self.lease.job.job_id,
                self.lease.lease_generation,
                self.lease.lease_owner_id,
            ),
        ).fetchone()
        expected_target_lease = cast(
            DurableProjectionTargetExecutionLeaseFact,
            build_projection_fact(
                DurableProjectionTargetExecutionLeaseFact,
                schema_version="durable_projection_target_execution_lease.v1",
                projection_kind=self.lease.job.projection_kind,
                target_key=self.lease.job.target_key,
                owner_job_id=self.lease.job.job_id,
                owner_source_sequence=self.lease.job.source_event_reference.sequence,
                lease_generation=self.lease.lease_generation,
                lease_owner_id=self.lease.lease_owner_id,
                lease_expires_at=self.lease.lease_expires_at,
                state_revision=self.lease.expected_state_revision,
            ),
        )
        existing = cursor.execute(
            """
            SELECT reservation_payload, reservation_fingerprint
            FROM background_derived_work_budget_reservations
            WHERE reservation_id = %s OR resolved_model_call_id = %s
            FOR UPDATE
            """,
            (
                self.reservation.reservation_id,
                start.resolved_call.resolved_model_call_id,
            ),
        ).fetchone()
        common_lease_valid = (
            state.status is DurableProjectionJobStatus.LEASED
            and state.lease_generation == self.lease.lease_generation
            and state.lease_owner_id == self.lease.lease_owner_id
            and state.attempt_count == 0
            and target is not None
            and DurableProjectionTargetExecutionLeaseFact.model_validate(
                target["lease_payload"]
            )
            == expected_target_lease
            and str(target["lease_fingerprint"])
            == expected_target_lease.lease_fingerprint
        )
        if not common_lease_valid:
            raise ValueError("background Start job/target lease CAS failed")
        if existing is not None:
            observed = BackgroundDerivedWorkBudgetReservationFact.model_validate(
                existing["reservation_payload"]
            )
            if (
                observed != self.reservation
                or observed.reservation_fingerprint
                != str(existing["reservation_fingerprint"])
                or state.dispatch_attempt_count
                != self.reservation.dispatch_attempt_ordinal
                or state.state_revision != self.lease.expected_state_revision + 1
            ):
                raise ValueError("background reservation identity conflict")
            account = _read_account(cursor, self.reservation.runtime_session_id)
            if (
                account.account_revision != self.reservation.source_account_revision + 1
                or account.open_reservation_count < 1
            ):
                raise ValueError("background reservation/account confirmation failed")
            self.resulting_state = state
            self.resulting_account = account
            return
        if (
            state.state_revision != self.lease.expected_state_revision
            or state.dispatch_attempt_count + 1
            != self.reservation.dispatch_attempt_ordinal
        ):
            raise ValueError("background Start job state CAS failed")
        account = _read_account(cursor, self.reservation.runtime_session_id)
        outcome = reserve_background_budget(
            account=account,
            policy=DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
            reservation_id=self.reservation.reservation_id,
            extraction_job_id=self.reservation.extraction_job_id,
            operation_id=self.reservation.operation_id,
            dispatch_attempt_ordinal=self.reservation.dispatch_attempt_ordinal,
            quote=self.reservation.model_call_reservation_quote,
        )
        if outcome.failure is not None or outcome.reservation != self.reservation:
            raise ValueError("background reservation no longer fits its account")
        next_state = cast(
            DurableProjectionJobOperationalStateFact,
            build_projection_fact(
                DurableProjectionJobOperationalStateFact,
                schema_version="durable_projection_job_operational_state.v1",
                status=DurableProjectionJobStatus.LEASED,
                state_revision=state.state_revision + 1,
                repair_generation=state.repair_generation,
                attempt_count=0,
                dispatch_attempt_count=self.reservation.dispatch_attempt_ordinal,
                settlement_generation=state.settlement_generation,
                lease_generation=state.lease_generation,
                lease_owner_id=state.lease_owner_id,
                lease_expires_at=state.lease_expires_at,
                next_attempt_at=None,
                last_failure=None,
                compaction_memory_deferral=None,
                result_receipt_reference=None,
            ),
        )
        _write_state(cursor, job_id=self.lease.job.job_id, state=next_state)
        _write_account(cursor, outcome.account)
        cursor.execute(
            """
            INSERT INTO background_derived_work_budget_reservations (
                reservation_id, runtime_session_id, extraction_job_id,
                operation_id, resolved_model_call_id, dispatch_attempt_ordinal,
                reservation_payload, reservation_fingerprint, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open')
            """,
            (
                self.reservation.reservation_id,
                self.reservation.runtime_session_id,
                self.reservation.extraction_job_id,
                self.reservation.operation_id,
                start.resolved_call.resolved_model_call_id,
                self.reservation.dispatch_attempt_ordinal,
                Jsonb(self.reservation.model_dump(mode="json")),
                self.reservation.reservation_fingerprint,
            ),
        )
        self.resulting_state = next_state
        self.resulting_account = outcome.account

    def apply_in_memory(self, stored_events: Sequence[AgentEvent]) -> None:
        del stored_events
        raise TypeError("durable background model jobs require PostgreSQL")


@dataclass(slots=True)
class CompactionMemoryExtractionModelTerminalCompanion:
    reservation: BackgroundDerivedWorkBudgetReservationFact
    identity: ModelLifecycleTransactionCompanionIdentityFact
    settlement: BackgroundDerivedWorkBudgetSettlementFact | None = field(
        default=None, init=False
    )
    resulting_account: BackgroundDerivedWorkBudgetAccountFact | None = field(
        default=None, init=False
    )
    reconciliation_required: bool = field(default=False, init=False)

    def apply_postgres(self, cursor, stored_events: Sequence[AgentEvent]) -> None:
        ends = tuple(
            event
            for event in stored_events
            if isinstance(event, ModelCallEndEvent)
            and event.id == self.identity.stable_primary_event_id
        )
        if len(ends) != 1:
            raise ValueError("background terminal companion lacks exact ModelCallEnd")
        model_end = ends[0]
        resolved_model_call_id = (
            self.reservation.model_call_reservation_quote.resolved_model_call_id
        )
        if (
            resolved_model_call_id is None
            or model_end.resolved_model_call_id != resolved_model_call_id
            or model_end.id != self.identity.stable_primary_event_id
        ):
            raise ValueError("background terminal model-call identity drifted")
        row = cursor.execute(
            """
            SELECT reservation_payload, reservation_fingerprint, status
            FROM background_derived_work_budget_reservations
            WHERE reservation_id = %s
            FOR UPDATE
            """,
            (self.reservation.reservation_id,),
        ).fetchone()
        if row is None:
            raise ValueError("background terminal lacks its reservation")
        observed = BackgroundDerivedWorkBudgetReservationFact.model_validate(
            row["reservation_payload"]
        )
        if (
            observed != self.reservation
            or str(row["reservation_fingerprint"])
            != self.reservation.reservation_fingerprint
        ):
            raise ValueError("background terminal reservation drifted")
        existing = cursor.execute(
            """
            SELECT settlement_payload, settlement_fingerprint
            FROM background_derived_work_budget_settlements
            WHERE reservation_id = %s
            """,
            (self.reservation.reservation_id,),
        ).fetchone()
        if existing is not None:
            settlement = BackgroundDerivedWorkBudgetSettlementFact.model_validate(
                existing["settlement_payload"]
            )
            if (
                settlement.settlement_fingerprint
                != str(existing["settlement_fingerprint"])
                or settlement.model_call_end_event_id != model_end.id
            ):
                raise ValueError("background terminal settlement conflict")
            self.settlement = settlement
            self.resulting_account = _read_account(
                cursor, self.reservation.runtime_session_id
            )
            return
        if str(row["status"]) != "open":
            raise ValueError("background terminal reservation is not open")
        account = _read_account(cursor, self.reservation.runtime_session_id)
        outcome = settle_background_budget(
            account=account,
            policy=DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
            reservation=self.reservation,
            model_end=model_end,
        )
        _write_account(cursor, outcome.account)
        if outcome.reconciliation_required:
            cursor.execute(
                """
                UPDATE background_derived_work_budget_reservations
                SET status = 'reconciliation_required', updated_at = now()
                WHERE reservation_id = %s AND status = 'open'
                """,
                (self.reservation.reservation_id,),
            )
            self.reconciliation_required = True
            self.resulting_account = outcome.account
            return
        settlement = outcome.settlement
        assert settlement is not None
        cursor.execute(
            """
            INSERT INTO background_derived_work_budget_settlements (
                settlement_fingerprint, reservation_id,
                model_call_end_event_id, settlement_payload
            ) VALUES (%s, %s, %s, %s)
            """,
            (
                settlement.settlement_fingerprint,
                self.reservation.reservation_id,
                model_end.id,
                Jsonb(settlement.model_dump(mode="json")),
            ),
        )
        changed = cursor.execute(
            """
            UPDATE background_derived_work_budget_reservations
            SET status = 'settled', updated_at = now()
            WHERE reservation_id = %s AND status = 'open'
            """,
            (self.reservation.reservation_id,),
        ).rowcount
        if changed != 1:
            raise ValueError("background reservation settlement CAS failed")
        self.settlement = settlement
        self.resulting_account = outcome.account

    def apply_in_memory(self, stored_events: Sequence[AgentEvent]) -> None:
        del stored_events
        raise TypeError("durable background model jobs require PostgreSQL")


def build_model_lifecycle_companions(
    *,
    lease: LeasedDurableProjectionJob,
    reservation: BackgroundDerivedWorkBudgetReservationFact,
    admission_lease: BackgroundModelCallAdmissionLease,
    model_call_start_event_id: str,
    model_call_end_event_id: str,
) -> tuple[
    CompactionMemoryExtractionModelStartCompanion,
    CompactionMemoryExtractionModelTerminalCompanion,
]:
    resolved_model_call_id = (
        reservation.model_call_reservation_quote.resolved_model_call_id
    )
    if not resolved_model_call_id:
        raise ValueError("background reservation quote lacks a resolved model call")
    external = context_fingerprint(
        "compaction-memory-model-job-owner-reference:v1",
        {
            "job_id": lease.job.job_id,
            "job_candidate_fingerprint": lease.job_candidate_fingerprint,
            "lease_generation": lease.lease_generation,
            "lease_owner_id": lease.lease_owner_id,
            "reservation_fingerprint": reservation.reservation_fingerprint,
        },
    )
    start_candidate = context_fingerprint(
        "compaction-memory-model-start-companion-candidate:v1",
        (external, model_call_start_event_id, reservation.reservation_fingerprint),
    )
    terminal_candidate = context_fingerprint(
        "compaction-memory-model-terminal-companion-candidate:v1",
        (external, model_call_end_event_id, reservation.reservation_fingerprint),
    )
    return (
        CompactionMemoryExtractionModelStartCompanion(
            lease=lease,
            reservation=reservation,
            admission_lease=admission_lease,
            identity=_identity(
                phase="start",
                resolved_model_call_id=resolved_model_call_id,
                stable_primary_event_id=model_call_start_event_id,
                external_owner_reference_fingerprint=external,
                stable_candidate_fingerprint=start_candidate,
            ),
        ),
        CompactionMemoryExtractionModelTerminalCompanion(
            reservation=reservation,
            identity=_identity(
                phase="terminal",
                resolved_model_call_id=resolved_model_call_id,
                stable_primary_event_id=model_call_end_event_id,
                external_owner_reference_fingerprint=external,
                stable_candidate_fingerprint=terminal_candidate,
            ),
        ),
    )


__all__ = [
    "CompactionMemoryExtractionModelStartCompanion",
    "CompactionMemoryExtractionModelTerminalCompanion",
    "build_model_lifecycle_companions",
]
