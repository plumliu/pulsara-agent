"""Task-scoped subagent conversation operations."""

from __future__ import annotations

from datetime import datetime
import json
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
    InlineContent,
)
from pulsara_agent.primitives.run_permission import (
    FrozenRunPermissionSnapshot,
    RunPermissionAdmissionSource,
)
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType, SubjectSlot
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .contracts import (
    AcceptedEntry,
    ConversationKernelConflict,
    PreparedSubagentTurnAdmission,
    NoToolResultSideBranch,
    TurnAdmissionConfirmation,
    TurnAdmissionConfirmationKind,
    _canonical_content_matches_utf8_text,
    _stable_identity,
    build_prepared_subagent_turn_admission,
)

from .matching import (
    _event_row_matches_draft,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    ExplicitSubagentResultConfirmation,
    ExplicitSubagentResultConfirmationKind,
    FrozenSubagentResultPublicFact,
    PreparedExplicitSubagentResultSettlement,
    PreparedInterAgentMailboxBatch,
    PreparedSubagentTaskBatchAdmission,
    PreparedSubagentTaskStart,
    PreparedSubagentTaskTerminalSettlement,
    SubagentBatchConfirmation,
    SubagentBatchConfirmationKind,
    SubagentTaskTerminalConfirmationKind,
    SubagentTaskStatus,
    build_dependency_result_context,
    derive_subagent_batch_initial_dispositions,
    subagent_task_batch_identity_digest,
)
from pulsara_agent.primitives.context import freeze_json, thaw_json


def _explicit_result_arguments_match(
    arguments: object, candidate: FrozenSubagentResultPublicFact
) -> bool:
    if not isinstance(arguments, Mapping):
        return False
    if set(arguments) - {"summary", "output_preview", "diagnostics"}:
        return False
    try:
        return (
            arguments.get("summary") == candidate.summary
            and arguments.get("output_preview") == candidate.output_preview
            and freeze_json(arguments.get("diagnostics", []))
            == candidate.diagnostics
        )
    except (TypeError, ValueError):
        return False


def _subagent_context_arguments_match(
    value: object,
    *,
    mode: str,
    last_n_turns: int | None,
) -> bool:
    if not isinstance(value, Mapping) or set(value) - {"mode", "turns"}:
        return False
    raw_mode = value.get("mode", "none")
    if raw_mode == "none":
        return (
            value.get("turns") is None
            and mode == "NONE"
            and last_n_turns is None
        )
    turns = value.get("turns")
    return (
        raw_mode == "last_n"
        and isinstance(turns, int)
        and not isinstance(turns, bool)
        and 1 <= turns <= 3
        and mode == "LAST_N"
        and last_n_turns == turns
    )


def _subagent_batch_arguments_match(
    arguments: object,
    *,
    tool_name: str,
    candidate: PreparedSubagentTaskBatchAdmission,
) -> bool:
    """Exact-join one public spawn/batch call to its sealed task candidate."""

    if not isinstance(arguments, Mapping):
        return False
    if tool_name == "spawn_agent":
        if set(arguments) - {"task", "task_name", "profile", "context"}:
            return False
        if len(candidate.ordered_tasks) != 1:
            return False
        item = candidate.ordered_tasks[0]
        task_name = arguments.get("task_name")
        return (
            arguments.get("task") == item.objective
            and task_name == item.task_key
            and task_name == item.label
            and item.display_role is None
            and str(arguments.get("profile", "general_worker"))
            == item.profile.value
            and _subagent_context_arguments_match(
                arguments.get("context", {"mode": "none"}),
                mode=item.context.mode.value,
                last_n_turns=item.context.last_n_turns,
            )
            and not item.dependency_task_ids
        )
    if tool_name != "create_agent_tasks" or set(arguments) != {"tasks"}:
        return False
    raw_tasks = arguments.get("tasks")
    if not isinstance(raw_tasks, list) or len(raw_tasks) != len(
        candidate.ordered_tasks
    ):
        return False
    key_to_id = {
        item.task_key: item.task_id
        for item in candidate.ordered_tasks
        if item.task_key is not None
    }
    allowed = {
        "task",
        "task_key",
        "label",
        "profile",
        "display_role",
        "context",
        "depends_on",
    }
    for raw, item in zip(raw_tasks, candidate.ordered_tasks, strict=True):
        if not isinstance(raw, Mapping) or set(raw) - allowed:
            return False
        dependencies = raw.get("depends_on", [])
        if not isinstance(dependencies, list):
            return False
        resolved: list[str] = []
        for token in dependencies:
            if not isinstance(token, str) or not token:
                return False
            if token in key_to_id:
                dependency_id = key_to_id[token]
            elif token.startswith("task:") and len(token) > 5:
                dependency_id = token[5:]
            else:
                return False
            resolved.append(dependency_id)
        if not (
            raw.get("task") == item.objective
            and raw.get("task_key") == item.task_key
            and raw.get("label") == item.label
            and raw.get("display_role") == item.display_role
            and str(raw.get("profile", "general_worker")) == item.profile.value
            and _subagent_context_arguments_match(
                raw.get("context", {"mode": "none"}),
                mode=item.context.mode.value,
                last_n_turns=item.context.last_n_turns,
            )
            and tuple(resolved) == item.dependency_task_ids
        ):
            return False
    return True


def _subagent_batch_subject_matches(
    parent: Mapping[str, object] | None,
    attempt: Mapping[str, object] | None,
    candidate: PreparedSubagentTaskBatchAdmission,
) -> bool:
    if parent is None or attempt is None:
        return False
    tool_name = str(attempt["tool_name"])
    return (
        str(parent["workspace_id"]) == candidate.workspace_id
        and str(parent["conversation_scope_kind"]) == "ROOT"
        and str(parent["effective_permission_mode"]) == "bypass-permissions"
        and str(parent["permission_snapshot_fingerprint"])
        == candidate.permission_snapshot_fingerprint
        and str(attempt["turn_id"]) == candidate.parent_turn_id
        and str(attempt["conversation_scope_kind"]) == "ROOT"
        and attempt["scope_subagent_task_id"] is None
        and str(attempt["permission_snapshot_fingerprint"])
        == candidate.permission_snapshot_fingerprint
        and tool_name in {"spawn_agent", "create_agent_tasks"}
        and _subagent_batch_arguments_match(
            attempt["tool_arguments"],
            tool_name=tool_name,
            candidate=candidate,
        )
    )

class _SubagentOperations:
    def prepare_subagent_launch_permission(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedSubagentTaskStart,
        deadline_monotonic: float,
    ) -> FrozenRunPermissionSnapshot:
        """Freeze the exact parent permission fact after task-start FULL."""

        if (
            candidate.session_id != guard.session_id
            or candidate.writer_generation != guard.writer_generation
        ):
            raise ValueError("subagent launch guard mismatch")
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            task = connection.execute(
                "SELECT * FROM pulsara_v3.subagent_tasks "
                "WHERE session_id=%s AND id=%s",
                (candidate.session_id, candidate.task_id),
            ).fetchone()
            event = connection.execute(
                "SELECT * FROM pulsara_v3.agent_events "
                "WHERE session_id=%s AND event_id=%s",
                (candidate.session_id, candidate.event_id),
            ).fetchone()
            expected_event = self._subagent_task_start_event(candidate)
            if task is None or event is None or not (
                str(task["workspace_id"]) == candidate.workspace_id
                and str(task["parent_turn_id"]) == candidate.parent_turn_id
                and str(task["objective"]) == candidate.objective
                and str(task["profile_kind"]) == candidate.profile.value
                and str(task["context_mode"]) == candidate.parent_context.mode.value
                and task["context_last_n_turns"]
                == candidate.parent_context.last_n_turns
                and int(task["execution_writer_generation"])
                == candidate.writer_generation
                and str(task["status"]) == "ACTIVE"
                and task["pending_reason"] is None
                and task["terminal_reason"] is None
                and _event_row_matches_draft(event, expected_event)
            ):
                raise ConversationKernelConflict(
                    "subagent launch no longer exact-joins task-start FULL"
                )
            parent = connection.execute(
                "SELECT * FROM pulsara_v3.turns "
                "WHERE session_id=%s AND id=%s",
                (candidate.session_id, candidate.parent_turn_id),
            ).fetchone()
            if parent is None:
                raise ConversationKernelConflict(
                    "subagent launch parent turn is absent"
                )
            return self._permission_from_row(parent)

    @staticmethod
    def _subagent_batch_task_event(
        candidate: PreparedSubagentTaskBatchAdmission,
        task_id: str,
        status: str,
    ) -> CommittedEventDraft:
        return CommittedEventDraft(
            event_id=_stable_identity(
                "event",
                subagent_task_batch_identity_digest(candidate),
                task_id,
                "SubagentTaskAccepted",
            ),
            event_type=CommittedEventType.SUBAGENT_TASK_ACCEPTED,
            subject=CommittedEventSubject(SubjectSlot.SUBAGENT_TASK, task_id),
            actor_kind="runtime",
            actor_id=candidate.actor_id,
            sensitivity_class="PUBLIC",
            projection_profile="DEFAULT",
            occurred_at=candidate.occurred_at,
            payload={"status": status, "batch_id": candidate.batch_id},
        )

    @staticmethod
    def _subagent_task_start_event(
        candidate: PreparedSubagentTaskStart,
    ) -> CommittedEventDraft:
        return CommittedEventDraft(
            event_id=candidate.event_id,
            event_type=CommittedEventType.SUBAGENT_TASK_STATUS_ACCEPTED,
            subject=CommittedEventSubject(
                SubjectSlot.SUBAGENT_TASK, candidate.task_id
            ),
            actor_kind="runtime",
            actor_id=candidate.actor_id,
            sensitivity_class="PUBLIC",
            projection_profile="DEFAULT",
            occurred_at=candidate.occurred_at,
            payload={"status": "ACTIVE", "reason": None},
        )

    @staticmethod
    def _subagent_task_terminal_event(
        candidate: PreparedSubagentTaskTerminalSettlement,
    ) -> CommittedEventDraft:
        return CommittedEventDraft(
            event_id=candidate.event_id,
            event_type=CommittedEventType.SUBAGENT_TASK_STATUS_ACCEPTED,
            subject=CommittedEventSubject(
                SubjectSlot.SUBAGENT_TASK, candidate.task_id
            ),
            actor_kind="runtime",
            actor_id=candidate.actor_id,
            sensitivity_class="PUBLIC",
            projection_profile="DEFAULT",
            occurred_at=candidate.occurred_at,
            payload={"status": candidate.status.value, "reason": candidate.reason},
        )

    def accept_subagent_task_batch(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedSubagentTaskBatchAdmission,
        deadline_monotonic: float,
    ) -> tuple[str, ...]:
        """Atomically accept one immutable ROOT-owned task DAG batch."""

        if (
            candidate.session_id != guard.session_id
            or candidate.writer_generation != guard.writer_generation
        ):
            raise ValueError("subagent batch guard identity mismatch")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            parent = connection.execute(
                """SELECT workspace_id, conversation_scope_kind,
                          effective_permission_mode,
                          permission_snapshot_fingerprint
                   FROM pulsara_v3.turns
                   WHERE session_id = %s AND id = %s FOR UPDATE""",
                (guard.session_id, candidate.parent_turn_id),
            ).fetchone()
            attempt = connection.execute(
                """SELECT attempt.id, attempt.assistant_entry_id,
                          attempt.tool_call_id,
                          attempt.permission_snapshot_fingerprint,
                          block.tool_name, block.tool_arguments, entry.turn_id,
                          entry.conversation_scope_kind,
                          entry.scope_subagent_task_id
                   FROM pulsara_v3.tool_execution_attempts AS attempt
                   JOIN pulsara_v3.assistant_message_blocks AS block
                     ON block.session_id = attempt.session_id
                    AND block.assistant_entry_id = attempt.assistant_entry_id
                    AND block.tool_call_id = attempt.tool_call_id
                    AND block.block_kind = 'TOOL_CALL'
                   JOIN pulsara_v3.transcript_entries AS entry
                     ON entry.session_id = block.session_id
                    AND entry.id = block.assistant_entry_id
                   WHERE attempt.session_id = %s AND attempt.id = %s""",
                (guard.session_id, candidate.source_tool_attempt_id),
            ).fetchone()
            if not _subagent_batch_subject_matches(parent, attempt, candidate):
                raise ConversationKernelConflict(
                    "subagent batch does not exact-join its ROOT tool attempt"
                )
            accepted_count = int(
                connection.execute(
                    """SELECT count(*) AS count
                       FROM pulsara_v3.subagent_tasks
                       WHERE session_id = %s AND parent_turn_id = %s""",
                    (guard.session_id, candidate.parent_turn_id),
                ).fetchone()["count"]
            )
            if accepted_count + len(candidate.ordered_tasks) > 16:
                raise ConversationKernelConflict(
                    "ROOT turn subagent task capacity is exhausted"
                )
            batch_ids = {item.task_id for item in candidate.ordered_tasks}
            external_ids = tuple(
                dict.fromkeys(
                    dependency_id
                    for item in candidate.ordered_tasks
                    for dependency_id in item.dependency_task_ids
                    if dependency_id not in batch_ids
                )
            )
            external: dict[str, Mapping[str, object]] = {}
            if external_ids:
                rows = connection.execute(
                    """SELECT t.id, t.workspace_id, t.status,
                              result.id AS result_id
                       FROM pulsara_v3.subagent_tasks AS t
                       LEFT JOIN pulsara_v3.subagent_task_children AS result
                         ON result.session_id = t.session_id
                        AND result.task_id = t.id
                        AND result.child_kind = 'RESULT'
                       WHERE t.session_id = %s AND t.id = ANY(%s)
                       FOR SHARE OF t""",
                    (guard.session_id, list(external_ids)),
                ).fetchall()
                external = {str(row["id"]): row for row in rows}
                if set(external) != set(external_ids):
                    raise ConversationKernelConflict(
                        "subagent dependency is unknown or foreign"
                    )
                if any(
                    str(row["workspace_id"]) != candidate.workspace_id
                    for row in external.values()
                ):
                    raise ConversationKernelConflict(
                        "subagent dependency crosses workspace"
                    )
            try:
                resolved_status = dict(
                    derive_subagent_batch_initial_dispositions(
                        ordered_tasks=tuple(
                            (item.task_id, item.dependency_task_ids)
                            for item in candidate.ordered_tasks
                        ),
                        external_states={
                            task_id: (
                                SubagentTaskStatus(str(row["status"])),
                                row["result_id"] is not None,
                            )
                            for task_id, row in external.items()
                        },
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ConversationKernelConflict(
                    "subagent dependency frontier is invalid"
                ) from exc
            events: list[CommittedEventDraft] = []
            for item in candidate.ordered_tasks:
                disposition = resolved_status[item.task_id]
                if (
                    disposition.status is not item.initial_status
                    or disposition.pending_reason != item.pending_reason
                    or disposition.terminal_reason != item.terminal_reason
                ):
                    raise ConversationKernelConflict(
                        "subagent dependency status changed before admission"
                    )
                connection.execute(
                    """INSERT INTO pulsara_v3.subagent_tasks (
                           id, session_id, workspace_id, parent_turn_id,
                           batch_id, task_key, label, profile_kind, display_role,
                           context_mode, context_last_n_turns, objective, status,
                           pending_reason, terminal_reason,
                           execution_writer_generation, terminal_at
                       ) VALUES (
                           %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s,
                           CASE WHEN %s THEN clock_timestamp() ELSE NULL END
                       )""",
                    (
                        item.task_id,
                        guard.session_id,
                        candidate.workspace_id,
                        candidate.parent_turn_id,
                        candidate.batch_id,
                        item.task_key,
                        item.label,
                        item.profile.value,
                        item.display_role,
                        item.context.mode.value,
                        item.context.last_n_turns,
                        item.objective,
                        item.initial_status.value,
                        item.pending_reason,
                        item.terminal_reason,
                        guard.writer_generation,
                        item.initial_status.terminal,
                    ),
                )
                events.append(
                    self._subagent_batch_task_event(
                        candidate, item.task_id, item.initial_status.value
                    )
                )
                for ordinal, dependency_id in enumerate(item.dependency_task_ids):
                    connection.execute(
                        """INSERT INTO pulsara_v3.subagent_task_dependencies (
                               session_id, task_id, dependency_task_id,
                               dependency_ordinal
                           ) VALUES (%s, %s, %s, %s)""",
                        (guard.session_id, item.task_id, dependency_id, ordinal),
                    )
            self._append_events(
                connection,
                guard,
                workspace_id=candidate.workspace_id,
                drafts=tuple(events),
            )
            return tuple(item.task_id for item in candidate.ordered_tasks)

    def confirm_subagent_task_batch(
        self,
        *,
        candidate: PreparedSubagentTaskBatchAdmission,
        deadline_monotonic: float,
    ) -> SubagentBatchConfirmation:
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            parent = connection.execute(
                """SELECT workspace_id, conversation_scope_kind,
                          effective_permission_mode,
                          permission_snapshot_fingerprint
                   FROM pulsara_v3.turns
                   WHERE session_id = %s AND id = %s""",
                (candidate.session_id, candidate.parent_turn_id),
            ).fetchone()
            attempt = connection.execute(
                """SELECT attempt.id, attempt.assistant_entry_id,
                          attempt.tool_call_id,
                          attempt.permission_snapshot_fingerprint,
                          block.tool_name, block.tool_arguments, entry.turn_id,
                          entry.conversation_scope_kind,
                          entry.scope_subagent_task_id
                   FROM pulsara_v3.tool_execution_attempts AS attempt
                   JOIN pulsara_v3.assistant_message_blocks AS block
                     ON block.session_id = attempt.session_id
                    AND block.assistant_entry_id = attempt.assistant_entry_id
                    AND block.tool_call_id = attempt.tool_call_id
                    AND block.block_kind = 'TOOL_CALL'
                   JOIN pulsara_v3.transcript_entries AS entry
                     ON entry.session_id = block.session_id
                    AND entry.id = block.assistant_entry_id
                   WHERE attempt.session_id = %s AND attempt.id = %s""",
                (candidate.session_id, candidate.source_tool_attempt_id),
            ).fetchone()
            if not _subagent_batch_subject_matches(parent, attempt, candidate):
                return SubagentBatchConfirmation(
                    SubagentBatchConfirmationKind.CONFLICT
                )
            tasks = tuple(
                connection.execute(
                    """SELECT * FROM pulsara_v3.subagent_tasks
                       WHERE session_id = %s AND id = %s""",
                    (candidate.session_id, item.task_id),
                ).fetchone()
                for item in candidate.ordered_tasks
            )
            edges = tuple(
                connection.execute(
                    """SELECT dependency_task_id, dependency_ordinal
                       FROM pulsara_v3.subagent_task_dependencies
                       WHERE session_id = %s AND task_id = %s
                       ORDER BY dependency_ordinal""",
                    (candidate.session_id, item.task_id),
                ).fetchall()
                for item in candidate.ordered_tasks
            )
            events = tuple(
                connection.execute(
                    """SELECT * FROM pulsara_v3.agent_events
                       WHERE session_id = %s AND event_id = %s""",
                    (
                        candidate.session_id,
                        _stable_identity(
                            "event",
                            subagent_task_batch_identity_digest(candidate),
                            item.task_id,
                            "SubagentTaskAccepted",
                        ),
                    ),
                ).fetchone()
                for item in candidate.ordered_tasks
            )
            if all(row is None for row in tasks) and all(not row for row in edges) and all(
                row is None for row in events
            ):
                return SubagentBatchConfirmation(SubagentBatchConfirmationKind.NONE)
            if any(row is None for row in tasks) or any(row is None for row in events):
                return SubagentBatchConfirmation(SubagentBatchConfirmationKind.CONFLICT)
            for item, row, edge_rows, event in zip(
                candidate.ordered_tasks, tasks, edges, events, strict=True
            ):
                assert row is not None and event is not None
                observed_dependencies = tuple(
                    str(edge["dependency_task_id"]) for edge in edge_rows
                )
                expected_event = self._subagent_batch_task_event(
                    candidate, item.task_id, item.initial_status.value
                )
                if not (
                    str(row["workspace_id"]) == candidate.workspace_id
                    and str(row["parent_turn_id"]) == candidate.parent_turn_id
                    and str(row["batch_id"]) == candidate.batch_id
                    and row["task_key"] == item.task_key
                    and row["label"] == item.label
                    and str(row["profile_kind"]) == item.profile.value
                    and row["display_role"] == item.display_role
                    and str(row["context_mode"]) == item.context.mode.value
                    and row["context_last_n_turns"] == item.context.last_n_turns
                    and str(row["objective"]) == item.objective
                    and str(row["status"]) == item.initial_status.value
                    and row["pending_reason"] == item.pending_reason
                    and row["terminal_reason"] == item.terminal_reason
                    and int(row["execution_writer_generation"])
                    == candidate.writer_generation
                    and observed_dependencies == item.dependency_task_ids
                    and tuple(int(edge["dependency_ordinal"]) for edge in edge_rows)
                    == tuple(range(len(edge_rows)))
                    and _event_row_matches_draft(event, expected_event)
                ):
                    return SubagentBatchConfirmation(
                        SubagentBatchConfirmationKind.CONFLICT
                    )
            return SubagentBatchConfirmation(SubagentBatchConfirmationKind.FULL)

    def accept_subagent_task_start(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedSubagentTaskStart,
        deadline_monotonic: float,
    ) -> bool:
        """CAS one exact runnable task to ACTIVE with its stable occurrence."""

        if (
            candidate.session_id != guard.session_id
            or candidate.writer_generation != guard.writer_generation
        ):
            raise ValueError("subagent start guard identity mismatch")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            task = connection.execute(
                """SELECT * FROM pulsara_v3.subagent_tasks
                   WHERE session_id = %s AND id = %s FOR UPDATE""",
                (guard.session_id, candidate.task_id),
            ).fetchone()
            if task is None:
                raise ConversationKernelConflict("subagent start target is absent")
            if str(task["status"]) != "PENDING_START":
                return False
            if not (
                str(task["workspace_id"]) == candidate.workspace_id
                and str(task["parent_turn_id"]) == candidate.parent_turn_id
                and str(task["objective"]) == candidate.objective
                and str(task["profile_kind"]) == candidate.profile.value
                and str(task["context_mode"])
                == candidate.parent_context.mode.value
                and task["context_last_n_turns"]
                == candidate.parent_context.last_n_turns
                and int(task["execution_writer_generation"])
                == candidate.writer_generation
                and task["pending_reason"] == "CAPACITY"
                and task["terminal_reason"] is None
            ):
                raise ConversationKernelConflict("subagent start target drifted")
            dependency_rows = connection.execute(
                """SELECT edge.task_id, edge.dependency_task_id,
                          edge.dependency_ordinal, dependency.status,
                          dependency.task_key, dependency.label,
                          result.id AS result_id, result.result_source,
                          result.summary, result.result_fingerprint
                   FROM pulsara_v3.subagent_task_dependencies AS edge
                   JOIN pulsara_v3.subagent_tasks AS dependency
                     ON dependency.session_id = edge.session_id
                    AND dependency.id = edge.dependency_task_id
                   LEFT JOIN pulsara_v3.subagent_task_children AS result
                     ON result.session_id = dependency.session_id
                    AND result.task_id = dependency.id
                    AND result.child_kind = 'RESULT'
                   WHERE edge.session_id = %s AND edge.task_id = %s
                   ORDER BY edge.dependency_ordinal
                   FOR SHARE OF dependency""",
                (guard.session_id, candidate.task_id),
            ).fetchall()
            try:
                observed_context = build_dependency_result_context(
                    target_task_id=candidate.task_id,
                    rows=dependency_rows,
                )
            except (TypeError, ValueError) as exc:
                raise ConversationKernelConflict(
                    "subagent start dependency result set is not ready"
                ) from exc
            if observed_context != candidate.dependency_context:
                raise ConversationKernelConflict(
                    "subagent start dependency context drifted"
                )
            updated = connection.execute(
                """UPDATE pulsara_v3.subagent_tasks
                   SET status = 'ACTIVE', pending_reason = NULL
                   WHERE session_id = %s AND id = %s
                     AND status = 'PENDING_START'
                     AND execution_writer_generation = %s
                   RETURNING id""",
                (guard.session_id, candidate.task_id, guard.writer_generation),
            ).fetchone()
            if updated is None:
                raise ConversationKernelConflict("subagent start lost its task winner")
            self._append_events(
                connection,
                guard,
                workspace_id=candidate.workspace_id,
                drafts=(self._subagent_task_start_event(candidate),),
            )
            return True

    def confirm_subagent_task_start(
        self,
        *,
        candidate: PreparedSubagentTaskStart,
        deadline_monotonic: float,
    ) -> SubagentBatchConfirmation:
        """Stateless FULL/NONE/CONFLICT confirmation for one exact start."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            task = connection.execute(
                "SELECT * FROM pulsara_v3.subagent_tasks WHERE session_id=%s AND id=%s",
                (candidate.session_id, candidate.task_id),
            ).fetchone()
            event = connection.execute(
                "SELECT * FROM pulsara_v3.agent_events WHERE session_id=%s AND event_id=%s",
                (candidate.session_id, candidate.event_id),
            ).fetchone()
            if task is None:
                return SubagentBatchConfirmation(SubagentBatchConfirmationKind.CONFLICT)
            common = (
                str(task["workspace_id"]) == candidate.workspace_id
                and str(task["parent_turn_id"]) == candidate.parent_turn_id
                and str(task["objective"]) == candidate.objective
                and str(task["profile_kind"]) == candidate.profile.value
                and str(task["context_mode"])
                == candidate.parent_context.mode.value
                and task["context_last_n_turns"]
                == candidate.parent_context.last_n_turns
                and int(task["execution_writer_generation"])
                == candidate.writer_generation
            )
            if (
                common
                and str(task["status"]) == "PENDING_START"
                and task["pending_reason"] == "CAPACITY"
                and event is None
            ):
                return SubagentBatchConfirmation(SubagentBatchConfirmationKind.NONE)
            expected_event = self._subagent_task_start_event(candidate)
            if (
                common
                and str(task["status"]) == "ACTIVE"
                and task["pending_reason"] is None
                and task["terminal_reason"] is None
                and event is not None
                and _event_row_matches_draft(event, expected_event)
            ):
                return SubagentBatchConfirmation(SubagentBatchConfirmationKind.FULL)
            return SubagentBatchConfirmation(SubagentBatchConfirmationKind.CONFLICT)

    def accept_subagent_task_terminal_settlement(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedSubagentTaskTerminalSettlement,
        deadline_monotonic: float,
    ) -> bool:
        """Install one exact task-only terminal winner without touching a turn."""

        if (
            candidate.session_id != guard.session_id
            or candidate.writer_generation != guard.writer_generation
        ):
            raise ValueError("subagent terminal settlement guard mismatch")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            task = connection.execute(
                "SELECT * FROM pulsara_v3.subagent_tasks "
                "WHERE session_id=%s AND id=%s FOR UPDATE",
                (candidate.session_id, candidate.task_id),
            ).fetchone()
            turn = connection.execute(
                "SELECT status, conversation_scope_kind, scope_subagent_task_id "
                "FROM pulsara_v3.turns WHERE session_id=%s AND id=%s FOR SHARE",
                (candidate.session_id, candidate.expected_turn_id),
            ).fetchone()
            if task is None or not (
                str(task["workspace_id"]) == candidate.workspace_id
                and int(task["execution_writer_generation"])
                == candidate.writer_generation
            ):
                raise ConversationKernelConflict(
                    "subagent terminal settlement target drifted"
                )
            if candidate.require_absent_turn:
                if turn is not None:
                    raise ConversationKernelConflict(
                        "task-only terminal settlement found an admitted turn"
                    )
            elif turn is not None and not (
                str(turn["conversation_scope_kind"]) == "SUBAGENT_TASK"
                and str(turn["scope_subagent_task_id"]) == candidate.task_id
                and str(turn["status"]) in {"COMPLETED", "INTERRUPTED"}
            ):
                raise ConversationKernelConflict(
                    "task terminal settlement found a nonterminal child turn"
                )
            if str(task["status"]) not in {
                "PENDING_START",
                "WAITING_DEPENDENCY",
                "ACTIVE",
            }:
                return False
            updated = connection.execute(
                """UPDATE pulsara_v3.subagent_tasks
                   SET status=%s, pending_reason=NULL, terminal_reason=%s,
                       terminal_at=clock_timestamp()
                   WHERE session_id=%s AND id=%s
                     AND execution_writer_generation=%s
                     AND status IN ('PENDING_START', 'WAITING_DEPENDENCY', 'ACTIVE')
                   RETURNING id""",
                (
                    candidate.status.value,
                    candidate.reason,
                    candidate.session_id,
                    candidate.task_id,
                    candidate.writer_generation,
                ),
            ).fetchone()
            if updated is None:
                raise ConversationKernelConflict(
                    "subagent terminal settlement lost its task winner"
                )
            self._append_events(
                connection,
                guard,
                workspace_id=candidate.workspace_id,
                drafts=(self._subagent_task_terminal_event(candidate),),
            )
            return True

    def confirm_subagent_task_terminal_settlement(
        self,
        *,
        candidate: PreparedSubagentTaskTerminalSettlement,
        deadline_monotonic: float,
    ) -> SubagentTaskTerminalConfirmationKind:
        """Statelessly classify one stable task-only terminal candidate."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            task = connection.execute(
                "SELECT * FROM pulsara_v3.subagent_tasks WHERE session_id=%s AND id=%s",
                (candidate.session_id, candidate.task_id),
            ).fetchone()
            turn = connection.execute(
                "SELECT status, conversation_scope_kind, scope_subagent_task_id "
                "FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
                (candidate.session_id, candidate.expected_turn_id),
            ).fetchone()
            event = connection.execute(
                "SELECT * FROM pulsara_v3.agent_events WHERE session_id=%s AND event_id=%s",
                (candidate.session_id, candidate.event_id),
            ).fetchone()
            if task is None or not (
                str(task["workspace_id"]) == candidate.workspace_id
                and int(task["execution_writer_generation"])
                == candidate.writer_generation
            ):
                return SubagentTaskTerminalConfirmationKind.CONFLICT
            turn_matches = (
                turn is None
                if candidate.require_absent_turn
                else turn is None
                or (
                    str(turn["conversation_scope_kind"]) == "SUBAGENT_TASK"
                    and str(turn["scope_subagent_task_id"]) == candidate.task_id
                    and str(turn["status"]) in {"COMPLETED", "INTERRUPTED"}
                )
            )
            if not turn_matches:
                return SubagentTaskTerminalConfirmationKind.CONFLICT
            if (
                str(task["status"])
                in {"PENDING_START", "WAITING_DEPENDENCY", "ACTIVE"}
                and event is None
            ):
                return SubagentTaskTerminalConfirmationKind.NONE
            expected_event = self._subagent_task_terminal_event(candidate)
            if (
                str(task["status"]) == candidate.status.value
                and task["pending_reason"] is None
                and str(task["terminal_reason"]) == candidate.reason
                and task["terminal_at"] is not None
                and event is not None
                and _event_row_matches_draft(event, expected_event)
            ):
                return SubagentTaskTerminalConfirmationKind.FULL
            return SubagentTaskTerminalConfirmationKind.CONFLICT

    @staticmethod
    def _explicit_result_events(
        candidate: PreparedExplicitSubagentResultSettlement,
    ) -> tuple[CommittedEventDraft, CommittedEventDraft, CommittedEventDraft]:
        result = candidate.result
        observed_at = candidate.tool_result.observed_at
        return (
            CommittedEventDraft(
                event_id=_stable_identity(
                    "event", result.result_id, "SubagentResultAccepted"
                ),
                event_type=CommittedEventType.SUBAGENT_RESULT_ACCEPTED,
                subject=CommittedEventSubject(
                    SubjectSlot.SUBAGENT_RESULT, result.result_id
                ),
                actor_kind="subagent",
                actor_id=candidate.task_id,
                sensitivity_class="PUBLIC",
                projection_profile="DEFAULT",
                occurred_at=observed_at,
                payload={"result_source": "EXPLICIT"},
            ),
            CommittedEventDraft(
                event_id=_stable_identity(
                    "event",
                    candidate.task_id,
                    candidate.tool_result.turn_id,
                    "SubagentTaskStatusAccepted",
                    "COMPLETED",
                ),
                event_type=CommittedEventType.SUBAGENT_TASK_STATUS_ACCEPTED,
                subject=CommittedEventSubject(
                    SubjectSlot.SUBAGENT_TASK, candidate.task_id
                ),
                actor_kind="runtime",
                actor_id="foreground-runner",
                sensitivity_class="PUBLIC",
                projection_profile="DEFAULT",
                occurred_at=observed_at,
                payload={"status": "COMPLETED", "reason": None},
            ),
            CommittedEventDraft(
                event_id=_stable_identity(
                    "event",
                    candidate.tool_result.turn_id,
                    candidate.tool_result.result_entry_id,
                    "TurnCompleted",
                ),
                event_type=CommittedEventType.TURN_COMPLETED,
                subject=CommittedEventSubject(
                    SubjectSlot.TURN, candidate.tool_result.turn_id
                ),
                actor_kind="runtime",
                actor_id="foreground-runner",
                sensitivity_class="PUBLIC",
                projection_profile="DEFAULT",
                occurred_at=observed_at,
                payload={"final_entry_id": candidate.tool_result.result_entry_id},
            ),
        )

    def accept_explicit_subagent_result(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedExplicitSubagentResultSettlement,
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        """Atomically accept report acknowledgement, result, turn and task."""

        tool = candidate.tool_result
        result = candidate.result
        if tool.session_id != guard.session_id:
            raise ValueError("explicit subagent result belongs to another session")
        if not isinstance(tool.side_branch, NoToolResultSideBranch):
            raise ValueError("explicit subagent result cannot own a side branch")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            subject = connection.execute(
                """SELECT t.workspace_id, t.status AS turn_status,
                          t.permission_snapshot_fingerprint,
                          t.scope_subagent_task_id,
                          task.status AS task_status,
                          block.tool_name, block.tool_arguments,
                          attempt.id AS exact_attempt_id,
                          attempt.permission_snapshot_fingerprint AS attempt_permission
                   FROM pulsara_v3.turns AS t
                   JOIN pulsara_v3.subagent_tasks AS task
                     ON task.session_id = t.session_id
                    AND task.id = t.scope_subagent_task_id
                   JOIN pulsara_v3.assistant_message_blocks AS block
                     ON block.session_id = t.session_id
                    AND block.assistant_entry_id = %s
                    AND block.tool_call_id = %s
                    AND block.block_kind = 'TOOL_CALL'
                   JOIN pulsara_v3.tool_execution_attempts AS attempt
                     ON attempt.session_id = block.session_id
                    AND attempt.assistant_entry_id = block.assistant_entry_id
                    AND attempt.tool_call_id = block.tool_call_id
                    AND attempt.id = %s
                   WHERE t.session_id = %s AND t.id = %s
                     AND t.conversation_scope_kind = 'SUBAGENT_TASK'
                     AND t.scope_subagent_task_id = %s
                   FOR UPDATE OF t, task""",
                (
                    tool.assistant_entry_id,
                    tool.tool_call_id,
                    tool.attempt_id,
                    guard.session_id,
                    tool.turn_id,
                    candidate.task_id,
                ),
            ).fetchone()
            if (
                subject is None
                or str(subject["workspace_id"]) != tool.workspace_id
                or str(subject["turn_status"]) != "RUNNING"
                or str(subject["task_status"]) != "ACTIVE"
                or str(subject["tool_name"]) != "report_agent_result"
                or str(subject["exact_attempt_id"]) != tool.attempt_id
                or str(subject["attempt_permission"])
                != str(subject["permission_snapshot_fingerprint"])
                or not _explicit_result_arguments_match(
                    subject["tool_arguments"], result
                )
            ):
                raise ConversationKernelConflict(
                    "explicit subagent result subject is not active and exact"
                )
            entry_sequence = self._allocate_entry_sequence(
                connection, guard.session_id
            )
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=tool.workspace_id,
                turn_id=tool.turn_id,
                entry_id=tool.result_entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.TOOL_RESULT,
                scope_kind=ConversationScopeKind.SUBAGENT_TASK,
                scope_task_id=candidate.task_id,
                content=tool.canonical_preview_content,
            )
            connection.execute(
                """INSERT INTO pulsara_v3.tool_results (
                       id, session_id, workspace_id,
                       tool_call_entry_id, tool_call_id, attempt_id,
                       result_origin_kind, result_entry_id, result_state,
                       permission_snapshot_fingerprint,
                       output_artifact_disposition, output_artifact_id,
                       output_artifact_blob_id, output_source_coverage,
                       output_display_kind, output_source_coverage_reason,
                       output_artifact_unavailability_reason,
                       model_visible_memory_fact_ids,
                       observed_at, observation_duration_microseconds,
                       observation_origin_kind,
                       tool_reported_duration_microseconds
                   ) VALUES (
                       %s, %s, %s, %s, %s, %s, 'PHYSICAL_ATTEMPT', %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s
                   )""",
                (
                    tool.result_id,
                    guard.session_id,
                    tool.workspace_id,
                    tool.assistant_entry_id,
                    tool.tool_call_id,
                    tool.attempt_id,
                    tool.result_entry_id,
                    tool.result_state,
                    subject["permission_snapshot_fingerprint"],
                    tool.artifact_disposition.value,
                    tool.artifact_id,
                    None,
                    tool.source_coverage.value,
                    tool.display_kind.value,
                    None
                    if tool.source_coverage_reason is None
                    else tool.source_coverage_reason.value,
                    None
                    if tool.artifact_unavailability_reason is None
                    else tool.artifact_unavailability_reason.value,
                    list(tool.model_visible_memory_fact_ids),
                    tool.observed_at,
                    tool.observation_duration_microseconds,
                    tool.observation_origin_kind.value,
                    tool.trusted_tool_reported_duration_microseconds,
                ),
            )
            connection.execute(
                """INSERT INTO pulsara_v3.subagent_task_children (
                       id, session_id, task_id, child_kind, child_ordinal,
                       entry_id, result_source, summary, output_preview,
                       diagnostics, result_fingerprint
                   ) VALUES (
                       %s, %s, %s, 'RESULT',
                       (SELECT count(*) FROM pulsara_v3.subagent_task_children
                         WHERE session_id = %s AND task_id = %s),
                       %s, 'EXPLICIT', %s, %s, %s::jsonb, %s
                   )""",
                (
                    result.result_id,
                    guard.session_id,
                    candidate.task_id,
                    guard.session_id,
                    candidate.task_id,
                    result.producer_entry_id,
                    result.summary,
                    result.output_preview,
                    json.dumps(
                        thaw_json(result.diagnostics),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    result.result_fingerprint,
                ),
            )
            task_row = connection.execute(
                """UPDATE pulsara_v3.subagent_tasks
                   SET status = 'COMPLETED', pending_reason = NULL,
                       terminal_reason = NULL, terminal_at = clock_timestamp()
                   WHERE session_id = %s AND id = %s AND status = 'ACTIVE'
                     AND execution_writer_generation = %s
                   RETURNING id""",
                (guard.session_id, candidate.task_id, guard.writer_generation),
            ).fetchone()
            turn_row = connection.execute(
                """UPDATE pulsara_v3.turns
                   SET status = 'COMPLETED', final_entry_id = %s,
                       terminal_reason = 'COMPLETED', terminal_at = clock_timestamp()
                   WHERE session_id = %s AND id = %s AND status = 'RUNNING'
                   RETURNING id""",
                (tool.result_entry_id, guard.session_id, tool.turn_id),
            ).fetchone()
            if task_row is None or turn_row is None:
                raise ConversationKernelConflict(
                    "explicit subagent result lost its terminal winner"
                )
            events = (
                tool.tool_result_occurrence,
                *self._explicit_result_events(candidate),
            )
            accepted_events = self._append_events(
                connection,
                guard,
                workspace_id=tool.workspace_id,
                drafts=events,
            )
            return AcceptedEntry(
                entry_id=tool.result_entry_id,
                turn_id=tool.turn_id,
                entry_sequence=entry_sequence,
                event_sequence=accepted_events[0].event_sequence,
                turn_completed=True,
            )

    def confirm_explicit_subagent_result(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedExplicitSubagentResultSettlement,
        deadline_monotonic: float,
    ) -> ExplicitSubagentResultConfirmation:
        """Stateless FULL/NONE/CONFLICT confirmation for the exact composite."""

        tool = candidate.tool_result
        result_fact = candidate.result
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            entry = connection.execute(
                "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=%s",
                (guard.session_id, tool.result_entry_id),
            ).fetchone()
            tool_row = connection.execute(
                "SELECT * FROM pulsara_v3.tool_results WHERE session_id=%s AND id=%s",
                (guard.session_id, tool.result_id),
            ).fetchone()
            result = connection.execute(
                """SELECT * FROM pulsara_v3.subagent_task_children
                   WHERE session_id=%s AND id=%s AND child_kind='RESULT'""",
                (guard.session_id, result_fact.result_id),
            ).fetchone()
            task = connection.execute(
                "SELECT * FROM pulsara_v3.subagent_tasks WHERE session_id=%s AND id=%s",
                (guard.session_id, candidate.task_id),
            ).fetchone()
            turn = connection.execute(
                "SELECT * FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
                (guard.session_id, tool.turn_id),
            ).fetchone()
            subject = connection.execute(
                """SELECT block.tool_name, block.tool_arguments,
                          attempt.id AS attempt_id,
                          attempt.permission_snapshot_fingerprint
                   FROM pulsara_v3.assistant_message_blocks AS block
                   JOIN pulsara_v3.tool_execution_attempts AS attempt
                     ON attempt.session_id = block.session_id
                    AND attempt.assistant_entry_id = block.assistant_entry_id
                    AND attempt.tool_call_id = block.tool_call_id
                   WHERE block.session_id=%s
                     AND block.assistant_entry_id=%s
                     AND block.tool_call_id=%s""",
                (guard.session_id, tool.assistant_entry_id, tool.tool_call_id),
            ).fetchone()
            if entry is None and tool_row is None and result is None:
                if (
                    task is not None
                    and turn is not None
                    and subject is not None
                    and str(task["status"]) == "ACTIVE"
                    and int(task["execution_writer_generation"])
                    == guard.writer_generation
                    and str(turn["status"]) == "RUNNING"
                    and str(turn["workspace_id"]) == tool.workspace_id
                    and str(turn["conversation_scope_kind"])
                    == "SUBAGENT_TASK"
                    and str(turn["scope_subagent_task_id"])
                    == candidate.task_id
                    and str(subject["tool_name"]) == "report_agent_result"
                    and str(subject["attempt_id"]) == tool.attempt_id
                    and str(subject["permission_snapshot_fingerprint"])
                    == str(turn["permission_snapshot_fingerprint"])
                    and _explicit_result_arguments_match(
                        subject["tool_arguments"], result_fact
                    )
                ):
                    return ExplicitSubagentResultConfirmation(
                        ExplicitSubagentResultConfirmationKind.NONE
                    )
                return ExplicitSubagentResultConfirmation(
                    ExplicitSubagentResultConfirmationKind.CONFLICT
                )
            if any(
                value is None
                for value in (entry, tool_row, result, task, turn, subject)
            ):
                return ExplicitSubagentResultConfirmation(
                    ExplicitSubagentResultConfirmationKind.CONFLICT
                )
            assert entry is not None and tool_row is not None and result is not None
            assert task is not None and turn is not None
            assert subject is not None
            blob = tool.artifact_blob_descriptor
            sibling_count = int(
                connection.execute(
                    """SELECT count(*) FROM pulsara_v3.subagent_task_children
                       WHERE session_id=%s AND task_id=%s AND id<>%s""",
                    (guard.session_id, candidate.task_id, result_fact.result_id),
                ).fetchone()["count"]
            )
            matches = (
                str(entry["workspace_id"]) == tool.workspace_id
                and str(entry["turn_id"]) == tool.turn_id
                and str(entry["entry_kind"]) == EntryKind.TOOL_RESULT.value
                and str(entry["conversation_scope_kind"])
                == ConversationScopeKind.SUBAGENT_TASK.value
                and str(entry["scope_subagent_task_id"]) == candidate.task_id
                and self._content_from_row(entry) == tool.canonical_preview_content
                and str(tool_row["workspace_id"]) == tool.workspace_id
                and str(tool_row["result_origin_kind"]) == "PHYSICAL_ATTEMPT"
                and tool_row["control_plan_workflow_id"] is None
                and tool_row["control_plan_interaction_id"] is None
                and str(tool_row["tool_call_entry_id"]) == tool.assistant_entry_id
                and str(tool_row["tool_call_id"]) == tool.tool_call_id
                and tool_row["attempt_id"] == tool.attempt_id
                and str(tool_row["result_entry_id"]) == tool.result_entry_id
                and str(tool_row["result_state"]) == "SUCCESS"
                and str(tool_row["permission_snapshot_fingerprint"])
                == str(turn["permission_snapshot_fingerprint"])
                and str(tool_row["output_artifact_disposition"])
                == tool.artifact_disposition.value
                and tool_row["output_artifact_id"] == tool.artifact_id
                and tool_row["output_artifact_blob_id"]
                == (None if blob is None else blob.blob_id)
                and str(tool_row["output_source_coverage"])
                == tool.source_coverage.value
                and str(tool_row["output_display_kind"]) == tool.display_kind.value
                and tool_row["output_source_coverage_reason"]
                == (
                    None
                    if tool.source_coverage_reason is None
                    else tool.source_coverage_reason.value
                )
                and tool_row["output_artifact_unavailability_reason"]
                == (
                    None
                    if tool.artifact_unavailability_reason is None
                    else tool.artifact_unavailability_reason.value
                )
                and tuple(tool_row["model_visible_memory_fact_ids"])
                == tool.model_visible_memory_fact_ids
                and tool_row["observed_at"] == tool.observed_at
                and tool_row["observation_duration_microseconds"]
                == tool.observation_duration_microseconds
                and str(tool_row["observation_origin_kind"])
                == tool.observation_origin_kind.value
                and tool_row["tool_reported_duration_microseconds"]
                == tool.trusted_tool_reported_duration_microseconds
                and str(subject["tool_name"]) == "report_agent_result"
                and str(subject["attempt_id"]) == tool.attempt_id
                and str(subject["permission_snapshot_fingerprint"])
                == str(turn["permission_snapshot_fingerprint"])
                and _explicit_result_arguments_match(
                    subject["tool_arguments"], result_fact
                )
                and str(result["task_id"]) == candidate.task_id
                and str(result["entry_id"]) == result_fact.producer_entry_id
                and str(result["result_source"]) == "EXPLICIT"
                and str(result["summary"]) == result_fact.summary
                and result["output_preview"] == result_fact.output_preview
                and freeze_json(result["diagnostics"]) == result_fact.diagnostics
                and str(result["result_fingerprint"])
                == result_fact.result_fingerprint
                and int(result["child_ordinal"]) == sibling_count
                and str(task["status"]) == "COMPLETED"
                and int(task["execution_writer_generation"])
                == guard.writer_generation
                and task["terminal_reason"] is None
                and str(turn["status"]) == "COMPLETED"
                and str(turn["workspace_id"]) == tool.workspace_id
                and str(turn["conversation_scope_kind"]) == "SUBAGENT_TASK"
                and str(turn["scope_subagent_task_id"]) == candidate.task_id
                and str(turn["final_entry_id"]) == tool.result_entry_id
                and str(turn["terminal_reason"]) == "COMPLETED"
            )
            if not matches:
                return ExplicitSubagentResultConfirmation(
                    ExplicitSubagentResultConfirmationKind.CONFLICT
                )
            if blob is not None:
                try:
                    self._require_exact_tool_artifact_blob(
                        connection,
                        workspace_id=tool.workspace_id,
                        expected=blob,
                    )
                except ConversationKernelConflict:
                    return ExplicitSubagentResultConfirmation(
                        ExplicitSubagentResultConfirmationKind.CONFLICT
                    )
            try:
                tool_event = self._exact_event_for_confirmation(
                    connection,
                    tool.tool_result_occurrence,
                    session_id=tool.session_id,
                    workspace_id=tool.workspace_id,
                )
                for event in self._explicit_result_events(candidate):
                    self._exact_event_for_confirmation(
                        connection,
                        event,
                        session_id=tool.session_id,
                        workspace_id=tool.workspace_id,
                    )
            except ConversationKernelConflict:
                return ExplicitSubagentResultConfirmation(
                    ExplicitSubagentResultConfirmationKind.CONFLICT
                )
            return ExplicitSubagentResultConfirmation(
                ExplicitSubagentResultConfirmationKind.FULL,
                accepted_entry_id=tool.result_entry_id,
                entry_sequence=int(entry["entry_sequence"]),
                event_sequence=int(tool_event["event_sequence"]),
            )

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
                     AND status IN ('PENDING_START', 'WAITING_DEPENDENCY', 'ACTIVE')""",
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

    def start_subagent_turn(
        self,
        guard: HostWriterGuard,
        *,
        task_id: str,
        turn_id: str,
        entry_id: str,
        context_binding_revision_id: str,
        task_start_event_id: str,
        expected_parent_permission_snapshot: FrozenRunPermissionSnapshot,
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
            task_start_event_id=task_start_event_id,
            expected_parent_permission_snapshot=(
                expected_parent_permission_snapshot
            ),
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
            or prepared.task_start_event_id != task_start_event_id
            or prepared.expected_parent_permission_snapshot
            != expected_parent_permission_snapshot
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
            parent = connection.execute(
                "SELECT * FROM pulsara_v3.turns "
                "WHERE session_id=%s AND id=%s FOR SHARE",
                (guard.session_id, str(task["parent_turn_id"])),
            ).fetchone()
            if (
                parent is None
                or self._permission_from_row(parent)
                != prepared.expected_parent_permission_snapshot
            ):
                raise ConversationKernelConflict(
                    "subagent launch precondition drifted before turn admission"
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
            self._insert_initial_context_binding_revision(
                connection,
                session_id=guard.session_id,
                turn_id=turn_id,
                revision_id=context_binding_revision_id,
                initial_entry_sequence=entry_sequence,
                scope_kind=ConversationScopeKind.SUBAGENT_TASK,
                scope_subagent_task_id=task_id,
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
            task_start_event_id=candidate.task_start_event_id,
            expected_parent_permission_snapshot=(
                candidate.expected_parent_permission_snapshot
            ),
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
            start_event = connection.execute(
                "SELECT event_type, subject_subagent_task_id "
                "FROM pulsara_v3.agent_events "
                "WHERE session_id=%s AND event_id=%s",
                (candidate.session_id, candidate.task_start_event_id),
            ).fetchone()
            parent = (
                None
                if task is None
                else connection.execute(
                    "SELECT * FROM pulsara_v3.turns "
                    "WHERE session_id=%s AND id=%s",
                    (candidate.session_id, str(task["parent_turn_id"])),
                ).fetchone()
            )
            required = (turn, revision, entry, event)
            if (
                task is None
                or start_event is None
                or parent is None
                or not _canonical_content_matches_utf8_text(
                    candidate.content, str(task["objective"])
                )
                or str(start_event["event_type"])
                != CommittedEventType.SUBAGENT_TASK_STATUS_ACCEPTED.value
                or str(start_event["subject_subagent_task_id"])
                != candidate.task_id
                or self._permission_from_row(parent)
                != candidate.expected_parent_permission_snapshot
            ):
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.CONFLICT)
            if all(row is None for row in required):
                if str(task["status"]) != "ACTIVE":
                    return TurnAdmissionConfirmation(
                        TurnAdmissionConfirmationKind.CONFLICT
                    )
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
                and self._initial_context_binding_revision_matches(
                    connection,
                    row=revision,
                    session_id=candidate.session_id,
                    turn_id=candidate.turn_id,
                    revision_id=candidate.context_binding_revision_id,
                    initial_entry_sequence=int(entry["entry_sequence"]),
                    scope_kind=ConversationScopeKind.SUBAGENT_TASK,
                    scope_subagent_task_id=candidate.task_id,
                )
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
                SELECT t.id, t.workspace_id, t.batch_id, t.task_key, t.label,
                       t.profile_kind,
                       t.display_role, t.context_mode, t.context_last_n_turns,
                       t.parent_turn_id, t.objective, t.status, t.pending_reason,
                       t.terminal_reason, c.id AS result_id,
                       c.entry_id AS result_entry_id, c.result_source,
                       c.summary AS result_summary,
                       c.output_preview AS result_output_preview,
                       c.diagnostics AS result_diagnostics,
                       c.result_fingerprint,
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
        after_accepted_at: datetime | None = None,
        after_task_id: str | None = None,
        include_lookahead: bool = False,
    ) -> tuple[Mapping[str, object], ...]:
        if not 1 <= maximum_items <= 50:
            raise ValueError("subagent list bound is invalid")
        if (after_accepted_at is None) != (after_task_id is None):
            raise ValueError("subagent list keyset cursor is incomplete")
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    """
                    WITH inventory AS (
                        SELECT t.id, t.batch_id, t.task_key, t.label,
                               t.profile_kind, t.display_role, t.context_mode,
                               t.context_last_n_turns, t.parent_turn_id,
                               t.objective, t.status, t.pending_reason,
                               t.terminal_reason, t.accepted_at,
                               c.id AS result_id,
                               c.entry_id AS result_entry_id, c.result_source,
                               c.summary AS result_summary,
                               c.output_preview AS result_output_preview,
                               c.diagnostics AS result_diagnostics,
                               c.result_fingerprint,
                               accepted.id AS accepted_root_entry_id,
                               count(*) OVER () AS total_count
                        FROM pulsara_v3.subagent_tasks AS t
                        LEFT JOIN pulsara_v3.subagent_task_children AS c
                          ON c.session_id = t.session_id AND c.task_id = t.id
                         AND c.child_kind = 'RESULT'
                        LEFT JOIN pulsara_v3.transcript_entries AS accepted
                          ON accepted.session_id = c.session_id
                         AND accepted.source_subagent_result_id = c.id
                        WHERE t.session_id = %s
                    )
                    SELECT * FROM inventory
                    WHERE %s::timestamptz IS NULL
                       OR (accepted_at, id) > (%s::timestamptz, %s::text)
                    ORDER BY accepted_at, id LIMIT %s
                    """,
                    (
                        session_id,
                        after_accepted_at,
                        after_accepted_at,
                        after_task_id,
                        maximum_items + int(include_lookahead),
                    ),
                ).fetchall()
            )

    def read_subagent_task_board(
        self,
        *,
        session_id: str,
        deadline_monotonic: float,
    ) -> tuple[tuple[Mapping[str, object], ...], tuple[tuple[str, int], ...]]:
        """Freeze the bounded nonterminal task-board rows and exact counts."""

        statuses = ("ACTIVE", "PENDING_START", "WAITING_DEPENDENCY")
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            count_rows = connection.execute(
                """SELECT status, count(*) AS total
                   FROM pulsara_v3.subagent_tasks
                   WHERE session_id = %s AND status = ANY(%s)
                   GROUP BY status""",
                (session_id, list(statuses)),
            ).fetchall()
            observed = {str(row["status"]): int(row["total"]) for row in count_rows}
            rows = connection.execute(
                """SELECT t.id, t.task_key, t.label, t.objective, t.status,
                          t.accepted_at,
                          count(edge.dependency_task_id) AS dependency_total,
                          count(edge.dependency_task_id) FILTER (
                            WHERE dependency.status <> 'COMPLETED'
                          ) AS dependency_remaining
                   FROM pulsara_v3.subagent_tasks AS t
                   LEFT JOIN pulsara_v3.subagent_task_dependencies AS edge
                     ON edge.session_id = t.session_id AND edge.task_id = t.id
                   LEFT JOIN pulsara_v3.subagent_tasks AS dependency
                     ON dependency.session_id = edge.session_id
                    AND dependency.id = edge.dependency_task_id
                   WHERE t.session_id = %s AND t.status = ANY(%s)
                   GROUP BY t.id, t.task_key, t.label, t.objective,
                            t.status, t.accepted_at
                   ORDER BY CASE t.status
                              WHEN 'ACTIVE' THEN 0
                              WHEN 'PENDING_START' THEN 1
                              ELSE 2
                            END,
                            t.accepted_at, t.id
                   LIMIT 16""",
                (session_id, list(statuses)),
            ).fetchall()
            return (
                tuple(dict(row) for row in rows),
                tuple((status, observed.get(status, 0)) for status in statuses),
            )

    def read_subagent_dependencies(
        self,
        *,
        session_id: str,
        task_ids: tuple[str, ...],
        deadline_monotonic: float,
    ) -> tuple[Mapping[str, object], ...]:
        if not task_ids or len(task_ids) > 32:
            raise ValueError("subagent dependency read bound is invalid")
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    """SELECT edge.task_id, edge.dependency_task_id,
                              edge.dependency_ordinal, dependency.status,
                              dependency.task_key, dependency.label,
                              result.id AS result_id, result.result_source,
                              result.summary, result.result_fingerprint
                       FROM pulsara_v3.subagent_task_dependencies AS edge
                       JOIN pulsara_v3.subagent_tasks AS dependency
                         ON dependency.session_id = edge.session_id
                        AND dependency.id = edge.dependency_task_id
                       LEFT JOIN pulsara_v3.subagent_task_children AS result
                         ON result.session_id = dependency.session_id
                        AND result.task_id = dependency.id
                        AND result.child_kind = 'RESULT'
                       WHERE edge.session_id = %s AND edge.task_id = ANY(%s)
                       ORDER BY edge.task_id, edge.dependency_ordinal""",
                    (session_id, list(task_ids)),
                ).fetchall()
            )

    def list_runnable_subagent_tasks(
        self,
        guard: HostWriterGuard,
        *,
        maximum_items: int,
        deadline_monotonic: float,
    ) -> tuple[Mapping[str, object], ...]:
        if not 1 <= maximum_items <= 4:
            raise ValueError("subagent runnable read bound is invalid")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM pulsara_v3.subagent_tasks
                       WHERE session_id = %s
                         AND execution_writer_generation = %s
                         AND status = 'PENDING_START'
                       ORDER BY accepted_at, id LIMIT %s
                       FOR UPDATE SKIP LOCKED""",
                    (guard.session_id, guard.writer_generation, maximum_items),
                ).fetchall()
            )

    def settle_subagent_dependency_frontier(
        self,
        guard: HostWriterGuard,
        *,
        terminal_task_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> tuple[tuple[str, str], ...]:
        changed: list[tuple[str, str]] = []
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            terminal = connection.execute(
                """SELECT status FROM pulsara_v3.subagent_tasks
                   WHERE session_id = %s AND id = %s FOR SHARE""",
                (guard.session_id, terminal_task_id),
            ).fetchone()
            if terminal is None or str(terminal["status"]) not in {
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "INTERRUPTED",
                "BLOCKED_DEPENDENCY_FAILED",
            }:
                return ()
            frontier = [terminal_task_id]
            seen: set[str] = set()
            while frontier:
                upstream = frontier.pop(0)
                downstream_rows = connection.execute(
                    """SELECT task.id
                       FROM pulsara_v3.subagent_task_dependencies AS edge
                       JOIN pulsara_v3.subagent_tasks AS task
                         ON task.session_id = edge.session_id
                        AND task.id = edge.task_id
                       WHERE edge.session_id = %s
                         AND edge.dependency_task_id = %s
                         AND task.status = 'WAITING_DEPENDENCY'
                       ORDER BY task.accepted_at, task.id
                       FOR UPDATE OF task""",
                    (guard.session_id, upstream),
                ).fetchall()
                for downstream in downstream_rows:
                    task_id = str(downstream["id"])
                    if task_id in seen:
                        continue
                    seen.add(task_id)
                    dependencies = connection.execute(
                        """SELECT dependency.status, result.id AS result_id
                           FROM pulsara_v3.subagent_task_dependencies AS edge
                           JOIN pulsara_v3.subagent_tasks AS dependency
                             ON dependency.session_id = edge.session_id
                            AND dependency.id = edge.dependency_task_id
                           LEFT JOIN pulsara_v3.subagent_task_children AS result
                             ON result.session_id = dependency.session_id
                            AND result.task_id = dependency.id
                            AND result.child_kind = 'RESULT'
                           WHERE edge.session_id = %s AND edge.task_id = %s
                           ORDER BY edge.dependency_ordinal""",
                        (guard.session_id, task_id),
                    ).fetchall()
                    statuses = tuple(str(row["status"]) for row in dependencies)
                    if any(
                        value in {
                            "FAILED", "CANCELLED", "INTERRUPTED",
                            "BLOCKED_DEPENDENCY_FAILED",
                        }
                        for value in statuses
                    ):
                        new_status, pending, reason = (
                            "BLOCKED_DEPENDENCY_FAILED", None, "DEPENDENCY_FAILED"
                        )
                        frontier.append(task_id)
                    elif all(
                        value == "COMPLETED" and row["result_id"] is not None
                        for value, row in zip(statuses, dependencies, strict=True)
                    ):
                        new_status, pending, reason = "PENDING_START", "CAPACITY", None
                    elif any(
                        value == "COMPLETED" and row["result_id"] is None
                        for value, row in zip(statuses, dependencies, strict=True)
                    ):
                        new_status, pending, reason = (
                            "FAILED", None, "DEPENDENCY_RESULT_INVARIANT"
                        )
                        frontier.append(task_id)
                    else:
                        continue
                    row = connection.execute(
                        """UPDATE pulsara_v3.subagent_tasks
                           SET status = %s, pending_reason = %s,
                               terminal_reason = %s,
                               terminal_at = CASE WHEN %s THEN clock_timestamp()
                                                  ELSE NULL END
                           WHERE session_id = %s AND id = %s
                             AND status = 'WAITING_DEPENDENCY'
                           RETURNING workspace_id""",
                        (
                            new_status,
                            pending,
                            reason,
                            SubagentTaskStatus(new_status).terminal,
                            guard.session_id,
                            task_id,
                        ),
                    ).fetchone()
                    if row is None:
                        continue
                    changed.append((task_id, new_status))
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
                                payload={"status": new_status, "reason": reason},
                            ),
                        ),
                    )
            return tuple(changed)

    def accept_inter_agent_mailbox_batch(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedInterAgentMailboxBatch,
        deadline_monotonic: float,
    ) -> tuple[str, ...]:
        """Append one exact process-local mailbox prefix to the child transcript."""

        items = candidate.items
        first = items[0]
        if (
            first.session_id != guard.session_id
            or any(
                item.session_id != first.session_id
                or item.recipient_task_id != first.recipient_task_id
                or item.recipient_turn_id != first.recipient_turn_id
                for item in items
            )
            or tuple(item.ordinal for item in items)
            != tuple(range(items[0].ordinal, items[0].ordinal + len(items)))
        ):
            raise ValueError("inter-agent mailbox batch identity is invalid")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            target = connection.execute(
                """SELECT t.workspace_id, t.status, turn.status AS turn_status,
                          turn.conversation_scope_kind, turn.scope_subagent_task_id
                   FROM pulsara_v3.subagent_tasks AS t
                   JOIN pulsara_v3.turns AS turn
                     ON turn.session_id = t.session_id
                    AND turn.id = %s
                   WHERE t.session_id = %s AND t.id = %s
                   FOR UPDATE OF t, turn""",
                (
                    first.recipient_turn_id,
                    guard.session_id,
                    first.recipient_task_id,
                ),
            ).fetchone()
            if (
                target is None
                or str(target["status"]) != "ACTIVE"
                or str(target["turn_status"]) != "RUNNING"
                or str(target["conversation_scope_kind"]) != "SUBAGENT_TASK"
                or str(target["scope_subagent_task_id"]) != first.recipient_task_id
            ):
                raise ConversationKernelConflict(
                    "inter-agent target is no longer active"
                )
            events: list[CommittedEventDraft] = []
            entry_ids: list[str] = []
            for item in items:
                attempt = connection.execute(
                    """SELECT entry.turn_id, entry.conversation_scope_kind,
                              block.tool_name, block.tool_arguments,
                              attempt.tool_call_id
                       FROM pulsara_v3.tool_execution_attempts AS attempt
                       JOIN pulsara_v3.assistant_message_blocks AS block
                         ON block.session_id = attempt.session_id
                        AND block.assistant_entry_id = attempt.assistant_entry_id
                        AND block.tool_call_id = attempt.tool_call_id
                       JOIN pulsara_v3.transcript_entries AS entry
                         ON entry.session_id = attempt.session_id
                        AND entry.id = attempt.assistant_entry_id
                       WHERE attempt.session_id = %s AND attempt.id = %s""",
                    (guard.session_id, item.sender_tool_attempt_id),
                ).fetchone()
                if (
                    attempt is None
                    or str(attempt["turn_id"]) != item.sender_turn_id
                    or str(attempt["conversation_scope_kind"]) != "ROOT"
                    or str(attempt["tool_name"]) != "send_agent_message"
                    or str(attempt["tool_call_id"]) != item.sender_tool_call_id
                    or dict(attempt["tool_arguments"])
                    != {
                        "task_id": item.recipient_task_id,
                        "message": item.message,
                    }
                ):
                    raise ConversationKernelConflict(
                        "inter-agent source attempt does not exact-join"
                    )
                content = InlineContent.from_bytes(item.message.encode("utf-8"))
                if content.digest != item.message_digest:
                    raise ConversationKernelConflict("inter-agent message digest drifted")
                sequence = self._allocate_entry_sequence(connection, guard.session_id)
                self._insert_entry(
                    connection,
                    session_id=guard.session_id,
                    workspace_id=str(target["workspace_id"]),
                    turn_id=item.recipient_turn_id,
                    entry_id=item.entry_id,
                    entry_sequence=sequence,
                    entry_kind=EntryKind.INTER_AGENT_MESSAGE,
                    scope_kind=ConversationScopeKind.SUBAGENT_TASK,
                    scope_task_id=item.recipient_task_id,
                    content=content,
                    source_inter_agent_tool_attempt_id=item.sender_tool_attempt_id,
                )
                events.append(
                    CommittedEventDraft(
                        event_id=item.event_id,
                        event_type=CommittedEventType.INTER_AGENT_MESSAGE_ACCEPTED,
                        subject=CommittedEventSubject(SubjectSlot.ENTRY, item.entry_id),
                        actor_kind="runtime",
                        actor_id=candidate.actor_id,
                        sensitivity_class="PUBLIC",
                        projection_profile="DEFAULT",
                        occurred_at=candidate.occurred_at,
                        payload={
                            "recipient_task_id": item.recipient_task_id,
                            "message_ordinal": item.ordinal,
                        },
                    )
                )
                entry_ids.append(item.entry_id)
            self._append_events(
                connection,
                guard,
                workspace_id=str(target["workspace_id"]),
                drafts=tuple(events),
            )
            return tuple(entry_ids)

    def confirm_inter_agent_mailbox_batch(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedInterAgentMailboxBatch,
        deadline_monotonic: float,
    ) -> str:
        items = candidate.items
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            rows = connection.execute(
                """SELECT entry.*, event.event_id, event.event_type,
                          event.actor_kind, event.actor_id, event.occurred_at,
                          event.payload
                   FROM pulsara_v3.transcript_entries AS entry
                   LEFT JOIN pulsara_v3.agent_events AS event
                     ON event.session_id = entry.session_id
                    AND event.subject_entry_id = entry.id
                    AND event.event_type = 'InterAgentMessageAccepted'
                   WHERE entry.session_id = %s AND entry.id = ANY(%s)""",
                (guard.session_id, [item.entry_id for item in items]),
            ).fetchall()
            if not rows:
                return "NONE"
            if len(rows) != len(items):
                return "CONFLICT"
            by_id = {str(row["id"]): row for row in rows}
            for item in items:
                row = by_id.get(item.entry_id)
                expected_payload = {
                    "recipient_task_id": item.recipient_task_id,
                    "message_ordinal": item.ordinal,
                }
                if (
                    row is None
                    or str(row["turn_id"]) != item.recipient_turn_id
                    or str(row["entry_kind"]) != "INTER_AGENT_MESSAGE"
                    or str(row["conversation_scope_kind"]) != "SUBAGENT_TASK"
                    or str(row["scope_subagent_task_id"]) != item.recipient_task_id
                    or str(row["source_inter_agent_tool_attempt_id"])
                    != item.sender_tool_attempt_id
                    or str(row["content_digest"]) != item.message_digest
                    or self._content_from_row(row)
                    != InlineContent.from_bytes(item.message.encode("utf-8"))
                    or str(row["event_id"]) != item.event_id
                    or str(row["event_type"]) != "InterAgentMessageAccepted"
                    or str(row["actor_kind"]) != "runtime"
                    or str(row["actor_id"]) != candidate.actor_id
                    or row["occurred_at"] != candidate.occurred_at
                    or dict(row["payload"]) != expected_payload
                ):
                    return "CONFLICT"
            return "FULL"
