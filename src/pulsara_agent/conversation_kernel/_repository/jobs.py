"""Durable job and transcript-source operations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pulsara_agent.conversation_kernel.contracts import CommittedEventDraft, HostWriterGuard, JobAttemptClaimGuard, JobSafetyClass, canonical_digest
from pulsara_agent.conversation_kernel.job_catalog import BACKGROUND_COMPACTION, job_handler_contract
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType, SubjectSlot
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .contracts import (
    AcceptedJobAttempt,
    AcceptedJobSettlement,
    ConversationKernelConflict,
    JobAttemptTerminalized,
    StaleJobClaim,
    _deterministic_retry_due,
    _id,
    _utcnow,
)

from .matching import _required_nonnegative_int

class _JobOperations:
    def enqueue_job(
        self,
        guard: HostWriterGuard,
        *,
        job_id: str,
        handler_type: str,
        intent_schema_version: str,
        intent_payload: Mapping[str, object],
        automatic_intent_key: str | None,
        safety_class: JobSafetyClass,
        retry_policy_id: str,
        retry_policy_version: int,
        maximum_attempts: int,
        attempt_timeout_ms: int,
        provider_input_token_limit_per_attempt: int | None,
        provider_output_token_limit_per_attempt: int | None,
        next_eligible_at: datetime,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> None:
        contract = job_handler_contract(handler_type)
        if (
            safety_class is not contract.safety_class
            or retry_policy_id != contract.retry_policy_id
            or retry_policy_version != contract.retry_policy_version
            or maximum_attempts != contract.maximum_attempts
            or attempt_timeout_ms != contract.attempt_timeout_ms
            or provider_input_token_limit_per_attempt != contract.input_token_limit
            or provider_output_token_limit_per_attempt != contract.output_token_limit
        ):
            raise ValueError("job policy does not match the closed handler catalog")
        intent_digest = canonical_digest(
            f"pulsara:job-intent:{intent_schema_version}", dict(intent_payload)
        )
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            workspace_id = self._workspace_id(connection, guard.session_id)
            connection.execute(
                """
                INSERT INTO pulsara_v3.durable_jobs (
                    id, workspace_id, origin_session_id, handler_type,
                    intent_schema_version, intent_digest, intent_payload,
                    automatic_intent_key, safety_class, status,
                    retry_policy_id, retry_policy_version, maximum_attempts,
                    attempt_timeout_ms, provider_input_token_limit_per_attempt,
                    provider_output_token_limit_per_attempt, next_eligible_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                          'PENDING', %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job_id,
                    workspace_id,
                    guard.session_id,
                    handler_type,
                    intent_schema_version,
                    intent_digest,
                    Jsonb(dict(intent_payload)),
                    automatic_intent_key,
                    safety_class.value,
                    retry_policy_id,
                    retry_policy_version,
                    maximum_attempts,
                    attempt_timeout_ms,
                    provider_input_token_limit_per_attempt,
                    provider_output_token_limit_per_attempt,
                    next_eligible_at,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        CommittedEventType.JOB_QUEUED,
                        SubjectSlot.JOB,
                        job_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=guard.writer_owner_id,
                        payload={"handler_type": handler_type},
                    ),
                ),
            )

    def enqueue_background_compaction(
        self,
        guard: HostWriterGuard,
        *,
        job_id: str,
        source_through_sequence: int,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> None:
        contract = job_handler_contract(BACKGROUND_COMPACTION)
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            session = self._require_writer(connection, guard, lock=False)
            if source_through_sequence > int(session["latest_entry_sequence"]):
                raise ConversationKernelConflict(
                    "compaction source exceeds canonical head"
                )
            rows = _load_root_transcript_cut(
                connection,
                session_id=guard.session_id,
                through_sequence=source_through_sequence,
            )
            source_digest = canonical_digest(
                "pulsara:background-compaction-source:v1", rows
            )
            intent = {
                "session_id": guard.session_id,
                "source_through_sequence": source_through_sequence,
                "source_digest": source_digest,
            }
            connection.execute(
                """
                INSERT INTO pulsara_v3.durable_jobs (
                    id, workspace_id, origin_session_id, handler_type,
                    intent_schema_version, intent_digest, intent_payload,
                    automatic_intent_key, safety_class, status,
                    retry_policy_id, retry_policy_version, maximum_attempts,
                    attempt_timeout_ms, provider_input_token_limit_per_attempt,
                    provider_output_token_limit_per_attempt, next_eligible_at
                ) VALUES (
                    %s, %s, %s, 'BACKGROUND_COMPACTION',
                    'background_compaction.v1', %s, %s, %s,
                    'RETRY_SAFE', 'PENDING', 'bounded-exponential', 1,
                    %s, %s, %s, %s, clock_timestamp()
                )
                """,
                (
                    job_id,
                    session["workspace_id"],
                    guard.session_id,
                    canonical_digest(
                        "pulsara:job-intent:background_compaction.v1", intent
                    ),
                    Jsonb(intent),
                    f"background-compaction:{guard.session_id}:{source_through_sequence}",
                    contract.maximum_attempts,
                    contract.attempt_timeout_ms,
                    contract.input_token_limit,
                    contract.output_token_limit,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=str(session["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.JOB_QUEUED,
                        SubjectSlot.JOB,
                        job_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=guard.writer_owner_id,
                        payload={"handler_type": "BACKGROUND_COMPACTION"},
                    ),
                ),
            )

    def request_job_cancel(
        self,
        guard: HostWriterGuard,
        *,
        job_id: str,
        actor_id: str,
        reason: str,
        deadline_monotonic: float,
    ) -> str:
        """Install one session-writer-owned cancellation request.

        This never fabricates a terminal result. The exact claim owner observes
        the set-once request and owns the attempt/job terminal transition.
        """

        if not job_id or not actor_id or not reason:
            raise ValueError("job cancellation request is incomplete")
        if len(reason.encode("utf-8")) > 4096:
            raise ValueError("job cancellation reason exceeds its bound")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                SELECT status, cancel_requested_at, cancel_requested_by,
                       cancel_request_reason
                FROM pulsara_v3.durable_jobs
                WHERE origin_session_id = %s AND id = %s
                FOR UPDATE
                """,
                (guard.session_id, job_id),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            status = str(row["status"])
            if status in {"SUCCEEDED", "FAILED", "CANCELLED", "OUTCOME_UNKNOWN"}:
                return status
            if row["cancel_requested_at"] is not None:
                if (
                    str(row["cancel_requested_by"]) != actor_id
                    or str(row["cancel_request_reason"]) != reason
                ):
                    raise ConversationKernelConflict(
                        "job cancellation request conflicts with installed authority"
                    )
                return "CANCEL_REQUESTED"
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET cancel_requested_at = clock_timestamp(),
                    cancel_requested_by = %s,
                    cancel_request_reason = %s
                WHERE origin_session_id = %s AND id = %s
                  AND status IN ('PENDING', 'ACTIVE')
                  AND cancel_requested_at IS NULL
                """,
                (actor_id, reason, guard.session_id, job_id),
            )
            return "CANCEL_REQUESTED"

    def claim_due_job(
        self,
        *,
        handler_type: str,
        claim_owner_id: str,
        lease_seconds: float,
        expected_job_id: str | None = None,
        deadline_monotonic: float,
    ) -> AcceptedJobAttempt | None:
        if lease_seconds <= 0:
            raise ValueError("claim lease must be finite and positive")
        if expected_job_id is None:
            expected_job_id = self.prepare_job_claim_candidate(
                handler_type=handler_type,
                deadline_monotonic=deadline_monotonic,
            )
            if expected_job_id is None:
                return None
        with self._event_transaction(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            candidate = connection.execute(
                """
                SELECT origin_session_id
                FROM pulsara_v3.durable_jobs
                WHERE id = %s AND handler_type = %s
                """,
                (expected_job_id, handler_type),
            ).fetchone()
            if candidate is None:
                return None
            origin_session_id = candidate["origin_session_id"]
            if origin_session_id is not None:
                session = connection.execute(
                    """
                    SELECT id FROM pulsara_v3.sessions
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (origin_session_id,),
                ).fetchone()
                if session is None:
                    raise ConversationKernelConflict("job origin session disappeared")
            expired = connection.execute(
                """
                SELECT j.*, a.id AS attempt_id, a.attempt_ordinal,
                       a.claim_generation, a.claim_owner_id, a.remote_identity,
                       a.accepted_at AS attempt_accepted_at
                FROM pulsara_v3.durable_jobs AS j
                JOIN pulsara_v3.durable_job_attempts AS a ON a.job_id = j.id
                WHERE j.handler_type = %s AND j.status = 'ACTIVE'
                  AND j.id = %s
                  AND a.terminal_status IS NULL
                  AND a.lease_expires_at <= clock_timestamp()
                ORDER BY a.lease_expires_at, j.id
                FOR UPDATE OF j, a SKIP LOCKED
                LIMIT 1
                """,
                (handler_type, expected_job_id),
            ).fetchone()
            if expired is not None:
                safety = JobSafetyClass(str(expired["safety_class"]))
                retry_safe_with_budget = safety is JobSafetyClass.RETRY_SAFE and int(
                    expired["attempt_ordinal"]
                ) < int(expired["maximum_attempts"])
                if retry_safe_with_budget:
                    next_eligible_at = _deterministic_retry_due(
                        accepted_at=expired["attempt_accepted_at"],
                        job_id=str(expired["id"]),
                        attempt_ordinal=int(expired["attempt_ordinal"]),
                    )
                    connection.execute(
                        """
                        UPDATE pulsara_v3.durable_job_attempts
                        SET terminal_status = 'FAILED', error_code = 'ATTEMPT_TIMEOUT',
                            terminal_at = clock_timestamp()
                        WHERE id = %s AND terminal_status IS NULL
                        """,
                        (expired["attempt_id"],),
                    )
                    connection.execute(
                        """
                        UPDATE pulsara_v3.durable_jobs
                        SET status = 'PENDING', next_eligible_at = %s
                        WHERE id = %s AND status = 'ACTIVE'
                        """,
                        (next_eligible_at, expired["id"]),
                    )
                else:
                    terminal_at = _utcnow()
                    aggregate_status = (
                        "FAILED"
                        if safety is JobSafetyClass.RETRY_SAFE
                        else "OUTCOME_UNKNOWN"
                    )
                    terminal_reason = (
                        "RETRY_EXHAUSTED"
                        if safety is JobSafetyClass.RETRY_SAFE
                        else "LEASE_LOST_OUTCOME_UNKNOWN"
                    )
                    reaper_generation = int(expired["claim_generation"]) + 1
                    reaper_lease = _utcnow() + timedelta(seconds=lease_seconds)
                    connection.execute(
                        """
                        UPDATE pulsara_v3.durable_job_attempts
                        SET claim_generation = %s, claim_owner_id = %s,
                            lease_expires_at = %s
                        WHERE id = %s AND terminal_status IS NULL
                        """,
                        (
                            reaper_generation,
                            claim_owner_id,
                            reaper_lease,
                            expired["attempt_id"],
                        ),
                    )
                    if expired["origin_session_id"] is not None:
                        reaper_guard = JobAttemptClaimGuard(
                            job_id=str(expired["id"]),
                            attempt_id=str(expired["attempt_id"]),
                            claim_generation=reaper_generation,
                            claim_owner_id=claim_owner_id,
                            origin_session_id=expired["origin_session_id"],
                        )
                        self._append_events(
                            connection,
                            reaper_guard,
                            workspace_id=str(expired["workspace_id"]),
                            drafts=(
                                self._event(
                                    CommittedEventType.JOB_TERMINAL_ACCEPTED,
                                    SubjectSlot.JOB,
                                    str(expired["id"]),
                                    occurred_at=terminal_at,
                                    actor_kind="job_worker",
                                    actor_id=claim_owner_id,
                                    payload={
                                        "status": aggregate_status,
                                        "terminal_reason": terminal_reason,
                                    },
                                ),
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE pulsara_v3.durable_job_attempts
                        SET terminal_status = %s, error_code = %s,
                            terminal_at = %s
                        WHERE id = %s AND terminal_status IS NULL
                        """,
                        (
                            aggregate_status,
                            terminal_reason,
                            terminal_at,
                            expired["attempt_id"],
                        ),
                    )
                    connection.execute(
                        """UPDATE pulsara_v3.durable_jobs
                           SET status = %s, terminal_reason = %s, terminal_at = %s
                           WHERE id = %s AND status = 'ACTIVE'""",
                        (
                            aggregate_status,
                            terminal_reason,
                            terminal_at,
                            expired["id"],
                        ),
                    )
            job = connection.execute(
                """
                SELECT * FROM pulsara_v3.durable_jobs
                WHERE handler_type = %s AND status = 'PENDING'
                  AND id = %s
                  AND next_eligible_at <= clock_timestamp()
                  AND (SELECT count(*) FROM pulsara_v3.durable_job_attempts a
                       WHERE a.job_id = pulsara_v3.durable_jobs.id) < maximum_attempts
                ORDER BY next_eligible_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (handler_type, expected_job_id),
            ).fetchone()
            if job is None:
                return None
            previous = connection.execute(
                """
                SELECT id, attempt_ordinal, claim_generation
                FROM pulsara_v3.durable_job_attempts
                WHERE job_id = %s
                ORDER BY attempt_ordinal DESC
                LIMIT 1
                """,
                (job["id"],),
            ).fetchone()
            ordinal = 1 if previous is None else int(previous["attempt_ordinal"]) + 1
            generation = (
                1 if previous is None else int(previous["claim_generation"]) + 1
            )
            attempt_id = _id("job-attempt")
            lease_expires_at = _utcnow() + timedelta(seconds=lease_seconds)
            deadline_at = _utcnow() + timedelta(
                milliseconds=int(job["attempt_timeout_ms"])
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.durable_job_attempts (
                    id, job_id, origin_session_id, attempt_ordinal,
                    claim_generation, claim_owner_id, lease_expires_at,
                    deadline_at, retry_of_attempt_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    attempt_id,
                    job["id"],
                    job["origin_session_id"],
                    ordinal,
                    generation,
                    claim_owner_id,
                    lease_expires_at,
                    deadline_at,
                    None if previous is None else previous["id"],
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = 'ACTIVE'
                WHERE id = %s AND status = 'PENDING'
                """,
                (job["id"],),
            )
            guard = JobAttemptClaimGuard(
                job_id=str(job["id"]),
                attempt_id=attempt_id,
                claim_generation=generation,
                claim_owner_id=claim_owner_id,
                origin_session_id=job["origin_session_id"],
            )
            if job["origin_session_id"] is not None:
                self._append_events(
                    connection,
                    guard,
                    workspace_id=str(job["workspace_id"]),
                    drafts=(
                        self._event(
                            CommittedEventType.JOB_ATTEMPT_ACCEPTED,
                            SubjectSlot.JOB_ATTEMPT,
                            attempt_id,
                            occurred_at=_utcnow(),
                            actor_kind="job_worker",
                            actor_id=claim_owner_id,
                            payload={"attempt_ordinal": ordinal},
                        ),
                    ),
                )
            return AcceptedJobAttempt(
                guard=guard,
                attempt_ordinal=ordinal,
                deadline_at=deadline_at,
                handler_type=str(job["handler_type"]),
                safety_class=JobSafetyClass(str(job["safety_class"])),
                intent_payload=dict(job["intent_payload"]),
                provider_input_token_limit=job[
                    "provider_input_token_limit_per_attempt"
                ],
                provider_output_token_limit=job[
                    "provider_output_token_limit_per_attempt"
                ],
                reclaimed_after_expiry=False,
                cancel_requested=job["cancel_requested_at"] is not None,
            )

    def prepare_job_claim_candidate(
        self,
        *,
        handler_type: str,
        deadline_monotonic: float,
    ) -> str | None:
        """Select a stable claim candidate before the mutation transaction."""

        job_handler_contract(handler_type)
        with self._provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT j.id
                FROM pulsara_v3.durable_jobs AS j
                LEFT JOIN pulsara_v3.durable_job_attempts AS a
                  ON a.job_id = j.id AND a.terminal_status IS NULL
                WHERE j.handler_type = %s AND (
                    (j.status = 'ACTIVE' AND a.lease_expires_at <= clock_timestamp())
                    OR
                    (j.status = 'PENDING' AND j.next_eligible_at <= clock_timestamp()
                     AND (SELECT count(*) FROM pulsara_v3.durable_job_attempts x
                          WHERE x.job_id = j.id) < j.maximum_attempts)
                )
                ORDER BY
                    CASE WHEN j.status = 'ACTIVE' THEN 0 ELSE 1 END,
                    COALESCE(a.lease_expires_at, j.next_eligible_at), j.id
                LIMIT 1
                """,
                (handler_type,),
            ).fetchone()
            return None if row is None else str(row["id"])

    def confirm_active_job_claim(
        self,
        *,
        job_id: str,
        handler_type: str,
        claim_owner_id: str,
        deadline_monotonic: float,
    ) -> AcceptedJobAttempt | None:
        """Exact-confirm a first/retry claim whose commit ACK was lost."""

        job_handler_contract(handler_type)
        with self._provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT j.*, a.id AS attempt_id, a.attempt_ordinal,
                       a.claim_generation, a.deadline_at
                FROM pulsara_v3.durable_jobs AS j
                JOIN pulsara_v3.durable_job_attempts AS a ON a.job_id = j.id
                WHERE j.id = %s AND j.handler_type = %s
                  AND j.status = 'ACTIVE'
                  AND a.claim_owner_id = %s
                  AND a.terminal_status IS NULL
                  AND a.lease_expires_at > clock_timestamp()
                ORDER BY a.attempt_ordinal DESC
                LIMIT 1
                """,
                (job_id, handler_type, claim_owner_id),
            ).fetchone()
            if row is None:
                return None
            guard = JobAttemptClaimGuard(
                job_id=job_id,
                attempt_id=str(row["attempt_id"]),
                claim_generation=int(row["claim_generation"]),
                claim_owner_id=claim_owner_id,
                origin_session_id=row["origin_session_id"],
            )
            return AcceptedJobAttempt(
                guard=guard,
                attempt_ordinal=int(row["attempt_ordinal"]),
                deadline_at=row["deadline_at"],
                handler_type=handler_type,
                safety_class=JobSafetyClass(str(row["safety_class"])),
                intent_payload=dict(row["intent_payload"]),
                provider_input_token_limit=row[
                    "provider_input_token_limit_per_attempt"
                ],
                provider_output_token_limit=row[
                    "provider_output_token_limit_per_attempt"
                ],
                reclaimed_after_expiry=False,
                cancel_requested=row["cancel_requested_at"] is not None,
            )

    def mark_job_provider_call_started(
        self,
        guard: JobAttemptClaimGuard,
        *,
        input_tokens: int,
        requested_output_tokens: int,
        deadline_monotonic: float,
    ) -> None:
        terminalized = False
        with self._job_transaction(
            guard,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            limits = connection.execute(
                """
                SELECT j.provider_input_token_limit_per_attempt AS input_limit,
                       j.provider_output_token_limit_per_attempt AS output_limit,
                       j.workspace_id, j.origin_session_id,
                       a.provider_call_started_at
                FROM pulsara_v3.durable_job_attempts AS a
                JOIN pulsara_v3.durable_jobs AS j ON j.id = a.job_id
                WHERE a.id = %s
                """,
                (guard.attempt_id,),
            ).fetchone()
            if limits is None or limits["provider_call_started_at"] is not None:
                raise StaleJobClaim("provider call admission already consumed")
            if (
                limits["input_limit"] is None
                or limits["output_limit"] is None
                or input_tokens < 0
                or requested_output_tokens <= 0
                or input_tokens > int(limits["input_limit"])
                or requested_output_tokens > int(limits["output_limit"])
            ):
                terminal_at = _utcnow()
                if limits["origin_session_id"] is not None:
                    self._append_events(
                        connection,
                        guard,
                        workspace_id=str(limits["workspace_id"]),
                        drafts=(
                            self._event(
                                CommittedEventType.JOB_TERMINAL_ACCEPTED,
                                SubjectSlot.JOB,
                                guard.job_id,
                                occurred_at=terminal_at,
                                actor_kind="job_worker",
                                actor_id=guard.claim_owner_id,
                                payload={
                                    "status": "FAILED",
                                    "terminal_reason": "PROVIDER_REQUEST_LIMIT_EXCEEDED",
                                },
                            ),
                        ),
                    )
                connection.execute(
                    """UPDATE pulsara_v3.durable_job_attempts
                       SET terminal_status = 'FAILED',
                           error_code = 'PROVIDER_REQUEST_LIMIT_EXCEEDED',
                           terminal_at = %s
                       WHERE id = %s AND terminal_status IS NULL""",
                    (terminal_at, guard.attempt_id),
                )
                connection.execute(
                    """UPDATE pulsara_v3.durable_jobs
                       SET status = 'FAILED',
                           terminal_reason = 'PROVIDER_REQUEST_LIMIT_EXCEEDED',
                           terminal_at = %s
                       WHERE id = %s AND status = 'ACTIVE'""",
                    (terminal_at, guard.job_id),
                )
                terminalized = True
            else:
                connection.execute(
                    """
                    UPDATE pulsara_v3.durable_job_attempts
                    SET provider_call_started_at = clock_timestamp(),
                        provider_input_tokens = %s,
                        provider_requested_output_tokens = %s
                    WHERE id = %s AND provider_call_started_at IS NULL
                    """,
                    (input_tokens, requested_output_tokens, guard.attempt_id),
                )
        if terminalized:
            raise JobAttemptTerminalized("provider request exceeded its frozen bound")

    def settle_job_attempt(
        self,
        guard: JobAttemptClaimGuard,
        *,
        terminal_status: str,
        result_payload: Mapping[str, object] | None,
        error_code: str | None,
        result_blob_id: str | None = None,
        retryable: bool = False,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedJobSettlement:
        if terminal_status not in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "OUTCOME_UNKNOWN",
        }:
            raise ValueError("job terminal status is not closed")
        with self._job_transaction(
            guard,
            deadline_monotonic=deadline_monotonic,
            allow_cancel_requested=True,
        ) as connection:
            row = connection.execute(
                """
                SELECT j.*, a.attempt_ordinal,
                       a.accepted_at AS attempt_accepted_at
                FROM pulsara_v3.durable_jobs AS j
                JOIN pulsara_v3.durable_job_attempts AS a ON a.job_id = j.id
                WHERE j.id = %s AND a.id = %s
                FOR UPDATE OF j, a
                """,
                (guard.job_id, guard.attempt_id),
            ).fetchone()
            if row is None:
                raise StaleJobClaim("job attempt is absent")
            ordinal = int(row["attempt_ordinal"])
            safety = JobSafetyClass(str(row["safety_class"]))
            may_retry = (
                terminal_status == "FAILED"
                and retryable
                and safety
                in {JobSafetyClass.RETRY_SAFE, JobSafetyClass.REMOTE_QUERYABLE}
                and ordinal < int(row["maximum_attempts"])
            )
            terminal_at = _utcnow()
            if may_retry:
                next_eligible_at = _deterministic_retry_due(
                    accepted_at=row["attempt_accepted_at"],
                    job_id=guard.job_id,
                    attempt_ordinal=ordinal,
                )
                connection.execute(
                    """
                    UPDATE pulsara_v3.durable_job_attempts
                    SET terminal_status = 'FAILED', result_payload = %s,
                        error_code = %s, terminal_at = %s
                    WHERE id = %s AND terminal_status IS NULL
                    """,
                    (
                        None if result_payload is None else Jsonb(dict(result_payload)),
                        error_code,
                        terminal_at,
                        guard.attempt_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE pulsara_v3.durable_jobs
                    SET status = 'PENDING', next_eligible_at = %s
                    WHERE id = %s AND status = 'ACTIVE'
                    """,
                    (next_eligible_at, guard.job_id),
                )
                return AcceptedJobSettlement(
                    guard.job_id,
                    guard.attempt_id,
                    "FAILED",
                    "PENDING",
                    True,
                    next_eligible_at,
                )
            if (
                terminal_status == "FAILED"
                and retryable
                and ordinal >= int(row["maximum_attempts"])
            ):
                error_code = "RETRY_EXHAUSTED"
            if row["origin_session_id"] is not None:
                self._append_events(
                    connection,
                    guard,
                    workspace_id=str(row["workspace_id"]),
                    drafts=(
                        self._event(
                            CommittedEventType.JOB_TERMINAL_ACCEPTED,
                            SubjectSlot.JOB,
                            guard.job_id,
                            occurred_at=occurred_at,
                            actor_kind="job_worker",
                            actor_id=guard.claim_owner_id,
                            payload={
                                "status": terminal_status,
                                "terminal_reason": error_code,
                            },
                        ),
                    ),
                )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_job_attempts
                SET terminal_status = %s, result_payload = %s,
                    error_code = %s, terminal_at = %s
                WHERE id = %s AND terminal_status IS NULL
                """,
                (
                    terminal_status,
                    None if result_payload is None else Jsonb(dict(result_payload)),
                    error_code,
                    terminal_at,
                    guard.attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = %s, result_blob_id = %s, terminal_reason = %s,
                    terminal_at = %s
                WHERE id = %s AND status = 'ACTIVE'
                """,
                (
                    terminal_status,
                    result_blob_id,
                    error_code,
                    terminal_at,
                    guard.job_id,
                ),
            )
            return AcceptedJobSettlement(
                guard.job_id,
                guard.attempt_id,
                terminal_status,
                terminal_status,
                False,
                None,
            )

    def read_compaction_job_source(
        self,
        guard: JobAttemptClaimGuard,
        *,
        deadline_monotonic: float,
    ) -> Mapping[str, object]:
        """Read one immutable ROOT transcript cut owned by a compaction job."""

        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            job = connection.execute(
                """
                SELECT handler_type, origin_session_id, intent_payload
                FROM pulsara_v3.durable_jobs WHERE id = %s
                """,
                (guard.job_id,),
            ).fetchone()
            if (
                job is None
                or job["handler_type"] != "BACKGROUND_COMPACTION"
                or job["origin_session_id"] is None
            ):
                raise ConversationKernelConflict("compaction job source is invalid")
            intent = dict(job["intent_payload"])
            session_id = str(job["origin_session_id"])
            through = _required_nonnegative_int(
                intent.get("source_through_sequence"), "source_through_sequence"
            )
            if intent.get("session_id") != session_id:
                raise ConversationKernelConflict("compaction session identity drifted")
            rows = _load_root_transcript_cut(
                connection,
                session_id=session_id,
                through_sequence=through,
            )
            digest = canonical_digest("pulsara:background-compaction-source:v1", rows)
            if intent.get("source_digest") != digest:
                raise ConversationKernelConflict("compaction source digest drifted")
            return {
                "session_id": session_id,
                "source_through_sequence": through,
                "source_digest": digest,
                "entries": rows,
            }

    def accept_compaction_job_result(
        self,
        guard: JobAttemptClaimGuard,
        *,
        summary: str,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> str:
        """Accept a compaction result without creating follow-on memory work."""

        if not summary.strip() or len(summary.encode("utf-8")) > 256 * 1024:
            raise ValueError("compaction summary is outside its bound")
        summary_digest = canonical_digest(
            "pulsara:background-compaction-result:v1", {"summary": summary}
        )
        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            job = connection.execute(
                """
                SELECT * FROM pulsara_v3.durable_jobs
                WHERE id = %s AND handler_type = 'BACKGROUND_COMPACTION'
                FOR UPDATE
                """,
                (guard.job_id,),
            ).fetchone()
            if job is None:
                raise ConversationKernelConflict("compaction job is absent")
            drafts: list[CommittedEventDraft] = []
            if guard.origin_session_id is not None:
                drafts.append(
                    self._event(
                        CommittedEventType.JOB_TERMINAL_ACCEPTED,
                        SubjectSlot.JOB,
                        guard.job_id,
                        occurred_at=occurred_at,
                        actor_kind="job_worker",
                        actor_id=guard.claim_owner_id,
                        payload={"status": "SUCCEEDED", "terminal_reason": None},
                    )
                )
                self._append_events(
                    connection,
                    guard,
                    workspace_id=str(job["workspace_id"]),
                    drafts=tuple(drafts),
                )
            terminal_at = _utcnow()
            connection.execute(
                """
                UPDATE pulsara_v3.durable_job_attempts
                SET terminal_status = 'SUCCEEDED', result_payload = %s,
                    terminal_at = %s
                WHERE id = %s AND terminal_status IS NULL
                """,
                (
                    Jsonb(
                        {
                            "summary": summary,
                            "summary_digest": summary_digest,
                        }
                    ),
                    terminal_at,
                    guard.attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = 'SUCCEEDED', terminal_reason = 'COMPACTION_ACCEPTED',
                    terminal_at = %s
                WHERE id = %s AND status = 'ACTIVE'
                """,
                (terminal_at, guard.job_id),
            )
        return guard.job_id



def _load_root_transcript_cut(
    connection: Connection,
    *,
    session_id: str,
    through_sequence: int,
) -> tuple[dict[str, object], ...]:
    rows = connection.execute(
        """
        SELECT e.id, e.turn_id, e.entry_sequence, e.entry_kind,
               e.content_digest, e.content_size, e.content_codec,
               COALESCE(e.inline_content, eb.body) AS entry_body,
               b.id AS block_id, b.block_ordinal, b.block_kind,
               b.tool_call_id, b.tool_name, b.tool_arguments,
               b.content_digest AS block_digest, b.content_size AS block_size,
               b.content_codec AS block_codec,
               COALESCE(b.inline_content, bb.body) AS block_body
        FROM pulsara_v3.transcript_entries AS e
        LEFT JOIN pulsara_v3.blobs AS eb ON eb.id = e.blob_id
        LEFT JOIN pulsara_v3.assistant_message_blocks AS b
          ON b.session_id = e.session_id AND b.assistant_entry_id = e.id
        LEFT JOIN pulsara_v3.blobs AS bb ON bb.id = b.blob_id
        WHERE e.session_id = %s AND e.conversation_scope_kind = 'ROOT'
          AND e.entry_sequence <= %s
        ORDER BY e.entry_sequence, b.block_ordinal NULLS FIRST
        """,
        (session_id, through_sequence),
    ).fetchall()
    if len(rows) > 8192:
        raise ConversationKernelConflict("compaction source row bound exceeded")
    entries: list[dict[str, object]] = []
    by_id: dict[str, dict[str, object]] = {}
    total = 0
    for row in rows:
        entry_id = str(row["id"])
        entry = by_id.get(entry_id)
        if entry is None:
            body = bytes(row["entry_body"] or b"")
            total += len(body)
            entry = {
                "entry_id": entry_id,
                "turn_id": str(row["turn_id"]),
                "entry_sequence": int(row["entry_sequence"]),
                "entry_kind": str(row["entry_kind"]),
                "content_digest": str(row["content_digest"]),
                "text": body.decode(str(row["content_codec"]), errors="replace"),
                "blocks": [],
            }
            entries.append(entry)
            by_id[entry_id] = entry
        if row["block_id"] is None:
            continue
        block: dict[str, object] = {
            "block_id": str(row["block_id"]),
            "block_ordinal": int(row["block_ordinal"]),
            "block_kind": str(row["block_kind"]),
        }
        if row["block_kind"] == "TOOL_CALL":
            block.update(
                {
                    "tool_call_id": str(row["tool_call_id"]),
                    "tool_name": str(row["tool_name"]),
                    "arguments": dict(row["tool_arguments"]),
                }
            )
        else:
            body = bytes(row["block_body"] or b"")
            total += len(body)
            block.update(
                {
                    "content_digest": str(row["block_digest"]),
                    "text": body.decode(str(row["block_codec"]), errors="replace"),
                }
            )
        blocks = entry["blocks"]
        assert isinstance(blocks, list)
        blocks.append(block)
    if total > 16 << 20:
        raise ConversationKernelConflict("compaction source byte bound exceeded")
    frozen: list[dict[str, object]] = []
    for entry in entries:
        copied = dict(entry)
        blocks = copied["blocks"]
        assert isinstance(blocks, list)
        copied["blocks"] = tuple(dict(block) for block in blocks)
        frozen.append(copied)
    return tuple(frozen)
