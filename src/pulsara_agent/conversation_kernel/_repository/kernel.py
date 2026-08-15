"""Shared physical repository kernel and transaction primitives."""

from __future__ import annotations

from datetime import datetime
from threading import local
from typing import Callable, Mapping, Sequence
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pulsara_agent.conversation_kernel.contracts import AssistantBlockKind, CanonicalContent, CommittedEventDraft, CommittedEventSubject, ConversationScopeKind, EntryKind, HostWriterGuard, JobAttemptClaimGuard, StoredCommittedEvent
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot, RunPermissionAdmissionSource, RunPermissionOverlay, build_run_permission_snapshot
from pulsara_agent.primitives.plan_workflow import PlanHandoffKind, PlanInteractionKind
from pulsara_agent.conversation_kernel.vocabulary import DESCRIPTOR_BY_TYPE, AppendGuardKind, CommittedEventType, SubjectSlot
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane, VerifiedPostgresConnectionProviderProtocol

from .contracts import (
    AcceptedEntry,
    AssistantBlock,
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    JobCancellationRequested,
    StaleHostWriter,
    StaleJobClaim,
    _content_columns,
    _id,
    _utcnow,
)

class _RepositoryKernel:
    def __init__(
        self,
        connection_provider: VerifiedPostgresConnectionProviderProtocol,
        *,
        post_commit_tap: Callable[[tuple[StoredCommittedEvent, ...]], None]
        | None = None,
    ) -> None:
        self._provider = connection_provider
        self._post_commit_tap = post_commit_tap
        self._event_batch_local = local()

    @property
    def connection_provider(self) -> VerifiedPostgresConnectionProviderProtocol:
        """Read-only construction seam for canonical query services."""

        return self._provider

    @staticmethod
    def _exact_event_for_confirmation(
        connection: Connection,
        expected: CommittedEventDraft,
        *,
        session_id: str,
        workspace_id: str,
    ) -> Mapping[str, object]:
        rows = connection.execute(
            "SELECT * FROM pulsara_v3.agent_events WHERE event_id = %s",
            (expected.event_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ConversationKernelConflict(
                "prepared tool result occurrence is absent or non-unique"
            )
        row = rows[0]
        subject_column = DESCRIPTOR_BY_TYPE[expected.event_type].subject_slot.value
        if (
            str(row["session_id"]) != session_id
            or str(row["workspace_id"]) != workspace_id
            or str(row["event_type"]) != expected.event_type.value
            or row[subject_column] != expected.subject.subject_id
            or row["occurred_at"] != expected.occurred_at
            or str(row["actor_kind"]) != expected.actor_kind
            or str(row["actor_id"]) != expected.actor_id
            or str(row["sensitivity_class"]) != expected.sensitivity_class
            or str(row["projection_profile"]) != expected.projection_profile
            or dict(row["payload"]) != dict(expected.payload)
        ):
            raise ConversationKernelConflict(
                "prepared occurrence identity names a different winner"
            )
        return row

    def _writer_transaction(self, guard: HostWriterGuard, *, deadline_monotonic: float):
        repository = self

        class _Scope:
            def __enter__(self) -> Connection:
                repository._begin_event_batch()
                self._cm = repository._provider.connection(
                    lane=PostgresConnectionLane.HOST_CONTROL,
                    row_factory=dict_row,
                    deadline_monotonic=deadline_monotonic,
                )
                try:
                    connection = self._cm.__enter__()
                except BaseException:
                    repository._finish_event_batch(committed=False)
                    raise
                try:
                    repository._require_writer(connection, guard, lock=True)
                except BaseException as error:
                    self._cm.__exit__(type(error), error, error.__traceback__)
                    repository._finish_event_batch(committed=False)
                    raise
                self._connection = connection
                return connection

            def __exit__(self, exc_type, exc, tb):
                try:
                    result = self._cm.__exit__(exc_type, exc, tb)
                except BaseException:
                    repository._finish_event_batch(committed=False)
                    raise
                repository._finish_event_batch(committed=exc_type is None)
                return result

        return _Scope()

    def _job_transaction(
        self,
        guard: JobAttemptClaimGuard,
        *,
        deadline_monotonic: float,
        allow_cancel_requested: bool = False,
    ):
        repository = self

        class _Scope:
            def __enter__(self) -> Connection:
                repository._begin_event_batch()
                self._cm = repository._provider.connection(
                    lane=PostgresConnectionLane.BACKGROUND_WORK,
                    row_factory=dict_row,
                    deadline_monotonic=deadline_monotonic,
                )
                try:
                    connection = self._cm.__enter__()
                except BaseException:
                    repository._finish_event_batch(committed=False)
                    raise
                try:
                    if guard.origin_session_id is not None:
                        session = connection.execute(
                            """
                            SELECT id FROM pulsara_v3.sessions
                            WHERE id = %s
                            FOR UPDATE
                            """,
                            (guard.origin_session_id,),
                        ).fetchone()
                        if session is None:
                            raise StaleJobClaim("job origin session is absent")
                    repository._require_job_claim(connection, guard, lock=True)
                    if not allow_cancel_requested:
                        cancellation = connection.execute(
                            """
                            SELECT cancel_requested_at
                            FROM pulsara_v3.durable_jobs
                            WHERE id = %s
                            """,
                            (guard.job_id,),
                        ).fetchone()
                        if (
                            cancellation is not None
                            and cancellation["cancel_requested_at"] is not None
                        ):
                            raise JobCancellationRequested(
                                "job cancellation was requested"
                            )
                except BaseException as error:
                    self._cm.__exit__(type(error), error, error.__traceback__)
                    repository._finish_event_batch(committed=False)
                    raise
                self._connection = connection
                return connection

            def __exit__(self, exc_type, exc, tb):
                try:
                    result = self._cm.__exit__(exc_type, exc, tb)
                except BaseException:
                    repository._finish_event_batch(committed=False)
                    raise
                repository._finish_event_batch(committed=exc_type is None)
                return result

        return _Scope()

    def _event_transaction(
        self, *, lane: PostgresConnectionLane, deadline_monotonic: float
    ):
        repository = self

        class _Scope:
            def __enter__(self) -> Connection:
                repository._begin_event_batch()
                self._cm = repository._provider.connection(
                    lane=lane,
                    row_factory=dict_row,
                    deadline_monotonic=deadline_monotonic,
                )
                try:
                    return self._cm.__enter__()
                except BaseException:
                    repository._finish_event_batch(committed=False)
                    raise

            def __exit__(self, exc_type, exc, tb):
                try:
                    result = self._cm.__exit__(exc_type, exc, tb)
                except BaseException:
                    repository._finish_event_batch(committed=False)
                    raise
                repository._finish_event_batch(committed=exc_type is None)
                return result

        return _Scope()

    def _begin_event_batch(self) -> None:
        stack = getattr(self._event_batch_local, "stack", None)
        if stack is None:
            stack = []
            self._event_batch_local.stack = stack
        stack.append([])

    def _record_event_batch(self, events: Sequence[StoredCommittedEvent]) -> None:
        if not events:
            return
        stack = getattr(self._event_batch_local, "stack", None)
        if stack:
            stack[-1].extend(events)

    def _finish_event_batch(self, *, committed: bool) -> None:
        stack = getattr(self._event_batch_local, "stack", None)
        if not stack:
            raise RuntimeError("repository event batch owner is absent")
        events = tuple(stack.pop())
        if not stack:
            del self._event_batch_local.stack
        if not committed:
            return
        if stack:
            stack[-1].extend(events)
            return
        tap = self._post_commit_tap
        if tap is None or not events:
            return
        try:
            tap(events)
        except BaseException:
            # An extension tap is process-local best effort.  A committed
            # canonical transaction can never be reclassified by observation.
            return

    @staticmethod
    def _require_writer(
        connection: Connection, guard: HostWriterGuard, *, lock: bool
    ) -> Mapping[str, object]:
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            """
            SELECT * FROM pulsara_v3.sessions
            WHERE id = %s AND lifecycle = 'OPEN'
              AND writer_generation = %s AND writer_lease_owner_id = %s
              AND writer_lease_expires_at > clock_timestamp()
            """
            + suffix,
            (guard.session_id, guard.writer_generation, guard.writer_owner_id),
        ).fetchone()
        if row is None:
            raise StaleHostWriter("host writer generation is stale")
        return row

    @staticmethod
    def _require_job_claim(
        connection: Connection, guard: JobAttemptClaimGuard, *, lock: bool
    ) -> Mapping[str, object]:
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            """
            SELECT a.*, j.status AS job_status
            FROM pulsara_v3.durable_job_attempts AS a
            JOIN pulsara_v3.durable_jobs AS j ON j.id = a.job_id
            WHERE a.id = %s AND a.job_id = %s
              AND a.claim_generation = %s AND a.claim_owner_id = %s
              AND a.lease_expires_at > clock_timestamp()
              AND a.terminal_status IS NULL AND j.status = 'ACTIVE'
            """
            + suffix,
            (
                guard.attempt_id,
                guard.job_id,
                guard.claim_generation,
                guard.claim_owner_id,
            ),
        ).fetchone()
        if row is None:
            raise StaleJobClaim("job attempt claim is stale")
        return row

    def _interrupt_prior_generation(
        self,
        connection: Connection,
        *,
        guard: HostWriterGuard,
        workspace_id: str,
    ) -> None:
        session_id = guard.session_id
        turn_ids = tuple(
            str(row["id"])
            for row in connection.execute(
                """
                UPDATE pulsara_v3.turns
                SET status = 'INTERRUPTED', terminal_reason = 'HOST_TAKEOVER',
                    terminal_at = clock_timestamp()
                WHERE session_id = %s AND status = 'RUNNING'
                RETURNING id
                """,
                (session_id,),
            ).fetchall()
        )
        if turn_ids:
            connection.execute(
                """
                UPDATE pulsara_v3.plan_interactions
                SET status = 'ABORTED', aborted_at = clock_timestamp()
                WHERE session_id = %s AND kind = 'QUESTION'
                  AND status = 'OPEN' AND origin_turn_id = ANY(%s)
                """,
                (session_id, list(turn_ids)),
            )
        task_ids = tuple(
            str(row["id"])
            for row in connection.execute(
                """
                UPDATE pulsara_v3.subagent_tasks
                SET status = 'INTERRUPTED',
                    terminal_reason = 'HOST_TAKEOVER',
                    terminal_at = clock_timestamp()
                WHERE session_id = %s
                  AND status IN ('PENDING', 'ACTIVE')
                RETURNING id
                """,
                (session_id,),
            ).fetchall()
        )
        rejected_steer_ids: tuple[str, ...] = ()
        if turn_ids:
            rejected_steer_ids = tuple(
                str(row["id"])
                for row in connection.execute(
                    """
                    UPDATE pulsara_v3.prompt_queue_items
                    SET status = 'REJECTED',
                        terminal_reason = 'TARGET_TURN_INTERRUPTED',
                        terminal_at = clock_timestamp()
                    WHERE session_id = %s AND status = 'PENDING'
                      AND delivery_mode = 'STEER_ACTIVE_TURN'
                      AND target_turn_id = ANY(%s)
                    RETURNING id
                    """,
                    (session_id, list(turn_ids)),
                ).fetchall()
            )
        if turn_ids or task_ids or rejected_steer_ids:
            occurred_at = _utcnow()
            drafts = (
                tuple(
                    self._event(
                        CommittedEventType.TURN_INTERRUPTED,
                        SubjectSlot.TURN,
                        turn_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=guard.writer_owner_id,
                        payload={"reason": "HOST_TAKEOVER"},
                    )
                    for turn_id in turn_ids
                )
                + tuple(
                    self._event(
                        CommittedEventType.SUBAGENT_TASK_STATUS_ACCEPTED,
                        SubjectSlot.SUBAGENT_TASK,
                        task_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=guard.writer_owner_id,
                        payload={"status": "INTERRUPTED", "reason": "HOST_TAKEOVER"},
                    )
                    for task_id in task_ids
                )
                + tuple(
                    self._event(
                        CommittedEventType.PROMPT_REJECTED,
                        SubjectSlot.QUEUE_ITEM,
                        queue_item_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=guard.writer_owner_id,
                        payload={"reason": "TARGET_TURN_INTERRUPTED"},
                    )
                    for queue_item_id in rejected_steer_ids
                )
            )
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=drafts,
            )

    @staticmethod
    def _permission_columns(
        snapshot: FrozenRunPermissionSnapshot,
    ) -> tuple[object, ...]:
        return (
            snapshot.snapshot_id,
            snapshot.requested_mode.value,
            snapshot.effective_mode.value,
            snapshot.admission_source.value,
            snapshot.overlay.value,
            snapshot.plan_context_ordinal_at_admission,
            snapshot.plan_workflow_id,
            snapshot.plan_workflow_revision_at_admission,
            snapshot.inherited_from_turn_id,
            snapshot.permission_contract_id,
            snapshot.permission_contract_fingerprint,
            snapshot.snapshot_fingerprint,
        )

    @staticmethod
    def _permission_from_row(row: Mapping[str, object]) -> FrozenRunPermissionSnapshot:
        return FrozenRunPermissionSnapshot(
            snapshot_id=str(row["permission_snapshot_id"]),
            requested_mode=PermissionMode(str(row["requested_permission_mode"])),
            effective_mode=PermissionMode(str(row["effective_permission_mode"])),
            admission_source=RunPermissionAdmissionSource(
                str(row["permission_admission_source"])
            ),
            overlay=RunPermissionOverlay(str(row["permission_overlay"])),
            plan_context_ordinal_at_admission=int(
                row["permission_plan_context_ordinal"]
            ),
            plan_workflow_id=(
                None
                if row["permission_plan_workflow_id"] is None
                else str(row["permission_plan_workflow_id"])
            ),
            plan_workflow_revision_at_admission=(
                None
                if row["permission_plan_revision_at_admission"] is None
                else int(row["permission_plan_revision_at_admission"])
            ),
            inherited_from_turn_id=(
                None
                if row["permission_inherited_from_turn_id"] is None
                else str(row["permission_inherited_from_turn_id"])
            ),
            permission_contract_id=str(row["permission_contract_id"]),
            permission_contract_fingerprint=str(row["permission_contract_fingerprint"]),
            snapshot_fingerprint=str(row["permission_snapshot_fingerprint"]),
        )

    @staticmethod
    def _open_plan_interaction(
        connection: Connection, session_id: str
    ) -> Mapping[str, object] | None:
        return connection.execute(
            """
            SELECT id, plan_workflow_id, kind, origin_turn_id
            FROM pulsara_v3.plan_interactions
            WHERE session_id = %s AND status = 'OPEN'
            ORDER BY accepted_at, id
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()

    @classmethod
    def _require_root_admission_open(
        cls,
        connection: Connection,
        *,
        session_id: str,
        allowed_plan_interaction_id: str | None = None,
    ) -> None:
        interaction = cls._open_plan_interaction(connection, session_id)
        if interaction is None:
            return
        if (
            allowed_plan_interaction_id is not None
            and str(interaction["id"]) == allowed_plan_interaction_id
        ):
            return
        code = (
            "PLAN_QUESTION_PENDING"
            if str(interaction["kind"]) == PlanInteractionKind.QUESTION.value
            else "PLAN_REVIEW_PENDING"
        )
        raise ConversationKernelConflict(code)

    @staticmethod
    def _freeze_root_permission_snapshot(
        connection: Connection,
        *,
        session_id: str,
        snapshot_id: str,
        requested_mode: PermissionMode,
        admission_source: RunPermissionAdmissionSource,
        inherited_from_turn_id: str | None = None,
        force_plan_workflow_id: str | None = None,
        force_plan_read_only: bool | None = None,
    ) -> FrozenRunPermissionSnapshot:
        pending_review = connection.execute(
            """
            SELECT i.id
            FROM pulsara_v3.plan_interactions AS i
            JOIN pulsara_v3.plan_workflows AS w
              ON w.session_id = i.session_id AND w.id = i.plan_workflow_id
            WHERE i.session_id = %s AND i.kind = 'DRAFT_REVIEW'
              AND i.status = 'OPEN' AND w.status = 'ACTIVE'
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if pending_review is not None:
            raise ConversationKernelConflict("PLAN_REVIEW_PENDING")
        latest = connection.execute(
            """
            SELECT workflow_ordinal FROM pulsara_v3.plan_workflows
            WHERE session_id = %s
            ORDER BY workflow_ordinal DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        active = connection.execute(
            """
            SELECT id, workflow_ordinal, workflow_revision
            FROM pulsara_v3.plan_workflows
            WHERE session_id = %s AND status = 'ACTIVE'
            """,
            (session_id,),
        ).fetchone()
        latest_ordinal = 0 if latest is None else int(latest["workflow_ordinal"])
        use_plan = active is not None
        if force_plan_read_only is not None:
            use_plan = force_plan_read_only
        if use_plan:
            if active is None:
                raise ConversationKernelConflict("active Plan workflow is absent")
            if (
                force_plan_workflow_id is not None
                and str(active["id"]) != force_plan_workflow_id
            ):
                raise ConversationKernelConflict("Plan workflow identity drifted")
            return build_run_permission_snapshot(
                snapshot_id=snapshot_id,
                requested_mode=requested_mode,
                effective_mode=PermissionMode.READ_ONLY,
                admission_source=admission_source,
                overlay=RunPermissionOverlay.PLAN_READ_ONLY,
                plan_context_ordinal_at_admission=int(active["workflow_ordinal"]),
                plan_workflow_id=str(active["id"]),
                plan_workflow_revision_at_admission=int(active["workflow_revision"]),
                inherited_from_turn_id=inherited_from_turn_id,
            )
        return build_run_permission_snapshot(
            snapshot_id=snapshot_id,
            requested_mode=requested_mode,
            effective_mode=requested_mode,
            admission_source=admission_source,
            overlay=RunPermissionOverlay.NONE,
            plan_context_ordinal_at_admission=latest_ordinal,
            inherited_from_turn_id=inherited_from_turn_id,
        )

    @classmethod
    def _freeze_subagent_permission_snapshot(
        cls,
        connection: Connection,
        *,
        session_id: str,
        snapshot_id: str,
        parent_turn_id: str,
    ) -> FrozenRunPermissionSnapshot:
        parent = connection.execute(
            """
            SELECT * FROM pulsara_v3.turns
            WHERE session_id = %s AND id = %s
            """,
            (session_id, parent_turn_id),
        ).fetchone()
        if parent is None:
            raise ConversationKernelConflict("subagent parent turn is absent")
        parent_snapshot = cls._permission_from_row(parent)
        return build_run_permission_snapshot(
            snapshot_id=snapshot_id,
            requested_mode=parent_snapshot.effective_mode,
            effective_mode=parent_snapshot.effective_mode,
            admission_source=RunPermissionAdmissionSource.SUBAGENT_INHERITANCE,
            overlay=RunPermissionOverlay.NONE,
            plan_context_ordinal_at_admission=(
                parent_snapshot.plan_context_ordinal_at_admission
            ),
            inherited_from_turn_id=parent_turn_id,
        )

    @staticmethod
    def _workspace_id(connection: Connection, session_id: str) -> str:
        row = connection.execute(
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id = %s",
            (session_id,),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("session is absent")
        return str(row["workspace_id"])

    @staticmethod
    def _require_provider_safe_turn_in_transaction(
        connection: Connection,
        *,
        session_id: str,
        turn_id: str,
        lock: bool,
    ) -> Mapping[str, object]:
        lock_clause = "FOR UPDATE OF t" if lock else ""
        row = connection.execute(
            f"""
            SELECT t.*
            FROM pulsara_v3.turns AS t
            WHERE t.session_id = %s AND t.id = %s AND t.status = 'RUNNING'
              AND NOT EXISTS (
                SELECT 1
                FROM pulsara_v3.assistant_message_blocks AS b
                LEFT JOIN pulsara_v3.tool_results AS r
                  ON r.session_id = b.session_id
                 AND r.tool_call_entry_id = b.assistant_entry_id
                 AND r.tool_call_id = b.tool_call_id
                WHERE b.session_id = t.session_id
                  AND b.block_kind = 'TOOL_CALL'
                  AND r.id IS NULL
                  AND EXISTS (
                    SELECT 1 FROM pulsara_v3.transcript_entries AS e
                    WHERE e.session_id = b.session_id
                      AND e.id = b.assistant_entry_id
                      AND e.turn_id = t.id
                  )
              )
            {lock_clause}
            """,
            (session_id, turn_id),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("turn is not at a provider safe point")
        return row

    @staticmethod
    def _allocate_entry_sequence(connection: Connection, session_id: str) -> int:
        row = connection.execute(
            """
            UPDATE pulsara_v3.sessions
            SET latest_entry_sequence = latest_entry_sequence + 1,
                updated_at = clock_timestamp()
            WHERE id = %s
            RETURNING latest_entry_sequence
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("session is absent")
        return int(row["latest_entry_sequence"])

    @staticmethod
    def _allocate_event_range(
        connection: Connection, session_id: str, count: int
    ) -> int:
        if count < 1:
            raise ValueError("event allocation count must be positive")
        row = connection.execute(
            """
            UPDATE pulsara_v3.sessions
            SET latest_event_sequence = latest_event_sequence + %s,
                updated_at = clock_timestamp()
            WHERE id = %s
            RETURNING latest_event_sequence
            """,
            (count, session_id),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("session is absent")
        return int(row["latest_event_sequence"]) - count + 1

    def _append_events(
        self,
        connection: Connection,
        guard: HostWriterGuard | JobAttemptClaimGuard,
        *,
        workspace_id: str,
        drafts: Sequence[CommittedEventDraft],
    ) -> tuple[StoredCommittedEvent, ...]:
        if not drafts:
            return ()
        if isinstance(guard, HostWriterGuard):
            self._require_writer(connection, guard, lock=False)
            session_id = guard.session_id
            guard_kind = AppendGuardKind.HOST_WRITER
        else:
            self._require_job_claim(connection, guard, lock=False)
            if guard.origin_session_id is None:
                raise ValueError("global job cannot append a session occurrence")
            session_id = guard.origin_session_id
            guard_kind = AppendGuardKind.JOB_ATTEMPT_CLAIM
        for draft in drafts:
            descriptor = DESCRIPTOR_BY_TYPE[draft.event_type]
            if draft.subject.slot is not descriptor.subject_slot:
                raise ValueError("event subject slot does not match descriptor")
            if guard_kind not in descriptor.append_guards:
                raise ValueError("append guard is not permitted for event type")
        start = self._allocate_event_range(connection, session_id, len(drafts))
        events = tuple(
            self._insert_event(
                connection,
                workspace_id=workspace_id,
                session_id=session_id,
                sequence=start + offset,
                draft=draft,
                turn_id=self._resolve_event_turn_id(connection, draft.subject),
            )
            for offset, draft in enumerate(drafts)
        )
        self._record_event_batch(events)
        return events

    @staticmethod
    def _insert_event(
        connection: Connection,
        *,
        workspace_id: str,
        session_id: str,
        sequence: int,
        draft: CommittedEventDraft,
        turn_id: str | None,
    ) -> StoredCommittedEvent:
        slots = {slot.value: None for slot in SubjectSlot}
        slots[draft.subject.slot.value] = draft.subject.subject_id
        ordered_slots = tuple(slots[slot.value] for slot in SubjectSlot)
        subagent_child_kind = None
        if draft.subject.slot is SubjectSlot.SUBAGENT_MESSAGE:
            subagent_child_kind = "MESSAGE"
        elif draft.subject.slot is SubjectSlot.SUBAGENT_RESULT:
            subagent_child_kind = "RESULT"
        row = connection.execute(
            """
            INSERT INTO pulsara_v3.agent_events (
                event_id, workspace_id, session_id, event_sequence,
                namespace, event_type, schema_major, schema_minor,
                occurred_at, actor_kind, actor_id, sensitivity_class,
                projection_profile, payload,
                subject_turn_id, subject_entry_id, subject_tool_attempt_id,
                subject_job_id, subject_job_attempt_id, subject_queue_item_id,
                subject_interaction_decision_id,
                subject_context_binding_revision_id,
                subject_subagent_task_id, subject_subagent_message_id,
                subject_subagent_result_id, subject_subagent_child_kind,
                subject_plan_workflow_id, subject_plan_interaction_id
            ) VALUES (
                %s, %s, %s, %s, 'pulsara.core', %s, 1, 0,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s
            )
            RETURNING accepted_at
            """,
            (
                draft.event_id,
                workspace_id,
                session_id,
                sequence,
                draft.event_type.value,
                draft.occurred_at,
                draft.actor_kind,
                draft.actor_id,
                draft.sensitivity_class,
                draft.projection_profile,
                Jsonb(dict(draft.payload)),
                *ordered_slots[:11],
                subagent_child_kind,
                *ordered_slots[11:],
            ),
        ).fetchone()
        assert row is not None
        return StoredCommittedEvent(
            event_id=draft.event_id,
            workspace_id=workspace_id,
            session_id=session_id,
            event_sequence=sequence,
            event_type=draft.event_type,
            subject=draft.subject,
            accepted_at=row["accepted_at"],
            occurred_at=draft.occurred_at,
            actor_kind=draft.actor_kind,
            actor_id=draft.actor_id,
            sensitivity_class=draft.sensitivity_class,
            projection_profile=draft.projection_profile,
            payload=draft.payload,
            turn_id=turn_id,
        )

    @staticmethod
    def _resolve_event_turn_id(
        connection: Connection, subject: CommittedEventSubject
    ) -> str | None:
        slot = subject.slot
        identity = subject.subject_id
        if slot is SubjectSlot.TURN:
            return identity
        query: str | None = None
        if slot is SubjectSlot.ENTRY:
            query = "SELECT turn_id FROM pulsara_v3.transcript_entries WHERE id = %s"
        elif slot is SubjectSlot.TOOL_ATTEMPT:
            query = """
                SELECT e.turn_id
                FROM pulsara_v3.tool_execution_attempts AS a
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = a.session_id AND e.id = a.assistant_entry_id
                WHERE a.id = %s
            """
        elif slot is SubjectSlot.QUEUE_ITEM:
            query = """
                SELECT coalesce(q.target_turn_id, e.turn_id) AS turn_id
                FROM pulsara_v3.prompt_queue_items AS q
                LEFT JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = q.session_id AND e.id = q.consumed_entry_id
                WHERE q.id = %s
            """
        elif slot is SubjectSlot.INTERACTION_DECISION:
            query = """
                SELECT coalesce(d.subject_turn_id, e.turn_id) AS turn_id
                FROM pulsara_v3.interaction_decisions AS d
                LEFT JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = d.session_id
                 AND e.id = d.subject_tool_call_entry_id
                WHERE d.id = %s
            """
        elif slot is SubjectSlot.CONTEXT_BINDING_REVISION:
            query = """
                SELECT turn_id FROM pulsara_v3.turn_context_binding_revisions
                WHERE id = %s
            """
        elif slot is SubjectSlot.SUBAGENT_TASK:
            query = "SELECT parent_turn_id AS turn_id FROM pulsara_v3.subagent_tasks WHERE id = %s"
        elif slot in {SubjectSlot.SUBAGENT_MESSAGE, SubjectSlot.SUBAGENT_RESULT}:
            query = """
                SELECT e.turn_id
                FROM pulsara_v3.subagent_task_children AS c
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = c.session_id AND e.id = c.entry_id
                WHERE c.id = %s
            """
        elif slot is SubjectSlot.PLAN_WORKFLOW:
            query = """
                SELECT entry_turn_id AS turn_id
                FROM pulsara_v3.plan_workflows WHERE id = %s
            """
        elif slot is SubjectSlot.PLAN_INTERACTION:
            query = """
                SELECT origin_turn_id AS turn_id
                FROM pulsara_v3.plan_interactions WHERE id = %s
            """
        if query is None:
            return None
        row = connection.execute(query, (identity,)).fetchone()
        if row is None or row["turn_id"] is None:
            return None
        return str(row["turn_id"])

    @staticmethod
    def _event(
        event_type: CommittedEventType,
        slot: SubjectSlot,
        subject_id: str,
        *,
        occurred_at: datetime,
        actor_kind: str,
        actor_id: str,
        payload: Mapping[str, object],
    ) -> CommittedEventDraft:
        return CommittedEventDraft(
            event_id=_id("event"),
            event_type=event_type,
            subject=CommittedEventSubject(slot=slot, subject_id=subject_id),
            actor_kind=actor_kind,
            actor_id=actor_id,
            sensitivity_class="PUBLIC",
            projection_profile="DEFAULT",
            occurred_at=occurred_at,
            payload=payload,
        )

    @staticmethod
    def _insert_entry(
        connection: Connection,
        *,
        session_id: str,
        workspace_id: str,
        turn_id: str,
        entry_id: str,
        entry_sequence: int,
        entry_kind: EntryKind,
        scope_kind: ConversationScopeKind,
        scope_task_id: str | None,
        content: CanonicalContent,
        context_binding_revision_id: str | None = None,
        provider_input_through_sequence: int | None = None,
        source_job_id: str | None = None,
        source_subagent_result_id: str | None = None,
        source_plan_workflow_id: str | None = None,
        source_plan_interaction_id: str | None = None,
        source_plan_handoff_kind: PlanHandoffKind | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO pulsara_v3.transcript_entries (
                id, session_id, workspace_id, turn_id, entry_sequence,
                entry_kind, conversation_scope_kind, scope_subagent_task_id,
                context_binding_revision_id, provider_input_through_sequence,
                source_job_id, source_subagent_result_id,
                source_plan_workflow_id, source_plan_interaction_id,
                source_plan_handoff_kind,
                inline_content, blob_id, content_digest, content_size,
                content_media_type, content_codec
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                entry_id,
                session_id,
                workspace_id,
                turn_id,
                entry_sequence,
                entry_kind.value,
                scope_kind.value,
                scope_task_id,
                context_binding_revision_id,
                provider_input_through_sequence,
                source_job_id,
                source_subagent_result_id,
                source_plan_workflow_id,
                source_plan_interaction_id,
                (
                    None
                    if source_plan_handoff_kind is None
                    else source_plan_handoff_kind.value
                ),
                *_content_columns(content),
            ),
        )

    @staticmethod
    def _insert_assistant_block(
        connection: Connection,
        *,
        session_id: str,
        workspace_id: str,
        entry_id: str,
        ordinal: int,
        block: AssistantBlock,
    ) -> None:
        if isinstance(block, AssistantToolCallBlock):
            tool_arguments = thaw_json(block.arguments)
            if not isinstance(tool_arguments, dict):
                raise TypeError("assistant tool-call arguments must thaw as an object")
            connection.execute(
                """
                INSERT INTO pulsara_v3.assistant_message_blocks (
                    id, session_id, workspace_id, assistant_entry_id, block_ordinal,
                    block_kind, tool_call_id, tool_name, tool_arguments
                ) VALUES (%s, %s, %s, %s, %s, 'TOOL_CALL', %s, %s, %s)
                """,
                (
                    block.block_id,
                    session_id,
                    workspace_id,
                    entry_id,
                    ordinal,
                    block.tool_call_id,
                    block.tool_name,
                    Jsonb(tool_arguments),
                ),
            )
            return
        kind = (
            AssistantBlockKind.TEXT
            if isinstance(block, AssistantTextBlock)
            else AssistantBlockKind.DATA
        )
        content = block.text if isinstance(block, AssistantTextBlock) else block.data
        connection.execute(
            """
            INSERT INTO pulsara_v3.assistant_message_blocks (
                id, session_id, workspace_id, assistant_entry_id,
                block_ordinal, block_kind,
                inline_content, blob_id, content_digest, content_size,
                content_media_type, content_codec
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                block.block_id,
                session_id,
                workspace_id,
                entry_id,
                ordinal,
                kind.value,
                *_content_columns(content),
            ),
        )

    @staticmethod
    def _accepted_entry(
        connection: Connection, session_id: str, entry_id: str
    ) -> AcceptedEntry:
        row = connection.execute(
            """
            SELECT e.id, e.turn_id, e.entry_sequence, a.event_sequence
            FROM pulsara_v3.transcript_entries AS e
            JOIN pulsara_v3.agent_events AS a
              ON a.session_id = e.session_id
             AND a.subject_entry_id = e.id
            WHERE e.session_id = %s AND e.id = %s
            """,
            (session_id, entry_id),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("accepted command target is absent")
        return AcceptedEntry(
            entry_id=str(row["id"]),
            turn_id=str(row["turn_id"]),
            entry_sequence=int(row["entry_sequence"]),
            event_sequence=int(row["event_sequence"]),
        )
