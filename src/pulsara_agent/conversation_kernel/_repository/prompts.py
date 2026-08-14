"""Prompt ingress, queue and steer operations."""

from __future__ import annotations

from datetime import datetime
from psycopg import Connection, IsolationLevel
from psycopg.rows import dict_row
from pulsara_agent.conversation_kernel.contracts import CanonicalContent, ConversationScopeKind, EntryKind, HostWriterGuard, PromptDeliveryMode, TurnStatus, canonical_digest
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import RunPermissionAdmissionSource, RunPermissionOverlay
from pulsara_agent.primitives.plan_workflow import PlanHandoffKind
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType, SubjectSlot
from pulsara_agent.conversation_kernel.steer import AcceptedSteerDispatchEntry, MAXIMUM_STEER_ITEMS_PER_SAFE_POINT, PendingPromptSteerFact, PreparedPromptIngressCommand, PreparedSteerConsumptionCandidate, PreparedSteerPlanConflictInterruption, PreparedSteerResourceRejection, PromptIngressConfirmation, PromptIngressConfirmationKind, PromptIngressWriteRejection, SteerConsumptionConfirmation, SteerConsumptionConfirmationKind, SteerPlanConflictConfirmation, SteerPlanConflictConfirmationKind, SteerResourceRejectionConfirmation, SteerResourceRejectionConfirmationKind, build_pending_prompt_steer_fact
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .contracts import (
    AcceptedEntry,
    ConversationKernelConflict,
    PromptIngressRejected,
    _content_columns,
)

from .matching import (
    _accepted_steer_entry_matches,
    _event_row_matches_draft,
    _prompt_steer_row_matches_candidate,
    _prompt_steer_row_matches_resource_rejection,
)

class _PromptOperations:
    def interrupt_prepared_steer_plan_conflict(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedSteerPlanConflictInterruption,
        deadline_monotonic: float,
    ) -> None:
        """Install one stable post-consumption interruption winner."""

        if candidate.session_id != guard.session_id:
            raise ValueError("steer plan-conflict candidate belongs to another session")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            turn = connection.execute(
                """
                SELECT workspace_id, status FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s FOR UPDATE
                """,
                (guard.session_id, candidate.exact_target_turn_id),
            ).fetchone()
            if turn is None:
                raise ConversationKernelConflict("steer target turn is absent")
            if str(turn["status"]) != TurnStatus.RUNNING.value:
                return
            connection.execute(
                """
                UPDATE pulsara_v3.plan_interactions
                SET status = 'ABORTED', aborted_at = clock_timestamp()
                WHERE session_id = %s AND origin_turn_id = %s
                  AND status IN ('OPEN', 'CLAIMED')
                """,
                (guard.session_id, candidate.exact_target_turn_id),
            )
            updated = connection.execute(
                """
                UPDATE pulsara_v3.turns
                SET status = 'INTERRUPTED',
                    terminal_reason = 'PROVIDER_INPUT_PLAN_CONFLICT',
                    terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'RUNNING'
                RETURNING id
                """,
                (guard.session_id, candidate.exact_target_turn_id),
            ).fetchone()
            if updated is None:
                raise ConversationKernelConflict(
                    "steer plan-conflict turn CAS was lost"
                )
            self._append_events(
                connection,
                guard,
                workspace_id=str(turn["workspace_id"]),
                drafts=(candidate.turn_interrupted_occurrence,),
            )
            self._reject_terminal_prompt_steer_heads(
                connection,
                guard,
                occurred_at=candidate.occurred_at,
                actor_id=candidate.actor_id,
            )

    def confirm_prepared_steer_plan_conflict(
        self,
        *,
        candidate: PreparedSteerPlanConflictInterruption,
        deadline_monotonic: float,
    ) -> SteerPlanConflictConfirmation:
        """Classify the exact stable interruption or a historical terminal turn."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            turn = connection.execute(
                """SELECT status, terminal_reason FROM pulsara_v3.turns
                   WHERE session_id = %s AND id = %s""",
                (candidate.session_id, candidate.exact_target_turn_id),
            ).fetchone()
            event = connection.execute(
                """SELECT event_id, event_sequence, event_type, occurred_at,
                          actor_kind, actor_id, sensitivity_class,
                          projection_profile, payload, subject_turn_id
                   FROM pulsara_v3.agent_events
                   WHERE session_id = %s AND event_id = %s""",
                (
                    candidate.session_id,
                    candidate.turn_interrupted_occurrence.event_id,
                ),
            ).fetchone()
            open_interaction = connection.execute(
                """SELECT 1 FROM pulsara_v3.plan_interactions
                   WHERE session_id = %s AND origin_turn_id = %s
                     AND status IN ('OPEN', 'CLAIMED') LIMIT 1""",
                (candidate.session_id, candidate.exact_target_turn_id),
            ).fetchone()
        if turn is None:
            return SteerPlanConflictConfirmation(
                SteerPlanConflictConfirmationKind.CONFLICT
            )
        status = TurnStatus(str(turn["status"]))
        if (
            status is TurnStatus.INTERRUPTED
            and str(turn["terminal_reason"]) == "PROVIDER_INPUT_PLAN_CONFLICT"
            and open_interaction is None
            and _event_row_matches_draft(event, candidate.turn_interrupted_occurrence)
        ):
            return SteerPlanConflictConfirmation(SteerPlanConflictConfirmationKind.FULL)
        if status is not TurnStatus.RUNNING:
            return SteerPlanConflictConfirmation(
                SteerPlanConflictConfirmationKind.HISTORICAL_TERMINAL
            )
        if event is None:
            return SteerPlanConflictConfirmation(SteerPlanConflictConfirmationKind.NONE)
        return SteerPlanConflictConfirmation(SteerPlanConflictConfirmationKind.CONFLICT)

    def confirm_prompt_ingress(
        self,
        *,
        candidate: PreparedPromptIngressCommand,
        deadline_monotonic: float,
    ) -> PromptIngressConfirmation:
        """Query one stable prompt command without binding a writer generation."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            command = connection.execute(
                """
                SELECT command_kind, request_schema_version, semantic_digest,
                       target_queue_item_id
                FROM pulsara_v3.session_commands
                WHERE session_id = %s AND command_id = %s
                """,
                (candidate.session_id, candidate.command_id),
            ).fetchone()
            queue = connection.execute(
                """
                SELECT queue_sequence, command_id, client_submission_id,
                       delivery_mode, target_turn_id, permission_snapshot_id,
                       requested_permission_mode, status,
                       inline_content, blob_id, content_digest, content_size,
                       content_media_type, content_codec
                FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND id = %s
                """,
                (candidate.session_id, candidate.queue_item_id),
            ).fetchone()
        if command is None and queue is None:
            return PromptIngressConfirmation(PromptIngressConfirmationKind.NONE)
        compatible = (
            command is not None
            and queue is not None
            and str(command["command_kind"]) == "QUEUE_PROMPT"
            and str(command["request_schema_version"]) == "queue_prompt.v1"
            and str(command["semantic_digest"]) == candidate.semantic_digest
            and str(command["target_queue_item_id"]) == candidate.queue_item_id
            and str(queue["command_id"]) == candidate.command_id
            and str(queue["client_submission_id"]) == candidate.client_submission_id
            and str(queue["delivery_mode"]) == candidate.delivery_mode.value
            and (
                None
                if queue["target_turn_id"] is None
                else str(queue["target_turn_id"])
            )
            == candidate.target_turn_id
            and (
                None
                if queue["permission_snapshot_id"] is None
                else str(queue["permission_snapshot_id"])
            )
            == candidate.permission_snapshot_id
            and (
                None
                if queue["requested_permission_mode"] is None
                else str(queue["requested_permission_mode"])
            )
            == (
                None
                if candidate.requested_permission_mode is None
                else candidate.requested_permission_mode.value
            )
            and str(queue["content_digest"]) == candidate.content_digest
            and int(queue["content_size"]) == candidate.content_size
            and str(queue["content_media_type"]) == "text/plain"
            and str(queue["content_codec"]) == "utf-8"
            and ((queue["inline_content"] is None) != (queue["blob_id"] is None))
        )
        if compatible:
            return PromptIngressConfirmation(
                PromptIngressConfirmationKind.FULL_COMPATIBLE,
                queue_sequence=int(queue["queue_sequence"]),
                status=str(queue["status"]),
            )
        return PromptIngressConfirmation(
            PromptIngressConfirmationKind.CONFLICT,
            rejection=PromptIngressWriteRejection.COMMAND_CONFLICT,
        )

    def enqueue_prompt(
        self,
        guard: HostWriterGuard,
        *,
        command_id: str,
        queue_item_id: str,
        client_submission_id: str,
        delivery_mode: PromptDeliveryMode,
        target_turn_id: str | None,
        permission_snapshot_id: str | None,
        requested_permission_mode: PermissionMode | None,
        content: CanonicalContent,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> int:
        if (delivery_mode is PromptDeliveryMode.NEW_TURN) != (target_turn_id is None):
            raise ValueError("prompt delivery target union is invalid")
        if (delivery_mode is PromptDeliveryMode.NEW_TURN) != (
            permission_snapshot_id is not None and requested_permission_mode is not None
        ):
            raise ValueError("queued new-turn permission candidate is invalid")
        digest = canonical_digest(
            "pulsara:queue-prompt-command:v1",
            {
                "queue_item_id": queue_item_id,
                "client_submission_id": client_submission_id,
                "delivery_mode": delivery_mode.value,
                "target_turn_id": target_turn_id,
                "content_digest": content.digest,
                "permission_snapshot_id": permission_snapshot_id,
                "requested_permission_mode": (
                    None
                    if requested_permission_mode is None
                    else requested_permission_mode.value
                ),
            },
        )
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            existing = connection.execute(
                """
                SELECT command_kind, request_schema_version, semantic_digest,
                       target_queue_item_id
                FROM pulsara_v3.session_commands
                WHERE session_id = %s AND command_id = %s
                """,
                (guard.session_id, command_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["command_kind"] != "QUEUE_PROMPT"
                    or existing["request_schema_version"] != "queue_prompt.v1"
                    or existing["semantic_digest"] != digest
                    or existing["target_queue_item_id"] != queue_item_id
                ):
                    raise PromptIngressRejected(
                        PromptIngressWriteRejection.COMMAND_CONFLICT
                    )
                row = connection.execute(
                    """
                    SELECT * FROM pulsara_v3.prompt_queue_items
                    WHERE session_id = %s AND id = %s
                    """,
                    (guard.session_id, queue_item_id),
                ).fetchone()
                if (
                    row is None
                    or str(row["command_id"]) != command_id
                    or str(row["client_submission_id"]) != client_submission_id
                    or str(row["delivery_mode"]) != delivery_mode.value
                    or (
                        None
                        if row["target_turn_id"] is None
                        else str(row["target_turn_id"])
                    )
                    != target_turn_id
                    or (
                        None
                        if row["permission_snapshot_id"] is None
                        else str(row["permission_snapshot_id"])
                    )
                    != permission_snapshot_id
                    or (
                        None
                        if row["requested_permission_mode"] is None
                        else str(row["requested_permission_mode"])
                    )
                    != (
                        None
                        if requested_permission_mode is None
                        else requested_permission_mode.value
                    )
                    or self._content_from_row(row) != content
                ):
                    raise PromptIngressRejected(
                        PromptIngressWriteRejection.COMMAND_CONFLICT
                    )
                return int(row["queue_sequence"])
            if target_turn_id is not None:
                target = connection.execute(
                    """
                    SELECT conversation_scope_kind, status
                    FROM pulsara_v3.turns
                    WHERE session_id = %s AND id = %s
                    FOR UPDATE
                    """,
                    (guard.session_id, target_turn_id),
                ).fetchone()
                if target is None or target["conversation_scope_kind"] != "ROOT":
                    raise PromptIngressRejected(
                        PromptIngressWriteRejection.TARGET_STALE_OR_NON_STEERABLE
                    )
                if target["status"] != "RUNNING":
                    raise PromptIngressRejected(
                        PromptIngressWriteRejection.TARGET_STALE_OR_NON_STEERABLE
                    )
            pending = connection.execute(
                """SELECT count(*) AS total
                   FROM pulsara_v3.prompt_queue_items
                   WHERE session_id = %s AND status = 'PENDING'""",
                (guard.session_id,),
            ).fetchone()
            if int(pending["total"]) >= STAGE2_LIMITS.pending_prompt_hard_items:
                raise PromptIngressRejected(
                    PromptIngressWriteRejection.CAPACITY_EXHAUSTED
                )
            row = connection.execute(
                """
                UPDATE pulsara_v3.sessions
                SET latest_prompt_queue_sequence = latest_prompt_queue_sequence + 1,
                    updated_at = clock_timestamp()
                WHERE id = %s
                RETURNING workspace_id, latest_prompt_queue_sequence
                """,
                (guard.session_id,),
            ).fetchone()
            assert row is not None
            queue_sequence = int(row["latest_prompt_queue_sequence"])
            permission = (
                None
                if requested_permission_mode is None
                else self._freeze_root_permission_snapshot(
                    connection,
                    session_id=guard.session_id,
                    snapshot_id=str(permission_snapshot_id),
                    requested_mode=requested_permission_mode,
                    admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
                )
            )
            handoff = (
                None
                if delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN
                else self._eligible_plan_handoff(
                    connection, session_id=guard.session_id
                )
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_queue_item_id
                ) VALUES (%s, %s, 'QUEUE_PROMPT', 'queue_prompt.v1',
                          %s, 'QUEUE_ITEM', %s)
                """,
                (guard.session_id, command_id, digest, queue_item_id),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.prompt_queue_items (
                    id, session_id, workspace_id, queue_sequence,
                    command_id, client_submission_id, delivery_mode,
                    target_turn_id, status, inline_content, blob_id,
                    content_digest, content_size, content_media_type,
                    content_codec, permission_snapshot_id,
                    requested_permission_mode, effective_permission_mode,
                    permission_admission_source, permission_overlay,
                    permission_plan_context_ordinal,
                    permission_plan_workflow_id,
                    permission_plan_revision_at_admission,
                    permission_inherited_from_turn_id,
                    permission_contract_id,
                    permission_contract_fingerprint,
                    permission_snapshot_fingerprint,
                    pending_plan_handoff_workflow_id,
                    pending_plan_handoff_interaction_id,
                    pending_plan_handoff_kind
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDING',
                          %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s)
                """,
                (
                    queue_item_id,
                    guard.session_id,
                    row["workspace_id"],
                    queue_sequence,
                    command_id,
                    client_submission_id,
                    delivery_mode.value,
                    target_turn_id,
                    *_content_columns(content),
                    *(
                        (None,) * 12
                        if permission is None
                        else self._permission_columns(permission)
                    ),
                    None if handoff is None else handoff.workflow_id,
                    None if handoff is None else handoff.interaction_id,
                    None if handoff is None else handoff.kind.value,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=str(row["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.PROMPT_QUEUED,
                        SubjectSlot.QUEUE_ITEM,
                        queue_item_id,
                        occurred_at=occurred_at,
                        actor_kind="human",
                        actor_id=actor_id,
                        payload={
                            "queue_sequence": queue_sequence,
                            "delivery_mode": delivery_mode.value,
                        },
                    ),
                ),
            )
            return queue_sequence

    def consume_prompt_head(
        self,
        guard: HostWriterGuard,
        *,
        new_turn_id: str,
        new_entry_id: str,
        new_context_binding_revision_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            self._reject_terminal_prompt_steer_heads(
                connection,
                guard,
                occurred_at=occurred_at,
                actor_id=actor_id,
            )
            active_root = connection.execute(
                """
                SELECT id FROM pulsara_v3.turns
                WHERE session_id = %s
                  AND conversation_scope_kind = 'ROOT'
                  AND status = 'RUNNING'
                LIMIT 1 FOR UPDATE
                """,
                (guard.session_id,),
            ).fetchone()
            if active_root is not None:
                # NEW_TURN is an independent delivery lane, but it is still a
                # future turn.  It may not overtake the physical ownership of
                # the current ROOT turn merely because the steer lane was
                # drained separately.
                return None
            item = connection.execute(
                """
                SELECT * FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND status = 'PENDING'
                  AND delivery_mode = 'NEW_TURN'
                ORDER BY queue_sequence, id
                LIMIT 1
                FOR UPDATE
                """,
                (guard.session_id,),
            ).fetchone()
            if item is None:
                return None
            if self._open_plan_interaction(connection, guard.session_id) is not None:
                # A QUESTION keeps its existing turn alive; a DRAFT_REVIEW
                # must be explicitly resolved.  The FIFO head remains level
                # truth and is not overtaken or terminalized here.
                return None
            permission = self._permission_from_row(item)
            latest_plan = connection.execute(
                """
                SELECT id, workflow_ordinal, status
                FROM pulsara_v3.plan_workflows
                WHERE session_id = %s
                ORDER BY workflow_ordinal DESC LIMIT 1
                """,
                (guard.session_id,),
            ).fetchone()
            latest_ordinal = (
                0 if latest_plan is None else int(latest_plan["workflow_ordinal"])
            )
            active_id = (
                None
                if latest_plan is None or str(latest_plan["status"]) != "ACTIVE"
                else str(latest_plan["id"])
            )
            incompatible = (
                latest_ordinal != permission.plan_context_ordinal_at_admission
                or (
                    permission.overlay is RunPermissionOverlay.NONE
                    and active_id is not None
                )
                or (
                    permission.overlay is RunPermissionOverlay.PLAN_READ_ONLY
                    and active_id != permission.plan_workflow_id
                )
            )
            if incompatible:
                connection.execute(
                    """
                    UPDATE pulsara_v3.prompt_queue_items
                    SET status = 'REJECTED',
                        terminal_reason = 'PLAN_CONTEXT_CHANGED_BEFORE_DELIVERY',
                        terminal_at = clock_timestamp()
                    WHERE session_id = %s AND id = %s AND status = 'PENDING'
                    """,
                    (guard.session_id, item["id"]),
                )
                self._append_events(
                    connection,
                    guard,
                    workspace_id=str(item["workspace_id"]),
                    drafts=(
                        self._event(
                            CommittedEventType.PROMPT_REJECTED,
                            SubjectSlot.QUEUE_ITEM,
                            str(item["id"]),
                            occurred_at=occurred_at,
                            actor_kind="runtime",
                            actor_id=actor_id,
                            payload={"reason": "PLAN_CONTEXT_CHANGED_BEFORE_DELIVERY"},
                        ),
                    ),
                )
                return None
            content = self._content_from_row(item)
            workspace_id = str(item["workspace_id"])
            turn_id = new_turn_id
            entry_kind = EntryKind.USER_MESSAGE
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            connection.execute(
                """
                INSERT INTO pulsara_v3.turns (
                    id, session_id, workspace_id, conversation_scope_kind,
                    status, initial_entry_id, current_context_binding_revision_id
                    , permission_snapshot_id, requested_permission_mode,
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
                    new_entry_id,
                    new_context_binding_revision_id,
                    *self._permission_columns(permission),
                ),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.turn_context_binding_revisions (
                    id, session_id, turn_id, revision_ordinal, base_kind,
                    source_through_sequence
                ) VALUES (%s, %s, %s, 0, 'FULL_HISTORY', %s)
                """,
                (
                    new_context_binding_revision_id,
                    guard.session_id,
                    turn_id,
                    entry_sequence - 1,
                ),
            )
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=workspace_id,
                turn_id=turn_id,
                entry_id=new_entry_id,
                entry_sequence=entry_sequence,
                entry_kind=entry_kind,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=content,
                source_plan_workflow_id=item["pending_plan_handoff_workflow_id"],
                source_plan_interaction_id=item["pending_plan_handoff_interaction_id"],
                source_plan_handoff_kind=(
                    None
                    if item["pending_plan_handoff_kind"] is None
                    else PlanHandoffKind(str(item["pending_plan_handoff_kind"]))
                ),
            )
            updated = connection.execute(
                """
                UPDATE pulsara_v3.prompt_queue_items
                SET status = 'CONSUMED', consumed_entry_id = %s,
                    terminal_reason = 'CONSUMED', terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'PENDING'
                RETURNING id
                """,
                (new_entry_id, guard.session_id, item["id"]),
            ).fetchone()
            if updated is None:
                raise ConversationKernelConflict("prompt queue terminal CAS lost")
            events = self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        CommittedEventType.PROMPT_CONSUMED,
                        SubjectSlot.QUEUE_ITEM,
                        str(item["id"]),
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"entry_id": new_entry_id},
                    ),
                    self._event(
                        CommittedEventType.USER_MESSAGE_ACCEPTED,
                        SubjectSlot.ENTRY,
                        new_entry_id,
                        occurred_at=occurred_at,
                        actor_kind="human",
                        actor_id=actor_id,
                        payload={"source": "PROMPT_QUEUE"},
                    ),
                ),
            )
            return AcceptedEntry(
                entry_id=new_entry_id,
                turn_id=turn_id,
                entry_sequence=entry_sequence,
                event_sequence=events[-1].event_sequence,
            )

    def _reject_terminal_prompt_steer_heads(
        self,
        connection: Connection,
        guard: HostWriterGuard,
        *,
        occurred_at: datetime,
        actor_id: str,
    ) -> None:
        """Reject the complete bounded prefix of terminal-target steers."""

        for _ in range(STAGE2_LIMITS.pending_prompt_hard_items):
            item = connection.execute(
                """
                SELECT * FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND status = 'PENDING'
                  AND delivery_mode = 'STEER_ACTIVE_TURN'
                ORDER BY queue_sequence, id
                LIMIT 1
                FOR UPDATE
                """,
                (guard.session_id,),
            ).fetchone()
            if (
                item is None
                or item["delivery_mode"] != PromptDeliveryMode.STEER_ACTIVE_TURN.value
            ):
                return
            target = connection.execute(
                """
                SELECT conversation_scope_kind, status
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s FOR UPDATE
                """,
                (guard.session_id, item["target_turn_id"]),
            ).fetchone()
            if (
                target is not None
                and target["conversation_scope_kind"] == "ROOT"
                and target["status"] == "RUNNING"
            ):
                return
            updated = connection.execute(
                """
                UPDATE pulsara_v3.prompt_queue_items
                SET status = 'REJECTED', terminal_reason = 'TARGET_TURN_TERMINAL',
                    terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'PENDING'
                RETURNING id
                """,
                (guard.session_id, item["id"]),
            ).fetchone()
            if updated is None:
                raise ConversationKernelConflict("stale steer rejection CAS lost")
            self._append_events(
                connection,
                guard,
                workspace_id=str(item["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.PROMPT_REJECTED,
                        SubjectSlot.QUEUE_ITEM,
                        str(item["id"]),
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"reason": "TARGET_TURN_TERMINAL"},
                    ),
                ),
            )

    def read_pending_prompt_steer_facts(
        self,
        *,
        session_id: str,
        target_turn_id: str,
        maximum_items: int = MAXIMUM_STEER_ITEMS_PER_SAFE_POINT,
        deadline_monotonic: float,
    ) -> tuple[PendingPromptSteerFact, ...]:
        """Read one bounded target-lane metadata cut without consuming rows."""

        if not 1 <= maximum_items <= MAXIMUM_STEER_ITEMS_PER_SAFE_POINT:
            raise ValueError("steer metadata read bound is invalid")
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, workspace_id, queue_sequence,
                       command_id, target_turn_id, inline_content, blob_id,
                       content_digest, content_size, content_media_type,
                       content_codec
                FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND status = 'PENDING'
                  AND delivery_mode = 'STEER_ACTIVE_TURN'
                  AND target_turn_id = %s
                ORDER BY queue_sequence, id
                LIMIT %s
                """,
                (session_id, target_turn_id, maximum_items + 1),
            ).fetchall()
        if len(rows) > maximum_items:
            # The caller quotes the first bounded prefix.  An additional row is
            # deliberately not hydrated and remains pending for the next cut.
            rows = rows[:maximum_items]
        return tuple(
            build_pending_prompt_steer_fact(
                session_id=str(row["session_id"]),
                workspace_id=str(row["workspace_id"]),
                queue_item_id=str(row["id"]),
                queue_sequence=int(row["queue_sequence"]),
                command_id=str(row["command_id"]),
                exact_target_turn_id=str(row["target_turn_id"]),
                content=self._content_from_row(row),
            )
            for row in rows
        )

    def consume_prepared_prompt_steer(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedSteerConsumptionCandidate,
        deadline_monotonic: float,
    ) -> AcceptedSteerDispatchEntry:
        """Consume one deterministic target-lane head in one canonical tx."""

        if candidate.session_id != guard.session_id:
            raise ValueError("steer candidate belongs to another session")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            item = connection.execute(
                """
                SELECT * FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND status = 'PENDING'
                  AND delivery_mode = 'STEER_ACTIVE_TURN'
                  AND target_turn_id = %s
                ORDER BY queue_sequence, id
                LIMIT 1 FOR UPDATE
                """,
                (guard.session_id, candidate.exact_target_turn_id),
            ).fetchone()
            if item is None or not _prompt_steer_row_matches_candidate(item, candidate):
                raise ConversationKernelConflict(
                    "prepared steer no longer owns the exact lane head"
                )
            target = connection.execute(
                """
                SELECT conversation_scope_kind, status, workspace_id,
                       current_context_binding_revision_id,
                       permission_snapshot_id, requested_permission_mode,
                       effective_permission_mode, permission_admission_source,
                       permission_overlay, permission_plan_context_ordinal,
                       permission_plan_workflow_id,
                       permission_plan_revision_at_admission,
                       permission_inherited_from_turn_id,
                       permission_contract_id,
                       permission_contract_fingerprint,
                       permission_snapshot_fingerprint
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s FOR UPDATE
                """,
                (guard.session_id, candidate.exact_target_turn_id),
            ).fetchone()
            if (
                target is None
                or target["conversation_scope_kind"] != "ROOT"
                or target["status"] != "RUNNING"
            ):
                raise ConversationKernelConflict(
                    "prepared steer target is no longer live"
                )
            fence = candidate.canonical_base_fence
            if (
                str(target["current_context_binding_revision_id"])
                != fence.context_binding_fact.binding_revision_id
                or self._permission_from_row(target)
                != fence.run_permission_snapshot
            ):
                raise ConversationKernelConflict(
                    "prepared steer canonical control base drifted"
                )
            expected_workflow = fence.plan_workflow_fact
            if expected_workflow is not None:
                workflow = connection.execute(
                    """
                    SELECT id, session_id, workspace_id, workflow_ordinal,
                           status, entered_by, resume_permission_mode,
                           permission_contract_id,
                           permission_contract_fingerprint, workflow_revision
                    FROM pulsara_v3.plan_workflows
                    WHERE session_id = %s AND id = %s
                    FOR UPDATE
                    """,
                    (guard.session_id, expected_workflow.workflow_id),
                ).fetchone()
                if workflow is None or (
                    str(workflow["id"]) != expected_workflow.workflow_id
                    or str(workflow["session_id"]) != expected_workflow.session_id
                    or str(workflow["workspace_id"])
                    != expected_workflow.workspace_id
                    or int(workflow["workflow_ordinal"])
                    != expected_workflow.workflow_ordinal
                    or str(workflow["status"])
                    != expected_workflow.workflow_status.value
                    or str(workflow["entered_by"])
                    != expected_workflow.entered_by.value
                    or str(workflow["resume_permission_mode"])
                    != expected_workflow.resume_permission_mode.value
                    or str(workflow["permission_contract_id"])
                    != expected_workflow.permission_contract_id
                    or str(workflow["permission_contract_fingerprint"])
                    != expected_workflow.permission_contract_fingerprint
                    or int(workflow["workflow_revision"])
                    != expected_workflow.current_workflow_revision
                ):
                    raise ConversationKernelConflict(
                        "prepared steer Plan control base drifted"
                    )
            allocator = connection.execute(
                """SELECT latest_entry_sequence FROM pulsara_v3.sessions
                   WHERE id = %s""",
                (guard.session_id,),
            ).fetchone()
            if allocator is None or int(allocator["latest_entry_sequence"]) + 1 != (
                candidate.expected_entry_sequence
            ):
                raise ConversationKernelConflict(
                    "prepared steer canonical base sequence drifted"
                )
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            if entry_sequence != candidate.expected_entry_sequence:
                raise ConversationKernelConflict(
                    "prepared steer entry sequence drifted"
                )
            workspace_id = str(item["workspace_id"])
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=workspace_id,
                turn_id=candidate.exact_target_turn_id,
                entry_id=candidate.new_entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.USER_STEER,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=candidate.content,
            )
            updated = connection.execute(
                """
                UPDATE pulsara_v3.prompt_queue_items
                SET status = 'CONSUMED', consumed_entry_id = %s,
                    terminal_reason = 'CONSUMED', terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'PENDING'
                RETURNING id
                """,
                (candidate.new_entry_id, guard.session_id, candidate.queue_item_id),
            ).fetchone()
            if updated is None:
                raise ConversationKernelConflict("prepared steer queue CAS lost")
            events = self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    candidate.prompt_consumed_occurrence,
                    candidate.user_steer_accepted_occurrence,
                ),
            )
            return AcceptedSteerDispatchEntry(
                queue_item_id=candidate.queue_item_id,
                queue_sequence=candidate.queue_sequence,
                entry_id=candidate.new_entry_id,
                entry_sequence=entry_sequence,
                target_turn_id=candidate.exact_target_turn_id,
                content_digest=candidate.content.digest,
                content_size=candidate.content.size,
                prompt_consumed_event_id=events[0].event_id,
                prompt_consumed_event_sequence=events[0].event_sequence,
                user_steer_event_id=events[1].event_id,
                user_steer_event_sequence=events[1].event_sequence,
            )

    def confirm_prepared_prompt_steer(
        self,
        *,
        candidate: PreparedSteerConsumptionCandidate,
        deadline_monotonic: float,
    ) -> SteerConsumptionConfirmation:
        """Stateless FULL/NONE/CONFLICT confirmation for ACK-unknown."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            queue = connection.execute(
                """SELECT * FROM pulsara_v3.prompt_queue_items
                   WHERE session_id = %s AND id = %s""",
                (candidate.session_id, candidate.queue_item_id),
            ).fetchone()
            entry = connection.execute(
                """SELECT * FROM pulsara_v3.transcript_entries
                   WHERE session_id = %s AND id = %s""",
                (candidate.session_id, candidate.new_entry_id),
            ).fetchone()
            event_rows = connection.execute(
                """
                SELECT event_id, event_sequence, event_type, occurred_at,
                       actor_kind, actor_id, sensitivity_class,
                       projection_profile, payload,
                       subject_queue_item_id, subject_entry_id
                FROM pulsara_v3.agent_events
                WHERE session_id = %s AND event_id = ANY(%s)
                """,
                (
                    candidate.session_id,
                    [
                        candidate.prompt_consumed_occurrence.event_id,
                        candidate.user_steer_accepted_occurrence.event_id,
                    ],
                ),
            ).fetchall()
        events = {str(row["event_id"]): row for row in event_rows}
        if (
            queue is not None
            and str(queue["status"]) == "CONSUMED"
            and str(queue["consumed_entry_id"]) == candidate.new_entry_id
            and _prompt_steer_row_matches_candidate(queue, candidate)
            and _accepted_steer_entry_matches(entry, candidate)
            and _event_row_matches_draft(
                events.get(candidate.prompt_consumed_occurrence.event_id),
                candidate.prompt_consumed_occurrence,
            )
            and _event_row_matches_draft(
                events.get(candidate.user_steer_accepted_occurrence.event_id),
                candidate.user_steer_accepted_occurrence,
            )
        ):
            prompt_row = events[candidate.prompt_consumed_occurrence.event_id]
            steer_row = events[candidate.user_steer_accepted_occurrence.event_id]
            return SteerConsumptionConfirmation(
                SteerConsumptionConfirmationKind.FULL,
                AcceptedSteerDispatchEntry(
                    queue_item_id=candidate.queue_item_id,
                    queue_sequence=candidate.queue_sequence,
                    entry_id=candidate.new_entry_id,
                    entry_sequence=candidate.expected_entry_sequence,
                    target_turn_id=candidate.exact_target_turn_id,
                    content_digest=candidate.content.digest,
                    content_size=candidate.content.size,
                    prompt_consumed_event_id=candidate.prompt_consumed_occurrence.event_id,
                    prompt_consumed_event_sequence=int(prompt_row["event_sequence"]),
                    user_steer_event_id=candidate.user_steer_accepted_occurrence.event_id,
                    user_steer_event_sequence=int(steer_row["event_sequence"]),
                ),
            )
        if (
            queue is not None
            and str(queue["status"]) == "PENDING"
            and _prompt_steer_row_matches_candidate(queue, candidate)
            and entry is None
            and not events
        ):
            return SteerConsumptionConfirmation(SteerConsumptionConfirmationKind.NONE)
        return SteerConsumptionConfirmation(SteerConsumptionConfirmationKind.CONFLICT)

    def reject_prepared_prompt_steer_resource_exhaustion(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedSteerResourceRejection,
        deadline_monotonic: float,
    ) -> None:
        """Reject one unfit steer and interrupt its ROOT turn atomically."""

        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            item = connection.execute(
                """
                SELECT * FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND status = 'PENDING'
                  AND delivery_mode = 'STEER_ACTIVE_TURN'
                  AND target_turn_id = %s
                ORDER BY queue_sequence, id
                LIMIT 1 FOR UPDATE
                """,
                (guard.session_id, candidate.exact_target_turn_id),
            ).fetchone()
            if not _prompt_steer_row_matches_resource_rejection(item, candidate):
                raise ConversationKernelConflict(
                    "steer resource rejection no longer owns the lane head"
                )
            turn = connection.execute(
                """
                SELECT workspace_id, conversation_scope_kind, status
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s
                FOR UPDATE
                """,
                (guard.session_id, candidate.exact_target_turn_id),
            ).fetchone()
            if (
                turn is None
                or str(turn["conversation_scope_kind"]) != "ROOT"
                or str(turn["status"]) != "RUNNING"
            ):
                raise ConversationKernelConflict(
                    "steer resource rejection target is no longer live"
                )
            updated = connection.execute(
                """
                UPDATE pulsara_v3.prompt_queue_items
                SET status = 'REJECTED', terminal_reason = %s,
                    terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'PENDING'
                RETURNING id
                """,
                (candidate.reason, guard.session_id, candidate.queue_item_id),
            ).fetchone()
            if updated is None:
                raise ConversationKernelConflict("steer resource rejection CAS lost")
            connection.execute(
                """
                UPDATE pulsara_v3.plan_interactions
                SET status = 'ABORTED', aborted_at = clock_timestamp()
                WHERE session_id = %s AND origin_turn_id = %s
                  AND status IN ('OPEN', 'CLAIMED')
                """,
                (guard.session_id, candidate.exact_target_turn_id),
            )
            interrupted = connection.execute(
                """
                UPDATE pulsara_v3.turns
                SET status = 'INTERRUPTED',
                    terminal_reason = 'PROVIDER_INPUT_RESOURCE_EXHAUSTED',
                    terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'RUNNING'
                RETURNING id
                """,
                (guard.session_id, candidate.exact_target_turn_id),
            ).fetchone()
            if interrupted is None:
                raise ConversationKernelConflict(
                    "steer resource rejection turn CAS lost"
                )
            self._append_events(
                connection,
                guard,
                workspace_id=str(turn["workspace_id"]),
                drafts=(
                    candidate.prompt_rejected_occurrence,
                    candidate.turn_interrupted_occurrence,
                ),
            )
            self._reject_terminal_prompt_steer_heads(
                connection,
                guard,
                occurred_at=candidate.occurred_at,
                actor_id=candidate.actor_id,
            )

    def confirm_prepared_prompt_steer_resource_rejection(
        self,
        *,
        session_id: str,
        candidate: PreparedSteerResourceRejection,
        deadline_monotonic: float,
    ) -> SteerResourceRejectionConfirmation:
        """Confirm the exact queue/turn/event winner after an unknown ACK."""

        if session_id != candidate.session_id:
            raise ValueError("steer rejection belongs to another session")
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            queue = connection.execute(
                """SELECT * FROM pulsara_v3.prompt_queue_items
                   WHERE session_id = %s AND id = %s""",
                (session_id, candidate.queue_item_id),
            ).fetchone()
            turn = connection.execute(
                """SELECT status, terminal_reason
                   FROM pulsara_v3.turns WHERE session_id = %s AND id = %s""",
                (session_id, candidate.exact_target_turn_id),
            ).fetchone()
            events = connection.execute(
                """
                SELECT event_id, event_sequence, event_type, occurred_at,
                       actor_kind, actor_id, sensitivity_class,
                       projection_profile, payload,
                       subject_queue_item_id, subject_turn_id
                FROM pulsara_v3.agent_events
                WHERE session_id = %s AND event_id = ANY(%s)
                """,
                (
                    session_id,
                    [
                        candidate.prompt_rejected_occurrence.event_id,
                        candidate.turn_interrupted_occurrence.event_id,
                    ],
                ),
            ).fetchall()
            open_interactions = connection.execute(
                """
                SELECT 1 FROM pulsara_v3.plan_interactions
                WHERE session_id = %s AND origin_turn_id = %s
                  AND status IN ('OPEN', 'CLAIMED') LIMIT 1
                """,
                (session_id, candidate.exact_target_turn_id),
            ).fetchone()
        by_id = {str(row["event_id"]): row for row in events}
        if (
            _prompt_steer_row_matches_resource_rejection(queue, candidate)
            and str(queue["status"]) == "REJECTED"
            and str(queue["terminal_reason"]) == candidate.reason
            and turn is not None
            and str(turn["status"]) == "INTERRUPTED"
            and str(turn["terminal_reason"]) == "PROVIDER_INPUT_RESOURCE_EXHAUSTED"
            and open_interactions is None
            and _event_row_matches_draft(
                by_id.get(candidate.prompt_rejected_occurrence.event_id),
                candidate.prompt_rejected_occurrence,
            )
            and _event_row_matches_draft(
                by_id.get(candidate.turn_interrupted_occurrence.event_id),
                candidate.turn_interrupted_occurrence,
            )
        ):
            return SteerResourceRejectionConfirmation(
                SteerResourceRejectionConfirmationKind.FULL
            )
        if (
            _prompt_steer_row_matches_resource_rejection(queue, candidate)
            and str(queue["status"]) == "PENDING"
            and turn is not None
            and str(turn["status"]) == "RUNNING"
            and not events
        ):
            return SteerResourceRejectionConfirmation(
                SteerResourceRejectionConfirmationKind.NONE
            )
        return SteerResourceRejectionConfirmation(
            SteerResourceRejectionConfirmationKind.CONFLICT
        )

    def cancel_prompt(
        self,
        guard: HostWriterGuard,
        *,
        queue_item_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> str:
        """CAS one pending queue item to CANCELLED and return the winner."""

        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                SELECT workspace_id, status
                FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND id = %s
                FOR UPDATE
                """,
                (guard.session_id, queue_item_id),
            ).fetchone()
            if row is None:
                raise KeyError(queue_item_id)
            if row["status"] != "PENDING":
                return str(row["status"])
            connection.execute(
                """
                UPDATE pulsara_v3.prompt_queue_items
                SET status = 'CANCELLED', terminal_reason = 'USER_CANCELLED',
                    terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'PENDING'
                """,
                (guard.session_id, queue_item_id),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=str(row["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.PROMPT_CANCELLED,
                        SubjectSlot.QUEUE_ITEM,
                        queue_item_id,
                        occurred_at=occurred_at,
                        actor_kind="human",
                        actor_id=actor_id,
                        payload={"reason": "USER_CANCELLED"},
                    ),
                ),
            )
            return "CANCELLED"

    def has_pending_prompt(
        self,
        *,
        session_id: str,
        delivery_mode: PromptDeliveryMode | None = None,
        deadline_monotonic: float,
    ) -> bool:
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM pulsara_v3.prompt_queue_items
                    WHERE session_id = %s AND status = 'PENDING'
                      AND (%s::text IS NULL OR delivery_mode = %s::text)
                    LIMIT 1
                    """,
                    (
                        session_id,
                        None if delivery_mode is None else delivery_mode.value,
                        None if delivery_mode is None else delivery_mode.value,
                    ),
                ).fetchone()
                is not None
            )

    def pending_prompt_head_mode(
        self,
        *,
        session_id: str,
        deadline_monotonic: float,
    ) -> PromptDeliveryMode | None:
        """Return whether the future-NEW_TURN delivery lane has a head."""

        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT delivery_mode FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND status = 'PENDING'
                  AND delivery_mode = 'NEW_TURN'
                ORDER BY queue_sequence, id LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return (
                None if row is None else PromptDeliveryMode(str(row["delivery_mode"]))
            )
