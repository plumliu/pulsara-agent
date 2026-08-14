"""Task-scoped subagent conversation operations."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping
from psycopg import IsolationLevel
from psycopg.rows import dict_row
from pulsara_agent.conversation_kernel.contracts import (
    CanonicalContent,
    CommittedEventDraft,
    CommittedEventSubject,
    ConversationScopeKind,
    EntryKind,
    HostWriterGuard,
)
from pulsara_agent.primitives.run_permission import RunPermissionAdmissionSource
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType, SubjectSlot
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .contracts import (
    AcceptedEntry,
    ConversationKernelConflict,
    PreparedSubagentTurnAdmission,
    TurnAdmissionConfirmation,
    TurnAdmissionConfirmationKind,
    _canonical_content_matches_utf8_text,
    _stable_identity,
    build_prepared_subagent_turn_admission,
)

from .matching import (
    _event_row_matches_draft,
)

class _SubagentOperations:
    @staticmethod
    def _subagent_cancellation_drafts(
        *,
        task_id: str,
        turn_id: str,
        task_status: str,
        task_reason: str,
        turn_reason: str,
        occurred_at: datetime,
        actor_id: str,
    ) -> tuple[CommittedEventDraft, CommittedEventDraft]:
        common = {
            "actor_kind": "runtime",
            "actor_id": actor_id,
            "sensitivity_class": "PUBLIC",
            "projection_profile": "DEFAULT",
            "occurred_at": occurred_at,
        }
        return (
            CommittedEventDraft(
                event_id=_stable_identity(
                    "event", task_id, turn_id, "TurnInterrupted", turn_reason
                ),
                event_type=CommittedEventType.TURN_INTERRUPTED,
                subject=CommittedEventSubject(SubjectSlot.TURN, turn_id),
                payload={"reason": turn_reason},
                **common,
            ),
            CommittedEventDraft(
                event_id=_stable_identity(
                    "event",
                    task_id,
                    turn_id,
                    "SubagentTaskStatusAccepted",
                    task_status,
                    task_reason,
                ),
                event_type=CommittedEventType.SUBAGENT_TASK_STATUS_ACCEPTED,
                subject=CommittedEventSubject(SubjectSlot.SUBAGENT_TASK, task_id),
                payload={"status": task_status, "reason": task_reason},
                **common,
            ),
        )

    def settle_cancelled_subagent_turn_and_task(
        self,
        guard: HostWriterGuard,
        *,
        task_id: str,
        turn_id: str,
        task_status: str,
        task_reason: str,
        turn_reason: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> bool:
        if (task_status, task_reason, turn_reason) not in {
            ("CANCELLED", "USER_CANCELLED", "USER_STOPPED"),
            ("INTERRUPTED", "HOST_CLOSING", "SESSION_CLOSED"),
        }:
            raise ValueError("subagent cancellation disposition is invalid")
        drafts = self._subagent_cancellation_drafts(
            task_id=task_id,
            turn_id=turn_id,
            task_status=task_status,
            task_reason=task_reason,
            turn_reason=turn_reason,
            occurred_at=occurred_at,
            actor_id=actor_id,
        )
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            task = connection.execute(
                """SELECT workspace_id, status, terminal_reason,
                          execution_writer_generation
                   FROM pulsara_v3.subagent_tasks
                   WHERE session_id = %s AND id = %s FOR UPDATE""",
                (guard.session_id, task_id),
            ).fetchone()
            turn = connection.execute(
                """SELECT workspace_id, status, terminal_reason,
                          conversation_scope_kind, scope_subagent_task_id
                   FROM pulsara_v3.turns
                   WHERE session_id = %s AND id = %s FOR UPDATE""",
                (guard.session_id, turn_id),
            ).fetchone()
            if task is None or turn is None:
                return False
            if (
                int(task["execution_writer_generation"]) != guard.writer_generation
                or str(turn["conversation_scope_kind"]) != "SUBAGENT_TASK"
                or str(turn["scope_subagent_task_id"]) != task_id
                or str(task["workspace_id"]) != str(turn["workspace_id"])
            ):
                raise ConversationKernelConflict(
                    "subagent cancellation target identity conflicts"
                )
            if (
                str(turn["status"]) != "RUNNING"
                or str(task["status"]) != "ACTIVE"
            ):
                return False
            connection.execute(
                """UPDATE pulsara_v3.plan_interactions
                   SET status = 'ABORTED', aborted_at = clock_timestamp()
                   WHERE session_id = %s AND origin_turn_id = %s
                     AND kind = 'QUESTION' AND status = 'OPEN'""",
                (guard.session_id, turn_id),
            )
            connection.execute(
                """UPDATE pulsara_v3.turns
                   SET status = 'INTERRUPTED', terminal_reason = %s,
                       terminal_at = clock_timestamp()
                   WHERE session_id = %s AND id = %s AND status = 'RUNNING'""",
                (turn_reason, guard.session_id, turn_id),
            )
            connection.execute(
                """UPDATE pulsara_v3.subagent_tasks
                   SET status = %s, terminal_reason = %s,
                       terminal_at = clock_timestamp()
                   WHERE session_id = %s AND id = %s
                     AND status IN ('PENDING', 'ACTIVE')""",
                (task_status, task_reason, guard.session_id, task_id),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=str(task["workspace_id"]),
                drafts=drafts,
            )
            return True

    def confirm_cancelled_subagent_turn_and_task(
        self,
        *,
        session_id: str,
        task_id: str,
        turn_id: str,
        task_status: str,
        task_reason: str,
        turn_reason: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> TurnAdmissionConfirmation:
        drafts = self._subagent_cancellation_drafts(
            task_id=task_id,
            turn_id=turn_id,
            task_status=task_status,
            task_reason=task_reason,
            turn_reason=turn_reason,
            occurred_at=occurred_at,
            actor_id=actor_id,
        )
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            task = connection.execute(
                """SELECT status, terminal_reason FROM pulsara_v3.subagent_tasks
                   WHERE session_id = %s AND id = %s""",
                (session_id, task_id),
            ).fetchone()
            turn = connection.execute(
                """SELECT status, terminal_reason, conversation_scope_kind,
                          scope_subagent_task_id, initial_entry_id, final_entry_id
                   FROM pulsara_v3.turns WHERE session_id = %s AND id = %s""",
                (session_id, turn_id),
            ).fetchone()
            events = tuple(
                connection.execute(
                    """SELECT * FROM pulsara_v3.agent_events
                       WHERE session_id = %s AND event_id = %s""",
                    (session_id, draft.event_id),
                ).fetchone()
                for draft in drafts
            )
            no_events = all(row is None for row in events)
            if turn is None and no_events and task is not None:
                # The task coordination row is accepted before the task-scoped
                # turn.  Cancellation may therefore win before admission; that
                # is a clean NONE for the joint candidate and is settled through
                # the existing task-only terminal transition.
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.NONE)
            if task is None and turn is None and no_events:
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.NONE)
            if task is None or turn is None:
                return TurnAdmissionConfirmation(
                    TurnAdmissionConfirmationKind.CONFLICT
                )
            scope_matches = (
                str(turn["conversation_scope_kind"]) == "SUBAGENT_TASK"
                and str(turn["scope_subagent_task_id"]) == task_id
            )
            if not scope_matches:
                return TurnAdmissionConfirmation(
                    TurnAdmissionConfirmationKind.CONFLICT
                )
            if (
                str(task["status"]) == "ACTIVE"
                and str(turn["status"]) == "RUNNING"
                and no_events
            ):
                # This is the ordinary confirm-before-write state.  Neither
                # accepted coordination row is partial; the immutable
                # cancellation candidate simply has no winner yet.
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.NONE)
            if (
                str(turn["status"]) == "COMPLETED"
                and str(task["status"]) in {"ACTIVE", "COMPLETED"}
                and no_events
                and turn["final_entry_id"] is not None
            ):
                # The assistant winner may commit immediately before the child
                # manager resumes from its runner await.  Cancellation cannot
                # replace that winner; return its exact entry so the existing
                # result/task lineage can finish under the same Host owner.
                return TurnAdmissionConfirmation(
                    TurnAdmissionConfirmationKind.HISTORICAL_TERMINAL,
                    self._accepted_entry(
                        connection, session_id, str(turn["final_entry_id"])
                    ),
                )
            if any(row is None for row in events):
                return TurnAdmissionConfirmation(
                    TurnAdmissionConfirmationKind.CONFLICT
                )
            assert all(row is not None for row in events)
            if not (
                str(task["status"]) == task_status
                and str(task["terminal_reason"]) == task_reason
                and str(turn["status"]) == "INTERRUPTED"
                and str(turn["terminal_reason"]) == turn_reason
                and all(
                    _event_row_matches_draft(row, draft)
                    for row, draft in zip(events, drafts, strict=True)
                )
            ):
                return TurnAdmissionConfirmation(
                    TurnAdmissionConfirmationKind.CONFLICT
                )
            return TurnAdmissionConfirmation(
                TurnAdmissionConfirmationKind.FULL,
                self._accepted_entry(
                    connection, session_id, str(turn["initial_entry_id"])
                ),
            )

    def accept_subagent_task(
        self,
        guard: HostWriterGuard,
        *,
        task_id: str,
        parent_turn_id: str,
        objective: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> None:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            workspace_id = self._workspace_id(connection, guard.session_id)
            parent = connection.execute(
                """
                SELECT conversation_scope_kind, status
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s
                """,
                (guard.session_id, parent_turn_id),
            ).fetchone()
            if parent is None or parent["conversation_scope_kind"] != "ROOT":
                raise ConversationKernelConflict("subagent parent is not a ROOT turn")
            connection.execute(
                """
                INSERT INTO pulsara_v3.subagent_tasks (
                    id, session_id, workspace_id, parent_turn_id,
                    objective, status, execution_writer_generation
                ) VALUES (%s, %s, %s, %s, %s, 'PENDING', %s)
                """,
                (
                    task_id,
                    guard.session_id,
                    workspace_id,
                    parent_turn_id,
                    objective,
                    guard.writer_generation,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        CommittedEventType.SUBAGENT_TASK_ACCEPTED,
                        SubjectSlot.SUBAGENT_TASK,
                        task_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"status": "PENDING"},
                    ),
                ),
            )

    def set_subagent_task_status(
        self,
        guard: HostWriterGuard,
        *,
        task_id: str,
        status: str,
        reason: str | None,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
        require_absent_turn_id: str | None = None,
    ) -> bool:
        if status not in {
            "ACTIVE",
            "COMPLETED",
            "FAILED",
            "INTERRUPTED",
            "CANCELLED",
        }:
            raise ValueError("subagent transition status is invalid")
        terminal = status in {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                UPDATE pulsara_v3.subagent_tasks
                SET status = %s, terminal_reason = %s,
                    terminal_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END
                WHERE session_id = %s AND id = %s
                  AND execution_writer_generation = %s
                  AND (
                    %s::text IS NULL OR NOT EXISTS (
                      SELECT 1 FROM pulsara_v3.turns AS child_turn
                      WHERE child_turn.session_id = subagent_tasks.session_id
                        AND child_turn.id = %s
                    )
                  )
                  AND (
                    (status = 'PENDING' AND %s = 'ACTIVE') OR
                    (status IN ('PENDING', 'ACTIVE') AND %s IN (
                      'COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED'
                    ))
                  )
                RETURNING workspace_id
                """,
                (
                    status,
                    reason,
                    terminal,
                    guard.session_id,
                    task_id,
                    guard.writer_generation,
                    require_absent_turn_id,
                    require_absent_turn_id,
                    status,
                    status,
                ),
            ).fetchone()
            if row is None:
                return False
            self._append_events(
                connection,
                guard,
                workspace_id=str(row["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.SUBAGENT_TASK_STATUS_ACCEPTED,
                        SubjectSlot.SUBAGENT_TASK,
                        task_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"status": status, "reason": reason},
                    ),
                ),
            )
            return True

    def start_subagent_turn(
        self,
        guard: HostWriterGuard,
        *,
        task_id: str,
        turn_id: str,
        entry_id: str,
        context_binding_revision_id: str,
        content: CanonicalContent,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
        _prepared_candidate: PreparedSubagentTurnAdmission | None = None,
    ) -> AcceptedEntry:
        prepared = _prepared_candidate or build_prepared_subagent_turn_admission(
            session_id=guard.session_id,
            task_id=task_id,
            turn_id=turn_id,
            entry_id=entry_id,
            context_binding_revision_id=context_binding_revision_id,
            permission_snapshot_id=_stable_identity("permission-snapshot", turn_id),
            content=content,
            occurred_at=occurred_at,
            actor_id=actor_id,
        )
        if (
            prepared.session_id != guard.session_id
            or prepared.task_id != task_id
            or prepared.turn_id != turn_id
            or prepared.entry_id != entry_id
            or prepared.context_binding_revision_id
            != context_binding_revision_id
            or prepared.content != content
            or prepared.occurred_at != occurred_at
            or prepared.actor_id != actor_id
        ):
            raise ValueError("prepared subagent admission does not exact-join arguments")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            task = connection.execute(
                """
                SELECT workspace_id, parent_turn_id, objective
                FROM pulsara_v3.subagent_tasks
                WHERE session_id = %s AND id = %s AND status = 'ACTIVE'
                  AND execution_writer_generation = %s
                FOR UPDATE
                """,
                (guard.session_id, task_id, guard.writer_generation),
            ).fetchone()
            if task is None:
                raise ConversationKernelConflict("subagent task is not active")
            if not _canonical_content_matches_utf8_text(
                prepared.content, str(task["objective"])
            ):
                raise ConversationKernelConflict(
                    "subagent initial content conflicts with immutable objective"
                )
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            permission = self._freeze_subagent_permission_snapshot(
                connection,
                session_id=guard.session_id,
                snapshot_id=prepared.permission_snapshot_id,
                parent_turn_id=str(task["parent_turn_id"]),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.turns (
                    id, session_id, workspace_id, conversation_scope_kind,
                    scope_subagent_task_id, status, initial_entry_id,
                    current_context_binding_revision_id,
                    permission_snapshot_id, requested_permission_mode,
                    effective_permission_mode, permission_admission_source,
                    permission_overlay, permission_plan_context_ordinal,
                    permission_plan_workflow_id,
                    permission_plan_revision_at_admission,
                    permission_inherited_from_turn_id, permission_contract_id,
                    permission_contract_fingerprint,
                    permission_snapshot_fingerprint
                ) VALUES (%s, %s, %s, 'SUBAGENT_TASK', %s,
                          'RUNNING', %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    turn_id,
                    guard.session_id,
                    task["workspace_id"],
                    task_id,
                    entry_id,
                    context_binding_revision_id,
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
                    context_binding_revision_id,
                    guard.session_id,
                    turn_id,
                    entry_sequence - 1,
                ),
            )
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=str(task["workspace_id"]),
                turn_id=turn_id,
                entry_id=entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.USER_MESSAGE,
                scope_kind=ConversationScopeKind.SUBAGENT_TASK,
                scope_task_id=task_id,
                content=content,
            )
            event = self._append_events(
                connection,
                guard,
                workspace_id=str(task["workspace_id"]),
                drafts=(prepared.event,),
            )[0]
            return AcceptedEntry(
                entry_id, turn_id, entry_sequence, event.event_sequence
            )

    def accept_subagent_turn(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedSubagentTurnAdmission,
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        if candidate.session_id != guard.session_id:
            raise ValueError("prepared subagent admission belongs to another session")
        return self.start_subagent_turn(
            guard,
            task_id=candidate.task_id,
            turn_id=candidate.turn_id,
            entry_id=candidate.entry_id,
            context_binding_revision_id=candidate.context_binding_revision_id,
            content=candidate.content,
            occurred_at=candidate.occurred_at,
            actor_id=candidate.actor_id,
            deadline_monotonic=deadline_monotonic,
            _prepared_candidate=candidate,
        )

    def confirm_subagent_turn_admission(
        self,
        *,
        candidate: PreparedSubagentTurnAdmission,
        guard: HostWriterGuard | None = None,
        deadline_monotonic: float,
    ) -> TurnAdmissionConfirmation:
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            if guard is not None:
                if guard.session_id != candidate.session_id:
                    raise ValueError(
                        "subagent admission guard belongs to another session"
                    )
                self._require_writer(connection, guard, lock=False)
            task = connection.execute(
                """SELECT * FROM pulsara_v3.subagent_tasks
                   WHERE session_id = %s AND id = %s""",
                (candidate.session_id, candidate.task_id),
            ).fetchone()
            turn = connection.execute(
                """SELECT * FROM pulsara_v3.turns
                   WHERE session_id = %s AND id = %s""",
                (candidate.session_id, candidate.turn_id),
            ).fetchone()
            revision = connection.execute(
                """SELECT * FROM pulsara_v3.turn_context_binding_revisions
                   WHERE session_id = %s AND id = %s""",
                (candidate.session_id, candidate.context_binding_revision_id),
            ).fetchone()
            entry = connection.execute(
                """SELECT * FROM pulsara_v3.transcript_entries
                   WHERE session_id = %s AND id = %s""",
                (candidate.session_id, candidate.entry_id),
            ).fetchone()
            event = connection.execute(
                """SELECT * FROM pulsara_v3.agent_events
                   WHERE session_id = %s AND event_id = %s""",
                (candidate.session_id, candidate.event.event_id),
            ).fetchone()
            required = (turn, revision, entry, event)
            if task is None or not _canonical_content_matches_utf8_text(
                candidate.content, str(task["objective"])
            ):
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.CONFLICT)
            if all(row is None for row in required):
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.NONE)
            if any(row is None for row in required):
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.CONFLICT)
            assert turn is not None and revision is not None
            assert entry is not None and event is not None
            try:
                permission = self._permission_from_row(turn)
            except (TypeError, ValueError):
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.CONFLICT)
            matches = (
                str(turn["conversation_scope_kind"]) == "SUBAGENT_TASK"
                and str(turn["scope_subagent_task_id"]) == candidate.task_id
                and str(turn["initial_entry_id"]) == candidate.entry_id
                and str(turn["current_context_binding_revision_id"])
                == candidate.context_binding_revision_id
                and permission.snapshot_id == candidate.permission_snapshot_id
                and permission.admission_source
                is RunPermissionAdmissionSource.SUBAGENT_INHERITANCE
                and permission.inherited_from_turn_id == str(task["parent_turn_id"])
                and int(revision["revision_ordinal"]) == 0
                and str(revision["base_kind"]) == "FULL_HISTORY"
                and revision["context_snapshot_id"] is None
                and int(revision["source_through_sequence"])
                == int(entry["entry_sequence"]) - 1
                and str(entry["turn_id"]) == candidate.turn_id
                and str(entry["entry_kind"]) == EntryKind.USER_MESSAGE.value
                and str(entry["conversation_scope_kind"]) == "SUBAGENT_TASK"
                and str(entry["scope_subagent_task_id"]) == candidate.task_id
                and self._content_from_row(entry) == candidate.content
                and _event_row_matches_draft(event, candidate.event)
            )
            if not matches:
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.CONFLICT)
            return TurnAdmissionConfirmation(
                TurnAdmissionConfirmationKind.FULL,
                self._accepted_entry(connection, candidate.session_id, candidate.entry_id),
            )

    def accept_subagent_child(
        self,
        guard: HostWriterGuard,
        *,
        child_id: str,
        task_id: str,
        child_kind: str,
        child_ordinal: int | None,
        entry_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> None:
        if (
            child_kind not in {"MESSAGE", "RESULT"}
            or (child_ordinal is None and child_kind != "RESULT")
            or (child_ordinal is not None and child_ordinal < 0)
        ):
            raise ValueError("subagent child carrier is invalid")
        event_type = (
            CommittedEventType.SUBAGENT_MESSAGE_ACCEPTED
            if child_kind == "MESSAGE"
            else CommittedEventType.SUBAGENT_RESULT_ACCEPTED
        )
        slot = (
            SubjectSlot.SUBAGENT_MESSAGE
            if child_kind == "MESSAGE"
            else SubjectSlot.SUBAGENT_RESULT
        )
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            workspace_id = self._workspace_id(connection, guard.session_id)
            counts = connection.execute(
                """
                SELECT count(*) FILTER (WHERE child_kind = 'MESSAGE') AS messages,
                       count(*) FILTER (WHERE child_kind = 'RESULT') AS results
                FROM pulsara_v3.subagent_task_children
                WHERE session_id = %s AND task_id = %s
                """,
                (guard.session_id, task_id),
            ).fetchone()
            message_count = int(counts["messages"])
            result_count = int(counts["results"])
            resolved_ordinal = (
                message_count if child_ordinal is None else child_ordinal
            )
            existing = connection.execute(
                """
                SELECT task_id, child_kind, child_ordinal, entry_id
                FROM pulsara_v3.subagent_task_children
                WHERE session_id = %s AND id = %s
                """,
                (guard.session_id, child_id),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["task_id"]) != task_id
                    or str(existing["child_kind"]) != child_kind
                    or int(existing["child_ordinal"]) != resolved_ordinal
                    or str(existing["entry_id"]) != entry_id
                ):
                    raise ConversationKernelConflict(
                        "subagent child identity names a different fact"
                    )
                return
            if result_count or resolved_ordinal != message_count:
                raise ConversationKernelConflict(
                    "subagent child ordinal or terminal result conflicts"
                )
            connection.execute(
                """
                INSERT INTO pulsara_v3.subagent_task_children (
                    id, session_id, task_id, child_kind, child_ordinal, entry_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    child_id,
                    guard.session_id,
                    task_id,
                    child_kind,
                    resolved_ordinal,
                    entry_id,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        event_type,
                        slot,
                        child_id,
                        occurred_at=occurred_at,
                        actor_kind="subagent",
                        actor_id=actor_id,
                        payload={"child_ordinal": resolved_ordinal},
                    ),
                ),
            )

    def query_subagent_task(
        self,
        *,
        session_id: str,
        task_id: str,
        deadline_monotonic: float,
    ) -> Mapping[str, object] | None:
        """Read durable task/result state without recovering execution."""
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT t.id, t.parent_turn_id, t.objective, t.status,
                       t.terminal_reason, c.id AS result_id,
                       c.entry_id AS result_entry_id,
                       accepted.id AS accepted_root_entry_id
                FROM pulsara_v3.subagent_tasks AS t
                LEFT JOIN pulsara_v3.subagent_task_children AS c
                  ON c.session_id = t.session_id AND c.task_id = t.id
                 AND c.child_kind = 'RESULT'
                LEFT JOIN pulsara_v3.transcript_entries AS accepted
                  ON accepted.session_id = c.session_id
                 AND accepted.source_subagent_result_id = c.id
                WHERE t.session_id = %s AND t.id = %s
                """,
                (session_id, task_id),
            ).fetchone()
            return None if row is None else dict(row)

    def list_subagent_tasks(
        self,
        *,
        session_id: str,
        maximum_items: int,
        deadline_monotonic: float,
    ) -> tuple[Mapping[str, object], ...]:
        if not 1 <= maximum_items <= 50:
            raise ValueError("subagent list bound is invalid")
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT t.id, t.parent_turn_id, t.objective, t.status,
                           t.terminal_reason, c.id AS result_id,
                           c.entry_id AS result_entry_id,
                           accepted.id AS accepted_root_entry_id
                    FROM pulsara_v3.subagent_tasks AS t
                    LEFT JOIN pulsara_v3.subagent_task_children AS c
                      ON c.session_id = t.session_id AND c.task_id = t.id
                     AND c.child_kind = 'RESULT'
                    LEFT JOIN pulsara_v3.transcript_entries AS accepted
                      ON accepted.session_id = c.session_id
                     AND accepted.source_subagent_result_id = c.id
                    WHERE t.session_id = %s
                    ORDER BY t.accepted_at DESC, t.id DESC LIMIT %s
                    """,
                    (session_id, maximum_items),
                ).fetchall()
            )
