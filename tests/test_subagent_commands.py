from __future__ import annotations

import pytest

from pulsara_agent.event import (
    EventContext,
    SubagentEdgeRecordedEvent,
    SubagentMessageSentEvent,
    SubagentResultConsumedEvent,
    SubagentResultDeliveredEvent,
    SubagentResultSubmittedEvent,
    SubagentRunCompletedEvent,
    SubagentRunFailedEvent,
    SubagentRunStartedEvent,
    SubagentRunSuspendedEvent,
    SubagentTaskCompletedEvent,
    SubagentTaskCreatedEvent,
    SubagentTaskFailedEvent,
    SubagentTaskScheduledEvent,
    SubagentTaskStartedEvent,
)
from pulsara_agent.runtime.permission import PermissionMode, preset_to_policy
from pulsara_agent.runtime.subagent.commands import (
    PlannedChildReservation,
    PlannedSubagentWrite,
    SubagentCommandPlanError,
    SubagentCommandPlanner,
)
from pulsara_agent.runtime.subagent.facts import SubagentGraphState
from pulsara_agent.runtime.subagent.reducer import fold_subagent_graph


CTX = EventContext(run_id="run:planner", turn_id="turn:planner", reply_id="reply:planner")


def test_command_plan_derives_affected_ids_and_records_reservation() -> None:
    task, started, message, task_started = _start_batch()
    reservation = PlannedChildReservation(
        reservation_id="reservation:one",
        parent_run_id=CTX.run_id,
        count=1,
    )
    plan = PlannedSubagentWrite(
        operation="start_task",
        expected_through_sequence=0,
        events=(task, started, message, task_started),
        required_reservations=(reservation,),
    )

    validated = SubagentCommandPlanner().validate(plan, state=SubagentGraphState.empty())

    assert validated.command_id.startswith("subagent_command:")
    assert validated.affected_task_ids == ("task:planner",)
    assert validated.affected_run_ids == ("subagent_run:planner",)
    assert validated.required_reservations == (reservation,)


def test_command_planner_rejects_invalid_same_batch_reference_before_commit() -> None:
    orphan_start = SubagentTaskStartedEvent(
        **CTX.event_fields(),
        task_id="task:missing",
        subagent_run_id="subagent_run:missing",
        run_index=1,
        spawn_initiator_kind="tool_call",
        spawn_initiator_id="call:create",
    )
    plan = PlannedSubagentWrite(
        operation="invalid",
        expected_through_sequence=0,
        events=(orphan_start,),
    )

    with pytest.raises(SubagentCommandPlanError, match="unknown task"):
        SubagentCommandPlanner().validate(plan, state=SubagentGraphState.empty())


def test_command_plan_rejects_duplicate_uncommitted_event_ids() -> None:
    task, _started, _message, _task_started = _start_batch()
    duplicate = task.model_copy()
    with pytest.raises(ValueError, match="event ids must be unique"):
        PlannedSubagentWrite(
            operation="duplicate",
            expected_through_sequence=0,
            events=(task, duplicate),
        )


def test_command_planner_rejects_explicit_result_replacement_before_commit() -> None:
    task, started, message, task_started = _start_batch()
    submitted = SubagentResultSubmittedEvent(
        **CTX.event_fields(),
        subagent_run_id=started.subagent_run_id,
        task_id=task.task_id,
        result_id="result:explicit",
        summary="explicit body",
        result_artifact_id="artifact:explicit",
        artifact_ids=["artifact:explicit"],
    )
    state = _fold(task, started, message, task_started, submitted)

    replacement = SubagentRunCompletedEvent(
        **CTX.event_fields(),
        subagent_run_id=started.subagent_run_id,
        parent_runtime_session_id=started.parent_runtime_session_id,
        child_runtime_session_id=started.child_runtime_session_id,
        result_id="result:replacement",
        summary="replacement",
        result_artifact_id="artifact:replacement",
        artifact_ids=["artifact:replacement"],
    )
    with pytest.raises(SubagentCommandPlanError, match="replace explicit result identity"):
        _validate_one(state, replacement)

    changed_body = replacement.model_copy(
        update={
            "result_id": "result:explicit",
            "result_artifact_id": "artifact:explicit",
            "artifact_ids": ["artifact:explicit"],
            "summary": "changed body",
        }
    )
    with pytest.raises(SubagentCommandPlanError, match="replace explicit result body"):
        _validate_one(state, changed_body)


def test_command_planner_rejects_running_task_terminal_without_terminal_owning_run() -> None:
    task, started, message, task_started = _start_batch()
    state = _fold(task, started, message, task_started)
    task_failed = SubagentTaskFailedEvent(
        **CTX.event_fields(),
        task_id=task.task_id,
        batch_id=task.batch_id,
        create_tool_call_id=task.create_tool_call_id,
        reason_code="failed",
    )

    with pytest.raises(SubagentCommandPlanError, match="task terminal run attribution"):
        _validate_one(state, task_failed)

    task_failed = task_failed.model_copy(
        update={"subagent_run_id": started.subagent_run_id}
    )
    with pytest.raises(SubagentCommandPlanError, match="invalid run transition"):
        _validate_one(state, task_failed)


def test_command_planner_rejects_cross_run_wait_result_before_commit() -> None:
    task_a, run_a, message_a, task_started_a = _start_batch("planner-a")
    task_b, run_b, message_b, task_started_b = _start_batch("planner-b")
    result_a = _completion(run_a, "result:planner-a")
    result_b = _completion(run_b, "result:planner-b")
    state = _fold(
        task_a,
        run_a,
        message_a,
        task_started_a,
        result_a,
        task_b,
        run_b,
        message_b,
        task_started_b,
        result_b,
    )
    wait = SubagentEdgeRecordedEvent(
        **CTX.event_fields(),
        edge_id="edge:cross-run-wait",
        edge_kind="wait",
        parent_runtime_session_id=run_a.parent_runtime_session_id,
        parent_run_id=CTX.run_id,
        subagent_run_id=run_a.subagent_run_id,
        child_runtime_session_id=run_a.child_runtime_session_id,
        result_id=result_b.result_id,
        result_artifact_id=result_b.result_artifact_id,
        returned_to_tool_call_id="tool:wait",
    )

    with pytest.raises(SubagentCommandPlanError, match="edge result/run attribution"):
        _validate_one(state, wait)


def test_command_planner_rejects_run_terminal_session_mismatch_before_commit() -> None:
    task, started, message, task_started = _start_batch()
    state = _fold(task, started, message, task_started)
    failed = SubagentRunFailedEvent(
        **CTX.event_fields(),
        subagent_run_id=started.subagent_run_id,
        parent_runtime_session_id="runtime:wrong",
        child_runtime_session_id=started.child_runtime_session_id,
        reason_code="failed",
        batch_id=task.batch_id,
        create_tool_call_id=task.create_tool_call_id,
    )

    with pytest.raises(SubagentCommandPlanError, match="runtime-session attribution"):
        _validate_one(state, failed)


def test_command_planner_rejects_task_schedule_creation_attribution_drift() -> None:
    task, _started, _message, _task_started = _start_batch()
    state = _fold(task)
    scheduled = SubagentTaskScheduledEvent(
        **CTX.event_fields(),
        task_id=task.task_id,
        batch_id="batch:wrong",
        create_tool_call_id="call:wrong",
        schedule_reason="immediate",
    )

    with pytest.raises(SubagentCommandPlanError, match="task creation attribution"):
        _validate_one(state, scheduled)


def test_command_planner_rejects_result_submitted_for_another_tasks_run() -> None:
    task_a, run_a, message_a, task_started_a = _start_batch("result-task-a")
    task_b, run_b, message_b, task_started_b = _start_batch("result-task-b")
    state = _fold(
        task_a,
        run_a,
        message_a,
        task_started_a,
        task_b,
        run_b,
        message_b,
        task_started_b,
    )
    submitted = SubagentResultSubmittedEvent(
        **CTX.event_fields(),
        subagent_run_id=run_a.subagent_run_id,
        task_id=task_b.task_id,
        result_id="result:wrong-task",
        summary="wrong task",
        result_artifact_id="artifact:wrong-task",
        artifact_ids=["artifact:wrong-task"],
    )

    with pytest.raises(SubagentCommandPlanError, match="result task/run attribution"):
        _validate_one(state, submitted)


def test_command_planner_rejects_consumed_status_drift_before_commit() -> None:
    task, started, message, task_started = _start_batch("consume-status")
    completed = _completion(started, "result:consume-status")
    task_completed = SubagentTaskCompletedEvent(
        **CTX.event_fields(),
        task_id=task.task_id,
        subagent_run_id=started.subagent_run_id,
        result_id=completed.result_id,
        primary_result_artifact_id=completed.result_artifact_id,
        result_source="inferred",
    )
    state = _fold(task, started, message, task_started, completed, task_completed)
    consumed = SubagentResultConsumedEvent(
        **CTX.event_fields(),
        consumption_id="consumption:wrong-status",
        consumer_tool_call_id="call:wait",
        kind="wait_task",
        task_id=task.task_id,
        subagent_run_id=started.subagent_run_id,
        result_id=completed.result_id,
        consumed_status="failed",
    )

    with pytest.raises(SubagentCommandPlanError, match="consumption task status"):
        _validate_one(state, consumed)


def test_command_planner_rejects_reducer_attribution_drift_before_commit() -> None:
    task, started, message, task_started = _start_batch("attribution-drift")
    running_state = _fold(task, started, message, task_started)

    wrong_message = SubagentMessageSentEvent(
        **CTX.event_fields(),
        edge_id="edge:wrong-parent-followup",
        subagent_run_id=started.subagent_run_id,
        parent_runtime_session_id="runtime:wrong-parent",
        parent_run_id=CTX.run_id,
        child_runtime_session_id=started.child_runtime_session_id,
        message_artifact_id="artifact:followup",
        message_preview="followup",
        delivery_kind="followup",
    )
    wrong_suspend = SubagentRunSuspendedEvent(
        **CTX.event_fields(),
        subagent_run_id=started.subagent_run_id,
        parent_runtime_session_id="runtime:wrong-parent",
        child_runtime_session_id="runtime:wrong-child",
        pending_kind="approval",
        reason_code="approval_required",
    )
    for event in (wrong_message, wrong_suspend):
        with pytest.raises(SubagentCommandPlanError, match="attribution"):
            _validate_one(running_state, event)

    result = _completion(started, "result:attribution-drift")
    completed_state = _fold(task, started, message, task_started, result)
    wrong_delivery = SubagentResultDeliveredEvent(
        **CTX.event_fields(),
        subagent_run_id=started.subagent_run_id,
        parent_runtime_session_id="runtime:wrong-parent",
        parent_run_id=CTX.run_id,
        context_id="context:wrong-parent",
        model_call_index=2,
        section_id="subagent:results",
        result_id=result.result_id,
        result_artifact_id=result.result_artifact_id,
        summary=result.summary,
    )
    with pytest.raises(SubagentCommandPlanError, match="attribution"):
        _validate_one(completed_state, wrong_delivery)

    first_child_run = "child-run:first"
    reported_edge = SubagentEdgeRecordedEvent(
        **CTX.event_fields(),
        edge_id="edge:reported-child-run",
        edge_kind="result",
        parent_runtime_session_id=started.parent_runtime_session_id,
        parent_run_id=CTX.run_id,
        subagent_run_id=started.subagent_run_id,
        child_runtime_session_id=started.child_runtime_session_id,
        child_run_id=first_child_run,
    )
    reported_state = _fold(task, started, message, task_started, reported_edge)
    changed_completion = result.model_copy(
        update={"child_run_id": "child-run:replacement"}
    )
    with pytest.raises(SubagentCommandPlanError, match="reported child run attribution"):
        _validate_one(reported_state, changed_completion)


def test_command_planner_reducer_guard_rejects_result_cross_event_drift() -> None:
    task, started, message, task_started = _start_batch("result-cross-event")
    submitted = SubagentResultSubmittedEvent(
        **CTX.event_fields(),
        subagent_run_id=started.subagent_run_id,
        task_id=task.task_id,
        result_id="result:cross-event",
        summary="canonical summary",
        result_artifact_id="artifact:cross-event",
        artifact_ids=["artifact:cross-event"],
    )
    completed = SubagentRunCompletedEvent(
        **CTX.event_fields(),
        subagent_run_id=started.subagent_run_id,
        parent_runtime_session_id=started.parent_runtime_session_id,
        child_runtime_session_id=started.child_runtime_session_id,
        result_id=submitted.result_id,
        summary=submitted.summary,
        result_artifact_id=submitted.result_artifact_id,
        artifact_ids=list(submitted.artifact_ids),
    )
    completed_run_state = _fold(
        task,
        started,
        message,
        task_started,
        submitted,
        completed,
    )
    wrong_source = SubagentTaskCompletedEvent(
        **CTX.event_fields(),
        task_id=task.task_id,
        subagent_run_id=started.subagent_run_id,
        result_id=submitted.result_id,
        primary_result_artifact_id=submitted.result_artifact_id,
        result_source="inferred",
    )
    with pytest.raises(
        SubagentCommandPlanError,
        match="subagent_result_cross_event_mismatch",
    ):
        _validate_one(completed_run_state, wrong_source)

    correct_task_completion = wrong_source.model_copy(
        update={"result_source": "explicit"}
    )
    completed_task_state = _fold(
        task,
        started,
        message,
        task_started,
        submitted,
        completed,
        correct_task_completion,
    )
    wrong_summary = SubagentResultDeliveredEvent(
        **CTX.event_fields(),
        subagent_run_id=started.subagent_run_id,
        parent_runtime_session_id=started.parent_runtime_session_id,
        parent_run_id=CTX.run_id,
        context_id="context:cross-event",
        model_call_index=3,
        section_id="subagent:results",
        result_id=submitted.result_id,
        result_artifact_id=submitted.result_artifact_id,
        summary="different summary",
    )
    with pytest.raises(
        SubagentCommandPlanError,
        match="subagent_result_cross_event_mismatch",
    ):
        _validate_one(completed_task_state, wrong_summary)


def _start_batch(key: str = "planner") -> tuple[
    SubagentTaskCreatedEvent,
    SubagentRunStartedEvent,
    SubagentMessageSentEvent,
    SubagentTaskStartedEvent,
]:
    task = SubagentTaskCreatedEvent(
        **CTX.event_fields(),
        task_id=f"task:{key}",
        batch_id="batch:planner",
        create_tool_call_id="call:create",
        task_key=key,
        profile_id="general_worker",
        objective_preview="work",
        objective_artifact_id=f"artifact:{key}",
    )
    started = SubagentRunStartedEvent(
        **CTX.event_fields(),
        subagent_run_id=f"subagent_run:{key}",
        task_id=task.task_id,
        batch_id=task.batch_id,
        create_tool_call_id=task.create_tool_call_id,
        run_index=1,
        edge_id=f"edge:{key}:spawn",
        parent_runtime_session_id="runtime:parent",
        parent_run_id=CTX.run_id,
        parent_turn_id=CTX.turn_id,
        parent_reply_id=CTX.reply_id,
        spawn_initiator_kind="tool_call",
        spawn_initiator_id="call:create",
        child_runtime_session_id=f"runtime:child:{key}",
        role="worker",
        task_preview="work",
        context_policy={
            "mode": "isolated",
            "include_parent_summary": False,
            "include_parent_current_task": True,
            "include_parent_memory_projection": False,
            "include_parent_artifact_refs": False,
            "max_parent_context_chars": None,
            "fork_source_context_id": None,
        },
        capability_profile={
            "profile_id": "profile:planner",
            "profile_name": "general_worker",
            "inherited_from_parent_context_id": None,
            "permission_mode": PermissionMode.READ_ONLY.value,
            "permission_policy": preset_to_policy(PermissionMode.READ_ONLY).to_dict(),
            "allowed_tool_names": (),
            "allowed_descriptor_ids": (),
            "allowed_skill_names": (),
            "allowed_mcp_server_ids": (),
            "can_spawn_subagents": False,
            "max_spawn_depth_from_root": 0,
            "memory_enabled": False,
            "computed_from_parent_exposure_generation": None,
            "diagnostics": (),
        },
        budget_snapshot={
            "max_concurrent_children_per_parent_run": 4,
            "max_concurrent_children_per_host_session": 8,
            "max_spawn_depth_from_root": 0,
            "child_timeout_seconds": None,
            "max_total_child_runs_per_parent_run": 16,
            "max_result_summary_chars_per_child": 4_000,
            "max_subagent_results_per_parent_compile": 8,
        },
    )
    message = SubagentMessageSentEvent(
        **CTX.event_fields(),
        edge_id=started.edge_id,
        subagent_run_id=started.subagent_run_id,
        parent_runtime_session_id=started.parent_runtime_session_id,
        parent_run_id=CTX.run_id,
        child_runtime_session_id=started.child_runtime_session_id,
        message_artifact_id=f"artifact:{key}",
        message_preview="work",
        delivery_kind="spawn_task",
    )
    task_started = SubagentTaskStartedEvent(
        **CTX.event_fields(),
        task_id=task.task_id,
        subagent_run_id=started.subagent_run_id,
        batch_id=task.batch_id,
        create_tool_call_id=task.create_tool_call_id,
        run_index=1,
        spawn_initiator_kind="tool_call",
        spawn_initiator_id="call:create",
    )
    return task, started, message, task_started


def _completion(
    started: SubagentRunStartedEvent,
    result_id: str,
) -> SubagentRunCompletedEvent:
    artifact_id = f"artifact:{result_id}"
    return SubagentRunCompletedEvent(
        **CTX.event_fields(),
        subagent_run_id=started.subagent_run_id,
        parent_runtime_session_id=started.parent_runtime_session_id,
        child_runtime_session_id=started.child_runtime_session_id,
        result_id=result_id,
        summary="done",
        result_artifact_id=artifact_id,
        artifact_ids=[artifact_id],
    )


def _fold(*events):
    return fold_subagent_graph(
        event.model_copy(update={"sequence": index})
        for index, event in enumerate(events, start=1)
    )


def _validate_one(state: SubagentGraphState, event) -> None:
    SubagentCommandPlanner().validate(
        PlannedSubagentWrite(
            operation="negative-regression",
            expected_through_sequence=state.through_sequence,
            events=(event,),
        ),
        state=state,
    )
