"""Canonical conversation and turn operations."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence
from psycopg import IsolationLevel
from psycopg.rows import dict_row
from pulsara_agent.conversation_kernel.contracts import AssistantBlockKind, CanonicalContent, CommittedEventDraft, CommittedEventSubject, ConversationScopeKind, EntryKind, HostWriterGuard, InlineContent, TurnStatus
from pulsara_agent.model_input.contracts import PreparedProviderInputCut
from pulsara_agent.ports.terminal_observation import ExistingTurnInstallation, NewTurnInstallation, TerminalObservationInstallationAttempt
from pulsara_agent.primitives.context import FrozenJsonObjectFact, freeze_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import RunPermissionAdmissionSource
from pulsara_agent.primitives.plan_workflow import PlanWorkflowStatus
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType, SubjectSlot
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .contracts import (
    AcceptedEntry,
    AssistantBlock,
    AssistantDataBlock,
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    PreparedRootTurnAdmission,
    StaleHostWriter,
    TurnAdmissionConfirmation,
    TurnAdmissionConfirmationKind,
    _content_columns,
    _stable_identity,
    _stable_subagent_message_child_id,
    _utcnow,
    build_prepared_root_turn_admission,
)

from .matching import (
    _event_row_matches_draft,
)

class _ConversationOperations:
    def start_root_turn(
        self,
        guard: HostWriterGuard,
        *,
        command_id: str,
        turn_id: str,
        entry_id: str,
        context_binding_revision_id: str,
        permission_snapshot_id: str,
        requested_permission_mode: PermissionMode,
        content: CanonicalContent,
        occurred_at: datetime,
        actor_kind: str = "human",
        actor_id: str = "user",
        deadline_monotonic: float,
        _prepared_candidate: PreparedRootTurnAdmission | None = None,
    ) -> AcceptedEntry:
        prepared = _prepared_candidate or build_prepared_root_turn_admission(
            session_id=guard.session_id,
            command_id=command_id,
            turn_id=turn_id,
            entry_id=entry_id,
            context_binding_revision_id=context_binding_revision_id,
            permission_snapshot_id=permission_snapshot_id,
            requested_permission_mode=requested_permission_mode,
            content=content,
            occurred_at=occurred_at,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if (
            prepared.session_id != guard.session_id
            or prepared.command_id != command_id
            or prepared.turn_id != turn_id
            or prepared.entry_id != entry_id
            or prepared.context_binding_revision_id
            != context_binding_revision_id
            or prepared.permission_snapshot_id != permission_snapshot_id
            or prepared.requested_permission_mode is not requested_permission_mode
            or prepared.content != content
            or prepared.occurred_at != occurred_at
            or prepared.actor_kind != actor_kind
            or prepared.actor_id != actor_id
        ):
            raise ValueError("prepared ROOT admission does not exact-join arguments")
        semantic_digest = prepared.semantic_digest
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            existing = connection.execute(
                """
                SELECT command_kind, semantic_digest, target_turn_id
                FROM pulsara_v3.session_commands
                WHERE session_id = %s AND command_id = %s
                """,
                (guard.session_id, command_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["command_kind"] != "SUBMIT_PROMPT"
                    or existing["semantic_digest"] != semantic_digest
                    or existing["target_turn_id"] != turn_id
                ):
                    raise ConversationKernelConflict("command identity conflict")
                return self._accepted_entry(connection, guard.session_id, entry_id)
            self._require_root_admission_open(connection, session_id=guard.session_id)
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            workspace_id = self._workspace_id(connection, guard.session_id)
            permission = self._freeze_root_permission_snapshot(
                connection,
                session_id=guard.session_id,
                snapshot_id=permission_snapshot_id,
                requested_mode=requested_permission_mode,
                admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
            )
            handoff = self._eligible_plan_handoff(
                connection, session_id=guard.session_id
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
                workspace_id=workspace_id,
                turn_id=turn_id,
                entry_id=entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.USER_MESSAGE,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=content,
                source_plan_workflow_id=(
                    None if handoff is None else handoff.workflow_id
                ),
                source_plan_interaction_id=(
                    None if handoff is None else handoff.interaction_id
                ),
                source_plan_handoff_kind=(None if handoff is None else handoff.kind),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_turn_id
                ) VALUES (%s, %s, 'SUBMIT_PROMPT',
                          'submit_prompt.v1', %s, 'TURN', %s)
                """,
                (guard.session_id, command_id, semantic_digest, turn_id),
            )
            event = self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(prepared.event,),
            )[0]
            return AcceptedEntry(
                entry_id=entry_id,
                turn_id=turn_id,
                entry_sequence=entry_sequence,
                event_sequence=event.event_sequence,
            )

    def accept_root_turn(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedRootTurnAdmission,
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        if candidate.session_id != guard.session_id:
            raise ValueError("prepared ROOT admission belongs to another session")
        return self.start_root_turn(
            guard,
            command_id=candidate.command_id,
            turn_id=candidate.turn_id,
            entry_id=candidate.entry_id,
            context_binding_revision_id=candidate.context_binding_revision_id,
            permission_snapshot_id=candidate.permission_snapshot_id,
            requested_permission_mode=candidate.requested_permission_mode,
            content=candidate.content,
            occurred_at=candidate.occurred_at,
            actor_kind=candidate.actor_kind,
            actor_id=candidate.actor_id,
            deadline_monotonic=deadline_monotonic,
            _prepared_candidate=candidate,
        )

    def confirm_root_turn_admission(
        self,
        *,
        candidate: PreparedRootTurnAdmission,
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
                    raise ValueError("ROOT admission guard belongs to another session")
                self._require_writer(connection, guard, lock=False)
            command = connection.execute(
                """SELECT * FROM pulsara_v3.session_commands
                   WHERE session_id = %s AND command_id = %s""",
                (candidate.session_id, candidate.command_id),
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
            rows = (command, turn, revision, entry, event)
            if all(row is None for row in rows):
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.NONE)
            if any(row is None for row in rows):
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.CONFLICT)
            assert command is not None and turn is not None and revision is not None
            assert entry is not None and event is not None
            try:
                permission = self._permission_from_row(turn)
            except (TypeError, ValueError):
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.CONFLICT)
            matches = (
                str(command["command_kind"]) == "SUBMIT_PROMPT"
                and str(command["semantic_digest"]) == candidate.semantic_digest
                and str(command["target_kind"]) == "TURN"
                and str(command["target_turn_id"]) == candidate.turn_id
                and str(turn["conversation_scope_kind"]) == "ROOT"
                and turn["scope_subagent_task_id"] is None
                and str(turn["initial_entry_id"]) == candidate.entry_id
                and str(turn["current_context_binding_revision_id"])
                == candidate.context_binding_revision_id
                and permission.snapshot_id == candidate.permission_snapshot_id
                and permission.requested_mode is candidate.requested_permission_mode
                and int(revision["revision_ordinal"]) == 0
                and str(revision["base_kind"]) == "FULL_HISTORY"
                and revision["context_snapshot_id"] is None
                and int(revision["source_through_sequence"])
                == int(entry["entry_sequence"]) - 1
                and str(entry["turn_id"]) == candidate.turn_id
                and str(entry["entry_kind"]) == EntryKind.USER_MESSAGE.value
                and str(entry["conversation_scope_kind"]) == "ROOT"
                and entry["scope_subagent_task_id"] is None
                and self._content_from_row(entry) == candidate.content
                and _event_row_matches_draft(event, candidate.event)
            )
            if not matches:
                return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.CONFLICT)
            return TurnAdmissionConfirmation(
                TurnAdmissionConfirmationKind.FULL,
                self._accepted_entry(connection, candidate.session_id, candidate.entry_id),
            )

    def prepare_provider_input_cut(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        deadline_monotonic: float,
    ) -> PreparedProviderInputCut:
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            row = connection.execute(
                """
                SELECT t.current_context_binding_revision_id,
                       s.latest_entry_sequence
                FROM pulsara_v3.turns AS t
                JOIN pulsara_v3.sessions AS s ON s.id = t.session_id
                WHERE t.session_id = %s AND t.id = %s AND t.status = 'RUNNING'
                """,
                (guard.session_id, turn_id),
            ).fetchone()
            if row is None:
                raise ConversationKernelConflict("turn is not running")
            return PreparedProviderInputCut(
                session_id=guard.session_id,
                turn_id=turn_id,
                context_binding_revision_id=str(
                    row["current_context_binding_revision_id"]
                ),
                provider_input_through_sequence=int(row["latest_entry_sequence"]),
            )

    def require_provider_safe_turn(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        deadline_monotonic: float,
    ) -> None:
        """Prove the canonical half of the provider safe-point predicate."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            self._require_provider_safe_turn_in_transaction(
                connection,
                session_id=guard.session_id,
                turn_id=turn_id,
                lock=False,
            )

    def accept_terminal_observation(
        self,
        guard: HostWriterGuard,
        *,
        candidate: TerminalObservationInstallationAttempt,
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        """Atomically accept one same-Host Terminal observation.

        The immutable process-local candidate is the only retry identity.  A
        successful transaction installs the entry, an optional new ROOT turn
        and its revision zero, plus the selective occurrence together.
        """

        if candidate.session_id != guard.session_id:
            raise ValueError("terminal observation belongs to another session")
        if candidate.writer_generation != guard.writer_generation:
            raise StaleHostWriter("terminal observation writer generation is stale")
        content = InlineContent.from_bytes(
            candidate.content.canonical_bytes(),
            media_type="application/vnd.pulsara.terminal-observation+json",
            codec="utf-8",
        )
        if content.digest != candidate.content_digest:
            raise ValueError("terminal observation content digest conflicts")
        target = candidate.target
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            workspace_id = self._workspace_id(connection, guard.session_id)
            if workspace_id != candidate.workspace_id:
                raise ConversationKernelConflict(
                    "terminal observation workspace conflicts"
                )
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            if isinstance(target, ExistingTurnInstallation):
                turn = self._require_provider_safe_turn_in_transaction(
                    connection,
                    session_id=guard.session_id,
                    turn_id=target.turn_id,
                    lock=True,
                )
                if (
                    str(turn["workspace_id"]) != workspace_id
                    or str(turn["conversation_scope_kind"])
                    != ConversationScopeKind.ROOT.value
                ):
                    raise ConversationKernelConflict(
                        "terminal observation target is not a ROOT turn"
                    )
                turn_id = target.turn_id
                entry_id = target.entry_id
            elif isinstance(target, NewTurnInstallation):
                self._require_root_admission_open(
                    connection, session_id=guard.session_id
                )
                running = connection.execute(
                    """
                    SELECT id FROM pulsara_v3.turns
                    WHERE session_id = %s AND conversation_scope_kind = 'ROOT'
                      AND status = 'RUNNING'
                    LIMIT 1
                    """,
                    (guard.session_id,),
                ).fetchone()
                if running is not None:
                    raise ConversationKernelConflict(
                        "idle terminal observation has a running ROOT turn"
                    )
                turn_id = target.turn_id
                entry_id = target.initial_entry_id
                origin_turn = connection.execute(
                    """
                    SELECT * FROM pulsara_v3.turns
                    WHERE session_id = %s AND id = %s
                    """,
                    (guard.session_id, candidate.origin_turn_id),
                ).fetchone()
                if origin_turn is None:
                    raise ConversationKernelConflict(
                        "terminal observation origin turn is absent"
                    )
                origin_permission = self._permission_from_row(origin_turn)
                permission = self._freeze_root_permission_snapshot(
                    connection,
                    session_id=guard.session_id,
                    snapshot_id=_stable_identity("permission-snapshot", turn_id),
                    requested_mode=origin_permission.effective_mode,
                    admission_source=(
                        RunPermissionAdmissionSource.TERMINAL_OBSERVATION
                    ),
                    inherited_from_turn_id=candidate.origin_turn_id,
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.turns (
                        id, session_id, workspace_id, conversation_scope_kind,
                        status, initial_entry_id,
                        current_context_binding_revision_id,
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
                        target.context_binding_revision_id,
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
                        target.context_binding_revision_id,
                        guard.session_id,
                        turn_id,
                        entry_sequence - 1,
                    ),
                )
            else:  # pragma: no cover - closed union exhaustiveness
                raise TypeError("terminal observation installation target is unknown")
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=workspace_id,
                turn_id=turn_id,
                entry_id=entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.TERMINAL_OBSERVATION,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=content,
            )
            event = self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(self._terminal_observation_event(candidate, entry_id),),
            )[0]
            return AcceptedEntry(
                entry_id=entry_id,
                turn_id=turn_id,
                entry_sequence=entry_sequence,
                event_sequence=event.event_sequence,
            )

    def confirm_terminal_observation_winner(
        self,
        guard: HostWriterGuard,
        *,
        candidate: TerminalObservationInstallationAttempt,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Stateless exact confirmation for an ambiguous observation ACK."""

        if candidate.session_id != guard.session_id:
            raise ValueError("terminal observation belongs to another session")
        content = InlineContent.from_bytes(
            candidate.content.canonical_bytes(),
            media_type="application/vnd.pulsara.terminal-observation+json",
            codec="utf-8",
        )
        if content.digest != candidate.content_digest:
            raise ValueError("terminal observation content digest conflicts")
        target = candidate.target
        entry_id = (
            target.entry_id
            if isinstance(target, ExistingTurnInstallation)
            else target.initial_entry_id
        )
        turn_id = target.turn_id
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
                (guard.session_id, entry_id),
            ).fetchone()
            event_id = _stable_identity(
                "event",
                candidate.content.observation_id,
                CommittedEventType.TERMINAL_OBSERVATION_ACCEPTED.value,
            )
            event_rows = connection.execute(
                "SELECT * FROM pulsara_v3.agent_events WHERE event_id = %s",
                (event_id,),
            ).fetchall()
            if entry is None and not event_rows:
                return None
            if entry is None or len(event_rows) != 1:
                raise ConversationKernelConflict(
                    "terminal observation winner is partially installed"
                )
            if (
                str(entry["workspace_id"]) != candidate.workspace_id
                or str(entry["turn_id"]) != turn_id
                or str(entry["entry_kind"]) != EntryKind.TERMINAL_OBSERVATION.value
                or str(entry["conversation_scope_kind"])
                != ConversationScopeKind.ROOT.value
                or entry["scope_subagent_task_id"] is not None
                or entry["context_binding_revision_id"] is not None
                or entry["provider_input_through_sequence"] is not None
                or self._content_from_row(entry) != content
            ):
                raise ConversationKernelConflict(
                    "terminal observation identity names a different entry"
                )
            event = self._exact_event_for_confirmation(
                connection,
                self._terminal_observation_event(candidate, entry_id),
                session_id=guard.session_id,
                workspace_id=candidate.workspace_id,
            )
            turn = connection.execute(
                """
                SELECT * FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s
                """,
                (guard.session_id, turn_id),
            ).fetchone()
            if turn is None:
                raise ConversationKernelConflict("terminal observation turn is absent")
            if isinstance(target, NewTurnInstallation):
                revision = connection.execute(
                    """
                    SELECT * FROM pulsara_v3.turn_context_binding_revisions
                    WHERE session_id = %s AND id = %s
                    """,
                    (guard.session_id, target.context_binding_revision_id),
                ).fetchone()
                if (
                    str(turn["initial_entry_id"]) != target.initial_entry_id
                    or str(turn["current_context_binding_revision_id"])
                    != target.context_binding_revision_id
                    or revision is None
                    or str(revision["turn_id"]) != target.turn_id
                    or int(revision["revision_ordinal"]) != 0
                    or str(revision["base_kind"]) != "FULL_HISTORY"
                    or int(revision["source_through_sequence"])
                    != int(entry["entry_sequence"]) - 1
                ):
                    raise ConversationKernelConflict(
                        "terminal observation genesis differs from candidate"
                    )
            return AcceptedEntry(
                entry_id=entry_id,
                turn_id=turn_id,
                entry_sequence=int(entry["entry_sequence"]),
                event_sequence=int(event["event_sequence"]),
            )

    def adopt_context_snapshot(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        snapshot_id: str,
        context_binding_revision_id: str,
        source_through_sequence: int,
        source_digest: str,
        compiler_contract: str,
        prompt_contract: str,
        model_contract: str,
        content: CanonicalContent,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> int:
        """Install one immutable mid-turn binding revision.

        The process-local safe-point coordinator owns exclusion from a model
        operation.  This transaction independently rechecks the canonical
        predicate and exact current revision before advancing the pointer.
        """

        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            turn = connection.execute(
                """
                SELECT t.workspace_id, t.current_context_binding_revision_id,
                       current.revision_ordinal,
                       current.source_through_sequence AS current_source_cut,
                       initial_entry.entry_sequence AS initial_entry_sequence
                FROM pulsara_v3.turns AS t
                JOIN pulsara_v3.turn_context_binding_revisions AS current
                  ON current.session_id = t.session_id
                 AND current.id = t.current_context_binding_revision_id
                JOIN pulsara_v3.transcript_entries AS initial_entry
                  ON initial_entry.session_id = t.session_id
                 AND initial_entry.id = t.initial_entry_id
                WHERE t.session_id = %s AND t.id = %s AND t.status = 'RUNNING'
                FOR UPDATE OF t
                """,
                (guard.session_id, turn_id),
            ).fetchone()
            if turn is None:
                raise ConversationKernelConflict("snapshot target turn is not running")
            missing = connection.execute(
                """
                SELECT 1
                FROM pulsara_v3.assistant_message_blocks AS b
                LEFT JOIN pulsara_v3.tool_results AS r
                  ON r.session_id = b.session_id
                 AND r.tool_call_entry_id = b.assistant_entry_id
                 AND r.tool_call_id = b.tool_call_id
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = b.session_id AND e.id = b.assistant_entry_id
                WHERE e.session_id = %s AND e.turn_id = %s
                  AND b.block_kind = 'TOOL_CALL' AND r.id IS NULL
                LIMIT 1
                """,
                (guard.session_id, turn_id),
            ).fetchone()
            if missing is not None:
                raise ConversationKernelConflict("tool request is not terminal")
            if source_through_sequence < int(
                turn["current_source_cut"]
            ) or source_through_sequence >= int(turn["initial_entry_sequence"]):
                raise ConversationKernelConflict("snapshot source range is invalid")
            revision_ordinal = int(turn["revision_ordinal"]) + 1
            connection.execute(
                """
                INSERT INTO pulsara_v3.context_snapshots (
                    id, session_id, workspace_id, source_through_sequence,
                    source_digest, compiler_contract, prompt_contract,
                    model_contract, inline_content, blob_id, content_digest,
                    content_size, content_media_type, content_codec
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s)
                """,
                (
                    snapshot_id,
                    guard.session_id,
                    turn["workspace_id"],
                    source_through_sequence,
                    source_digest,
                    compiler_contract,
                    prompt_contract,
                    model_contract,
                    *_content_columns(content),
                ),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.turn_context_binding_revisions (
                    id, session_id, turn_id, revision_ordinal,
                    base_kind, context_snapshot_id, source_through_sequence
                ) VALUES (%s, %s, %s, %s, 'SNAPSHOT', %s, %s)
                """,
                (
                    context_binding_revision_id,
                    guard.session_id,
                    turn_id,
                    revision_ordinal,
                    snapshot_id,
                    source_through_sequence,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.turns
                SET current_context_binding_revision_id = %s
                WHERE session_id = %s AND id = %s
                """,
                (context_binding_revision_id, guard.session_id, turn_id),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=str(turn["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.COMPACTION_ADOPTED,
                        SubjectSlot.CONTEXT_BINDING_REVISION,
                        context_binding_revision_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"revision_ordinal": revision_ordinal},
                    ),
                ),
            )
            return revision_ordinal

    def commit_assistant_message(
        self,
        guard: HostWriterGuard,
        *,
        cut: PreparedProviderInputCut,
        entry_id: str,
        parent_content: CanonicalContent,
        blocks: Sequence[AssistantBlock],
        complete_turn: bool = False,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        if cut.session_id != guard.session_id:
            raise ValueError("prepared input cut belongs to another session")
        if not blocks:
            raise ValueError("assistant message requires at least one block")
        tool_request = any(isinstance(item, AssistantToolCallBlock) for item in blocks)
        if complete_turn and tool_request:
            raise ValueError("a tool-request message cannot complete its turn")
        event_type = (
            CommittedEventType.ASSISTANT_TOOL_REQUEST_ACCEPTED
            if tool_request
            else CommittedEventType.ASSISTANT_MESSAGE_ACCEPTED
        )
        entry_kind = (
            EntryKind.ASSISTANT_TOOL_REQUEST
            if tool_request
            else EntryKind.ASSISTANT_MESSAGE
        )
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            turn = connection.execute(
                """
                SELECT workspace_id, conversation_scope_kind,
                       scope_subagent_task_id, current_context_binding_revision_id
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s AND status = 'RUNNING'
                FOR UPDATE
                """,
                (guard.session_id, cut.turn_id),
            ).fetchone()
            if (
                turn is None
                or str(turn["current_context_binding_revision_id"])
                != cut.context_binding_revision_id
            ):
                raise ConversationKernelConflict("prepared input cut is stale")
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            if cut.provider_input_through_sequence >= entry_sequence:
                raise ConversationKernelConflict("provider input cut is not historical")
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=str(turn["workspace_id"]),
                turn_id=cut.turn_id,
                entry_id=entry_id,
                entry_sequence=entry_sequence,
                entry_kind=entry_kind,
                scope_kind=ConversationScopeKind(str(turn["conversation_scope_kind"])),
                scope_task_id=turn["scope_subagent_task_id"],
                content=parent_content,
                context_binding_revision_id=cut.context_binding_revision_id,
                provider_input_through_sequence=cut.provider_input_through_sequence,
            )
            for ordinal, block in enumerate(blocks):
                self._insert_assistant_block(
                    connection,
                    session_id=guard.session_id,
                    workspace_id=str(turn["workspace_id"]),
                    entry_id=entry_id,
                    ordinal=ordinal,
                    block=block,
                )
            subagent_message: tuple[str, int, str] | None = None
            if (
                str(turn["conversation_scope_kind"])
                == ConversationScopeKind.SUBAGENT_TASK.value
            ):
                task_id = str(turn["scope_subagent_task_id"])
                message_ordinal = int(
                    connection.execute(
                        """
                        SELECT count(*) AS total
                        FROM pulsara_v3.subagent_task_children
                        WHERE session_id = %s AND task_id = %s
                          AND child_kind = 'MESSAGE'
                        """,
                        (guard.session_id, task_id),
                    ).fetchone()["total"]
                )
                child_id = _stable_subagent_message_child_id(task_id, entry_id)
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.subagent_task_children (
                        id, session_id, task_id, child_kind,
                        child_ordinal, entry_id
                    ) VALUES (%s, %s, %s, 'MESSAGE', %s, %s)
                    """,
                    (
                        child_id,
                        guard.session_id,
                        task_id,
                        message_ordinal,
                        entry_id,
                    ),
                )
                subagent_message = (child_id, message_ordinal, task_id)
            event_drafts = [
                self._event(
                    event_type,
                    SubjectSlot.ENTRY,
                    entry_id,
                    occurred_at=occurred_at,
                    actor_kind="model",
                    actor_id=actor_id,
                    payload={
                        "entry_kind": entry_kind.value,
                        "block_count": len(blocks),
                    },
                )
            ]
            if subagent_message is not None:
                child_id, message_ordinal, task_id = subagent_message
                event_drafts.append(
                    self._event(
                        CommittedEventType.SUBAGENT_MESSAGE_ACCEPTED,
                        SubjectSlot.SUBAGENT_MESSAGE,
                        child_id,
                        occurred_at=occurred_at,
                        actor_kind="subagent",
                        actor_id=task_id,
                        payload={"child_ordinal": message_ordinal},
                    )
                )
            pending_steer = False
            if complete_turn:
                pending_steer = bool(
                    connection.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM pulsara_v3.prompt_queue_items
                            WHERE session_id = %s AND status = 'PENDING'
                              AND delivery_mode = 'STEER_ACTIVE_TURN'
                              AND target_turn_id = %s
                        ) AS present
                        """,
                        (guard.session_id, cut.turn_id),
                    ).fetchone()["present"]
                )
            turn_completed = complete_turn and not pending_steer
            if turn_completed:
                terminal = connection.execute(
                    """
                    UPDATE pulsara_v3.turns
                    SET status = 'COMPLETED', final_entry_id = %s,
                        terminal_reason = 'COMPLETED',
                        terminal_at = clock_timestamp()
                    WHERE session_id = %s AND id = %s AND status = 'RUNNING'
                    RETURNING id
                    """,
                    (entry_id, guard.session_id, cut.turn_id),
                ).fetchone()
                if terminal is None:
                    raise ConversationKernelConflict("turn has a terminal winner")
                event_drafts.append(
                    self._event(
                        CommittedEventType.TURN_COMPLETED,
                        SubjectSlot.TURN,
                        cut.turn_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id="foreground-runner",
                        payload={"final_entry_id": entry_id},
                    )
                )
            event = self._append_events(
                connection,
                guard,
                workspace_id=str(turn["workspace_id"]),
                drafts=tuple(event_drafts),
            )[0]
            return AcceptedEntry(
                entry_id=entry_id,
                turn_id=cut.turn_id,
                entry_sequence=entry_sequence,
                event_sequence=event.event_sequence,
                turn_completed=turn_completed,
            )

    def confirm_assistant_message_winner(
        self,
        guard: HostWriterGuard,
        *,
        cut: PreparedProviderInputCut,
        entry_id: str,
        parent_content: CanonicalContent,
        blocks: Sequence[AssistantBlock],
        complete_turn: bool,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Exact-confirm a stable assistant candidate after an unknown ACK.

        This is a read of canonical rows and their accepted occurrence.  It is
        neither a second write nor a confirmation receipt.
        """

        tool_request = any(isinstance(item, AssistantToolCallBlock) for item in blocks)
        expected_entry_kind = (
            EntryKind.ASSISTANT_TOOL_REQUEST
            if tool_request
            else EntryKind.ASSISTANT_MESSAGE
        )
        expected_event_type = (
            CommittedEventType.ASSISTANT_TOOL_REQUEST_ACCEPTED
            if tool_request
            else CommittedEventType.ASSISTANT_MESSAGE_ACCEPTED
        )
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            rows = connection.execute(
                """
                SELECT e.*, a.event_sequence, a.event_type,
                       a.actor_kind, a.actor_id, a.occurred_at, a.payload
                FROM pulsara_v3.transcript_entries AS e
                JOIN pulsara_v3.agent_events AS a
                  ON a.session_id = e.session_id
                 AND a.subject_entry_id = e.id
                 AND a.event_type IN (
                    'AssistantMessageAccepted',
                    'AssistantToolRequestAccepted'
                 )
                WHERE e.session_id = %s AND e.id = %s
                """,
                (guard.session_id, entry_id),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise ConversationKernelConflict(
                    "assistant winner occurrence is not unique"
                )
            row = rows[0]
            expected_payload = {
                "entry_kind": expected_entry_kind.value,
                "block_count": len(blocks),
            }
            if (
                cut.session_id != guard.session_id
                or str(row["turn_id"]) != cut.turn_id
                or str(row["entry_kind"]) != expected_entry_kind.value
                or str(row["context_binding_revision_id"])
                != cut.context_binding_revision_id
                or int(row["provider_input_through_sequence"])
                != cut.provider_input_through_sequence
                or self._content_from_row(row) != parent_content
                or str(row["event_type"]) != expected_event_type.value
                or str(row["actor_kind"]) != "model"
                or str(row["actor_id"]) != actor_id
                or row["occurred_at"] != occurred_at
                or dict(row["payload"]) != expected_payload
            ):
                raise ConversationKernelConflict(
                    "assistant entry identity names a different winner"
                )
            block_rows = connection.execute(
                """
                SELECT * FROM pulsara_v3.assistant_message_blocks
                WHERE session_id = %s AND assistant_entry_id = %s
                ORDER BY block_ordinal, id
                """,
                (guard.session_id, entry_id),
            ).fetchall()
            actual_blocks: list[AssistantBlock] = []
            for block_row in block_rows:
                kind = str(block_row["block_kind"])
                if kind == AssistantBlockKind.TOOL_CALL.value:
                    frozen_arguments = freeze_json(dict(block_row["tool_arguments"]))
                    if not isinstance(frozen_arguments, FrozenJsonObjectFact):
                        raise ConversationKernelConflict(
                            "assistant winner tool arguments are not an object"
                        )
                    actual_blocks.append(
                        AssistantToolCallBlock(
                            block_id=str(block_row["id"]),
                            tool_call_id=str(block_row["tool_call_id"]),
                            tool_name=str(block_row["tool_name"]),
                            arguments=frozen_arguments,
                        )
                    )
                elif kind == AssistantBlockKind.TEXT.value:
                    actual_blocks.append(
                        AssistantTextBlock(
                            str(block_row["id"]), self._content_from_row(block_row)
                        )
                    )
                elif kind == AssistantBlockKind.DATA.value:
                    actual_blocks.append(
                        AssistantDataBlock(
                            str(block_row["id"]), self._content_from_row(block_row)
                        )
                    )
                else:
                    raise ConversationKernelConflict(
                        "assistant winner contains an unknown block kind"
                    )
            if tuple(actual_blocks) != tuple(blocks):
                raise ConversationKernelConflict(
                    "assistant entry blocks differ from the stable candidate"
                )
            if (
                str(row["conversation_scope_kind"])
                == ConversationScopeKind.SUBAGENT_TASK.value
            ):
                task_id = str(row["scope_subagent_task_id"])
                child_id = _stable_subagent_message_child_id(task_id, entry_id)
                child = connection.execute(
                    """
                    SELECT c.child_kind, c.child_ordinal, c.entry_id,
                           a.event_type, a.actor_kind, a.actor_id, a.payload
                    FROM pulsara_v3.subagent_task_children AS c
                    JOIN pulsara_v3.agent_events AS a
                      ON a.session_id = c.session_id
                     AND a.subject_subagent_message_id = c.id
                    WHERE c.session_id = %s AND c.id = %s
                    """,
                    (guard.session_id, child_id),
                ).fetchall()
                if (
                    len(child) != 1
                    or str(child[0]["child_kind"]) != "MESSAGE"
                    or str(child[0]["entry_id"]) != entry_id
                    or str(child[0]["event_type"])
                    != CommittedEventType.SUBAGENT_MESSAGE_ACCEPTED.value
                    or str(child[0]["actor_kind"]) != "subagent"
                    or str(child[0]["actor_id"]) != task_id
                    or dict(child[0]["payload"])
                    != {"child_ordinal": int(child[0]["child_ordinal"])}
                ):
                    raise ConversationKernelConflict(
                        "subagent assistant winner lacks its exact message child"
                    )
            terminal = connection.execute(
                """
                SELECT event_sequence FROM pulsara_v3.agent_events
                WHERE session_id = %s AND event_type = 'TurnCompleted'
                  AND subject_turn_id = %s
                  AND payload->>'final_entry_id' = %s
                """,
                (guard.session_id, cut.turn_id, entry_id),
            ).fetchall()
            if len(terminal) > 1 or (terminal and not complete_turn):
                raise ConversationKernelConflict(
                    "assistant candidate terminal disposition conflicts"
                )
            return AcceptedEntry(
                entry_id=entry_id,
                turn_id=cut.turn_id,
                entry_sequence=int(row["entry_sequence"]),
                event_sequence=int(row["event_sequence"]),
                turn_completed=bool(terminal),
            )

    def interrupt_turn(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        reason: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> bool:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            turn = connection.execute(
                """
                SELECT workspace_id FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s AND status = 'RUNNING'
                FOR UPDATE
                """,
                (guard.session_id, turn_id),
            ).fetchone()
            if turn is None:
                return False
            connection.execute(
                """
                UPDATE pulsara_v3.plan_interactions
                SET status = 'ABORTED', aborted_at = clock_timestamp()
                WHERE session_id = %s AND origin_turn_id = %s
                  AND kind = 'QUESTION' AND status = 'OPEN'
                """,
                (guard.session_id, turn_id),
            )
            row = connection.execute(
                """
                UPDATE pulsara_v3.turns
                SET status = 'INTERRUPTED', terminal_reason = %s,
                    terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'RUNNING'
                RETURNING workspace_id
                """,
                (reason, guard.session_id, turn_id),
            ).fetchone()
            assert row is not None
            self._append_events(
                connection,
                guard,
                workspace_id=str(row["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.TURN_INTERRUPTED,
                        SubjectSlot.TURN,
                        turn_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"reason": reason},
                    ),
                ),
            )
            return True

    def read_turn_status(
        self,
        *,
        session_id: str,
        turn_id: str,
        deadline_monotonic: float,
    ) -> TurnStatus | None:
        """Read only the canonical lifecycle needed by physical settlement."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """SELECT status FROM pulsara_v3.turns
                   WHERE session_id = %s AND id = %s""",
                (session_id, turn_id),
            ).fetchone()
            return None if row is None else TurnStatus(str(row["status"]))

    def read_turn_terminal_outcome(
        self,
        *,
        session_id: str,
        turn_id: str,
        deadline_monotonic: float,
    ) -> Mapping[str, object] | None:
        """Read one exact lifecycle winner, including interruption reason."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            row = connection.execute(
                """SELECT status, terminal_reason, terminal_at
                   FROM pulsara_v3.turns
                   WHERE session_id = %s AND id = %s""",
                (session_id, turn_id),
            ).fetchone()
            if row is None:
                return None
            status = str(row["status"])
            reason = (
                None if row["terminal_reason"] is None else str(row["terminal_reason"])
            )
            if status == "INTERRUPTED":
                events = connection.execute(
                    """SELECT payload FROM pulsara_v3.agent_events
                       WHERE session_id = %s AND event_type = 'TurnInterrupted'
                         AND subject_turn_id = %s""",
                    (session_id, turn_id),
                ).fetchall()
                if len(events) != 1 or events[0]["payload"] != {"reason": reason}:
                    raise ConversationKernelConflict(
                        "turn interruption winner lacks its exact occurrence"
                    )
            return {
                "status": status,
                "terminal_reason": reason,
                "terminal_at": row["terminal_at"],
            }

    def rehydrate_session(
        self,
        *,
        session_id: str,
        deadline_monotonic: float,
    ) -> tuple[Mapping[str, object], ...]:
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
                    SELECT e.*, b.block_ordinal, b.block_kind, b.tool_call_id,
                           b.tool_name, b.tool_arguments,
                           b.inline_content AS block_inline_content,
                           b.blob_id AS block_blob_id
                    FROM pulsara_v3.transcript_entries AS e
                    LEFT JOIN pulsara_v3.assistant_message_blocks AS b
                      ON b.session_id = e.session_id
                     AND b.assistant_entry_id = e.id
                    WHERE e.session_id = %s
                    ORDER BY e.entry_sequence, b.block_ordinal NULLS FIRST
                    """,
                    (session_id,),
                ).fetchall()
            )

    def query_command(
        self,
        *,
        session_id: str,
        command_id: str,
        deadline_monotonic: float,
    ) -> Mapping[str, object] | None:
        """Return the canonical command target; no process receipt is replayed."""
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            row = connection.execute(
                """
                SELECT c.*, t.status AS turn_status, t.final_entry_id,
                       t.terminal_reason,
                       q.status AS queue_status,
                       q.terminal_reason AS queue_terminal_reason,
                       q.consumed_entry_id,
                       qe.turn_id AS consumed_turn_id,
                       qt.status AS consumed_turn_status,
                       qt.final_entry_id AS consumed_turn_final_entry_id,
                       qt.terminal_reason AS consumed_turn_terminal_reason,
                       te.turn_id AS target_entry_turn_id,
                       te.source_job_id AS target_entry_source_job_id,
                       te.source_subagent_result_id AS target_entry_source_subagent_result_id,
                       d.decision AS interaction_decision,
                       d.subject_kind AS interaction_subject_kind,
                       d.subject_tool_call_entry_id,
                       d.subject_tool_call_id,
                       pw.status AS plan_workflow_status,
                       pw.workflow_revision AS plan_workflow_revision,
                       pw.resume_permission_mode AS plan_resume_permission_mode,
                       pi.status AS plan_interaction_status,
                       pi.kind AS plan_interaction_kind,
                       pi.plan_workflow_id AS interaction_plan_workflow_id,
                       pi.decision_continuation_entry_id,
                       pi.control_tool_result_id,
                       piw.status AS interaction_workflow_status,
                       piw.workflow_revision AS interaction_workflow_revision,
                       piw.resume_permission_mode AS interaction_resume_permission_mode,
                       pce.turn_id AS plan_continuation_turn_id
                FROM pulsara_v3.session_commands AS c
                LEFT JOIN pulsara_v3.turns AS t
                  ON t.session_id = c.session_id AND t.id = c.target_turn_id
                LEFT JOIN pulsara_v3.prompt_queue_items AS q
                  ON q.session_id = c.session_id AND q.id = c.target_queue_item_id
                LEFT JOIN pulsara_v3.transcript_entries AS qe
                  ON qe.session_id = q.session_id AND qe.id = q.consumed_entry_id
                LEFT JOIN pulsara_v3.turns AS qt
                  ON qt.session_id = qe.session_id AND qt.id = qe.turn_id
                LEFT JOIN pulsara_v3.transcript_entries AS te
                  ON te.session_id = c.session_id AND te.id = c.target_entry_id
                LEFT JOIN pulsara_v3.interaction_decisions AS d
                  ON d.session_id = c.session_id
                 AND d.id = c.target_interaction_decision_id
                LEFT JOIN pulsara_v3.plan_workflows AS pw
                  ON pw.session_id = c.session_id
                 AND pw.id = c.target_plan_workflow_id
                LEFT JOIN pulsara_v3.plan_interactions AS pi
                  ON pi.session_id = c.session_id
                 AND pi.id = c.target_plan_interaction_id
                LEFT JOIN pulsara_v3.plan_workflows AS piw
                  ON piw.session_id = pi.session_id
                 AND piw.id = pi.plan_workflow_id
                LEFT JOIN pulsara_v3.transcript_entries AS pce
                  ON pce.session_id = pi.session_id
                 AND pce.id = pi.decision_continuation_entry_id
                WHERE c.session_id = %s AND c.command_id = %s
                """,
                (session_id, command_id),
            ).fetchone()
            return dict(row) if row is not None else None

    def close_session(
        self,
        guard: HostWriterGuard,
        *,
        deadline_monotonic: float,
    ) -> None:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            occurred_at = _utcnow()
            running = tuple(
                connection.execute(
                    """
                    UPDATE pulsara_v3.turns
                    SET status = 'INTERRUPTED',
                        terminal_reason = 'SESSION_CLOSED',
                        terminal_at = clock_timestamp()
                    WHERE session_id = %s AND status = 'RUNNING'
                    RETURNING id, workspace_id
                    """,
                    (guard.session_id,),
                ).fetchall()
            )
            connection.execute(
                """
                UPDATE pulsara_v3.plan_interactions
                SET status = 'ABORTED', aborted_at = clock_timestamp()
                WHERE session_id = %s AND status = 'OPEN'
                """,
                (guard.session_id,),
            )
            exited = connection.execute(
                """
                UPDATE pulsara_v3.plan_workflows
                SET status = 'FORCE_EXITED',
                    workflow_revision = workflow_revision + 1,
                    accepted_plan_interaction_id = NULL,
                    terminal_at = clock_timestamp()
                WHERE session_id = %s AND status = 'ACTIVE'
                RETURNING id, workspace_id
                """,
                (guard.session_id,),
            ).fetchone()
            drafts: list[CommittedEventDraft] = [
                self._event(
                    CommittedEventType.TURN_INTERRUPTED,
                    SubjectSlot.TURN,
                    str(row["id"]),
                    occurred_at=occurred_at,
                    actor_kind="runtime",
                    actor_id=guard.writer_owner_id,
                    payload={"reason": "SESSION_CLOSED"},
                )
                for row in running
            ]
            if exited is not None:
                drafts.append(
                    self._event(
                        CommittedEventType.PLAN_WORKFLOW_EXITED,
                        SubjectSlot.PLAN_WORKFLOW,
                        str(exited["id"]),
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=guard.writer_owner_id,
                        payload={"status": PlanWorkflowStatus.FORCE_EXITED.value},
                    )
                )
            if drafts:
                workspace_id = (
                    str(exited["workspace_id"])
                    if exited is not None
                    else str(running[0]["workspace_id"])
                )
                self._append_events(
                    connection,
                    guard,
                    workspace_id=workspace_id,
                    drafts=tuple(drafts),
                )
            connection.execute(
                """
                UPDATE pulsara_v3.sessions
                SET lifecycle = 'CLOSED', writer_lease_owner_id = NULL,
                    writer_lease_expires_at = NULL, updated_at = clock_timestamp()
                WHERE id = %s
                """,
                (guard.session_id,),
            )

    def events_after(
        self,
        *,
        session_id: str,
        after_sequence: int,
        limit: int,
        deadline_monotonic: float,
    ) -> tuple[Mapping[str, object], ...]:
        if limit < 1 or limit > 1024:
            raise ValueError("event page limit is out of bounds")
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM pulsara_v3.agent_events
                    WHERE session_id = %s AND event_sequence > %s
                    ORDER BY event_sequence
                    LIMIT %s
                    """,
                    (session_id, after_sequence, limit),
                ).fetchall()
            )

    @staticmethod
    def _terminal_observation_event(
        candidate: TerminalObservationInstallationAttempt,
        entry_id: str,
    ) -> CommittedEventDraft:
        return CommittedEventDraft(
            event_id=_stable_identity(
                "event",
                candidate.content.observation_id,
                CommittedEventType.TERMINAL_OBSERVATION_ACCEPTED.value,
            ),
            event_type=CommittedEventType.TERMINAL_OBSERVATION_ACCEPTED,
            subject=CommittedEventSubject(
                slot=SubjectSlot.ENTRY,
                subject_id=entry_id,
            ),
            actor_kind="runtime",
            actor_id=candidate.actor_id,
            sensitivity_class="S1",
            projection_profile="IMMUTABLE_ENTRY",
            occurred_at=candidate.occurred_at,
            payload={
                "entry_kind": EntryKind.TERMINAL_OBSERVATION.value,
                "observation_kind": candidate.content.observation_kind.value,
            },
        )
