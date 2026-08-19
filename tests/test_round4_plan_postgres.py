from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from time import monotonic
from uuid import uuid4

import psycopg
from psycopg.errors import CheckViolation
from psycopg.rows import dict_row
import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.repository import (
    AssistantToolCallBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    PlanQuestionAnswer,
    PlanToolControlKind,
    PreparedPlanBatchCall,
    PreparedPlanToolBatch,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.conversation_kernel.steer import (
    QueuedRootTurnAdmissionConfirmationKind,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    live_digest,
)
from pulsara_agent.model_input.contracts import FrozenProviderInputItemKind
from pulsara_agent.model_input.contracts import ProviderToolResultClosureKind
from pulsara_agent.primitives.context import FrozenJsonObjectFact, freeze_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import (
    PlanApprovedMaterializationDisposition,
    PlanDraftDecision,
    PlanHandoffKind,
    PlanInteractionBinding,
    PlanQuestionAnswerKind,
    PlanWorkflowStatus,
)
from pulsara_agent.primitives.run_permission import RunPermissionOverlay
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from tests.support.postgres import verified_postgres_provider
from tests.support.round3 import ScriptedKernelModel, StaticContextSourceCollector


pytestmark = pytest.mark.postgres


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _repository(database) -> ConversationKernelRepository:
    return ConversationKernelRepository(verified_postgres_provider(database.runtime_dsn))


def _lease(repository: ConversationKernelRepository):
    return repository.acquire_host_writer(
        session_id=_id("session"),
        workspace_id=_id("workspace"),
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )


def _start_root(
    repository: ConversationKernelRepository,
    lease,
    *,
    mode: PermissionMode = PermissionMode.ACCEPT_EDITS,
    text: bytes = b"plan this change",
):
    turn_id = _id("turn")
    accepted = repository.start_root_turn(
        lease.guard,
        command_id=_id("command"),
        turn_id=turn_id,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("context-revision"),
        permission_snapshot_id=_id("permission-snapshot"),
        requested_permission_mode=mode,
        content=InlineContent.from_bytes(text),
        occurred_at=_now(),
        deadline_monotonic=monotonic() + 30,
    )
    return turn_id, accepted


def _permission(repository: ConversationKernelRepository, lease, turn_id: str):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            "SELECT * FROM pulsara_v3.turns WHERE session_id = %s AND id = %s",
            (lease.guard.session_id, turn_id),
        ).fetchone()
    assert row is not None
    return repository._permission_from_row(row)


def _admin_corrupt(database, statement: str, parameters: tuple[object, ...]) -> None:
    """Inject an otherwise unreachable post-COMMIT corruption for ACK probes."""

    with psycopg.connect(database.admin_dsn) as connection:
        connection.execute("SET session_replication_role = replica")
        connection.execute(statement, parameters)


@dataclass(frozen=True, slots=True)
class _AcceptedAssistantBatch:
    candidate: PreparedPlanToolBatch
    assistant_entry_id: str
    selected_tool_call_id: str


def _commit_plan_batch(
    repository: ConversationKernelRepository,
    lease,
    *,
    workspace_id: str,
    turn_id: str,
    selected_tool_name: str,
    selected_arguments: dict[str, object],
    workflow_id: str,
    expected_workflow_revision: int | None,
    sibling_before: bool = False,
) -> _AcceptedAssistantBatch:
    selected_call_id = _id("tool-call")
    selected_block_id = _id("block")
    blocks: list[AssistantToolCallBlock] = []
    sibling_call_id = _id("tool-call")
    sibling_block_id = _id("block")
    sibling = AssistantToolCallBlock(
        sibling_block_id,
        sibling_call_id,
        "write_file",
        freeze_json({"path": "never.txt", "content": "must not run"}),
    )
    selected = AssistantToolCallBlock(
        selected_block_id,
        selected_call_id,
        selected_tool_name,
        freeze_json(selected_arguments),
    )
    if sibling_before:
        blocks.extend((sibling, selected))
        selected_ordinal = 1
    else:
        blocks.extend((selected, sibling))
        selected_ordinal = 0
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    assistant_entry_id = _id("entry")
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b""),
        blocks=tuple(blocks),
        occurred_at=_now(),
        actor_id="model:round4",
        deadline_monotonic=monotonic() + 30,
    )
    control_kind = {
        "enter_plan": PlanToolControlKind.ENTER,
        "ask_plan_question": PlanToolControlKind.QUESTION,
        "exit_plan": PlanToolControlKind.DRAFT,
    }[selected_tool_name]
    frozen_arguments = freeze_json(selected_arguments)
    assert isinstance(frozen_arguments, FrozenJsonObjectFact)
    calls: list[PreparedPlanBatchCall] = []
    for block in blocks:
        is_selected_question = (
            block.tool_call_id == selected_call_id
            and control_kind is PlanToolControlKind.QUESTION
        )
        calls.append(
            PreparedPlanBatchCall(
                block_id=block.block_id,
                tool_call_id=block.tool_call_id,
                tool_name=block.tool_name,
                result_id=(
                    None if is_selected_question else _id("tool-result")
                ),
                result_entry_id=(
                    None if is_selected_question else _id("tool-result-entry")
                ),
            )
        )
    binding = builtin_tool_catalog_entry(selected_tool_name).binding_contract.base
    fresh_enter = control_kind is PlanToolControlKind.ENTER
    interaction_id = (
        None if fresh_enter else _id("plan-interaction")
    )
    continuation_turn_id = _id("turn") if fresh_enter else None
    continuation_entry_id = _id("entry") if fresh_enter else None
    continuation_revision_id = _id("context-revision") if fresh_enter else None
    candidate = PreparedPlanToolBatch(
        session_id=lease.guard.session_id,
        workspace_id=workspace_id,
        origin_turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        selected_call_ordinal=selected_ordinal,
        control_kind=control_kind,
        selected_arguments=frozen_arguments,
        request_binding=PlanInteractionBinding(
            binding.contract_id,
            binding.contract_version,
            binding.binding_fingerprint,
        ),
        permission_snapshot=_permission(repository, lease, turn_id),
        workflow_id=workflow_id,
        expected_workflow_revision=expected_workflow_revision,
        interaction_id=interaction_id,
        continuation_turn_id=continuation_turn_id,
        continuation_entry_id=continuation_entry_id,
        continuation_context_binding_revision_id=continuation_revision_id,
        calls=tuple(calls),
        occurred_at=_now(),
        actor_id="plan-runtime:test",
    )
    return _AcceptedAssistantBatch(candidate, assistant_entry_id, selected_call_id)


def _open_user_plan(repository, lease, *, resume=PermissionMode.ACCEPT_EDITS):
    workflow_id = _id("plan-workflow")
    accepted = repository.enter_plan_by_user(
        lease.guard,
        command_id=_id("command"),
        workflow_id=workflow_id,
        entry_reason="plan before implementation",
        resume_permission_mode=resume,
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    return workflow_id, accepted


def _runner_plan_batch_stream() -> list[object]:
    plan_arguments = json.dumps(
        {"reason": "inspect before changing files"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    sibling_arguments = json.dumps(
        {"path": "must-not-exist.txt", "content": "must not dispatch"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return [
        ToolCallStartPayload("call:write", "call:write", "write_file"),
        ToolCallDeltaPayload("call:write", "call:write", sibling_arguments),
        ToolCallEndPayload(
            block_identity="call:write",
            tool_call_id="call:write",
            tool_name="write_file",
            arguments_json=sibling_arguments,
            utf8_bytes=len(sibling_arguments.encode("utf-8")),
            digest=live_digest(sibling_arguments),
        ),
        ToolCallStartPayload("call:plan", "call:plan", "enter_plan"),
        ToolCallDeltaPayload("call:plan", "call:plan", plan_arguments),
        ToolCallEndPayload(
            block_identity="call:plan",
            tool_call_id="call:plan",
            tool_name="enter_plan",
            arguments_json=plan_arguments,
            utf8_bytes=len(plan_arguments.encode("utf-8")),
            digest=live_digest(plan_arguments),
        ),
    ]


def _runner_rejected_plan_batch_stream(
    plan_arguments: dict[str, object],
) -> list[object]:
    encoded_plan_arguments = json.dumps(
        plan_arguments,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    sibling_arguments = json.dumps(
        {"path": "must-not-exist.txt", "content": "must not dispatch"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return [
        ToolCallStartPayload("call:write", "call:write", "write_file"),
        ToolCallDeltaPayload("call:write", "call:write", sibling_arguments),
        ToolCallEndPayload(
            block_identity="call:write",
            tool_call_id="call:write",
            tool_name="write_file",
            arguments_json=sibling_arguments,
            utf8_bytes=len(sibling_arguments.encode("utf-8")),
            digest=live_digest(sibling_arguments),
        ),
        ToolCallStartPayload("call:plan", "call:plan", "ask_plan_question"),
        ToolCallDeltaPayload("call:plan", "call:plan", encoded_plan_arguments),
        ToolCallEndPayload(
            block_identity="call:plan",
            tool_call_id="call:plan",
            tool_name="ask_plan_question",
            arguments_json=encoded_plan_arguments,
            utf8_bytes=len(encoded_plan_arguments.encode("utf-8")),
            digest=live_digest(encoded_plan_arguments),
        ),
    ]


def _runner_text_stream(text: str) -> list[object]:
    return [
        TextStartPayload("text:final"),
        TextDeltaPayload("text:final", text),
        TextEndPayload(
            "text:final",
            text,
            len(text.encode("utf-8")),
            live_digest(text),
        ),
    ]


def test_round4_user_plan_freezes_permission_and_cancel_handoff_once(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    workflow_id, entered = _open_user_plan(repository, lease)
    assert entered.workflow_status is PlanWorkflowStatus.ACTIVE
    turn_id, _ = _start_root(
        repository, lease, mode=PermissionMode.BYPASS_PERMISSIONS
    )
    permission = _permission(repository, lease, turn_id)
    assert permission.requested_mode is PermissionMode.BYPASS_PERMISSIONS
    assert permission.effective_mode is PermissionMode.READ_ONLY
    assert permission.overlay is RunPermissionOverlay.PLAN_READ_ONLY
    assert permission.plan_workflow_id == workflow_id
    repository.interrupt_turn(
        lease.guard,
        turn_id=turn_id,
        reason="TEST_BOUNDARY",
        actor_id="test",
        occurred_at=_now(),
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _id("command")
    cancelled = repository.exit_plan_by_user(
        lease.guard,
        command_id=command_id,
        command_kind="CANCEL_PLAN",
        workflow_id=workflow_id,
        expected_workflow_revision=1,
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert cancelled.handoff_created_at_commit
    protocol = CanonicalProtocolReader(repository.connection_provider)
    pending_snapshot = protocol.snapshot(
        session_id=lease.guard.session_id,
        maximum_entries=64,
        maximum_control_items=64,
        deadline_monotonic=monotonic() + 30,
    )
    assert pending_snapshot.control.latest_plan_handoff.disposition == (
        wire.PLAN_HANDOFF_PENDING
    )

    first_turn, first = _start_root(repository, lease, text=b"real prompt one")
    claimed_snapshot = protocol.snapshot(
        session_id=lease.guard.session_id,
        maximum_entries=64,
        maximum_control_items=64,
        deadline_monotonic=monotonic() + 30,
    )
    assert claimed_snapshot.control.latest_plan_handoff.disposition == (
        wire.PLAN_HANDOFF_CLAIMED
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        first_row = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE id = %s",
            (first.entry_id,),
        ).fetchone()
    assert first_row["source_plan_workflow_id"] == workflow_id
    assert first_row["source_plan_handoff_kind"] == "CANCELLED_PLAN"
    repository.interrupt_turn(
        lease.guard,
        turn_id=first_turn,
        reason="TEST_BOUNDARY",
        actor_id="test",
        occurred_at=_now(),
        deadline_monotonic=monotonic() + 30,
    )
    _, second = _start_root(repository, lease, text=b"real prompt two")
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        second_row = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE id = %s",
            (second.entry_id,),
        ).fetchone()
    assert second_row["source_plan_workflow_id"] is None

    # Writer generation is write-attempt authority, not stable command semantics.
    replacement = repository.acquire_host_writer(
        session_id=lease.guard.session_id,
        workspace_id=repository.read_session_workspace_id(
            lease.guard, deadline_monotonic=monotonic() + 30
        ),
        writer_owner_id=_id("replacement-host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    confirmed = repository.exit_plan_by_user(
        replacement.guard,
        command_id=command_id,
        command_kind="CANCEL_PLAN",
        workflow_id=workflow_id,
        expected_workflow_revision=1,
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert confirmed == cancelled


def test_round4_database_rejects_permission_mutation_and_wrong_initial_entry(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    first_turn, first = _start_root(repository, lease, text=b"first")
    repository.interrupt_turn(
        lease.guard,
        turn_id=first_turn,
        reason="TEST_BOUNDARY",
        actor_id="test",
        occurred_at=_now(),
        deadline_monotonic=monotonic() + 30,
    )
    second_turn, _ = _start_root(repository, lease, text=b"second")

    with pytest.raises(CheckViolation) as turn_violation:
        with repository.connection_provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            connection.execute(
                "UPDATE pulsara_v3.turns SET effective_permission_mode = %s "
                "WHERE session_id = %s AND id = %s",
                (PermissionMode.READ_ONLY.value, lease.guard.session_id, second_turn),
            )
    assert turn_violation.value.diag.constraint_name == (
        "ck_turn_permission_overlay_exact"
    )

    queue_item_id = _id("queue")
    repository.enqueue_prompt(
        lease.guard,
        command_id=_id("command"),
        queue_item_id=queue_item_id,
        client_submission_id=_id("submission"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=PermissionMode.READ_ONLY,
        content=InlineContent.from_bytes(b"queued read-only request"),
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    with pytest.raises(CheckViolation) as queue_violation:
        with repository.connection_provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            connection.execute(
                "UPDATE pulsara_v3.prompt_queue_items "
                "SET effective_permission_mode = %s "
                "WHERE session_id = %s AND id = %s",
                (
                    PermissionMode.BYPASS_PERMISSIONS.value,
                    lease.guard.session_id,
                    queue_item_id,
                ),
            )
    assert queue_violation.value.diag.constraint_name == (
        "ck_prompt_queue_permission_overlay_exact"
    )

    with pytest.raises(CheckViolation, match="exact turn and scope"):
        with repository.connection_provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            connection.execute(
                "UPDATE pulsara_v3.turns SET initial_entry_id = %s "
                "WHERE session_id = %s AND id = %s",
                (first.entry_id, lease.guard.session_id, second_turn),
            )


@pytest.mark.parametrize("sibling_before", [False, True])
def test_round4_agent_enter_batch_cancels_every_sibling_before_dispatch(
    stage2_migrated_postgres_database, sibling_before: bool
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    workspace_id = repository.read_session_workspace_id(
        lease.guard, deadline_monotonic=monotonic() + 30
    )
    origin_turn_id, _ = _start_root(repository, lease)
    workflow_id = _id("plan-workflow")
    prepared = _commit_plan_batch(
        repository,
        lease,
        workspace_id=workspace_id,
        turn_id=origin_turn_id,
        selected_tool_name="enter_plan",
        selected_arguments={"reason": "need a plan"},
        workflow_id=workflow_id,
        expected_workflow_revision=None,
        sibling_before=sibling_before,
    )
    accepted = repository.accept_plan_tool_batch(
        lease.guard,
        candidate=prepared.candidate,
        deadline_monotonic=monotonic() + 30,
    )
    confirmed = repository.confirm_plan_tool_batch_winner(
        candidate=prepared.candidate, deadline_monotonic=monotonic() + 30
    )
    assert confirmed == accepted
    assert accepted.origin_turn_completed
    assert accepted.continuation_turn_id is not None
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        attempts = connection.execute(
            "SELECT count(*) AS count FROM pulsara_v3.tool_execution_attempts WHERE session_id = %s",
            (lease.guard.session_id,),
        ).fetchone()
        results = connection.execute(
            "SELECT result_origin_kind, result_state FROM pulsara_v3.tool_results WHERE session_id = %s ORDER BY result_origin_kind",
            (lease.guard.session_id,),
        ).fetchall()
    assert attempts["count"] == 0
    assert {tuple(row.values()) for row in results} == {
        ("PLAN_CONTROL", "SUCCESS"),
        ("POLICY_NO_ATTEMPT", "CANCELLED_BEFORE_DISPATCH"),
    }
    continuation_permission = _permission(
        repository, lease, str(accepted.continuation_turn_id)
    )
    assert continuation_permission.overlay is RunPermissionOverlay.PLAN_READ_ONLY
    assert continuation_permission.effective_mode is PermissionMode.READ_ONLY


@pytest.mark.parametrize(
    "corruption_surface",
    (
        "assistant_arguments",
        "result_content",
        "result_event",
        "workflow_identity",
        "continuation_cut",
    ),
)
def test_round4_plan_batch_confirmation_rejects_every_exact_identity_drift(
    stage2_migrated_postgres_database,
    corruption_surface: str,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    workspace_id = repository.read_session_workspace_id(
        lease.guard, deadline_monotonic=monotonic() + 30
    )
    origin_turn_id, _ = _start_root(repository, lease)
    prepared = _commit_plan_batch(
        repository,
        lease,
        workspace_id=workspace_id,
        turn_id=origin_turn_id,
        selected_tool_name="enter_plan",
        selected_arguments={"reason": "exact ACK confirmation"},
        workflow_id=_id("plan-workflow"),
        expected_workflow_revision=None,
    )
    repository.accept_plan_tool_batch(
        lease.guard,
        candidate=prepared.candidate,
        deadline_monotonic=monotonic() + 30,
    )
    candidate = prepared.candidate
    selected = candidate.calls[candidate.selected_call_ordinal]
    assert selected.result_entry_id is not None

    if corruption_surface == "assistant_arguments":
        _admin_corrupt(
            stage2_migrated_postgres_database,
            "UPDATE pulsara_v3.assistant_message_blocks "
            "SET tool_arguments = %s::jsonb "
            "WHERE session_id = %s AND id = %s",
            (
                json.dumps({"reason": "different semantic candidate"}),
                candidate.session_id,
                selected.block_id,
            ),
        )
    elif corruption_surface == "result_content":
        corrupted = InlineContent.from_bytes(b'{"status":"corrupt"}')
        _admin_corrupt(
            stage2_migrated_postgres_database,
            "UPDATE pulsara_v3.transcript_entries "
            "SET inline_content = %s, content_digest = %s, content_size = %s "
            "WHERE session_id = %s AND id = %s",
            (
                corrupted.canonical_bytes,
                corrupted.digest,
                corrupted.size,
                candidate.session_id,
                selected.result_entry_id,
            ),
        )
    elif corruption_surface == "result_event":
        _admin_corrupt(
            stage2_migrated_postgres_database,
            "UPDATE pulsara_v3.agent_events SET payload = %s::jsonb "
            "WHERE session_id = %s AND event_type = 'ToolResultAccepted' "
            "AND subject_entry_id = %s",
            (
                json.dumps({"corrupt": True}),
                candidate.session_id,
                selected.result_entry_id,
            ),
        )
    elif corruption_surface == "workflow_identity":
        _admin_corrupt(
            stage2_migrated_postgres_database,
            "UPDATE pulsara_v3.plan_workflows SET entry_reason = %s "
            "WHERE session_id = %s AND id = %s",
            ("different reason", candidate.session_id, candidate.workflow_id),
        )
    else:
        assert candidate.continuation_context_binding_revision_id is not None
        _admin_corrupt(
            stage2_migrated_postgres_database,
            "UPDATE pulsara_v3.turn_context_binding_revisions "
            "SET source_through_sequence = source_through_sequence + 1 "
            "WHERE session_id = %s AND id = %s",
            (
                candidate.session_id,
                candidate.continuation_context_binding_revision_id,
            ),
        )

    with pytest.raises(ConversationKernelConflict):
        repository.confirm_plan_tool_batch_winner(
            candidate=candidate,
            deadline_monotonic=monotonic() + 30,
        )


def test_round4_plan_question_confirmation_rejects_binding_drift(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    workspace_id = repository.read_session_workspace_id(
        lease.guard, deadline_monotonic=monotonic() + 30
    )
    workflow_id, _ = _open_user_plan(repository, lease)
    origin_turn_id, _ = _start_root(repository, lease)
    prepared = _commit_plan_batch(
        repository,
        lease,
        workspace_id=workspace_id,
        turn_id=origin_turn_id,
        selected_tool_name="ask_plan_question",
        selected_arguments={
            "question": "Which exact path?",
            "options": [],
            "allow_free_text": True,
        },
        workflow_id=workflow_id,
        expected_workflow_revision=1,
    )
    repository.accept_plan_tool_batch(
        lease.guard,
        candidate=prepared.candidate,
        deadline_monotonic=monotonic() + 30,
    )
    candidate = prepared.candidate
    assert candidate.interaction_id is not None
    _admin_corrupt(
        stage2_migrated_postgres_database,
        "UPDATE pulsara_v3.plan_interactions "
        "SET request_semantic_digest = %s "
        "WHERE session_id = %s AND id = %s",
        ("sha256:" + "f" * 64, candidate.session_id, candidate.interaction_id),
    )

    with pytest.raises(ConversationKernelConflict):
        repository.confirm_plan_tool_batch_winner(
            candidate=candidate,
            deadline_monotonic=monotonic() + 30,
        )


def test_round4_runner_plan_barrier_prevents_earlier_sibling_dispatch(
    stage2_migrated_postgres_database, tmp_path
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    live_bus = LiveAgentEventBus()
    tools = DirectKernelToolPort(
        workspace_root=tmp_path,
        host_owner_id=_id("host"),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        session_id=lease.guard.session_id,
        live_bus=live_bus,
    )
    model = ScriptedKernelModel([_runner_plan_batch_stream()])

    async def accept_automatic_plan(
        candidate: PreparedPlanToolBatch, deadline: float
    ):
        return repository.accept_plan_tool_batch(
            lease.guard,
            candidate=candidate,
            deadline_monotonic=deadline,
        )

    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=tools,
        live_bus=live_bus,
        context_source_collector=StaticContextSourceCollector(),
        automatic_plan_continuation=accept_automatic_plan,
    )

    async def exercise():
        try:
            return await runner.run_turn(
                "Inspect the repository and enter Plan mode before editing."
            )
        finally:
            await tools.aclose()
            live_bus.close()

    result = asyncio.run(exercise())
    assert result.continuation_turn_id is not None
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        attempts = connection.execute(
            "SELECT count(*) AS count FROM pulsara_v3.tool_execution_attempts "
            "WHERE session_id = %s",
            (lease.guard.session_id,),
        ).fetchone()
        sibling = connection.execute(
            """
            SELECT r.result_origin_kind, r.result_state
            FROM pulsara_v3.tool_results AS r
            WHERE r.session_id = %s AND r.tool_call_id = 'call:write'
            """,
            (lease.guard.session_id,),
        ).fetchone()
    assert attempts["count"] == 0
    assert sibling == {
        "result_origin_kind": "POLICY_NO_ATTEMPT",
        "result_state": "CANCELLED_BEFORE_DISPATCH",
    }
    assert not (tmp_path / "must-not-exist.txt").exists()
    permission = _permission(repository, lease, str(result.continuation_turn_id))
    assert permission.overlay is RunPermissionOverlay.PLAN_READ_ONLY
    assert permission.effective_mode is PermissionMode.READ_ONLY


@pytest.mark.parametrize(
    ("plan_arguments", "expected_state"),
    [
        (
            {
                "question": "Choose a path",
                "options": [
                    {"label": "same", "description": "first"},
                    {"label": "same", "description": "second"},
                ],
                "allow_free_text": True,
            },
            "INVALID_ARGUMENTS",
        ),
        (
            {
                "question": "Choose a path",
                "options": [],
                "allow_free_text": True,
            },
            "TOOL_UNAVAILABLE",
        ),
    ],
)
def test_round4_rejected_plan_call_owns_batch_and_continues_without_dispatch(
    stage2_migrated_postgres_database,
    tmp_path,
    monkeypatch,
    plan_arguments: dict[str, object],
    expected_state: str,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    live_bus = LiveAgentEventBus()
    tools = DirectKernelToolPort(
        workspace_root=tmp_path,
        host_owner_id=_id("host"),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        session_id=lease.guard.session_id,
        live_bus=live_bus,
    )
    model = ScriptedKernelModel(
        [
            _runner_rejected_plan_batch_stream(plan_arguments),
            _runner_text_stream("The Plan request was invalid; no sibling ran."),
        ]
    )
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=tools,
        live_bus=live_bus,
        context_source_collector=StaticContextSourceCollector(),
    )
    original_accept = repository.accept_plan_tool_batch
    physical_accepts = 0

    def commit_then_lose_ack(*args, **kwargs):
        nonlocal physical_accepts
        physical_accepts += 1
        original_accept(*args, **kwargs)
        raise TimeoutError("simulated post-COMMIT ACK loss")

    monkeypatch.setattr(repository, "accept_plan_tool_batch", commit_then_lose_ack)

    async def exercise():
        try:
            return await runner.run_turn("Ask safely, without writing files.")
        finally:
            await tools.aclose()
            live_bus.close()

    result = asyncio.run(exercise())
    assert result.final_text == "The Plan request was invalid; no sibling ran."
    assert len(model.requests) == 2
    assert physical_accepts == 1
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        attempts = connection.execute(
            "SELECT count(*) AS count FROM pulsara_v3.tool_execution_attempts "
            "WHERE session_id = %s",
            (lease.guard.session_id,),
        ).fetchone()
        results = connection.execute(
            """
            SELECT tool_call_id, result_origin_kind, result_state
            FROM pulsara_v3.tool_results
            WHERE session_id = %s ORDER BY tool_call_id
            """,
            (lease.guard.session_id,),
        ).fetchall()
        plan_rows = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM pulsara_v3.plan_workflows
               WHERE session_id = %s) AS workflows,
              (SELECT count(*) FROM pulsara_v3.plan_interactions
               WHERE session_id = %s) AS interactions
            """,
            (lease.guard.session_id, lease.guard.session_id),
        ).fetchone()
    assert attempts["count"] == 0
    assert results == [
        {
            "tool_call_id": "call:plan",
            "result_origin_kind": "POLICY_NO_ATTEMPT",
            "result_state": expected_state,
        },
        {
            "tool_call_id": "call:write",
            "result_origin_kind": "POLICY_NO_ATTEMPT",
            "result_state": "CANCELLED_BEFORE_DISPATCH",
        },
    ]
    assert plan_rows == {"workflows": 0, "interactions": 0}
    assert not (tmp_path / "must-not-exist.txt").exists()


def test_round4_question_revise_approve_and_one_cut_materialization(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    workspace_id = repository.read_session_workspace_id(
        lease.guard, deadline_monotonic=monotonic() + 30
    )
    workflow_id, _ = _open_user_plan(repository, lease)
    turn_id, _ = _start_root(repository, lease)

    question_batch = _commit_plan_batch(
        repository,
        lease,
        workspace_id=workspace_id,
        turn_id=turn_id,
        selected_tool_name="ask_plan_question",
        selected_arguments={
            "question": "Which path?",
            "options": [
                {"label": "A", "description": "safe", "recommended": True},
                {"label": "B", "description": "fast", "recommended": False},
            ],
            "allow_free_text": True,
        },
        workflow_id=workflow_id,
        expected_workflow_revision=1,
    )
    opened_question = repository.accept_plan_tool_batch(
        lease.guard,
        candidate=question_batch.candidate,
        deadline_monotonic=monotonic() + 30,
    )
    assert repository.confirm_plan_tool_batch_winner(
        candidate=question_batch.candidate,
        deadline_monotonic=monotonic() + 30,
    ) == opened_question
    assert opened_question.question is not None
    assert opened_question.workflow_revision == 2
    with pytest.raises(ConversationKernelConflict, match="option answer is absent"):
        repository.resolve_plan_question(
            lease.guard,
            command_id=_id("command"),
            workflow_id=workflow_id,
            expected_workflow_revision=2,
            interaction_id=str(opened_question.interaction_id),
            answer=PlanQuestionAnswer(
                PlanQuestionAnswerKind.OPTION,
                option_ordinal=len(opened_question.question.options),
            ),
            result_id=_id("tool-result"),
            result_entry_id=_id("entry"),
            occurred_at=_now(),
            actor_id="user:test",
            deadline_monotonic=monotonic() + 30,
        )
    answer_command = _id("command")
    answered = repository.resolve_plan_question(
        lease.guard,
        command_id=answer_command,
        workflow_id=workflow_id,
        expected_workflow_revision=2,
        interaction_id=str(opened_question.interaction_id),
        answer=PlanQuestionAnswer(PlanQuestionAnswerKind.OPTION, option_ordinal=0),
        result_id=_id("tool-result"),
        result_entry_id=_id("entry"),
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert answered.workflow_revision == 3

    first_plan = "1. inspect\n2. implement 🙂\n3. verify"
    draft_batch = _commit_plan_batch(
        repository,
        lease,
        workspace_id=workspace_id,
        turn_id=turn_id,
        selected_tool_name="exit_plan",
        selected_arguments={"plan": first_plan, "summary": "first draft"},
        workflow_id=workflow_id,
        expected_workflow_revision=3,
    )
    opened_draft = repository.accept_plan_tool_batch(
        lease.guard,
        candidate=draft_batch.candidate,
        deadline_monotonic=monotonic() + 30,
    )
    assert repository.confirm_plan_tool_batch_winner(
        candidate=draft_batch.candidate,
        deadline_monotonic=monotonic() + 30,
    ) == opened_draft
    assert opened_draft.draft is not None
    assert opened_draft.origin_turn_completed
    chunk = repository.read_plan_draft_text_chunk(
        session_id=lease.guard.session_id,
        interaction_id=str(opened_draft.interaction_id),
        offset_utf8_bytes=0,
        limit_bytes=8,
        expected_plan_utf8_digest=opened_draft.draft.identity.plan_utf8_digest,
        deadline_monotonic=monotonic() + 30,
    )
    assert chunk.body
    assert chunk.next_offset_utf8_bytes > 0

    revise_command = _id("command")
    revise_turn = _id("turn")
    revise_entry = _id("entry")
    revise_revision = _id("context-revision")
    revised = repository.resolve_plan_draft_review(
        lease.guard,
        command_id=revise_command,
        workflow_id=workflow_id,
        expected_workflow_revision=4,
        interaction_id=str(opened_draft.interaction_id),
        decision=PlanDraftDecision.REVISE,
        feedback="please add rollback",
        continuation_turn_id=revise_turn,
        continuation_entry_id=revise_entry,
        continuation_context_binding_revision_id=revise_revision,
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert revised.workflow_status is PlanWorkflowStatus.ACTIVE
    assert _permission(repository, lease, revise_turn).overlay is (
        RunPermissionOverlay.PLAN_READ_ONLY
    )

    second_plan = first_plan + "\n4. rollback"
    second_draft_batch = _commit_plan_batch(
        repository,
        lease,
        workspace_id=workspace_id,
        turn_id=revise_turn,
        selected_tool_name="exit_plan",
        selected_arguments={"plan": second_plan},
        workflow_id=workflow_id,
        expected_workflow_revision=5,
    )
    second_draft = repository.accept_plan_tool_batch(
        lease.guard,
        candidate=second_draft_batch.candidate,
        deadline_monotonic=monotonic() + 30,
    )
    assert repository.confirm_plan_tool_batch_winner(
        candidate=second_draft_batch.candidate,
        deadline_monotonic=monotonic() + 30,
    ) == second_draft
    implementation_turn = _id("turn")
    implementation_entry = _id("entry")
    implementation_revision = _id("context-revision")
    approve_command = _id("command")
    approved = repository.resolve_plan_draft_review(
        lease.guard,
        command_id=approve_command,
        workflow_id=workflow_id,
        expected_workflow_revision=6,
        interaction_id=str(second_draft.interaction_id),
        decision=PlanDraftDecision.APPROVE,
        feedback=None,
        continuation_turn_id=implementation_turn,
        continuation_entry_id=implementation_entry,
        continuation_context_binding_revision_id=implementation_revision,
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert approved.workflow_status is PlanWorkflowStatus.APPROVED
    implementation_permission = _permission(repository, lease, implementation_turn)
    assert implementation_permission.overlay is RunPermissionOverlay.NONE
    assert implementation_permission.effective_mode is PermissionMode.ACCEPT_EDITS

    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=implementation_turn,
        deadline_monotonic=monotonic() + 30,
    )
    facts = CanonicalProviderInputReader(
        repository.connection_provider
    ).read_frozen_compile_snapshot(cut, deadline_monotonic=monotonic() + 30)
    assert facts.plan_workflow_fact is None
    assert facts.plan_handoff_fact is not None
    assert facts.plan_handoff_fact.handoff_kind is PlanHandoffKind.APPROVED_PLAN
    assert facts.approved_plan_materialization_fact is not None
    assert facts.approved_plan_materialization_fact.exact_plan_utf8 == second_plan.encode()
    assert facts.approved_plan_materialization_fact.disposition is (
        PlanApprovedMaterializationDisposition.PIN_EXISTING_CANONICAL_BLOCK
    )
    continuation_items = tuple(
        item
        for item in facts.canonical_input.items
        if (
            item.item_kind is FrozenProviderInputItemKind.PLAN_CONTINUATION
            and item.source_entry_id == implementation_entry
        )
    )
    assert len(continuation_items) == 1

    # A legal adopted snapshot may cover the original exit_plan block.  The
    # same RR reader must then select the other closed disposition and carry
    # the exact body for one compiler-owned materialization.
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        initial_sequence = int(
            connection.execute(
                "SELECT entry_sequence FROM pulsara_v3.transcript_entries "
                "WHERE session_id = %s AND id = %s",
                (lease.guard.session_id, implementation_entry),
            ).fetchone()["entry_sequence"]
        )
    repository.adopt_context_snapshot(
        lease.guard,
        turn_id=implementation_turn,
        snapshot_id=_id("context-snapshot"),
        context_binding_revision_id=_id("context-revision"),
        source_through_sequence=initial_sequence - 1,
        source_digest="sha256:" + "4" * 64,
        compiler_contract="test.compiler.v1",
        prompt_contract="test.prompt.v1",
        model_contract="test.model.v1",
        content=InlineContent.from_bytes(b"bounded adopted summary"),
        occurred_at=_now(),
        actor_id="test:compaction",
        deadline_monotonic=monotonic() + 30,
    )
    materialized_cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=implementation_turn,
        deadline_monotonic=monotonic() + 30,
    )
    materialized_facts = CanonicalProviderInputReader(
        repository.connection_provider
    ).read_frozen_compile_snapshot(
        materialized_cut, deadline_monotonic=monotonic() + 30
    )
    assert materialized_facts.approved_plan_materialization_fact is not None
    assert materialized_facts.approved_plan_materialization_fact.disposition is (
        PlanApprovedMaterializationDisposition.MATERIALIZE_REFERENCED_BLOCK
    )
    assert (
        materialized_facts.approved_plan_materialization_fact.exact_plan_utf8
        == second_plan.encode()
    )
    assert all(
        item.source_entry_id != second_draft_batch.assistant_entry_id
        for item in materialized_facts.canonical_input.items
    )

    # Present-empty and absent feedback normalize to the same stable winner.
    replacement = repository.acquire_host_writer(
        session_id=lease.guard.session_id,
        workspace_id=workspace_id,
        writer_owner_id=_id("replacement-host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    confirmed = repository.resolve_plan_draft_review(
        replacement.guard,
        command_id=approve_command,
        workflow_id=workflow_id,
        expected_workflow_revision=6,
        interaction_id=str(second_draft.interaction_id),
        decision=PlanDraftDecision.APPROVE,
        feedback=None,
        continuation_turn_id=implementation_turn,
        continuation_entry_id=implementation_entry,
        continuation_context_binding_revision_id=implementation_revision,
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert confirmed == approved

    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        plan_events = connection.execute(
            "SELECT event_type, payload FROM pulsara_v3.agent_events "
            "WHERE session_id = %s AND event_type LIKE 'Plan%%'",
            (lease.guard.session_id,),
        ).fetchall()
    allowed_payload_keys = {
        "PlanWorkflowEntered": {"entered_by", "workflow_revision"},
        "PlanQuestionAsked": {"workflow_id", "workflow_revision"},
        "PlanQuestionAnswered": {"selected_option", "answer_present"},
        "PlanDraftSubmitted": {"workflow_id", "workflow_revision"},
        "PlanDraftDecisionAccepted": {"decision", "feedback_present"},
        "PlanWorkflowExited": {"status"},
        "PlanContinuationAccepted": {"handoff_kind", "workflow_id"},
    }
    observed_types = {str(row["event_type"]) for row in plan_events}
    assert observed_types == set(allowed_payload_keys)
    for row in plan_events:
        event_type = str(row["event_type"])
        payload = dict(row["payload"])
        assert set(payload) == allowed_payload_keys[event_type]
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        assert first_plan not in serialized
        assert second_plan not in serialized
        assert "please add rollback" not in serialized


@pytest.mark.parametrize("first_disposition", ["CONSUMED", "CANCELLED"])
def test_round4_draft_cancel_handoff_is_queue_owned_exactly_once(
    stage2_migrated_postgres_database,
    first_disposition: str,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    workspace_id = repository.read_session_workspace_id(
        lease.guard, deadline_monotonic=monotonic() + 30
    )
    workflow_id, _ = _open_user_plan(repository, lease)
    origin_turn_id, _ = _start_root(repository, lease)
    draft_batch = _commit_plan_batch(
        repository,
        lease,
        workspace_id=workspace_id,
        turn_id=origin_turn_id,
        selected_tool_name="exit_plan",
        selected_arguments={"plan": "one immutable plan"},
        workflow_id=workflow_id,
        expected_workflow_revision=1,
    )
    opened = repository.accept_plan_tool_batch(
        lease.guard,
        candidate=draft_batch.candidate,
        deadline_monotonic=monotonic() + 30,
    )
    interaction_id = str(opened.interaction_id)
    cancelled = repository.resolve_plan_draft_review(
        lease.guard,
        command_id=_id("command"),
        workflow_id=workflow_id,
        expected_workflow_revision=2,
        interaction_id=interaction_id,
        decision=PlanDraftDecision.CANCEL,
        feedback=None,
        continuation_turn_id=None,
        continuation_entry_id=None,
        continuation_context_binding_revision_id=None,
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert cancelled.handoff_created_at_commit
    first_queue_id = _id("queue")
    repository.enqueue_prompt(
        lease.guard,
        command_id=_id("command"),
        queue_item_id=first_queue_id,
        client_submission_id=_id("submission"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=PermissionMode.READ_ONLY,
        content=InlineContent.from_bytes(b"first real prompt"),
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        queued = connection.execute(
            "SELECT * FROM pulsara_v3.prompt_queue_items WHERE id = %s",
            (first_queue_id,),
        ).fetchone()
    assert queued["pending_plan_handoff_workflow_id"] == workflow_id
    assert queued["pending_plan_handoff_interaction_id"] == interaction_id
    assert queued["pending_plan_handoff_kind"] == "CANCELLED_PLAN"

    if first_disposition == "CONSUMED":
        candidate = repository.prepare_prompt_head_consumption(
            session_id=lease.guard.session_id,
            occurred_at=_now(),
            actor_id="runtime:test",
            deadline_monotonic=monotonic() + 30,
        )
        assert candidate is not None
        confirmation = repository.consume_prepared_prompt_head(
            lease.guard,
            candidate=candidate,
            deadline_monotonic=monotonic() + 30,
        )
        assert confirmation is not None
        assert confirmation.kind is QueuedRootTurnAdmissionConfirmationKind.FULL
        accepted = confirmation.accepted
        assert accepted is not None
        with repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            entry = connection.execute(
                "SELECT * FROM pulsara_v3.transcript_entries WHERE id = %s",
                (accepted.entry_id,),
            ).fetchone()
        assert entry["source_plan_workflow_id"] == workflow_id
        assert entry["source_plan_interaction_id"] == interaction_id
        assert entry["source_plan_handoff_kind"] == "CANCELLED_PLAN"
        repository.interrupt_turn(
            lease.guard,
            turn_id=accepted.turn_id,
            reason="TEST_BOUNDARY",
            actor_id="test",
            occurred_at=_now(),
            deadline_monotonic=monotonic() + 30,
        )
    else:
        assert (
            repository.cancel_prompt(
                lease.guard,
                queue_item_id=first_queue_id,
                occurred_at=_now(),
                actor_id="user:test",
                deadline_monotonic=monotonic() + 30,
            )
            == "CANCELLED"
        )

    second_queue_id = _id("queue")
    repository.enqueue_prompt(
        lease.guard,
        command_id=_id("command"),
        queue_item_id=second_queue_id,
        client_submission_id=_id("submission"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=PermissionMode.ACCEPT_EDITS,
        content=InlineContent.from_bytes(b"second real prompt"),
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        second = connection.execute(
            "SELECT * FROM pulsara_v3.prompt_queue_items WHERE id = %s",
            (second_queue_id,),
        ).fetchone()
    assert second["pending_plan_handoff_workflow_id"] is None


def test_round4_aborted_plan_question_lowers_without_effect_unknown(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    workspace_id = repository.read_session_workspace_id(
        lease.guard, deadline_monotonic=monotonic() + 30
    )
    workflow_id, _ = _open_user_plan(repository, lease)
    turn_id, _ = _start_root(repository, lease)
    question_batch = _commit_plan_batch(
        repository,
        lease,
        workspace_id=workspace_id,
        turn_id=turn_id,
        selected_tool_name="ask_plan_question",
        selected_arguments={
            "question": "Continue?",
            "options": [],
            "allow_free_text": True,
        },
        workflow_id=workflow_id,
        expected_workflow_revision=1,
    )
    repository.accept_plan_tool_batch(
        lease.guard,
        candidate=question_batch.candidate,
        deadline_monotonic=monotonic() + 30,
    )
    repository.interrupt_turn(
        lease.guard,
        turn_id=turn_id,
        reason="FORCE_EXIT_TEST",
        occurred_at=_now(),
        actor_id="runtime:test",
        deadline_monotonic=monotonic() + 30,
    )
    repository.exit_plan_by_user(
        lease.guard,
        command_id=_id("command"),
        command_kind="FORCE_EXIT_PLAN",
        workflow_id=workflow_id,
        expected_workflow_revision=2,
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    control = CanonicalProtocolReader(repository.connection_provider).snapshot(
        session_id=lease.guard.session_id,
        maximum_entries=64,
        maximum_control_items=64,
        deadline_monotonic=monotonic() + 30,
    ).control
    assert control.latest_plan_handoff.handoff_kind == "FORCE_EXITED_PLAN"
    assert control.latest_plan_handoff.interaction_id == (
        question_batch.candidate.interaction_id
    )
    next_turn, _ = _start_root(repository, lease, text=b"continue outside plan")
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=next_turn,
        deadline_monotonic=monotonic() + 30,
    )
    facts = CanonicalProviderInputReader(
        repository.connection_provider
    ).read_frozen_compile_snapshot(cut, deadline_monotonic=monotonic() + 30)
    matching = tuple(
        item
        for item in facts.canonical_input.closures
        if item.assistant_entry_id == question_batch.assistant_entry_id
        and item.tool_call_id == question_batch.selected_tool_call_id
    )
    assert len(matching) == 1
    assert matching[0].closure_kind is (
        ProviderToolResultClosureKind.PLAN_INTERACTION_ABORTED
    )
    closure_items = tuple(
        item
        for item in facts.canonical_input.items
        if item.item_kind is FrozenProviderInputItemKind.TOOL_RESULT_CLOSURE
        and item.tool_call_id == question_batch.selected_tool_call_id
    )
    assert len(closure_items) == 1
    assert "no physical tool effect occurred" in closure_items[0].text
