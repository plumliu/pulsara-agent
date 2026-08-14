"""Plan workflow and continuation operations."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping
from psycopg import Connection, IsolationLevel
from psycopg.rows import dict_row
from pulsara_agent.conversation_kernel.contracts import CommittedEventDraft, CommittedEventSubject, ConversationScopeKind, EntryKind, HostWriterGuard, InlineContent, canonical_digest
from pulsara_agent.primitives.context import FrozenJsonObjectFact, freeze_json, thaw_json
from pulsara_agent.primitives.permission import PERMISSION_PRESET_CONTRACT_FINGERPRINT, PERMISSION_PRESET_CONTRACT_ID, PermissionMode
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot, RunPermissionAdmissionSource, RunPermissionOverlay, build_run_permission_snapshot
from pulsara_agent.primitives.plan_workflow import ExtractedPlanDraft, PlanDraftTextChunk, PlanDraftDecision, PlanHandoffKind, PlanInteractionBinding, PlanInteractionKind, PlanQuestionAnswerKind, PlanQuestionContent, PlanWorkflowStatus, extract_plan_entry_reason, extract_plan_draft, extract_plan_question, read_plan_draft_chunk
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType, SubjectSlot
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .contracts import (
    AcceptedPlanResolution,
    AcceptedPlanToolBatch,
    AcceptedPlanWorkflowCommand,
    ConversationKernelConflict,
    ForcePlanExitPhaseOneResult,
    MAXIMUM_PLAN_DRAFT_REVISIONS_PER_WORKFLOW,
    MAXIMUM_PLAN_INTERACTIONS_PER_TURN,
    MAXIMUM_PLAN_INTERACTIONS_PER_WORKFLOW,
    PlanContinuationDisposition,
    PlanContinuationInspection,
    PlanDraftIdentityConflict,
    PlanQuestionAnswer,
    PlanToolBatchDisposition,
    PlanToolControlKind,
    PreparedPlanToolBatch,
    _EligiblePlanHandoff,
    _plan_inline,
    _stable_identity,
    plan_draft_review_semantic_candidate,
    plan_exit_semantic_fingerprint,
    plan_question_resolution_semantic_fingerprint,
)

from .kernel import _RepositoryKernel


def _plan_question_response(
    *, content: PlanQuestionContent, answer: PlanQuestionAnswer
) -> dict[str, object]:
    if answer.kind is PlanQuestionAnswerKind.OPTION:
        assert answer.option_ordinal is not None
        if not 0 <= answer.option_ordinal < len(content.options):
            raise ConversationKernelConflict("Plan option answer is absent")
        return {
            "answer_kind": "OPTION",
            "selected_option_ordinal": answer.option_ordinal,
            "selected_label": content.options[answer.option_ordinal].label,
        }
    if not content.allow_free_text:
        raise ConversationKernelConflict("Plan question does not allow free text")
    return {"answer_kind": "FREE_TEXT", "answer": answer.free_text}


class _PlanOperations:
    def accept_plan_tool_batch(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedPlanToolBatch,
        deadline_monotonic: float,
    ) -> AcceptedPlanToolBatch:
        """Accept one Plan control and cancel every sibling atomically."""

        if candidate.session_id != guard.session_id:
            raise ValueError("prepared Plan batch belongs to another session")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            winner = self._confirm_plan_tool_batch_in_transaction(
                connection, candidate=candidate
            )
            if winner is not None:
                return winner
            turn = connection.execute(
                """
                SELECT * FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s AND status = 'RUNNING'
                FOR UPDATE
                """,
                (guard.session_id, candidate.origin_turn_id),
            ).fetchone()
            if (
                turn is None
                or str(turn["workspace_id"]) != candidate.workspace_id
                or str(turn["conversation_scope_kind"]) != "ROOT"
                or str(turn["permission_snapshot_fingerprint"])
                != candidate.permission_snapshot.snapshot_fingerprint
                or self._permission_from_row(turn) != candidate.permission_snapshot
            ):
                raise ConversationKernelConflict(
                    "prepared Plan batch origin or permission drifted"
                )
            block_rows = connection.execute(
                """
                SELECT id, tool_call_id, tool_name, tool_arguments
                FROM pulsara_v3.assistant_message_blocks
                WHERE session_id = %s AND assistant_entry_id = %s
                  AND block_kind = 'TOOL_CALL'
                ORDER BY block_ordinal, id
                """,
                (guard.session_id, candidate.assistant_entry_id),
            ).fetchall()
            if tuple(
                (str(row["id"]), str(row["tool_call_id"]), str(row["tool_name"]))
                for row in block_rows
            ) != tuple(
                (item.block_id, item.tool_call_id, item.tool_name)
                for item in candidate.calls
            ):
                raise ConversationKernelConflict("prepared Plan batch blocks drifted")
            selected_row = block_rows[candidate.selected_call_ordinal]
            if freeze_json(dict(selected_row["tool_arguments"])) != (
                candidate.selected_arguments
            ):
                raise ConversationKernelConflict(
                    "prepared Plan control arguments drifted"
                )
            if candidate.selected_disposition is not PlanToolBatchDisposition.APPLY:
                return self._accept_rejected_plan_tool_batch_in_transaction(
                    connection,
                    guard=guard,
                    candidate=candidate,
                )
            active = connection.execute(
                """
                SELECT * FROM pulsara_v3.plan_workflows
                WHERE session_id = %s AND status = 'ACTIVE'
                FOR UPDATE
                """,
                (guard.session_id,),
            ).fetchone()
            question: PlanQuestionContent | None = None
            draft: ExtractedPlanDraft | None = None
            interaction_kind: PlanInteractionKind | None = None
            if candidate.control_kind is PlanToolControlKind.ENTER:
                if candidate.idempotent_existing:
                    if (
                        active is None
                        or str(active["id"]) != candidate.workflow_id
                        or int(active["workflow_revision"])
                        != candidate.expected_workflow_revision
                    ):
                        raise ConversationKernelConflict(
                            "idempotent Plan enter active workflow drifted"
                        )
                    workflow_ordinal = int(active["workflow_ordinal"])
                    workflow_revision = int(active["workflow_revision"])
                else:
                    workflow_ordinal, workflow_revision = (
                        self._insert_agent_plan_workflow(
                            connection, candidate=candidate, active=active
                        )
                    )
            else:
                workflow_ordinal, workflow_revision = (
                    self._advance_plan_workflow_for_interaction(
                        connection, candidate=candidate, active=active
                    )
                )
                question, draft, interaction_kind = self._insert_plan_interaction(
                    connection,
                    candidate=candidate,
                    workflow_revision=workflow_revision,
                )

            event_drafts: list[CommittedEventDraft] = []
            selected_result_entry_id: str | None = None
            final_entry_id: str | None = None
            for ordinal, call in enumerate(candidate.calls):
                if call.result_id is None:
                    continue
                assert call.result_entry_id is not None
                selected = ordinal == candidate.selected_call_ordinal
                if selected:
                    selected_result_entry_id = call.result_entry_id
                    if candidate.control_kind is PlanToolControlKind.ENTER:
                        payload = {
                            "status": "success",
                            "plan_control": (
                                "PLAN_ALREADY_ACTIVE"
                                if candidate.idempotent_existing
                                else "ENTERED_PLAN"
                            ),
                            "workflow_id": candidate.workflow_id,
                        }
                        control_workflow_id = candidate.workflow_id
                        control_interaction_id = None
                    else:
                        payload = {
                            "status": "success",
                            "plan_control": "DRAFT_SUBMITTED_FOR_REVIEW",
                            "workflow_id": candidate.workflow_id,
                            "interaction_id": candidate.interaction_id,
                        }
                        control_workflow_id = None
                        control_interaction_id = candidate.interaction_id
                    result_state = "SUCCESS"
                    origin_kind = "PLAN_CONTROL"
                else:
                    payload = {
                        "status": "cancelled_before_dispatch",
                        "reason": "plan_workflow_batch_barrier",
                    }
                    control_workflow_id = None
                    control_interaction_id = None
                    result_state = "CANCELLED_BEFORE_DISPATCH"
                    origin_kind = "POLICY_NO_ATTEMPT"
                entry_sequence = self._allocate_entry_sequence(
                    connection, guard.session_id
                )
                self._insert_entry(
                    connection,
                    session_id=guard.session_id,
                    workspace_id=candidate.workspace_id,
                    turn_id=candidate.origin_turn_id,
                    entry_id=call.result_entry_id,
                    entry_sequence=entry_sequence,
                    entry_kind=EntryKind.TOOL_RESULT,
                    scope_kind=ConversationScopeKind.ROOT,
                    scope_task_id=None,
                    content=_plan_inline(payload),
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.tool_results (
                        id, session_id, workspace_id, tool_call_entry_id,
                        tool_call_id, attempt_id, result_origin_kind,
                        control_plan_workflow_id, control_plan_interaction_id,
                        permission_snapshot_fingerprint,
                        result_entry_id, result_state,
                        observed_at, observation_duration_microseconds,
                        observation_origin_kind,
                        tool_reported_duration_microseconds
                    ) VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s,
                              %s, %s, %s, %s, NULL, %s, NULL)
                    """,
                    (
                        call.result_id,
                        guard.session_id,
                        candidate.workspace_id,
                        candidate.assistant_entry_id,
                        call.tool_call_id,
                        origin_kind,
                        control_workflow_id,
                        control_interaction_id,
                        candidate.permission_snapshot.snapshot_fingerprint,
                        call.result_entry_id,
                        result_state,
                        candidate.occurred_at,
                        (
                            "PLAN_CONTROL"
                            if origin_kind == "PLAN_CONTROL"
                            else "POLICY"
                        ),
                    ),
                )
                event_drafts.append(
                    self._plan_event(
                        CommittedEventType.TOOL_RESULT_ACCEPTED,
                        SubjectSlot.ENTRY,
                        call.result_entry_id,
                        candidate=candidate,
                        actor_kind="runtime",
                        payload={
                            "tool_call_id": call.tool_call_id,
                            "result_state": result_state,
                        },
                    )
                )
                final_entry_id = call.result_entry_id

            if not candidate.idempotent_existing:
                event_drafts.insert(
                    0,
                    self._plan_open_event(
                        candidate=candidate,
                        workflow_revision=workflow_revision,
                    ),
                )
            continuation_entry_id: str | None = None
            origin_completed = candidate.control_kind in {
                PlanToolControlKind.DRAFT,
            }
            if (
                candidate.control_kind is PlanToolControlKind.ENTER
                and not candidate.idempotent_existing
            ):
                origin_completed = True
            if origin_completed:
                if final_entry_id is None:
                    raise ConversationKernelConflict(
                        "terminal Plan batch has no final result entry"
                    )
                terminal = connection.execute(
                    """
                    UPDATE pulsara_v3.turns
                    SET status = 'COMPLETED', final_entry_id = %s,
                        terminal_reason = 'COMPLETED', terminal_at = clock_timestamp()
                    WHERE session_id = %s AND id = %s AND status = 'RUNNING'
                    RETURNING id
                    """,
                    (final_entry_id, guard.session_id, candidate.origin_turn_id),
                ).fetchone()
                if terminal is None:
                    raise ConversationKernelConflict("Plan origin turn is terminal")
                event_drafts.append(
                    self._plan_event(
                        CommittedEventType.TURN_COMPLETED,
                        SubjectSlot.TURN,
                        candidate.origin_turn_id,
                        candidate=candidate,
                        actor_kind="runtime",
                        payload={"final_entry_id": final_entry_id},
                    )
                )
            if (
                candidate.control_kind is PlanToolControlKind.ENTER
                and not candidate.idempotent_existing
            ):
                assert candidate.continuation_turn_id is not None
                assert candidate.continuation_entry_id is not None
                assert candidate.continuation_context_binding_revision_id is not None
                continuation_entry_id = candidate.continuation_entry_id
                continuation_sequence = self._allocate_entry_sequence(
                    connection, guard.session_id
                )
                permission = self._freeze_root_permission_snapshot(
                    connection,
                    session_id=guard.session_id,
                    snapshot_id=_stable_identity(
                        "permission-snapshot", candidate.continuation_turn_id
                    ),
                    requested_mode=candidate.permission_snapshot.requested_mode,
                    admission_source=(
                        RunPermissionAdmissionSource.RUNTIME_PLAN_CONTINUATION
                    ),
                    inherited_from_turn_id=candidate.origin_turn_id,
                    force_plan_workflow_id=candidate.workflow_id,
                    force_plan_read_only=True,
                )
                self._insert_plan_continuation_turn(
                    connection,
                    candidate=candidate,
                    permission=permission,
                    entry_sequence=continuation_sequence,
                    handoff_kind=PlanHandoffKind.ENTERED_PLAN,
                    interaction_id=None,
                    body=_plan_inline(
                        {
                            "transition": "ENTERED_PLAN",
                            "workflow_id": candidate.workflow_id,
                        }
                    ),
                )
                event_drafts.append(
                    self._plan_event(
                        CommittedEventType.PLAN_CONTINUATION_ACCEPTED,
                        SubjectSlot.ENTRY,
                        candidate.continuation_entry_id,
                        candidate=candidate,
                        actor_kind="runtime",
                        payload={
                            "handoff_kind": PlanHandoffKind.ENTERED_PLAN.value,
                            "workflow_id": candidate.workflow_id,
                        },
                    )
                )
            self._append_events(
                connection,
                guard,
                workspace_id=candidate.workspace_id,
                drafts=tuple(event_drafts),
            )
            return AcceptedPlanToolBatch(
                workflow_id=candidate.workflow_id,
                workflow_revision=workflow_revision,
                interaction_id=candidate.interaction_id,
                interaction_kind=interaction_kind,
                question=question,
                draft=draft,
                selected_result_entry_id=selected_result_entry_id,
                continuation_turn_id=candidate.continuation_turn_id,
                continuation_entry_id=continuation_entry_id,
                origin_turn_completed=origin_completed,
            )

    def _accept_rejected_plan_tool_batch_in_transaction(
        self,
        connection: Connection,
        *,
        guard: HostWriterGuard,
        candidate: PreparedPlanToolBatch,
    ) -> AcceptedPlanToolBatch:
        """Install a no-attempt Plan rejection and every sibling cancellation."""

        if candidate.selected_disposition is PlanToolBatchDisposition.APPLY:
            raise ConversationKernelConflict(
                "applied Plan batch entered rejection path"
            )
        event_drafts: list[CommittedEventDraft] = []
        selected_result_entry_id: str | None = None
        for ordinal, call in enumerate(candidate.calls):
            assert call.result_id is not None
            assert call.result_entry_id is not None
            selected = ordinal == candidate.selected_call_ordinal
            if selected:
                result_state = candidate.selected_disposition.value
                payload: Mapping[str, object] = {
                    "status": "error",
                    "plan_control": "REJECTED",
                    "error_kind": result_state,
                }
                selected_result_entry_id = call.result_entry_id
            else:
                result_state = "CANCELLED_BEFORE_DISPATCH"
                payload = {
                    "status": "cancelled_before_dispatch",
                    "reason": "plan_workflow_batch_barrier",
                }
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=candidate.workspace_id,
                turn_id=candidate.origin_turn_id,
                entry_id=call.result_entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.TOOL_RESULT,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=_plan_inline(payload),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.tool_results (
                    id, session_id, workspace_id, tool_call_entry_id,
                    tool_call_id, attempt_id, result_origin_kind,
                    control_plan_workflow_id, control_plan_interaction_id,
                    permission_snapshot_fingerprint,
                    result_entry_id, result_state,
                    observed_at, observation_duration_microseconds,
                    observation_origin_kind,
                    tool_reported_duration_microseconds
                ) VALUES (%s, %s, %s, %s, %s, NULL,
                          'POLICY_NO_ATTEMPT', NULL, NULL, %s, %s, %s,
                          %s, NULL, 'POLICY', NULL)
                """,
                (
                    call.result_id,
                    guard.session_id,
                    candidate.workspace_id,
                    candidate.assistant_entry_id,
                    call.tool_call_id,
                    candidate.permission_snapshot.snapshot_fingerprint,
                    call.result_entry_id,
                    result_state,
                    candidate.occurred_at,
                ),
            )
            event_drafts.append(
                self._plan_event(
                    CommittedEventType.TOOL_RESULT_ACCEPTED,
                    SubjectSlot.ENTRY,
                    call.result_entry_id,
                    candidate=candidate,
                    actor_kind="runtime",
                    payload={
                        "tool_call_id": call.tool_call_id,
                        "result_state": result_state,
                    },
                )
            )
        self._append_events(
            connection,
            guard,
            workspace_id=candidate.workspace_id,
            drafts=tuple(event_drafts),
        )
        return AcceptedPlanToolBatch(
            workflow_id=candidate.workflow_id,
            workflow_revision=int(candidate.expected_workflow_revision or 0),
            interaction_id=None,
            interaction_kind=None,
            question=None,
            draft=None,
            selected_result_entry_id=selected_result_entry_id,
            continuation_turn_id=None,
            continuation_entry_id=None,
            origin_turn_completed=False,
        )

    def confirm_plan_tool_batch_winner(
        self,
        *,
        candidate: PreparedPlanToolBatch,
        deadline_monotonic: float,
    ) -> AcceptedPlanToolBatch | None:
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            return self._confirm_plan_tool_batch_in_transaction(
                connection, candidate=candidate
            )

    def resolve_plan_question(
        self,
        guard: HostWriterGuard,
        *,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
        interaction_id: str,
        answer: PlanQuestionAnswer,
        result_id: str,
        result_entry_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedPlanResolution:
        semantic_digest = plan_question_resolution_semantic_fingerprint(
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            interaction_id=interaction_id,
            answer=answer,
            result_id=result_id,
            result_entry_id=result_entry_id,
        )
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            winner = self._confirm_plan_resolution_in_transaction(
                connection,
                session_id=guard.session_id,
                command_id=command_id,
                semantic_digest=semantic_digest,
                expected_question_answer=answer,
                expected_result_id=result_id,
                expected_result_entry_id=result_entry_id,
            )
            if winner is not None:
                return winner
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            winner = self._confirm_plan_resolution_in_transaction(
                connection,
                session_id=guard.session_id,
                command_id=command_id,
                semantic_digest=semantic_digest,
                expected_question_answer=answer,
                expected_result_id=result_id,
                expected_result_entry_id=result_entry_id,
            )
            if winner is not None:
                return winner
            interaction = connection.execute(
                """
                SELECT i.*, w.status AS workflow_status,
                       w.workflow_revision, w.resume_permission_mode,
                       b.tool_arguments, t.permission_snapshot_fingerprint
                FROM pulsara_v3.plan_interactions AS i
                JOIN pulsara_v3.plan_workflows AS w
                  ON w.session_id = i.session_id AND w.id = i.plan_workflow_id
                JOIN pulsara_v3.assistant_message_blocks AS b
                  ON b.session_id = i.session_id
                 AND b.assistant_entry_id = i.assistant_entry_id
                 AND b.tool_call_id = i.tool_call_id
                JOIN pulsara_v3.turns AS t
                  ON t.session_id = i.session_id AND t.id = i.origin_turn_id
                WHERE i.session_id = %s AND i.id = %s
                FOR UPDATE OF w, i
                """,
                (guard.session_id, interaction_id),
            ).fetchone()
            if (
                interaction is None
                or str(interaction["plan_workflow_id"]) != workflow_id
                or int(interaction["workflow_revision"]) != expected_workflow_revision
                or str(interaction["kind"]) != PlanInteractionKind.QUESTION.value
                or str(interaction["status"]) != "OPEN"
                or str(interaction["workflow_status"]) != "ACTIVE"
            ):
                raise ConversationKernelConflict("Plan question is not open")
            frozen = freeze_json(dict(interaction["tool_arguments"]))
            if not isinstance(frozen, FrozenJsonObjectFact):
                raise ConversationKernelConflict("Plan question arguments are invalid")
            content = extract_plan_question(
                interaction_id=interaction_id,
                binding=PlanInteractionBinding(
                    str(interaction["request_contract_id"]),
                    str(interaction["request_contract_version"]),
                    str(interaction["request_contract_fingerprint"]),
                ),
                arguments=frozen,
            )
            response = _plan_question_response(content=content, answer=answer)
            result_content = _plan_inline(
                {
                    "status": "success",
                    "plan_control": "QUESTION_ANSWERED",
                    "interaction_id": interaction_id,
                    **response,
                }
            )
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=str(interaction["workspace_id"]),
                turn_id=str(interaction["origin_turn_id"]),
                entry_id=result_entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.TOOL_RESULT,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=result_content,
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.tool_results (
                    id, session_id, workspace_id, tool_call_entry_id,
                    tool_call_id, attempt_id, result_origin_kind,
                    control_plan_interaction_id,
                    permission_snapshot_fingerprint,
                    result_entry_id, result_state,
                    observed_at, observation_duration_microseconds,
                    observation_origin_kind,
                    tool_reported_duration_microseconds
                ) VALUES (%s, %s, %s, %s, %s, NULL, 'PLAN_CONTROL',
                          %s, %s, %s, 'SUCCESS', %s, NULL, 'PLAN_CONTROL', NULL)
                """,
                (
                    result_id,
                    guard.session_id,
                    interaction["workspace_id"],
                    interaction["assistant_entry_id"],
                    interaction["tool_call_id"],
                    interaction_id,
                    str(interaction["permission_snapshot_fingerprint"]),
                    result_entry_id,
                    occurred_at,
                ),
            )
            response_digest = canonical_digest(
                "pulsara:plan-question-response:v1", response
            )
            connection.execute(
                """
                UPDATE pulsara_v3.plan_interactions
                SET status = 'ANSWERED', control_tool_result_id = %s,
                    resolution_command_id = %s,
                    response_semantic_digest = %s,
                    answer_kind = %s, selected_option_ordinal = %s,
                    resolved_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'OPEN'
                """,
                (
                    result_id,
                    command_id,
                    response_digest,
                    answer.kind.value,
                    answer.option_ordinal,
                    guard.session_id,
                    interaction_id,
                ),
            )
            revision = int(interaction["workflow_revision"]) + 1
            connection.execute(
                """
                UPDATE pulsara_v3.plan_workflows SET workflow_revision = %s
                WHERE session_id = %s AND id = %s AND status = 'ACTIVE'
                """,
                (revision, guard.session_id, interaction["plan_workflow_id"]),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_plan_interaction_id
                ) VALUES (%s, %s, 'RESOLVE_PLAN_INTERACTION',
                          'resolve_plan_question.v1', %s,
                          'PLAN_INTERACTION', %s)
                """,
                (guard.session_id, command_id, semantic_digest, interaction_id),
            )
            drafts = (
                CommittedEventDraft(
                    event_id=_stable_identity(
                        "event", command_id, "PlanQuestionAnswered"
                    ),
                    event_type=CommittedEventType.PLAN_QUESTION_ANSWERED,
                    subject=CommittedEventSubject(
                        SubjectSlot.PLAN_INTERACTION, interaction_id
                    ),
                    actor_kind="human",
                    actor_id=actor_id,
                    sensitivity_class="S1",
                    projection_profile="DEFAULT",
                    occurred_at=occurred_at,
                    payload={
                        "selected_option": answer.kind is PlanQuestionAnswerKind.OPTION,
                        "answer_present": True,
                    },
                ),
                CommittedEventDraft(
                    event_id=_stable_identity(
                        "event", result_entry_id, "ToolResultAccepted"
                    ),
                    event_type=CommittedEventType.TOOL_RESULT_ACCEPTED,
                    subject=CommittedEventSubject(SubjectSlot.ENTRY, result_entry_id),
                    actor_kind="runtime",
                    actor_id=actor_id,
                    sensitivity_class="S1",
                    projection_profile="DEFAULT",
                    occurred_at=occurred_at,
                    payload={
                        "tool_call_id": str(interaction["tool_call_id"]),
                        "result_state": "SUCCESS",
                    },
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=str(interaction["workspace_id"]),
                drafts=drafts,
            )
            return AcceptedPlanResolution(
                command_id=command_id,
                workflow_id=str(interaction["plan_workflow_id"]),
                workflow_status=PlanWorkflowStatus.ACTIVE,
                interaction_id=interaction_id,
                interaction_status="ANSWERED",
                resume_permission_mode=PermissionMode(
                    str(interaction["resume_permission_mode"])
                ),
                continuation_turn_id=None,
                continuation_entry_id=None,
                handoff_created_at_commit=False,
                question_result_entry_id=result_entry_id,
                workflow_revision=revision,
            )

    def confirm_plan_question_winner(
        self,
        *,
        session_id: str,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
        interaction_id: str,
        answer: PlanQuestionAnswer,
        result_id: str,
        result_entry_id: str,
        deadline_monotonic: float,
    ) -> AcceptedPlanResolution | None:
        """Query the exact semantic question winner without writer authority."""

        semantic_digest = plan_question_resolution_semantic_fingerprint(
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            interaction_id=interaction_id,
            answer=answer,
            result_id=result_id,
            result_entry_id=result_entry_id,
        )
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            return self._confirm_plan_resolution_in_transaction(
                connection,
                session_id=session_id,
                command_id=command_id,
                semantic_digest=semantic_digest,
                expected_question_answer=answer,
                expected_result_id=result_id,
                expected_result_entry_id=result_entry_id,
            )

    def enter_plan_by_user(
        self,
        guard: HostWriterGuard,
        *,
        command_id: str,
        workflow_id: str,
        entry_reason: str,
        resume_permission_mode: PermissionMode,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedPlanWorkflowCommand:
        if not entry_reason or len(entry_reason.encode("utf-8")) > 4096:
            raise ValueError("Plan entry reason is outside its bound")
        semantic_digest = canonical_digest(
            "pulsara:user-enter-plan:v1",
            {
                "workflow_id": workflow_id,
                "entry_reason": entry_reason,
                "resume_permission_mode": resume_permission_mode.value,
            },
        )
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            winner = self._confirm_plan_workflow_command_in_transaction(
                connection,
                session_id=guard.session_id,
                command_id=command_id,
                command_kind="ENTER_PLAN",
                semantic_digest=semantic_digest,
                expected_workflow_id=workflow_id,
            )
            if winner is not None:
                return winner
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            winner = self._confirm_plan_workflow_command_in_transaction(
                connection,
                session_id=guard.session_id,
                command_id=command_id,
                command_kind="ENTER_PLAN",
                semantic_digest=semantic_digest,
                expected_workflow_id=workflow_id,
            )
            if winner is not None:
                return winner
            if (
                connection.execute(
                    """SELECT 1 FROM pulsara_v3.turns
                   WHERE session_id = %s AND conversation_scope_kind = 'ROOT'
                     AND status = 'RUNNING'""",
                    (guard.session_id,),
                ).fetchone()
                is not None
            ):
                raise ConversationKernelConflict(
                    "user Plan enter requires an idle ROOT slot"
                )
            if (
                connection.execute(
                    """SELECT 1 FROM pulsara_v3.plan_interactions
                   WHERE session_id = %s AND status = 'OPEN'""",
                    (guard.session_id,),
                ).fetchone()
                is not None
            ):
                raise ConversationKernelConflict(
                    "user Plan enter conflicts with an open interaction"
                )
            if (
                connection.execute(
                    """SELECT 1 FROM pulsara_v3.plan_workflows
                   WHERE session_id = %s AND status = 'ACTIVE'""",
                    (guard.session_id,),
                ).fetchone()
                is not None
            ):
                raise ConversationKernelConflict("a Plan workflow is already active")
            workspace_id = self._workspace_id(connection, guard.session_id)
            ordinal = int(
                connection.execute(
                    """SELECT coalesce(max(workflow_ordinal), 0) + 1 AS next
                       FROM pulsara_v3.plan_workflows WHERE session_id = %s""",
                    (guard.session_id,),
                ).fetchone()["next"]
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_plan_workflow_id
                ) VALUES (%s, %s, 'ENTER_PLAN', 'enter_plan.v1', %s,
                          'PLAN_WORKFLOW', %s)
                """,
                (guard.session_id, command_id, semantic_digest, workflow_id),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.plan_workflows (
                    id, session_id, workspace_id, workflow_ordinal,
                    status, entered_by, entry_reason, entry_command_id,
                    resume_permission_mode, permission_contract_id,
                    permission_contract_fingerprint, workflow_revision
                ) VALUES (%s, %s, %s, %s, 'ACTIVE', 'USER', %s, %s,
                          %s, %s, %s, 1)
                """,
                (
                    workflow_id,
                    guard.session_id,
                    workspace_id,
                    ordinal,
                    entry_reason,
                    command_id,
                    resume_permission_mode.value,
                    PERMISSION_PRESET_CONTRACT_ID,
                    PERMISSION_PRESET_CONTRACT_FINGERPRINT,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    CommittedEventDraft(
                        event_id=_stable_identity(
                            "event", command_id, "PlanWorkflowEntered"
                        ),
                        event_type=CommittedEventType.PLAN_WORKFLOW_ENTERED,
                        subject=CommittedEventSubject(
                            SubjectSlot.PLAN_WORKFLOW, workflow_id
                        ),
                        actor_kind="human",
                        actor_id=actor_id,
                        sensitivity_class="PUBLIC",
                        projection_profile="DEFAULT",
                        occurred_at=occurred_at,
                        payload={"entered_by": "USER", "workflow_revision": 1},
                    ),
                ),
            )
            return AcceptedPlanWorkflowCommand(
                command_id,
                workflow_id,
                PlanWorkflowStatus.ACTIVE,
                resume_permission_mode,
                False,
                1,
            )

    def prepare_force_plan_exit(
        self,
        guard: HostWriterGuard,
        *,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
        expected_active_turn_id: str | None,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> ForcePlanExitPhaseOneResult:
        """Validate and canonically terminalize the exact force-exit target.

        This is phase one of the Host operation.  It creates no command row or
        durable coordination owner: the workflow remains ACTIVE until the Host
        has cancelled and joined its matching process-local ROOT task.
        """

        semantic_digest = plan_exit_semantic_fingerprint(
            command_kind="FORCE_EXIT_PLAN",
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
        )
        terminal_reason = f"PLAN_FORCE_EXIT:{semantic_digest}"
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            workflow = connection.execute(
                """SELECT * FROM pulsara_v3.plan_workflows
                   WHERE session_id = %s AND id = %s FOR UPDATE""",
                (guard.session_id, workflow_id),
            ).fetchone()
            if (
                workflow is None
                or str(workflow["status"]) != "ACTIVE"
                or int(workflow["workflow_revision"]) != expected_workflow_revision
            ):
                raise ConversationKernelConflict("Plan force-exit target drifted")

            expected_turn = None
            if expected_active_turn_id is not None:
                expected_turn = connection.execute(
                    """SELECT id, status, conversation_scope_kind,
                              permission_plan_workflow_id
                       FROM pulsara_v3.turns
                       WHERE session_id = %s AND id = %s FOR UPDATE""",
                    (guard.session_id, expected_active_turn_id),
                ).fetchone()
                if (
                    expected_turn is None
                    or str(expected_turn["conversation_scope_kind"]) != "ROOT"
                ):
                    raise ConversationKernelConflict(
                        "Plan force-exit Host turn identity drifted"
                    )

            running_rows = connection.execute(
                """SELECT id, workspace_id, permission_plan_workflow_id
                   FROM pulsara_v3.turns
                   WHERE session_id = %s
                     AND conversation_scope_kind = 'ROOT'
                     AND status = 'RUNNING'
                   FOR UPDATE""",
                (guard.session_id,),
            ).fetchall()
            if len(running_rows) > 1:
                raise ConversationKernelConflict(
                    "multiple canonical ROOT turns are running"
                )
            running = running_rows[0] if running_rows else None
            if running is not None:
                running_id = str(running["id"])
                if str(running["permission_plan_workflow_id"] or "") != workflow_id:
                    raise ConversationKernelConflict(
                        "Plan force-exit running turn belongs to another workflow"
                    )
                if (
                    expected_turn is not None
                    and str(expected_turn["status"]) == "RUNNING"
                    and running_id != expected_active_turn_id
                ):
                    raise ConversationKernelConflict(
                        "Plan force-exit physical and canonical turns diverged"
                    )

            open_interactions = connection.execute(
                """SELECT id FROM pulsara_v3.plan_interactions
                   WHERE session_id = %s AND plan_workflow_id = %s
                     AND status = 'OPEN' FOR UPDATE""",
                (guard.session_id, workflow_id),
            ).fetchall()
            if len(open_interactions) > 1:
                raise ConversationKernelConflict("multiple Plan interactions are open")
            interaction_aborted = bool(open_interactions)
            if interaction_aborted:
                connection.execute(
                    """UPDATE pulsara_v3.plan_interactions
                       SET status = 'ABORTED', aborted_at = clock_timestamp()
                       WHERE session_id = %s AND plan_workflow_id = %s
                         AND status = 'OPEN'""",
                    (guard.session_id, workflow_id),
                )

            interrupted_turn_id = None if running is None else str(running["id"])
            turn_interrupted = interrupted_turn_id is not None
            if turn_interrupted:
                connection.execute(
                    """UPDATE pulsara_v3.turns
                       SET status = 'INTERRUPTED', terminal_reason = %s,
                           terminal_at = clock_timestamp()
                       WHERE session_id = %s AND id = %s AND status = 'RUNNING'""",
                    (terminal_reason, guard.session_id, interrupted_turn_id),
                )
                self._append_events(
                    connection,
                    guard,
                    workspace_id=str(running["workspace_id"]),
                    drafts=(
                        CommittedEventDraft(
                            event_id=_stable_identity(
                                "event", command_id, "ForceExitTurnInterrupted"
                            ),
                            event_type=CommittedEventType.TURN_INTERRUPTED,
                            subject=CommittedEventSubject(
                                SubjectSlot.TURN, interrupted_turn_id
                            ),
                            actor_kind="runtime",
                            actor_id=actor_id,
                            sensitivity_class="PUBLIC",
                            projection_profile="DEFAULT",
                            occurred_at=occurred_at,
                            payload={"reason": terminal_reason},
                        ),
                    ),
                )
            return ForcePlanExitPhaseOneResult(
                workflow_id=workflow_id,
                workflow_revision=expected_workflow_revision,
                expected_active_turn_id=expected_active_turn_id,
                canonical_interrupted_turn_id=interrupted_turn_id,
                terminal_reason=terminal_reason,
                turn_interrupted_at_commit=turn_interrupted,
                interaction_aborted_at_commit=interaction_aborted,
            )

    def exit_plan_by_user(
        self,
        guard: HostWriterGuard,
        *,
        command_id: str,
        command_kind: str,
        workflow_id: str,
        expected_workflow_revision: int,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedPlanWorkflowCommand:
        target_status = (
            PlanWorkflowStatus.CANCELLED
            if command_kind == "CANCEL_PLAN"
            else PlanWorkflowStatus.FORCE_EXITED
        )
        semantic_digest = plan_exit_semantic_fingerprint(
            command_kind=command_kind,
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
        )
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            winner = self._confirm_plan_workflow_command_in_transaction(
                connection,
                session_id=guard.session_id,
                command_id=command_id,
                command_kind=command_kind,
                semantic_digest=semantic_digest,
                expected_workflow_id=workflow_id,
            )
            if winner is not None:
                return winner
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            winner = self._confirm_plan_workflow_command_in_transaction(
                connection,
                session_id=guard.session_id,
                command_id=command_id,
                command_kind=command_kind,
                semantic_digest=semantic_digest,
                expected_workflow_id=workflow_id,
            )
            if winner is not None:
                return winner
            workflow = connection.execute(
                """SELECT * FROM pulsara_v3.plan_workflows
                   WHERE session_id = %s AND id = %s FOR UPDATE""",
                (guard.session_id, workflow_id),
            ).fetchone()
            if (
                workflow is None
                or str(workflow["status"]) != "ACTIVE"
                or int(workflow["workflow_revision"]) != expected_workflow_revision
            ):
                raise ConversationKernelConflict("Plan exit target drifted")
            running = connection.execute(
                """SELECT 1 FROM pulsara_v3.turns
                   WHERE session_id = %s AND conversation_scope_kind = 'ROOT'
                     AND status = 'RUNNING'""",
                (guard.session_id,),
            ).fetchone()
            open_interaction = connection.execute(
                """SELECT * FROM pulsara_v3.plan_interactions
                   WHERE session_id = %s AND plan_workflow_id = %s
                     AND status = 'OPEN' FOR UPDATE""",
                (guard.session_id, workflow_id),
            ).fetchone()
            if command_kind == "CANCEL_PLAN" and (
                running is not None or open_interaction is not None
            ):
                raise ConversationKernelConflict(
                    "ordinary Plan cancel requires an idle workflow"
                )
            if running is not None:
                raise ConversationKernelConflict(
                    "force Plan exit requires physical turn termination first"
                )
            if open_interaction is not None:
                connection.execute(
                    """UPDATE pulsara_v3.plan_interactions
                       SET status = 'ABORTED', aborted_at = clock_timestamp()
                       WHERE session_id = %s AND id = %s AND status = 'OPEN'""",
                    (guard.session_id, open_interaction["id"]),
                )
            next_revision = expected_workflow_revision + 1
            connection.execute(
                """UPDATE pulsara_v3.plan_workflows
                   SET status = %s, workflow_revision = %s,
                       accepted_plan_interaction_id = NULL,
                       terminal_at = clock_timestamp()
                   WHERE session_id = %s AND id = %s AND status = 'ACTIVE'""",
                (
                    target_status.value,
                    next_revision,
                    guard.session_id,
                    workflow_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_plan_workflow_id
                ) VALUES (%s, %s, %s, 'plan_exit.v1', %s,
                          'PLAN_WORKFLOW', %s)
                """,
                (
                    guard.session_id,
                    command_id,
                    command_kind,
                    semantic_digest,
                    workflow_id,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=str(workflow["workspace_id"]),
                drafts=(
                    CommittedEventDraft(
                        event_id=_stable_identity(
                            "event", command_id, "PlanWorkflowExited"
                        ),
                        event_type=CommittedEventType.PLAN_WORKFLOW_EXITED,
                        subject=CommittedEventSubject(
                            SubjectSlot.PLAN_WORKFLOW, workflow_id
                        ),
                        actor_kind="human",
                        actor_id=actor_id,
                        sensitivity_class="PUBLIC",
                        projection_profile="DEFAULT",
                        occurred_at=occurred_at,
                        payload={"status": target_status.value},
                    ),
                ),
            )
            return AcceptedPlanWorkflowCommand(
                command_id,
                workflow_id,
                target_status,
                PermissionMode(str(workflow["resume_permission_mode"])),
                True,
                next_revision,
            )

    def resolve_plan_draft_review(
        self,
        guard: HostWriterGuard,
        *,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
        interaction_id: str,
        decision: PlanDraftDecision,
        feedback: str | None,
        continuation_turn_id: str | None,
        continuation_entry_id: str | None,
        continuation_context_binding_revision_id: str | None,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedPlanResolution:
        normalized_feedback, continuation_values, semantic_digest = (
            plan_draft_review_semantic_candidate(
                workflow_id=workflow_id,
                expected_workflow_revision=expected_workflow_revision,
                interaction_id=interaction_id,
                decision=decision,
                feedback=feedback,
                continuation_turn_id=continuation_turn_id,
                continuation_entry_id=continuation_entry_id,
                continuation_context_binding_revision_id=(
                    continuation_context_binding_revision_id
                ),
            )
        )
        creates_turn = decision in {
            PlanDraftDecision.APPROVE,
            PlanDraftDecision.REVISE,
        }
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            winner = self._confirm_plan_resolution_in_transaction(
                connection,
                session_id=guard.session_id,
                command_id=command_id,
                semantic_digest=semantic_digest,
            )
            if winner is not None:
                return winner
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            winner = self._confirm_plan_resolution_in_transaction(
                connection,
                session_id=guard.session_id,
                command_id=command_id,
                semantic_digest=semantic_digest,
            )
            if winner is not None:
                return winner
            interaction = connection.execute(
                """
                SELECT i.*, w.status AS workflow_status,
                       w.workflow_revision, w.workflow_ordinal,
                       w.resume_permission_mode, w.permission_contract_id,
                       w.permission_contract_fingerprint,
                       b.tool_arguments, t.permission_snapshot_fingerprint
                FROM pulsara_v3.plan_interactions AS i
                JOIN pulsara_v3.plan_workflows AS w
                  ON w.session_id = i.session_id AND w.id = i.plan_workflow_id
                JOIN pulsara_v3.assistant_message_blocks AS b
                  ON b.session_id = i.session_id
                 AND b.assistant_entry_id = i.assistant_entry_id
                 AND b.tool_call_id = i.tool_call_id
                JOIN pulsara_v3.turns AS t
                  ON t.session_id = i.session_id AND t.id = i.origin_turn_id
                WHERE i.session_id = %s AND i.id = %s
                FOR UPDATE OF w, i
                """,
                (guard.session_id, interaction_id),
            ).fetchone()
            if (
                interaction is None
                or str(interaction["plan_workflow_id"]) != workflow_id
                or int(interaction["workflow_revision"]) != expected_workflow_revision
                or str(interaction["kind"]) != PlanInteractionKind.DRAFT_REVIEW.value
                or str(interaction["status"]) != "OPEN"
                or str(interaction["workflow_status"]) != "ACTIVE"
            ):
                raise ConversationKernelConflict("Plan draft review is not open")
            extracted: ExtractedPlanDraft | None = None
            if decision is not PlanDraftDecision.CANCEL:
                raw_arguments = interaction["tool_arguments"]
                if not isinstance(raw_arguments, Mapping):
                    raise ConversationKernelConflict(
                        "Plan draft arguments are unavailable"
                    )
                frozen = freeze_json(dict(raw_arguments))
                if not isinstance(frozen, FrozenJsonObjectFact):
                    raise ConversationKernelConflict("Plan draft arguments are invalid")
                extracted = extract_plan_draft(
                    interaction_id=interaction_id,
                    assistant_entry_id=str(interaction["assistant_entry_id"]),
                    tool_call_id=str(interaction["tool_call_id"]),
                    binding=PlanInteractionBinding(
                        str(interaction["request_contract_id"]),
                        str(interaction["request_contract_version"]),
                        str(interaction["request_contract_fingerprint"]),
                    ),
                    request_semantic_digest=str(interaction["request_semantic_digest"]),
                    arguments=frozen,
                )
            status = {
                PlanDraftDecision.APPROVE: "APPROVED",
                PlanDraftDecision.REVISE: "REVISION_REQUESTED",
                PlanDraftDecision.CANCEL: "CANCELLED",
            }[decision]
            response_digest = canonical_digest(
                "pulsara:plan-draft-review-response:v1",
                {
                    "decision": decision.value,
                    "feedback": normalized_feedback,
                    "plan_utf8_digest": (
                        None
                        if extracted is None
                        else extracted.identity.plan_utf8_digest
                    ),
                },
            )
            connection.execute(
                """
                UPDATE pulsara_v3.plan_interactions
                SET status = %s, resolution_command_id = %s,
                    response_semantic_digest = %s,
                    decision_continuation_entry_id = %s,
                    feedback_present = %s, resolved_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'OPEN'
                """,
                (
                    status,
                    command_id,
                    response_digest,
                    continuation_entry_id,
                    normalized_feedback is not None,
                    guard.session_id,
                    interaction_id,
                ),
            )
            revision = int(interaction["workflow_revision"]) + 1
            terminal_status = {
                PlanDraftDecision.APPROVE: "APPROVED",
                PlanDraftDecision.REVISE: "ACTIVE",
                PlanDraftDecision.CANCEL: "CANCELLED",
            }[decision]
            connection.execute(
                """
                UPDATE pulsara_v3.plan_workflows
                SET status = %s, workflow_revision = %s,
                    accepted_plan_interaction_id = %s,
                    terminal_at = CASE WHEN %s = 'ACTIVE' THEN NULL
                                       ELSE clock_timestamp() END
                WHERE session_id = %s AND id = %s AND status = 'ACTIVE'
                """,
                (
                    terminal_status,
                    revision,
                    (interaction_id if decision is PlanDraftDecision.APPROVE else None),
                    terminal_status,
                    guard.session_id,
                    interaction["plan_workflow_id"],
                ),
            )
            handoff_kind: PlanHandoffKind | None = None
            if creates_turn:
                assert continuation_turn_id is not None
                assert continuation_entry_id is not None
                assert continuation_context_binding_revision_id is not None
                handoff_kind = (
                    PlanHandoffKind.APPROVED_PLAN
                    if decision is PlanDraftDecision.APPROVE
                    else PlanHandoffKind.REVISION_REQUESTED
                )
                if decision is PlanDraftDecision.REVISE:
                    permission = build_run_permission_snapshot(
                        snapshot_id=_stable_identity(
                            "permission-snapshot", continuation_turn_id
                        ),
                        requested_mode=PermissionMode(
                            str(interaction["resume_permission_mode"])
                        ),
                        effective_mode=PermissionMode.READ_ONLY,
                        admission_source=(
                            RunPermissionAdmissionSource.RUNTIME_PLAN_CONTINUATION
                        ),
                        overlay=RunPermissionOverlay.PLAN_READ_ONLY,
                        plan_context_ordinal_at_admission=int(
                            interaction["workflow_ordinal"]
                        ),
                        plan_workflow_id=str(interaction["plan_workflow_id"]),
                        plan_workflow_revision_at_admission=revision,
                        inherited_from_turn_id=str(interaction["origin_turn_id"]),
                    )
                else:
                    permission = build_run_permission_snapshot(
                        snapshot_id=_stable_identity(
                            "permission-snapshot", continuation_turn_id
                        ),
                        requested_mode=PermissionMode(
                            str(interaction["resume_permission_mode"])
                        ),
                        effective_mode=PermissionMode(
                            str(interaction["resume_permission_mode"])
                        ),
                        admission_source=(
                            RunPermissionAdmissionSource.RUNTIME_PLAN_CONTINUATION
                        ),
                        overlay=RunPermissionOverlay.NONE,
                        plan_context_ordinal_at_admission=int(
                            interaction["workflow_ordinal"]
                        ),
                        inherited_from_turn_id=str(interaction["origin_turn_id"]),
                    )
                entry_sequence = self._allocate_entry_sequence(
                    connection, guard.session_id
                )
                body_payload: dict[str, object] = {
                    "transition": handoff_kind.value,
                    "workflow_id": str(interaction["plan_workflow_id"]),
                    "interaction_id": interaction_id,
                }
                if normalized_feedback is not None:
                    body_payload["feedback"] = normalized_feedback
                if decision is PlanDraftDecision.APPROVE:
                    assert extracted is not None
                    body_payload["approved_plan"] = {
                        "plan_utf8_size": extracted.identity.plan_utf8_size,
                        "plan_utf8_digest": extracted.identity.plan_utf8_digest,
                        "assistant_entry_id": extracted.identity.assistant_entry_id,
                        "tool_call_id": extracted.identity.tool_call_id,
                    }
                self._insert_resolution_plan_continuation(
                    connection,
                    session_id=guard.session_id,
                    workspace_id=str(interaction["workspace_id"]),
                    workflow_id=str(interaction["plan_workflow_id"]),
                    interaction_id=interaction_id,
                    origin_turn_id=str(interaction["origin_turn_id"]),
                    turn_id=continuation_turn_id,
                    entry_id=continuation_entry_id,
                    context_binding_revision_id=(
                        continuation_context_binding_revision_id
                    ),
                    entry_sequence=entry_sequence,
                    permission=permission,
                    handoff_kind=handoff_kind,
                    body=_plan_inline(body_payload),
                )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_plan_interaction_id
                ) VALUES (%s, %s, 'RESOLVE_PLAN_INTERACTION',
                          'resolve_plan_draft.v1', %s,
                          'PLAN_INTERACTION', %s)
                """,
                (guard.session_id, command_id, semantic_digest, interaction_id),
            )
            drafts: list[CommittedEventDraft] = [
                CommittedEventDraft(
                    event_id=_stable_identity(
                        "event", command_id, "PlanDraftDecisionAccepted"
                    ),
                    event_type=CommittedEventType.PLAN_DRAFT_DECISION_ACCEPTED,
                    subject=CommittedEventSubject(
                        SubjectSlot.PLAN_INTERACTION, interaction_id
                    ),
                    actor_kind="human",
                    actor_id=actor_id,
                    sensitivity_class="S1",
                    projection_profile="DEFAULT",
                    occurred_at=occurred_at,
                    payload={
                        "decision": decision.value,
                        "feedback_present": normalized_feedback is not None,
                    },
                )
            ]
            if decision in {PlanDraftDecision.APPROVE, PlanDraftDecision.CANCEL}:
                drafts.append(
                    CommittedEventDraft(
                        event_id=_stable_identity(
                            "event", command_id, "PlanWorkflowExited"
                        ),
                        event_type=CommittedEventType.PLAN_WORKFLOW_EXITED,
                        subject=CommittedEventSubject(
                            SubjectSlot.PLAN_WORKFLOW,
                            str(interaction["plan_workflow_id"]),
                        ),
                        actor_kind="human",
                        actor_id=actor_id,
                        sensitivity_class="PUBLIC",
                        projection_profile="DEFAULT",
                        occurred_at=occurred_at,
                        payload={"status": terminal_status},
                    )
                )
            if creates_turn:
                assert continuation_entry_id is not None
                assert handoff_kind is not None
                drafts.append(
                    CommittedEventDraft(
                        event_id=_stable_identity(
                            "event", command_id, "PlanContinuationAccepted"
                        ),
                        event_type=CommittedEventType.PLAN_CONTINUATION_ACCEPTED,
                        subject=CommittedEventSubject(
                            SubjectSlot.ENTRY, continuation_entry_id
                        ),
                        actor_kind="runtime",
                        actor_id=actor_id,
                        sensitivity_class="S1",
                        projection_profile="DEFAULT",
                        occurred_at=occurred_at,
                        payload={
                            "handoff_kind": handoff_kind.value,
                            "workflow_id": str(interaction["plan_workflow_id"]),
                        },
                    )
                )
            self._append_events(
                connection,
                guard,
                workspace_id=str(interaction["workspace_id"]),
                drafts=tuple(drafts),
            )
            return AcceptedPlanResolution(
                command_id=command_id,
                workflow_id=str(interaction["plan_workflow_id"]),
                workflow_status=PlanWorkflowStatus(terminal_status),
                interaction_id=interaction_id,
                interaction_status=status,
                resume_permission_mode=PermissionMode(
                    str(interaction["resume_permission_mode"])
                ),
                continuation_turn_id=continuation_turn_id,
                continuation_entry_id=continuation_entry_id,
                handoff_created_at_commit=(decision is PlanDraftDecision.CANCEL),
                workflow_revision=revision,
                draft_decision=decision,
            )

    def confirm_plan_draft_review_winner(
        self,
        *,
        session_id: str,
        command_id: str,
        workflow_id: str,
        expected_workflow_revision: int,
        interaction_id: str,
        decision: PlanDraftDecision,
        feedback: str | None,
        continuation_turn_id: str | None,
        continuation_entry_id: str | None,
        continuation_context_binding_revision_id: str | None,
        deadline_monotonic: float,
    ) -> AcceptedPlanResolution | None:
        """Query one stable Plan review winner without writer authority."""

        _, _, semantic_digest = plan_draft_review_semantic_candidate(
            workflow_id=workflow_id,
            expected_workflow_revision=expected_workflow_revision,
            interaction_id=interaction_id,
            decision=decision,
            feedback=feedback,
            continuation_turn_id=continuation_turn_id,
            continuation_entry_id=continuation_entry_id,
            continuation_context_binding_revision_id=(
                continuation_context_binding_revision_id
            ),
        )
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            return self._confirm_plan_resolution_in_transaction(
                connection,
                session_id=session_id,
                command_id=command_id,
                semantic_digest=semantic_digest,
            )

    def inspect_plan_continuation(
        self,
        *,
        session_id: str,
        turn_id: str,
        initial_entry_id: str,
        workflow_id: str,
        interaction_id: str | None,
        handoff_kind: PlanHandoffKind,
        deadline_monotonic: float,
    ) -> PlanContinuationInspection | None:
        """Read an exact canonical successor without write authority."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            row = connection.execute(
                """
                SELECT t.status, t.initial_entry_id,
                       e.source_plan_workflow_id,
                       e.source_plan_interaction_id,
                       e.source_plan_handoff_kind,
                       s.lifecycle AS session_lifecycle,
                       s.writer_generation, s.writer_lease_owner_id
                FROM pulsara_v3.turns AS t
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = t.session_id AND e.id = t.initial_entry_id
                JOIN pulsara_v3.sessions AS s ON s.id = t.session_id
                WHERE t.session_id = %s AND t.id = %s
                """,
                (session_id, turn_id),
            ).fetchone()
            if row is None:
                return None
            observed_interaction = (
                None
                if row["source_plan_interaction_id"] is None
                else str(row["source_plan_interaction_id"])
            )
            if (
                str(row["initial_entry_id"]) != initial_entry_id
                or str(row["source_plan_workflow_id"]) != workflow_id
                or observed_interaction != interaction_id
                or str(row["source_plan_handoff_kind"]) != handoff_kind.value
            ):
                raise ConversationKernelConflict(
                    "Plan continuation identity names another winner"
                )
            return PlanContinuationInspection(
                turn_id=turn_id,
                initial_entry_id=initial_entry_id,
                status=str(row["status"]),
                workflow_id=workflow_id,
                interaction_id=interaction_id,
                handoff_kind=handoff_kind,
                session_lifecycle=str(row["session_lifecycle"]),
                writer_generation=int(row["writer_generation"]),
                writer_owner_id=(
                    None
                    if row["writer_lease_owner_id"] is None
                    else str(row["writer_lease_owner_id"])
                ),
            )

    def read_plan_question_content(
        self,
        *,
        session_id: str,
        interaction_id: str,
        deadline_monotonic: float,
    ) -> PlanQuestionContent:
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            row = self._plan_interaction_content_row(
                connection,
                session_id=session_id,
                interaction_id=interaction_id,
            )
            if str(row["kind"]) != PlanInteractionKind.QUESTION.value:
                raise ConversationKernelConflict("Plan interaction is not a question")
            arguments = freeze_json(dict(row["tool_arguments"]))
            if not isinstance(arguments, FrozenJsonObjectFact):
                raise ConversationKernelConflict("Plan question arguments are invalid")
            return extract_plan_question(
                interaction_id=interaction_id,
                binding=PlanInteractionBinding(
                    str(row["request_contract_id"]),
                    str(row["request_contract_version"]),
                    str(row["request_contract_fingerprint"]),
                ),
                arguments=arguments,
            )

    def read_plan_draft_text_chunk(
        self,
        *,
        session_id: str,
        interaction_id: str,
        offset_utf8_bytes: int,
        limit_bytes: int,
        expected_plan_utf8_digest: str | None = None,
        deadline_monotonic: float,
    ) -> PlanDraftTextChunk:
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            row = self._plan_interaction_content_row(
                connection,
                session_id=session_id,
                interaction_id=interaction_id,
            )
            if str(row["kind"]) != PlanInteractionKind.DRAFT_REVIEW.value:
                raise ConversationKernelConflict("Plan interaction is not a draft")
            arguments = freeze_json(dict(row["tool_arguments"]))
            if not isinstance(arguments, FrozenJsonObjectFact):
                raise ConversationKernelConflict("Plan draft arguments are invalid")
            draft = extract_plan_draft(
                interaction_id=interaction_id,
                assistant_entry_id=str(row["assistant_entry_id"]),
                tool_call_id=str(row["tool_call_id"]),
                binding=PlanInteractionBinding(
                    str(row["request_contract_id"]),
                    str(row["request_contract_version"]),
                    str(row["request_contract_fingerprint"]),
                ),
                request_semantic_digest=str(row["request_semantic_digest"]),
                arguments=arguments,
            )
            if (
                expected_plan_utf8_digest is not None
                and draft.identity.plan_utf8_digest != expected_plan_utf8_digest
            ):
                raise PlanDraftIdentityConflict("Plan draft content identity changed")
            return read_plan_draft_chunk(
                draft,
                offset_utf8_bytes=offset_utf8_bytes,
                limit_bytes=limit_bytes,
            )

    @staticmethod
    def _plan_interaction_content_row(
        connection: Connection,
        *,
        session_id: str,
        interaction_id: str,
    ) -> Mapping[str, object]:
        row = connection.execute(
            """
            SELECT i.*, b.tool_arguments
            FROM pulsara_v3.plan_interactions AS i
            JOIN pulsara_v3.assistant_message_blocks AS b
              ON b.session_id = i.session_id
             AND b.assistant_entry_id = i.assistant_entry_id
             AND b.tool_call_id = i.tool_call_id
            WHERE i.session_id = %s AND i.id = %s
            """,
            (session_id, interaction_id),
        ).fetchone()
        if row is None or row["tool_arguments"] is None:
            raise KeyError(interaction_id)
        return row

    def _insert_agent_plan_workflow(
        self,
        connection: Connection,
        *,
        candidate: PreparedPlanToolBatch,
        active: Mapping[str, object] | None,
    ) -> tuple[int, int]:
        if active is not None:
            raise ConversationKernelConflict(
                "Plan enter candidate lost the no-active-workflow cut"
            )
        try:
            reason = extract_plan_entry_reason(
                binding=candidate.request_binding,
                arguments=candidate.selected_arguments,
            )
        except ValueError as exc:
            raise ConversationKernelConflict("Plan entry reason is invalid") from exc
        ordinal = int(
            connection.execute(
                """
                SELECT coalesce(max(workflow_ordinal), 0) + 1 AS next
                FROM pulsara_v3.plan_workflows WHERE session_id = %s
                """,
                (candidate.session_id,),
            ).fetchone()["next"]
        )
        selected = candidate.calls[candidate.selected_call_ordinal]
        connection.execute(
            """
            INSERT INTO pulsara_v3.plan_workflows (
                id, session_id, workspace_id, workflow_ordinal,
                status, entered_by, entry_reason, entry_turn_id,
                entry_assistant_entry_id, entry_tool_call_id,
                resume_permission_mode, permission_contract_id,
                permission_contract_fingerprint, workflow_revision
            ) VALUES (%s, %s, %s, %s, 'ACTIVE', 'AGENT', %s,
                      %s, %s, %s, %s, %s, %s, 1)
            """,
            (
                candidate.workflow_id,
                candidate.session_id,
                candidate.workspace_id,
                ordinal,
                reason,
                candidate.origin_turn_id,
                candidate.assistant_entry_id,
                selected.tool_call_id,
                candidate.permission_snapshot.requested_mode.value,
                candidate.permission_snapshot.permission_contract_id,
                candidate.permission_snapshot.permission_contract_fingerprint,
            ),
        )
        return ordinal, 1

    @staticmethod
    def _advance_plan_workflow_for_interaction(
        connection: Connection,
        *,
        candidate: PreparedPlanToolBatch,
        active: Mapping[str, object] | None,
    ) -> tuple[int, int]:
        if (
            active is None
            or str(active["id"]) != candidate.workflow_id
            or int(active["workflow_revision"]) != candidate.expected_workflow_revision
        ):
            raise ConversationKernelConflict(
                "prepared Plan interaction workflow drifted"
            )
        revision = int(active["workflow_revision"]) + 1
        connection.execute(
            """
            UPDATE pulsara_v3.plan_workflows SET workflow_revision = %s
            WHERE session_id = %s AND id = %s
              AND status = 'ACTIVE' AND workflow_revision = %s
            """,
            (
                revision,
                candidate.session_id,
                candidate.workflow_id,
                candidate.expected_workflow_revision,
            ),
        )
        return int(active["workflow_ordinal"]), revision

    @staticmethod
    def _insert_plan_interaction(
        connection: Connection,
        *,
        candidate: PreparedPlanToolBatch,
        workflow_revision: int,
    ) -> tuple[
        PlanQuestionContent | None,
        ExtractedPlanDraft | None,
        PlanInteractionKind,
    ]:
        del workflow_revision
        assert candidate.interaction_id is not None
        counts = connection.execute(
            """
            SELECT count(*)::integer AS workflow_total,
                   count(*) FILTER (
                       WHERE origin_turn_id = %s
                   )::integer AS turn_total,
                   count(*) FILTER (
                       WHERE kind = 'DRAFT_REVIEW'
                   )::integer AS draft_total,
                   coalesce(max(interaction_ordinal), 0) + 1 AS next
            FROM pulsara_v3.plan_interactions
            WHERE plan_workflow_id = %s
            """,
            (candidate.origin_turn_id, candidate.workflow_id),
        ).fetchone()
        if (
            int(counts["workflow_total"]) >= MAXIMUM_PLAN_INTERACTIONS_PER_WORKFLOW
            or int(counts["turn_total"]) >= MAXIMUM_PLAN_INTERACTIONS_PER_TURN
            or (
                candidate.control_kind is PlanToolControlKind.DRAFT
                and int(counts["draft_total"])
                >= MAXIMUM_PLAN_DRAFT_REVISIONS_PER_WORKFLOW
            )
        ):
            raise ConversationKernelConflict("Plan interaction budget is exhausted")
        ordinal = int(counts["next"])
        request_digest = canonical_digest(
            "pulsara:plan-interaction-request:v1",
            {
                "binding": {
                    "id": candidate.request_binding.contract_id,
                    "version": candidate.request_binding.contract_version,
                    "fingerprint": candidate.request_binding.contract_fingerprint,
                },
                "arguments": thaw_json(candidate.selected_arguments),
            },
        )
        selected = candidate.calls[candidate.selected_call_ordinal]
        if candidate.control_kind is PlanToolControlKind.QUESTION:
            question = extract_plan_question(
                interaction_id=candidate.interaction_id,
                binding=candidate.request_binding,
                arguments=candidate.selected_arguments,
            )
            draft = None
            kind = PlanInteractionKind.QUESTION
            control_result_id = None
        else:
            question = None
            draft = extract_plan_draft(
                interaction_id=candidate.interaction_id,
                assistant_entry_id=candidate.assistant_entry_id,
                tool_call_id=selected.tool_call_id,
                binding=candidate.request_binding,
                request_semantic_digest=request_digest,
                arguments=candidate.selected_arguments,
            )
            kind = PlanInteractionKind.DRAFT_REVIEW
            control_result_id = selected.result_id
        connection.execute(
            """
            INSERT INTO pulsara_v3.plan_interactions (
                id, session_id, workspace_id, plan_workflow_id,
                interaction_ordinal, kind, status, origin_turn_id,
                assistant_entry_id, tool_call_id,
                request_contract_id, request_contract_version,
                request_contract_fingerprint, request_semantic_digest,
                control_tool_result_id
            ) VALUES (%s, %s, %s, %s, %s, %s, 'OPEN', %s, %s,
                      %s, %s, %s, %s, %s, %s)
            """,
            (
                candidate.interaction_id,
                candidate.session_id,
                candidate.workspace_id,
                candidate.workflow_id,
                ordinal,
                kind.value,
                candidate.origin_turn_id,
                candidate.assistant_entry_id,
                selected.tool_call_id,
                candidate.request_binding.contract_id,
                candidate.request_binding.contract_version,
                candidate.request_binding.contract_fingerprint,
                request_digest,
                control_result_id,
            ),
        )
        return question, draft, kind

    @staticmethod
    def _insert_plan_continuation_turn(
        connection: Connection,
        *,
        candidate: PreparedPlanToolBatch,
        permission: FrozenRunPermissionSnapshot,
        entry_sequence: int,
        handoff_kind: PlanHandoffKind,
        interaction_id: str | None,
        body: InlineContent,
    ) -> None:
        assert candidate.continuation_turn_id is not None
        assert candidate.continuation_entry_id is not None
        assert candidate.continuation_context_binding_revision_id is not None
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
                permission_contract_fingerprint, permission_snapshot_fingerprint
            ) VALUES (%s, %s, %s, 'ROOT', 'RUNNING', %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                candidate.continuation_turn_id,
                candidate.session_id,
                candidate.workspace_id,
                candidate.continuation_entry_id,
                candidate.continuation_context_binding_revision_id,
                *_RepositoryKernel._permission_columns(permission),
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
                candidate.continuation_context_binding_revision_id,
                candidate.session_id,
                candidate.continuation_turn_id,
                entry_sequence - 1,
            ),
        )
        _RepositoryKernel._insert_entry(
            connection,
            session_id=candidate.session_id,
            workspace_id=candidate.workspace_id,
            turn_id=candidate.continuation_turn_id,
            entry_id=candidate.continuation_entry_id,
            entry_sequence=entry_sequence,
            entry_kind=EntryKind.PLAN_CONTINUATION,
            scope_kind=ConversationScopeKind.ROOT,
            scope_task_id=None,
            content=body,
            source_plan_workflow_id=candidate.workflow_id,
            source_plan_interaction_id=interaction_id,
            source_plan_handoff_kind=handoff_kind,
        )

    @staticmethod
    def _plan_event(
        event_type: CommittedEventType,
        slot: SubjectSlot,
        subject_id: str,
        *,
        candidate: PreparedPlanToolBatch,
        actor_kind: str,
        payload: Mapping[str, object],
    ) -> CommittedEventDraft:
        return CommittedEventDraft(
            event_id=_stable_identity(
                "event", candidate.candidate_fingerprint, event_type.value, subject_id
            ),
            event_type=event_type,
            subject=CommittedEventSubject(slot, subject_id),
            actor_kind=actor_kind,
            actor_id=candidate.actor_id,
            sensitivity_class="PUBLIC",
            projection_profile="DEFAULT",
            occurred_at=candidate.occurred_at,
            payload=payload,
        )

    @classmethod
    def _plan_open_event(
        cls,
        *,
        candidate: PreparedPlanToolBatch,
        workflow_revision: int,
    ) -> CommittedEventDraft:
        if candidate.control_kind is PlanToolControlKind.ENTER:
            return cls._plan_event(
                CommittedEventType.PLAN_WORKFLOW_ENTERED,
                SubjectSlot.PLAN_WORKFLOW,
                candidate.workflow_id,
                candidate=candidate,
                actor_kind="model",
                payload={"entered_by": "AGENT", "workflow_revision": 1},
            )
        assert candidate.interaction_id is not None
        event_type = (
            CommittedEventType.PLAN_QUESTION_ASKED
            if candidate.control_kind is PlanToolControlKind.QUESTION
            else CommittedEventType.PLAN_DRAFT_SUBMITTED
        )
        return cls._plan_event(
            event_type,
            SubjectSlot.PLAN_INTERACTION,
            candidate.interaction_id,
            candidate=candidate,
            actor_kind="model",
            payload={
                "workflow_id": candidate.workflow_id,
                "workflow_revision": workflow_revision,
            },
        )

    def _confirm_plan_tool_batch_in_transaction(
        self,
        connection: Connection,
        *,
        candidate: PreparedPlanToolBatch,
    ) -> AcceptedPlanToolBatch | None:
        assistant = connection.execute(
            """SELECT * FROM pulsara_v3.transcript_entries
               WHERE session_id = %s AND id = %s""",
            (candidate.session_id, candidate.assistant_entry_id),
        ).fetchone()
        block_rows = connection.execute(
            """SELECT id, tool_call_id, tool_name, tool_arguments
               FROM pulsara_v3.assistant_message_blocks
               WHERE session_id = %s AND assistant_entry_id = %s
                 AND block_kind = 'TOOL_CALL'
               ORDER BY block_ordinal, id""",
            (candidate.session_id, candidate.assistant_entry_id),
        ).fetchall()
        if (
            assistant is None
            or str(assistant["workspace_id"]) != candidate.workspace_id
            or str(assistant["turn_id"]) != candidate.origin_turn_id
            or str(assistant["entry_kind"]) != EntryKind.ASSISTANT_TOOL_REQUEST.value
            or str(assistant["conversation_scope_kind"]) != "ROOT"
            or tuple(
                (str(row["id"]), str(row["tool_call_id"]), str(row["tool_name"]))
                for row in block_rows
            )
            != tuple(
                (item.block_id, item.tool_call_id, item.tool_name)
                for item in candidate.calls
            )
            or freeze_json(
                dict(block_rows[candidate.selected_call_ordinal]["tool_arguments"])
            )
            != candidate.selected_arguments
        ):
            raise ConversationKernelConflict(
                "Plan batch assistant or tool blocks drifted"
            )
        if candidate.control_kind is PlanToolControlKind.ENTER:
            key_row = connection.execute(
                "SELECT * FROM pulsara_v3.plan_workflows WHERE session_id = %s AND id = %s",
                (candidate.session_id, candidate.workflow_id),
            ).fetchone()
        else:
            key_row = connection.execute(
                "SELECT * FROM pulsara_v3.plan_interactions WHERE session_id = %s AND id = %s",
                (candidate.session_id, candidate.interaction_id),
            ).fetchone()
        result_ids = tuple(
            item.result_id for item in candidate.calls if item.result_id is not None
        )
        result_rows = (
            []
            if not result_ids
            else connection.execute(
                "SELECT * FROM pulsara_v3.tool_results WHERE session_id = %s AND id = ANY(%s)",
                (candidate.session_id, list(result_ids)),
            ).fetchall()
        )
        continuation = (
            None
            if candidate.continuation_entry_id is None
            else connection.execute(
                "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id = %s AND id = %s",
                (candidate.session_id, candidate.continuation_entry_id),
            ).fetchone()
        )
        if candidate.selected_disposition is not PlanToolBatchDisposition.APPLY:
            if not result_rows:
                return None
            if (
                key_row is not None
                or len(result_rows) != len(result_ids)
                or continuation is not None
            ):
                raise ConversationKernelConflict(
                    "rejected Plan batch winner is partially installed"
                )
            result_by_id = {str(row["id"]): row for row in result_rows}
            selected_result_entry_id: str | None = None
            for ordinal, item in enumerate(candidate.calls):
                assert item.result_id is not None
                assert item.result_entry_id is not None
                row = result_by_id.get(item.result_id)
                selected = ordinal == candidate.selected_call_ordinal
                expected_state = (
                    candidate.selected_disposition.value
                    if selected
                    else "CANCELLED_BEFORE_DISPATCH"
                )
                if (
                    row is None
                    or str(row["workspace_id"]) != candidate.workspace_id
                    or str(row["tool_call_entry_id"]) != candidate.assistant_entry_id
                    or str(row["tool_call_id"]) != item.tool_call_id
                    or str(row["result_entry_id"]) != item.result_entry_id
                    or str(row["result_origin_kind"]) != "POLICY_NO_ATTEMPT"
                    or row["attempt_id"] is not None
                    or row["control_plan_workflow_id"] is not None
                    or row["control_plan_interaction_id"] is not None
                    or str(row["permission_snapshot_fingerprint"])
                    != candidate.permission_snapshot.snapshot_fingerprint
                    or str(row["result_state"]) != expected_state
                    or row["observed_at"] != candidate.occurred_at
                    or row["observation_duration_microseconds"] is not None
                    or str(row["observation_origin_kind"]) != "POLICY"
                    or row["tool_reported_duration_microseconds"] is not None
                ):
                    raise ConversationKernelConflict(
                        "rejected Plan batch result identity conflicts"
                    )
                expected_payload: Mapping[str, object]
                if selected:
                    expected_payload = {
                        "status": "error",
                        "plan_control": "REJECTED",
                        "error_kind": expected_state,
                    }
                    selected_result_entry_id = item.result_entry_id
                else:
                    expected_payload = {
                        "status": "cancelled_before_dispatch",
                        "reason": "plan_workflow_batch_barrier",
                    }
                entry = connection.execute(
                    """
                    SELECT * FROM pulsara_v3.transcript_entries
                    WHERE session_id = %s AND id = %s
                    """,
                    (candidate.session_id, item.result_entry_id),
                ).fetchone()
                if (
                    entry is None
                    or str(entry["workspace_id"]) != candidate.workspace_id
                    or str(entry["turn_id"]) != candidate.origin_turn_id
                    or str(entry["entry_kind"]) != EntryKind.TOOL_RESULT.value
                    or str(entry["conversation_scope_kind"]) != "ROOT"
                    or self._content_from_row(entry) != _plan_inline(expected_payload)
                ):
                    raise ConversationKernelConflict(
                        "rejected Plan batch result content conflicts"
                    )
                self._exact_event_for_confirmation(
                    connection,
                    self._plan_event(
                        CommittedEventType.TOOL_RESULT_ACCEPTED,
                        SubjectSlot.ENTRY,
                        item.result_entry_id,
                        candidate=candidate,
                        actor_kind="runtime",
                        payload={
                            "tool_call_id": item.tool_call_id,
                            "result_state": expected_state,
                        },
                    ),
                    session_id=candidate.session_id,
                    workspace_id=candidate.workspace_id,
                )
            turn = connection.execute(
                "SELECT status FROM pulsara_v3.turns WHERE session_id = %s AND id = %s",
                (candidate.session_id, candidate.origin_turn_id),
            ).fetchone()
            if turn is None or str(turn["status"]) != "RUNNING":
                raise ConversationKernelConflict(
                    "rejected Plan batch origin status conflicts"
                )
            return AcceptedPlanToolBatch(
                workflow_id=candidate.workflow_id,
                workflow_revision=int(candidate.expected_workflow_revision or 0),
                interaction_id=None,
                interaction_kind=None,
                question=None,
                draft=None,
                selected_result_entry_id=selected_result_entry_id,
                continuation_turn_id=None,
                continuation_entry_id=None,
                origin_turn_completed=False,
            )
        if (
            candidate.idempotent_existing
            and key_row is not None
            and not result_rows
            and continuation is None
        ):
            return None
        if key_row is None and not result_rows and continuation is None:
            return None
        if key_row is None or len(result_rows) != len(result_ids):
            raise ConversationKernelConflict("Plan batch winner is partially installed")
        result_by_id = {str(row["id"]): row for row in result_rows}
        selected_result_entry_id: str | None = None
        final_entry_id: str | None = None
        for ordinal, item in enumerate(candidate.calls):
            if item.result_id is None:
                continue
            row = result_by_id.get(item.result_id)
            expected_selected = ordinal == candidate.selected_call_ordinal
            expected_state = (
                "SUCCESS" if expected_selected else "CANCELLED_BEFORE_DISPATCH"
            )
            expected_workflow_subject = (
                candidate.workflow_id
                if expected_selected
                and candidate.control_kind is PlanToolControlKind.ENTER
                else None
            )
            expected_interaction_subject = (
                candidate.interaction_id
                if expected_selected
                and candidate.control_kind is PlanToolControlKind.DRAFT
                else None
            )
            if (
                row is None
                or str(row["workspace_id"]) != candidate.workspace_id
                or str(row["tool_call_entry_id"]) != candidate.assistant_entry_id
                or str(row["tool_call_id"]) != item.tool_call_id
                or str(row["result_entry_id"]) != item.result_entry_id
                or str(row["result_origin_kind"])
                != ("PLAN_CONTROL" if expected_selected else "POLICY_NO_ATTEMPT")
                or row["attempt_id"] is not None
                or row["control_plan_workflow_id"] != expected_workflow_subject
                or row["control_plan_interaction_id"] != expected_interaction_subject
                or str(row["permission_snapshot_fingerprint"])
                != candidate.permission_snapshot.snapshot_fingerprint
                or str(row["result_state"]) != expected_state
                or row["observed_at"] != candidate.occurred_at
                or row["observation_duration_microseconds"] is not None
                or str(row["observation_origin_kind"])
                != ("PLAN_CONTROL" if expected_selected else "POLICY")
                or row["tool_reported_duration_microseconds"] is not None
            ):
                raise ConversationKernelConflict(
                    "Plan batch result identity names another winner"
                )
            if expected_selected:
                selected_result_entry_id = item.result_entry_id
                expected_payload: Mapping[str, object]
                if candidate.control_kind is PlanToolControlKind.ENTER:
                    expected_payload = {
                        "status": "success",
                        "plan_control": (
                            "PLAN_ALREADY_ACTIVE"
                            if candidate.idempotent_existing
                            else "ENTERED_PLAN"
                        ),
                        "workflow_id": candidate.workflow_id,
                    }
                else:
                    expected_payload = {
                        "status": "success",
                        "plan_control": "DRAFT_SUBMITTED_FOR_REVIEW",
                        "workflow_id": candidate.workflow_id,
                        "interaction_id": candidate.interaction_id,
                    }
            else:
                expected_payload = {
                    "status": "cancelled_before_dispatch",
                    "reason": "plan_workflow_batch_barrier",
                }
            assert item.result_entry_id is not None
            entry = connection.execute(
                """SELECT * FROM pulsara_v3.transcript_entries
                   WHERE session_id = %s AND id = %s""",
                (candidate.session_id, item.result_entry_id),
            ).fetchone()
            if (
                entry is None
                or str(entry["workspace_id"]) != candidate.workspace_id
                or str(entry["turn_id"]) != candidate.origin_turn_id
                or str(entry["entry_kind"]) != EntryKind.TOOL_RESULT.value
                or str(entry["conversation_scope_kind"]) != "ROOT"
                or self._content_from_row(entry) != _plan_inline(expected_payload)
            ):
                raise ConversationKernelConflict(
                    "Plan batch result content names another winner"
                )
            self._exact_event_for_confirmation(
                connection,
                self._plan_event(
                    CommittedEventType.TOOL_RESULT_ACCEPTED,
                    SubjectSlot.ENTRY,
                    item.result_entry_id,
                    candidate=candidate,
                    actor_kind="runtime",
                    payload={
                        "tool_call_id": item.tool_call_id,
                        "result_state": expected_state,
                    },
                ),
                session_id=candidate.session_id,
                workspace_id=candidate.workspace_id,
            )
            final_entry_id = item.result_entry_id
        question: PlanQuestionContent | None = None
        draft: ExtractedPlanDraft | None = None
        interaction_kind: PlanInteractionKind | None = None
        if candidate.control_kind is PlanToolControlKind.ENTER:
            if candidate.idempotent_existing:
                if (
                    str(key_row["workspace_id"]) != candidate.workspace_id
                    or str(key_row["status"]) != "ACTIVE"
                    or int(key_row["workflow_revision"])
                    != candidate.expected_workflow_revision
                    or continuation is not None
                ):
                    raise ConversationKernelConflict(
                        "idempotent Plan workflow winner conflicts"
                    )
                workflow_revision = int(key_row["workflow_revision"])
            else:
                expected_reason = extract_plan_entry_reason(
                    binding=candidate.request_binding,
                    arguments=candidate.selected_arguments,
                )
                if (
                    str(key_row["workspace_id"]) != candidate.workspace_id
                    or str(key_row["status"]) != "ACTIVE"
                    or str(key_row["entered_by"]) != "AGENT"
                    or str(key_row["entry_reason"]) != expected_reason
                    or str(key_row["entry_turn_id"]) != candidate.origin_turn_id
                    or str(key_row["entry_assistant_entry_id"])
                    != candidate.assistant_entry_id
                    or str(key_row["entry_tool_call_id"])
                    != candidate.calls[candidate.selected_call_ordinal].tool_call_id
                    or str(key_row["resume_permission_mode"])
                    != candidate.permission_snapshot.requested_mode.value
                    or str(key_row["permission_contract_id"])
                    != candidate.permission_snapshot.permission_contract_id
                    or str(key_row["permission_contract_fingerprint"])
                    != candidate.permission_snapshot.permission_contract_fingerprint
                    or int(key_row["workflow_revision"]) != 1
                    or key_row["accepted_plan_interaction_id"] is not None
                ):
                    raise ConversationKernelConflict("Plan workflow winner conflicts")
                workflow_revision = 1
                if continuation is None:
                    raise ConversationKernelConflict(
                        "Plan enter winner lacks its continuation"
                    )
        else:
            request_digest = canonical_digest(
                "pulsara:plan-interaction-request:v1",
                {
                    "binding": {
                        "id": candidate.request_binding.contract_id,
                        "version": candidate.request_binding.contract_version,
                        "fingerprint": (candidate.request_binding.contract_fingerprint),
                    },
                    "arguments": thaw_json(candidate.selected_arguments),
                },
            )
            expected_interaction_kind = (
                PlanInteractionKind.QUESTION
                if candidate.control_kind is PlanToolControlKind.QUESTION
                else PlanInteractionKind.DRAFT_REVIEW
            )
            expected_control_result_id = (
                None
                if expected_interaction_kind is PlanInteractionKind.QUESTION
                else candidate.calls[candidate.selected_call_ordinal].result_id
            )
            if (
                str(key_row["workspace_id"]) != candidate.workspace_id
                or str(key_row["plan_workflow_id"]) != candidate.workflow_id
                or str(key_row["origin_turn_id"]) != candidate.origin_turn_id
                or str(key_row["assistant_entry_id"]) != candidate.assistant_entry_id
                or str(key_row["tool_call_id"])
                != candidate.calls[candidate.selected_call_ordinal].tool_call_id
                or str(key_row["kind"]) != expected_interaction_kind.value
                or str(key_row["status"]) != "OPEN"
                or str(key_row["request_contract_id"])
                != candidate.request_binding.contract_id
                or str(key_row["request_contract_version"])
                != candidate.request_binding.contract_version
                or str(key_row["request_contract_fingerprint"])
                != candidate.request_binding.contract_fingerprint
                or str(key_row["request_semantic_digest"]) != request_digest
                or key_row["control_tool_result_id"] != expected_control_result_id
                or key_row["resolution_command_id"] is not None
                or key_row["response_semantic_digest"] is not None
                or key_row["decision_continuation_entry_id"] is not None
                or key_row["answer_kind"] is not None
                or key_row["selected_option_ordinal"] is not None
                or bool(key_row["feedback_present"])
                or key_row["resolved_at"] is not None
                or key_row["aborted_at"] is not None
            ):
                raise ConversationKernelConflict("Plan interaction winner conflicts")
            workflow_revision = int(candidate.expected_workflow_revision or 0) + 1
            workflow = connection.execute(
                """SELECT * FROM pulsara_v3.plan_workflows
                   WHERE session_id = %s AND id = %s""",
                (candidate.session_id, candidate.workflow_id),
            ).fetchone()
            if (
                workflow is None
                or str(workflow["workspace_id"]) != candidate.workspace_id
                or str(workflow["status"]) != "ACTIVE"
                or int(workflow["workflow_revision"]) != workflow_revision
            ):
                raise ConversationKernelConflict(
                    "Plan interaction workflow state conflicts"
                )
            interaction_kind = expected_interaction_kind
            selected_arguments = candidate.selected_arguments
            if interaction_kind is PlanInteractionKind.QUESTION:
                question = extract_plan_question(
                    interaction_id=str(key_row["id"]),
                    binding=candidate.request_binding,
                    arguments=selected_arguments,
                )
            else:
                draft = extract_plan_draft(
                    interaction_id=str(key_row["id"]),
                    assistant_entry_id=candidate.assistant_entry_id,
                    tool_call_id=str(key_row["tool_call_id"]),
                    binding=candidate.request_binding,
                    request_semantic_digest=str(key_row["request_semantic_digest"]),
                    arguments=selected_arguments,
                )
        turn = connection.execute(
            "SELECT * FROM pulsara_v3.turns WHERE session_id = %s AND id = %s",
            (candidate.session_id, candidate.origin_turn_id),
        ).fetchone()
        origin_completed = candidate.control_kind is PlanToolControlKind.DRAFT or (
            candidate.control_kind is PlanToolControlKind.ENTER
            and not candidate.idempotent_existing
        )
        if (
            turn is None
            or str(turn["workspace_id"]) != candidate.workspace_id
            or str(turn["conversation_scope_kind"]) != "ROOT"
            or str(turn["permission_snapshot_fingerprint"])
            != candidate.permission_snapshot.snapshot_fingerprint
            or self._permission_from_row(turn) != candidate.permission_snapshot
            or (str(turn["status"]) == "COMPLETED") != origin_completed
            or (
                origin_completed
                and (
                    turn["final_entry_id"] != final_entry_id
                    or str(turn["terminal_reason"]) != "COMPLETED"
                    or turn["terminal_at"] is None
                )
            )
        ):
            raise ConversationKernelConflict("Plan batch origin status conflicts")
        if not candidate.idempotent_existing:
            self._exact_event_for_confirmation(
                connection,
                self._plan_open_event(
                    candidate=candidate,
                    workflow_revision=workflow_revision,
                ),
                session_id=candidate.session_id,
                workspace_id=candidate.workspace_id,
            )
        if origin_completed:
            assert final_entry_id is not None
            self._exact_event_for_confirmation(
                connection,
                self._plan_event(
                    CommittedEventType.TURN_COMPLETED,
                    SubjectSlot.TURN,
                    candidate.origin_turn_id,
                    candidate=candidate,
                    actor_kind="runtime",
                    payload={"final_entry_id": final_entry_id},
                ),
                session_id=candidate.session_id,
                workspace_id=candidate.workspace_id,
            )
        if (
            candidate.control_kind is PlanToolControlKind.ENTER
            and not candidate.idempotent_existing
        ):
            assert candidate.continuation_turn_id is not None
            assert candidate.continuation_entry_id is not None
            assert candidate.continuation_context_binding_revision_id is not None
            continuation_turn = connection.execute(
                """SELECT * FROM pulsara_v3.turns
                   WHERE session_id = %s AND id = %s""",
                (candidate.session_id, candidate.continuation_turn_id),
            ).fetchone()
            continuation_revision = connection.execute(
                """SELECT * FROM pulsara_v3.turn_context_binding_revisions
                   WHERE session_id = %s AND id = %s""",
                (
                    candidate.session_id,
                    candidate.continuation_context_binding_revision_id,
                ),
            ).fetchone()
            expected_permission = build_run_permission_snapshot(
                snapshot_id=_stable_identity(
                    "permission-snapshot", candidate.continuation_turn_id
                ),
                requested_mode=candidate.permission_snapshot.requested_mode,
                effective_mode=PermissionMode.READ_ONLY,
                admission_source=(
                    RunPermissionAdmissionSource.RUNTIME_PLAN_CONTINUATION
                ),
                overlay=RunPermissionOverlay.PLAN_READ_ONLY,
                plan_context_ordinal_at_admission=int(key_row["workflow_ordinal"]),
                plan_workflow_id=candidate.workflow_id,
                plan_workflow_revision_at_admission=workflow_revision,
                inherited_from_turn_id=candidate.origin_turn_id,
            )
            if (
                continuation is None
                or continuation_turn is None
                or continuation_revision is None
                or str(continuation["workspace_id"]) != candidate.workspace_id
                or str(continuation["turn_id"]) != candidate.continuation_turn_id
                or str(continuation["entry_kind"]) != EntryKind.PLAN_CONTINUATION.value
                or str(continuation["conversation_scope_kind"]) != "ROOT"
                or str(continuation["source_plan_workflow_id"]) != candidate.workflow_id
                or continuation["source_plan_interaction_id"] is not None
                or str(continuation["source_plan_handoff_kind"])
                != PlanHandoffKind.ENTERED_PLAN.value
                or self._content_from_row(continuation)
                != _plan_inline(
                    {
                        "transition": "ENTERED_PLAN",
                        "workflow_id": candidate.workflow_id,
                    }
                )
                or str(continuation_turn["status"]) != "RUNNING"
                or str(continuation_turn["initial_entry_id"])
                != candidate.continuation_entry_id
                or str(continuation_turn["current_context_binding_revision_id"])
                != candidate.continuation_context_binding_revision_id
                or self._permission_from_row(continuation_turn) != expected_permission
                or str(continuation_revision["turn_id"])
                != candidate.continuation_turn_id
                or int(continuation_revision["revision_ordinal"]) != 0
                or str(continuation_revision["base_kind"]) != "FULL_HISTORY"
                or int(continuation_revision["source_through_sequence"])
                != int(continuation["entry_sequence"]) - 1
            ):
                raise ConversationKernelConflict(
                    "Plan continuation winner identity conflicts"
                )
            self._exact_event_for_confirmation(
                connection,
                self._plan_event(
                    CommittedEventType.PLAN_CONTINUATION_ACCEPTED,
                    SubjectSlot.ENTRY,
                    candidate.continuation_entry_id,
                    candidate=candidate,
                    actor_kind="runtime",
                    payload={
                        "handoff_kind": PlanHandoffKind.ENTERED_PLAN.value,
                        "workflow_id": candidate.workflow_id,
                    },
                ),
                session_id=candidate.session_id,
                workspace_id=candidate.workspace_id,
            )
        elif continuation is not None:
            raise ConversationKernelConflict(
                "Plan batch installed an unexpected continuation"
            )
        return AcceptedPlanToolBatch(
            workflow_id=candidate.workflow_id,
            workflow_revision=workflow_revision,
            interaction_id=candidate.interaction_id,
            interaction_kind=interaction_kind,
            question=question,
            draft=draft,
            selected_result_entry_id=selected_result_entry_id,
            continuation_turn_id=candidate.continuation_turn_id,
            continuation_entry_id=candidate.continuation_entry_id,
            origin_turn_completed=origin_completed,
        )

    @staticmethod
    def _insert_resolution_plan_continuation(
        connection: Connection,
        *,
        session_id: str,
        workspace_id: str,
        workflow_id: str,
        interaction_id: str,
        origin_turn_id: str,
        turn_id: str,
        entry_id: str,
        context_binding_revision_id: str,
        entry_sequence: int,
        permission: FrozenRunPermissionSnapshot,
        handoff_kind: PlanHandoffKind,
        body: InlineContent,
    ) -> None:
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
                permission_contract_fingerprint, permission_snapshot_fingerprint
            ) VALUES (%s, %s, %s, 'ROOT', 'RUNNING', %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                turn_id,
                session_id,
                workspace_id,
                entry_id,
                context_binding_revision_id,
                *_RepositoryKernel._permission_columns(permission),
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
                session_id,
                turn_id,
                entry_sequence - 1,
            ),
        )
        _RepositoryKernel._insert_entry(
            connection,
            session_id=session_id,
            workspace_id=workspace_id,
            turn_id=turn_id,
            entry_id=entry_id,
            entry_sequence=entry_sequence,
            entry_kind=EntryKind.PLAN_CONTINUATION,
            scope_kind=ConversationScopeKind.ROOT,
            scope_task_id=None,
            content=body,
            source_plan_workflow_id=workflow_id,
            source_plan_interaction_id=interaction_id,
            source_plan_handoff_kind=handoff_kind,
        )
        if permission.inherited_from_turn_id != origin_turn_id:
            raise ConversationKernelConflict(
                "Plan continuation inheritance does not exact-join"
            )

    @staticmethod
    def _confirm_plan_workflow_command_in_transaction(
        connection: Connection,
        *,
        session_id: str,
        command_id: str,
        command_kind: str,
        semantic_digest: str,
        expected_workflow_id: str,
    ) -> AcceptedPlanWorkflowCommand | None:
        if command_kind not in {"ENTER_PLAN", "CANCEL_PLAN", "FORCE_EXIT_PLAN"}:
            raise ValueError("Plan workflow command kind is invalid")
        row = connection.execute(
            """
            SELECT c.command_kind, c.semantic_digest,
                   c.target_plan_workflow_id,
                   w.status, w.workflow_revision,
                   w.resume_permission_mode, w.entry_command_id,
                   w.entered_by,
                   EXISTS (
                       SELECT 1 FROM pulsara_v3.agent_events AS e
                       WHERE e.session_id = c.session_id
                         AND e.subject_plan_workflow_id = w.id
                         AND e.event_type = CASE c.command_kind
                             WHEN 'ENTER_PLAN' THEN 'PlanWorkflowEntered'
                             ELSE 'PlanWorkflowExited'
                         END
                   ) AS event_present
            FROM pulsara_v3.session_commands AS c
            JOIN pulsara_v3.plan_workflows AS w
              ON w.session_id = c.session_id
             AND w.id = c.target_plan_workflow_id
            WHERE c.session_id = %s AND c.command_id = %s
            """,
            (session_id, command_id),
        ).fetchone()
        if row is None:
            return None
        if (
            str(row["command_kind"]) != command_kind
            or str(row["semantic_digest"]) != semantic_digest
            or str(row["target_plan_workflow_id"]) != expected_workflow_id
        ):
            raise ConversationKernelConflict("Plan workflow command conflicts")
        expected_status = {
            "ENTER_PLAN": PlanWorkflowStatus.ACTIVE,
            "CANCEL_PLAN": PlanWorkflowStatus.CANCELLED,
            "FORCE_EXIT_PLAN": PlanWorkflowStatus.FORCE_EXITED,
        }[command_kind]
        status = PlanWorkflowStatus(str(row["status"]))
        if status is not expected_status or not bool(row["event_present"]):
            raise ConversationKernelConflict(
                "Plan workflow command winner is partially installed"
            )
        if command_kind == "ENTER_PLAN" and (
            str(row["entered_by"]) != "USER"
            or str(row["entry_command_id"]) != command_id
        ):
            raise ConversationKernelConflict("Plan enter winner identity conflicts")
        return AcceptedPlanWorkflowCommand(
            command_id=command_id,
            workflow_id=expected_workflow_id,
            workflow_status=status,
            resume_permission_mode=PermissionMode(str(row["resume_permission_mode"])),
            handoff_created_at_commit=command_kind != "ENTER_PLAN",
            workflow_revision=int(row["workflow_revision"]),
        )

    def _confirm_plan_resolution_in_transaction(
        self,
        connection: Connection,
        *,
        session_id: str,
        command_id: str,
        semantic_digest: str,
        expected_question_answer: PlanQuestionAnswer | None = None,
        expected_result_id: str | None = None,
        expected_result_entry_id: str | None = None,
    ) -> AcceptedPlanResolution | None:
        row = connection.execute(
            """
            SELECT c.semantic_digest, c.target_plan_interaction_id,
                   i.status AS interaction_status, i.kind AS interaction_kind,
                   i.plan_workflow_id, i.decision_continuation_entry_id,
                   i.control_tool_result_id,
                   i.origin_turn_id, i.workspace_id,
                   i.assistant_entry_id, i.tool_call_id,
                   i.request_contract_id, i.request_contract_version,
                   i.request_contract_fingerprint,
                   i.answer_kind, i.selected_option_ordinal,
                   w.status AS workflow_status, w.resume_permission_mode,
                   w.workflow_revision,
                   e.turn_id AS continuation_turn_id,
                   b.tool_arguments,
                   t.permission_snapshot_fingerprint
            FROM pulsara_v3.session_commands AS c
            JOIN pulsara_v3.plan_interactions AS i
              ON i.session_id = c.session_id
             AND i.id = c.target_plan_interaction_id
            JOIN pulsara_v3.plan_workflows AS w
              ON w.session_id = i.session_id AND w.id = i.plan_workflow_id
            JOIN pulsara_v3.assistant_message_blocks AS b
              ON b.session_id = i.session_id
             AND b.assistant_entry_id = i.assistant_entry_id
             AND b.tool_call_id = i.tool_call_id
            JOIN pulsara_v3.turns AS t
              ON t.session_id = i.session_id AND t.id = i.origin_turn_id
            LEFT JOIN pulsara_v3.transcript_entries AS e
              ON e.session_id = i.session_id
             AND e.id = i.decision_continuation_entry_id
            WHERE c.session_id = %s AND c.command_id = %s
              AND c.command_kind = 'RESOLVE_PLAN_INTERACTION'
            """,
            (session_id, command_id),
        ).fetchone()
        if row is None:
            return None
        if str(row["semantic_digest"]) != semantic_digest:
            raise ConversationKernelConflict("Plan resolution command conflicts")
        interaction_status = str(row["interaction_status"])
        question_result = (
            str(row["control_tool_result_id"])
            if interaction_status == "ANSWERED"
            else None
        )
        if question_result is not None:
            if (
                expected_question_answer is None
                or expected_result_id is None
                or expected_result_entry_id is None
                or question_result != expected_result_id
            ):
                raise ConversationKernelConflict(
                    "Plan question result identity conflicts"
                )
            result = connection.execute(
                """
                SELECT * FROM pulsara_v3.tool_results
                WHERE session_id = %s AND id = %s
                """,
                (session_id, question_result),
            ).fetchone()
            result_entry = connection.execute(
                """SELECT * FROM pulsara_v3.transcript_entries
                   WHERE session_id = %s AND id = %s""",
                (session_id, expected_result_entry_id),
            ).fetchone()
            frozen_arguments = freeze_json(dict(row["tool_arguments"]))
            if not isinstance(frozen_arguments, FrozenJsonObjectFact):
                raise ConversationKernelConflict(
                    "Plan question arguments are invalid"
                )
            question = extract_plan_question(
                interaction_id=str(row["target_plan_interaction_id"]),
                binding=PlanInteractionBinding(
                    str(row["request_contract_id"]),
                    str(row["request_contract_version"]),
                    str(row["request_contract_fingerprint"]),
                ),
                arguments=frozen_arguments,
            )
            response = _plan_question_response(
                content=question, answer=expected_question_answer
            )
            expected_content = _plan_inline(
                {
                    "status": "success",
                    "plan_control": "QUESTION_ANSWERED",
                    "interaction_id": str(row["target_plan_interaction_id"]),
                    **response,
                }
            )
            if (
                result is None
                or result_entry is None
                or str(result["result_entry_id"]) != expected_result_entry_id
                or result["attempt_id"] is not None
                or str(result["result_origin_kind"]) != "PLAN_CONTROL"
                or str(result["control_plan_interaction_id"])
                != str(row["target_plan_interaction_id"])
                or str(result["tool_call_entry_id"])
                != str(row["assistant_entry_id"])
                or str(result["tool_call_id"]) != str(row["tool_call_id"])
                or str(result["permission_snapshot_fingerprint"])
                != str(row["permission_snapshot_fingerprint"])
                or str(result["result_state"]) != "SUCCESS"
                or result["observation_duration_microseconds"] is not None
                or str(result["observation_origin_kind"]) != "PLAN_CONTROL"
                or result["tool_reported_duration_microseconds"] is not None
                or str(result_entry["workspace_id"]) != str(row["workspace_id"])
                or str(result_entry["turn_id"]) != str(row["origin_turn_id"])
                or str(result_entry["entry_kind"]) != EntryKind.TOOL_RESULT.value
                or self._content_from_row(result_entry) != expected_content
            ):
                raise ConversationKernelConflict(
                    "Plan question winner is partially installed"
                )
            question_event = connection.execute(
                """SELECT * FROM pulsara_v3.agent_events
                   WHERE event_id = %s""",
                (
                    _stable_identity(
                        "event", command_id, "PlanQuestionAnswered"
                    ),
                ),
            ).fetchone()
            result_event = connection.execute(
                """SELECT * FROM pulsara_v3.agent_events
                   WHERE event_id = %s""",
                (
                    _stable_identity(
                        "event", expected_result_entry_id, "ToolResultAccepted"
                    ),
                ),
            ).fetchone()
            expected_result_payload = {
                "tool_call_id": str(row["tool_call_id"]),
                "result_state": "SUCCESS",
            }
            if (
                question_event is None
                or result_event is None
                or str(question_event["event_type"]) != "PlanQuestionAnswered"
                or question_event["subject_plan_interaction_id"]
                != str(row["target_plan_interaction_id"])
                or str(result_event["event_type"]) != "ToolResultAccepted"
                or result_event["subject_entry_id"] != expected_result_entry_id
                or result_event["occurred_at"] != result["observed_at"]
                or question_event["occurred_at"] != result["observed_at"]
                or str(result_event["actor_kind"]) != "runtime"
                or str(result_event["actor_id"])
                != str(question_event["actor_id"])
                or str(result_event["sensitivity_class"]) != "S1"
                or str(result_event["projection_profile"]) != "DEFAULT"
                or dict(result_event["payload"]) != expected_result_payload
            ):
                raise ConversationKernelConflict(
                    "Plan question occurrence is partially installed"
                )
            question_result_entry_id = expected_result_entry_id
        else:
            if any(
                value is not None
                for value in (
                    expected_question_answer,
                    expected_result_id,
                    expected_result_entry_id,
                )
            ):
                raise ConversationKernelConflict(
                    "Plan question winner is absent"
                )
            question_result_entry_id = None
        return AcceptedPlanResolution(
            command_id=command_id,
            workflow_id=str(row["plan_workflow_id"]),
            workflow_status=PlanWorkflowStatus(str(row["workflow_status"])),
            interaction_id=str(row["target_plan_interaction_id"]),
            interaction_status=interaction_status,
            resume_permission_mode=PermissionMode(str(row["resume_permission_mode"])),
            continuation_turn_id=(
                None
                if row["continuation_turn_id"] is None
                else str(row["continuation_turn_id"])
            ),
            continuation_entry_id=(
                None
                if row["decision_continuation_entry_id"] is None
                else str(row["decision_continuation_entry_id"])
            ),
            handoff_created_at_commit=interaction_status == "CANCELLED",
            question_result_entry_id=question_result_entry_id,
            workflow_revision=int(row["workflow_revision"]),
            draft_decision=(
                {
                    "APPROVED": PlanDraftDecision.APPROVE,
                    "REVISION_REQUESTED": PlanDraftDecision.REVISE,
                    "CANCELLED": PlanDraftDecision.CANCEL,
                }.get(interaction_status)
                if str(row["interaction_kind"])
                == PlanInteractionKind.DRAFT_REVIEW.value
                else None
            ),
        )

    @staticmethod
    def _eligible_plan_handoff(
        connection: Connection, *, session_id: str
    ) -> _EligiblePlanHandoff | None:
        workflow = connection.execute(
            """
            SELECT id, status
            FROM pulsara_v3.plan_workflows
            WHERE session_id = %s
            ORDER BY workflow_ordinal DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if workflow is None or str(workflow["status"]) not in {
            PlanWorkflowStatus.CANCELLED.value,
            PlanWorkflowStatus.FORCE_EXITED.value,
        }:
            return None
        workflow_id = str(workflow["id"])
        already_claimed = connection.execute(
            """
            SELECT 1 FROM pulsara_v3.transcript_entries
            WHERE session_id = %s AND source_plan_workflow_id = %s
              AND source_plan_handoff_kind IN (
                  'CANCELLED_PLAN', 'FORCE_EXITED_PLAN'
              )
            UNION ALL
            SELECT 1 FROM pulsara_v3.prompt_queue_items
            WHERE session_id = %s
              AND pending_plan_handoff_workflow_id = %s
            LIMIT 1
            """,
            (session_id, workflow_id, session_id, workflow_id),
        ).fetchone()
        if already_claimed is not None:
            return None
        interaction = connection.execute(
            """
            SELECT id FROM pulsara_v3.plan_interactions
            WHERE session_id = %s AND plan_workflow_id = %s
              AND status IN ('CANCELLED', 'ABORTED')
            ORDER BY interaction_ordinal DESC LIMIT 1
            """,
            (session_id, workflow_id),
        ).fetchone()
        kind = (
            PlanHandoffKind.CANCELLED_PLAN
            if str(workflow["status"]) == PlanWorkflowStatus.CANCELLED.value
            else PlanHandoffKind.FORCE_EXITED_PLAN
        )
        return _EligiblePlanHandoff(
            workflow_id=workflow_id,
            interaction_id=(None if interaction is None else str(interaction["id"])),
            kind=kind,
        )

    @staticmethod
    def classify_plan_continuation(
        inspection: PlanContinuationInspection,
        guard: HostWriterGuard,
    ) -> PlanContinuationDisposition:
        """Closed settlement for an exact canonical continuation winner."""

        if inspection.status in {"COMPLETED", "INTERRUPTED"}:
            return PlanContinuationDisposition.HISTORICAL_TERMINAL
        if inspection.status != "RUNNING":
            raise ConversationKernelConflict(
                "Plan continuation has an invalid canonical state"
            )
        if (
            inspection.session_lifecycle == "OPEN"
            and inspection.writer_generation == guard.writer_generation
            and inspection.writer_owner_id == guard.writer_owner_id
        ):
            return PlanContinuationDisposition.RUNNING_CURRENT_WRITER
        return PlanContinuationDisposition.NOT_OWNED_BY_CURRENT_WRITER
