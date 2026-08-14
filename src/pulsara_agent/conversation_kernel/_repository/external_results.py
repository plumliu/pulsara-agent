"""External result safe-point acceptance operations."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from psycopg import Connection
from pulsara_agent.conversation_kernel.contracts import BlobContent, CanonicalContent, ConversationScopeKind, EntryKind, HostWriterGuard, InlineContent, canonical_digest
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import RunPermissionAdmissionSource
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType, SubjectSlot

from .contracts import (
    AcceptedEntry,
    ConversationKernelConflict,
    _stable_identity,
)

class _ExternalResultOperations:
    def accept_subagent_result_into_root(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        new_context_binding_revision_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
        child_result_id: str,
        command_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Accept one exact completed child result into an explicit ROOT target.

        The caller must own the process-local provider safe-point lock.  The
        source child is independently durable; this transaction owns only the
        unique ROOT-visible acceptance and its idempotent command.  A missing
        ``new_context_binding_revision_id`` means an existing RUNNING ROOT;
        otherwise this command creates a fresh ROOT turn.  Neither branch
        resumes or redirects child execution.
        """

        if not child_result_id or not command_id:
            raise ValueError("external result acceptance identity is empty")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                SELECT t.workspace_id, c.id AS child_id, c.entry_id,
                       e.inline_content, e.blob_id,
                       e.content_digest, e.content_size, e.content_media_type,
                       e.content_codec, accepted.id AS accepted_entry_id
                FROM pulsara_v3.subagent_tasks AS t
                JOIN pulsara_v3.subagent_task_children AS c
                  ON c.session_id = t.session_id AND c.task_id = t.id
                 AND c.child_kind = 'RESULT'
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = c.session_id AND e.id = c.entry_id
                LEFT JOIN pulsara_v3.transcript_entries AS accepted
                  ON accepted.session_id = c.session_id
                 AND accepted.source_subagent_result_id = c.id
                WHERE t.session_id = %s AND t.status = 'COMPLETED' AND c.id = %s
                FOR UPDATE OF t
                """,
                (guard.session_id, child_result_id),
            ).fetchone()
            entry_id = "entry:" + sha256(command_id.encode()).hexdigest()
            if row is None:
                return None
            child_id = str(row["child_id"])
            digest = canonical_digest(
                "pulsara:accept-subagent-result:v1",
                {
                    "turn_id": turn_id,
                    "new_context_binding_revision_id": (
                        new_context_binding_revision_id
                    ),
                    "requested_permission_mode": (
                        None
                        if requested_permission_mode is None
                        else requested_permission_mode.value
                    ),
                    "source_subagent_result_id": child_id,
                    "content_digest": str(row["content_digest"]),
                },
            )
            existing = connection.execute(
                """
                SELECT c.command_kind, c.semantic_digest, c.target_entry_id,
                       e.turn_id, e.entry_sequence,
                       e.source_subagent_result_id, a.event_sequence
                FROM pulsara_v3.session_commands AS c
                LEFT JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = c.session_id AND e.id = c.target_entry_id
                LEFT JOIN pulsara_v3.agent_events AS a
                  ON a.session_id = e.session_id
                 AND a.subject_entry_id = e.id
                 AND a.event_type = 'UserMessageAccepted'
                WHERE c.session_id = %s AND c.command_id = %s
                """,
                (guard.session_id, command_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["command_kind"] != "ACCEPT_SUBAGENT_RESULT"
                    or existing["semantic_digest"] != digest
                    or existing["target_entry_id"] != entry_id
                    or existing["turn_id"] != turn_id
                    or existing["source_subagent_result_id"] != child_id
                    or existing["event_sequence"] is None
                ):
                    raise ConversationKernelConflict(
                        "subagent result acceptance command conflict"
                    )
                return AcceptedEntry(
                    entry_id,
                    turn_id,
                    int(existing["entry_sequence"]),
                    int(existing["event_sequence"]),
                )
            if row["accepted_entry_id"] is not None:
                return None
            sequence = self._prepare_external_result_target(
                connection,
                guard,
                turn_id=turn_id,
                entry_id=entry_id,
                new_context_binding_revision_id=new_context_binding_revision_id,
                source_workspace_id=str(row["workspace_id"]),
                requested_permission_mode=requested_permission_mode,
            )
            if sequence is None:
                return None
            content = self._content_from_row(row)
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=str(row["workspace_id"]),
                turn_id=turn_id,
                entry_id=entry_id,
                entry_sequence=sequence,
                entry_kind=EntryKind.USER_MESSAGE,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=content,
                source_subagent_result_id=child_id,
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_entry_id
                ) VALUES (%s, %s, 'ACCEPT_SUBAGENT_RESULT',
                          'accept_subagent_result.v1', %s, 'ENTRY', %s)
                """,
                (guard.session_id, command_id, digest, entry_id),
            )
            event = self._append_events(
                connection,
                guard,
                workspace_id=str(row["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.USER_MESSAGE_ACCEPTED,
                        SubjectSlot.ENTRY,
                        entry_id,
                        occurred_at=occurred_at,
                        actor_kind="subagent",
                        actor_id=actor_id,
                        payload={
                            "source_subagent_result_id": child_id,
                            "source_entry_id": str(row["entry_id"]),
                        },
                    ),
                ),
            )[0]
            return AcceptedEntry(entry_id, turn_id, sequence, event.event_sequence)

    def accept_job_result_into_root(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        new_context_binding_revision_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
        job_id: str,
        command_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Accept one immutable SUCCEEDED job result into an explicit ROOT target."""

        if not job_id or not command_id:
            raise ValueError("job result acceptance identity is empty")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            entry_id = "entry:" + sha256(command_id.encode()).hexdigest()
            job = connection.execute(
                """
                SELECT j.workspace_id, j.status, j.result_blob_id,
                       a.result_payload, accepted.id AS accepted_entry_id
                FROM pulsara_v3.durable_jobs AS j
                JOIN pulsara_v3.durable_job_attempts AS a ON a.job_id = j.id
                LEFT JOIN pulsara_v3.transcript_entries AS accepted
                  ON accepted.session_id = j.origin_session_id
                 AND accepted.source_job_id = j.id
                WHERE j.origin_session_id = %s AND j.id = %s
                  AND j.status = 'SUCCEEDED'
                  AND a.terminal_status = 'SUCCEEDED'
                ORDER BY a.attempt_ordinal DESC
                LIMIT 1
                FOR UPDATE OF j, a
                """,
                (guard.session_id, job_id),
            ).fetchone()
            if job is None:
                return None
            if job["result_blob_id"] is not None:
                blob = connection.execute(
                    """
                    SELECT id, logical_digest, logical_size, media_type, codec
                    FROM pulsara_v3.blobs
                    WHERE id = %s AND workspace_id = %s
                    """,
                    (job["result_blob_id"], job["workspace_id"]),
                ).fetchone()
                if blob is None:
                    raise ConversationKernelConflict("job result blob is absent")
                content: CanonicalContent = BlobContent(
                    blob_id=str(blob["id"]),
                    digest=str(blob["logical_digest"]),
                    size=int(blob["logical_size"]),
                    media_type=str(blob["media_type"]),
                    codec=str(blob["codec"]),
                )
            else:
                encoded = json.dumps(
                    dict(job["result_payload"] or {}),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                content = InlineContent.from_bytes(
                    encoded, media_type="application/json", codec="utf-8"
                )
            digest = canonical_digest(
                "pulsara:accept-job-result:v1",
                {
                    "turn_id": turn_id,
                    "new_context_binding_revision_id": (
                        new_context_binding_revision_id
                    ),
                    "requested_permission_mode": (
                        None
                        if requested_permission_mode is None
                        else requested_permission_mode.value
                    ),
                    "source_job_id": job_id,
                    "content_digest": content.digest,
                },
            )
            compatible = connection.execute(
                """
                SELECT c.command_kind, c.semantic_digest, c.target_entry_id,
                       e.turn_id, e.entry_sequence, e.source_job_id,
                       a.event_sequence
                FROM pulsara_v3.session_commands AS c
                LEFT JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = c.session_id AND e.id = c.target_entry_id
                LEFT JOIN pulsara_v3.agent_events AS a
                  ON a.session_id = e.session_id
                 AND a.subject_entry_id = e.id
                 AND a.event_type = 'UserMessageAccepted'
                WHERE c.session_id = %s AND c.command_id = %s
                """,
                (guard.session_id, command_id),
            ).fetchone()
            if compatible is not None:
                if (
                    compatible["command_kind"] != "ACCEPT_JOB_RESULT"
                    or compatible["semantic_digest"] != digest
                    or compatible["target_entry_id"] != entry_id
                    or compatible["turn_id"] != turn_id
                    or compatible["source_job_id"] != job_id
                    or compatible["event_sequence"] is None
                ):
                    raise ConversationKernelConflict(
                        "job result acceptance command conflict"
                    )
                return AcceptedEntry(
                    entry_id,
                    turn_id,
                    int(compatible["entry_sequence"]),
                    int(compatible["event_sequence"]),
                )
            if job["accepted_entry_id"] is not None:
                return None
            sequence = self._prepare_external_result_target(
                connection,
                guard,
                turn_id=turn_id,
                entry_id=entry_id,
                new_context_binding_revision_id=new_context_binding_revision_id,
                source_workspace_id=str(job["workspace_id"]),
                requested_permission_mode=requested_permission_mode,
            )
            if sequence is None:
                return None
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=str(job["workspace_id"]),
                turn_id=turn_id,
                entry_id=entry_id,
                entry_sequence=sequence,
                entry_kind=EntryKind.USER_MESSAGE,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=content,
                source_job_id=job_id,
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_entry_id
                ) VALUES (%s, %s, 'ACCEPT_JOB_RESULT',
                          'accept_job_result.v1', %s, 'ENTRY', %s)
                """,
                (guard.session_id, command_id, digest, entry_id),
            )
            event = self._append_events(
                connection,
                guard,
                workspace_id=str(job["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.USER_MESSAGE_ACCEPTED,
                        SubjectSlot.ENTRY,
                        entry_id,
                        occurred_at=occurred_at,
                        actor_kind="job",
                        actor_id=actor_id,
                        payload={"source_job_id": job_id},
                    ),
                ),
            )[0]
            return AcceptedEntry(entry_id, turn_id, sequence, event.event_sequence)

    def _prepare_external_result_target(
        self,
        connection: Connection,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        entry_id: str,
        new_context_binding_revision_id: str | None,
        source_workspace_id: str,
        requested_permission_mode: PermissionMode | None,
    ) -> int | None:
        """Prepare the only two legal ROOT acceptance targets.

        The writer transaction already owns the session allocator row.  A
        missing revision selects an existing RUNNING ROOT; a present revision
        creates revision zero for an exact new ROOT.  Terminal parents are
        never silently reused or redirected.
        """

        workspace_id = self._workspace_id(connection, guard.session_id)
        if workspace_id != source_workspace_id:
            raise ConversationKernelConflict("external result workspace drifted")
        if new_context_binding_revision_id is None:
            turn = connection.execute(
                """
                SELECT workspace_id, conversation_scope_kind, status
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s
                FOR UPDATE
                """,
                (guard.session_id, turn_id),
            ).fetchone()
            if (
                turn is None
                or turn["conversation_scope_kind"] != "ROOT"
                or turn["status"] != "RUNNING"
            ):
                return None
            if str(turn["workspace_id"]) != source_workspace_id:
                raise ConversationKernelConflict("external result target drifted")
            return self._allocate_entry_sequence(connection, guard.session_id)
        if not new_context_binding_revision_id:
            raise ValueError("new ROOT context binding revision is empty")
        if requested_permission_mode is None:
            raise ValueError("new ROOT external result requires a permission mode")
        self._require_root_admission_open(connection, session_id=guard.session_id)
        existing = connection.execute(
            """
            SELECT id FROM pulsara_v3.turns
            WHERE session_id = %s
              AND (id = %s OR (conversation_scope_kind = 'ROOT' AND status = 'RUNNING'))
            LIMIT 1
            """,
            (guard.session_id, turn_id),
        ).fetchone()
        if existing is not None:
            return None
        sequence = self._allocate_entry_sequence(connection, guard.session_id)
        permission = self._freeze_root_permission_snapshot(
            connection,
            session_id=guard.session_id,
            snapshot_id=_stable_identity("permission-snapshot", turn_id),
            requested_mode=requested_permission_mode,
            admission_source=RunPermissionAdmissionSource.EXTERNAL_RESULT_COMMAND,
        )
        connection.execute(
            """
            INSERT INTO pulsara_v3.turns (
                id, session_id, workspace_id, conversation_scope_kind,
                status, initial_entry_id, current_context_binding_revision_id,
                permission_snapshot_id, requested_permission_mode,
                effective_permission_mode, permission_admission_source,
                permission_overlay, permission_plan_context_ordinal,
                permission_plan_workflow_id,
                permission_plan_revision_at_admission,
                permission_inherited_from_turn_id, permission_contract_id,
                permission_contract_fingerprint,
                permission_snapshot_fingerprint
            ) VALUES (%s, %s, %s, 'ROOT', 'RUNNING', %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                turn_id,
                guard.session_id,
                workspace_id,
                entry_id,
                new_context_binding_revision_id,
                *self._permission_columns(permission),
            ),
        )
        connection.execute(
            """
            INSERT INTO pulsara_v3.turn_context_binding_revisions (
                id, session_id, turn_id, revision_ordinal,
                base_kind, source_through_sequence
            ) VALUES (%s, %s, %s, 0, 'FULL_HISTORY', %s)
            """,
            (
                new_context_binding_revision_id,
                guard.session_id,
                turn_id,
                sequence - 1,
            ),
        )
        return sequence
