"""Tool attempt, decision, remote identity and result operations."""

from __future__ import annotations

from datetime import datetime
from psycopg import Connection, IsolationLevel
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pulsara_agent.conversation_kernel.contracts import BlobContent, CanonicalContent, ConversationScopeKind, EntryKind, HostWriterGuard, canonical_digest
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType, SubjectSlot
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .contracts import (
    AcceptedCapabilityDecision,
    AcceptedEntry,
    AcceptedInteractionDecision,
    AcceptedToolAttempt,
    ConversationKernelConflict,
    PreparedMemoryProposalSideBranch,
    PreparedToolRemoteIdentityPublication,
    PreparedToolResultAcceptance,
    ToolRemoteIdentityConfirmationKind,
)

from .matching import (
    _event_row_matches_draft,
)

class _ToolOperations:
    def accept_tool_capability_decision(
        self,
        guard: HostWriterGuard,
        *,
        decision_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        decision: str,
        authorization_reference: str,
        redacted_subject: str,
        attempt_id: str | None,
        result_id: str | None,
        result_entry_id: str | None,
        denial_content: CanonicalContent | None,
        denial_result_state: str | None,
        occurred_at: datetime,
        actor_id: str,
        permission_snapshot_fingerprint: str,
        deadline_monotonic: float,
    ) -> AcceptedCapabilityDecision:
        """Accept one machine-policy decision and its immediate effect atomically."""

        if decision not in {"ALLOW", "DENY", "REQUIRE_CONFIRMATION"}:
            raise ValueError("machine capability decision is not closed")
        allow = decision == "ALLOW"
        deny = decision == "DENY"
        require_confirmation = decision == "REQUIRE_CONFIRMATION"
        if allow != (
            attempt_id is not None
            and result_id is None
            and result_entry_id is None
            and denial_content is None
            and denial_result_state is None
        ):
            raise ValueError("allowed capability effect union is invalid")
        if deny != (
            attempt_id is None
            and result_id is not None
            and result_entry_id is not None
            and denial_content is not None
            and denial_result_state == "PERMISSION_DENIED"
        ):
            raise ValueError("denied capability effect union is invalid")
        if require_confirmation != (
            attempt_id is None
            and result_id is None
            and result_entry_id is None
            and denial_content is None
            and denial_result_state is None
        ):
            raise ValueError("confirmation capability effect union is invalid")
        if not redacted_subject or len(redacted_subject.encode("utf-8")) > 4096:
            raise ValueError("capability redacted subject is outside its bound")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            subject = connection.execute(
                """
                SELECT e.turn_id, e.workspace_id, e.conversation_scope_kind,
                       e.scope_subagent_task_id,
                       t.permission_snapshot_fingerprint
                FROM pulsara_v3.assistant_message_blocks AS b
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = b.session_id
                 AND e.id = b.assistant_entry_id
                JOIN pulsara_v3.turns AS t
                  ON t.session_id = e.session_id AND t.id = e.turn_id
                WHERE b.session_id = %s AND b.assistant_entry_id = %s
                  AND b.tool_call_id = %s AND b.block_kind = 'TOOL_CALL'
                  AND t.status = 'RUNNING'
                FOR UPDATE OF t
                """,
                (guard.session_id, assistant_entry_id, tool_call_id),
            ).fetchone()
            if subject is None:
                raise ConversationKernelConflict(
                    "capability tool-call subject is not active"
                )
            if (
                str(subject["permission_snapshot_fingerprint"])
                != permission_snapshot_fingerprint
            ):
                raise ConversationKernelConflict(
                    "capability decision permission snapshot drifted"
                )
            connection.execute(
                """
                INSERT INTO pulsara_v3.interaction_decisions (
                    id, session_id, command_id, subject_kind,
                    subject_tool_call_entry_id, subject_tool_call_id,
                    decision, actor_kind, actor_id, redacted_subject,
                    permission_snapshot_fingerprint
                ) VALUES (%s, %s, NULL, 'TOOL_CALL', %s, %s, %s,
                          'machine', %s, %s, %s)
                """,
                (
                    decision_id,
                    guard.session_id,
                    assistant_entry_id,
                    tool_call_id,
                    decision,
                    actor_id,
                    redacted_subject,
                    permission_snapshot_fingerprint,
                ),
            )
            drafts = [
                self._event(
                    CommittedEventType.CAPABILITY_DECISION_ACCEPTED,
                    SubjectSlot.INTERACTION_DECISION,
                    decision_id,
                    occurred_at=occurred_at,
                    actor_kind="machine",
                    actor_id=actor_id,
                    payload={"decision": decision, "subject_kind": "TOOL_CALL"},
                )
            ]
            if allow:
                assert attempt_id is not None
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.tool_execution_attempts (
                        id, session_id, assistant_entry_id, tool_call_id,
                        authorization_kind, authorization_reference,
                        permission_snapshot_fingerprint, actor_kind, actor_id
                    ) VALUES (%s, %s, %s, %s, 'machine', %s,
                              %s, 'runtime', 'foreground-tool-executor')
                    """,
                    (
                        attempt_id,
                        guard.session_id,
                        assistant_entry_id,
                        tool_call_id,
                        authorization_reference,
                        permission_snapshot_fingerprint,
                    ),
                )
                drafts.append(
                    self._event(
                        CommittedEventType.TOOL_ATTEMPT_ACCEPTED,
                        SubjectSlot.TOOL_ATTEMPT,
                        attempt_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id="foreground-tool-executor",
                        payload={
                            "assistant_entry_id": assistant_entry_id,
                            "tool_call_id": tool_call_id,
                        },
                    )
                )
            elif deny:
                assert result_id is not None
                assert result_entry_id is not None
                assert denial_content is not None
                entry_sequence = self._allocate_entry_sequence(
                    connection, guard.session_id
                )
                self._insert_entry(
                    connection,
                    session_id=guard.session_id,
                    workspace_id=str(subject["workspace_id"]),
                    turn_id=str(subject["turn_id"]),
                    entry_id=result_entry_id,
                    entry_sequence=entry_sequence,
                    entry_kind=EntryKind.TOOL_RESULT,
                    scope_kind=ConversationScopeKind(
                        str(subject["conversation_scope_kind"])
                    ),
                    scope_task_id=subject["scope_subagent_task_id"],
                    content=denial_content,
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.tool_results (
                        id, session_id, workspace_id,
                        tool_call_entry_id, tool_call_id,
                        attempt_id, result_origin_kind,
                        permission_snapshot_fingerprint,
                        result_entry_id, result_state,
                        observed_at, observation_duration_microseconds,
                        observation_origin_kind,
                        tool_reported_duration_microseconds
                    ) VALUES (%s, %s, %s, %s, %s, NULL,
                              'POLICY_NO_ATTEMPT', %s, %s,
                              'PERMISSION_DENIED', %s, NULL, 'POLICY', NULL)
                    """,
                    (
                        result_id,
                        guard.session_id,
                        subject["workspace_id"],
                        assistant_entry_id,
                        tool_call_id,
                        permission_snapshot_fingerprint,
                        result_entry_id,
                        occurred_at,
                    ),
                )
                drafts.append(
                    self._event(
                        CommittedEventType.TOOL_RESULT_ACCEPTED,
                        SubjectSlot.ENTRY,
                        result_entry_id,
                        occurred_at=occurred_at,
                        actor_kind="tool",
                        actor_id="permission",
                        payload={
                            "tool_call_id": tool_call_id,
                            "result_state": "PERMISSION_DENIED",
                        },
                    )
                )
            self._append_events(
                connection,
                guard,
                workspace_id=str(subject["workspace_id"]),
                drafts=tuple(drafts),
            )
        return AcceptedCapabilityDecision(
            decision_id=decision_id,
            decision=decision,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            result_entry_id=result_entry_id,
            permission_snapshot_fingerprint=permission_snapshot_fingerprint,
        )

    def accept_tool_attempt(
        self,
        guard: HostWriterGuard,
        *,
        attempt_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        authorization_kind: str,
        authorization_reference: str,
        actor_kind: str,
        actor_id: str,
        remote_idempotency_key: str | None,
        retry_of_attempt_id: str | None,
        permission_snapshot_fingerprint: str,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedToolAttempt:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            subject = connection.execute(
                """
                SELECT t.workspace_id, t.permission_snapshot_fingerprint
                FROM pulsara_v3.assistant_message_blocks AS b
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = b.session_id
                 AND e.id = b.assistant_entry_id
                JOIN pulsara_v3.turns AS t
                  ON t.session_id = e.session_id AND t.id = e.turn_id
                WHERE b.session_id = %s AND b.assistant_entry_id = %s
                  AND b.tool_call_id = %s AND b.block_kind = 'TOOL_CALL'
                  AND t.status = 'RUNNING'
                FOR UPDATE OF t
                """,
                (guard.session_id, assistant_entry_id, tool_call_id),
            ).fetchone()
            if subject is None:
                raise ConversationKernelConflict("tool attempt subject is not active")
            if (
                str(subject["permission_snapshot_fingerprint"])
                != permission_snapshot_fingerprint
            ):
                raise ConversationKernelConflict(
                    "tool attempt permission snapshot drifted"
                )
            workspace_id = str(subject["workspace_id"])
            connection.execute(
                """
                INSERT INTO pulsara_v3.tool_execution_attempts (
                    id, session_id, assistant_entry_id, tool_call_id,
                    authorization_kind, authorization_reference,
                    permission_snapshot_fingerprint,
                    actor_kind, actor_id, remote_idempotency_key,
                    retry_of_attempt_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    attempt_id,
                    guard.session_id,
                    assistant_entry_id,
                    tool_call_id,
                    authorization_kind,
                    authorization_reference,
                    permission_snapshot_fingerprint,
                    actor_kind,
                    actor_id,
                    remote_idempotency_key,
                    retry_of_attempt_id,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        CommittedEventType.TOOL_ATTEMPT_ACCEPTED,
                        SubjectSlot.TOOL_ATTEMPT,
                        attempt_id,
                        occurred_at=occurred_at,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        payload={
                            "assistant_entry_id": assistant_entry_id,
                            "tool_call_id": tool_call_id,
                        },
                    ),
                ),
            )
        return AcceptedToolAttempt(
            attempt_id=attempt_id,
            session_id=guard.session_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            permission_snapshot_fingerprint=permission_snapshot_fingerprint,
        )

    def publish_tool_remote_identity(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedToolRemoteIdentityPublication,
        deadline_monotonic: float,
    ) -> bool:
        """Install one immutable remote identity before accepting its result.

        A compatible repeat is a read-confirmed success and emits no second
        occurrence.  A different identity is canonical corruption.
        """

        if candidate.session_id != guard.session_id:
            raise ValueError("tool remote identity belongs to another session")
        installed = False
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                SELECT remote_identity
                FROM pulsara_v3.tool_execution_attempts
                WHERE session_id = %s AND id = %s
                FOR UPDATE
                """,
                (guard.session_id, candidate.attempt_id),
            ).fetchone()
            if row is None:
                raise ConversationKernelConflict("tool attempt is absent")
            event = connection.execute(
                """SELECT * FROM pulsara_v3.agent_events
                   WHERE session_id = %s AND event_id = %s""",
                (guard.session_id, candidate.event.event_id),
            ).fetchone()
            current = row["remote_identity"]
            if current is not None or event is not None:
                if (
                    current is not None
                    and str(current) == candidate.remote_identity
                    and event is not None
                    and _event_row_matches_draft(event, candidate.event)
                ):
                    return False
                raise ConversationKernelConflict(
                    "tool remote identity conflicts with installed authority"
                )
            connection.execute(
                """
                UPDATE pulsara_v3.tool_execution_attempts
                SET remote_identity = %s,
                    remote_identity_published_at = clock_timestamp()
                WHERE session_id = %s AND id = %s
                """,
                (
                    candidate.remote_identity,
                    guard.session_id,
                    candidate.attempt_id,
                ),
            )
            workspace_id = self._workspace_id(connection, guard.session_id)
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(candidate.event,),
            )
            installed = True
        return installed

    def confirm_tool_remote_identity(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedToolRemoteIdentityPublication,
        deadline_monotonic: float,
    ) -> ToolRemoteIdentityConfirmationKind:
        if candidate.session_id != guard.session_id:
            raise ValueError("tool remote identity belongs to another session")
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            row = connection.execute(
                """SELECT remote_identity
                   FROM pulsara_v3.tool_execution_attempts
                   WHERE session_id = %s AND id = %s""",
                (guard.session_id, candidate.attempt_id),
            ).fetchone()
            if row is None:
                return ToolRemoteIdentityConfirmationKind.CONFLICT
            event = connection.execute(
                """SELECT * FROM pulsara_v3.agent_events
                   WHERE session_id = %s AND event_id = %s""",
                (guard.session_id, candidate.event.event_id),
            ).fetchone()
            current = row["remote_identity"]
            if current is None and event is None:
                return ToolRemoteIdentityConfirmationKind.NONE
            if (
                current is not None
                and str(current) == candidate.remote_identity
                and event is not None
                and _event_row_matches_draft(event, candidate.event)
            ):
                return ToolRemoteIdentityConfirmationKind.FULL
            return ToolRemoteIdentityConfirmationKind.CONFLICT

    def accept_tool_result(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedToolResultAcceptance,
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        if candidate.session_id != guard.session_id:
            raise ValueError("prepared tool result belongs to another session")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            turn = connection.execute(
                """
                SELECT workspace_id, conversation_scope_kind, scope_subagent_task_id,
                       permission_snapshot_fingerprint
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s
                FOR UPDATE
                """,
                (guard.session_id, candidate.turn_id),
            ).fetchone()
            if turn is None or str(turn["workspace_id"]) != candidate.workspace_id:
                raise ConversationKernelConflict("tool result turn is absent")
            if candidate.artifact_blob_descriptor is not None:
                self._require_exact_tool_artifact_blob(
                    connection,
                    workspace_id=candidate.workspace_id,
                    expected=candidate.artifact_blob_descriptor,
                )
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=candidate.workspace_id,
                turn_id=candidate.turn_id,
                entry_id=candidate.result_entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.TOOL_RESULT,
                scope_kind=ConversationScopeKind(str(turn["conversation_scope_kind"])),
                scope_task_id=turn["scope_subagent_task_id"],
                content=candidate.canonical_preview_content,
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.tool_results (
                    id, session_id, workspace_id,
                    tool_call_entry_id, tool_call_id, attempt_id,
                    result_origin_kind, result_entry_id, result_state,
                    permission_snapshot_fingerprint,
                    output_artifact_disposition, output_artifact_id,
                    output_artifact_blob_id, output_source_coverage,
                    output_display_kind, output_source_coverage_reason,
                    output_artifact_unavailability_reason,
                    observed_at, observation_duration_microseconds,
                    observation_origin_kind,
                    tool_reported_duration_microseconds
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    candidate.result_id,
                    guard.session_id,
                    candidate.workspace_id,
                    candidate.assistant_entry_id,
                    candidate.tool_call_id,
                    candidate.attempt_id,
                    (
                        "PHYSICAL_ATTEMPT"
                        if candidate.attempt_id is not None
                        else "POLICY_NO_ATTEMPT"
                    ),
                    candidate.result_entry_id,
                    candidate.result_state,
                    str(turn["permission_snapshot_fingerprint"]),
                    candidate.artifact_disposition.value,
                    candidate.artifact_id,
                    (
                        None
                        if candidate.artifact_blob_descriptor is None
                        else candidate.artifact_blob_descriptor.blob_id
                    ),
                    candidate.source_coverage.value,
                    candidate.display_kind.value,
                    (
                        None
                        if candidate.source_coverage_reason is None
                        else candidate.source_coverage_reason.value
                    ),
                    (
                        None
                        if candidate.artifact_unavailability_reason is None
                        else candidate.artifact_unavailability_reason.value
                    ),
                    candidate.observed_at,
                    candidate.observation_duration_microseconds,
                    candidate.observation_origin_kind.value,
                    candidate.trusted_tool_reported_duration_microseconds,
                ),
            )
            event_drafts = [candidate.tool_result_occurrence]
            side = candidate.side_branch
            if isinstance(side, PreparedMemoryProposalSideBranch):
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.memory_candidates (
                        id, workspace_id, origin_session_id, source_entry_id,
                        proposal_kind, semantic_digest, proposal_payload, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
                    """,
                    (
                        side.memory_candidate_id,
                        candidate.workspace_id,
                        guard.session_id,
                        candidate.assistant_entry_id,
                        side.proposal_kind,
                        side.candidate_semantic_digest,
                        Jsonb(dict(side.proposal_payload)),
                    ),
                )
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
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        side.governance_job_id,
                        candidate.workspace_id,
                        guard.session_id,
                        side.job_handler_type,
                        side.intent_schema_version,
                        side.intent_digest,
                        Jsonb(dict(side.intent_payload)),
                        side.automatic_intent_key,
                        side.safety_class,
                        side.initial_status,
                        side.retry_policy_id,
                        side.retry_policy_version,
                        side.maximum_attempts,
                        side.attempt_timeout_ms,
                        side.provider_input_token_limit_per_attempt,
                        side.provider_output_token_limit_per_attempt,
                        side.next_eligible_at,
                    ),
                )
                event_drafts.append(side.job_queued_occurrence)
            event = self._append_events(
                connection,
                guard,
                workspace_id=candidate.workspace_id,
                drafts=tuple(event_drafts),
            )[0]
            return AcceptedEntry(
                entry_id=candidate.result_entry_id,
                turn_id=candidate.turn_id,
                entry_sequence=entry_sequence,
                event_sequence=event.event_sequence,
            )

    def confirm_tool_result_winner(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedToolResultAcceptance,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Stateless exact confirmation after an ambiguous canonical ACK."""

        if candidate.session_id != guard.session_id:
            raise ValueError("prepared tool result belongs to another session")
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            entry = connection.execute(
                """
                SELECT * FROM pulsara_v3.transcript_entries
                WHERE session_id = %s AND id = %s
                """,
                (guard.session_id, candidate.result_entry_id),
            ).fetchone()
            result = connection.execute(
                """
                SELECT * FROM pulsara_v3.tool_results
                WHERE session_id = %s AND id = %s
                """,
                (guard.session_id, candidate.result_id),
            ).fetchone()
            turn = connection.execute(
                """
                SELECT permission_snapshot_fingerprint
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s
                """,
                (guard.session_id, candidate.turn_id),
            ).fetchone()
            if entry is None and result is None:
                return None
            if entry is None or result is None or turn is None:
                raise ConversationKernelConflict(
                    "tool result winner is only partially installed"
                )
            blob = candidate.artifact_blob_descriptor
            if (
                str(entry["workspace_id"]) != candidate.workspace_id
                or str(entry["turn_id"]) != candidate.turn_id
                or str(entry["entry_kind"]) != EntryKind.TOOL_RESULT.value
                or self._content_from_row(entry) != candidate.canonical_preview_content
                or str(result["workspace_id"]) != candidate.workspace_id
                or str(result["tool_call_entry_id"]) != candidate.assistant_entry_id
                or str(result["tool_call_id"]) != candidate.tool_call_id
                or result["attempt_id"] != candidate.attempt_id
                or str(result["result_entry_id"]) != candidate.result_entry_id
                or str(result["result_state"]) != candidate.result_state
                or str(result["permission_snapshot_fingerprint"])
                != str(turn["permission_snapshot_fingerprint"])
                or str(result["output_artifact_disposition"])
                != candidate.artifact_disposition.value
                or result["output_artifact_id"] != candidate.artifact_id
                or result["output_artifact_blob_id"]
                != (None if blob is None else blob.blob_id)
                or str(result["output_source_coverage"])
                != candidate.source_coverage.value
                or str(result["output_display_kind"]) != candidate.display_kind.value
                or result["output_source_coverage_reason"]
                != (
                    None
                    if candidate.source_coverage_reason is None
                    else candidate.source_coverage_reason.value
                )
                or result["output_artifact_unavailability_reason"]
                != (
                    None
                    if candidate.artifact_unavailability_reason is None
                    else candidate.artifact_unavailability_reason.value
                )
                or result["observed_at"] != candidate.observed_at
                or result["observation_duration_microseconds"]
                != candidate.observation_duration_microseconds
                or str(result["observation_origin_kind"])
                != candidate.observation_origin_kind.value
                or result["tool_reported_duration_microseconds"]
                != candidate.trusted_tool_reported_duration_microseconds
            ):
                raise ConversationKernelConflict(
                    "tool result identity names a different winner"
                )
            result_event = self._exact_event_for_confirmation(
                connection,
                candidate.tool_result_occurrence,
                session_id=candidate.session_id,
                workspace_id=candidate.workspace_id,
            )
            if blob is not None:
                self._require_exact_tool_artifact_blob(
                    connection,
                    workspace_id=candidate.workspace_id,
                    expected=blob,
                )
            side = candidate.side_branch
            if isinstance(side, PreparedMemoryProposalSideBranch):
                self._confirm_memory_proposal_side_branch(connection, candidate, side)
            return AcceptedEntry(
                entry_id=candidate.result_entry_id,
                turn_id=candidate.turn_id,
                entry_sequence=int(entry["entry_sequence"]),
                event_sequence=int(result_event["event_sequence"]),
            )

    @staticmethod
    def _require_exact_tool_artifact_blob(
        connection: Connection,
        *,
        workspace_id: str,
        expected: BlobContent,
    ) -> None:
        row = connection.execute(
            """
            SELECT id, logical_digest, logical_size, media_type, codec
            FROM pulsara_v3.blobs
            WHERE id = %s AND workspace_id = %s
            """,
            (expected.blob_id, workspace_id),
        ).fetchone()
        if (
            row is None
            or BlobContent(
                blob_id=str(row["id"]),
                digest=str(row["logical_digest"]),
                size=int(row["logical_size"]),
                media_type=str(row["media_type"]),
                codec=str(row["codec"]),
            )
            != expected
        ):
            raise ConversationKernelConflict(
                "prepared tool artifact descriptor names a different blob"
            )

    def _confirm_memory_proposal_side_branch(
        self,
        connection: Connection,
        candidate: PreparedToolResultAcceptance,
        side: PreparedMemoryProposalSideBranch,
    ) -> None:
        memory = connection.execute(
            """
            SELECT * FROM pulsara_v3.memory_candidates
            WHERE workspace_id = %s AND id = %s
            """,
            (candidate.workspace_id, side.memory_candidate_id),
        ).fetchone()
        job = connection.execute(
            """
            SELECT * FROM pulsara_v3.durable_jobs
            WHERE workspace_id = %s AND id = %s
            """,
            (candidate.workspace_id, side.governance_job_id),
        ).fetchone()
        if memory is None or job is None:
            raise ConversationKernelConflict(
                "prepared memory proposal side branch is only partially installed"
            )
        if (
            str(memory["origin_session_id"]) != candidate.session_id
            or str(memory["source_entry_id"]) != candidate.assistant_entry_id
            or str(memory["proposal_kind"]) != side.proposal_kind
            or str(memory["semantic_digest"]) != side.candidate_semantic_digest
            or dict(memory["proposal_payload"]) != dict(side.proposal_payload)
            or str(memory["status"]) != "PENDING"
            or str(job["origin_session_id"]) != candidate.session_id
            or str(job["handler_type"]) != side.job_handler_type
            or str(job["intent_schema_version"]) != side.intent_schema_version
            or str(job["intent_digest"]) != side.intent_digest
            or dict(job["intent_payload"]) != dict(side.intent_payload)
            or str(job["automatic_intent_key"]) != side.automatic_intent_key
            or str(job["safety_class"]) != side.safety_class
            or str(job["status"]) != side.initial_status
            or str(job["retry_policy_id"]) != side.retry_policy_id
            or int(job["retry_policy_version"]) != side.retry_policy_version
            or int(job["maximum_attempts"]) != side.maximum_attempts
            or int(job["attempt_timeout_ms"]) != side.attempt_timeout_ms
            or int(job["provider_input_token_limit_per_attempt"])
            != side.provider_input_token_limit_per_attempt
            or int(job["provider_output_token_limit_per_attempt"])
            != side.provider_output_token_limit_per_attempt
            or job["next_eligible_at"] != side.next_eligible_at
        ):
            raise ConversationKernelConflict(
                "prepared memory proposal side branch names a different winner"
            )
        self._exact_event_for_confirmation(
            connection,
            side.job_queued_occurrence,
            session_id=candidate.session_id,
            workspace_id=candidate.workspace_id,
        )

    def accept_tool_interaction_decision(
        self,
        guard: HostWriterGuard,
        *,
        command_id: str,
        decision_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        decision: str,
        attempt_id: str | None,
        result_id: str | None,
        result_entry_id: str | None,
        denial_content: CanonicalContent | None,
        redacted_subject: str,
        actor_id: str,
        occurred_at: datetime,
        permission_snapshot_fingerprint: str,
        deadline_monotonic: float,
    ) -> AcceptedInteractionDecision:
        """Accept one human tool decision and its physical-effect boundary.

        ALLOW installs the exact execution attempt in this transaction.  DENY
        installs the no-attempt tool result and its transcript entry.  The
        process-local pending request is deliberately not represented here.
        """

        allow = decision == "ALLOW"
        deny = decision == "DENY"
        if not (allow or deny):
            raise ValueError("tool interaction decision must be ALLOW or DENY")
        if allow != (
            attempt_id is not None
            and result_id is None
            and result_entry_id is None
            and denial_content is None
        ):
            raise ValueError("allowed interaction effect union is invalid")
        if deny != (
            attempt_id is None
            and result_id is not None
            and result_entry_id is not None
            and denial_content is not None
        ):
            raise ValueError("denied interaction effect union is invalid")
        if not redacted_subject or len(redacted_subject.encode("utf-8")) > 4096:
            raise ValueError("interaction redacted subject is outside its bound")
        semantic_digest = canonical_digest(
            "pulsara:resolve-tool-interaction:v1",
            {
                "assistant_entry_id": assistant_entry_id,
                "tool_call_id": tool_call_id,
                "decision": decision,
            },
        )
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            existing = connection.execute(
                """
                SELECT c.semantic_digest, c.target_interaction_decision_id,
                       d.decision, d.subject_tool_call_entry_id,
                       d.subject_tool_call_id,
                       d.permission_snapshot_fingerprint,
                       a.id AS attempt_id, r.result_entry_id
                FROM pulsara_v3.session_commands AS c
                JOIN pulsara_v3.interaction_decisions AS d
                  ON d.session_id = c.session_id
                 AND d.id = c.target_interaction_decision_id
                LEFT JOIN pulsara_v3.tool_execution_attempts AS a
                  ON a.session_id = d.session_id
                 AND a.assistant_entry_id = d.subject_tool_call_entry_id
                 AND a.tool_call_id = d.subject_tool_call_id
                LEFT JOIN pulsara_v3.tool_results AS r
                  ON r.session_id = d.session_id
                 AND r.tool_call_entry_id = d.subject_tool_call_entry_id
                 AND r.tool_call_id = d.subject_tool_call_id
                WHERE c.session_id = %s AND c.command_id = %s
                  AND c.command_kind = 'RESOLVE_INTERACTION'
                """,
                (guard.session_id, command_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["semantic_digest"] != semantic_digest
                    or existing["target_interaction_decision_id"] != decision_id
                    or existing["decision"] != decision
                    or existing["subject_tool_call_entry_id"] != assistant_entry_id
                    or existing["subject_tool_call_id"] != tool_call_id
                    or existing["attempt_id"] != attempt_id
                    or existing["result_entry_id"] != result_entry_id
                ):
                    raise ConversationKernelConflict(
                        "interaction command identity conflict"
                    )
                return AcceptedInteractionDecision(
                    decision_id,
                    command_id,
                    decision,
                    assistant_entry_id,
                    tool_call_id,
                    attempt_id,
                    result_entry_id,
                    str(existing["permission_snapshot_fingerprint"]),
                )
            subject = connection.execute(
                """
                SELECT e.turn_id, e.workspace_id, e.conversation_scope_kind,
                       e.scope_subagent_task_id,
                       t.permission_snapshot_fingerprint
                FROM pulsara_v3.assistant_message_blocks AS b
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = b.session_id
                 AND e.id = b.assistant_entry_id
                JOIN pulsara_v3.turns AS t
                  ON t.session_id = e.session_id AND t.id = e.turn_id
                WHERE b.session_id = %s AND b.assistant_entry_id = %s
                  AND b.tool_call_id = %s AND b.block_kind = 'TOOL_CALL'
                  AND t.status = 'RUNNING'
                FOR UPDATE OF t
                """,
                (guard.session_id, assistant_entry_id, tool_call_id),
            ).fetchone()
            if subject is None:
                raise ConversationKernelConflict(
                    "interaction tool-call subject is not active"
                )
            if (
                str(subject["permission_snapshot_fingerprint"])
                != permission_snapshot_fingerprint
            ):
                raise ConversationKernelConflict(
                    "interaction decision permission snapshot drifted"
                )
            prior_effect = connection.execute(
                """
                SELECT 1 FROM pulsara_v3.tool_execution_attempts
                WHERE session_id = %s AND assistant_entry_id = %s
                  AND tool_call_id = %s
                UNION ALL
                SELECT 1 FROM pulsara_v3.tool_results
                WHERE session_id = %s AND tool_call_entry_id = %s
                  AND tool_call_id = %s
                LIMIT 1
                """,
                (
                    guard.session_id,
                    assistant_entry_id,
                    tool_call_id,
                    guard.session_id,
                    assistant_entry_id,
                    tool_call_id,
                ),
            ).fetchone()
            if prior_effect is not None:
                raise ConversationKernelConflict(
                    "interaction subject already has a physical outcome"
                )
            connection.execute(
                """
                INSERT INTO pulsara_v3.interaction_decisions (
                    id, session_id, command_id, subject_kind,
                    subject_tool_call_entry_id, subject_tool_call_id,
                    decision, actor_kind, actor_id, redacted_subject,
                    permission_snapshot_fingerprint
                ) VALUES (%s, %s, %s, 'TOOL_CALL', %s, %s, %s,
                          'human', %s, %s, %s)
                """,
                (
                    decision_id,
                    guard.session_id,
                    command_id,
                    assistant_entry_id,
                    tool_call_id,
                    decision,
                    actor_id,
                    redacted_subject,
                    permission_snapshot_fingerprint,
                ),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_interaction_decision_id
                ) VALUES (%s, %s, 'RESOLVE_INTERACTION',
                          'resolve_tool_interaction.v1', %s,
                          'INTERACTION_DECISION', %s)
                """,
                (guard.session_id, command_id, semantic_digest, decision_id),
            )
            drafts = [
                self._event(
                    CommittedEventType.INTERACTION_DECISION_ACCEPTED,
                    SubjectSlot.INTERACTION_DECISION,
                    decision_id,
                    occurred_at=occurred_at,
                    actor_kind="human",
                    actor_id=actor_id,
                    payload={"decision": decision, "subject_kind": "TOOL_CALL"},
                )
            ]
            if allow:
                assert attempt_id is not None
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.tool_execution_attempts (
                        id, session_id, assistant_entry_id, tool_call_id,
                        authorization_kind, authorization_reference,
                        permission_snapshot_fingerprint, actor_kind, actor_id
                    ) VALUES (%s, %s, %s, %s, 'human', %s,
                              %s, 'runtime', 'foreground-tool-executor')
                    """,
                    (
                        attempt_id,
                        guard.session_id,
                        assistant_entry_id,
                        tool_call_id,
                        f"interaction-decision:{decision_id}",
                        permission_snapshot_fingerprint,
                    ),
                )
                drafts.append(
                    self._event(
                        CommittedEventType.TOOL_ATTEMPT_ACCEPTED,
                        SubjectSlot.TOOL_ATTEMPT,
                        attempt_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id="foreground-tool-executor",
                        payload={
                            "assistant_entry_id": assistant_entry_id,
                            "tool_call_id": tool_call_id,
                        },
                    )
                )
            else:
                assert result_id is not None
                assert result_entry_id is not None
                assert denial_content is not None
                entry_sequence = self._allocate_entry_sequence(
                    connection, guard.session_id
                )
                self._insert_entry(
                    connection,
                    session_id=guard.session_id,
                    workspace_id=str(subject["workspace_id"]),
                    turn_id=str(subject["turn_id"]),
                    entry_id=result_entry_id,
                    entry_sequence=entry_sequence,
                    entry_kind=EntryKind.TOOL_RESULT,
                    scope_kind=ConversationScopeKind(
                        str(subject["conversation_scope_kind"])
                    ),
                    scope_task_id=subject["scope_subagent_task_id"],
                    content=denial_content,
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.tool_results (
                        id, session_id, workspace_id,
                        tool_call_entry_id, tool_call_id,
                        attempt_id, result_origin_kind,
                        permission_snapshot_fingerprint,
                        result_entry_id, result_state,
                        observed_at, observation_duration_microseconds,
                        observation_origin_kind,
                        tool_reported_duration_microseconds
                    ) VALUES (%s, %s, %s, %s, %s, NULL,
                              'POLICY_NO_ATTEMPT', %s, %s,
                              'PERMISSION_DENIED', %s, NULL, 'POLICY', NULL)
                    """,
                    (
                        result_id,
                        guard.session_id,
                        subject["workspace_id"],
                        assistant_entry_id,
                        tool_call_id,
                        permission_snapshot_fingerprint,
                        result_entry_id,
                        occurred_at,
                    ),
                )
                drafts.append(
                    self._event(
                        CommittedEventType.TOOL_RESULT_ACCEPTED,
                        SubjectSlot.ENTRY,
                        result_entry_id,
                        occurred_at=occurred_at,
                        actor_kind="tool",
                        actor_id="permission",
                        payload={
                            "tool_call_id": tool_call_id,
                            "result_state": "PERMISSION_DENIED",
                        },
                    )
                )
            self._append_events(
                connection,
                guard,
                workspace_id=str(subject["workspace_id"]),
                drafts=tuple(drafts),
            )
        return AcceptedInteractionDecision(
            decision_id,
            command_id,
            decision,
            assistant_entry_id,
            tool_call_id,
            attempt_id,
            result_entry_id,
            permission_snapshot_fingerprint,
        )
