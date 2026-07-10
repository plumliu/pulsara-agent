from __future__ import annotations

import json
import random
from dataclasses import replace

import pytest

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    RunEndEvent,
    SubagentEdgeRecordedEvent,
    SubagentMessageSentEvent,
    SubagentPhaseReportedEvent,
    SubagentResultConsumedEvent,
    SubagentResultDeliveredEvent,
    SubagentResultSubmittedEvent,
    SubagentRunCancelledEvent,
    SubagentRunCompletedEvent,
    SubagentRunFailedEvent,
    SubagentRunStartedEvent,
    SubagentRunSuspendedEvent,
    SubagentTaskBlockedEvent,
    SubagentTaskCancelledEvent,
    SubagentTaskCompletedEvent,
    SubagentTaskCreatedEvent,
    SubagentTaskFailedEvent,
    SubagentTaskScheduledEvent,
    SubagentTaskStartedEvent,
)
from pulsara_agent.runtime.permission import PermissionMode, preset_to_policy
from pulsara_agent.runtime.subagent.facts import (
    SubagentGraphDiagnostic,
    SubagentGraphState,
    subagent_dependency_generation,
)
from pulsara_agent.runtime.subagent.projection import project_subagent_graph
from pulsara_agent.runtime.subagent.reducer import apply_subagent_event, fold_subagent_graph


CTX = EventContext(run_id="run:parent", turn_id="turn:parent", reply_id="reply:parent")


class _StoredEvents:
    def __init__(self) -> None:
        self.sequence = 0

    def __call__(self, event: AgentEvent) -> AgentEvent:
        self.sequence += 1
        return event.model_copy(update={"sequence": self.sequence})


def _context() -> dict[str, object]:
    return {
        "mode": "isolated",
        "include_parent_summary": False,
        "include_parent_current_task": True,
        "include_parent_memory_projection": False,
        "include_parent_artifact_refs": False,
        "max_parent_context_chars": None,
        "fork_source_context_id": None,
    }


def _capability() -> dict[str, object]:
    return {
        "profile_id": "profile:test",
        "profile_name": "general_worker",
        "inherited_from_parent_context_id": None,
        "permission_mode": PermissionMode.READ_ONLY.value,
        "permission_policy": preset_to_policy(PermissionMode.READ_ONLY).to_dict(),
        "allowed_tool_names": ["artifact_read", "report_agent_phase", "report_agent_result"],
        "allowed_descriptor_ids": [],
        "allowed_skill_names": [],
        "allowed_mcp_server_ids": [],
        "can_spawn_subagents": False,
        "max_spawn_depth_from_root": 0,
        "memory_enabled": False,
        "computed_from_parent_exposure_generation": None,
        "diagnostics": [],
    }


def _budget() -> dict[str, object]:
    return {
        "max_concurrent_children_per_parent_run": 4,
        "max_concurrent_children_per_host_session": 8,
        "max_spawn_depth_from_root": 0,
        "child_timeout_seconds": None,
        "max_total_child_runs_per_parent_run": 16,
        "max_result_summary_chars_per_child": 4_000,
        "max_subagent_results_per_parent_compile": 8,
    }


def _task_created(task_id: str, *, depends_on: list[str] | None = None) -> SubagentTaskCreatedEvent:
    return SubagentTaskCreatedEvent(
        **CTX.event_fields(),
        task_id=task_id,
        batch_id="batch:test",
        create_tool_call_id="tool:create",
        task_key=task_id,
        profile_id="general_worker",
        objective_preview=f"objective {task_id}",
        objective_artifact_id=f"{task_id}:objective",
        depends_on=depends_on or [],
    )


def _run_started(
    run_id: str,
    *,
    task_id: str | None = None,
    batch_id: str | None = None,
    create_tool_call_id: str | None = None,
) -> SubagentRunStartedEvent:
    return SubagentRunStartedEvent(
        **CTX.event_fields(),
        subagent_run_id=run_id,
        task_id=task_id,
        batch_id=batch_id,
        create_tool_call_id=create_tool_call_id,
        run_index=1 if task_id is not None else None,
        edge_id=f"edge:{run_id}:spawn",
        parent_runtime_session_id="runtime:parent",
        parent_run_id=CTX.run_id,
        child_runtime_session_id=f"runtime:child:{run_id}",
        role="worker",
        profile_id="general_worker",
        task_preview="objective",
        context_policy=_context(),
        capability_profile=_capability(),
        budget_snapshot=_budget(),
    )


def _message(run_id: str, *, artifact_id: str) -> SubagentMessageSentEvent:
    return SubagentMessageSentEvent(
        **CTX.event_fields(),
        edge_id=f"edge:{run_id}:spawn",
        subagent_run_id=run_id,
        parent_runtime_session_id="runtime:parent",
        parent_run_id=CTX.run_id,
        child_runtime_session_id=f"runtime:child:{run_id}",
        message_artifact_id=artifact_id,
        message_preview="objective",
        delivery_kind="spawn_task",
    )


def _completed(run_id: str, result_id: str) -> SubagentRunCompletedEvent:
    artifact_id = f"artifact:{result_id}"
    return SubagentRunCompletedEvent(
        **CTX.event_fields(),
        subagent_run_id=run_id,
        parent_runtime_session_id="runtime:parent",
        child_runtime_session_id=f"runtime:child:{run_id}",
        child_run_id=f"child-run:{run_id}",
        result_id=result_id,
        summary="done",
        result_artifact_id=artifact_id,
        artifact_ids=[artifact_id],
    )


def _full_task_stream() -> list[AgentEvent]:
    stored = _StoredEvents()
    task_id = "task:a"
    run_id = "subagent_run:a"
    result_id = "result:a"
    events = [
        stored(_task_created(task_id)),
        stored(
            SubagentTaskBlockedEvent(
                **CTX.event_fields(),
                task_id=task_id,
                status="waiting_dependency",
                blocked_reason="waiting_dependency",
                blocked_by_task_ids=[],
            )
        ),
        stored(
            SubagentTaskScheduledEvent(
                **CTX.event_fields(),
                task_id=task_id,
                batch_id="batch:test",
                create_tool_call_id="tool:create",
                schedule_reason="dependency_satisfied",
            )
        ),
        stored(
            _run_started(
                run_id,
                task_id=task_id,
                batch_id="batch:test",
                create_tool_call_id="tool:create",
            )
        ),
        stored(_message(run_id, artifact_id=f"{task_id}:objective")),
        stored(
            SubagentTaskStartedEvent(
                **CTX.event_fields(),
                task_id=task_id,
                subagent_run_id=run_id,
                batch_id="batch:test",
                create_tool_call_id="tool:create",
                run_index=1,
                spawn_initiator_kind="dependency_satisfied",
                spawn_initiator_id=task_id,
            )
        ),
        stored(
            SubagentResultSubmittedEvent(
                **CTX.event_fields(),
                subagent_run_id=run_id,
                task_id=task_id,
                result_id=result_id,
                summary="done",
                result_artifact_id=f"artifact:{result_id}",
                artifact_ids=[f"artifact:{result_id}"],
            )
        ),
        stored(_completed(run_id, result_id)),
        stored(
            SubagentTaskCompletedEvent(
                **CTX.event_fields(),
                task_id=task_id,
                subagent_run_id=run_id,
                result_id=result_id,
                primary_result_artifact_id=f"artifact:{result_id}",
                result_source="explicit",
            )
        ),
    ]
    return events


def _generated_legal_subagent_streams(*, seed: int) -> tuple[list[AgentEvent], ...]:
    rng = random.Random(seed)
    streams: list[list[AgentEvent]] = [_full_task_stream()]

    # Primitive inferred completion, background delivery, then explicit wait.
    stored = _StoredEvents()
    primitive_run = "subagent_run:primitive-generated"
    primitive_result = "result:primitive-generated"
    primitive = [
        stored(_run_started(primitive_run)),
        stored(_message(primitive_run, artifact_id="artifact:primitive-task")),
        stored(_completed(primitive_run, primitive_result)),
        stored(
            SubagentResultDeliveredEvent(
                **CTX.event_fields(),
                subagent_run_id=primitive_run,
                parent_runtime_session_id="runtime:parent",
                parent_run_id=CTX.run_id,
                context_id="context:generated",
                model_call_index=4,
                section_id="subagent:results",
                result_id=primitive_result,
                result_artifact_id=f"artifact:{primitive_result}",
                summary="done",
            )
        ),
        stored(
            SubagentEdgeRecordedEvent(
                **CTX.event_fields(),
                edge_id="edge:generated-wait",
                edge_kind="wait",
                parent_runtime_session_id="runtime:parent",
                parent_run_id=CTX.run_id,
                subagent_run_id=primitive_run,
                child_runtime_session_id=f"runtime:child:{primitive_run}",
                result_id=primitive_result,
                result_artifact_id=f"artifact:{primitive_result}",
                returned_to_tool_call_id="tool:wait-generated",
            )
        ),
    ]
    streams.append(primitive)

    # Five-level terminal dependency cascade with real event-id lineage.
    stored = _StoredEvents()
    task_ids = [f"task:dag:{index}" for index in range(5)]
    dag: list[AgentEvent] = []
    for index, task_id in enumerate(task_ids):
        dag.append(
            stored(
                _task_created(
                    task_id,
                    depends_on=[] if index == 0 else [task_ids[index - 1]],
                )
            )
        )
    failed = stored(
        SubagentTaskFailedEvent(
            **CTX.event_fields(),
            task_id=task_ids[0],
            batch_id="batch:test",
            create_tool_call_id="tool:create",
            reason_code="generated_failure",
        )
    )
    dag.append(failed)
    upstream_event = failed
    upstream_status = "failed"
    for task_id, dependency_id in zip(task_ids[1:], task_ids[:-1], strict=True):
        refs = {dependency_id: upstream_event.id}
        blocked = stored(
            SubagentTaskBlockedEvent(
                **CTX.event_fields(),
                task_id=task_id,
                status="blocked_dependency_failed",
                blocked_reason="dependency_failed",
                blocked_by_task_ids=[dependency_id],
                dependency_status_snapshot={dependency_id: upstream_status},
                dependency_terminal_event_ids=refs,
                dependency_generation=subagent_dependency_generation(refs),
            )
        )
        dag.append(blocked)
        upstream_event = blocked
        upstream_status = "blocked_dependency_failed"
    streams.append(dag)

    # Cancelled upstream is a distinct legal terminal cause from failure.
    stored = _StoredEvents()
    cancelled_stream: list[AgentEvent] = [
        stored(_task_created("task:cancelled:a")),
        stored(
            _task_created(
                "task:cancelled:b",
                depends_on=["task:cancelled:a"],
            )
        ),
    ]
    cancelled = stored(
        SubagentTaskCancelledEvent(
            **CTX.event_fields(),
            task_id="task:cancelled:a",
            batch_id="batch:test",
            create_tool_call_id="tool:create",
            reason_code="generated_cancel",
            cancelled_by="runtime",
        )
    )
    refs = {"task:cancelled:a": cancelled.id}
    cancelled_stream.extend(
        [
            cancelled,
            stored(
                SubagentTaskBlockedEvent(
                    **CTX.event_fields(),
                    task_id="task:cancelled:b",
                    status="blocked_dependency_failed",
                    blocked_reason="dependency_failed",
                    blocked_by_task_ids=["task:cancelled:a"],
                    dependency_status_snapshot={"task:cancelled:a": "cancelled"},
                    dependency_terminal_event_ids=refs,
                    dependency_generation=subagent_dependency_generation(refs),
                )
            ),
        ]
    )
    streams.append(cancelled_stream)

    # Two independent task runs in one batch, one explicit and one inferred,
    # followed by task-level wait consumption and a post-parent-RunEnd terminal.
    stored = _StoredEvents()
    independent: list[AgentEvent] = []
    keys = ["left", "right"]
    rng.shuffle(keys)
    for key in keys:
        task_id = f"task:independent:{key}"
        run_id = f"subagent_run:independent:{key}"
        result_id = f"result:independent:{key}"
        independent.extend(
            [
                stored(_task_created(task_id)),
                stored(
                    SubagentTaskScheduledEvent(
                        **CTX.event_fields(),
                        task_id=task_id,
                        batch_id="batch:test",
                        create_tool_call_id="tool:create",
                        schedule_reason="immediate",
                    )
                ),
                stored(
                    _run_started(
                        run_id,
                        task_id=task_id,
                        batch_id="batch:test",
                        create_tool_call_id="tool:create",
                    )
                ),
                stored(_message(run_id, artifact_id=f"{task_id}:objective")),
                stored(
                    SubagentTaskStartedEvent(
                        **CTX.event_fields(),
                        task_id=task_id,
                        subagent_run_id=run_id,
                        batch_id="batch:test",
                        create_tool_call_id="tool:create",
                        spawn_initiator_kind="tool_call",
                        spawn_initiator_id="tool:create",
                    )
                ),
            ]
        )
        if key == "left":
            independent.append(
                stored(
                    SubagentResultSubmittedEvent(
                        **CTX.event_fields(),
                        subagent_run_id=run_id,
                        task_id=task_id,
                        result_id=result_id,
                        summary="explicit",
                        result_artifact_id=f"artifact:{result_id}",
                        artifact_ids=[f"artifact:{result_id}"],
                    )
                )
            )
        if key == "right":
            independent.append(
                stored(
                    RunEndEvent(
                        **CTX.event_fields(),
                        status="finished",
                        stop_reason="final",
                    )
                )
            )
        independent.extend(
            [
                stored(
                    _completed(run_id, result_id).model_copy(
                        update={"summary": "explicit" if key == "left" else "done"}
                    )
                ),
                stored(
                    SubagentTaskCompletedEvent(
                        **CTX.event_fields(),
                        task_id=task_id,
                        subagent_run_id=run_id,
                        result_id=result_id,
                        primary_result_artifact_id=f"artifact:{result_id}",
                        result_source="explicit" if key == "left" else "inferred",
                    )
                ),
                stored(
                    SubagentResultConsumedEvent(
                        **CTX.event_fields(),
                        consumption_id=f"consumption:{key}",
                        consumer_tool_call_id="tool:wait-all",
                        kind="wait_task",
                        task_id=task_id,
                        subagent_run_id=run_id,
                        result_id=result_id,
                        consumed_status="completed",
                    )
                ),
            ]
        )
    streams.append(independent)
    return tuple(streams)


def _normalized_state(state: SubagentGraphState) -> tuple[object, ...]:
    return (
        tuple(sorted((item.task_id, item.status, item.result_id) for item in state.tasks.values())),
        tuple(sorted((item.subagent_run_id, item.status, item.result_id) for item in state.runs.values())),
        tuple(sorted(state.results)),
        tuple(sorted(state.deliveries)),
        tuple(
            sorted(
                (item.task_id, item.subagent_run_id, item.result_id)
                for item in state.consumptions.values()
            )
        ),
    )


def _normalized_projection(state: SubagentGraphState) -> tuple[object, ...]:
    projection = project_subagent_graph("runtime:parent", state)
    result_ids = {
        node.result_id
        for node in projection.nodes
        if node.result_id is not None
    }
    delivered = {
        node.result_id
        for node in projection.nodes
        if node.result_id is not None and node.delivered
    }
    consumed = {
        (task.task_id, task.current_run_id, task.result_id)
        for task in projection.tasks
        if task.consumed_by_wait
    } | {
        (None, node.subagent_run_id, node.result_id)
        for node in projection.nodes
        if node.consumed_by_wait
        and not any(task.current_run_id == node.subagent_run_id for task in projection.tasks)
    }
    return (
        tuple(sorted((task.task_id, task.status, task.result_id) for task in projection.tasks)),
        tuple(sorted((node.subagent_run_id, node.status, node.result_id) for node in projection.nodes)),
        tuple(sorted(result_ids)),
        tuple(sorted(delivered)),
        tuple(sorted(consumed, key=lambda item: tuple(value or "" for value in item))),
    )


def test_reducer_task_created_waiting_started_completed() -> None:
    state = fold_subagent_graph(_full_task_stream())
    task = state.tasks["task:a"]
    run = state.runs["subagent_run:a"]
    assert state.consistent
    assert task.status == "completed"
    assert task.current_run_id == run.subagent_run_id
    assert run.status == "completed"
    assert state.results["result:a"].result_source == "explicit"


def test_reducer_state_and_nested_dependency_facts_are_immutable() -> None:
    state = fold_subagent_graph(_full_task_stream())

    with pytest.raises(TypeError):
        state.tasks["task:injected"] = state.tasks["task:a"]  # type: ignore[index]
    with pytest.raises(TypeError):
        state.tasks["task:a"].dependency_status_snapshot["task:injected"] = "failed"  # type: ignore[index]


def test_reducer_recursively_freezes_capability_snapshot_and_convenience_value() -> None:
    stored = _StoredEvents()
    event = _run_started("subagent_run:immutable-profile")
    event = event.model_copy(
        update={
            "capability_profile": event.capability_profile.model_copy(
                update={"diagnostics": ({"nested": {"value": "original"}},)}
            )
        }
    )
    state = fold_subagent_graph([stored(event)])
    fact = state.runs[event.subagent_run_id]

    event.capability_profile.permission_policy["filesystem"]["terminal"] = "allow"
    event.capability_profile.diagnostics[0]["nested"]["value"] = "mutated"

    assert fact.capability_profile.permission_policy["filesystem"]["terminal"] == "off"
    assert fact.capability_profile.diagnostics[0]["nested"]["value"] == "original"
    with pytest.raises(TypeError):
        fact.capability_profile.permission_policy["terminal_access"] = "allow"  # type: ignore[index]
    with pytest.raises(TypeError):
        fact.capability_profile.permission_policy["filesystem"]["terminal"] = "allow"  # type: ignore[index]
    with pytest.raises(TypeError):
        fact.capability_profile.diagnostics[0]["nested"]["value"] = "mutated"  # type: ignore[index]

    convenience = fact.capability_profile_value
    with pytest.raises(TypeError):
        convenience.permission_policy["filesystem"]["terminal"] = "allow"  # type: ignore[index]
    with pytest.raises(TypeError):
        convenience.diagnostics[0]["nested"]["value"] = "mutated"  # type: ignore[index]


def test_projection_recursively_thaws_inconsistent_graph_diagnostics_for_json() -> None:
    diagnostic = SubagentGraphDiagnostic(
        code="nested_diagnostic",
        severity="error",
        event_id="event:nested",
        sequence=1,
        entity_kind="graph",
        entity_id=None,
        message="nested",
        metadata={"outer": {"inner": ["value"]}},
    )
    state = replace(
        SubagentGraphState.empty(),
        diagnostics=(diagnostic,),
        consistent=False,
    )

    projection = project_subagent_graph("runtime:parent", state)
    payload = json.loads(json.dumps(projection.diagnostics))

    assert payload[0]["metadata"] == {"outer": {"inner": ["value"]}}


def test_reducer_scheduled_prefix_preserves_status_and_records_schedule_fact() -> None:
    stored = _StoredEvents()
    state = fold_subagent_graph(
        (
            stored(_task_created("task:scheduled")),
            stored(
                SubagentTaskScheduledEvent(
                    **CTX.event_fields(),
                    task_id="task:scheduled",
                    batch_id="batch:test",
                    create_tool_call_id="tool:create",
                    schedule_reason="immediate",
                )
            ),
        )
    )

    task = state.tasks["task:scheduled"]
    assert task.status == "created"
    assert task.schedule_reason == "immediate"
    assert task.scheduled_at is not None


def test_reducer_run_suspension_and_phase_transition() -> None:
    stored = _StoredEvents()
    run_id = "subagent_run:suspended"
    state = fold_subagent_graph(
        (
            stored(_run_started(run_id)),
            stored(_message(run_id, artifact_id="artifact:suspended-task")),
            stored(
                SubagentRunSuspendedEvent(
                    **CTX.event_fields(),
                    subagent_run_id=run_id,
                    parent_runtime_session_id="runtime:parent",
                    child_runtime_session_id=f"runtime:child:{run_id}",
                    pending_kind="approval",
                    reason_code="approval_required",
                )
            ),
            stored(
                SubagentPhaseReportedEvent(
                    **CTX.event_fields(),
                    subagent_run_id=run_id,
                    phase="waiting",
                )
            ),
        )
    )

    run = state.runs[run_id]
    assert run.status == "suspended"
    assert run.pending_kind == "approval"
    assert run.pending_reason_code == "approval_required"
    assert run.phase == "waiting"


def test_reducer_task_waiting_then_blocked_dependency_failed() -> None:
    stored = _StoredEvents()
    upstream = stored(_task_created("task:a"))
    downstream = stored(_task_created("task:b", depends_on=["task:a"]))
    failed = stored(
        SubagentTaskFailedEvent(
            **CTX.event_fields(),
            task_id="task:a",
            batch_id="batch:test",
            create_tool_call_id="tool:create",
            reason_code="failed",
        )
    )
    blocked = stored(
        SubagentTaskBlockedEvent(
            **CTX.event_fields(),
            task_id="task:b",
            status="blocked_dependency_failed",
            blocked_reason="dependency_failed",
            blocked_by_task_ids=["task:a"],
            dependency_status_snapshot={"task:a": "failed"},
            dependency_terminal_event_ids={"task:a": failed.id},
            dependency_generation=subagent_dependency_generation({"task:a": failed.id}),
        )
    )
    state = fold_subagent_graph([upstream, downstream, failed, blocked])
    assert state.consistent
    assert state.tasks["task:b"].status == "blocked_dependency_failed"
    assert state.tasks["task:b"].provenance.terminal_event_id == blocked.id


def test_reducer_transitive_blocker_snapshot_uses_planned_status() -> None:
    stored = _StoredEvents()
    events: list[AgentEvent] = [
        stored(_task_created("task:a")),
        stored(_task_created("task:b", depends_on=["task:a"])),
        stored(_task_created("task:c", depends_on=["task:b"])),
    ]
    failed = stored(
        SubagentTaskFailedEvent(
            **CTX.event_fields(),
            task_id="task:a",
            batch_id="batch:test",
            create_tool_call_id="tool:create",
            reason_code="failed",
        )
    )
    blocked_b = stored(
        SubagentTaskBlockedEvent(
            **CTX.event_fields(),
            task_id="task:b",
            status="blocked_dependency_failed",
            blocked_reason="dependency_failed",
            blocked_by_task_ids=["task:a"],
            dependency_status_snapshot={"task:a": "failed"},
            dependency_terminal_event_ids={"task:a": failed.id},
            dependency_generation=subagent_dependency_generation({"task:a": failed.id}),
        )
    )
    blocked_c = stored(
        SubagentTaskBlockedEvent(
            **CTX.event_fields(),
            task_id="task:c",
            status="blocked_dependency_failed",
            blocked_reason="dependency_failed",
            blocked_by_task_ids=["task:b"],
            dependency_status_snapshot={"task:b": "blocked_dependency_failed"},
            dependency_terminal_event_ids={"task:b": blocked_b.id},
            dependency_generation=subagent_dependency_generation({"task:b": blocked_b.id}),
        )
    )
    events.extend([failed, blocked_b, blocked_c])
    state = fold_subagent_graph(events)
    assert state.consistent
    assert state.tasks["task:c"].dependency_status_snapshot == {
        "task:b": "blocked_dependency_failed"
    }
    assert state.tasks["task:c"].dependency_terminal_event_ids == {"task:b": blocked_b.id}


def test_reducer_run_started_message_enriches_spawn_edge() -> None:
    stored = _StoredEvents()
    run_id = "subagent_run:primitive"
    state = fold_subagent_graph(
        [stored(_run_started(run_id)), stored(_message(run_id, artifact_id="artifact:task"))]
    )
    assert state.consistent
    assert state.runs[run_id].task_artifact_id == "artifact:task"
    assert state.edges[f"edge:{run_id}:spawn"].payload_artifact_id == "artifact:task"


def test_reducer_explicit_result_submitted_then_completed() -> None:
    state = fold_subagent_graph(_full_task_stream()[:-1])
    result = state.results["result:a"]
    assert state.consistent
    assert result.status == "completed"
    assert result.result_source == "explicit"


def test_reducer_completion_cannot_replace_explicit_result_identity_or_body() -> None:
    stored = _StoredEvents()
    run_id = "subagent_run:explicit-stable"
    explicit_result_id = "result:explicit-stable"
    explicit_artifact_id = "artifact:explicit-stable"
    prefix = [
        stored(_run_started(run_id)),
        stored(_message(run_id, artifact_id="artifact:task")),
        stored(
            SubagentResultSubmittedEvent(
                **CTX.event_fields(),
                subagent_run_id=run_id,
                result_id=explicit_result_id,
                summary="explicit body",
                output_preview="explicit preview",
                result_artifact_id=explicit_artifact_id,
                artifact_ids=[explicit_artifact_id],
                diagnostics=[{"code": "explicit", "nested": {"kept": True}}],
            )
        ),
    ]

    different_id = stored(_completed(run_id, "result:replacement"))
    different_id_state = fold_subagent_graph([*prefix, different_id])
    assert not different_id_state.consistent
    assert different_id_state.runs[run_id].result_id == explicit_result_id
    assert set(different_id_state.results) == {explicit_result_id}
    assert (
        different_id_state.diagnostics[-1].code
        == "subagent_explicit_result_completion_mismatch"
    )

    same_id_different_body = SubagentRunCompletedEvent(
        **CTX.event_fields(),
        subagent_run_id=run_id,
        parent_runtime_session_id="runtime:parent",
        child_runtime_session_id=f"runtime:child:{run_id}",
        result_id=explicit_result_id,
        summary="replacement body",
        result_artifact_id=explicit_artifact_id,
        artifact_ids=[explicit_artifact_id],
    ).model_copy(update={"sequence": different_id.sequence})
    different_body_state = fold_subagent_graph([*prefix, same_id_different_body])
    assert not different_body_state.consistent
    assert different_body_state.results[explicit_result_id].summary == "explicit body"
    assert (
        different_body_state.results[explicit_result_id].output_preview
        == "explicit preview"
    )

    matching_completion = SubagentRunCompletedEvent(
        **CTX.event_fields(),
        subagent_run_id=run_id,
        parent_runtime_session_id="runtime:parent",
        child_runtime_session_id=f"runtime:child:{run_id}",
        result_id=explicit_result_id,
        summary="explicit body",
        result_artifact_id=explicit_artifact_id,
        artifact_ids=[explicit_artifact_id],
        token_usage={"input_tokens": 17},
        tool_call_count=3,
    ).model_copy(update={"sequence": different_id.sequence})
    completed_state = fold_subagent_graph([*prefix, matching_completion])
    result = completed_state.results[explicit_result_id]
    assert completed_state.consistent
    assert result.status == "completed"
    assert result.summary == "explicit body"
    assert result.output_preview == "explicit preview"
    assert result.final_message_artifact_id == explicit_artifact_id
    assert result.diagnostics[0]["nested"]["kept"] is True
    assert result.token_usage == {"input_tokens": 17}
    assert result.tool_call_count == 3


def test_reducer_inferred_completion_creates_result_fact() -> None:
    stored = _StoredEvents()
    run_id = "subagent_run:inferred"
    result_id = "result:inferred"
    state = fold_subagent_graph(
        [
            stored(_run_started(run_id)),
            stored(_message(run_id, artifact_id="artifact:task")),
            stored(_completed(run_id, result_id)),
        ]
    )
    assert state.consistent
    assert state.results[result_id].result_source == "inferred"


def test_reducer_wait_edge_and_task_consumption_are_distinct() -> None:
    events = _full_task_stream()
    stored = _StoredEvents()
    stored.sequence = len(events)
    wait = stored(
        SubagentEdgeRecordedEvent(
            **CTX.event_fields(),
            edge_id="edge:wait",
            edge_kind="wait",
            parent_runtime_session_id="runtime:parent",
            parent_run_id=CTX.run_id,
            subagent_run_id="subagent_run:a",
            child_runtime_session_id="runtime:child:subagent_run:a",
            result_id="result:a",
            result_artifact_id="artifact:result:a",
            returned_to_tool_call_id="tool:wait-run",
        )
    )
    consumed = stored(
        SubagentResultConsumedEvent(
            **CTX.event_fields(),
            consumption_id="consumption:task",
            consumer_tool_call_id="tool:wait-task",
            kind="wait_task",
            task_id="task:a",
            subagent_run_id="subagent_run:a",
            result_id="result:a",
            consumed_status="completed",
        )
    )
    state = fold_subagent_graph([*events, wait, consumed])
    assert state.consistent
    assert set(state.consumptions) == {"edge:wait", "consumption:task"}


def test_reducer_rejects_wait_edge_consuming_another_runs_result() -> None:
    stored = _StoredEvents()
    run_a = "subagent_run:wait-a"
    run_b = "subagent_run:wait-b"
    result_a = "result:wait-a"
    result_b = "result:wait-b"
    events = [
        stored(_run_started(run_a)),
        stored(_message(run_a, artifact_id="artifact:task-a")),
        stored(_completed(run_a, result_a)),
        stored(_run_started(run_b)),
        stored(_message(run_b, artifact_id="artifact:task-b")),
        stored(_completed(run_b, result_b)),
    ]
    cross_run_wait = stored(
        SubagentEdgeRecordedEvent(
            **CTX.event_fields(),
            edge_id="edge:cross-run-wait",
            edge_kind="wait",
            parent_runtime_session_id="runtime:parent",
            parent_run_id=CTX.run_id,
            subagent_run_id=run_a,
            child_runtime_session_id=f"runtime:child:{run_a}",
            result_id=result_b,
            result_artifact_id=f"artifact:{result_b}",
            returned_to_tool_call_id="tool:wait-cross-run",
        )
    )

    state = fold_subagent_graph([*events, cross_run_wait])

    assert not state.consistent
    assert "edge:cross-run-wait" not in state.consumptions
    assert state.diagnostics[-1].code == "subagent_task_run_attribution_mismatch"


def test_reducer_rejects_run_terminal_session_attribution_mismatch() -> None:
    run_id = "subagent_run:session-mismatch"
    terminal_events: tuple[AgentEvent, ...] = (
        _completed(run_id, "result:session-mismatch").model_copy(
            update={"parent_runtime_session_id": "runtime:wrong-parent"}
        ),
        SubagentRunFailedEvent(
            **CTX.event_fields(),
            subagent_run_id=run_id,
            parent_runtime_session_id="runtime:wrong-parent",
            child_runtime_session_id=f"runtime:child:{run_id}",
            reason_code="failed",
        ),
        SubagentRunCancelledEvent(
            **CTX.event_fields(),
            subagent_run_id=run_id,
            parent_runtime_session_id="runtime:parent",
            child_runtime_session_id="runtime:wrong-child",
            reason_code="cancelled",
            cancelled_by="runtime",
        ),
    )

    for terminal_event in terminal_events:
        stored = _StoredEvents()
        state = fold_subagent_graph(
            [
                stored(_run_started(run_id)),
                stored(terminal_event),
            ]
        )
        assert not state.consistent
        assert state.runs[run_id].status == "running"
        assert (
            state.diagnostics[-1].code
            == "subagent_task_run_attribution_mismatch"
        )


def test_reducer_rejects_runtime_and_child_run_attribution_drift() -> None:
    run_id = "subagent_run:attribution-drift"

    stored = _StoredEvents()
    suspended = SubagentRunSuspendedEvent(
        **CTX.event_fields(),
        subagent_run_id=run_id,
        parent_runtime_session_id="runtime:wrong-parent",
        child_runtime_session_id="runtime:wrong-child",
        pending_kind="approval",
        reason_code="approval_required",
    )
    suspended_state = fold_subagent_graph(
        [stored(_run_started(run_id)), stored(suspended)]
    )
    assert not suspended_state.consistent
    assert suspended_state.runs[run_id].status == "running"

    stored = _StoredEvents()
    wrong_parent_message = SubagentMessageSentEvent(
        **CTX.event_fields(),
        edge_id="edge:wrong-parent-send",
        subagent_run_id=run_id,
        parent_runtime_session_id="runtime:wrong-parent",
        parent_run_id=CTX.run_id,
        child_runtime_session_id=f"runtime:child:{run_id}",
        message_artifact_id="artifact:followup",
        message_preview="follow up",
        delivery_kind="followup",
    )
    message_state = fold_subagent_graph(
        [stored(_run_started(run_id)), stored(wrong_parent_message)]
    )
    assert not message_state.consistent
    assert "edge:wrong-parent-send" not in message_state.edges

    stored = _StoredEvents()
    result_id = "result:attribution-drift"
    completed_state = fold_subagent_graph(
        [
            stored(_run_started(run_id)),
            stored(_completed(run_id, result_id)),
        ]
    )
    wrong_parent_delivery = stored(
        SubagentResultDeliveredEvent(
            **CTX.event_fields(),
            subagent_run_id=run_id,
            parent_runtime_session_id="runtime:wrong-parent",
            parent_run_id=CTX.run_id,
            context_id="context:wrong-parent",
            model_call_index=2,
            section_id="subagent:results",
            result_id=result_id,
            result_artifact_id=f"artifact:{result_id}",
            summary="done",
        )
    )
    delivery_state = apply_subagent_event(completed_state, wrong_parent_delivery)
    assert not delivery_state.consistent
    assert result_id not in delivery_state.deliveries

    stored = _StoredEvents()
    first_child_run = "child-run:first"
    edge = SubagentEdgeRecordedEvent(
        **CTX.event_fields(),
        edge_id="edge:reported-child-run",
        edge_kind="result",
        parent_runtime_session_id="runtime:parent",
        parent_run_id=CTX.run_id,
        subagent_run_id=run_id,
        child_runtime_session_id=f"runtime:child:{run_id}",
        child_run_id=first_child_run,
    )
    changed_completion = _completed(run_id, result_id).model_copy(
        update={"child_run_id": "child-run:replacement"}
    )
    child_run_state = fold_subagent_graph(
        [
            stored(_run_started(run_id)),
            stored(edge),
            stored(changed_completion),
        ]
    )
    assert not child_run_state.consistent
    assert child_run_state.runs[run_id].status == "running"
    assert child_run_state.runs[run_id].reported_child_run_id == first_child_run
    assert child_run_state.diagnostics[-1].code == "child_run_attribution_mismatch"


def test_reducer_delivery_requires_completed_result() -> None:
    events = _full_task_stream()
    stored = _StoredEvents()
    stored.sequence = len(events)
    delivery = stored(
        SubagentResultDeliveredEvent(
            **CTX.event_fields(),
            subagent_run_id="subagent_run:a",
            parent_runtime_session_id="runtime:parent",
            parent_run_id=CTX.run_id,
            context_id="context:parent",
            model_call_index=2,
            section_id="subagent:results",
            result_id="result:a",
            result_artifact_id="artifact:result:a",
            summary="done",
        )
    )
    state = fold_subagent_graph([*events, delivery])
    assert state.consistent
    assert state.deliveries["result:a"].context_id == "context:parent"


def test_reducer_rejects_result_summary_and_source_cross_event_drift() -> None:
    completed_task_events = _full_task_stream()
    stored = _StoredEvents()
    stored.sequence = len(completed_task_events)
    wrong_summary_delivery = stored(
        SubagentResultDeliveredEvent(
            **CTX.event_fields(),
            subagent_run_id="subagent_run:a",
            parent_runtime_session_id="runtime:parent",
            parent_run_id=CTX.run_id,
            context_id="context:wrong-summary",
            model_call_index=2,
            section_id="subagent:results",
            result_id="result:a",
            result_artifact_id="artifact:result:a",
            summary="different summary",
        )
    )
    delivery_state = fold_subagent_graph(
        [*completed_task_events, wrong_summary_delivery]
    )
    assert not delivery_state.consistent
    assert "result:a" not in delivery_state.deliveries
    assert (
        delivery_state.diagnostics[-1].code
        == "subagent_result_cross_event_mismatch"
    )

    completed_run_prefix = _full_task_stream()[:-1]
    wrong_source_task_completion = SubagentTaskCompletedEvent(
        **CTX.event_fields(),
        task_id="task:a",
        subagent_run_id="subagent_run:a",
        result_id="result:a",
        primary_result_artifact_id="artifact:result:a",
        result_source="inferred",
    ).model_copy(update={"sequence": len(completed_run_prefix) + 1})
    task_state = fold_subagent_graph(
        [*completed_run_prefix, wrong_source_task_completion]
    )
    assert not task_state.consistent
    assert task_state.tasks["task:a"].status == "running"
    assert task_state.results["result:a"].result_source == "explicit"
    assert task_state.diagnostics[-1].code == "subagent_result_cross_event_mismatch"


def test_reducer_duplicate_event_id_is_idempotent() -> None:
    stored = _StoredEvents()
    event = stored(_run_started("subagent_run:a"))
    state = apply_subagent_event(SubagentGraphState.empty(), event)
    assert apply_subagent_event(state, event) == state


def test_reducer_rejects_known_event_id_at_new_sequence() -> None:
    event = _run_started("subagent_run:sequence-reuse").model_copy(update={"sequence": 1})
    state = apply_subagent_event(SubagentGraphState.empty(), event)

    state = apply_subagent_event(state, event.model_copy(update={"sequence": 2}))

    assert not state.consistent
    assert state.diagnostics[-1].code == "subagent_event_sequence_reuse"


def test_reducer_rejects_non_utc_or_malformed_created_at() -> None:
    for created_at in ("not-a-time", "2026-07-10T12:00:00"):
        event = _run_started("subagent_run:invalid-time").model_copy(
            update={"sequence": 1, "created_at": created_at}
        )
        state = apply_subagent_event(SubagentGraphState.empty(), event)
        assert not state.consistent
        assert state.diagnostics[-1].code == "subagent_event_invalid_created_at"


def test_reducer_rejects_conflicting_terminal_facts() -> None:
    stored = _StoredEvents()
    run_id = "subagent_run:a"
    state = fold_subagent_graph(
        [
            stored(_run_started(run_id)),
            stored(
                SubagentRunFailedEvent(
                    **CTX.event_fields(),
                    subagent_run_id=run_id,
                    parent_runtime_session_id="runtime:parent",
                    child_runtime_session_id=f"runtime:child:{run_id}",
                    reason_code="failed",
                )
            ),
            stored(
                SubagentRunCancelledEvent(
                    **CTX.event_fields(),
                    subagent_run_id=run_id,
                    parent_runtime_session_id="runtime:parent",
                    child_runtime_session_id=f"runtime:child:{run_id}",
                    reason_code="cancelled",
                    cancelled_by="runtime",
                )
            ),
        ]
    )
    assert not state.consistent
    assert state.runs[run_id].status == "failed"
    assert state.diagnostics[-1].code == "conflicting_terminal_fact"


def test_reducer_rejects_spawn_and_message_edge_identity_collisions() -> None:
    stored = _StoredEvents()
    run_a = _run_started("subagent_run:edge-a").model_copy(
        update={"edge_id": "edge:shared"}
    )
    run_b = _run_started("subagent_run:edge-b").model_copy(
        update={"edge_id": "edge:shared"}
    )
    state = fold_subagent_graph([stored(run_a), stored(run_b)])
    assert not state.consistent
    assert "subagent_run:edge-b" not in state.runs
    assert state.edges["edge:shared"].subagent_run_id == "subagent_run:edge-a"

    stored = _StoredEvents()
    run_a = stored(_run_started("subagent_run:message-edge-a"))
    run_b = stored(_run_started("subagent_run:message-edge-b"))
    conflicting_message = stored(
        _message("subagent_run:message-edge-b", artifact_id="artifact:wrong-owner").model_copy(
            update={"edge_id": "edge:subagent_run:message-edge-a:spawn"}
        )
    )
    message_state = fold_subagent_graph([run_a, run_b, conflicting_message])
    assert not message_state.consistent
    assert message_state.diagnostics[-1].code == "subagent_edge_identity_conflict"
    assert (
        message_state.edges["edge:subagent_run:message-edge-a:spawn"].subagent_run_id
        == "subagent_run:message-edge-a"
    )


def test_reducer_running_task_terminal_requires_matching_terminal_owning_run() -> None:
    prefix = _full_task_stream()[:6]
    task_id = "task:a"
    run_id = "subagent_run:a"
    next_sequence = len(prefix) + 1

    missing_run = SubagentTaskFailedEvent(
        **CTX.event_fields(),
        task_id=task_id,
        batch_id="batch:test",
        create_tool_call_id="tool:create",
        reason_code="task_failed",
    ).model_copy(update={"sequence": next_sequence})
    missing_run_state = fold_subagent_graph([*prefix, missing_run])
    assert not missing_run_state.consistent
    assert missing_run_state.tasks[task_id].status == "running"
    assert missing_run_state.runs[run_id].status == "running"
    assert (
        missing_run_state.diagnostics[-1].code
        == "subagent_task_run_attribution_mismatch"
    )

    live_run = missing_run.model_copy(update={"subagent_run_id": run_id})
    live_run_state = fold_subagent_graph([*prefix, live_run])
    assert not live_run_state.consistent
    assert live_run_state.tasks[task_id].status == "running"
    assert live_run_state.runs[run_id].status == "running"
    assert (
        live_run_state.diagnostics[-1].code
        == "subagent_task_terminal_run_mismatch"
    )

    run_failed = SubagentRunFailedEvent(
        **CTX.event_fields(),
        subagent_run_id=run_id,
        parent_runtime_session_id="runtime:parent",
        child_runtime_session_id=f"runtime:child:{run_id}",
        batch_id="batch:test",
        create_tool_call_id="tool:create",
        reason_code="run_failed",
    ).model_copy(update={"sequence": next_sequence})
    task_failed = live_run.model_copy(update={"sequence": next_sequence + 1})
    terminal_state = fold_subagent_graph([*prefix, run_failed, task_failed])
    assert terminal_state.consistent
    assert terminal_state.runs[run_id].status == "failed"
    assert terminal_state.tasks[task_id].status == "failed"


def test_reducer_marks_sequence_gap_inconsistent() -> None:
    event = _run_started("subagent_run:gap").model_copy(update={"sequence": 2})
    state = apply_subagent_event(SubagentGraphState.empty(), event)
    assert not state.consistent
    assert state.diagnostics[-1].code == "subagent_event_sequence_gap"


def test_reducer_task_run_batch_attribution_mismatch() -> None:
    stored = _StoredEvents()
    state = fold_subagent_graph(
        [
            stored(_task_created("task:a")),
            stored(
                _run_started(
                    "subagent_run:a",
                    task_id="task:a",
                    batch_id="batch:wrong",
                    create_tool_call_id="tool:create",
                )
            ),
        ]
    )
    assert not state.consistent
    assert state.diagnostics[-1].code == "subagent_task_run_attribution_mismatch"


def test_reducer_prefix_fold_equals_incremental_apply() -> None:
    for events in _generated_legal_subagent_streams(seed=0x5A17):
        incremental = SubagentGraphState.empty()
        for index, event in enumerate(events, start=1):
            incremental = apply_subagent_event(incremental, event)
            folded = fold_subagent_graph(events[:index])
            assert incremental == folded
            assert folded.consistent
            assert _normalized_projection(folded) == _normalized_state(folded)
