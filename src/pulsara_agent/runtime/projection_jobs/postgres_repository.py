"""PostgreSQL ownership for durable projection admission and job scheduling."""

from __future__ import annotations

from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
from time import monotonic
from typing import Any, cast

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from pulsara_agent.event import (
    ContextCompactionMemoryExtractionCompletedEvent,
    ToolResultEndEvent,
)
from pulsara_agent.runtime.projection_jobs.compaction_budget import (
    DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
    BackgroundBudgetReserveOutcome,
    reserve_background_budget,
)
from pulsara_agent.primitives.stored_event import (
    RawStoredEventEnvelope,
    RawTranscriptDomainPrefixFact,
)
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    canonical_utc_timestamp,
    context_fingerprint,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    BoundedRuntimeFailureDiagnosticFact,
    build_bounded_runtime_failure_diagnostic,
)
from pulsara_agent.primitives.long_horizon import ModelCallReservationQuoteFact
from pulsara_agent.primitives.compaction import (
    BackgroundDerivedWorkBudgetReservationFact,
    BackgroundDerivedWorkBudgetSettlementFact,
)
from pulsara_agent.projection_jobs.compaction_memory import (
    CompactionMemoryExtractionResultCandidateFact,
    ResultCandidateInstallationGuard,
    result_candidate_installation_guard,
)
from pulsara_agent.projection_jobs.contracts import (
    CompactionMemoryExtractionJobDeferralFact,
    CompactionMemoryExtractionProjectionResultReceiptFact,
    CompactionMemoryExtractionSupersededReceiptFact,
    DurableProjectionCommitConfirmation,
    DurableProjectionDeliveryPolicyFact,
    DurableProjectionFailureKind,
    DurableProjectionHandlerContractFact,
    DurableProjectionJobCandidateFact,
    DurableProjectionJobOperationalStateFact,
    DurableProjectionJobSemanticFact,
    DurableProjectionJobStatus,
    DurableProjectionKind,
    DurableProjectionKindActivationFact,
    DurableProjectionLedgerHorizonFact,
    DurableProjectionRepairActionFact,
    DurableProjectionRepairReason,
    DurableRepairAuthorityReferenceFact,
    DurableProjectionResultReceiptFact,
    DurableProjectionResultReceiptReferenceFact,
    DurableProjectionAppliedResultReceiptFact,
    DurableProjectionSettlementOutcome,
    PreparedDurableProjectionResultFact,
    CanonicalGraphRelationRowFact,
    ProjectionJobResultOwnerFact,
    DurableProjectionSeedCommitCandidateFact,
    DurableProjectionSeedCommitOutcome,
    DurableProjectionSeedFailureCommitCandidateFact,
    DurableProjectionSeedFailureFact,
    DurableProjectionSeedFailureResolutionFact,
    DurableProjectionSeedRepairActionFact,
    DurableProjectionSeedStateFact,
    DurableProjectionSeedWriteCandidate,
    DurableProjectionSessionCutoverFact,
    DurableProjectionSourceEventReferenceFact,
    DurableProjectionTargetExecutionLeaseFact,
    DurableProjectionTargetAuthorityConflictFact,
    DurableProjectionTargetHeadFact,
    DurableProjectionTargetUpdatePolicy,
    LeasedDurableProjectionJob,
    PreActivationHookResultOwnerFact,
    PreActivationProjectionCommitOutcomeFact,
    PreActivationProjectionHookContractFact,
    RuntimeWriteAdmissionGuard,
    RuntimeWriteAdmissionGuardHandle,
    build_projection_fact,
    durable_result_receipt_reference,
    projection_target_key,
)
from pulsara_agent.runtime.projection_jobs.registry import (
    DURABLE_PROJECTION_TRIGGER_REGISTRY,
)
from pulsara_agent.runtime.projection_jobs.repository import (
    DurableProjectionJobRecord,
    initial_job_state,
)
from pulsara_agent.runtime.projection_jobs.result import (
    applied_result_receipt,
    applied_result_receipt_for_source,
    exact_applied_head_receipt,
    projection_result_mutation_owner,
    superseded_result_receipt,
    superseded_result_receipt_for_owner,
    superseded_result_receipt_for_source,
    target_head_from_applied_receipt,
    validate_prepared_job_result,
    document_semantic_fingerprint,
)
from pulsara_agent.runtime.projection_jobs.seeder import (
    build_seed_commit_candidate,
    build_seed_failure_resolution,
    build_seed_repair_action,
    canonical_seed_state,
)
from pulsara_agent.runtime.projection_jobs.source import (
    BoundDurableProjectionStoredEvent,
    build_job_candidate,
    ledger_horizon,
    source_event_reference,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)
from pulsara_agent.storage.runtime_write_admission import (
    acquire_normal_runtime_write_guard,
    read_runtime_write_epoch,
)


_RECEIPT_ADAPTER = TypeAdapter(DurableProjectionResultReceiptFact)
_SEED_SCHEMA_VERSION = "durable_projection_seed_state.v1"
_MAX_SEED_EVENTS = 512
_MODEL_JOB_DEFERRAL_POLICY_FINGERPRINT = context_fingerprint(
    "compaction-memory-extraction-job-deferral-policy:v1",
    {"minimum_seconds": 1, "maximum_seconds": 5, "basis": "lease_generation"},
)
_MAX_SEED_BYTES = 8 * 1024 * 1024


class DurableProjectionSeedBlockedError(RuntimeError):
    """An unresolved seed failure owns this authority until typed repair."""


@dataclass(frozen=True, slots=True)
class ClaimedCompactionMemoryExtractionSettlement:
    result_candidate: CompactionMemoryExtractionResultCandidateFact
    state: DurableProjectionJobOperationalStateFact


@dataclass(frozen=True, slots=True)
class SessionModelWorkSchedule:
    immediate: bool
    database_now: datetime
    next_eligible_at: datetime | None


def seed_projection_checkpoint_kind(kind: DurableProjectionKind) -> str:
    return f"durable_projection_event_seed.{kind.value}"


def _json(value: Any) -> Jsonb:
    if hasattr(value, "model_dump"):
        return Jsonb(value.model_dump(mode="json"))
    return Jsonb(value)


def _set_deadline(connection: Connection, deadline_monotonic: float) -> None:
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        raise TimeoutError("durable projection database deadline exceeded")
    milliseconds = max(1, int(remaining * 1000))
    connection.execute(
        "SELECT pg_catalog.set_config('statement_timeout', %s, true)",
        (str(milliseconds),),
    )
    connection.execute(
        "SELECT pg_catalog.set_config('lock_timeout', %s, true)",
        (str(milliseconds),),
    )


def _raw_prefix_payload(
    horizon: DurableProjectionLedgerHorizonFact,
) -> dict[str, object]:
    return {
        "through_sequence": horizon.through_sequence,
        "ledger_payload_bytes": horizon.ledger_payload_prefix_bytes,
        "semantic_event_count": horizon.transcript_semantic_prefix_count,
        "semantic_accumulator": horizon.transcript_semantic_prefix_accumulator,
        "ledger_continuity_accumulator": horizon.ledger_continuity_accumulator,
    }


def _horizon_from_event_row(
    row: dict[str, object],
) -> DurableProjectionLedgerHorizonFact:
    prefix = RawTranscriptDomainPrefixFact(
        through_sequence=int(row["sequence"]),
        ledger_payload_bytes=int(row["ledger_payload_prefix_bytes"]),
        semantic_event_count=int(row["transcript_semantic_prefix_count"]),
        semantic_accumulator=str(row["transcript_semantic_prefix_accumulator"]),
        ledger_continuity_accumulator=str(row["ledger_continuity_accumulator"]),
    )
    return ledger_horizon(
        runtime_session_id=str(row["session_id"]),
        prefix=prefix,
    )


def _raw_event_from_row(row: dict[str, object]) -> RawStoredEventEnvelope:
    payload_bytes = canonical_json_bytes(row["payload"])
    values = {
        "stored_envelope_version": "stored-agent-event:v1",
        "event_id": str(row["id"]),
        "runtime_session_id": str(row["session_id"]),
        "run_id": str(row["run_id"]),
        "turn_id": str(row["turn_id"]),
        "reply_id": str(row["reply_id"]),
        "sequence": int(row["sequence"]),
        "created_at_utc": canonical_utc_timestamp(
            cast(datetime, row["created_at"]).isoformat()
        ),
        "event_type": str(row["event_type"]),
        "event_schema_version": str(row["event_schema_version"]),
        "event_schema_fingerprint": str(row["event_schema_fingerprint"]),
        "event_domain_contract_fingerprint": str(
            row["event_domain_contract_fingerprint"]
        ),
        "canonical_payload_bytes": payload_bytes,
        "payload_fingerprint": f"sha256:{sha256(payload_bytes).hexdigest()}",
    }
    return RawStoredEventEnvelope(
        **values,
        envelope_fingerprint=context_fingerprint(
            "stored-agent-event-envelope:v1",
            {
                key: value
                for key, value in values.items()
                if key != "canonical_payload_bytes"
            },
        ),
    )


def _bound_event_from_row(
    row: dict[str, object],
) -> BoundDurableProjectionStoredEvent:
    envelope = _raw_event_from_row(row)
    reference = source_event_reference(envelope)
    horizon = _horizon_from_event_row(row)
    from pulsara_agent.projection_jobs.contracts import (
        DurableProjectionStoredEventFact,
    )

    stored = cast(
        DurableProjectionStoredEventFact,
        build_projection_fact(
            DurableProjectionStoredEventFact,
            schema_version="durable_projection_stored_event.v1",
            event_reference=reference,
            canonical_payload_json_utf8=(
                envelope.canonical_payload_bytes.decode("utf-8")
            ),
            canonical_payload_utf8_bytes=len(envelope.canonical_payload_bytes),
            canonical_payload_sha256=envelope.payload_fingerprint,
        ),
    )
    return BoundDurableProjectionStoredEvent(
        envelope=envelope,
        stored_event=stored,
        source_reference=reference,
        trigger_horizon=horizon,
    )


@dataclass(slots=True)
class PostgresDurableProjectionRepository:
    """One transaction owner for seed, claim, and exact durable reads."""

    connection_provider: VerifiedPostgresConnectionProviderProtocol

    def list_kind_activations(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[DurableProjectionKindActivationFact, ...]:
        deadline = deadline_monotonic or monotonic() + 20.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            rows = tuple(
                connection.execute(
                    """
                    SELECT projection_kind, activation_payload,
                           activation_fingerprint
                    FROM durable_projection_kind_activations
                    ORDER BY projection_kind
                    """
                ).fetchall()
            )
        activations: list[DurableProjectionKindActivationFact] = []
        for row in rows:
            activation = DurableProjectionKindActivationFact.model_validate(
                row["activation_payload"]
            )
            if activation.activation_semantic.projection_kind.value != str(
                row["projection_kind"]
            ) or activation.activation_fingerprint != str(
                row["activation_fingerprint"]
            ):
                raise ValueError("projection activation row drifted")
            activations.append(activation)
        return tuple(activations)

    def list_active_seed_authorities(
        self,
        *,
        after_runtime_session_id: str | None = None,
        after_projection_kind: str | None = None,
        limit: int = 256,
        deadline_monotonic: float | None = None,
    ) -> tuple[
        tuple[
            DurableProjectionKindActivationFact,
            DurableProjectionSessionCutoverFact,
        ],
        ...,
    ]:
        """Read one stable page of active session/kind seed authorities."""

        if limit < 1 or limit > 256:
            raise ValueError("projection seed authority page limit is invalid")
        if (after_runtime_session_id is None) != (after_projection_kind is None):
            raise ValueError("projection seed authority cursor is incomplete")
        deadline = deadline_monotonic or monotonic() + 20.0
        cursor_session = after_runtime_session_id or ""
        cursor_kind = after_projection_kind or ""
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            rows = tuple(
                connection.execute(
                    """
                    SELECT a.activation_payload, c.cutover_payload
                    FROM durable_projection_kind_activations AS a
                    JOIN durable_projection_session_cutovers AS c
                      ON c.projection_kind = a.projection_kind
                    WHERE (c.runtime_session_id, c.projection_kind) > (%s, %s)
                    ORDER BY c.runtime_session_id, c.projection_kind
                    LIMIT %s
                    """,
                    (cursor_session, cursor_kind, limit),
                ).fetchall()
            )
        authorities: list[
            tuple[
                DurableProjectionKindActivationFact,
                DurableProjectionSessionCutoverFact,
            ]
        ] = []
        for row in rows:
            activation = DurableProjectionKindActivationFact.model_validate(
                row["activation_payload"]
            )
            cutover = DurableProjectionSessionCutoverFact.model_validate(
                row["cutover_payload"]
            )
            if (
                activation.activation_semantic.projection_kind
                is not cutover.projection_kind
                or activation.activation_fingerprint != cutover.activation_fingerprint
                or activation.activation_semantic.seed_contract.seed_contract_fingerprint
                != cutover.seed_contract_fingerprint
            ):
                raise ValueError(
                    "projection activation and session cutover do not join"
                )
            authorities.append((activation, cutover))
        return tuple(authorities)

    def read_active_seed_authority(
        self,
        *,
        runtime_session_id: str,
        projection_kind: DurableProjectionKind,
        deadline_monotonic: float | None = None,
    ) -> (
        tuple[
            DurableProjectionKindActivationFact,
            DurableProjectionSessionCutoverFact,
        ]
        | None
    ):
        deadline = deadline_monotonic or monotonic() + 20.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            try:
                activation, cutover = self._read_active_authority(
                    connection,
                    runtime_session_id=runtime_session_id,
                    projection_kind=projection_kind,
                    lock=False,
                )
            except ValueError as error:
                if str(error) == ("durable projection activation/cutover is absent"):
                    return None
                raise
        return cast(DurableProjectionKindActivationFact, activation), cutover

    def prepare_next_seed_candidate(
        self,
        *,
        runtime_session_id: str,
        projection_kind: DurableProjectionKind,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionSeedCommitCandidateFact | None:
        """Build one bounded page without mutating the seed checkpoint."""

        deadline = deadline_monotonic or monotonic() + 20.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            activation, cutover = self._read_active_authority(
                connection,
                runtime_session_id=runtime_session_id,
                projection_kind=projection_kind,
                lock=False,
            )
            expected = self._read_seed_state(
                connection,
                runtime_session_id=runtime_session_id,
                projection_kind=projection_kind,
                lock=False,
            )
            if expected is None:
                expected = canonical_seed_state(cutover)
            active_failure = self._read_active_seed_failure(
                connection,
                runtime_session_id=runtime_session_id,
                projection_kind=projection_kind,
                lock=False,
            )
            repair_action = (
                self._read_latest_seed_repair_action(
                    connection,
                    failure=active_failure,
                )
                if active_failure is not None
                else None
            )
            if active_failure is not None and repair_action is None:
                raise DurableProjectionSeedBlockedError(
                    "durable projection seed authority is latched by an "
                    "unresolved failure"
                )
            rows = tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           transcript_semantic_prefix_count,
                           transcript_semantic_prefix_accumulator,
                           ledger_continuity_accumulator,
                           ledger_payload_prefix_bytes,
                           created_at, payload
                    FROM agent_events
                    WHERE session_id = %s AND sequence > %s
                    ORDER BY sequence
                    LIMIT %s
                    """,
                    (
                        runtime_session_id,
                        expected.through_sequence,
                        _MAX_SEED_EVENTS,
                    ),
                ).fetchall()
            )
        if not rows:
            return None
        bounded_rows: list[dict[str, object]] = []
        for row in rows:
            row_payload_bytes = (
                int(row["ledger_payload_prefix_bytes"])
                - expected.ledger_payload_prefix_bytes
            )
            if row_payload_bytes < 0:
                raise ValueError("projection seed payload prefix moved backwards")
            if row_payload_bytes > _MAX_SEED_BYTES:
                break
            bounded_rows.append(row)
        if not bounded_rows:
            raise ValueError(
                "one projection source event exceeds the seed storage hard bound"
            )
        rows = tuple(bounded_rows)
        scan_horizon = _horizon_from_event_row(rows[-1])
        source_payload_bytes = (
            scan_horizon.ledger_payload_prefix_bytes
            - expected.ledger_payload_prefix_bytes
        )
        trigger_types = {
            item.trigger_event_type
            for item in activation.activation_semantic.seed_contract.ordered_trigger_bindings
        }
        candidates = tuple(
            build_job_candidate(
                stored=_bound_event_from_row(row),
                projection_kind=projection_kind,
                activation_fingerprint=activation.activation_fingerprint,
                trigger_registry=DURABLE_PROJECTION_TRIGGER_REGISTRY,
            )
            for row in rows
            if str(row["event_type"]) in trigger_types
        )
        return build_seed_commit_candidate(
            expected_state=expected,
            scan_horizon=scan_horizon,
            ordered_job_candidates=candidates,
            source_event_count=len(rows),
            source_payload_bytes=source_payload_bytes,
            repaired_seed_failure_fingerprint=(
                active_failure.failure_fingerprint
                if active_failure is not None
                else None
            ),
            seed_repair_action_fingerprint=(
                repair_action.action_fingerprint if repair_action is not None else None
            ),
        )

    def read_seed_state(
        self,
        runtime_session_id: str,
        projection_kind: DurableProjectionKind,
        *,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionSeedStateFact | None:
        deadline = deadline_monotonic or monotonic() + 20.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            return self._read_seed_state(
                connection,
                runtime_session_id=runtime_session_id,
                projection_kind=projection_kind,
                lock=False,
            )

    def read_active_seed_failure(
        self,
        runtime_session_id: str,
        projection_kind: DurableProjectionKind,
        *,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionSeedFailureFact | None:
        deadline = deadline_monotonic or monotonic() + 20.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            return self._read_active_seed_failure(
                connection,
                runtime_session_id=runtime_session_id,
                projection_kind=projection_kind,
                lock=False,
            )

    def repair_seed_failure(
        self,
        *,
        failure_id: str,
        action: str,
        operator_authority_id: str,
        deadline_monotonic: float,
    ) -> DurableProjectionSeedRepairActionFact:
        """Install the exact CAS authority required to cross one seed latch."""

        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            row = connection.execute(
                """
                SELECT f.failure_payload, f.failure_fingerprint
                FROM durable_projection_seed_failures AS f
                LEFT JOIN durable_projection_seed_failure_resolutions AS r
                  ON r.failure_id = f.failure_id
                WHERE f.failure_id = %s AND r.failure_id IS NULL
                """,
                (failure_id,),
            ).fetchone()
            if row is None:
                raise KeyError(failure_id)
            failure = DurableProjectionSeedFailureFact.model_validate(
                row["failure_payload"]
            )
            if failure.failure_fingerprint != str(row["failure_fingerprint"]):
                raise ValueError("projection seed failure row drifted")
            self._lock_seed_authority(
                connection,
                runtime_session_id=failure.runtime_session_id,
                projection_kind=failure.projection_kind,
            )
            if (
                self._read_active_seed_failure(
                    connection,
                    runtime_session_id=failure.runtime_session_id,
                    projection_kind=failure.projection_kind,
                    lock=False,
                )
                != failure
            ):
                raise ValueError("projection seed failure changed before repair")
            _, cutover = self._read_active_authority(
                connection,
                runtime_session_id=failure.runtime_session_id,
                projection_kind=failure.projection_kind,
                lock=True,
            )
            state = self._read_seed_state(
                connection,
                runtime_session_id=failure.runtime_session_id,
                projection_kind=failure.projection_kind,
                lock=True,
            )
            if state is None:
                state = canonical_seed_state(cutover)
            if state.state_fingerprint != failure.expected_seed_state_fingerprint:
                raise ValueError("projection seed failure/state authority drifted")
            latest = self._read_latest_seed_repair_action(
                connection,
                failure=failure,
            )
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
                generation = latest.repair_generation + 1
                predecessor = latest.action_fingerprint
            else:
                generation = 1
                predecessor = None
            repair = build_seed_repair_action(
                failure=failure,
                expected_state=state,
                action=action,
                operator_authority_id=operator_authority_id,
                repair_generation=generation,
                predecessor_repair_action_fingerprint=predecessor,
            )
            self._validate_or_issue_guard(
                connection,
                admission_guard=None,
                transaction_owner_id=repair.repair_action_id,
            )
            connection.execute(
                """
                INSERT INTO durable_projection_repair_actions (
                    repair_action_id, owner_kind, owner_id,
                    repair_generation, action_payload, action_fingerprint
                ) VALUES (%s, 'projection_seed', %s, %s, %s, %s)
                """,
                (
                    repair.repair_action_id,
                    failure.failure_id,
                    repair.repair_generation,
                    _json(repair),
                    repair.action_fingerprint,
                ),
            )
            return repair

    def commit(
        self,
        *,
        candidate: DurableProjectionSeedWriteCandidate,
        admission_guard: RuntimeWriteAdmissionGuard | None = None,
        deadline_monotonic: float,
    ) -> DurableProjectionSeedCommitOutcome:
        try:
            with self.connection_provider.connection(
                lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
                row_factory=dict_row,
                deadline_monotonic=deadline_monotonic,
            ) as connection:
                _set_deadline(connection, deadline_monotonic)
                self._validate_or_issue_guard(
                    connection,
                    admission_guard=admission_guard,
                    transaction_owner_id=(
                        "projection-seed:" + candidate.candidate_fingerprint
                    ),
                )
                self._lock_seed_authority(
                    connection,
                    runtime_session_id=candidate.runtime_session_id,
                    projection_kind=candidate.projection_kind,
                )
                if isinstance(
                    candidate,
                    DurableProjectionSeedFailureCommitCandidateFact,
                ):
                    outcome = self._commit_seed_failure(connection, candidate)
                else:
                    outcome = self._commit_seed_state(connection, candidate)
            return outcome
        except BaseException as exc:
            return self._confirm_seed_candidate(
                candidate=candidate,
                deadline_monotonic=deadline_monotonic,
                error=exc,
            )

    def commit_seed(
        self,
        candidate: DurableProjectionSeedCommitCandidateFact,
        *,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionSeedCommitOutcome:
        """Compatibility surface for shared repository fixtures."""

        return self.commit(
            candidate=candidate,
            deadline_monotonic=deadline_monotonic or monotonic() + 20.0,
        )

    def _commit_seed_state(
        self,
        connection: Connection,
        candidate: DurableProjectionSeedCommitCandidateFact,
    ) -> DurableProjectionSeedCommitOutcome:
        activation, cutover = self._read_active_authority(
            connection,
            runtime_session_id=candidate.runtime_session_id,
            projection_kind=candidate.projection_kind,
            lock=True,
        )
        expected = self._read_seed_state(
            connection,
            runtime_session_id=candidate.runtime_session_id,
            projection_kind=candidate.projection_kind,
            lock=True,
        )
        if expected is None:
            expected = canonical_seed_state(cutover)
        active_failure = self._read_active_seed_failure(
            connection,
            runtime_session_id=candidate.runtime_session_id,
            projection_kind=candidate.projection_kind,
            lock=True,
        )
        repair_resolution: DurableProjectionSeedFailureResolutionFact | None = None
        if active_failure is not None:
            repair_action = self._read_latest_seed_repair_action(
                connection,
                failure=active_failure,
            )
            if (
                repair_action is None
                or candidate.repaired_seed_failure_fingerprint
                != active_failure.failure_fingerprint
                or candidate.seed_repair_action_fingerprint
                != repair_action.action_fingerprint
                or repair_action.expected_seed_state_fingerprint
                != expected.state_fingerprint
            ):
                return self._seed_outcome(
                    DurableProjectionCommitConfirmation.CONFLICT,
                    candidate,
                )
            repair_resolution = build_seed_failure_resolution(
                failure=active_failure,
                repair_action=repair_action,
                resulting_state=candidate.resulting_seed_state,
            )
        elif candidate.repaired_seed_failure_fingerprint is not None:
            historical_failure = self._read_seed_failure_by_fingerprint(
                connection,
                candidate.repaired_seed_failure_fingerprint,
            )
            historical_action = (
                self._read_seed_repair_action_by_fingerprint(
                    connection,
                    failure=historical_failure,
                    action_fingerprint=cast(
                        str, candidate.seed_repair_action_fingerprint
                    ),
                )
                if historical_failure is not None
                else None
            )
            if historical_failure is None or historical_action is None:
                return self._seed_outcome(
                    DurableProjectionCommitConfirmation.CONFLICT,
                    candidate,
                )
            repair_resolution = build_seed_failure_resolution(
                failure=historical_failure,
                repair_action=historical_action,
                resulting_state=candidate.resulting_seed_state,
            )
            if (
                self._read_seed_failure_resolution(
                    connection,
                    failure=historical_failure,
                )
                != repair_resolution
            ):
                return self._seed_outcome(
                    DurableProjectionCommitConfirmation.CONFLICT,
                    candidate,
                )
        if expected == candidate.resulting_seed_state:
            for job in candidate.ordered_job_candidates:
                self._confirm_exact_job(connection, job)
            return self._seed_outcome(
                DurableProjectionCommitConfirmation.FULL,
                candidate,
                committed_state=candidate.resulting_seed_state.state_fingerprint,
                committed_resolution=(
                    repair_resolution.resolution_fingerprint
                    if repair_resolution is not None
                    else None
                ),
                committed_jobs=tuple(
                    item.job_semantic.job_id
                    for item in candidate.ordered_job_candidates
                ),
            )
        if expected != candidate.expected_seed_state:
            return self._seed_outcome(
                DurableProjectionCommitConfirmation.NONE,
                candidate,
            )
        if (
            cutover.cutover_fingerprint
            != candidate.expected_seed_state.cutover_fingerprint
            or activation.activation_fingerprint != cutover.activation_fingerprint
            or activation.activation_semantic.seed_contract.seed_contract_fingerprint
            != candidate.expected_seed_state.seed_contract_fingerprint
        ):
            return self._seed_outcome(
                DurableProjectionCommitConfirmation.CONFLICT,
                candidate,
            )
        rows = self._read_seed_delta_rows(
            connection,
            runtime_session_id=candidate.runtime_session_id,
            start_exclusive=expected.through_sequence,
            end_inclusive=candidate.scan_horizon.through_sequence,
        )
        expected_source_count = (
            candidate.scan_horizon.through_sequence - expected.through_sequence
        )
        expected_source_bytes = (
            candidate.scan_horizon.ledger_payload_prefix_bytes
            - expected.ledger_payload_prefix_bytes
        )
        if (
            len(rows) != expected_source_count
            or candidate.source_event_count != expected_source_count
            or candidate.source_payload_bytes != expected_source_bytes
            or expected_source_count > _MAX_SEED_EVENTS
            or expected_source_bytes > _MAX_SEED_BYTES
            or expected_source_bytes < 0
        ):
            return self._seed_outcome(
                DurableProjectionCommitConfirmation.CONFLICT,
                candidate,
            )
        observed_horizon = (
            _horizon_from_event_row(rows[-1])
            if rows
            else DurableProjectionLedgerHorizonFact.model_validate(
                candidate.scan_horizon.model_dump(mode="json")
            )
        )
        if observed_horizon != candidate.scan_horizon:
            return self._seed_outcome(
                DurableProjectionCommitConfirmation.CONFLICT,
                candidate,
            )
        trigger_types = {
            item.trigger_event_type
            for item in activation.activation_semantic.seed_contract.ordered_trigger_bindings
        }
        expected_jobs = tuple(
            build_job_candidate(
                stored=_bound_event_from_row(row),
                projection_kind=candidate.projection_kind,
                activation_fingerprint=activation.activation_fingerprint,
                trigger_registry=DURABLE_PROJECTION_TRIGGER_REGISTRY,
            )
            for row in rows
            if str(row["event_type"]) in trigger_types
        )
        if expected_jobs != candidate.ordered_job_candidates:
            return self._seed_outcome(
                DurableProjectionCommitConfirmation.CONFLICT,
                candidate,
            )
        committed_ids: list[str] = []
        for job in expected_jobs:
            self._insert_or_confirm_job(connection, job)
            committed_ids.append(job.job_semantic.job_id)
        self._write_seed_checkpoint(
            connection,
            expected_state=expected,
            resulting_state=candidate.resulting_seed_state,
            scan_horizon=candidate.scan_horizon,
        )
        if repair_resolution is not None:
            assert active_failure is not None
            self._insert_or_confirm_seed_failure_resolution(
                connection,
                failure=active_failure,
                resolution=repair_resolution,
            )
        return self._seed_outcome(
            DurableProjectionCommitConfirmation.FULL,
            candidate,
            committed_state=candidate.resulting_seed_state.state_fingerprint,
            committed_resolution=(
                repair_resolution.resolution_fingerprint
                if repair_resolution is not None
                else None
            ),
            committed_jobs=tuple(committed_ids),
        )

    def _commit_seed_failure(
        self,
        connection: Connection,
        candidate: DurableProjectionSeedFailureCommitCandidateFact,
    ) -> DurableProjectionSeedCommitOutcome:
        _, cutover = self._read_active_authority(
            connection,
            runtime_session_id=candidate.runtime_session_id,
            projection_kind=candidate.projection_kind,
            lock=True,
        )
        current = self._read_seed_state(
            connection,
            runtime_session_id=candidate.runtime_session_id,
            projection_kind=candidate.projection_kind,
            lock=True,
        )
        if current is None:
            current = canonical_seed_state(cutover)
        if current.state_fingerprint != candidate.expected_seed_state_fingerprint:
            return self._seed_outcome(
                DurableProjectionCommitConfirmation.NONE,
                candidate,
            )
        active_failure = self._read_active_seed_failure(
            connection,
            runtime_session_id=candidate.runtime_session_id,
            projection_kind=candidate.projection_kind,
            lock=True,
        )
        if active_failure is not None:
            return self._seed_outcome(
                (
                    DurableProjectionCommitConfirmation.FULL
                    if active_failure == candidate.failure
                    else DurableProjectionCommitConfirmation.CONFLICT
                ),
                candidate,
                committed_failure=(
                    candidate.failure.failure_fingerprint
                    if active_failure == candidate.failure
                    else None
                ),
            )
        payload = candidate.failure.model_dump(mode="json")
        row = connection.execute(
            """
            INSERT INTO durable_projection_seed_failures (
                failure_id, runtime_session_id, projection_kind,
                blocked_from_sequence, blocked_through_sequence,
                failure_payload, failure_fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (failure_id) DO NOTHING
            RETURNING failure_fingerprint
            """,
            (
                candidate.failure.failure_id,
                candidate.runtime_session_id,
                candidate.projection_kind.value,
                candidate.failure.blocked_from_sequence,
                candidate.failure.blocked_through_sequence,
                Jsonb(payload),
                candidate.failure.failure_fingerprint,
            ),
        ).fetchone()
        if row is None:
            existing = connection.execute(
                """
                SELECT failure_payload, failure_fingerprint
                FROM durable_projection_seed_failures
                WHERE failure_id = %s
                """,
                (candidate.failure.failure_id,),
            ).fetchone()
            if (
                existing is None
                or str(existing["failure_fingerprint"])
                != candidate.failure.failure_fingerprint
                or dict(existing["failure_payload"]) != payload
            ):
                return self._seed_outcome(
                    DurableProjectionCommitConfirmation.CONFLICT,
                    candidate,
                )
        return self._seed_outcome(
            DurableProjectionCommitConfirmation.FULL,
            candidate,
            committed_failure=candidate.failure.failure_fingerprint,
        )

    def _read_active_authority(
        self,
        connection: Connection,
        *,
        runtime_session_id: str,
        projection_kind: DurableProjectionKind,
        lock: bool,
    ) -> tuple[Any, DurableProjectionSessionCutoverFact]:
        del lock
        activation_row = connection.execute(
            """
            SELECT activation_payload
            FROM durable_projection_kind_activations
            WHERE projection_kind = %s
            """,
            (projection_kind.value,),
        ).fetchone()
        cutover_row = connection.execute(
            """
            SELECT cutover_payload
            FROM durable_projection_session_cutovers
            WHERE runtime_session_id = %s AND projection_kind = %s
            """,
            (runtime_session_id, projection_kind.value),
        ).fetchone()
        if activation_row is None or cutover_row is None:
            raise ValueError("durable projection activation/cutover is absent")
        from pulsara_agent.projection_jobs.contracts import (
            DurableProjectionKindActivationFact,
        )

        activation = DurableProjectionKindActivationFact.model_validate(
            activation_row["activation_payload"]
        )
        cutover = DurableProjectionSessionCutoverFact.model_validate(
            cutover_row["cutover_payload"]
        )
        if (
            activation.activation_semantic.projection_kind is not projection_kind
            or cutover.projection_kind is not projection_kind
            or cutover.runtime_session_id != runtime_session_id
        ):
            raise ValueError("durable projection activation/cutover drifted")
        return activation, cutover

    def _read_active_seed_failure(
        self,
        connection: Connection,
        *,
        runtime_session_id: str,
        projection_kind: DurableProjectionKind,
        lock: bool,
    ) -> DurableProjectionSeedFailureFact | None:
        del lock
        rows = connection.execute(
            """
                SELECT f.failure_id, f.failure_payload, f.failure_fingerprint
                FROM durable_projection_seed_failures AS f
                LEFT JOIN durable_projection_seed_failure_resolutions AS r
                  ON r.failure_id = f.failure_id
                WHERE f.runtime_session_id = %s
                  AND f.projection_kind = %s
                  AND r.failure_id IS NULL
                ORDER BY f.blocked_from_sequence, f.failure_id
                LIMIT 2
                """,
            (runtime_session_id, projection_kind.value),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("projection seed authority has multiple active failures")
        if not rows:
            return None
        row = rows[0]
        failure = DurableProjectionSeedFailureFact.model_validate(
            row["failure_payload"]
        )
        if (
            failure.failure_id != str(row["failure_id"])
            or failure.failure_fingerprint != str(row["failure_fingerprint"])
            or failure.runtime_session_id != runtime_session_id
            or failure.projection_kind is not projection_kind
        ):
            raise ValueError("projection seed failure row drifted")
        return failure

    @staticmethod
    def _read_latest_seed_repair_action(
        connection: Connection,
        *,
        failure: DurableProjectionSeedFailureFact,
    ) -> DurableProjectionSeedRepairActionFact | None:
        row = connection.execute(
            """
            SELECT action_payload, action_fingerprint, repair_generation
            FROM durable_projection_repair_actions
            WHERE owner_kind = 'projection_seed' AND owner_id = %s
            ORDER BY repair_generation DESC
            LIMIT 1
            """,
            (failure.failure_id,),
        ).fetchone()
        if row is None:
            return None
        action = DurableProjectionSeedRepairActionFact.model_validate(
            row["action_payload"]
        )
        if (
            action.action_fingerprint != str(row["action_fingerprint"])
            or action.repair_generation != int(row["repair_generation"])
            or action.runtime_session_id != failure.runtime_session_id
            or action.projection_kind is not failure.projection_kind
            or action.expected_seed_failure_fingerprint != failure.failure_fingerprint
            or action.expected_seed_state_fingerprint
            != failure.expected_seed_state_fingerprint
        ):
            raise ValueError("projection seed repair action row drifted")
        return action

    @staticmethod
    def _read_seed_repair_action_by_fingerprint(
        connection: Connection,
        *,
        failure: DurableProjectionSeedFailureFact,
        action_fingerprint: str,
    ) -> DurableProjectionSeedRepairActionFact | None:
        row = connection.execute(
            """
            SELECT action_payload, action_fingerprint, repair_generation
            FROM durable_projection_repair_actions
            WHERE owner_kind = 'projection_seed'
              AND owner_id = %s
              AND action_fingerprint = %s
            """,
            (failure.failure_id, action_fingerprint),
        ).fetchone()
        if row is None:
            return None
        action = DurableProjectionSeedRepairActionFact.model_validate(
            row["action_payload"]
        )
        if (
            action.action_fingerprint != str(row["action_fingerprint"])
            or action.repair_generation != int(row["repair_generation"])
            or action.expected_seed_failure_fingerprint != failure.failure_fingerprint
            or action.expected_seed_state_fingerprint
            != failure.expected_seed_state_fingerprint
        ):
            raise ValueError("projection seed repair action row drifted")
        return action

    @staticmethod
    def _read_seed_failure_resolution(
        connection: Connection,
        *,
        failure: DurableProjectionSeedFailureFact,
    ) -> DurableProjectionSeedFailureResolutionFact | None:
        row = connection.execute(
            """
            SELECT resolution_payload, resolution_fingerprint
            FROM durable_projection_seed_failure_resolutions
            WHERE failure_id = %s
            """,
            (failure.failure_id,),
        ).fetchone()
        if row is None:
            return None
        resolution = DurableProjectionSeedFailureResolutionFact.model_validate(
            row["resolution_payload"]
        )
        if (
            resolution.resolution_fingerprint != str(row["resolution_fingerprint"])
            or resolution.seed_failure_fingerprint != failure.failure_fingerprint
        ):
            raise ValueError("projection seed failure resolution row drifted")
        return resolution

    @staticmethod
    def _read_seed_failure_by_fingerprint(
        connection: Connection,
        failure_fingerprint: str,
    ) -> DurableProjectionSeedFailureFact | None:
        row = connection.execute(
            """
            SELECT failure_payload, failure_fingerprint
            FROM durable_projection_seed_failures
            WHERE failure_fingerprint = %s
            """,
            (failure_fingerprint,),
        ).fetchone()
        if row is None:
            return None
        failure = DurableProjectionSeedFailureFact.model_validate(
            row["failure_payload"]
        )
        if failure.failure_fingerprint != str(row["failure_fingerprint"]):
            raise ValueError("projection seed failure row drifted")
        return failure

    @staticmethod
    def _insert_or_confirm_seed_failure_resolution(
        connection: Connection,
        *,
        failure: DurableProjectionSeedFailureFact,
        resolution: DurableProjectionSeedFailureResolutionFact,
    ) -> None:
        inserted = connection.execute(
            """
            INSERT INTO durable_projection_seed_failure_resolutions (
                resolution_fingerprint, failure_id, resolution_payload
            ) VALUES (%s, %s, %s)
            ON CONFLICT (resolution_fingerprint) DO NOTHING
            RETURNING resolution_fingerprint
            """,
            (
                resolution.resolution_fingerprint,
                failure.failure_id,
                _json(resolution),
            ),
        ).fetchone()
        if inserted is not None:
            return
        existing = PostgresDurableProjectionRepository._read_seed_failure_resolution(
            connection,
            failure=failure,
        )
        if existing != resolution:
            raise ValueError("projection seed failure resolution conflict")

    def _read_seed_state(
        self,
        connection: Connection,
        *,
        runtime_session_id: str,
        projection_kind: DurableProjectionKind,
        lock: bool,
    ) -> DurableProjectionSeedStateFact | None:
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            """
            SELECT through_sequence, projection_schema_version, ledger_prefix,
                   validation_base_through_sequence,
                   validation_base_state_payload, state_payload,
                   payload_fingerprint
            FROM runtime_projection_checkpoints
            WHERE session_id = %s AND projection_kind = %s
            """
            + suffix,
            (
                runtime_session_id,
                seed_projection_checkpoint_kind(projection_kind),
            ),
        ).fetchone()
        if row is None:
            return None
        if str(row["projection_schema_version"]) != _SEED_SCHEMA_VERSION:
            raise ValueError("projection seed checkpoint schema drifted")
        state = DurableProjectionSeedStateFact.model_validate(row["state_payload"])
        observed_horizon = cast(
            DurableProjectionLedgerHorizonFact,
            build_projection_fact(
                DurableProjectionLedgerHorizonFact,
                schema_version="durable_projection_ledger_horizon.v1",
                runtime_session_id=state.runtime_session_id,
                through_sequence=state.through_sequence,
                ledger_continuity_accumulator=(state.ledger_continuity_accumulator),
                ledger_payload_prefix_bytes=state.ledger_payload_prefix_bytes,
                transcript_semantic_prefix_count=(
                    state.transcript_semantic_prefix_count
                ),
                transcript_semantic_prefix_accumulator=(
                    state.transcript_semantic_prefix_accumulator
                ),
            ),
        )
        if (
            state.runtime_session_id != runtime_session_id
            or state.projection_kind is not projection_kind
            or state.through_sequence != int(row["through_sequence"])
            or state.state_fingerprint != str(row["payload_fingerprint"])
            or _raw_prefix_payload(observed_horizon) != dict(row["ledger_prefix"])
        ):
            raise ValueError("projection seed checkpoint authority drifted")
        return state

    def _read_seed_delta_rows(
        self,
        connection: Connection,
        *,
        runtime_session_id: str,
        start_exclusive: int,
        end_inclusive: int,
    ) -> tuple[dict[str, object], ...]:
        if end_inclusive == start_exclusive:
            return ()
        rows = connection.execute(
            """
            SELECT id, session_id, run_id, turn_id, reply_id, sequence,
                   event_type, event_schema_version,
                   event_schema_fingerprint,
                   event_domain_contract_fingerprint,
                   transcript_semantic_prefix_count,
                   transcript_semantic_prefix_accumulator,
                   ledger_continuity_accumulator,
                   ledger_payload_prefix_bytes,
                   created_at, payload
            FROM agent_events
            WHERE session_id = %s
              AND sequence > %s
              AND sequence <= %s
            ORDER BY sequence
            LIMIT %s
            """,
            (
                runtime_session_id,
                start_exclusive,
                end_inclusive,
                _MAX_SEED_EVENTS + 1,
            ),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def _write_seed_checkpoint(
        self,
        connection: Connection,
        *,
        expected_state: DurableProjectionSeedStateFact,
        resulting_state: DurableProjectionSeedStateFact,
        scan_horizon: DurableProjectionLedgerHorizonFact,
    ) -> None:
        checkpoint_kind = seed_projection_checkpoint_kind(
            resulting_state.projection_kind
        )
        state_payload = resulting_state.model_dump(mode="json")
        connection.execute(
            """
            INSERT INTO runtime_projection_checkpoints (
                session_id, projection_kind, through_sequence,
                projection_schema_version, ledger_prefix,
                validation_base_through_sequence,
                validation_base_state_payload,
                payload_fingerprint, state_payload, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (session_id, projection_kind) DO UPDATE SET
                through_sequence = EXCLUDED.through_sequence,
                projection_schema_version = EXCLUDED.projection_schema_version,
                ledger_prefix = EXCLUDED.ledger_prefix,
                validation_base_through_sequence =
                    EXCLUDED.validation_base_through_sequence,
                validation_base_state_payload =
                    EXCLUDED.validation_base_state_payload,
                payload_fingerprint = EXCLUDED.payload_fingerprint,
                state_payload = EXCLUDED.state_payload,
                updated_at = now()
            """,
            (
                resulting_state.runtime_session_id,
                checkpoint_kind,
                resulting_state.through_sequence,
                _SEED_SCHEMA_VERSION,
                Jsonb(_raw_prefix_payload(scan_horizon)),
                expected_state.through_sequence,
                _json(expected_state),
                resulting_state.state_fingerprint,
                Jsonb(state_payload),
            ),
        )

    def _insert_or_confirm_job(
        self,
        connection: Connection,
        candidate: DurableProjectionJobCandidateFact,
    ) -> None:
        job = candidate.job_semantic
        state = initial_job_state()
        inserted = connection.execute(
            """
            INSERT INTO durable_projection_jobs (
                job_id, projection_kind, target_key, runtime_session_id,
                run_id, source_event_id, source_sequence, source_event_type,
                source_reference, trigger_horizon, handler_contract,
                handler_contract_fingerprint, activation_fingerprint,
                seed_contract_fingerprint, delivery_policy,
                delivery_policy_fingerprint,
                canonical_mutation_surface_plan,
                canonical_mutation_surface_plan_fingerprint,
                job_semantic_fingerprint, job_candidate_fingerprint,
                status, state_revision, repair_generation, attempt_count,
                dispatch_attempt_count, settlement_generation,
                lease_generation, lease_owner_id, lease_expires_at,
                next_attempt_at, last_failure, result_receipt_reference,
                compaction_memory_deferral, state_fingerprint
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, NULL, NULL, NULL, NULL, NULL,
                NULL, %s
            )
            ON CONFLICT (job_id) DO NOTHING
            RETURNING job_id
            """,
            (
                job.job_id,
                job.projection_kind.value,
                job.target_key,
                job.source_event_reference.runtime_session_id,
                job.source_event_reference.run_id,
                job.source_event_reference.event_id,
                job.source_event_reference.sequence,
                job.source_event_reference.event_type,
                _json(job.source_event_reference),
                _json(job.trigger_horizon),
                _json(job.handler_contract),
                job.handler_contract.contract_fingerprint,
                candidate.activation_fingerprint,
                candidate.seed_contract_fingerprint,
                _json(candidate.delivery_policy),
                candidate.delivery_policy.delivery_policy_fingerprint,
                _json(candidate.canonical_mutation_surface_plan),
                candidate.canonical_mutation_surface_plan.plan_fingerprint,
                job.job_semantic_fingerprint,
                candidate.candidate_fingerprint,
                state.status.value,
                state.state_revision,
                state.repair_generation,
                state.attempt_count,
                state.dispatch_attempt_count,
                state.settlement_generation,
                state.lease_generation,
                state.state_fingerprint,
            ),
        ).fetchone()
        if inserted is None:
            self._confirm_exact_job(connection, candidate)

    def _confirm_exact_job(
        self,
        connection: Connection,
        candidate: DurableProjectionJobCandidateFact,
    ) -> None:
        existing = self._read_job_row(
            connection,
            candidate.job_semantic.job_id,
            lock=True,
        )
        if existing is None or self._candidate_from_row(existing) != candidate:
            raise ValueError("durable projection job identity conflict")

    @staticmethod
    def _lock_seed_authority(
        connection: Connection,
        *,
        runtime_session_id: str,
        projection_kind: DurableProjectionKind,
    ) -> None:
        connection.execute(
            """
            SELECT pg_advisory_xact_lock(
                hashtextextended(
                    'pulsara-projection-seed:'
                    || %s || ':' || %s,
                    0
                )
            )
            """,
            (runtime_session_id, projection_kind.value),
        ).fetchone()

    def _validate_or_issue_guard(
        self,
        connection: Connection,
        *,
        admission_guard: RuntimeWriteAdmissionGuard | None,
        transaction_owner_id: str,
    ) -> RuntimeWriteAdmissionGuardHandle:
        epoch = read_runtime_write_epoch(connection)
        issued = acquire_normal_runtime_write_guard(
            connection,
            expected_epoch=epoch,
            transaction_owner_id=transaction_owner_id,
        )
        if admission_guard is not None and (
            admission_guard.admission_epoch.epoch_fingerprint
            != issued.admission_epoch.epoch_fingerprint
            or admission_guard.maintenance_authority_fingerprint is not None
        ):
            raise ValueError("projection seed admission guard drifted")
        return issued

    def _confirm_seed_candidate(
        self,
        *,
        candidate: DurableProjectionSeedWriteCandidate,
        deadline_monotonic: float,
        error: BaseException,
    ) -> DurableProjectionSeedCommitOutcome:
        if monotonic() >= deadline_monotonic:
            return self._seed_outcome(
                DurableProjectionCommitConfirmation.UNRESOLVED,
                candidate,
                failure=_diagnostic(error),
            )
        try:
            if isinstance(
                candidate,
                DurableProjectionSeedFailureCommitCandidateFact,
            ):
                with self.connection_provider.connection(
                    lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
                    row_factory=dict_row,
                    deadline_monotonic=deadline_monotonic,
                ) as connection:
                    _set_deadline(connection, deadline_monotonic)
                    row = connection.execute(
                        """
                        SELECT failure_payload, failure_fingerprint
                        FROM durable_projection_seed_failures
                        WHERE failure_id = %s
                        """,
                        (candidate.failure.failure_id,),
                    ).fetchone()
                if row is None:
                    return self._seed_outcome(
                        DurableProjectionCommitConfirmation.NONE,
                        candidate,
                        failure=_diagnostic(error),
                    )
                if str(
                    row["failure_fingerprint"]
                ) == candidate.failure.failure_fingerprint and dict(
                    row["failure_payload"]
                ) == candidate.failure.model_dump(mode="json"):
                    return self._seed_outcome(
                        DurableProjectionCommitConfirmation.FULL,
                        candidate,
                        committed_failure=(candidate.failure.failure_fingerprint),
                    )
                return self._seed_outcome(
                    DurableProjectionCommitConfirmation.CONFLICT,
                    candidate,
                    failure=_diagnostic(error),
                )
            current = self.read_seed_state(
                candidate.runtime_session_id,
                candidate.projection_kind,
                deadline_monotonic=deadline_monotonic,
            )
            if current is None:
                with self.connection_provider.connection(
                    lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
                    row_factory=dict_row,
                    deadline_monotonic=deadline_monotonic,
                ) as connection:
                    _set_deadline(connection, deadline_monotonic)
                    _, cutover = self._read_active_authority(
                        connection,
                        runtime_session_id=candidate.runtime_session_id,
                        projection_kind=candidate.projection_kind,
                        lock=False,
                    )
                current = canonical_seed_state(cutover)
            if current == candidate.resulting_seed_state:
                with self.connection_provider.connection(
                    lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
                    row_factory=dict_row,
                    deadline_monotonic=deadline_monotonic,
                ) as connection:
                    _set_deadline(connection, deadline_monotonic)
                    jobs = tuple(
                        self._candidate_from_row(
                            cast(
                                dict[str, object],
                                self._read_job_row(
                                    connection,
                                    item.job_semantic.job_id,
                                    lock=False,
                                ),
                            )
                        )
                        for item in candidate.ordered_job_candidates
                    )
                    committed_resolution: str | None = None
                    if candidate.repaired_seed_failure_fingerprint is not None:
                        repaired_failure = self._read_seed_failure_by_fingerprint(
                            connection,
                            candidate.repaired_seed_failure_fingerprint,
                        )
                        repaired_action = (
                            self._read_seed_repair_action_by_fingerprint(
                                connection,
                                failure=repaired_failure,
                                action_fingerprint=cast(
                                    str,
                                    candidate.seed_repair_action_fingerprint,
                                ),
                            )
                            if repaired_failure is not None
                            else None
                        )
                        if repaired_failure is None or repaired_action is None:
                            return self._seed_outcome(
                                DurableProjectionCommitConfirmation.CONFLICT,
                                candidate,
                                failure=_diagnostic(error),
                            )
                        expected_resolution = build_seed_failure_resolution(
                            failure=repaired_failure,
                            repair_action=repaired_action,
                            resulting_state=candidate.resulting_seed_state,
                        )
                        if (
                            self._read_seed_failure_resolution(
                                connection,
                                failure=repaired_failure,
                            )
                            != expected_resolution
                        ):
                            return self._seed_outcome(
                                DurableProjectionCommitConfirmation.CONFLICT,
                                candidate,
                                failure=_diagnostic(error),
                            )
                        committed_resolution = (
                            expected_resolution.resolution_fingerprint
                        )
                if jobs == candidate.ordered_job_candidates:
                    return self._seed_outcome(
                        DurableProjectionCommitConfirmation.FULL,
                        candidate,
                        committed_state=current.state_fingerprint,
                        committed_resolution=committed_resolution,
                        committed_jobs=tuple(item.job_semantic.job_id for item in jobs),
                    )
                return self._seed_outcome(
                    DurableProjectionCommitConfirmation.CONFLICT,
                    candidate,
                    failure=_diagnostic(error),
                )
            if current == candidate.expected_seed_state:
                return self._seed_outcome(
                    DurableProjectionCommitConfirmation.NONE,
                    candidate,
                    failure=_diagnostic(error),
                )
            return self._seed_outcome(
                DurableProjectionCommitConfirmation.CONFLICT,
                candidate,
                failure=_diagnostic(error),
            )
        except BaseException as confirmation_error:
            return self._seed_outcome(
                DurableProjectionCommitConfirmation.UNRESOLVED,
                candidate,
                failure=_diagnostic(confirmation_error),
            )

    @staticmethod
    def _seed_outcome(
        confirmation: DurableProjectionCommitConfirmation,
        candidate: DurableProjectionSeedWriteCandidate,
        *,
        committed_state: str | None = None,
        committed_failure: str | None = None,
        committed_resolution: str | None = None,
        committed_jobs: tuple[str, ...] = (),
        failure: BoundedRuntimeFailureDiagnosticFact | None = None,
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
                committed_seed_failure_resolution_fingerprint=(committed_resolution),
                committed_job_ids=committed_jobs,
                failure=failure,
            ),
        )

    def read_job(
        self,
        job_id: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionJobRecord | None:
        deadline = deadline_monotonic or monotonic() + 20.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            row = self._read_job_row(connection, job_id, lock=False)
            if row is None:
                return None
            return DurableProjectionJobRecord(
                candidate=self._candidate_from_row(row),
                state=self._state_from_row(row),
            )

    def install_compaction_memory_result_candidate(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        result_candidate: CompactionMemoryExtractionResultCandidateFact,
        installation_guard: ResultCandidateInstallationGuard,
        deadline_monotonic: float,
    ) -> DurableProjectionCommitConfirmation:
        """Atomically replace a live model lease with immutable RESULT_READY."""

        if (
            lease.job.projection_kind
            is not DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION
            or result_candidate.job_id != lease.job.job_id
            or result_candidate.target_key != lease.job.target_key
            or result_candidate.result_owner.job_candidate_fingerprint
            != lease.job_candidate_fingerprint
            or result_candidate.result_owner.job_semantic_fingerprint
            != lease.job.job_semantic_fingerprint
            or installation_guard.result_candidate_id
            != result_candidate.result_candidate_id
            or installation_guard.result_candidate_fingerprint
            != result_candidate.result_candidate_fingerprint
            or installation_guard.job_id != lease.job.job_id
            or installation_guard.source_job_lease_generation != lease.lease_generation
            or installation_guard.source_job_lease_fingerprint
            != lease.lease_fingerprint
        ):
            raise ValueError("extraction result candidate/job authority drifted")
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            with connection.transaction():
                row = self._read_job_row(
                    connection,
                    lease.job.job_id,
                    lock=True,
                )
                if row is None:
                    return DurableProjectionCommitConfirmation.CONFLICT
                state = self._state_from_row(row)
                existing = connection.execute(
                    """
                    SELECT candidate_payload, candidate_fingerprint
                    FROM compaction_memory_extraction_result_candidates
                    WHERE job_id = %s OR result_candidate_id = %s
                    """,
                    (lease.job.job_id, result_candidate.result_candidate_id),
                ).fetchone()
                if existing is not None:
                    observed = (
                        CompactionMemoryExtractionResultCandidateFact.model_validate(
                            existing["candidate_payload"]
                        )
                    )
                    if (
                        observed != result_candidate
                        or str(existing["candidate_fingerprint"])
                        != result_candidate.result_candidate_fingerprint
                    ):
                        return DurableProjectionCommitConfirmation.CONFLICT
                    if state.status is DurableProjectionJobStatus.RESULT_READY:
                        return DurableProjectionCommitConfirmation.FULL
                    return DurableProjectionCommitConfirmation.CONFLICT
                if (
                    state.status is not DurableProjectionJobStatus.LEASED
                    or state.state_revision
                    != installation_guard.source_job_state_revision
                    or state.lease_generation != lease.lease_generation
                    or state.lease_owner_id != lease.lease_owner_id
                ):
                    return DurableProjectionCommitConfirmation.NONE
                target_lease = connection.execute(
                    """
                    SELECT lease_fingerprint
                    FROM durable_projection_target_execution_leases
                    WHERE projection_kind = %s AND target_key = %s
                      AND owner_job_id = %s AND lease_generation = %s
                      AND lease_owner_id = %s
                    FOR UPDATE
                    """,
                    (
                        lease.job.projection_kind.value,
                        lease.job.target_key,
                        lease.job.job_id,
                        lease.lease_generation,
                        lease.lease_owner_id,
                    ),
                ).fetchone()
                if (
                    target_lease is None
                    or str(target_lease["lease_fingerprint"])
                    != installation_guard.target_lease_fingerprint
                ):
                    return DurableProjectionCommitConfirmation.NONE
                head = self._read_head_in_connection(
                    connection,
                    projection_kind=lease.job.projection_kind,
                    target_key=lease.job.target_key,
                    lock=True,
                )
                if (
                    (head.head_fingerprint if head is not None else None)
                    != result_candidate.expected_target_head_fingerprint
                    or result_candidate.intended_target_head_revision
                    != (1 if head is None else head.head_revision + 1)
                ):
                    return DurableProjectionCommitConfirmation.CONFLICT
                connection.execute(
                    """
                    INSERT INTO compaction_memory_extraction_result_candidates (
                        result_candidate_id, job_id, target_key,
                        completed_event_id, result_semantic_fingerprint,
                        candidate_payload, candidate_fingerprint
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        result_candidate.result_candidate_id,
                        result_candidate.job_id,
                        result_candidate.target_key,
                        result_candidate.completed_event_id,
                        result_candidate.result_semantic_fingerprint,
                        _json(result_candidate),
                        result_candidate.result_candidate_fingerprint,
                    ),
                )
                next_state = cast(
                    DurableProjectionJobOperationalStateFact,
                    build_projection_fact(
                        DurableProjectionJobOperationalStateFact,
                        schema_version="durable_projection_job_operational_state.v1",
                        status=DurableProjectionJobStatus.RESULT_READY,
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
                        result_receipt_reference=None,
                    ),
                )
                self._write_job_state(
                    connection,
                    job_id=lease.job.job_id,
                    state=next_state,
                )
                self._release_target_lease(connection, lease)
        return DurableProjectionCommitConfirmation.FULL

    def prepare_compaction_memory_result_installation_guard(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        result_candidate: CompactionMemoryExtractionResultCandidateFact,
        deadline_monotonic: float,
    ) -> ResultCandidateInstallationGuard:
        """Freeze the exact job and target lease state used by installation CAS."""

        if (
            lease.job.projection_kind
            is not DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION
            or result_candidate.job_id != lease.job.job_id
            or result_candidate.target_key != lease.job.target_key
        ):
            raise ValueError("extraction installation guard authority drifted")
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            row = self._read_job_row(connection, lease.job.job_id, lock=False)
            if row is None:
                raise ValueError("extraction installation job disappeared")
            state = self._state_from_row(row)
            target_row = connection.execute(
                """
                SELECT lease_payload, lease_fingerprint
                FROM durable_projection_target_execution_leases
                WHERE projection_kind = %s AND target_key = %s
                  AND owner_job_id = %s AND lease_generation = %s
                  AND lease_owner_id = %s AND lease_expires_at > clock_timestamp()
                """,
                (
                    lease.job.projection_kind.value,
                    lease.job.target_key,
                    lease.job.job_id,
                    lease.lease_generation,
                    lease.lease_owner_id,
                ),
            ).fetchone()
        if (
            state.status is not DurableProjectionJobStatus.LEASED
            or state.lease_generation != lease.lease_generation
            or state.lease_owner_id != lease.lease_owner_id
            or target_row is None
        ):
            raise ValueError("extraction installation lease is no longer active")
        target_lease = DurableProjectionTargetExecutionLeaseFact.model_validate(
            target_row["lease_payload"]
        )
        if (
            target_lease.projection_kind != lease.job.projection_kind
            or target_lease.target_key != lease.job.target_key
            or target_lease.owner_job_id != lease.job.job_id
            or target_lease.lease_generation != lease.lease_generation
            or target_lease.lease_owner_id != lease.lease_owner_id
            or target_lease.lease_fingerprint != str(target_row["lease_fingerprint"])
        ):
            raise ValueError("extraction target lease authority drifted")
        return result_candidate_installation_guard(
            result_candidate=result_candidate,
            source_job_state_revision=state.state_revision,
            source_job_lease_generation=lease.lease_generation,
            source_job_lease_fingerprint=lease.lease_fingerprint,
            target_lease_fingerprint=target_lease.lease_fingerprint,
        )

    def read_compaction_memory_result_candidate(
        self,
        job_id: str,
        *,
        deadline_monotonic: float,
    ) -> CompactionMemoryExtractionResultCandidateFact | None:
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            row = connection.execute(
                """
                SELECT candidate_payload, candidate_fingerprint
                FROM compaction_memory_extraction_result_candidates
                WHERE job_id = %s
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        candidate = CompactionMemoryExtractionResultCandidateFact.model_validate(
            row["candidate_payload"]
        )
        if candidate.result_candidate_fingerprint != str(row["candidate_fingerprint"]):
            raise ValueError("extraction result candidate row drifted")
        return candidate

    def confirm_compaction_memory_settlement(
        self,
        result_candidate: CompactionMemoryExtractionResultCandidateFact,
        *,
        deadline_monotonic: float,
    ) -> tuple[
        DurableProjectionCommitConfirmation,
        DurableProjectionResultReceiptReferenceFact | None,
        int | None,
    ]:
        """Confirm one extraction settlement from a single durable snapshot."""

        completed_event = ContextCompactionMemoryExtractionCompletedEvent.model_validate_json(
            result_candidate.producer_event_candidate.canonical_unsequenced_payload_utf8
        )
        runtime_session_id = completed_event.occurrence_attribution.request_event_reference.stable_identity.runtime_session_id
        if completed_event.id != result_candidate.completed_event_id:
            return DurableProjectionCommitConfirmation.CONFLICT, None, None

        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            result_row = connection.execute(
                """
                SELECT candidate_payload, candidate_fingerprint
                FROM compaction_memory_extraction_result_candidates
                WHERE result_candidate_id = %s AND job_id = %s
                """,
                (result_candidate.result_candidate_id, result_candidate.job_id),
            ).fetchone()
            job_row = self._read_job_row(
                connection,
                result_candidate.job_id,
                lock=False,
            )
            if result_row is None or job_row is None:
                return DurableProjectionCommitConfirmation.NONE, None, None
            observed_candidate = (
                CompactionMemoryExtractionResultCandidateFact.model_validate(
                    result_row["candidate_payload"]
                )
            )
            if (
                observed_candidate != result_candidate
                or str(result_row["candidate_fingerprint"])
                != result_candidate.result_candidate_fingerprint
            ):
                return DurableProjectionCommitConfirmation.CONFLICT, None, None
            state = self._state_from_row(job_row)
            if (
                state.status is DurableProjectionJobStatus.SETTLEMENT_WRITING
                and state.result_receipt_reference is None
            ):
                event_row = connection.execute(
                    """
                    SELECT 1
                    FROM agent_events
                    WHERE session_id = %s AND id = %s
                    LIMIT 1
                    """,
                    (
                        runtime_session_id,
                        result_candidate.completed_event_id,
                    ),
                ).fetchone()
                receipt_row = connection.execute(
                    """
                    SELECT 1
                    FROM durable_projection_result_receipts
                    WHERE receipt_id = %s
                    LIMIT 1
                    """,
                    (result_candidate.receipt_id,),
                ).fetchone()
                outbox_row = connection.execute(
                    """
                    SELECT 1
                    FROM memory_candidate_projection_outbox
                    WHERE runtime_session_id = %s
                      AND producer_kind = 'compaction_memory_extraction'
                      AND producer_event_id = %s
                    LIMIT 1
                    """,
                    (
                        runtime_session_id,
                        result_candidate.completed_event_id,
                    ),
                ).fetchone()
                head = self._read_head_in_connection(
                    connection,
                    projection_kind=DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION,
                    target_key=result_candidate.target_key,
                    lock=False,
                )
                observed_head_fingerprint = (
                    head.head_fingerprint if head is not None else None
                )
                if (
                    event_row is not None
                    or receipt_row is not None
                    or outbox_row is not None
                    or observed_head_fingerprint
                    != result_candidate.expected_target_head_fingerprint
                ):
                    return DurableProjectionCommitConfirmation.CONFLICT, None, None
                return DurableProjectionCommitConfirmation.NONE, None, None
            if (
                state.status is not DurableProjectionJobStatus.SUCCEEDED
                or state.result_receipt_reference is None
                or state.result_receipt_reference.receipt_id
                != result_candidate.receipt_id
            ):
                return DurableProjectionCommitConfirmation.CONFLICT, None, None
            receipt = self._read_receipt_in_connection(
                connection,
                state.result_receipt_reference.receipt_id,
            )
            if not isinstance(
                receipt,
                CompactionMemoryExtractionProjectionResultReceiptFact,
            ):
                return DurableProjectionCommitConfirmation.CONFLICT, None, None
            head = self._read_head_in_connection(
                connection,
                projection_kind=DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION,
                target_key=result_candidate.target_key,
                lock=False,
            )
            if (
                receipt.receipt_fingerprint
                != state.result_receipt_reference.receipt_fingerprint
                or receipt.job_id != result_candidate.job_id
                or receipt.target_key != result_candidate.target_key
                or receipt.completed_event_reference.stable_identity.event_id
                != result_candidate.completed_event_id
                or receipt.completed_event_reference.stable_identity.runtime_session_id
                != runtime_session_id
                or receipt.source_request_event_reference.runtime_session_id
                != runtime_session_id
                or receipt.completed_result_semantic_fingerprint
                != result_candidate.result_semantic_fingerprint
                or receipt.outbox_item_count
                != result_candidate.candidate_outbox_plan.item_count
                or receipt.outbox_item_accumulator
                != result_candidate.candidate_outbox_plan.ordered_item_accumulator
                or head is None
                or head.head_revision != receipt.target_head_revision
                or head.applied_result_receipt_reference
                != state.result_receipt_reference
            ):
                return DurableProjectionCommitConfirmation.CONFLICT, None, None
            return (
                DurableProjectionCommitConfirmation.FULL,
                state.result_receipt_reference,
                head.head_revision,
            )

    def prepare_background_budget_reservation(
        self,
        *,
        runtime_session_id: str,
        reservation_id: str,
        extraction_job_id: str,
        operation_id: str,
        dispatch_attempt_ordinal: int,
        quote: ModelCallReservationQuoteFact,
        deadline_monotonic: float,
    ) -> BackgroundBudgetReserveOutcome:
        """Read one quote from a stable account revision; Start revalidates it."""

        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            row = connection.execute(
                """
                SELECT policy_payload, policy_fingerprint, account_payload,
                       account_fingerprint, account_revision
                FROM background_derived_work_budget_accounts
                WHERE runtime_session_id = %s
                """,
                (runtime_session_id,),
            ).fetchone()
        if row is None:
            raise ValueError(
                "background budget genesis is absent from session bootstrap"
            )
        from pulsara_agent.primitives.compaction import (
            BackgroundDerivedWorkBudgetAccountFact,
        )

        account = BackgroundDerivedWorkBudgetAccountFact.model_validate(
            row["account_payload"]
        )
        if (
            account.account_fingerprint != str(row["account_fingerprint"])
            or account.account_revision != int(row["account_revision"])
            or str(row["policy_fingerprint"])
            != DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY.policy_fingerprint
            or row["policy_payload"]
            != DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY.model_dump(mode="json")
        ):
            raise ValueError("background budget account row drifted")
        return reserve_background_budget(
            account=account,
            policy=DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
            reservation_id=reservation_id,
            extraction_job_id=extraction_job_id,
            operation_id=operation_id,
            dispatch_attempt_ordinal=dispatch_attempt_ordinal,
            quote=quote,
        )

    def read_background_budget_terminal_authority(
        self,
        *,
        reservation_id: str,
        extraction_job_id: str,
        resolved_model_call_id: str,
        dispatch_attempt_ordinal: int,
        deadline_monotonic: float,
    ) -> tuple[
        BackgroundDerivedWorkBudgetReservationFact,
        BackgroundDerivedWorkBudgetSettlementFact,
    ]:
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            row = connection.execute(
                """
                SELECT r.reservation_payload, r.reservation_fingerprint, r.status,
                       s.settlement_payload, s.settlement_fingerprint
                FROM background_derived_work_budget_reservations AS r
                LEFT JOIN background_derived_work_budget_settlements AS s
                  ON s.reservation_id = r.reservation_id
                WHERE r.reservation_id = %s
                """,
                (reservation_id,),
            ).fetchone()
        if row is None or row["settlement_payload"] is None:
            raise ValueError("background model terminal budget authority is incomplete")
        reservation = BackgroundDerivedWorkBudgetReservationFact.model_validate(
            row["reservation_payload"]
        )
        settlement = BackgroundDerivedWorkBudgetSettlementFact.model_validate(
            row["settlement_payload"]
        )
        if (
            reservation.reservation_fingerprint != str(row["reservation_fingerprint"])
            or settlement.settlement_fingerprint != str(row["settlement_fingerprint"])
            or str(row["status"]) != "settled"
            or reservation.extraction_job_id != extraction_job_id
            or reservation.dispatch_attempt_ordinal != dispatch_attempt_ordinal
            or reservation.model_call_reservation_quote.resolved_model_call_id
            != resolved_model_call_id
            or settlement.reservation_fingerprint != reservation.reservation_fingerprint
        ):
            raise ValueError("background model terminal budget authority drifted")
        return reservation, settlement

    def claim_compaction_memory_settlements(
        self,
        *,
        runtime_session_ids: tuple[str, ...],
        limit: int,
        bypass_retry_not_before: bool = False,
        reclaim_active_writing: bool = False,
        settlement_attempt_seconds: float = 20.0,
        deadline_monotonic: float,
    ) -> tuple[ClaimedCompactionMemoryExtractionSettlement, ...]:
        if not runtime_session_ids or limit < 1:
            return ()
        if not 0.1 <= settlement_attempt_seconds <= 120.0:
            raise ValueError("settlement attempt duration is outside the closed bound")
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            with connection.transaction():
                rows = tuple(
                    connection.execute(
                        """
                        SELECT j.*, r.candidate_payload,
                               r.candidate_fingerprint
                        FROM durable_projection_jobs AS j
                        JOIN compaction_memory_extraction_result_candidates AS r
                          ON r.job_id = j.job_id
                        WHERE j.runtime_session_id = ANY(%s)
                          AND j.projection_kind = %s
                          AND (
                            j.status = 'result_ready'
                            OR (
                              j.status = 'settlement_retry_wait'
                              AND (%s OR j.next_attempt_at <= clock_timestamp())
                            )
                            OR (
                              j.status = 'settlement_writing'
                              AND (
                                %s
                                OR j.next_attempt_at IS NULL
                                OR j.next_attempt_at <= clock_timestamp()
                              )
                            )
                          )
                        ORDER BY j.created_at, j.job_id
                        FOR UPDATE OF j SKIP LOCKED
                        LIMIT %s
                        """,
                        (
                            list(runtime_session_ids),
                            DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION.value,
                            bypass_retry_not_before,
                            reclaim_active_writing,
                            limit,
                        ),
                    ).fetchall()
                )
                database_now = cast(
                    datetime,
                    connection.execute(
                        "SELECT clock_timestamp() AS database_now"
                    ).fetchone()["database_now"],
                )
                claimed: list[ClaimedCompactionMemoryExtractionSettlement] = []
                for row in rows:
                    state = self._state_from_row(row)
                    candidate = (
                        CompactionMemoryExtractionResultCandidateFact.model_validate(
                            row["candidate_payload"]
                        )
                    )
                    if candidate.result_candidate_fingerprint != str(
                        row["candidate_fingerprint"]
                    ):
                        raise ValueError("extraction result candidate row drifted")
                    next_state = cast(
                        DurableProjectionJobOperationalStateFact,
                        build_projection_fact(
                            DurableProjectionJobOperationalStateFact,
                            schema_version=(
                                "durable_projection_job_operational_state.v1"
                            ),
                            status=DurableProjectionJobStatus.SETTLEMENT_WRITING,
                            state_revision=state.state_revision + 1,
                            repair_generation=state.repair_generation,
                            attempt_count=state.attempt_count,
                            dispatch_attempt_count=state.dispatch_attempt_count,
                            settlement_generation=state.settlement_generation + 1,
                            lease_generation=state.lease_generation,
                            lease_owner_id=None,
                            lease_expires_at=None,
                            # For SETTLEMENT_WRITING this is the durable physical
                            # owner expiry, not a retry eligibility time.
                            next_attempt_at=database_now
                            + timedelta(seconds=settlement_attempt_seconds),
                            last_failure=None,
                            result_receipt_reference=None,
                        ),
                    )
                    self._write_job_state(
                        connection,
                        job_id=candidate.job_id,
                        state=next_state,
                    )
                    claimed.append(
                        ClaimedCompactionMemoryExtractionSettlement(
                            result_candidate=candidate,
                            state=next_state,
                        )
                    )
        return tuple(claimed)

    def defer_compaction_memory_settlement(
        self,
        *,
        result_candidate: CompactionMemoryExtractionResultCandidateFact,
        settlement_generation: int,
        failure: BoundedRuntimeFailureDiagnosticFact,
        delay_seconds: float,
        reconciliation_required: bool,
        deadline_monotonic: float,
    ) -> None:
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            with connection.transaction():
                row = self._read_job_row(
                    connection,
                    result_candidate.job_id,
                    lock=True,
                )
                if row is None:
                    raise ValueError("extraction settlement job disappeared")
                state = self._state_from_row(row)
                if (
                    state.status is not DurableProjectionJobStatus.SETTLEMENT_WRITING
                    or state.settlement_generation != settlement_generation
                ):
                    raise ValueError("extraction settlement attempt is stale")
                database_now = cast(
                    datetime,
                    connection.execute(
                        "SELECT clock_timestamp() AS database_now"
                    ).fetchone()["database_now"],
                )
                next_state = cast(
                    DurableProjectionJobOperationalStateFact,
                    build_projection_fact(
                        DurableProjectionJobOperationalStateFact,
                        schema_version="durable_projection_job_operational_state.v1",
                        status=(
                            DurableProjectionJobStatus.RECONCILIATION_REQUIRED
                            if reconciliation_required
                            else DurableProjectionJobStatus.SETTLEMENT_RETRY_WAIT
                        ),
                        state_revision=state.state_revision + 1,
                        repair_generation=state.repair_generation,
                        attempt_count=state.attempt_count,
                        dispatch_attempt_count=state.dispatch_attempt_count,
                        settlement_generation=state.settlement_generation,
                        lease_generation=state.lease_generation,
                        lease_owner_id=None,
                        lease_expires_at=None,
                        next_attempt_at=(
                            None
                            if reconciliation_required
                            else database_now
                            + timedelta(seconds=max(0.01, delay_seconds))
                        ),
                        last_failure=failure,
                        result_receipt_reference=None,
                    ),
                )
                self._write_job_state(
                    connection,
                    job_id=result_candidate.job_id,
                    state=next_state,
                )

    def repair_dead_letter(
        self,
        *,
        job_id: str,
        reason: DurableProjectionRepairReason,
        operator_authority_id: str,
        deadline_monotonic: float,
    ) -> DurableProjectionRepairActionFact:
        """Install one typed repair action and advance the exact dead letter."""

        if not operator_authority_id.strip():
            raise ValueError("operator authority id must be non-empty")
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            row = self._read_job_row(connection, job_id, lock=True)
            if row is None:
                raise KeyError(job_id)
            candidate = self._candidate_from_row(row)
            state = self._state_from_row(row)
            latest_action_row = connection.execute(
                """
                SELECT action_payload, action_fingerprint
                FROM durable_projection_repair_actions
                WHERE owner_kind = 'projection_job' AND owner_id = %s
                ORDER BY repair_generation DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if (
                state.status is DurableProjectionJobStatus.PENDING
                and latest_action_row is not None
            ):
                latest_action = DurableProjectionRepairActionFact.model_validate(
                    latest_action_row["action_payload"]
                )
                if (
                    latest_action.action_fingerprint
                    != latest_action_row["action_fingerprint"]
                    or latest_action.resulting_repair_generation
                    != state.repair_generation
                ):
                    raise ValueError("projection repair lineage drifted")
                authority_ids = tuple(
                    item.authority_id
                    for item in latest_action.authority_references
                    if item.authority_kind == "operator_command"
                )
                if latest_action.operator_reason_code is reason and authority_ids == (
                    operator_authority_id,
                ):
                    return latest_action
            if state.status is not DurableProjectionJobStatus.DEAD_LETTER:
                raise ValueError("projection repair requires a dead-letter job")
            conflict = connection.execute(
                """
                SELECT 1
                FROM durable_projection_target_authority_conflicts
                WHERE projection_kind = %s AND target_key = %s
                LIMIT 1
                """,
                (
                    candidate.job_semantic.projection_kind.value,
                    candidate.job_semantic.target_key,
                ),
            ).fetchone()
            if conflict is not None:
                raise ValueError(
                    "target authority conflict requires offline authority repair"
                )
            resulting_generation = state.repair_generation + 1
            action_id = "projection-repair:" + context_fingerprint(
                "durable-projection-repair-action-id:v1",
                {
                    "job_id": job_id,
                    "job_semantic_fingerprint": (
                        candidate.job_semantic.job_semantic_fingerprint
                    ),
                    "expected_state_revision": state.state_revision,
                    "expected_repair_generation": state.repair_generation,
                    "reason": reason.value,
                    "operator_authority_id": operator_authority_id,
                },
            )
            existing = connection.execute(
                """
                SELECT action_payload, action_fingerprint
                FROM durable_projection_repair_actions
                WHERE repair_action_id = %s
                """,
                (action_id,),
            ).fetchone()
            if existing is not None:
                action = DurableProjectionRepairActionFact.model_validate(
                    existing["action_payload"]
                )
                if action.action_fingerprint != existing["action_fingerprint"]:
                    raise ValueError("projection repair action row drifted")
                raise ValueError("projection repair action/job state conflict")

            authority_semantic_fingerprint = context_fingerprint(
                "durable-projection-operator-authority:v1",
                {
                    "operator_authority_id": operator_authority_id,
                    "job_id": job_id,
                    "reason": reason.value,
                    "expected_repair_generation": state.repair_generation,
                },
            )
            authority = cast(
                DurableRepairAuthorityReferenceFact,
                build_projection_fact(
                    DurableRepairAuthorityReferenceFact,
                    schema_version="durable_repair_authority_reference.v1",
                    authority_kind="operator_command",
                    authority_id=operator_authority_id,
                    authority_semantic_fingerprint=(authority_semantic_fingerprint),
                ),
            )
            requested_at = connection.execute(
                "SELECT clock_timestamp() AS requested_at"
            ).fetchone()["requested_at"]
            action = cast(
                DurableProjectionRepairActionFact,
                build_projection_fact(
                    DurableProjectionRepairActionFact,
                    schema_version="durable_projection_repair_action.v1",
                    repair_action_id=action_id,
                    job_id=job_id,
                    expected_state_revision=state.state_revision,
                    expected_job_semantic_fingerprint=(
                        candidate.job_semantic.job_semantic_fingerprint
                    ),
                    expected_repair_generation=state.repair_generation,
                    action="retry_same_contract",
                    operator_reason_code=reason,
                    authority_references=(authority,),
                    requested_at=requested_at,
                    resulting_repair_generation=resulting_generation,
                ),
            )
            self._validate_or_issue_guard(
                connection,
                admission_guard=None,
                transaction_owner_id=action.repair_action_id,
            )
            connection.execute(
                """
                INSERT INTO durable_projection_repair_actions (
                    repair_action_id, owner_kind, owner_id,
                    repair_generation, action_payload, action_fingerprint
                ) VALUES (%s, 'projection_job', %s, %s, %s, %s)
                """,
                (
                    action.repair_action_id,
                    job_id,
                    action.resulting_repair_generation,
                    _json(action),
                    action.action_fingerprint,
                ),
            )
            next_state = cast(
                DurableProjectionJobOperationalStateFact,
                build_projection_fact(
                    DurableProjectionJobOperationalStateFact,
                    schema_version=("durable_projection_job_operational_state.v1"),
                    status=DurableProjectionJobStatus.PENDING,
                    state_revision=state.state_revision + 1,
                    repair_generation=resulting_generation,
                    attempt_count=0,
                    lease_generation=state.lease_generation,
                    lease_owner_id=None,
                    lease_expires_at=None,
                    next_attempt_at=None,
                    last_failure=None,
                    result_receipt_reference=None,
                ),
            )
            self._write_job_state(
                connection,
                job_id=job_id,
                state=next_state,
            )
            return action

    @staticmethod
    def _read_job_row(
        connection: Connection,
        job_id: str,
        *,
        lock: bool,
    ) -> dict[str, object] | None:
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            "SELECT * FROM durable_projection_jobs WHERE job_id = %s" + suffix,
            (job_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _candidate_from_row(
        row: dict[str, object],
    ) -> DurableProjectionJobCandidateFact:
        source = DurableProjectionSourceEventReferenceFact.model_validate(
            row["source_reference"]
        )
        horizon = DurableProjectionLedgerHorizonFact.model_validate(
            row["trigger_horizon"]
        )
        handler = DurableProjectionHandlerContractFact.model_validate(
            row["handler_contract"]
        )
        job = cast(
            DurableProjectionJobSemanticFact,
            build_projection_fact(
                DurableProjectionJobSemanticFact,
                schema_version="durable_projection_job_semantic.v1",
                job_id=str(row["job_id"]),
                projection_kind=DurableProjectionKind(str(row["projection_kind"])),
                target_key=str(row["target_key"]),
                source_event_reference=source,
                trigger_horizon=horizon,
                handler_contract=handler,
            ),
        )
        from pulsara_agent.projection_jobs.contracts import (
            CanonicalMutationSurfacePlanFact,
        )

        candidate = cast(
            DurableProjectionJobCandidateFact,
            build_projection_fact(
                DurableProjectionJobCandidateFact,
                schema_version="durable_projection_job_candidate.v1",
                job_semantic=job,
                activation_fingerprint=str(row["activation_fingerprint"]),
                seed_contract_fingerprint=str(row["seed_contract_fingerprint"]),
                delivery_policy=DurableProjectionDeliveryPolicyFact.model_validate(
                    row["delivery_policy"]
                ),
                canonical_mutation_surface_plan=(
                    CanonicalMutationSurfacePlanFact.model_validate(
                        row["canonical_mutation_surface_plan"]
                    )
                ),
            ),
        )
        if (
            job.job_semantic_fingerprint != str(row["job_semantic_fingerprint"])
            or candidate.candidate_fingerprint != str(row["job_candidate_fingerprint"])
            or source.event_id != str(row["source_event_id"])
            or source.sequence != int(row["source_sequence"])
            or source.event_type != str(row["source_event_type"])
        ):
            raise ValueError("durable projection job row is self-inconsistent")
        return candidate

    @staticmethod
    def _state_from_row(
        row: dict[str, object],
    ) -> DurableProjectionJobOperationalStateFact:
        last_failure = (
            BoundedRuntimeFailureDiagnosticFact.model_validate(row["last_failure"])
            if row["last_failure"] is not None
            else None
        )
        reference = (
            DurableProjectionResultReceiptReferenceFact.model_validate(
                row["result_receipt_reference"]
            )
            if row["result_receipt_reference"] is not None
            else None
        )
        deferral = (
            CompactionMemoryExtractionJobDeferralFact.model_validate(
                row["compaction_memory_deferral"]
            )
            if row.get("compaction_memory_deferral") is not None
            else None
        )
        state = cast(
            DurableProjectionJobOperationalStateFact,
            build_projection_fact(
                DurableProjectionJobOperationalStateFact,
                schema_version="durable_projection_job_operational_state.v1",
                status=DurableProjectionJobStatus(str(row["status"])),
                state_revision=int(row["state_revision"]),
                repair_generation=int(row["repair_generation"]),
                attempt_count=int(row["attempt_count"]),
                dispatch_attempt_count=int(row.get("dispatch_attempt_count", 0)),
                settlement_generation=int(row.get("settlement_generation", 0)),
                lease_generation=int(row["lease_generation"]),
                lease_owner_id=(
                    str(row["lease_owner_id"])
                    if row["lease_owner_id"] is not None
                    else None
                ),
                lease_expires_at=row["lease_expires_at"],
                next_attempt_at=row["next_attempt_at"],
                last_failure=last_failure,
                compaction_memory_deferral=deferral,
                result_receipt_reference=reference,
            ),
        )
        if state.state_fingerprint != str(row["state_fingerprint"]):
            raise ValueError("durable projection job state fingerprint drifted")
        return state

    def claim_due(
        self,
        *,
        owner_id: str,
        limit: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[LeasedDurableProjectionJob, ...]:
        if not owner_id or limit < 1:
            raise ValueError("projection claim identity/bound is invalid")
        deadline = deadline_monotonic or monotonic() + 20.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            rows = tuple(
                dict(row)
                for row in connection.execute(
                    """
                    WITH due_jobs AS (
                        SELECT j.*,
                               row_number() OVER (
                                   PARTITION BY projection_kind, target_key
                                   ORDER BY next_attempt_at NULLS FIRST,
                                            created_at, job_id
                               ) AS target_row_ordinal
                        FROM durable_projection_jobs AS j
                        WHERE projection_kind <> %s
                          AND (
                            status = 'pending'
                            OR (
                                status = 'retry_wait'
                                AND next_attempt_at <= clock_timestamp()
                            )
                            OR (
                                status = 'leased'
                                AND lease_expires_at <= clock_timestamp()
                            )
                        )
                    ),
                    eligible_targets AS (
                        SELECT d.projection_kind, d.target_key,
                               min(
                                   COALESCE(d.next_attempt_at, d.created_at)
                               ) AS first_due_at,
                               min(d.created_at) AS first_created_at,
                               min(d.job_id) AS first_job_id
                        FROM due_jobs AS d
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM durable_projection_target_authority_conflicts AS c
                            WHERE c.projection_kind = d.projection_kind
                              AND c.target_key = d.target_key
                        )
                          AND NOT EXISTS (
                            SELECT 1
                            FROM durable_projection_target_execution_leases AS l
                            WHERE l.projection_kind = d.projection_kind
                              AND l.target_key = d.target_key
                              AND l.lease_expires_at > clock_timestamp()
                        )
                        GROUP BY d.projection_kind, d.target_key
                        ORDER BY first_due_at, first_created_at, first_job_id,
                                 d.projection_kind, d.target_key
                        LIMIT %s
                    ),
                    candidate_ids AS (
                        SELECT d.job_id
                        FROM due_jobs AS d
                        JOIN eligible_targets AS t
                          ON t.projection_kind = d.projection_kind
                         AND t.target_key = d.target_key
                        WHERE d.target_row_ordinal <= 8
                    )
                    SELECT j.*
                    FROM durable_projection_jobs AS j
                    JOIN candidate_ids AS c ON c.job_id = j.job_id
                    ORDER BY j.next_attempt_at NULLS FIRST,
                             j.created_at, j.job_id
                    FOR UPDATE OF j SKIP LOCKED
                    """,
                    (
                        DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION.value,
                        max(limit * 8, limit),
                    ),
                ).fetchall()
            )
            selected = self._select_claim_rows(connection, rows, limit=limit)
            return self._lease_selected_rows(
                connection,
                selected,
                owner_id=owner_id,
                advance_attempt_count=True,
            )

    def claim_due_session_model(
        self,
        *,
        owner_id: str,
        runtime_session_ids: tuple[str, ...],
        limit: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[LeasedDurableProjectionJob, ...]:
        """Claim only jobs with a live session driver without consuming an attempt."""

        if not owner_id or limit < 1 or not runtime_session_ids:
            return ()
        if len(runtime_session_ids) > 256:
            raise ValueError("session-model claim driver set exceeds its hard bound")
        deadline = deadline_monotonic or monotonic() + 20.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            rows = tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT j.*
                    FROM durable_projection_jobs AS j
                    WHERE j.projection_kind = %s
                      AND j.runtime_session_id = ANY(%s)
                      AND (
                              j.status = 'pending'
                              AND (
                                  j.next_attempt_at IS NULL
                                  OR j.next_attempt_at <= clock_timestamp()
                              )
                          OR (
                              j.status = 'model_retry_wait'
                              AND j.next_attempt_at <= clock_timestamp()
                          )
                          OR (
                              j.status = 'leased'
                              AND j.lease_expires_at <= clock_timestamp()
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM durable_projection_target_authority_conflicts AS c
                          WHERE c.projection_kind = j.projection_kind
                            AND c.target_key = j.target_key
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM durable_projection_target_execution_leases AS l
                          WHERE l.projection_kind = j.projection_kind
                            AND l.target_key = j.target_key
                            AND l.lease_expires_at > clock_timestamp()
                      )
                    ORDER BY j.next_attempt_at NULLS FIRST,
                             j.created_at, j.job_id
                    LIMIT %s
                    FOR UPDATE OF j SKIP LOCKED
                    """,
                    (
                        DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION.value,
                        list(runtime_session_ids),
                        max(limit * 8, limit),
                    ),
                ).fetchall()
            )
            selected = self._select_claim_rows(connection, rows, limit=limit)
            return self._lease_selected_rows(
                connection,
                selected,
                owner_id=owner_id,
                advance_attempt_count=False,
            )

    def session_model_work_schedule(
        self,
        *,
        runtime_session_id: str,
        deadline_monotonic: float,
    ) -> SessionModelWorkSchedule:
        """Return the database-clock schedule for all active model jobs."""

        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            row = connection.execute(
                """
                SELECT
                    clock_timestamp() AS database_now,
                    COALESCE(bool_or(
                        status = 'result_ready'
                        OR (status = 'pending' AND (
                            next_attempt_at IS NULL
                            OR next_attempt_at <= clock_timestamp()
                        ))
                        OR (status IN (
                            'model_retry_wait', 'settlement_retry_wait'
                        ) AND next_attempt_at <= clock_timestamp())
                        OR (status = 'leased'
                            AND lease_expires_at <= clock_timestamp())
                    ), false) AS immediate,
                    min(CASE
                        WHEN status = 'pending' THEN next_attempt_at
                        WHEN status IN (
                            'model_retry_wait', 'settlement_retry_wait'
                        ) THEN next_attempt_at
                        WHEN status = 'leased' THEN lease_expires_at
                        ELSE NULL
                    END) AS next_eligible_at
                FROM durable_projection_jobs
                WHERE runtime_session_id = %s
                  AND projection_kind = %s
                  AND status IN (
                      'pending', 'leased', 'model_retry_wait', 'result_ready',
                      'settlement_retry_wait'
                  )
                """,
                (
                    runtime_session_id,
                    DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION.value,
                ),
            ).fetchone()
        return SessionModelWorkSchedule(
            immediate=bool(row["immediate"]),
            database_now=cast(datetime, row["database_now"]),
            next_eligible_at=cast(datetime | None, row["next_eligible_at"]),
        )

    def _lease_selected_rows(
        self,
        connection: Connection,
        rows: tuple[dict[str, object], ...],
        *,
        owner_id: str,
        advance_attempt_count: bool,
    ) -> tuple[LeasedDurableProjectionJob, ...]:
        leases: list[LeasedDurableProjectionJob] = []
        for row in rows:
            candidate = self._candidate_from_row(row)
            state = self._state_from_row(row)
            policy = candidate.delivery_policy.retry_policy
            attempts_used = (
                state.attempt_count
                if advance_attempt_count
                else state.dispatch_attempt_count
            )
            if attempts_used >= policy.maximum_attempts:
                self._dead_letter_attempts_exhausted(
                    connection,
                    candidate=candidate,
                    state=state,
                )
                continue
            database_now_row = connection.execute(
                "SELECT clock_timestamp() AS database_now"
            ).fetchone()
            database_now = cast(datetime, database_now_row["database_now"])
            expires = database_now + timedelta(seconds=policy.lease_duration_seconds)
            next_state = cast(
                DurableProjectionJobOperationalStateFact,
                build_projection_fact(
                    DurableProjectionJobOperationalStateFact,
                    schema_version="durable_projection_job_operational_state.v1",
                    status=DurableProjectionJobStatus.LEASED,
                    state_revision=state.state_revision + 1,
                    repair_generation=state.repair_generation,
                    attempt_count=(
                        state.attempt_count + 1
                        if advance_attempt_count
                        else state.attempt_count
                    ),
                    dispatch_attempt_count=state.dispatch_attempt_count,
                    settlement_generation=state.settlement_generation,
                    lease_generation=state.lease_generation + 1,
                    lease_owner_id=owner_id,
                    lease_expires_at=expires,
                    next_attempt_at=None,
                    last_failure=state.last_failure,
                    compaction_memory_deferral=None,
                    result_receipt_reference=None,
                ),
            )
            target_lease = cast(
                DurableProjectionTargetExecutionLeaseFact,
                build_projection_fact(
                    DurableProjectionTargetExecutionLeaseFact,
                    schema_version="durable_projection_target_execution_lease.v1",
                    projection_kind=candidate.job_semantic.projection_kind,
                    target_key=candidate.job_semantic.target_key,
                    owner_job_id=candidate.job_semantic.job_id,
                    owner_source_sequence=(
                        candidate.job_semantic.source_event_reference.sequence
                    ),
                    lease_generation=next_state.lease_generation,
                    lease_owner_id=owner_id,
                    lease_expires_at=expires,
                    state_revision=next_state.state_revision,
                ),
            )
            self._write_job_state(
                connection,
                job_id=candidate.job_semantic.job_id,
                state=next_state,
            )
            target_inserted = connection.execute(
                """
                INSERT INTO durable_projection_target_execution_leases (
                    projection_kind, target_key, owner_job_id,
                    source_sequence, lease_generation, lease_owner_id,
                    lease_expires_at, lease_payload, lease_fingerprint
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (projection_kind, target_key) DO UPDATE SET
                    owner_job_id = EXCLUDED.owner_job_id,
                    source_sequence = EXCLUDED.source_sequence,
                    lease_generation = EXCLUDED.lease_generation,
                    lease_owner_id = EXCLUDED.lease_owner_id,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    lease_payload = EXCLUDED.lease_payload,
                    lease_fingerprint = EXCLUDED.lease_fingerprint
                WHERE durable_projection_target_execution_leases.lease_expires_at
                      <= clock_timestamp()
                RETURNING lease_fingerprint
                """,
                (
                    candidate.job_semantic.projection_kind.value,
                    candidate.job_semantic.target_key,
                    candidate.job_semantic.job_id,
                    candidate.job_semantic.source_event_reference.sequence,
                    next_state.lease_generation,
                    owner_id,
                    expires,
                    _json(target_lease),
                    target_lease.lease_fingerprint,
                ),
            ).fetchone()
            if (
                target_inserted is None
                or str(target_inserted["lease_fingerprint"])
                != target_lease.lease_fingerprint
            ):
                raise ValueError("projection target lease compare-and-set failed")
            leases.append(
                cast(
                    LeasedDurableProjectionJob,
                    build_projection_fact(
                        LeasedDurableProjectionJob,
                        schema_version="leased_durable_projection_job.v1",
                        job=candidate.job_semantic,
                        job_candidate_fingerprint=candidate.candidate_fingerprint,
                        activation_fingerprint=candidate.activation_fingerprint,
                        seed_contract_fingerprint=(candidate.seed_contract_fingerprint),
                        delivery_policy=candidate.delivery_policy,
                        canonical_mutation_surface_plan=(
                            candidate.canonical_mutation_surface_plan
                        ),
                        expected_state_revision=next_state.state_revision,
                        repair_generation=next_state.repair_generation,
                        attempt_count=next_state.attempt_count,
                        dispatch_attempt_count=next_state.dispatch_attempt_count,
                        lease_generation=next_state.lease_generation,
                        lease_owner_id=owner_id,
                        lease_expires_at=expires,
                    ),
                )
            )
        return tuple(leases)

    def release_session_model_lease_without_attempt(
        self,
        lease: LeasedDurableProjectionJob,
        *,
        reason: str,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionCommitConfirmation:
        """Return a never-dispatched session-model lease to PENDING."""

        if (
            lease.job.projection_kind
            is not DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION
        ):
            raise ValueError("only session-model projection leases may be deferred")
        if reason not in {"driver_busy", "safe_point_stale"}:
            raise ValueError("session-model deferral reason is invalid")
        deadline = deadline_monotonic or monotonic() + 10.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            row = connection.execute(
                """
                SELECT * FROM durable_projection_jobs
                WHERE job_id = %s
                FOR UPDATE
                """,
                (lease.job.job_id,),
            ).fetchone()
            if row is None:
                return DurableProjectionCommitConfirmation.CONFLICT
            state = self._state_from_row(dict(row))
            if state.status is DurableProjectionJobStatus.PENDING:
                deferral = state.compaction_memory_deferral
                return (
                    DurableProjectionCommitConfirmation.FULL
                    if deferral is not None
                    and deferral.job_id == lease.job.job_id
                    and deferral.reason == reason
                    else DurableProjectionCommitConfirmation.CONFLICT
                )
            if (
                state.status is not DurableProjectionJobStatus.LEASED
                or state.state_revision != lease.expected_state_revision
                or state.lease_generation != lease.lease_generation
                or state.lease_owner_id != lease.lease_owner_id
                or state.dispatch_attempt_count != lease.dispatch_attempt_count
            ):
                return DurableProjectionCommitConfirmation.CONFLICT
            database_now = cast(
                datetime,
                connection.execute(
                    "SELECT clock_timestamp() AS database_now"
                ).fetchone()["database_now"],
            )
            not_before = database_now + timedelta(
                seconds=1 + ((state.lease_generation - 1) % 5)
            )
            deferral = cast(
                CompactionMemoryExtractionJobDeferralFact,
                build_projection_fact(
                    CompactionMemoryExtractionJobDeferralFact,
                    schema_version="compaction_memory_extraction_job_deferral.v1",
                    job_id=lease.job.job_id,
                    reason=reason,
                    deferral_ordinal=state.lease_generation,
                    not_before_utc=not_before,
                    deferral_policy_fingerprint=(
                        _MODEL_JOB_DEFERRAL_POLICY_FINGERPRINT
                    ),
                ),
            )
            next_state = cast(
                DurableProjectionJobOperationalStateFact,
                build_projection_fact(
                    DurableProjectionJobOperationalStateFact,
                    schema_version="durable_projection_job_operational_state.v1",
                    status=DurableProjectionJobStatus.PENDING,
                    state_revision=state.state_revision + 1,
                    repair_generation=state.repair_generation,
                    attempt_count=state.attempt_count,
                    dispatch_attempt_count=state.dispatch_attempt_count,
                    settlement_generation=state.settlement_generation,
                    lease_generation=state.lease_generation,
                    lease_owner_id=None,
                    lease_expires_at=None,
                    next_attempt_at=not_before,
                    last_failure=None,
                    compaction_memory_deferral=deferral,
                    result_receipt_reference=None,
                ),
            )
            self._write_job_state(
                connection,
                job_id=lease.job.job_id,
                state=next_state,
            )
            self._release_target_lease(connection, lease)
        return DurableProjectionCommitConfirmation.FULL

    def dead_letter_session_model_job_before_attempt(
        self,
        lease: LeasedDurableProjectionJob,
        *,
        failure_kind: DurableProjectionFailureKind,
        error: BaseException,
        deadline_monotonic: float,
    ) -> DurableProjectionCommitConfirmation:
        """Terminalize a deterministic failure before ModelCallStart is FULL."""

        if (
            lease.job.projection_kind
            is not DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION
        ):
            raise ValueError("only extraction model jobs use pre-dispatch failure")
        if failure_kind not in {
            DurableProjectionFailureKind.SOURCE_AUTHORITY_CONFLICT,
            DurableProjectionFailureKind.TARGET_AUTHORITY_CONFLICT,
            DurableProjectionFailureKind.HANDLER_CONTRACT_MISMATCH,
        }:
            raise ValueError("pre-dispatch dead-letter failure kind is invalid")
        diagnostic = _diagnostic(error)
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            with connection.transaction():
                row = self._read_job_row(connection, lease.job.job_id, lock=True)
                if row is None:
                    return DurableProjectionCommitConfirmation.CONFLICT
                state = self._state_from_row(row)
                if state.status is DurableProjectionJobStatus.DEAD_LETTER:
                    return (
                        DurableProjectionCommitConfirmation.FULL
                        if state.dispatch_attempt_count == lease.dispatch_attempt_count
                        and state.last_failure == diagnostic
                        else DurableProjectionCommitConfirmation.CONFLICT
                    )
                if (
                    state.status is not DurableProjectionJobStatus.LEASED
                    or state.state_revision != lease.expected_state_revision
                    or state.lease_generation != lease.lease_generation
                    or state.lease_owner_id != lease.lease_owner_id
                    or state.dispatch_attempt_count != lease.dispatch_attempt_count
                ):
                    return DurableProjectionCommitConfirmation.CONFLICT
                terminal = cast(
                    DurableProjectionJobOperationalStateFact,
                    build_projection_fact(
                        DurableProjectionJobOperationalStateFact,
                        schema_version=("durable_projection_job_operational_state.v1"),
                        status=DurableProjectionJobStatus.DEAD_LETTER,
                        state_revision=state.state_revision + 1,
                        repair_generation=state.repair_generation,
                        attempt_count=state.attempt_count,
                        dispatch_attempt_count=state.dispatch_attempt_count,
                        settlement_generation=state.settlement_generation,
                        lease_generation=state.lease_generation,
                        lease_owner_id=None,
                        lease_expires_at=None,
                        next_attempt_at=None,
                        last_failure=diagnostic,
                        compaction_memory_deferral=None,
                        result_receipt_reference=None,
                    ),
                )
                self._write_job_state(
                    connection,
                    job_id=lease.job.job_id,
                    state=terminal,
                )
                self._release_target_lease(connection, lease)
        return DurableProjectionCommitConfirmation.FULL

    def defer_session_model_job_after_attempt(
        self,
        lease: LeasedDurableProjectionJob,
        *,
        failure: BoundedRuntimeFailureDiagnosticFact,
        delay_seconds: float,
        deadline_monotonic: float,
    ) -> DurableProjectionCommitConfirmation:
        """Close one dispatched attempt while preserving its durable ordinal."""

        if (
            lease.job.projection_kind
            is not DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION
        ):
            raise ValueError("only extraction model jobs use model retry wait")
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            with connection.transaction():
                row = self._read_job_row(
                    connection,
                    lease.job.job_id,
                    lock=True,
                )
                if row is None:
                    return DurableProjectionCommitConfirmation.CONFLICT
                state = self._state_from_row(row)
                if state.status is DurableProjectionJobStatus.MODEL_RETRY_WAIT:
                    target_lease = connection.execute(
                        """
                        SELECT 1
                        FROM durable_projection_target_execution_leases
                        WHERE projection_kind = %s AND target_key = %s
                        """,
                        (
                            lease.job.projection_kind.value,
                            lease.job.target_key,
                        ),
                    ).fetchone()
                    return (
                        DurableProjectionCommitConfirmation.FULL
                        if state.dispatch_attempt_count
                        == lease.dispatch_attempt_count + 1
                        and state.state_revision == lease.expected_state_revision + 2
                        and state.attempt_count == 0
                        and state.lease_generation == lease.lease_generation
                        and state.last_failure == failure
                        and state.lease_owner_id is None
                        and state.lease_expires_at is None
                        and state.next_attempt_at is not None
                        and state.compaction_memory_deferral is None
                        and state.result_receipt_reference is None
                        and target_lease is None
                        else DurableProjectionCommitConfirmation.CONFLICT
                    )
                if (
                    state.status is not DurableProjectionJobStatus.LEASED
                    or state.lease_generation != lease.lease_generation
                    or state.lease_owner_id != lease.lease_owner_id
                    or state.dispatch_attempt_count != lease.dispatch_attempt_count + 1
                ):
                    return DurableProjectionCommitConfirmation.CONFLICT
                now = cast(
                    datetime,
                    connection.execute(
                        "SELECT clock_timestamp() AS database_now"
                    ).fetchone()["database_now"],
                )
                next_state = cast(
                    DurableProjectionJobOperationalStateFact,
                    build_projection_fact(
                        DurableProjectionJobOperationalStateFact,
                        schema_version="durable_projection_job_operational_state.v1",
                        status=DurableProjectionJobStatus.MODEL_RETRY_WAIT,
                        state_revision=state.state_revision + 1,
                        repair_generation=state.repair_generation,
                        attempt_count=state.attempt_count,
                        dispatch_attempt_count=state.dispatch_attempt_count,
                        settlement_generation=state.settlement_generation,
                        lease_generation=state.lease_generation,
                        lease_owner_id=None,
                        lease_expires_at=None,
                        next_attempt_at=now
                        + timedelta(seconds=max(0.01, delay_seconds)),
                        last_failure=failure,
                        result_receipt_reference=None,
                    ),
                )
                self._write_job_state(
                    connection,
                    job_id=lease.job.job_id,
                    state=next_state,
                )
                self._release_target_lease(connection, lease)
        return DurableProjectionCommitConfirmation.FULL

    def supersede_unstarted_compaction_memory_jobs(
        self,
        *,
        runtime_session_id: str,
        deadline_monotonic: float,
    ) -> int:
        """Terminalize jobs that have no live model or RESULT_READY owner."""

        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            with connection.transaction():
                rows = tuple(
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT * FROM durable_projection_jobs
                        WHERE runtime_session_id = %s
                          AND projection_kind = %s
                          AND status IN ('pending', 'model_retry_wait')
                        ORDER BY source_sequence, job_id
                        FOR UPDATE
                        """,
                        (
                            runtime_session_id,
                            DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION.value,
                        ),
                    ).fetchall()
                )
                for row in rows:
                    candidate = self._candidate_from_row(row)
                    state = self._state_from_row(row)
                    receipt_id = (
                        "projection-receipt:compaction-memory-superseded:"
                        + context_fingerprint(
                            "compaction-memory-extraction-superseded-receipt-id:v1",
                            (
                                candidate.job_semantic.job_id,
                                candidate.job_semantic.source_event_reference.reference_fingerprint,
                                "graceful_session_close",
                            ),
                        ).removeprefix("sha256:")
                    )
                    receipt = cast(
                        CompactionMemoryExtractionSupersededReceiptFact,
                        build_projection_fact(
                            CompactionMemoryExtractionSupersededReceiptFact,
                            schema_version=(
                                "compaction_memory_extraction_superseded_receipt.v1"
                            ),
                            receipt_kind=("compaction_memory_extraction_superseded"),
                            receipt_id=receipt_id,
                            projection_kind=(
                                DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION
                            ),
                            job_id=candidate.job_semantic.job_id,
                            target_key=candidate.job_semantic.target_key,
                            source_request_event_reference=(
                                candidate.job_semantic.source_event_reference
                            ),
                            supersession_reason="graceful_session_close",
                            dispatch_attempt_count=state.dispatch_attempt_count,
                        ),
                    )
                    self._insert_receipt(connection, receipt)
                    terminal = cast(
                        DurableProjectionJobOperationalStateFact,
                        build_projection_fact(
                            DurableProjectionJobOperationalStateFact,
                            schema_version=(
                                "durable_projection_job_operational_state.v1"
                            ),
                            status=DurableProjectionJobStatus.SUPERSEDED,
                            state_revision=state.state_revision + 1,
                            repair_generation=state.repair_generation,
                            attempt_count=0,
                            dispatch_attempt_count=state.dispatch_attempt_count,
                            settlement_generation=state.settlement_generation,
                            lease_generation=state.lease_generation,
                            lease_owner_id=None,
                            lease_expires_at=None,
                            next_attempt_at=None,
                            last_failure=None,
                            compaction_memory_deferral=None,
                            result_receipt_reference=(
                                durable_result_receipt_reference(receipt)
                            ),
                        ),
                    )
                    self._write_job_state(
                        connection,
                        job_id=candidate.job_semantic.job_id,
                        state=terminal,
                    )
                return len(rows)

    def defer_recovered_session_model_attempt(
        self,
        lease: LeasedDurableProjectionJob,
        *,
        failure: BoundedRuntimeFailureDiagnosticFact,
        delay_seconds: float,
        deadline_monotonic: float,
    ) -> DurableProjectionCommitConfirmation:
        """Defer a previously dispatched attempt without advancing its ordinal."""

        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            with connection.transaction():
                row = self._read_job_row(connection, lease.job.job_id, lock=True)
                if row is None:
                    return DurableProjectionCommitConfirmation.CONFLICT
                state = self._state_from_row(row)
                if state.status is DurableProjectionJobStatus.MODEL_RETRY_WAIT:
                    return DurableProjectionCommitConfirmation.FULL
                if (
                    state.status is not DurableProjectionJobStatus.LEASED
                    or state.lease_generation != lease.lease_generation
                    or state.lease_owner_id != lease.lease_owner_id
                    or state.dispatch_attempt_count != lease.dispatch_attempt_count
                    or state.dispatch_attempt_count < 1
                ):
                    return DurableProjectionCommitConfirmation.CONFLICT
                now = cast(
                    datetime,
                    connection.execute(
                        "SELECT clock_timestamp() AS database_now"
                    ).fetchone()["database_now"],
                )
                next_state = cast(
                    DurableProjectionJobOperationalStateFact,
                    build_projection_fact(
                        DurableProjectionJobOperationalStateFact,
                        schema_version="durable_projection_job_operational_state.v1",
                        status=DurableProjectionJobStatus.MODEL_RETRY_WAIT,
                        state_revision=state.state_revision + 1,
                        repair_generation=state.repair_generation,
                        attempt_count=state.attempt_count,
                        dispatch_attempt_count=state.dispatch_attempt_count,
                        settlement_generation=state.settlement_generation,
                        lease_generation=state.lease_generation,
                        lease_owner_id=None,
                        lease_expires_at=None,
                        next_attempt_at=now
                        + timedelta(seconds=max(0.01, delay_seconds)),
                        last_failure=failure,
                        result_receipt_reference=None,
                    ),
                )
                self._write_job_state(
                    connection,
                    job_id=lease.job.job_id,
                    state=next_state,
                )
                self._release_target_lease(connection, lease)
        return DurableProjectionCommitConfirmation.FULL

    def _select_claim_rows(
        self,
        connection: Connection,
        rows: tuple[dict[str, object], ...],
        *,
        limit: int,
    ) -> tuple[dict[str, object], ...]:
        grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
        database_now_row = connection.execute(
            "SELECT clock_timestamp() AS database_now"
        ).fetchone()
        database_now = cast(datetime, database_now_row["database_now"])
        for row in rows:
            key = (str(row["projection_kind"]), str(row["target_key"]))
            grouped.setdefault(key, []).append(row)
        selected: list[dict[str, object]] = []
        for key in sorted(grouped):
            conflict = connection.execute(
                """
                SELECT 1
                FROM durable_projection_target_authority_conflicts
                WHERE projection_kind = %s AND target_key = %s
                LIMIT 1
                """,
                key,
            ).fetchone()
            if conflict is not None:
                continue
            head = self._read_head_in_connection(
                connection,
                projection_kind=DurableProjectionKind(key[0]),
                target_key=key[1],
                lock=True,
            )
            active = connection.execute(
                """
                SELECT lease_expires_at
                FROM durable_projection_target_execution_leases
                WHERE projection_kind = %s AND target_key = %s
                FOR UPDATE
                """,
                key,
            ).fetchone()
            if (
                active is not None
                and cast(datetime, active["lease_expires_at"]) > database_now
            ):
                continue
            candidates = grouped[key]
            policy = self._candidate_from_row(
                candidates[0]
            ).job_semantic.handler_contract.target_update_policy
            if (
                policy is DurableProjectionTargetUpdatePolicy.FULL_REPLACEMENT
                and head is not None
            ):
                effective = self._read_applied_head_receipt_in_connection(
                    connection,
                    head,
                )
                advancing: list[dict[str, object]] = []
                for row in candidates:
                    candidate = self._candidate_from_row(row)
                    source = candidate.job_semantic.source_event_reference
                    state = self._state_from_row(row)
                    if source.sequence < head.applied_source_sequence:
                        self._terminalize_superseded_candidate(
                            connection,
                            candidate=candidate,
                            state=state,
                            effective=effective,
                        )
                    elif source.sequence == head.applied_source_sequence:
                        if (
                            source.reference_fingerprint
                            == head.applied_source_event_reference_fingerprint
                        ):
                            self._terminalize_existing_assignment(
                                connection,
                                candidate=candidate,
                                state=state,
                                head=head,
                            )
                        else:
                            raise ValueError(
                                "full-replacement head has a distinct source "
                                "at the same ledger sequence"
                            )
                    else:
                        advancing.append(row)
                candidates = advancing
                if not candidates:
                    continue
            if (
                policy is DurableProjectionTargetUpdatePolicy.SINGLE_ASSIGNMENT
                and head is not None
            ):
                candidates.sort(
                    key=lambda item: (
                        int(item["source_sequence"]),
                        str(item["job_id"]),
                    )
                )
                for row in candidates:
                    candidate = self._candidate_from_row(row)
                    if (
                        candidate.job_semantic.source_event_reference.reference_fingerprint
                        == head.applied_source_event_reference_fingerprint
                    ):
                        self._terminalize_existing_assignment(
                            connection,
                            candidate=candidate,
                            state=self._state_from_row(row),
                            head=head,
                        )
                    else:
                        self._dead_letter_distinct_assignment(
                            connection,
                            candidate=candidate,
                            state=self._state_from_row(row),
                            head=head,
                        )
                continue
            candidates.sort(
                key=lambda item: (
                    int(item["source_sequence"]),
                    str(item["job_id"]),
                ),
                reverse=(
                    policy is DurableProjectionTargetUpdatePolicy.FULL_REPLACEMENT
                ),
            )
            selected.append(candidates[0])
            if len(selected) >= limit:
                break
        return tuple(selected)

    def _terminalize_existing_assignment(
        self,
        connection: Connection,
        *,
        candidate: DurableProjectionJobCandidateFact,
        state: DurableProjectionJobOperationalStateFact,
        head: DurableProjectionTargetHeadFact,
    ) -> None:
        self._read_applied_head_receipt_in_connection(
            connection,
            head,
        )
        next_state = cast(
            DurableProjectionJobOperationalStateFact,
            build_projection_fact(
                DurableProjectionJobOperationalStateFact,
                schema_version="durable_projection_job_operational_state.v1",
                status=DurableProjectionJobStatus.SUCCEEDED,
                state_revision=state.state_revision + 1,
                repair_generation=state.repair_generation,
                attempt_count=state.attempt_count,
                lease_generation=state.lease_generation,
                lease_owner_id=None,
                lease_expires_at=None,
                next_attempt_at=None,
                last_failure=None,
                result_receipt_reference=(head.applied_result_receipt_reference),
            ),
        )
        self._write_job_state(
            connection,
            job_id=candidate.job_semantic.job_id,
            state=next_state,
        )

    def _terminalize_superseded_candidate(
        self,
        connection: Connection,
        *,
        candidate: DurableProjectionJobCandidateFact,
        state: DurableProjectionJobOperationalStateFact,
        effective: DurableProjectionAppliedResultReceiptFact,
    ) -> None:
        job = candidate.job_semantic
        owner = cast(
            ProjectionJobResultOwnerFact,
            build_projection_fact(
                ProjectionJobResultOwnerFact,
                schema_version="projection_job_result_owner.v1",
                owner_kind="durable_projection_job",
                job_id=job.job_id,
                job_semantic_fingerprint=job.job_semantic_fingerprint,
                job_candidate_fingerprint=candidate.candidate_fingerprint,
                source_event_reference_fingerprint=(
                    job.source_event_reference.reference_fingerprint
                ),
            ),
        )
        receipt = superseded_result_receipt_for_owner(
            candidate_result_owner=owner,
            projection_kind=job.projection_kind,
            target_key=job.target_key,
            source_event_reference=job.source_event_reference,
            effective=effective,
        )
        self._insert_receipt(connection, receipt)
        next_state = cast(
            DurableProjectionJobOperationalStateFact,
            build_projection_fact(
                DurableProjectionJobOperationalStateFact,
                schema_version=("durable_projection_job_operational_state.v1"),
                status=DurableProjectionJobStatus.SUPERSEDED,
                state_revision=state.state_revision + 1,
                repair_generation=state.repair_generation,
                attempt_count=state.attempt_count,
                lease_generation=state.lease_generation,
                lease_owner_id=None,
                lease_expires_at=None,
                next_attempt_at=None,
                last_failure=None,
                result_receipt_reference=(durable_result_receipt_reference(receipt)),
            ),
        )
        self._write_job_state(
            connection,
            job_id=job.job_id,
            state=next_state,
        )

    def _dead_letter_distinct_assignment(
        self,
        connection: Connection,
        *,
        candidate: DurableProjectionJobCandidateFact,
        state: DurableProjectionJobOperationalStateFact,
        head: DurableProjectionTargetHeadFact,
    ) -> None:
        conflict = self._target_conflict(
            candidate=candidate,
            head=head,
            conflict_kind="distinct_source_for_single_assignment",
            candidate_result_semantic_fingerprint=None,
        )
        self._insert_target_conflict(connection, conflict)
        diagnostic = _diagnostic(
            ValueError(DurableProjectionFailureKind.TARGET_AUTHORITY_CONFLICT)
        )
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
        self._write_job_state(
            connection,
            job_id=candidate.job_semantic.job_id,
            state=next_state,
        )

    @staticmethod
    def _write_job_state(
        connection: Connection,
        *,
        job_id: str,
        state: DurableProjectionJobOperationalStateFact,
    ) -> None:
        connection.execute(
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
                (_json(state.last_failure) if state.last_failure is not None else None),
                (
                    _json(state.result_receipt_reference)
                    if state.result_receipt_reference is not None
                    else None
                ),
                (
                    _json(state.compaction_memory_deferral)
                    if state.compaction_memory_deferral is not None
                    else None
                ),
                state.state_fingerprint,
                job_id,
            ),
        )

    def _dead_letter_attempts_exhausted(
        self,
        connection: Connection,
        *,
        candidate: DurableProjectionJobCandidateFact,
        state: DurableProjectionJobOperationalStateFact,
    ) -> None:
        diagnostic = _diagnostic(
            RuntimeError(DurableProjectionFailureKind.ATTEMPTS_EXHAUSTED.value)
        )
        terminal = cast(
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
        self._write_job_state(
            connection,
            job_id=candidate.job_semantic.job_id,
            state=terminal,
        )

    def settle_success(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        prepared: PreparedDurableProjectionResultFact,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionSettlementOutcome:
        deadline = deadline_monotonic or monotonic() + 20.0
        try:
            with self.connection_provider.connection(
                lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
                row_factory=dict_row,
                deadline_monotonic=deadline,
            ) as connection:
                _set_deadline(connection, deadline)
                self._validate_or_issue_guard(
                    connection,
                    admission_guard=None,
                    transaction_owner_id=(
                        "projection-settlement:" + lease.lease_fingerprint
                    ),
                )
                outcome = self._settle_success_in_transaction(
                    connection,
                    lease=lease,
                    prepared=prepared,
                )
            return outcome
        except BaseException as error:
            return self._confirm_success_settlement(
                lease=lease,
                prepared=prepared,
                deadline_monotonic=deadline,
                error=error,
            )

    def _settle_success_in_transaction(
        self,
        connection: Connection,
        *,
        lease: LeasedDurableProjectionJob,
        prepared: PreparedDurableProjectionResultFact,
    ) -> DurableProjectionSettlementOutcome:
        row, candidate, state = self._validated_lease_row(connection, lease)
        del row
        try:
            validate_prepared_job_result(
                candidate=candidate,
                lease=lease,
                prepared=prepared,
            )
        except ValueError:
            return self._settle_result_conflict(
                connection,
                lease=lease,
                candidate=candidate,
                state=state,
                candidate_result_semantic_fingerprint=(
                    prepared.result_semantic.result_semantic_fingerprint
                ),
            )
        head = self._read_head_in_connection(
            connection,
            projection_kind=lease.job.projection_kind,
            target_key=lease.job.target_key,
            lock=True,
        )
        policy = lease.job.handler_contract.target_update_policy
        source_sequence = lease.job.source_event_reference.sequence
        if head is not None:
            effective = self._read_applied_head_receipt_in_connection(
                connection,
                head,
            )
            same_source = (
                head.applied_source_event_reference_fingerprint
                == lease.job.source_event_reference.reference_fingerprint
            )
            same_result = (
                effective.result_semantic.result_semantic_fingerprint
                == prepared.result_semantic.result_semantic_fingerprint
            )
            if policy is DurableProjectionTargetUpdatePolicy.SINGLE_ASSIGNMENT:
                if not same_source:
                    return self._settle_target_conflict(
                        connection,
                        lease=lease,
                        candidate=candidate,
                        state=state,
                        head=head,
                        conflict_kind=("distinct_source_for_single_assignment"),
                        candidate_result_semantic_fingerprint=None,
                    )
                if not same_result:
                    return self._settle_target_conflict(
                        connection,
                        lease=lease,
                        candidate=candidate,
                        state=state,
                        head=head,
                        conflict_kind="same_source_different_result",
                        candidate_result_semantic_fingerprint=(
                            prepared.result_semantic.result_semantic_fingerprint
                        ),
                    )
                return self._terminalize_job(
                    connection,
                    lease=lease,
                    candidate=candidate,
                    state=state,
                    status=DurableProjectionJobStatus.SUCCEEDED,
                    receipt=effective,
                )
            if source_sequence < head.applied_source_sequence:
                receipt = superseded_result_receipt(
                    lease=lease,
                    prepared=prepared,
                    effective=effective,
                )
                self._insert_receipt(connection, receipt)
                return self._terminalize_job(
                    connection,
                    lease=lease,
                    candidate=candidate,
                    state=state,
                    status=DurableProjectionJobStatus.SUPERSEDED,
                    receipt=receipt,
                )
            if source_sequence == head.applied_source_sequence:
                if not same_source or not same_result:
                    return self._settle_target_conflict(
                        connection,
                        lease=lease,
                        candidate=candidate,
                        state=state,
                        head=head,
                        conflict_kind="same_source_different_result",
                        candidate_result_semantic_fingerprint=(
                            prepared.result_semantic.result_semantic_fingerprint
                        ),
                    )
                return self._terminalize_job(
                    connection,
                    lease=lease,
                    candidate=candidate,
                    state=state,
                    status=DurableProjectionJobStatus.SUCCEEDED,
                    receipt=effective,
                )

        self._persist_prepared_documents(
            connection,
            source_event_reference=(candidate.job_semantic.source_event_reference),
            prepared=prepared,
            expected_head=head,
        )
        if prepared.canonical_mutation_candidates:
            from pulsara_agent.runtime.projection_jobs.postgres_canonical_mutation_repository import (
                PostgresCanonicalMutationRepository,
            )

            PostgresCanonicalMutationRepository.append_candidates_in_transaction(
                connection,
                source_owner=projection_result_mutation_owner(
                    prepared=prepared,
                    source_event_reference=(
                        candidate.job_semantic.source_event_reference
                    ),
                ),
                surface_plan=candidate.canonical_mutation_surface_plan,
                candidates=prepared.canonical_mutation_candidates,
            )
        receipt = applied_result_receipt(
            lease=lease,
            prepared=prepared,
            target_head_revision=head.head_revision + 1 if head else 1,
        )
        self._insert_receipt(connection, receipt)
        resulting_head = target_head_from_applied_receipt(receipt)
        self._write_target_head(
            connection,
            expected=head,
            resulting=resulting_head,
        )
        return self._terminalize_job(
            connection,
            lease=lease,
            candidate=candidate,
            state=state,
            status=DurableProjectionJobStatus.SUCCEEDED,
            receipt=receipt,
        )

    @classmethod
    def commit_pre_activation_in_transaction(
        cls,
        connection: Connection,
        *,
        prepared_result: PreparedDurableProjectionResultFact,
        admission_guard: RuntimeWriteAdmissionGuard,
    ) -> PreActivationProjectionCommitOutcomeFact:
        """Commit one transitional hook result without manufacturing a job."""

        owner = prepared_result.result_owner
        if not isinstance(owner, PreActivationHookResultOwnerFact):
            raise ValueError("pre-activation commit requires hook-owned result")
        epoch = admission_guard.admission_epoch
        if (
            epoch.mode.value != "maintenance"
            or epoch.maintenance_operation_id is None
            or admission_guard.maintenance_authority_fingerprint is None
        ):
            raise ValueError("pre-activation commit requires maintenance guard")
        contract_row = connection.execute(
            """
            SELECT contract_payload, contract_fingerprint
            FROM durable_projection_pre_activation_contracts
            WHERE projection_kind = %s
            FOR UPDATE
            """,
            (owner.projection_kind.value,),
        ).fetchone()
        if contract_row is None:
            raise ValueError("pre-activation hook contract is absent")
        contract = PreActivationProjectionHookContractFact.model_validate(
            contract_row["contract_payload"]
            if isinstance(contract_row, dict)
            else contract_row[0]
        )
        stored_contract_fingerprint = str(
            contract_row["contract_fingerprint"]
            if isinstance(contract_row, dict)
            else contract_row[1]
        )
        if (
            contract.contract_fingerprint != stored_contract_fingerprint
            or owner.hook_contract_fingerprint != contract.contract_fingerprint
            or contract.contract_semantic.projection_kind is not owner.projection_kind
            or prepared_result.result_semantic.projection_kind
            is not owner.projection_kind
        ):
            raise ValueError("pre-activation hook contract rebind failed")
        activated = connection.execute(
            """
            SELECT 1 FROM durable_projection_kind_activations
            WHERE projection_kind = %s
            """,
            (owner.projection_kind.value,),
        ).fetchone()
        if activated is not None:
            raise ValueError("pre-activation writer is disabled after activation")

        event_row = (
            connection.cursor(row_factory=dict_row)
            .execute(
                """
            SELECT id, session_id, run_id, turn_id, reply_id, sequence,
                   event_type, event_schema_version,
                   event_schema_fingerprint,
                   event_domain_contract_fingerprint,
                   transcript_semantic_prefix_count,
                   transcript_semantic_prefix_accumulator,
                   ledger_continuity_accumulator,
                   ledger_payload_prefix_bytes,
                   created_at, payload
            FROM agent_events
            WHERE id = %s
            """,
                (owner.source_event_reference.event_id,),
            )
            .fetchone()
        )
        if event_row is None:
            raise ValueError("pre-activation source event is absent")
        bound = _bound_event_from_row(dict(event_row))
        if bound.source_reference != owner.source_event_reference:
            raise ValueError("pre-activation source event exact rebind failed")
        semantic = contract.contract_semantic
        matching_bindings = tuple(
            item
            for item in semantic.ordered_trigger_bindings
            if item.trigger_event_type == bound.envelope.event_type
            and bound.envelope.event_schema_fingerprint
            in item.accepted_event_schema_fingerprints
        )
        if len(matching_bindings) != 1:
            raise ValueError("pre-activation source schema is not accepted")
        decoded = decode_raw_stored_event_envelope(
            bound.envelope, DEFAULT_EVENT_SCHEMA_REGISTRY
        )
        tool_call_id = (
            decoded.tool_call_id if isinstance(decoded, ToolResultEndEvent) else None
        )
        target_key = projection_target_key(
            projection_kind=owner.projection_kind,
            runtime_session_id=owner.source_event_reference.runtime_session_id,
            run_id=owner.source_event_reference.run_id,
            tool_call_id=tool_call_id,
        )

        document_semantics = tuple(
            document_semantic_fingerprint(item)
            for item in prepared_result.ordered_documents
        )
        mutation_semantics = tuple(
            item.mutation_semantic.mutation_semantic_fingerprint
            for item in prepared_result.canonical_mutation_candidates
        )
        if (
            prepared_result.result_semantic.ordered_document_semantic_fingerprints
            != document_semantics
            or prepared_result.result_semantic.ordered_canonical_mutation_semantic_fingerprints
            != mutation_semantics
            or any(
                item.source_owner_fingerprint
                != projection_result_mutation_owner(
                    prepared=prepared_result,
                    source_event_reference=owner.source_event_reference,
                ).owner_fingerprint
                for item in prepared_result.canonical_mutation_candidates
            )
        ):
            raise ValueError("pre-activation prepared result drifted")

        head = cls._read_head_in_connection(
            connection,
            projection_kind=owner.projection_kind,
            target_key=target_key,
            lock=True,
        )
        source_reference = owner.source_event_reference
        if head is not None:
            effective = cls._read_applied_head_receipt_in_connection(
                connection,
                head,
            )
            same_source = (
                head.applied_source_event_reference_fingerprint
                == source_reference.reference_fingerprint
            )
            same_result = (
                effective.result_semantic.result_semantic_fingerprint
                == prepared_result.result_semantic.result_semantic_fingerprint
            )
            if (
                semantic.handler_contract.target_update_policy
                is DurableProjectionTargetUpdatePolicy.SINGLE_ASSIGNMENT
            ):
                if not same_source or not same_result:
                    return cls._pre_activation_outcome(
                        confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                        owner=owner,
                        receipt_reference=None,
                        head=head,
                        failure=_diagnostic(
                            ValueError(
                                "single-assignment pre-activation target conflict"
                            )
                        ),
                    )
                return cls._pre_activation_outcome(
                    confirmation=DurableProjectionCommitConfirmation.FULL,
                    owner=owner,
                    receipt_reference=(head.applied_result_receipt_reference),
                    head=head,
                    failure=None,
                )
            if source_reference.sequence < head.applied_source_sequence:
                superseded = superseded_result_receipt_for_source(
                    prepared=prepared_result,
                    projection_kind=owner.projection_kind,
                    target_key=target_key,
                    source_event_reference=source_reference,
                    effective=effective,
                )
                cls._insert_receipt(connection, superseded)
                return cls._pre_activation_outcome(
                    confirmation=DurableProjectionCommitConfirmation.FULL,
                    owner=owner,
                    receipt_reference=durable_result_receipt_reference(superseded),
                    head=head,
                    failure=None,
                )
            if source_reference.sequence == head.applied_source_sequence:
                if not same_source or not same_result:
                    return cls._pre_activation_outcome(
                        confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                        owner=owner,
                        receipt_reference=None,
                        head=head,
                        failure=_diagnostic(
                            ValueError("same-sequence pre-activation result conflict")
                        ),
                    )
                return cls._pre_activation_outcome(
                    confirmation=DurableProjectionCommitConfirmation.FULL,
                    owner=owner,
                    receipt_reference=(head.applied_result_receipt_reference),
                    head=head,
                    failure=None,
                )

        cls._persist_prepared_documents(
            connection,
            source_event_reference=source_reference,
            prepared=prepared_result,
            expected_head=head,
        )
        if prepared_result.canonical_mutation_candidates:
            from pulsara_agent.runtime.projection_jobs.postgres_canonical_mutation_repository import (
                PostgresCanonicalMutationRepository,
            )

            PostgresCanonicalMutationRepository.append_candidates_in_transaction(
                connection,
                source_owner=projection_result_mutation_owner(
                    prepared=prepared_result,
                    source_event_reference=source_reference,
                ),
                surface_plan=semantic.canonical_mutation_surface_plan,
                candidates=prepared_result.canonical_mutation_candidates,
            )
        receipt = applied_result_receipt_for_source(
            prepared=prepared_result,
            target_key=target_key,
            source_event_reference=source_reference,
            target_head_revision=head.head_revision + 1 if head else 1,
        )
        cls._insert_receipt(connection, receipt)
        resulting_head = target_head_from_applied_receipt(receipt)
        cls._write_target_head(
            connection,
            expected=head,
            resulting=resulting_head,
        )
        return cls._pre_activation_outcome(
            confirmation=DurableProjectionCommitConfirmation.FULL,
            owner=owner,
            receipt_reference=durable_result_receipt_reference(receipt),
            head=resulting_head,
            failure=None,
        )

    @staticmethod
    def _pre_activation_outcome(
        *,
        confirmation: DurableProjectionCommitConfirmation,
        owner: PreActivationHookResultOwnerFact,
        receipt_reference: DurableProjectionResultReceiptReferenceFact | None,
        head: DurableProjectionTargetHeadFact | None,
        failure: BoundedRuntimeFailureDiagnosticFact | None,
    ) -> PreActivationProjectionCommitOutcomeFact:
        return cast(
            PreActivationProjectionCommitOutcomeFact,
            build_projection_fact(
                PreActivationProjectionCommitOutcomeFact,
                schema_version="pre_activation_projection_commit_outcome.v1",
                confirmation=confirmation,
                attempted_result_owner_fingerprint=owner.owner_fingerprint,
                result_receipt_reference=receipt_reference,
                resulting_target_head_fingerprint=(
                    head.head_fingerprint if head is not None else None
                ),
                failure=failure,
            ),
        )

    def settle_failure(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        failure_kind: DurableProjectionFailureKind,
        error: BaseException,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionSettlementOutcome:
        deadline = deadline_monotonic or monotonic() + 20.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            self._validate_or_issue_guard(
                connection,
                admission_guard=None,
                transaction_owner_id=("projection-failure:" + lease.lease_fingerprint),
            )
            _, candidate, state = self._validated_lease_row(
                connection,
                lease,
            )
            retryable = failure_kind in {
                DurableProjectionFailureKind.TRANSIENT_STORAGE_UNAVAILABLE,
                DurableProjectionFailureKind.TRANSIENT_EXTERNAL_SURFACE_UNAVAILABLE,
                DurableProjectionFailureKind.DEADLINE_EXCEEDED,
                DurableProjectionFailureKind.SOURCE_NOT_READY,
            }
            exhausted = (
                state.attempt_count
                >= candidate.delivery_policy.retry_policy.maximum_attempts
            )
            diagnostic = _diagnostic(error)
            if retryable and not exhausted:
                database_now_row = connection.execute(
                    "SELECT clock_timestamp() AS database_now"
                ).fetchone()
                database_now = cast(
                    datetime,
                    database_now_row["database_now"],
                )
                multiplier = 2 ** max(0, state.attempt_count - 1)
                delay = min(
                    candidate.delivery_policy.retry_policy.maximum_delay_milliseconds,
                    candidate.delivery_policy.retry_policy.base_delay_milliseconds
                    * multiplier,
                )
                status = DurableProjectionJobStatus.RETRY_WAIT
                next_attempt_at = database_now + timedelta(milliseconds=delay)
                confirmation = DurableProjectionCommitConfirmation.FULL
            else:
                status = DurableProjectionJobStatus.DEAD_LETTER
                next_attempt_at = None
                confirmation = (
                    DurableProjectionCommitConfirmation.FULL
                    if exhausted
                    else DurableProjectionCommitConfirmation.CONFLICT
                )
            next_state = cast(
                DurableProjectionJobOperationalStateFact,
                build_projection_fact(
                    DurableProjectionJobOperationalStateFact,
                    schema_version=("durable_projection_job_operational_state.v1"),
                    status=status,
                    state_revision=state.state_revision + 1,
                    repair_generation=state.repair_generation,
                    attempt_count=state.attempt_count,
                    lease_generation=state.lease_generation,
                    lease_owner_id=None,
                    lease_expires_at=None,
                    next_attempt_at=next_attempt_at,
                    last_failure=diagnostic,
                    result_receipt_reference=None,
                ),
            )
            self._write_job_state(
                connection,
                job_id=candidate.job_semantic.job_id,
                state=next_state,
            )
            self._release_target_lease(connection, lease)
            return self._settlement_outcome(
                confirmation=confirmation,
                lease=lease,
                state=next_state,
                receipt_reference=None,
                failure=diagnostic,
            )

    def release_lease(
        self,
        lease: LeasedDurableProjectionJob,
        *,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionJobOperationalStateFact:
        deadline = deadline_monotonic or monotonic() + 20.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            self._validate_or_issue_guard(
                connection,
                admission_guard=None,
                transaction_owner_id=("projection-release:" + lease.lease_fingerprint),
            )
            _, candidate, state = self._validated_lease_row(
                connection,
                lease,
            )
            next_state = cast(
                DurableProjectionJobOperationalStateFact,
                build_projection_fact(
                    DurableProjectionJobOperationalStateFact,
                    schema_version=("durable_projection_job_operational_state.v1"),
                    status=DurableProjectionJobStatus.RETRY_WAIT,
                    state_revision=state.state_revision + 1,
                    repair_generation=state.repair_generation,
                    attempt_count=state.attempt_count,
                    lease_generation=state.lease_generation,
                    lease_owner_id=None,
                    lease_expires_at=None,
                    next_attempt_at=connection.execute(
                        "SELECT clock_timestamp() AS database_now"
                    ).fetchone()["database_now"],
                    last_failure=_diagnostic(
                        RuntimeError("projection physical attempt cancelled")
                    ),
                    result_receipt_reference=None,
                ),
            )
            self._write_job_state(
                connection,
                job_id=candidate.job_semantic.job_id,
                state=next_state,
            )
            self._release_target_lease(connection, lease)
            return next_state

    def _validated_lease_row(
        self,
        connection: Connection,
        lease: LeasedDurableProjectionJob,
    ) -> tuple[
        dict[str, object],
        DurableProjectionJobCandidateFact,
        DurableProjectionJobOperationalStateFact,
    ]:
        row = self._read_job_row(connection, lease.job.job_id, lock=True)
        if row is None:
            raise ValueError("leased projection job is absent")
        candidate = self._candidate_from_row(row)
        state = self._state_from_row(row)
        target_row = connection.execute(
            """
            SELECT lease_payload, lease_fingerprint,
                   lease_owner_id, lease_generation, lease_expires_at
            FROM durable_projection_target_execution_leases
            WHERE projection_kind = %s AND target_key = %s
            FOR UPDATE
            """,
            (lease.job.projection_kind.value, lease.job.target_key),
        ).fetchone()
        if target_row is None:
            raise ValueError("projection target execution lease is absent")
        target = DurableProjectionTargetExecutionLeaseFact.model_validate(
            target_row["lease_payload"]
        )
        if (
            candidate.job_semantic != lease.job
            or candidate.candidate_fingerprint != lease.job_candidate_fingerprint
            or candidate.activation_fingerprint != lease.activation_fingerprint
            or candidate.seed_contract_fingerprint != lease.seed_contract_fingerprint
            or state.status is not DurableProjectionJobStatus.LEASED
            or state.state_revision != lease.expected_state_revision
            or state.repair_generation != lease.repair_generation
            or state.attempt_count != lease.attempt_count
            or state.lease_generation != lease.lease_generation
            or state.lease_owner_id != lease.lease_owner_id
            or target.owner_job_id != lease.job.job_id
            or target.lease_generation != lease.lease_generation
            or target.lease_owner_id != lease.lease_owner_id
            or target.state_revision != lease.expected_state_revision
            or target.lease_fingerprint != str(target_row["lease_fingerprint"])
        ):
            raise ValueError("projection job lease is stale")
        return row, candidate, state

    @classmethod
    def _persist_prepared_documents(
        cls,
        connection: Connection,
        *,
        source_event_reference: DurableProjectionSourceEventReferenceFact,
        prepared: PreparedDurableProjectionResultFact,
        expected_head: DurableProjectionTargetHeadFact | None,
    ) -> None:
        from pulsara_agent.projection_jobs.contracts import (
            PreparedDurableProjectionArtifactDocumentFact,
            PreparedDurableProjectionGraphDocumentFact,
            PreparedDurableProjectionGraphRelationFact,
        )

        for document in prepared.ordered_documents:
            if isinstance(
                document,
                PreparedDurableProjectionArtifactDocumentFact,
            ):
                digest = document.content_sha256
                inserted = connection.execute(
                    """
                    INSERT INTO artifacts (
                        id, session_id, run_id, media_type, text_body,
                        digest, size_bytes, stored_at, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        document.artifact_reference.artifact_semantic_id,
                        source_event_reference.runtime_session_id,
                        source_event_reference.run_id,
                        document.media_type,
                        document.canonical_content_utf8,
                        digest,
                        document.content_utf8_bytes,
                        (
                            "postgres://artifacts/"
                            + document.artifact_reference.artifact_semantic_id
                        ),
                        Jsonb(
                            {
                                "artifact_semantic_fingerprint": (
                                    document.artifact_reference.artifact_semantic_fingerprint
                                ),
                                "content_codec_contract_fingerprint": (
                                    document.content_codec_contract_fingerprint
                                ),
                                "metadata_contract_fingerprint": (
                                    document.metadata_contract_fingerprint
                                ),
                            }
                        ),
                    ),
                ).fetchone()
                if inserted is None:
                    row = connection.execute(
                        """
                        SELECT media_type, text_body, digest, size_bytes
                        FROM artifacts WHERE id = %s
                        """,
                        (document.artifact_reference.artifact_semantic_id,),
                    ).fetchone()
                    if row is None or (
                        str(row["media_type"]),
                        str(row["text_body"]),
                        str(row["digest"]),
                        int(row["size_bytes"]),
                    ) != (
                        document.media_type,
                        document.canonical_content_utf8,
                        digest,
                        document.content_utf8_bytes,
                    ):
                        raise ValueError("projection artifact identity conflict")
            elif isinstance(
                document,
                PreparedDurableProjectionGraphDocumentFact,
            ):
                payload = json.loads(document.canonical_json_utf8)
                if not isinstance(payload, dict):
                    raise ValueError("projection graph document must be an object")
                inserted = connection.execute(
                    """
                    INSERT INTO graph_documents (
                        graph_id, id, type, payload, updated_at
                    ) VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (graph_id, id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        document.graph_id,
                        document.semantic_document_id,
                        document.graph_document_type,
                        Jsonb(payload),
                    ),
                ).fetchone()
                if inserted is None:
                    row = connection.execute(
                        """
                        SELECT type, payload FROM graph_documents
                        WHERE graph_id = %s AND id = %s
                        """,
                        (
                            document.graph_id,
                            document.semantic_document_id,
                        ),
                    ).fetchone()
                    if row is None:
                        raise ValueError("projection graph document disappeared")
                    current = (
                        str(row["type"]),
                        dict(row["payload"]),
                    )
                    if current == (document.graph_document_type, payload):
                        continue
                    if (
                        prepared.result_semantic.projection_kind
                        is not DurableProjectionKind.RUN_TIMELINE
                        or expected_head is None
                    ):
                        raise ValueError("projection graph document identity conflict")
                    expected_receipt = cls._read_applied_head_receipt_in_connection(
                        connection,
                        expected_head,
                    )
                    prior_refs = tuple(
                        item
                        for item in expected_receipt.result_document_references
                        if getattr(item, "document_kind", None) == "graph_document"
                        and getattr(item, "graph_id", None) == document.graph_id
                        and getattr(item, "semantic_document_id", None)
                        == document.semantic_document_id
                    )
                    if len(prior_refs) != 1:
                        raise ValueError(
                            "timeline graph replacement lacks exact prior reference"
                        )
                    current_payload = canonical_json_bytes(current[1])
                    current_sha = f"sha256:{sha256(current_payload).hexdigest()}"
                    if (
                        current[0] != prior_refs[0].graph_document_type
                        or current_sha != prior_refs[0].canonical_json_sha256
                        or len(current_payload)
                        != prior_refs[0].canonical_json_utf8_bytes
                    ):
                        raise ValueError("timeline graph replacement prior CAS failed")
                    connection.execute(
                        """
                        UPDATE graph_documents
                        SET type = %s, payload = %s, updated_at = now()
                        WHERE graph_id = %s AND id = %s
                        """,
                        (
                            document.graph_document_type,
                            Jsonb(payload),
                            document.graph_id,
                            document.semantic_document_id,
                        ),
                    )
            elif isinstance(
                document,
                PreparedDurableProjectionGraphRelationFact,
            ):
                relation = document.relation_reference
                relation_kind_by_predicate = {
                    "https://pulsara.dev/runtime#produced": (
                        "turn_produced_tool_result"
                    ),
                    "https://pulsara.dev/runtime#provides": (
                        "tool_result_provides_artifact"
                    ),
                }
                try:
                    relation_kind = relation_kind_by_predicate[relation.predicate_iri]
                except KeyError as error:
                    raise ValueError(
                        "projection relation predicate is not registered"
                    ) from error
                row = cast(
                    CanonicalGraphRelationRowFact,
                    build_projection_fact(
                        CanonicalGraphRelationRowFact,
                        schema_version="canonical_graph_relation_row.v1",
                        relation_id=relation.relation_id,
                        graph_id=relation.graph_id,
                        relation_kind=relation_kind,
                        source_document_id=relation.source_document_id,
                        predicate_iri=relation.predicate_iri,
                        target_document_id=relation.target_document_id,
                        relation_semantic_fingerprint=(
                            relation.relation_semantic_fingerprint
                        ),
                        source_authority_fingerprint=(
                            document.source_authority_fingerprint
                        ),
                        lowering_contract_fingerprint=(
                            relation.lowering_contract_fingerprint
                        ),
                    ),
                )
                from pulsara_agent.graph.projection_relations import (
                    PostgresCanonicalGraphRelationRepository,
                )

                PostgresCanonicalGraphRelationRepository._put_row(
                    connection,
                    row,
                )

    @staticmethod
    def _read_head_in_connection(
        connection: Connection,
        *,
        projection_kind: DurableProjectionKind,
        target_key: str,
        lock: bool,
    ) -> DurableProjectionTargetHeadFact | None:
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            """
            SELECT source_sequence, head_payload, head_fingerprint
            FROM durable_projection_target_heads
            WHERE projection_kind = %s AND target_key = %s
            """
            + suffix,
            (projection_kind.value, target_key),
        ).fetchone()
        if row is None:
            return None
        head = DurableProjectionTargetHeadFact.model_validate(row["head_payload"])
        if (
            head.projection_kind is not projection_kind
            or head.target_key != target_key
            or head.applied_source_sequence != int(row["source_sequence"])
            or head.head_fingerprint != str(row["head_fingerprint"])
        ):
            raise ValueError("projection target head row drifted")
        return head

    @staticmethod
    def _read_receipt_in_connection(
        connection: Connection,
        receipt_id: str,
    ) -> DurableProjectionResultReceiptFact:
        row = connection.execute(
            """
            SELECT receipt_payload, receipt_fingerprint
            FROM durable_projection_result_receipts
            WHERE receipt_id = %s
            """,
            (receipt_id,),
        ).fetchone()
        if row is None:
            raise ValueError("projection result receipt is absent")
        receipt = _RECEIPT_ADAPTER.validate_python(row["receipt_payload"])
        if receipt.receipt_fingerprint != str(row["receipt_fingerprint"]):
            raise ValueError("projection result receipt row drifted")
        return receipt

    @classmethod
    def _read_applied_head_receipt_in_connection(
        cls,
        connection: Connection,
        head: DurableProjectionTargetHeadFact,
    ) -> DurableProjectionAppliedResultReceiptFact:
        receipt = cls._read_receipt_in_connection(
            connection,
            head.applied_result_receipt_reference.receipt_id,
        )
        return exact_applied_head_receipt(head=head, receipt=receipt)

    @classmethod
    def _insert_receipt(
        cls,
        connection: Connection,
        receipt: DurableProjectionResultReceiptFact,
    ) -> None:
        if isinstance(receipt, DurableProjectionAppliedResultReceiptFact):
            projection_kind = receipt.result_semantic.projection_kind
            candidate_sequence = receipt.source_sequence
            effective_sequence = receipt.source_sequence
            semantic_fingerprint = receipt.result_semantic.result_semantic_fingerprint
        elif isinstance(receipt, CompactionMemoryExtractionProjectionResultReceiptFact):
            projection_kind = receipt.projection_kind
            candidate_sequence = receipt.source_request_event_reference.sequence
            effective_sequence = candidate_sequence
            semantic_fingerprint = receipt.completed_result_semantic_fingerprint
        elif isinstance(receipt, CompactionMemoryExtractionSupersededReceiptFact):
            projection_kind = receipt.projection_kind
            candidate_sequence = receipt.source_request_event_reference.sequence
            effective_sequence = candidate_sequence
            semantic_fingerprint = context_fingerprint(
                "compaction-memory-extraction-supersession-semantic:v1",
                (
                    receipt.source_request_event_reference.reference_fingerprint,
                    receipt.supersession_reason,
                ),
            )
        else:
            effective = cls._read_receipt_in_connection(
                connection,
                receipt.effective_applied_result_receipt_reference.receipt_id,
            )
            if not isinstance(
                effective,
                DurableProjectionAppliedResultReceiptFact,
            ):
                raise ValueError("superseded receipt effective branch drifted")
            projection_kind = receipt.projection_kind
            candidate_sequence = receipt.candidate_source_sequence
            effective_sequence = effective.source_sequence
            semantic_fingerprint = effective.result_semantic.result_semantic_fingerprint
        inserted = connection.execute(
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
                projection_kind.value,
                receipt.target_key,
                candidate_sequence,
                effective_sequence,
                semantic_fingerprint,
                _json(receipt),
                receipt.receipt_fingerprint,
            ),
        ).fetchone()
        if inserted is None:
            existing = cls._read_receipt_in_connection(
                connection,
                receipt.receipt_id,
            )
            if existing != receipt:
                raise ValueError("projection result receipt identity conflict")

    @staticmethod
    def _write_target_head(
        connection: Connection,
        *,
        expected: DurableProjectionTargetHeadFact | None,
        resulting: DurableProjectionTargetHeadFact,
    ) -> None:
        if expected is None:
            row = connection.execute(
                """
                INSERT INTO durable_projection_target_heads (
                    projection_kind, target_key, source_sequence,
                    head_payload, head_fingerprint
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (projection_kind, target_key) DO NOTHING
                RETURNING head_fingerprint
                """,
                (
                    resulting.projection_kind.value,
                    resulting.target_key,
                    resulting.applied_source_sequence,
                    _json(resulting),
                    resulting.head_fingerprint,
                ),
            ).fetchone()
        else:
            row = connection.execute(
                """
                UPDATE durable_projection_target_heads
                SET source_sequence = %s, head_payload = %s,
                    head_fingerprint = %s, updated_at = now()
                WHERE projection_kind = %s AND target_key = %s
                  AND head_fingerprint = %s
                RETURNING head_fingerprint
                """,
                (
                    resulting.applied_source_sequence,
                    _json(resulting),
                    resulting.head_fingerprint,
                    resulting.projection_kind.value,
                    resulting.target_key,
                    expected.head_fingerprint,
                ),
            ).fetchone()
        if row is None or str(row["head_fingerprint"]) != resulting.head_fingerprint:
            raise ValueError("projection target head compare-and-set failed")

    def _terminalize_job(
        self,
        connection: Connection,
        *,
        lease: LeasedDurableProjectionJob,
        candidate: DurableProjectionJobCandidateFact,
        state: DurableProjectionJobOperationalStateFact,
        status: DurableProjectionJobStatus,
        receipt: DurableProjectionResultReceiptFact,
    ) -> DurableProjectionSettlementOutcome:
        reference = durable_result_receipt_reference(receipt)
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
        self._write_job_state(
            connection,
            job_id=candidate.job_semantic.job_id,
            state=next_state,
        )
        self._release_target_lease(connection, lease)
        return self._settlement_outcome(
            confirmation=DurableProjectionCommitConfirmation.FULL,
            lease=lease,
            state=next_state,
            receipt_reference=reference,
            failure=None,
        )

    @staticmethod
    def _release_target_lease(
        connection: Connection,
        lease: LeasedDurableProjectionJob,
    ) -> None:
        row = connection.execute(
            """
            SELECT lease_payload, lease_fingerprint
            FROM durable_projection_target_execution_leases
            WHERE projection_kind = %s AND target_key = %s
              AND owner_job_id = %s AND lease_generation = %s
              AND lease_owner_id = %s
            FOR UPDATE
            """,
            (
                lease.job.projection_kind.value,
                lease.job.target_key,
                lease.job.job_id,
                lease.lease_generation,
                lease.lease_owner_id,
            ),
        ).fetchone()
        if row is None:
            raise ValueError("projection target lease release CAS failed")
        current = DurableProjectionTargetExecutionLeaseFact.model_validate(
            row["lease_payload"]
        )
        released_at = datetime(1970, 1, 1, tzinfo=current.lease_expires_at.tzinfo)
        released = cast(
            DurableProjectionTargetExecutionLeaseFact,
            build_projection_fact(
                DurableProjectionTargetExecutionLeaseFact,
                schema_version=("durable_projection_target_execution_lease.v1"),
                projection_kind=current.projection_kind,
                target_key=current.target_key,
                owner_job_id=current.owner_job_id,
                owner_source_sequence=current.owner_source_sequence,
                lease_generation=current.lease_generation,
                lease_owner_id=current.lease_owner_id,
                lease_expires_at=released_at,
                state_revision=current.state_revision,
            ),
        )
        updated = connection.execute(
            """
            UPDATE durable_projection_target_execution_leases
            SET lease_expires_at = %s,
                lease_payload = %s,
                lease_fingerprint = %s
            WHERE projection_kind = %s AND target_key = %s
              AND owner_job_id = %s AND lease_generation = %s
              AND lease_owner_id = %s AND lease_fingerprint = %s
            RETURNING owner_job_id
            """,
            (
                released_at,
                _json(released),
                released.lease_fingerprint,
                lease.job.projection_kind.value,
                lease.job.target_key,
                lease.job.job_id,
                lease.lease_generation,
                lease.lease_owner_id,
                str(row["lease_fingerprint"]),
            ),
        ).fetchone()
        if updated is None:
            raise ValueError("projection target lease release CAS failed")

    def _settle_target_conflict(
        self,
        connection: Connection,
        *,
        lease: LeasedDurableProjectionJob,
        candidate: DurableProjectionJobCandidateFact,
        state: DurableProjectionJobOperationalStateFact,
        head: DurableProjectionTargetHeadFact,
        conflict_kind: str,
        candidate_result_semantic_fingerprint: str | None,
    ) -> DurableProjectionSettlementOutcome:
        conflict = self._target_conflict(
            candidate=candidate,
            head=head,
            conflict_kind=conflict_kind,
            candidate_result_semantic_fingerprint=(
                candidate_result_semantic_fingerprint
            ),
        )
        self._insert_target_conflict(connection, conflict)
        diagnostic = _diagnostic(
            ValueError(DurableProjectionFailureKind.TARGET_AUTHORITY_CONFLICT)
        )
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
        self._write_job_state(
            connection,
            job_id=candidate.job_semantic.job_id,
            state=next_state,
        )
        self._release_target_lease(connection, lease)
        return self._settlement_outcome(
            confirmation=DurableProjectionCommitConfirmation.CONFLICT,
            lease=lease,
            state=next_state,
            receipt_reference=None,
            failure=diagnostic,
        )

    def _settle_result_conflict(
        self,
        connection: Connection,
        *,
        lease: LeasedDurableProjectionJob,
        candidate: DurableProjectionJobCandidateFact,
        state: DurableProjectionJobOperationalStateFact,
        candidate_result_semantic_fingerprint: str,
    ) -> DurableProjectionSettlementOutcome:
        head = self._read_head_in_connection(
            connection,
            projection_kind=lease.job.projection_kind,
            target_key=lease.job.target_key,
            lock=True,
        )
        if head is not None:
            return self._settle_target_conflict(
                connection,
                lease=lease,
                candidate=candidate,
                state=state,
                head=head,
                conflict_kind="same_source_different_result",
                candidate_result_semantic_fingerprint=(
                    candidate_result_semantic_fingerprint
                ),
            )
        diagnostic = _diagnostic(
            ValueError(DurableProjectionFailureKind.RESULT_IDENTITY_CONFLICT)
        )
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
        self._write_job_state(
            connection,
            job_id=candidate.job_semantic.job_id,
            state=next_state,
        )
        self._release_target_lease(connection, lease)
        return self._settlement_outcome(
            confirmation=DurableProjectionCommitConfirmation.CONFLICT,
            lease=lease,
            state=next_state,
            receipt_reference=None,
            failure=diagnostic,
        )

    def _target_conflict(
        self,
        *,
        candidate: DurableProjectionJobCandidateFact,
        head: DurableProjectionTargetHeadFact,
        conflict_kind: str,
        candidate_result_semantic_fingerprint: str | None,
    ) -> DurableProjectionTargetAuthorityConflictFact:
        job = candidate.job_semantic
        conflict_id = "projection-target-conflict:" + context_fingerprint(
            "durable-projection-target-authority-conflict-id:v1",
            {
                "projection_kind": job.projection_kind.value,
                "target_key": job.target_key,
                "conflict_kind": conflict_kind,
                "candidate_source_event_reference_fingerprint": (
                    job.source_event_reference.reference_fingerprint
                ),
                "candidate_result_semantic_fingerprint": (
                    candidate_result_semantic_fingerprint
                ),
                "existing_head_fingerprint": head.head_fingerprint,
                "existing_applied_result_receipt_fingerprint": (
                    head.applied_result_receipt_reference.receipt_fingerprint
                ),
                "handler_contract_fingerprint": (
                    job.handler_contract.contract_fingerprint
                ),
            },
        )
        return cast(
            DurableProjectionTargetAuthorityConflictFact,
            build_projection_fact(
                DurableProjectionTargetAuthorityConflictFact,
                schema_version=("durable_projection_target_authority_conflict.v1"),
                conflict_id=conflict_id,
                projection_kind=job.projection_kind,
                target_key=job.target_key,
                target_update_policy=(job.handler_contract.target_update_policy),
                conflict_kind=conflict_kind,
                candidate_source_event_reference_fingerprint=(
                    job.source_event_reference.reference_fingerprint
                ),
                candidate_source_sequence=job.source_event_reference.sequence,
                candidate_result_semantic_fingerprint=(
                    candidate_result_semantic_fingerprint
                ),
                existing_head_fingerprint=head.head_fingerprint,
                existing_applied_result_receipt_reference=(
                    head.applied_result_receipt_reference
                ),
                handler_contract_fingerprint=(
                    job.handler_contract.contract_fingerprint
                ),
            ),
        )

    @staticmethod
    def _insert_target_conflict(
        connection: Connection,
        conflict: DurableProjectionTargetAuthorityConflictFact,
    ) -> None:
        inserted = connection.execute(
            """
            INSERT INTO durable_projection_target_authority_conflicts (
                conflict_id, projection_kind, target_key,
                candidate_source_sequence, existing_target_head_fingerprint,
                conflict_payload, conflict_fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (conflict_id) DO NOTHING
            RETURNING conflict_id
            """,
            (
                conflict.conflict_id,
                conflict.projection_kind.value,
                conflict.target_key,
                conflict.candidate_source_sequence,
                conflict.existing_head_fingerprint,
                _json(conflict),
                conflict.conflict_fingerprint,
            ),
        ).fetchone()
        if inserted is None:
            row = connection.execute(
                """
                SELECT conflict_payload, conflict_fingerprint
                FROM durable_projection_target_authority_conflicts
                WHERE conflict_id = %s
                """,
                (conflict.conflict_id,),
            ).fetchone()
            if (
                row is None
                or DurableProjectionTargetAuthorityConflictFact.model_validate(
                    row["conflict_payload"]
                )
                != conflict
                or str(row["conflict_fingerprint"]) != conflict.conflict_fingerprint
            ):
                raise ValueError("projection target conflict identity conflict")

    @staticmethod
    def _settlement_outcome(
        *,
        confirmation: DurableProjectionCommitConfirmation,
        lease: LeasedDurableProjectionJob,
        state: DurableProjectionJobOperationalStateFact | None,
        receipt_reference: (DurableProjectionResultReceiptReferenceFact | None),
        failure: BoundedRuntimeFailureDiagnosticFact | None,
    ) -> DurableProjectionSettlementOutcome:
        return cast(
            DurableProjectionSettlementOutcome,
            build_projection_fact(
                DurableProjectionSettlementOutcome,
                schema_version="durable_projection_settlement_outcome.v1",
                confirmation=confirmation,
                job_id=lease.job.job_id,
                attempted_lease_fingerprint=lease.lease_fingerprint,
                resulting_status=state.status if state else None,
                resulting_state_revision=(state.state_revision if state else None),
                resulting_repair_generation=(
                    state.repair_generation if state else None
                ),
                result_receipt_reference=receipt_reference,
                failure=failure,
            ),
        )

    def _confirm_success_settlement(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        prepared: PreparedDurableProjectionResultFact,
        deadline_monotonic: float,
        error: BaseException,
    ) -> DurableProjectionSettlementOutcome:
        if monotonic() >= deadline_monotonic:
            return self._settlement_outcome(
                confirmation=DurableProjectionCommitConfirmation.UNRESOLVED,
                lease=lease,
                state=None,
                receipt_reference=None,
                failure=_diagnostic(error),
            )
        try:
            record = self.read_job(
                lease.job.job_id,
                deadline_monotonic=deadline_monotonic,
            )
            if record is None:
                confirmation = DurableProjectionCommitConfirmation.CONFLICT
            elif record.state.status in {
                DurableProjectionJobStatus.SUCCEEDED,
                DurableProjectionJobStatus.SUPERSEDED,
            }:
                reference = record.state.result_receipt_reference
                assert reference is not None
                receipt = self.read_receipt(
                    reference.receipt_id,
                    deadline_monotonic=deadline_monotonic,
                )
                owner = (
                    receipt.result_owner
                    if isinstance(
                        receipt,
                        DurableProjectionAppliedResultReceiptFact,
                    )
                    else receipt.candidate_result_owner
                )
                if owner.owner_fingerprint == prepared.result_owner.owner_fingerprint:
                    return self._settlement_outcome(
                        confirmation=DurableProjectionCommitConfirmation.FULL,
                        lease=lease,
                        state=record.state,
                        receipt_reference=reference,
                        failure=None,
                    )
                confirmation = DurableProjectionCommitConfirmation.CONFLICT
            elif (
                record.state.status is DurableProjectionJobStatus.LEASED
                and record.state.state_revision == lease.expected_state_revision
                and record.state.lease_generation == lease.lease_generation
                and record.state.lease_owner_id == lease.lease_owner_id
            ):
                confirmation = DurableProjectionCommitConfirmation.NONE
            else:
                confirmation = DurableProjectionCommitConfirmation.CONFLICT
            return self._settlement_outcome(
                confirmation=confirmation,
                lease=lease,
                state=None,
                receipt_reference=None,
                failure=_diagnostic(error),
            )
        except BaseException as confirmation_error:
            return self._settlement_outcome(
                confirmation=DurableProjectionCommitConfirmation.UNRESOLVED,
                lease=lease,
                state=None,
                receipt_reference=None,
                failure=_diagnostic(confirmation_error),
            )

    def read_receipt(
        self,
        receipt_id: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionResultReceiptFact:
        deadline = deadline_monotonic or monotonic() + 20.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            row = connection.execute(
                """
                SELECT receipt_payload, receipt_fingerprint
                FROM durable_projection_result_receipts
                WHERE receipt_id = %s
                """,
                (receipt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"projection result receipt is absent: {receipt_id}")
        receipt = _RECEIPT_ADAPTER.validate_python(row["receipt_payload"])
        if receipt.receipt_fingerprint != str(row["receipt_fingerprint"]):
            raise ValueError("projection result receipt row drifted")
        return receipt

    def read_head(
        self,
        projection_kind: DurableProjectionKind,
        target_key: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> DurableProjectionTargetHeadFact | None:
        deadline = deadline_monotonic or monotonic() + 20.0
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        ) as connection:
            _set_deadline(connection, deadline)
            row = connection.execute(
                """
                SELECT head_payload, head_fingerprint
                FROM durable_projection_target_heads
                WHERE projection_kind = %s AND target_key = %s
                """,
                (projection_kind.value, target_key),
            ).fetchone()
        if row is None:
            return None
        head = DurableProjectionTargetHeadFact.model_validate(row["head_payload"])
        if head.head_fingerprint != str(row["head_fingerprint"]):
            raise ValueError("projection target head row drifted")
        return head


def _diagnostic(error: BaseException) -> BoundedRuntimeFailureDiagnosticFact:
    return build_bounded_runtime_failure_diagnostic(
        error=error,
        redaction_profile_id="durable_projection_job_error.v1",
    )


__all__ = [
    "ClaimedCompactionMemoryExtractionSettlement",
    "PostgresDurableProjectionRepository",
    "seed_projection_checkpoint_kind",
]
