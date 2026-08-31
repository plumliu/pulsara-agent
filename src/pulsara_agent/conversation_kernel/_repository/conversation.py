"""Canonical conversation and turn operations."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from typing import Mapping, Sequence

from psycopg import Connection
from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionAdoptionConfirmation,
    CompactionCanonicalWritePreconditions,
    CompactionConfirmationKind,
    CompactionLineageBaseKind,
    CompactionSourceLineageBase,
    PreparedCompactionCanonicalAdoption,
    PreparedManualCompactionCommand,
    canonical_compaction_range_digest,
    freeze_compaction_canonical_range,
)
from pulsara_agent.conversation_kernel.contracts import (
    AssistantBlockKind,
    CanonicalContent,
    CommittedEventDraft,
    CommittedEventSubject,
    ConversationScopeKind,
    EntryKind,
    HostWriterGuard,
    InlineContent,
    TurnStatus,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.vocabulary import (
    CommittedEventType,
    SubjectSlot,
)
from pulsara_agent.llm.provider_replay import (
    PreparedDurableProviderAssistantReplay,
    ProviderReplayDisposition,
)
from pulsara_agent.model_input.contracts import PreparedProviderInputCut
from pulsara_agent.ports.terminal_observation import ExistingTurnInstallation, NewTurnInstallation, TerminalObservationInstallationAttempt
from pulsara_agent.primitives.context import FrozenJsonObjectFact, freeze_json, thaw_json
from pulsara_agent.conversation_kernel.subagents.contracts import (
    FrozenSubagentResultPublicFact,
    SubagentResultSource,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import PlanWorkflowStatus
from pulsara_agent.primitives.run_permission import (
    FrozenRunPermissionSnapshot,
    RunPermissionAdmissionSource,
)
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


def _manual_compaction_turn_matches(
    row: Mapping[str, object], candidate: PreparedManualCompactionCommand
) -> bool:
    return (
        str(row["conversation_scope_kind"]) == candidate.scope_kind.value
        and row["scope_subagent_task_id"] == candidate.scope_subagent_task_id
    )


class _ConversationOperations:
    def prepare_root_permission_snapshot(
        self,
        guard: HostWriterGuard,
        *,
        snapshot_id: str,
        requested_mode: PermissionMode,
        deadline_monotonic: float,
    ) -> FrozenRunPermissionSnapshot:
        """Read the exact Plan/permission cut required by prompt Hook admission."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            return self._freeze_root_permission_snapshot(
                connection,
                session_id=guard.session_id,
                snapshot_id=snapshot_id,
                requested_mode=requested_mode,
                admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
            )

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
            if (
                prepared.expected_permission_snapshot is not None
                and permission != prepared.expected_permission_snapshot
            ):
                raise ConversationKernelConflict("INGRESS_PRECONDITION_CHANGED")
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
            self._insert_initial_context_binding_revision(
                connection,
                session_id=guard.session_id,
                turn_id=turn_id,
                revision_id=context_binding_revision_id,
                initial_entry_sequence=entry_sequence,
                scope_kind=ConversationScopeKind.ROOT,
                scope_subagent_task_id=None,
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
                and self._initial_context_binding_revision_matches(
                    connection,
                    row=revision,
                    session_id=candidate.session_id,
                    turn_id=candidate.turn_id,
                    revision_id=candidate.context_binding_revision_id,
                    initial_entry_sequence=int(entry["entry_sequence"]),
                    scope_kind=ConversationScopeKind.ROOT,
                    scope_subagent_task_id=None,
                )
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

    def prepare_compaction_input_cut(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        allow_terminal: bool,
        deadline_monotonic: float,
    ) -> PreparedProviderInputCut:
        """Prepare the exact active/idle compaction cut under the current writer."""

        statuses = ("RUNNING", "COMPLETED", "INTERRUPTED") if allow_terminal else ("RUNNING",)
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
                       t.status, s.latest_entry_sequence
                FROM pulsara_v3.turns AS t
                JOIN pulsara_v3.sessions AS s ON s.id = t.session_id
                WHERE t.session_id = %s AND t.id = %s
                  AND t.status = ANY(%s)
                """,
                (guard.session_id, turn_id, list(statuses)),
            ).fetchone()
            if row is None:
                raise ConversationKernelConflict(
                    "compaction target status is not admissible"
                )
            return PreparedProviderInputCut(
                session_id=guard.session_id,
                turn_id=turn_id,
                context_binding_revision_id=str(
                    row["current_context_binding_revision_id"]
                ),
                provider_input_through_sequence=int(row["latest_entry_sequence"]),
            )

    def read_latest_terminal_scope_turn_id(
        self,
        guard: HostWriterGuard,
        *,
        scope_kind: ConversationScopeKind,
        scope_subagent_task_id: str | None,
        deadline_monotonic: float,
    ) -> str | None:
        if (scope_kind is ConversationScopeKind.ROOT) != (
            scope_subagent_task_id is None
        ):
            raise ValueError("terminal compaction scope union is invalid")
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            row = connection.execute(
                """
                SELECT id
                FROM pulsara_v3.turns
                WHERE session_id = %s
                  AND conversation_scope_kind = %s
                  AND scope_subagent_task_id IS NOT DISTINCT FROM %s
                  AND status IN ('COMPLETED', 'INTERRUPTED')
                ORDER BY accepted_at DESC, id DESC
                LIMIT 1
                """,
                (guard.session_id, scope_kind.value, scope_subagent_task_id),
            ).fetchone()
            return None if row is None else str(row["id"])

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
                self._insert_initial_context_binding_revision(
                    connection,
                    session_id=guard.session_id,
                    turn_id=turn_id,
                    revision_id=target.context_binding_revision_id,
                    initial_entry_sequence=entry_sequence,
                    scope_kind=ConversationScopeKind.ROOT,
                    scope_subagent_task_id=None,
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
                    or not self._initial_context_binding_revision_matches(
                        connection,
                        row=revision,
                        session_id=guard.session_id,
                        turn_id=target.turn_id,
                        revision_id=target.context_binding_revision_id,
                        initial_entry_sequence=int(entry["entry_sequence"]),
                        scope_kind=ConversationScopeKind.ROOT,
                        scope_subagent_task_id=None,
                    )
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

    def confirm_manual_compaction_command(
        self,
        *,
        candidate: PreparedManualCompactionCommand,
        deadline_monotonic: float,
    ) -> CompactionConfirmationKind:
        """Classify the stable manual command without process-local state."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            command = connection.execute(
                "SELECT * FROM pulsara_v3.session_commands "
                "WHERE session_id = %s AND command_id = %s",
                (candidate.session_id, candidate.command_id),
            ).fetchone()
            turn = connection.execute(
                "SELECT conversation_scope_kind, scope_subagent_task_id "
                "FROM pulsara_v3.turns WHERE session_id = %s AND id = %s",
                (candidate.session_id, candidate.target_turn_id),
            ).fetchone()
            if command is None:
                if turn is None or not _manual_compaction_turn_matches(
                    turn, candidate
                ):
                    return CompactionConfirmationKind.CONFLICT
                return CompactionConfirmationKind.NONE
            if turn is None or not _manual_compaction_turn_matches(turn, candidate):
                return CompactionConfirmationKind.CONFLICT
            if (
                str(command["command_kind"]) != "COMPACT_CONTEXT"
                or str(command["request_schema_version"]) != "compact_context.v1"
                or str(command["semantic_digest"]) != candidate.semantic_digest
                or str(command["target_kind"]) != "TURN"
                or str(command["target_turn_id"]) != candidate.target_turn_id
            ):
                return CompactionConfirmationKind.CONFLICT
            return CompactionConfirmationKind.FULL

    def accept_manual_compaction_command(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedManualCompactionCommand,
        deadline_monotonic: float,
    ) -> CompactionConfirmationKind:
        """Accept one exact manual command before its process-local attempt."""

        if candidate.session_id != guard.session_id:
            raise ValueError("manual compaction command belongs to another session")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            turn = connection.execute(
                "SELECT conversation_scope_kind, scope_subagent_task_id "
                "FROM pulsara_v3.turns WHERE session_id = %s AND id = %s FOR SHARE",
                (guard.session_id, candidate.target_turn_id),
            ).fetchone()
            if turn is None or not _manual_compaction_turn_matches(turn, candidate):
                raise ConversationKernelConflict(
                    "manual compaction target scope drifted"
                )
            existing = connection.execute(
                "SELECT * FROM pulsara_v3.session_commands "
                "WHERE session_id = %s AND command_id = %s",
                (guard.session_id, candidate.command_id),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["command_kind"]) != "COMPACT_CONTEXT"
                    or str(existing["request_schema_version"])
                    != "compact_context.v1"
                    or str(existing["semantic_digest"])
                    != candidate.semantic_digest
                    or str(existing["target_kind"]) != "TURN"
                    or str(existing["target_turn_id"])
                    != candidate.target_turn_id
                ):
                    raise ConversationKernelConflict(
                        "manual compaction command identity conflict"
                    )
                return CompactionConfirmationKind.FULL
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_turn_id
                ) VALUES (%s, %s, 'COMPACT_CONTEXT', 'compact_context.v1',
                          %s, 'TURN', %s)
                """,
                (
                    guard.session_id,
                    candidate.command_id,
                    candidate.semantic_digest,
                    candidate.target_turn_id,
                ),
            )
            return CompactionConfirmationKind.FULL

    def adopt_context_snapshot(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedCompactionCanonicalAdoption,
        preconditions: CompactionCanonicalWritePreconditions,
        deadline_monotonic: float,
    ) -> CompactionAdoptionConfirmation:
        """Atomically install the exact prepared snapshot/revision/event winner."""

        if (
            candidate.scope.session_id != guard.session_id
            or preconditions.scope != candidate.scope
            or preconditions.expected_turn_status != candidate.expected_turn_status
            or candidate.snapshot.source_through_sequence
            > preconditions.expected_safe_head
        ):
            raise ValueError("compaction adoption arguments do not exact-join")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            turn = connection.execute(
                """
                SELECT t.*, s.latest_entry_sequence,
                       current.revision_ordinal AS current_revision_ordinal,
                       current.base_kind AS current_base_kind,
                       current.context_snapshot_id AS current_snapshot_id,
                       current.source_through_sequence AS current_source_cut
                FROM pulsara_v3.turns AS t
                JOIN pulsara_v3.sessions AS s ON s.id = t.session_id
                JOIN pulsara_v3.turn_context_binding_revisions AS current
                  ON current.session_id = t.session_id
                 AND current.id = t.current_context_binding_revision_id
                WHERE t.session_id = %s AND t.id = %s
                FOR UPDATE OF t
                """,
                (guard.session_id, candidate.scope.turn_id),
            ).fetchone()
            self._require_compaction_target(
                connection,
                turn=turn,
                candidate=candidate,
                preconditions=preconditions,
            )
            self._require_compaction_source_digest(
                connection,
                candidate=candidate,
                safe_head=preconditions.expected_safe_head,
                deadline_monotonic=deadline_monotonic,
            )
            snapshot = candidate.snapshot
            binding = candidate.binding
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
                    snapshot.snapshot_id,
                    snapshot.session_id,
                    snapshot.workspace_id,
                    snapshot.source_through_sequence,
                    snapshot.source_digest,
                    snapshot.compiler_contract,
                    snapshot.prompt_contract,
                    snapshot.model_contract,
                    *_content_columns(snapshot.content),
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
                    binding.binding_revision_id,
                    binding.session_id,
                    binding.turn_id,
                    binding.revision_ordinal,
                    binding.context_snapshot_id,
                    binding.source_through_sequence,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.turns
                SET current_context_binding_revision_id = %s
                WHERE session_id = %s AND id = %s
                """,
                (
                    binding.binding_revision_id,
                    binding.session_id,
                    binding.turn_id,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=candidate.scope.workspace_id,
                drafts=(candidate.event,),
            )
            return CompactionAdoptionConfirmation(
                CompactionConfirmationKind.FULL,
                binding.revision_ordinal,
            )

    def confirm_context_snapshot_adoption(
        self,
        *,
        candidate: PreparedCompactionCanonicalAdoption,
        deadline_monotonic: float,
    ) -> CompactionAdoptionConfirmation:
        """Statelessly classify the exact prepared compaction winner."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            snapshot = connection.execute(
                "SELECT * FROM pulsara_v3.context_snapshots "
                "WHERE session_id = %s AND id = %s",
                (candidate.scope.session_id, candidate.snapshot.snapshot_id),
            ).fetchone()
            revision = connection.execute(
                "SELECT * FROM pulsara_v3.turn_context_binding_revisions "
                "WHERE session_id = %s AND id = %s",
                (
                    candidate.scope.session_id,
                    candidate.binding.binding_revision_id,
                ),
            ).fetchone()
            event = connection.execute(
                "SELECT * FROM pulsara_v3.agent_events "
                "WHERE session_id = %s AND event_id = %s",
                (candidate.scope.session_id, candidate.event.event_id),
            ).fetchone()
            turn = connection.execute(
                "SELECT current_context_binding_revision_id FROM pulsara_v3.turns "
                "WHERE session_id = %s AND id = %s",
                (candidate.scope.session_id, candidate.scope.turn_id),
            ).fetchone()
            predecessor = connection.execute(
                "SELECT * FROM pulsara_v3.turn_context_binding_revisions "
                "WHERE session_id = %s AND id = %s",
                (
                    candidate.scope.session_id,
                    candidate.predecessor.binding_revision_id,
                ),
            ).fetchone()
            rows = (snapshot, revision, event)
            if all(row is None for row in rows):
                if (
                    turn is not None
                    and predecessor is not None
                    and str(turn["current_context_binding_revision_id"])
                    == candidate.predecessor.binding_revision_id
                    and self._compaction_predecessor_row_matches(
                        predecessor, candidate
                    )
                ):
                    return CompactionAdoptionConfirmation(
                        CompactionConfirmationKind.NONE
                    )
                return CompactionAdoptionConfirmation(
                    CompactionConfirmationKind.CONFLICT
                )
            if (
                any(row is None for row in rows)
                or turn is None
                or predecessor is None
                or str(turn["current_context_binding_revision_id"])
                != candidate.binding.binding_revision_id
                or not self._compaction_snapshot_row_matches(snapshot, candidate)
                or not self._compaction_binding_row_matches(revision, candidate)
                or not self._compaction_predecessor_row_matches(
                    predecessor, candidate
                )
                or not _event_row_matches_draft(event, candidate.event)
            ):
                return CompactionAdoptionConfirmation(
                    CompactionConfirmationKind.CONFLICT
                )
            return CompactionAdoptionConfirmation(
                CompactionConfirmationKind.FULL,
                candidate.binding.revision_ordinal,
            )

    def _require_compaction_target(
        self,
        connection: Connection[Mapping[str, object]],
        *,
        turn: Mapping[str, object] | None,
        candidate: PreparedCompactionCanonicalAdoption,
        preconditions: CompactionCanonicalWritePreconditions,
    ) -> None:
        if turn is None:
            raise ConversationKernelConflict("compaction target turn is absent")
        predecessor = candidate.predecessor
        if (
            str(turn["workspace_id"]) != candidate.scope.workspace_id
            or str(turn["conversation_scope_kind"])
            != candidate.scope.scope_kind.value
            or (
                None
                if turn["scope_subagent_task_id"] is None
                else str(turn["scope_subagent_task_id"])
            )
            != candidate.scope.scope_subagent_task_id
            or str(turn["status"]) != candidate.expected_turn_status
            or str(turn["current_context_binding_revision_id"])
            != predecessor.binding_revision_id
            or int(turn["current_revision_ordinal"])
            != predecessor.revision_ordinal
            or str(turn["current_base_kind"]) != predecessor.base_kind
            or (
                None
                if turn["current_snapshot_id"] is None
                else str(turn["current_snapshot_id"])
            )
            != predecessor.context_snapshot_id
            or int(turn["current_source_cut"])
            != predecessor.source_through_sequence
        ):
            raise ConversationKernelConflict(
                "compaction target or predecessor identity drifted"
            )
        if candidate.target_branch.value == "IDLE_BASE_ONLY":
            latest = connection.execute(
                """
                SELECT t.id
                FROM pulsara_v3.turns AS t
                JOIN pulsara_v3.transcript_entries AS initial_entry
                  ON initial_entry.session_id = t.session_id
                 AND initial_entry.id = t.initial_entry_id
                WHERE t.session_id = %s
                  AND t.conversation_scope_kind = %s
                  AND t.scope_subagent_task_id IS NOT DISTINCT FROM %s
                  AND t.status IN ('COMPLETED', 'INTERRUPTED')
                ORDER BY initial_entry.entry_sequence DESC, t.id DESC
                LIMIT 1
                """,
                (
                    candidate.scope.session_id,
                    candidate.scope.scope_kind.value,
                    candidate.scope.scope_subagent_task_id,
                ),
            ).fetchone()
            if latest is None or str(latest["id"]) != candidate.scope.turn_id:
                raise ConversationKernelConflict(
                    "idle compaction target is not the latest terminal turn"
                )
        later = connection.execute(
            """
            SELECT 1
            FROM pulsara_v3.transcript_entries
            WHERE session_id = %s
              AND conversation_scope_kind = %s
              AND scope_subagent_task_id IS NOT DISTINCT FROM %s
              AND entry_sequence > %s
            LIMIT 1
            """,
            (
                candidate.scope.session_id,
                candidate.scope.scope_kind.value,
                candidate.scope.scope_subagent_task_id,
                preconditions.expected_safe_head,
            ),
        ).fetchone()
        if later is not None:
            raise ConversationKernelConflict("compaction canonical head advanced")
        split_group = connection.execute(
            """
            SELECT 1
            FROM pulsara_v3.assistant_message_blocks AS call
            JOIN pulsara_v3.transcript_entries AS request
              ON request.session_id = call.session_id
             AND request.id = call.assistant_entry_id
            JOIN pulsara_v3.tool_results AS result
              ON result.session_id = call.session_id
             AND result.tool_call_entry_id = call.assistant_entry_id
             AND result.tool_call_id = call.tool_call_id
            JOIN pulsara_v3.transcript_entries AS result_entry
              ON result_entry.session_id = result.session_id
             AND result_entry.id = result.result_entry_id
            WHERE request.session_id = %s
              AND request.conversation_scope_kind = %s
              AND request.scope_subagent_task_id IS NOT DISTINCT FROM %s
              AND call.block_kind = 'TOOL_CALL'
              AND request.entry_sequence <= %s
              AND result_entry.entry_sequence > %s
            LIMIT 1
            """,
            (
                candidate.scope.session_id,
                candidate.scope.scope_kind.value,
                candidate.scope.scope_subagent_task_id,
                candidate.snapshot.source_through_sequence,
                candidate.snapshot.source_through_sequence,
            ),
        ).fetchone()
        if split_group is not None:
            raise ConversationKernelConflict(
                "compaction source boundary splits a tool group"
            )

    def _require_compaction_source_digest(
        self,
        connection: Connection[Mapping[str, object]],
        *,
        candidate: PreparedCompactionCanonicalAdoption,
        safe_head: int,
        deadline_monotonic: float,
    ) -> None:
        predecessor = candidate.predecessor
        if predecessor.base_kind == "FULL_HISTORY":
            lineage = CompactionSourceLineageBase(
                kind=CompactionLineageBaseKind.FULL_HISTORY_GENESIS,
                scope=candidate.scope,
                binding_revision_id=predecessor.binding_revision_id,
                binding_revision_ordinal=predecessor.revision_ordinal,
                persisted_revision_genesis_marker=(
                    predecessor.source_through_sequence
                ),
                effective_materialization_lineage_floor=0,
            )
        else:
            snapshot = connection.execute(
                """
                SELECT source_through_sequence, source_digest
                FROM pulsara_v3.context_snapshots
                WHERE session_id = %s AND id = %s
                """,
                (
                    candidate.scope.session_id,
                    predecessor.context_snapshot_id,
                ),
            ).fetchone()
            if snapshot is None or int(snapshot["source_through_sequence"]) != (
                predecessor.source_through_sequence
            ):
                raise ConversationKernelConflict(
                    "compaction predecessor snapshot drifted"
                )
            lineage = CompactionSourceLineageBase(
                kind=CompactionLineageBaseKind.CURRENT_SNAPSHOT,
                scope=candidate.scope,
                binding_revision_id=predecessor.binding_revision_id,
                binding_revision_ordinal=predecessor.revision_ordinal,
                persisted_revision_genesis_marker=(
                    predecessor.source_through_sequence
                ),
                effective_materialization_lineage_floor=(
                    predecessor.source_through_sequence
                ),
                snapshot_id=predecessor.context_snapshot_id,
                prior_source_digest=str(snapshot["source_digest"]),
            )

        class _TransactionBlobReader:
            """Read immutable blobs on this exact writer transaction."""

            def read_exact(
                self,
                *,
                blob_id: str,
                expected_digest: str,
                expected_size: int,
                deadline_monotonic: float,
            ) -> bytes:
                del deadline_monotonic
                row = connection.execute(
                    """
                    SELECT logical_digest, logical_size, body
                    FROM pulsara_v3.blobs
                    WHERE id = %s
                    """,
                    (blob_id,),
                ).fetchone()
                if row is None:
                    raise ConversationKernelConflict(
                        "compaction source blob is absent"
                    )
                body = bytes(row["body"])
                if (
                    str(row["logical_digest"]) != expected_digest
                    or int(row["logical_size"]) != expected_size
                    or len(body) != expected_size
                    or "sha256:" + sha256(body).hexdigest() != expected_digest
                ):
                    raise ConversationKernelConflict(
                        "compaction source blob integrity drifted"
                    )
                return body

        reader = CanonicalProviderInputReader(
            self._provider,
            blob_reader=_TransactionBlobReader(),
        )
        dispatch = reader.read_frozen_dispatch(
            PreparedProviderInputCut(
                session_id=candidate.scope.session_id,
                turn_id=candidate.scope.turn_id,
                context_binding_revision_id=(
                    predecessor.binding_revision_id
                ),
                provider_input_through_sequence=(
                    safe_head
                ),
            ),
            deadline_monotonic=deadline_monotonic,
            _connection=connection,
        )
        canonical = dispatch.compile_snapshot.canonical_input
        if (
            canonical.identity.provider_input_through_sequence
            != safe_head
            or safe_head < candidate.snapshot.source_through_sequence
        ):
            raise ConversationKernelConflict(
                "compaction source materialization cut drifted"
            )
        canonical_range = freeze_compaction_canonical_range(
            scope=candidate.scope,
            effective_materialization_lineage_floor=(
                lineage.effective_materialization_lineage_floor
            ),
            source_through_sequence=(
                candidate.snapshot.source_through_sequence
            ),
            ordered_items=canonical.items,
            closures=canonical.closures,
            late_outcomes=canonical.late_outcomes,
        )
        if (
            canonical_compaction_range_digest(lineage, canonical_range)
            != candidate.snapshot.source_digest
        ):
            raise ConversationKernelConflict(
                "compaction source lineage digest drifted"
            )

    def _compaction_snapshot_row_matches(
        self,
        row: Mapping[str, object] | None,
        candidate: PreparedCompactionCanonicalAdoption,
    ) -> bool:
        if row is None:
            return False
        snapshot = candidate.snapshot
        try:
            content = self._content_from_row(row)
        except (KeyError, TypeError, ValueError):
            return False
        return bool(
            str(row["id"]) == snapshot.snapshot_id
            and str(row["session_id"]) == snapshot.session_id
            and str(row["workspace_id"]) == snapshot.workspace_id
            and int(row["source_through_sequence"])
            == snapshot.source_through_sequence
            and str(row["source_digest"]) == snapshot.source_digest
            and str(row["compiler_contract"]) == snapshot.compiler_contract
            and str(row["prompt_contract"]) == snapshot.prompt_contract
            and str(row["model_contract"]) == snapshot.model_contract
            and content == snapshot.content
        )

    @staticmethod
    def _compaction_binding_row_matches(
        row: Mapping[str, object] | None,
        candidate: PreparedCompactionCanonicalAdoption,
    ) -> bool:
        if row is None:
            return False
        binding = candidate.binding
        return bool(
            str(row["id"]) == binding.binding_revision_id
            and str(row["session_id"]) == binding.session_id
            and str(row["turn_id"]) == binding.turn_id
            and int(row["revision_ordinal"]) == binding.revision_ordinal
            and str(row["base_kind"]) == binding.base_kind
            and str(row["context_snapshot_id"])
            == binding.context_snapshot_id
            and int(row["source_through_sequence"])
            == binding.source_through_sequence
        )

    @staticmethod
    def _compaction_predecessor_row_matches(
        row: Mapping[str, object] | None,
        candidate: PreparedCompactionCanonicalAdoption,
    ) -> bool:
        if row is None:
            return False
        predecessor = candidate.predecessor
        return bool(
            str(row["id"]) == predecessor.binding_revision_id
            and int(row["revision_ordinal"]) == predecessor.revision_ordinal
            and str(row["base_kind"]) == predecessor.base_kind
            and (
                None
                if row["context_snapshot_id"] is None
                else str(row["context_snapshot_id"])
            )
            == predecessor.context_snapshot_id
            and int(row["source_through_sequence"])
            == predecessor.source_through_sequence
        )

    def commit_assistant_message(
        self,
        guard: HostWriterGuard,
        *,
        cut: PreparedProviderInputCut,
        entry_id: str,
        parent_content: CanonicalContent,
        blocks: Sequence[AssistantBlock],
        provider_wire_api: str = "openai_chat_completions",
        provider_replay_disposition: ProviderReplayDisposition = (
            ProviderReplayDisposition.PUBLIC_SEMANTIC_ONLY
        ),
        provider_replay: PreparedDurableProviderAssistantReplay | None = None,
        subagent_result: FrozenSubagentResultPublicFact | None = None,
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
        if subagent_result is not None and (
            not complete_turn
            or tool_request
            or subagent_result.source is not SubagentResultSource.INFERRED
            or subagent_result.producer_entry_id != entry_id
        ):
            raise ValueError("inferred subagent result composite is invalid")
        if (
            (provider_replay is not None)
            != (provider_replay_disposition is ProviderReplayDisposition.NATIVE_REPLAY)
            or provider_wire_api
            not in {"openai_chat_completions", "openai_responses"}
            or (
                provider_wire_api == "openai_responses"
                and provider_replay_disposition
                is not ProviderReplayDisposition.NATIVE_REPLAY
            )
        ):
            raise ValueError("assistant provider replay union is invalid")
        if provider_replay is not None and (
            provider_replay.session_id != guard.session_id
            or provider_replay.assistant_entry_id != entry_id
            or provider_replay.wire_api != provider_wire_api
        ):
            raise ValueError("assistant provider replay does not exact-join")
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
                provider_wire_api=provider_wire_api,
                provider_replay_disposition=provider_replay_disposition.value,
                provider_replay_fragment_id=(
                    None if provider_replay is None else provider_replay.replay_id
                ),
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
            if provider_replay is not None:
                if provider_replay.workspace_id != str(turn["workspace_id"]):
                    raise ConversationKernelConflict(
                        "assistant provider replay workspace drifted"
                    )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.provider_assistant_replay_fragments (
                        id, session_id, workspace_id, assistant_entry_id,
                        wire_api, codec_kind,
                        provider_replay_contract_fingerprint,
                        replay_target_fingerprint,
                        public_projection_fingerprint,
                        payload_bytes, payload_digest, payload_size,
                        item_count, fragment_fingerprint
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        provider_replay.replay_id,
                        provider_replay.session_id,
                        provider_replay.workspace_id,
                        provider_replay.assistant_entry_id,
                        provider_replay.wire_api,
                        provider_replay.codec_kind.value,
                        provider_replay.provider_replay_contract_fingerprint,
                        provider_replay.replay_target_fingerprint,
                        provider_replay.public_projection_fingerprint,
                        provider_replay.payload_bytes,
                        provider_replay.payload_digest,
                        provider_replay.payload_size,
                        provider_replay.item_count,
                        provider_replay.fragment_fingerprint,
                    ),
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
            if subagent_result is not None:
                task_id = str(turn["scope_subagent_task_id"])
                if (
                    str(turn["conversation_scope_kind"])
                    != ConversationScopeKind.SUBAGENT_TASK.value
                    or subagent_result.task_id != task_id
                ):
                    raise ConversationKernelConflict(
                        "inferred result belongs to another child scope"
                    )
                task = connection.execute(
                    """SELECT status FROM pulsara_v3.subagent_tasks
                       WHERE session_id = %s AND id = %s FOR UPDATE""",
                    (guard.session_id, task_id),
                ).fetchone()
                if task is None or str(task["status"]) != "ACTIVE":
                    raise ConversationKernelConflict(
                        "inferred result task is no longer active"
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
                           %s, 'INFERRED', %s, %s, %s::jsonb, %s
                       )""",
                    (
                        subagent_result.result_id,
                        guard.session_id,
                        task_id,
                        guard.session_id,
                        task_id,
                        entry_id,
                        subagent_result.summary,
                        subagent_result.output_preview,
                        json.dumps(thaw_json(subagent_result.diagnostics)),
                        subagent_result.result_fingerprint,
                    ),
                )
                updated = connection.execute(
                    """UPDATE pulsara_v3.subagent_tasks
                       SET status = 'COMPLETED', pending_reason = NULL,
                           terminal_reason = NULL,
                           terminal_public_detail = NULL,
                           terminal_at = clock_timestamp()
                       WHERE session_id = %s AND id = %s AND status = 'ACTIVE'
                       RETURNING id""",
                    (guard.session_id, task_id),
                ).fetchone()
                if updated is None:
                    raise ConversationKernelConflict(
                        "inferred result lost its task terminal winner"
                    )
                event_drafts.extend(
                    (
                        self._event(
                            CommittedEventType.SUBAGENT_RESULT_ACCEPTED,
                            SubjectSlot.SUBAGENT_RESULT,
                            subagent_result.result_id,
                            occurred_at=occurred_at,
                            actor_kind="subagent",
                            actor_id=task_id,
                            payload={"result_source": "INFERRED"},
                        ),
                        self._event(
                            CommittedEventType.SUBAGENT_TASK_STATUS_ACCEPTED,
                            SubjectSlot.SUBAGENT_TASK,
                            task_id,
                            occurred_at=occurred_at,
                            actor_kind="runtime",
                            actor_id="foreground-runner",
                            payload={"status": "COMPLETED", "reason": None},
                        ),
                    )
                )
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
                pending_steer_at_settlement=pending_steer,
            )

    def confirm_assistant_message_winner(
        self,
        guard: HostWriterGuard,
        *,
        cut: PreparedProviderInputCut,
        entry_id: str,
        parent_content: CanonicalContent,
        blocks: Sequence[AssistantBlock],
        provider_wire_api: str = "openai_chat_completions",
        provider_replay_disposition: ProviderReplayDisposition = (
            ProviderReplayDisposition.PUBLIC_SEMANTIC_ONLY
        ),
        provider_replay: PreparedDurableProviderAssistantReplay | None = None,
        subagent_result: FrozenSubagentResultPublicFact | None = None,
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
                or str(row["provider_wire_api"]) != provider_wire_api
                or str(row["provider_replay_disposition"])
                != provider_replay_disposition.value
                or row["provider_replay_fragment_id"]
                != (
                    None if provider_replay is None else provider_replay.replay_id
                )
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
            replay_rows = connection.execute(
                """
                SELECT * FROM pulsara_v3.provider_assistant_replay_fragments
                WHERE session_id = %s AND assistant_entry_id = %s
                """,
                (guard.session_id, entry_id),
            ).fetchall()
            if provider_replay is None:
                if replay_rows:
                    raise ConversationKernelConflict(
                        "assistant winner owns an unexpected provider replay"
                    )
            elif (
                len(replay_rows) != 1
                or str(replay_rows[0]["id"]) != provider_replay.replay_id
                or str(replay_rows[0]["workspace_id"])
                != provider_replay.workspace_id
                or str(replay_rows[0]["wire_api"]) != provider_replay.wire_api
                or str(replay_rows[0]["codec_kind"])
                != provider_replay.codec_kind.value
                or str(
                    replay_rows[0]["provider_replay_contract_fingerprint"]
                )
                != provider_replay.provider_replay_contract_fingerprint
                or str(replay_rows[0]["replay_target_fingerprint"])
                != provider_replay.replay_target_fingerprint
                or str(replay_rows[0]["public_projection_fingerprint"])
                != provider_replay.public_projection_fingerprint
                or bytes(replay_rows[0]["payload_bytes"])
                != provider_replay.payload_bytes
                or str(replay_rows[0]["payload_digest"])
                != provider_replay.payload_digest
                or int(replay_rows[0]["payload_size"])
                != provider_replay.payload_size
                or int(replay_rows[0]["item_count"])
                != provider_replay.item_count
                or str(replay_rows[0]["fragment_fingerprint"])
                != provider_replay.fragment_fingerprint
            ):
                raise ConversationKernelConflict(
                    "assistant provider replay differs from the stable candidate"
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
            result_rows = connection.execute(
                """SELECT result.*, task.status,
                          result_event.event_type AS result_event_type,
                          status_event.event_type AS status_event_type
                   FROM pulsara_v3.subagent_task_children AS result
                   JOIN pulsara_v3.subagent_tasks AS task
                     ON task.session_id = result.session_id
                    AND task.id = result.task_id
                   LEFT JOIN pulsara_v3.agent_events AS result_event
                     ON result_event.session_id = result.session_id
                    AND result_event.subject_subagent_result_id = result.id
                    AND result_event.event_type = 'SubagentResultAccepted'
                   LEFT JOIN pulsara_v3.agent_events AS status_event
                     ON status_event.session_id = task.session_id
                    AND status_event.subject_subagent_task_id = task.id
                    AND status_event.event_type = 'SubagentTaskStatusAccepted'
                    AND status_event.payload->>'status' = 'COMPLETED'
                   WHERE result.session_id = %s AND result.entry_id = %s
                     AND result.child_kind = 'RESULT'""",
                (guard.session_id, entry_id),
            ).fetchall()
            if subagent_result is None:
                if result_rows:
                    raise ConversationKernelConflict(
                        "assistant winner owns an unexpected inferred result"
                    )
            elif (
                len(result_rows) != 1
                or str(result_rows[0]["id"]) != subagent_result.result_id
                or str(result_rows[0]["task_id"]) != subagent_result.task_id
                or str(result_rows[0]["result_source"]) != "INFERRED"
                or str(result_rows[0]["summary"]) != subagent_result.summary
                or result_rows[0]["output_preview"] != subagent_result.output_preview
                or freeze_json(result_rows[0]["diagnostics"])
                != subagent_result.diagnostics
                or str(result_rows[0]["result_fingerprint"])
                != subagent_result.result_fingerprint
                or str(result_rows[0]["status"]) != "COMPLETED"
                or str(result_rows[0]["result_event_type"])
                != "SubagentResultAccepted"
                or str(result_rows[0]["status_event_type"])
                != "SubagentTaskStatusAccepted"
            ):
                raise ConversationKernelConflict(
                    "inferred subagent result differs from stable candidate"
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
            return AcceptedEntry(
                entry_id=entry_id,
                turn_id=cut.turn_id,
                entry_sequence=int(row["entry_sequence"]),
                event_sequence=int(row["event_sequence"]),
                turn_completed=bool(terminal),
                pending_steer_at_settlement=pending_steer,
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
                       te.source_subagent_task_id AS target_entry_source_subagent_task_id,
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
