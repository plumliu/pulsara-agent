"""Canonical ROOT delivery for terminal subagent completions."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from psycopg import Connection
from psycopg.types.json import Jsonb

from pulsara_agent.conversation_kernel.contracts import (
    CommittedEventDraft,
    CommittedEventSubject,
    ConversationScopeKind,
    EntryKind,
    HostWriterGuard,
    InlineContent,
    canonical_digest,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    SUBAGENT_COMPLETION_MEDIA_TYPE,
    SubagentTaskStatus,
    build_subagent_completion_storage_body,
)
from pulsara_agent.conversation_kernel.vocabulary import (
    CommittedEventType,
    SubjectSlot,
)
from pulsara_agent.model_input.contracts import PreparedProviderInputCut
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import RunPermissionAdmissionSource

from .contracts import (
    AcceptedEntry,
    AcceptedSubagentCompletion,
    ConversationKernelConflict,
    SubagentCompletionDisposition,
    _stable_identity,
)


class _SubagentCompletionOperations:
    def accept_subagent_completion_into_root(
        self,
        guard: HostWriterGuard,
        *,
        task_id: str,
        turn_id: str,
        new_context_binding_revision_id: str | None = None,
        requested_permission_mode: PermissionMode | None = None,
        command_id: str | None = None,
        expected_provider_input_cut: PreparedProviderInputCut | None = None,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedSubagentCompletion:
        """Deliver one terminal task to one exact ROOT turn.

        Automatic delivery supplies an exact provider-input cut and creates no
        session command. Manual delivery supplies a client command, which is
        bound to the canonical winner even if automatic delivery won first.
        """

        if not task_id or not turn_id:
            raise ValueError("subagent completion identity is empty")
        automatic = command_id is None
        if automatic:
            if (
                expected_provider_input_cut is None
                or new_context_binding_revision_id is not None
                or requested_permission_mode is not None
            ):
                raise ValueError("automatic completion requires one exact active cut")
        elif not command_id:
            raise ValueError("manual completion command identity is empty")

        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            task = self._completion_source_row(
                connection, session_id=guard.session_id, task_id=task_id
            )
            if task is None:
                return AcceptedSubagentCompletion(
                    SubagentCompletionDisposition.TARGET_STALE
                )

            semantic_digest: str | None = None
            if command_id is not None:
                semantic_digest = canonical_digest(
                    "pulsara:accept-subagent-completion:v1",
                    {
                        "task_id": task_id,
                        "turn_id": turn_id,
                        "new_context_binding_revision_id": (
                            new_context_binding_revision_id
                        ),
                        "requested_permission_mode": (
                            None
                            if requested_permission_mode is None
                            else requested_permission_mode.value
                        ),
                    },
                )
                existing_command = connection.execute(
                    """
                    SELECT c.command_kind, c.request_schema_version,
                           c.semantic_digest, c.target_entry_id,
                           e.turn_id, e.entry_sequence,
                           e.source_subagent_task_id,
                           a.event_sequence, a.payload
                    FROM pulsara_v3.session_commands AS c
                    LEFT JOIN pulsara_v3.transcript_entries AS e
                      ON e.entry_owner_kind = 'EXECUTED_TURN' AND e.session_id = c.session_id
                     AND e.id = c.target_entry_id
                    LEFT JOIN pulsara_v3.agent_events AS a
                      ON a.session_id = e.session_id
                     AND a.subject_entry_id = e.id
                     AND a.event_type = 'InterAgentMessageAccepted'
                    WHERE c.session_id = %s AND c.command_id = %s
                    """,
                    (guard.session_id, command_id),
                ).fetchone()
                if existing_command is not None:
                    if (
                        str(existing_command["command_kind"])
                        != "ACCEPT_SUBAGENT_COMPLETION"
                        or str(existing_command["request_schema_version"])
                        != "accept_subagent_completion.v1"
                        or str(existing_command["semantic_digest"])
                        != semantic_digest
                        or str(existing_command["source_subagent_task_id"])
                        != task_id
                        or existing_command["event_sequence"] is None
                    ):
                        raise ConversationKernelConflict(
                            "subagent completion command conflicts"
                        )
                    accepted = AcceptedEntry(
                        str(existing_command["target_entry_id"]),
                        str(existing_command["turn_id"]),
                        int(existing_command["entry_sequence"]),
                        int(existing_command["event_sequence"]),
                    )
                    payload = existing_command["payload"]
                    created_by_command = isinstance(payload, Mapping) and (
                        payload.get("manual_command_id") == command_id
                    )
                    return AcceptedSubagentCompletion(
                        (
                            SubagentCompletionDisposition.CREATED
                            if created_by_command
                            else SubagentCompletionDisposition.ALREADY_DELIVERED
                        ),
                        accepted,
                    )

            accepted = self._accepted_completion_row(
                connection, session_id=guard.session_id, task_id=task_id
            )
            if accepted is not None:
                accepted_entry = self._completion_accepted_entry(accepted)
                if command_id is not None:
                    assert semantic_digest is not None
                    self._bind_completion_command(
                        connection,
                        session_id=guard.session_id,
                        command_id=command_id,
                        semantic_digest=semantic_digest,
                        target_entry_id=accepted_entry.entry_id,
                    )
                return AcceptedSubagentCompletion(
                    SubagentCompletionDisposition.ALREADY_DELIVERED,
                    accepted_entry,
                )

            entry_id = _stable_identity(
                "entry", guard.session_id, task_id, turn_id, "subagent-completion"
            )
            sequence = self._prepare_completion_target(
                connection,
                guard,
                turn_id=turn_id,
                entry_id=entry_id,
                new_context_binding_revision_id=new_context_binding_revision_id,
                source_workspace_id=str(task["workspace_id"]),
                requested_permission_mode=requested_permission_mode,
                expected_provider_input_cut=expected_provider_input_cut,
                automatic=automatic,
            )
            if sequence is None:
                return AcceptedSubagentCompletion(
                    SubagentCompletionDisposition.TARGET_STALE
                )

            failed_dependency_task_ids = tuple(
                str(row["dependency_task_id"])
                for row in connection.execute(
                    """
                    SELECT edge.dependency_task_id
                    FROM pulsara_v3.subagent_task_dependencies AS edge
                    JOIN pulsara_v3.subagent_tasks AS dependency
                      ON dependency.session_id = edge.session_id
                     AND dependency.id = edge.dependency_task_id
                    LEFT JOIN pulsara_v3.subagent_task_children AS result
                      ON result.session_id = dependency.session_id
                     AND result.task_id = dependency.id
                     AND result.child_kind = 'RESULT'
                    WHERE edge.session_id = %s AND edge.task_id = %s
                      AND (dependency.status <> 'COMPLETED' OR result.id IS NULL)
                    ORDER BY edge.dependency_ordinal
                    """,
                    (guard.session_id, task_id),
                ).fetchall()
            )
            status = SubagentTaskStatus(str(task["status"]))
            body = build_subagent_completion_storage_body(
                task_id=task_id,
                task_key=(None if task["task_key"] is None else str(task["task_key"])),
                label=(None if task["label"] is None else str(task["label"])),
                display_role=(
                    None
                    if task["display_role"] is None
                    else str(task["display_role"])
                ),
                profile=str(task["profile_kind"]),
                status=status,
                terminal_reason=(
                    None
                    if task["terminal_reason"] is None
                    else str(task["terminal_reason"])
                ),
                terminal_public_detail=(
                    None
                    if task["terminal_public_detail"] is None
                    else str(task["terminal_public_detail"])
                ),
                failed_dependency_task_ids=failed_dependency_task_ids,
                result_id=(None if task["result_id"] is None else str(task["result_id"])),
                result_source=(
                    None
                    if task["result_source"] is None
                    else str(task["result_source"])
                ),
                result_summary=(
                    None
                    if task["result_summary"] is None
                    else str(task["result_summary"])
                ),
            )
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=str(task["workspace_id"]),
                turn_id=turn_id,
                entry_id=entry_id,
                entry_sequence=sequence,
                entry_kind=EntryKind.INTER_AGENT_MESSAGE,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=InlineContent.from_bytes(
                    body,
                    media_type=SUBAGENT_COMPLETION_MEDIA_TYPE,
                    codec="utf-8",
                ),
                source_subagent_task_id=task_id,
            )
            if command_id is not None:
                assert semantic_digest is not None
                self._bind_completion_command(
                    connection,
                    session_id=guard.session_id,
                    command_id=command_id,
                    semantic_digest=semantic_digest,
                    target_entry_id=entry_id,
                )
            event = self._append_events(
                connection,
                guard,
                workspace_id=str(task["workspace_id"]),
                drafts=(
                    CommittedEventDraft(
                        event_id=_stable_identity(
                            "event", guard.session_id, task_id, turn_id,
                            "InterAgentMessageAccepted",
                        ),
                        event_type=CommittedEventType.INTER_AGENT_MESSAGE_ACCEPTED,
                        subject=CommittedEventSubject(SubjectSlot.ENTRY, entry_id),
                        actor_kind=("runtime" if automatic else "user"),
                        actor_id=actor_id,
                        sensitivity_class="PUBLIC",
                        projection_profile="DEFAULT",
                        occurred_at=occurred_at,
                        payload={
                            "source_subagent_task_id": task_id,
                            "status": status.value,
                            **(
                                {}
                                if command_id is None
                                else {"manual_command_id": command_id}
                            ),
                        },
                    ),
                ),
            )[0]
            return AcceptedSubagentCompletion(
                SubagentCompletionDisposition.CREATED,
                AcceptedEntry(entry_id, turn_id, sequence, event.event_sequence),
            )

    @staticmethod
    def _completion_source_row(
        connection: Connection, *, session_id: str, task_id: str
    ) -> Mapping[str, object] | None:
        return connection.execute(
            """
            SELECT t.workspace_id, t.task_key, t.label, t.display_role,
                   t.profile_kind, t.status, t.terminal_reason,
                   t.terminal_public_detail,
                   result.id AS result_id, result.result_source,
                   result.summary AS result_summary
            FROM pulsara_v3.subagent_tasks AS t
            LEFT JOIN pulsara_v3.subagent_task_children AS result
              ON result.session_id = t.session_id AND result.task_id = t.id
             AND result.child_kind = 'RESULT'
            WHERE t.session_id = %s AND t.id = %s
              AND t.status IN (
                'COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED',
                'BLOCKED_DEPENDENCY_FAILED'
              )
            FOR UPDATE OF t
            """,
            (session_id, task_id),
        ).fetchone()

    @staticmethod
    def _accepted_completion_row(
        connection: Connection, *, session_id: str, task_id: str
    ) -> Mapping[str, object] | None:
        return connection.execute(
            """
            SELECT e.id, e.turn_id, e.entry_sequence, a.event_sequence
            FROM pulsara_v3.transcript_entries AS e
            JOIN pulsara_v3.agent_events AS a
              ON a.session_id = e.session_id
             AND a.subject_entry_id = e.id
             AND a.event_type = 'InterAgentMessageAccepted'
            WHERE e.entry_owner_kind = 'EXECUTED_TURN' AND e.session_id = %s AND e.source_subagent_task_id = %s
            """,
            (session_id, task_id),
        ).fetchone()

    @staticmethod
    def _completion_accepted_entry(row: Mapping[str, object]) -> AcceptedEntry:
        return AcceptedEntry(
            str(row["id"]),
            str(row["turn_id"]),
            int(row["entry_sequence"]),
            int(row["event_sequence"]),
        )

    @staticmethod
    def _bind_completion_command(
        connection: Connection,
        *,
        session_id: str,
        command_id: str,
        semantic_digest: str,
        target_entry_id: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO pulsara_v3.session_commands (
                session_id, command_id, command_kind,
                request_schema_version, semantic_digest,
                target_kind, target_entry_id
            ) VALUES (%s, %s, 'ACCEPT_SUBAGENT_COMPLETION',
                      'accept_subagent_completion.v1', %s, 'ENTRY', %s)
            """,
            (session_id, command_id, semantic_digest, target_entry_id),
        )

    def _prepare_completion_target(
        self,
        connection: Connection,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        entry_id: str,
        new_context_binding_revision_id: str | None,
        source_workspace_id: str,
        requested_permission_mode: PermissionMode | None,
        expected_provider_input_cut: PreparedProviderInputCut | None,
        automatic: bool,
    ) -> int | None:
        workspace_id = self._workspace_id(connection, guard.session_id)
        if workspace_id != source_workspace_id:
            raise ConversationKernelConflict("subagent completion workspace drifted")
        if new_context_binding_revision_id is None:
            turn = connection.execute(
                """
                SELECT t.workspace_id, t.conversation_scope_kind, t.status,
                       t.current_context_binding_revision_id,
                       s.latest_entry_sequence
                FROM pulsara_v3.turns AS t
                JOIN pulsara_v3.sessions AS s ON s.id = t.session_id
                WHERE t.session_id = %s AND t.id = %s
                FOR UPDATE OF t
                """,
                (guard.session_id, turn_id),
            ).fetchone()
            if (
                turn is None
                or str(turn["conversation_scope_kind"]) != "ROOT"
                or str(turn["status"]) != "RUNNING"
            ):
                return None
            if str(turn["workspace_id"]) != source_workspace_id:
                raise ConversationKernelConflict("completion target drifted")
            if requested_permission_mode is not None:
                raise ValueError("active completion cannot replace permission")
            if automatic:
                assert expected_provider_input_cut is not None
                if (
                    expected_provider_input_cut.session_id != guard.session_id
                    or expected_provider_input_cut.turn_id != turn_id
                    or expected_provider_input_cut.context_binding_revision_id
                    != str(turn["current_context_binding_revision_id"])
                    or expected_provider_input_cut.provider_input_through_sequence
                    != int(turn["latest_entry_sequence"])
                ):
                    return None
                pending_steer = connection.execute(
                    """
                    SELECT 1 FROM pulsara_v3.prompt_queue_items
                    WHERE session_id = %s AND target_turn_id = %s
                      AND status = 'PENDING'
                      AND delivery_mode = 'STEER_ACTIVE_TURN'
                    LIMIT 1
                    """,
                    (guard.session_id, turn_id),
                ).fetchone()
                if pending_steer is not None:
                    return None
            return self._allocate_entry_sequence(connection, guard.session_id)

        if automatic or not new_context_binding_revision_id:
            raise ValueError("new ROOT completion target is invalid")
        if requested_permission_mode is None:
            raise ValueError("new ROOT completion requires a permission mode")
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
            admission_source=RunPermissionAdmissionSource.SUBAGENT_COMPLETION_COMMAND,
        )
        session = self._require_writer(connection, guard, lock=False)
        model_call_binding = session["model_call_binding"]
        if model_call_binding is None:
            raise ConversationKernelConflict(
                "session has no model binding for the new ROOT turn"
            )
        connection.execute(
            """
            INSERT INTO pulsara_v3.turns (
                id, session_id, workspace_id, conversation_scope_kind,
                model_call_binding, status, initial_entry_id,
                current_context_binding_revision_id,
                permission_snapshot_id, requested_permission_mode,
                effective_permission_mode, permission_admission_source,
                permission_overlay, permission_plan_context_ordinal,
                permission_plan_workflow_id,
                permission_plan_revision_at_admission,
                permission_inherited_from_turn_id, permission_contract_id,
                permission_contract_fingerprint,
                permission_snapshot_fingerprint
            ) VALUES (%s, %s, %s, 'ROOT', %s, 'RUNNING', %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                turn_id,
                guard.session_id,
                workspace_id,
                Jsonb(model_call_binding),
                entry_id,
                new_context_binding_revision_id,
                *self._permission_columns(permission),
            ),
        )
        self._insert_initial_context_binding_revision(
            connection,
            session_id=guard.session_id,
            turn_id=turn_id,
            revision_id=new_context_binding_revision_id,
            initial_entry_sequence=sequence,
            scope_kind=ConversationScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        return sequence
