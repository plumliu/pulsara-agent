"""Pure reducer for parent-owned subagent graph events."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Literal, cast

from pulsara_agent.event import (
    AgentEvent,
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
from pulsara_agent.runtime.subagent.facts import (
    SubagentConsumptionFact,
    SubagentDeliveryFact,
    SubagentEdgeFact,
    SubagentFactProvenance,
    SubagentGraphDiagnostic,
    SubagentGraphState,
    SubagentResultFact,
    SubagentRunFact,
    SubagentTaskFact,
    subagent_dependency_generation,
)
from pulsara_agent.runtime.subagent.invariants import run_attribution_error
from pulsara_agent.runtime.subagent.types import (
    SubagentBudget,
    SubagentCapabilityProfile,
    SubagentContextPolicy,
)


_SUBAGENT_EVENT_TYPES = (
    SubagentRunStartedEvent,
    SubagentMessageSentEvent,
    SubagentRunSuspendedEvent,
    SubagentRunCompletedEvent,
    SubagentRunFailedEvent,
    SubagentRunCancelledEvent,
    SubagentEdgeRecordedEvent,
    SubagentResultDeliveredEvent,
    SubagentTaskCreatedEvent,
    SubagentTaskScheduledEvent,
    SubagentTaskStartedEvent,
    SubagentTaskBlockedEvent,
    SubagentTaskCompletedEvent,
    SubagentTaskFailedEvent,
    SubagentTaskCancelledEvent,
    SubagentPhaseReportedEvent,
    SubagentResultSubmittedEvent,
    SubagentResultConsumedEvent,
)
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
_TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "blocked_dependency_failed"}
)
LiteralRunTerminal = Literal["failed", "cancelled"]
LiteralTaskTerminal = Literal["failed", "cancelled"]


def fold_subagent_graph(
    events: Iterable[AgentEvent],
    *,
    initial: SubagentGraphState | None = None,
) -> SubagentGraphState:
    state = initial or SubagentGraphState.empty()
    for event in events:
        state = apply_subagent_event(state, event)
    return state


def apply_subagent_event(state: SubagentGraphState, event: AgentEvent) -> SubagentGraphState:
    if not event.id:
        return _diagnose(
            state,
            event,
            code="subagent_event_invalid_id",
            entity_kind="graph",
            entity_id=None,
            message="Stored subagent reducer input requires a non-empty event id.",
        )
    sequence = event.sequence
    if sequence is None:
        return _diagnose(
            state,
            event,
            code="subagent_event_missing_sequence",
            entity_kind="graph",
            entity_id=None,
            message="Stored subagent reducer input must have a canonical sequence.",
        )
    if sequence < 1:
        return _diagnose(
            state,
            event,
            code="subagent_event_invalid_sequence",
            entity_kind="graph",
            entity_id=None,
            message="Stored subagent reducer input requires a positive canonical sequence.",
        )
    try:
        created_at = datetime.fromisoformat(event.created_at)
    except ValueError:
        created_at = None
    if (
        created_at is None
        or created_at.tzinfo is None
        or created_at.utcoffset() != timedelta(0)
    ):
        return _diagnose(
            state,
            event,
            code="subagent_event_invalid_created_at",
            entity_kind="graph",
            entity_id=None,
            message="Stored subagent reducer input requires a parseable UTC created_at.",
        )
    is_subagent = isinstance(event, _SUBAGENT_EVENT_TYPES)
    if (
        is_subagent
        and event.id in state.applied_subagent_event_ids
        and sequence > state.through_sequence
    ):
        return _diagnose(
            replace(state, through_sequence=sequence),
            event,
            code="subagent_event_sequence_reuse",
            entity_kind="graph",
            entity_id=None,
            message="A known subagent event id appeared at a new canonical sequence.",
        )
    if sequence <= state.through_sequence:
        if is_subagent and event.id in state.applied_subagent_event_ids:
            return state
        return state
    if sequence != state.through_sequence + 1:
        return _diagnose(
            replace(state, through_sequence=sequence),
            event,
            code="subagent_event_sequence_gap",
            entity_kind="graph",
            entity_id=None,
            message="Parent event stream is not contiguous.",
            metadata={"expected": state.through_sequence + 1, "actual": sequence},
        )
    state = replace(state, through_sequence=sequence)
    if not is_subagent:
        return state
    state = replace(
        state,
        applied_subagent_event_ids=state.applied_subagent_event_ids | {event.id},
    )
    if isinstance(event, SubagentRunStartedEvent):
        return _run_started(state, event)
    if isinstance(event, SubagentMessageSentEvent):
        return _message_sent(state, event)
    if isinstance(event, SubagentRunSuspendedEvent):
        return _run_suspended(state, event)
    if isinstance(event, SubagentRunCompletedEvent):
        return _run_completed(state, event)
    if isinstance(event, SubagentRunFailedEvent):
        return _run_terminal(state, event, "failed", event.reason_code)
    if isinstance(event, SubagentRunCancelledEvent):
        return _run_terminal(state, event, "cancelled", event.reason_code)
    if isinstance(event, SubagentEdgeRecordedEvent):
        return _edge_recorded(state, event)
    if isinstance(event, SubagentResultDeliveredEvent):
        return _result_delivered(state, event)
    if isinstance(event, SubagentTaskCreatedEvent):
        return _task_created(state, event)
    if isinstance(event, SubagentTaskScheduledEvent):
        return _task_scheduled(state, event)
    if isinstance(event, SubagentTaskStartedEvent):
        return _task_started(state, event)
    if isinstance(event, SubagentTaskBlockedEvent):
        return _task_blocked(state, event)
    if isinstance(event, SubagentTaskCompletedEvent):
        return _task_completed(state, event)
    if isinstance(event, SubagentTaskFailedEvent):
        return _task_terminal(state, event, "failed", event.reason_code)
    if isinstance(event, SubagentTaskCancelledEvent):
        return _task_terminal(state, event, "cancelled", event.reason_code)
    if isinstance(event, SubagentPhaseReportedEvent):
        return _phase_reported(state, event)
    if isinstance(event, SubagentResultSubmittedEvent):
        return _result_submitted(state, event)
    if isinstance(event, SubagentResultConsumedEvent):
        return _result_consumed(state, event)
    return state


def _run_started(state: SubagentGraphState, event: SubagentRunStartedEvent) -> SubagentGraphState:
    if event.subagent_run_id in state.runs:
        return _entity_error(state, event, "run", event.subagent_run_id, "duplicate_entity_creation")
    if event.edge_id in state.edges:
        return _entity_error(state, event, "edge", event.edge_id, "duplicate_entity_creation")
    task = state.tasks.get(event.task_id or "")
    if event.task_id is not None:
        if task is None:
            return _entity_error(state, event, "task", event.task_id, "orphan_subagent_event_reference")
        if task.current_run_id is not None:
            return _entity_error(state, event, "task", event.task_id, "subagent_task_run_attribution_mismatch")
        if task.batch_id != event.batch_id or task.create_tool_call_id != event.create_tool_call_id:
            return _entity_error(state, event, "task", event.task_id, "subagent_task_run_attribution_mismatch")
    created_at = _dt(event.created_at)
    provenance = _provenance(event, created_at)
    run = SubagentRunFact(
        subagent_run_id=event.subagent_run_id,
        parent_runtime_session_id=event.parent_runtime_session_id,
        parent_run_id=event.parent_run_id,
        parent_turn_id=event.parent_turn_id or event.turn_id,
        parent_reply_id=event.parent_reply_id or event.reply_id,
        parent_context_id=event.parent_context_id,
        parent_model_call_index=event.parent_model_call_index,
        edge_id=event.edge_id,
        spawning_tool_name=event.spawning_tool_name,
        spawn_initiator_kind=cast(object, event.spawn_initiator_kind),
        spawn_initiator_id=event.spawn_initiator_id,
        child_runtime_session_id=event.child_runtime_session_id,
        reported_child_run_id=None,
        task_id=event.task_id,
        batch_id=event.batch_id,
        create_tool_call_id=event.create_tool_call_id,
        run_index=event.run_index,
        label=event.label,
        role=cast(object, event.role),
        profile_id=event.profile_id,
        task_preview=event.task_preview,
        task_artifact_id=None,
        context_policy=SubagentContextPolicy(
            mode=event.context_policy.mode,
            include_parent_summary=event.context_policy.include_parent_summary,
            include_parent_current_task=event.context_policy.include_parent_current_task,
            include_parent_memory_projection=event.context_policy.include_parent_memory_projection,
            include_parent_artifact_refs=event.context_policy.include_parent_artifact_refs,
            max_parent_context_chars=event.context_policy.max_parent_context_chars,
            fork_source_context_id=event.context_policy.fork_source_context_id,
        ),
        capability_profile=SubagentCapabilityProfile(
            profile_id=event.capability_profile.profile_id,
            profile_name=event.capability_profile.profile_name,
            inherited_from_parent_context_id=(
                event.capability_profile.inherited_from_parent_context_id
            ),
            permission_mode=event.capability_profile.permission_mode,
            permission_policy=event.capability_profile.permission_policy,
            allowed_tool_names=event.capability_profile.allowed_tool_names,
            allowed_descriptor_ids=event.capability_profile.allowed_descriptor_ids,
            allowed_skill_names=event.capability_profile.allowed_skill_names,
            allowed_mcp_server_ids=event.capability_profile.allowed_mcp_server_ids,
            can_spawn_subagents=event.capability_profile.can_spawn_subagents,
            max_spawn_depth_from_root=event.capability_profile.max_spawn_depth_from_root,
            memory_enabled=event.capability_profile.memory_enabled,
            computed_from_parent_exposure_generation=(
                event.capability_profile.computed_from_parent_exposure_generation
            ),
            diagnostics=event.capability_profile.diagnostics,
        ),
        budget_snapshot=SubagentBudget.from_event_snapshot(event.budget_snapshot),
        status="running",
        phase=None,
        pending_kind=None,
        pending_reason_code=None,
        result_id=None,
        failure_reason_code=None,
        cancellation_reason_code=None,
        provenance=provenance,
    )
    edge = SubagentEdgeFact(
        edge_id=event.edge_id,
        edge_kind="spawn",
        parent_runtime_session_id=event.parent_runtime_session_id,
        parent_run_id=event.parent_run_id,
        parent_turn_id=event.parent_turn_id or event.turn_id,
        parent_reply_id=event.parent_reply_id or event.reply_id,
        subagent_run_id=event.subagent_run_id,
        child_runtime_session_id=event.child_runtime_session_id,
        child_run_id=None,
        source_context_id=event.parent_context_id,
        source_model_call_index=event.parent_model_call_index,
        source_tool_call_id=(
            event.spawn_initiator_id if event.spawn_initiator_kind == "tool_call" else None
        ),
        source_tool_name=event.spawning_tool_name,
        target_context_id=None,
        payload_artifact_id=None,
        result_id=None,
        result_artifact_id=None,
        returned_to_tool_call_id=None,
        provenance=provenance,
    )
    runs = dict(state.runs)
    edges = dict(state.edges)
    runs[run.subagent_run_id] = run
    edges[edge.edge_id] = edge
    return replace(state, runs=runs, edges=edges)


def _message_sent(state: SubagentGraphState, event: SubagentMessageSentEvent) -> SubagentGraphState:
    run = state.runs.get(event.subagent_run_id)
    if run is None:
        return _entity_error(state, event, "run", event.subagent_run_id, "orphan_subagent_event_reference")
    attribution_error = _run_attribution_state_error(
        state,
        event,
        run,
        parent_runtime_session_id=event.parent_runtime_session_id,
        child_runtime_session_id=event.child_runtime_session_id,
    )
    if attribution_error is not None:
        return attribution_error
    updated_at = _dt(event.created_at)
    runs = dict(state.runs)
    if event.delivery_kind == "spawn_task":
        task = state.tasks.get(run.task_id or "")
        if task is not None and task.objective_artifact_id != event.message_artifact_id:
            return _entity_error(state, event, "task", task.task_id, "subagent_task_run_attribution_mismatch")
        runs[run.subagent_run_id] = replace(
            run,
            task_artifact_id=event.message_artifact_id,
            provenance=_touch(run.provenance, event, updated_at),
        )
    edge_kind = "spawn" if event.delivery_kind == "spawn_task" else event.delivery_kind
    edges = dict(state.edges)
    existing = edges.get(event.edge_id)
    if existing is not None:
        existing_identity = (
            existing.edge_kind,
            existing.parent_runtime_session_id,
            existing.parent_run_id,
            existing.parent_turn_id,
            existing.parent_reply_id,
            existing.subagent_run_id,
            existing.child_runtime_session_id,
        )
        message_identity = (
            edge_kind,
            event.parent_runtime_session_id,
            event.parent_run_id,
            event.turn_id,
            event.reply_id,
            event.subagent_run_id,
            event.child_runtime_session_id,
        )
        if existing_identity != message_identity:
            return _entity_error(
                state,
                event,
                "edge",
                event.edge_id,
                "subagent_edge_identity_conflict",
            )
        if (
            existing.payload_artifact_id is not None
            and existing.payload_artifact_id != event.message_artifact_id
        ):
            return _entity_error(
                state,
                event,
                "edge",
                event.edge_id,
                "subagent_edge_payload_conflict",
            )
    if existing is None:
        edges[event.edge_id] = SubagentEdgeFact(
            edge_id=event.edge_id,
            edge_kind=edge_kind,
            parent_runtime_session_id=event.parent_runtime_session_id,
            parent_run_id=event.parent_run_id,
            parent_turn_id=event.turn_id,
            parent_reply_id=event.reply_id,
            subagent_run_id=event.subagent_run_id,
            child_runtime_session_id=event.child_runtime_session_id,
            child_run_id=None,
            source_context_id=None,
            source_model_call_index=None,
            source_tool_call_id=None,
            source_tool_name=None,
            target_context_id=None,
            payload_artifact_id=event.message_artifact_id,
            result_id=None,
            result_artifact_id=None,
            returned_to_tool_call_id=None,
            provenance=_provenance(event, updated_at),
        )
    else:
        edges[event.edge_id] = replace(
            existing,
            payload_artifact_id=event.message_artifact_id,
            provenance=_touch(existing.provenance, event, updated_at),
        )
    return replace(state, runs=runs, edges=edges)


def _run_suspended(state: SubagentGraphState, event: SubagentRunSuspendedEvent) -> SubagentGraphState:
    run = state.runs.get(event.subagent_run_id)
    if run is None:
        return _entity_error(state, event, "run", event.subagent_run_id, "orphan_subagent_event_reference")
    attribution_error = _run_attribution_state_error(
        state,
        event,
        run,
        parent_runtime_session_id=event.parent_runtime_session_id,
        child_runtime_session_id=event.child_runtime_session_id,
    )
    if attribution_error is not None:
        return attribution_error
    if run.status != "running":
        return _terminal_conflict(state, event, "run", event.subagent_run_id)
    updated_at = _dt(event.created_at)
    runs = dict(state.runs)
    runs[event.subagent_run_id] = replace(
        run,
        status="suspended",
        pending_kind=event.pending_kind,
        pending_reason_code=event.reason_code,
        provenance=_touch(run.provenance, event, updated_at),
    )
    return replace(state, runs=runs)


def _run_completed(state: SubagentGraphState, event: SubagentRunCompletedEvent) -> SubagentGraphState:
    run = state.runs.get(event.subagent_run_id)
    if run is None:
        return _entity_error(state, event, "run", event.subagent_run_id, "orphan_subagent_event_reference")
    attribution_error = _run_attribution_state_error(
        state,
        event,
        run,
        parent_runtime_session_id=event.parent_runtime_session_id,
        child_runtime_session_id=event.child_runtime_session_id,
        reported_child_run_id=event.child_run_id,
    )
    if attribution_error is not None:
        return attribution_error
    if run.status in _TERMINAL_RUN_STATUSES:
        return _terminal_conflict(state, event, "run", event.subagent_run_id)
    if run.result_id is not None and run.result_id != event.result_id:
        return _entity_error(
            state,
            event,
            "result",
            event.result_id,
            "subagent_explicit_result_completion_mismatch",
        )
    existing = state.results.get(event.result_id)
    if run.result_id is not None and existing is None:
        return _entity_error(
            state,
            event,
            "result",
            event.result_id,
            "orphan_subagent_event_reference",
        )
    if existing is not None:
        if existing.subagent_run_id != event.subagent_run_id:
            return _entity_error(
                state,
                event,
                "result",
                event.result_id,
                "subagent_task_run_attribution_mismatch",
            )
        if run.result_id is None:
            return _entity_error(
                state,
                event,
                "result",
                event.result_id,
                "duplicate_entity_creation",
            )
        if (
            existing.result_source != "explicit"
            or existing.status != "submitted"
            or existing.summary != event.summary
            or existing.final_message_artifact_id != event.result_artifact_id
            or existing.artifact_ids != tuple(event.artifact_ids)
        ):
            return _entity_error(
                state,
                event,
                "result",
                event.result_id,
                "subagent_explicit_result_completion_mismatch",
            )
    completed_at = _dt(event.created_at)
    provenance = _provenance(event, completed_at, terminal=True) if existing is None else _touch(
        existing.provenance,
        event,
        completed_at,
        terminal=True,
    )
    if existing is not None:
        result = replace(
            existing,
            status="completed",
            token_usage=dict(event.token_usage) if event.token_usage is not None else None,
            tool_call_count=event.tool_call_count,
            provenance=provenance,
        )
    else:
        result = SubagentResultFact(
            result_id=event.result_id,
            subagent_run_id=event.subagent_run_id,
            task_id=run.task_id,
            status="completed",
            result_source="inferred",
            summary=event.summary,
            output_preview=None,
            final_message_artifact_id=event.result_artifact_id,
            artifact_ids=tuple(event.artifact_ids),
            diagnostics=(),
            token_usage=dict(event.token_usage) if event.token_usage is not None else None,
            tool_call_count=event.tool_call_count,
            provenance=provenance,
        )
    runs = dict(state.runs)
    results = dict(state.results)
    runs[event.subagent_run_id] = replace(
        run,
        status="completed",
        reported_child_run_id=event.child_run_id or run.reported_child_run_id,
        result_id=event.result_id,
        pending_kind=None,
        pending_reason_code=None,
        provenance=_touch(run.provenance, event, completed_at, terminal=True),
    )
    results[event.result_id] = result
    return replace(state, runs=runs, results=results)


def _run_terminal(
    state: SubagentGraphState,
    event: SubagentRunFailedEvent | SubagentRunCancelledEvent,
    status: LiteralRunTerminal,
    reason_code: str,
) -> SubagentGraphState:
    run = state.runs.get(event.subagent_run_id)
    if run is None:
        return _entity_error(state, event, "run", event.subagent_run_id, "orphan_subagent_event_reference")
    attribution_error = _run_attribution_state_error(
        state,
        event,
        run,
        parent_runtime_session_id=event.parent_runtime_session_id,
        child_runtime_session_id=event.child_runtime_session_id,
    )
    if attribution_error is not None:
        return attribution_error
    if run.status in _TERMINAL_RUN_STATUSES:
        return _terminal_conflict(state, event, "run", event.subagent_run_id)
    if run.batch_id != event.batch_id or run.create_tool_call_id != event.create_tool_call_id:
        return _entity_error(state, event, "run", event.subagent_run_id, "subagent_task_run_attribution_mismatch")
    updated_at = _dt(event.created_at)
    runs = dict(state.runs)
    runs[event.subagent_run_id] = replace(
        run,
        status=status,
        failure_reason_code=reason_code if status == "failed" else None,
        cancellation_reason_code=reason_code if status == "cancelled" else None,
        pending_kind=None,
        pending_reason_code=None,
        provenance=_touch(run.provenance, event, updated_at, terminal=True),
    )
    return replace(state, runs=runs)


def _edge_recorded(state: SubagentGraphState, event: SubagentEdgeRecordedEvent) -> SubagentGraphState:
    run = state.runs.get(event.subagent_run_id)
    if run is None:
        return _entity_error(state, event, "run", event.subagent_run_id, "orphan_subagent_event_reference")
    attribution_error = _run_attribution_state_error(
        state,
        event,
        run,
        parent_runtime_session_id=event.parent_runtime_session_id,
        child_runtime_session_id=event.child_runtime_session_id,
        reported_child_run_id=event.child_run_id,
    )
    if attribution_error is not None:
        return attribution_error
    if event.edge_id in state.edges:
        return _entity_error(state, event, "edge", event.edge_id, "duplicate_entity_creation")
    created_at = _dt(event.created_at)
    edge = SubagentEdgeFact(
        edge_id=event.edge_id,
        edge_kind=event.edge_kind,
        parent_runtime_session_id=event.parent_runtime_session_id,
        parent_run_id=event.parent_run_id,
        parent_turn_id=event.parent_turn_id,
        parent_reply_id=event.parent_reply_id,
        subagent_run_id=event.subagent_run_id,
        child_runtime_session_id=event.child_runtime_session_id,
        child_run_id=event.child_run_id,
        source_context_id=event.source_context_id,
        source_model_call_index=event.source_model_call_index,
        source_tool_call_id=event.source_tool_call_id,
        source_tool_name=event.source_tool_name,
        target_context_id=event.target_context_id,
        payload_artifact_id=event.payload_artifact_id,
        result_id=event.result_id,
        result_artifact_id=event.result_artifact_id,
        returned_to_tool_call_id=event.returned_to_tool_call_id,
        provenance=_provenance(event, created_at),
    )
    edges = dict(state.edges)
    edges[event.edge_id] = edge
    runs = dict(state.runs)
    if event.child_run_id is not None:
        if run.reported_child_run_id not in {None, event.child_run_id}:
            return _entity_error(state, event, "run", event.subagent_run_id, "child_run_attribution_mismatch")
        runs[event.subagent_run_id] = replace(run, reported_child_run_id=event.child_run_id)
    consumptions = dict(state.consumptions)
    if event.edge_kind == "wait" and event.result_id is not None:
        if event.returned_to_tool_call_id is None:
            return _entity_error(state, event, "edge", event.edge_id, "orphan_subagent_event_reference")
        result = state.results.get(event.result_id)
        if result is None or result.status != "completed":
            return _entity_error(state, event, "result", event.result_id, "orphan_subagent_event_reference")
        if (
            result.subagent_run_id != event.subagent_run_id
            or result.final_message_artifact_id != event.result_artifact_id
        ):
            return _entity_error(
                state,
                event,
                "result",
                event.result_id,
                "subagent_task_run_attribution_mismatch",
            )
        consumptions[event.edge_id] = SubagentConsumptionFact(
            consumption_id=event.edge_id,
            kind="wait_run",
            consumer_tool_call_id=event.returned_to_tool_call_id,
            task_id=run.task_id,
            subagent_run_id=event.subagent_run_id,
            result_id=event.result_id,
            consumed_status="completed",
            terminal_event_id=None,
            diagnostics=(),
            provenance=_provenance(event, created_at),
        )
    return replace(state, edges=edges, runs=runs, consumptions=consumptions)


def _result_delivered(state: SubagentGraphState, event: SubagentResultDeliveredEvent) -> SubagentGraphState:
    result = state.results.get(event.result_id)
    if result is None or result.status != "completed" or result.subagent_run_id != event.subagent_run_id:
        return _entity_error(state, event, "result", event.result_id, "orphan_subagent_event_reference")
    run = state.runs.get(event.subagent_run_id)
    if run is None:
        return _entity_error(state, event, "run", event.subagent_run_id, "orphan_subagent_event_reference")
    attribution_error = _run_attribution_state_error(
        state,
        event,
        run,
        parent_runtime_session_id=event.parent_runtime_session_id,
    )
    if attribution_error is not None:
        return attribution_error
    if result.final_message_artifact_id != event.result_artifact_id:
        return _entity_error(state, event, "result", event.result_id, "subagent_task_run_attribution_mismatch")
    if result.summary != event.summary:
        return _entity_error(
            state,
            event,
            "result",
            event.result_id,
            "subagent_result_cross_event_mismatch",
        )
    deliveries = dict(state.deliveries)
    if event.result_id in deliveries:
        return _entity_error(state, event, "result", event.result_id, "duplicate_entity_creation")
    created_at = _dt(event.created_at)
    deliveries[event.result_id] = SubagentDeliveryFact(
        result_id=event.result_id,
        subagent_run_id=event.subagent_run_id,
        parent_run_id=event.parent_run_id,
        parent_turn_id=event.parent_turn_id,
        parent_reply_id=event.parent_reply_id,
        context_id=event.context_id,
        model_call_index=event.model_call_index,
        section_id=event.section_id,
        result_artifact_id=event.result_artifact_id,
        provenance=_provenance(event, created_at),
    )
    return replace(state, deliveries=deliveries)


def _task_created(state: SubagentGraphState, event: SubagentTaskCreatedEvent) -> SubagentGraphState:
    if event.task_id in state.tasks:
        return _entity_error(state, event, "task", event.task_id, "duplicate_entity_creation")
    if event.task_id in event.depends_on:
        return _entity_error(state, event, "task", event.task_id, "subagent_task_self_dependency")
    created_at = _dt(event.created_at)
    task = SubagentTaskFact(
        task_id=event.task_id,
        parent_run_id=event.run_id,
        parent_turn_id=event.turn_id,
        parent_reply_id=event.reply_id,
        batch_id=event.batch_id,
        create_tool_call_id=event.create_tool_call_id,
        task_key=event.task_key,
        label=event.label,
        profile_id=event.profile_id,
        display_role=event.display_role,
        objective_preview=event.objective_preview,
        objective_artifact_id=event.objective_artifact_id,
        depends_on=tuple(event.depends_on),
        status="created",
        current_run_id=None,
        run_index=None,
        scheduled_at=None,
        schedule_reason=None,
        phase=None,
        result_id=None,
        blocked_reason=None,
        blocked_by_task_ids=(),
        dependency_status_snapshot={},
        dependency_terminal_event_ids={},
        dependency_generation=None,
        failure_reason_code=None,
        cancellation_reason_code=None,
        provenance=_provenance(event, created_at),
    )
    tasks = dict(state.tasks)
    tasks[event.task_id] = task
    return replace(state, tasks=tasks)


def _task_scheduled(state: SubagentGraphState, event: SubagentTaskScheduledEvent) -> SubagentGraphState:
    task = state.tasks.get(event.task_id)
    if task is None:
        return _entity_error(state, event, "task", event.task_id, "orphan_subagent_event_reference")
    if task.status not in {"created", "waiting_dependency"} or task.current_run_id is not None:
        return _terminal_conflict(state, event, "task", event.task_id)
    if task.batch_id != event.batch_id or task.create_tool_call_id != event.create_tool_call_id:
        return _entity_error(state, event, "task", event.task_id, "subagent_task_run_attribution_mismatch")
    updated_at = _dt(event.created_at)
    tasks = dict(state.tasks)
    tasks[event.task_id] = replace(
        task,
        scheduled_at=updated_at,
        schedule_reason=event.schedule_reason,
        provenance=_touch(task.provenance, event, updated_at),
    )
    return replace(state, tasks=tasks)


def _task_started(state: SubagentGraphState, event: SubagentTaskStartedEvent) -> SubagentGraphState:
    task = state.tasks.get(event.task_id)
    run = state.runs.get(event.subagent_run_id)
    if task is None or run is None:
        return _entity_error(state, event, "task", event.task_id, "orphan_subagent_event_reference")
    if task.status not in {"created", "waiting_dependency"} or task.current_run_id is not None:
        return _terminal_conflict(state, event, "task", event.task_id)
    if run.task_id != event.task_id or run.run_index != event.run_index or event.run_index != 1:
        return _entity_error(state, event, "task", event.task_id, "subagent_task_run_attribution_mismatch")
    if run.batch_id != event.batch_id or run.create_tool_call_id != event.create_tool_call_id:
        return _entity_error(state, event, "task", event.task_id, "subagent_task_run_attribution_mismatch")
    updated_at = _dt(event.created_at)
    tasks = dict(state.tasks)
    tasks[event.task_id] = replace(
        task,
        status="running",
        current_run_id=event.subagent_run_id,
        run_index=event.run_index,
        blocked_reason=None,
        provenance=_touch(task.provenance, event, updated_at),
    )
    return replace(state, tasks=tasks)


def _task_blocked(state: SubagentGraphState, event: SubagentTaskBlockedEvent) -> SubagentGraphState:
    task = state.tasks.get(event.task_id)
    if task is None:
        return _entity_error(state, event, "task", event.task_id, "orphan_subagent_event_reference")
    if task.status not in {"created", "waiting_dependency"} or task.current_run_id is not None:
        return _terminal_conflict(state, event, "task", event.task_id)
    updated_at = _dt(event.created_at)
    terminal = event.status == "blocked_dependency_failed"
    if terminal and not event.dependency_terminal_event_ids:
        return _entity_error(state, event, "task", event.task_id, "orphan_subagent_event_reference")
    blocked_ids = tuple(event.blocked_by_task_ids)
    if set(event.dependency_status_snapshot) != set(blocked_ids):
        return _entity_error(
            state,
            event,
            "task",
            event.task_id,
            "subagent_dependency_snapshot_mismatch",
        )
    if terminal and set(event.dependency_terminal_event_ids) != set(blocked_ids):
        return _entity_error(
            state,
            event,
            "task",
            event.task_id,
            "subagent_dependency_terminal_ref_mismatch",
        )
    if terminal and event.dependency_generation != subagent_dependency_generation(
        event.dependency_terminal_event_ids
    ):
        return _entity_error(
            state,
            event,
            "task",
            event.task_id,
            "subagent_dependency_generation_mismatch",
        )
    for dependency_id in blocked_ids:
        dependency = state.tasks.get(dependency_id)
        if dependency is None:
            return _entity_error(
                state,
                event,
                "task",
                dependency_id,
                "orphan_subagent_event_reference",
            )
        if event.dependency_status_snapshot.get(dependency_id) != dependency.status:
            return _entity_error(
                state,
                event,
                "task",
                event.task_id,
                "subagent_dependency_snapshot_mismatch",
            )
        if terminal and event.dependency_terminal_event_ids.get(dependency_id) != (
            dependency.provenance.terminal_event_id
        ):
            return _entity_error(
                state,
                event,
                "task",
                event.task_id,
                "subagent_dependency_terminal_ref_mismatch",
            )
    tasks = dict(state.tasks)
    tasks[event.task_id] = replace(
        task,
        status=event.status,
        blocked_reason=event.blocked_reason,
        blocked_by_task_ids=tuple(event.blocked_by_task_ids),
        dependency_status_snapshot=dict(event.dependency_status_snapshot),
        dependency_terminal_event_ids=dict(event.dependency_terminal_event_ids),
        dependency_generation=event.dependency_generation,
        provenance=_touch(task.provenance, event, updated_at, terminal=terminal),
    )
    return replace(state, tasks=tasks)


def _task_completed(state: SubagentGraphState, event: SubagentTaskCompletedEvent) -> SubagentGraphState:
    task = state.tasks.get(event.task_id)
    run = state.runs.get(event.subagent_run_id)
    result = state.results.get(event.result_id)
    if task is None or run is None or result is None:
        return _entity_error(state, event, "task", event.task_id, "orphan_subagent_event_reference")
    if task.status != "running" or run.status != "completed":
        return _terminal_conflict(state, event, "task", event.task_id)
    if run.task_id != event.task_id or run.result_id != event.result_id:
        return _entity_error(state, event, "task", event.task_id, "subagent_task_run_attribution_mismatch")
    if result.final_message_artifact_id != event.primary_result_artifact_id:
        return _entity_error(state, event, "result", event.result_id, "subagent_task_run_attribution_mismatch")
    if result.result_source != event.result_source:
        return _entity_error(
            state,
            event,
            "result",
            event.result_id,
            "subagent_result_cross_event_mismatch",
        )
    updated_at = _dt(event.created_at)
    tasks = dict(state.tasks)
    tasks[event.task_id] = replace(
        task,
        status="completed",
        current_run_id=event.subagent_run_id,
        result_id=event.result_id,
        provenance=_touch(task.provenance, event, updated_at, terminal=True),
    )
    return replace(state, tasks=tasks)


def _task_terminal(
    state: SubagentGraphState,
    event: SubagentTaskFailedEvent | SubagentTaskCancelledEvent,
    status: LiteralTaskTerminal,
    reason_code: str,
) -> SubagentGraphState:
    task = state.tasks.get(event.task_id)
    if task is None:
        return _entity_error(state, event, "task", event.task_id, "orphan_subagent_event_reference")
    repair_cancel_of_blocked = (
        task.status == "blocked_dependency_failed"
        and status == "cancelled"
        and isinstance(event, SubagentTaskCancelledEvent)
        and event.repair_id is not None
    )
    if task.status in _TERMINAL_TASK_STATUSES and not repair_cancel_of_blocked:
        return _terminal_conflict(state, event, "task", event.task_id)
    if task.batch_id != event.batch_id or task.create_tool_call_id != event.create_tool_call_id:
        return _entity_error(state, event, "task", event.task_id, "subagent_task_run_attribution_mismatch")
    if task.current_run_id is None:
        if event.subagent_run_id is not None:
            return _entity_error(
                state,
                event,
                "task",
                event.task_id,
                "subagent_task_run_attribution_mismatch",
            )
    else:
        if event.subagent_run_id != task.current_run_id:
            return _entity_error(
                state,
                event,
                "task",
                event.task_id,
                "subagent_task_run_attribution_mismatch",
            )
        owning_run = state.runs.get(task.current_run_id)
        if owning_run is None:
            return _entity_error(
                state,
                event,
                "run",
                task.current_run_id,
                "orphan_subagent_event_reference",
            )
        if owning_run.task_id != task.task_id:
            return _entity_error(
                state,
                event,
                "task",
                event.task_id,
                "subagent_task_run_attribution_mismatch",
            )
        if owning_run.status != status:
            return _entity_error(
                state,
                event,
                "task",
                event.task_id,
                "subagent_task_terminal_run_mismatch",
            )
    updated_at = _dt(event.created_at)
    tasks = dict(state.tasks)
    tasks[event.task_id] = replace(
        task,
        status=status,
        current_run_id=event.subagent_run_id or task.current_run_id,
        failure_reason_code=reason_code if status == "failed" else None,
        cancellation_reason_code=reason_code if status == "cancelled" else None,
        provenance=_touch(task.provenance, event, updated_at, terminal=True),
    )
    return replace(state, tasks=tasks)


def _phase_reported(state: SubagentGraphState, event: SubagentPhaseReportedEvent) -> SubagentGraphState:
    run = state.runs.get(event.subagent_run_id)
    if run is None:
        return _entity_error(state, event, "run", event.subagent_run_id, "orphan_subagent_event_reference")
    if run.status in _TERMINAL_RUN_STATUSES:
        return _diagnose(
            state,
            event,
            code="subagent_phase_after_terminal",
            severity="warning",
            entity_kind="run",
            entity_id=event.subagent_run_id,
            message="Late phase report was ignored after terminal run state.",
        )
    if event.task_id is not None and run.task_id != event.task_id:
        return _entity_error(state, event, "task", event.task_id, "subagent_task_run_attribution_mismatch")
    updated_at = _dt(event.created_at)
    runs = dict(state.runs)
    runs[event.subagent_run_id] = replace(
        run,
        phase=event.phase,
        provenance=_touch(run.provenance, event, updated_at),
    )
    tasks = dict(state.tasks)
    if event.task_id is not None:
        task = tasks.get(event.task_id)
        if task is None:
            return _entity_error(state, event, "task", event.task_id, "orphan_subagent_event_reference")
        tasks[event.task_id] = replace(
            task,
            phase=event.phase,
            provenance=_touch(task.provenance, event, updated_at),
        )
    return replace(state, runs=runs, tasks=tasks)


def _result_submitted(state: SubagentGraphState, event: SubagentResultSubmittedEvent) -> SubagentGraphState:
    run = state.runs.get(event.subagent_run_id)
    if run is None:
        return _entity_error(state, event, "run", event.subagent_run_id, "orphan_subagent_event_reference")
    if run.status in _TERMINAL_RUN_STATUSES:
        return _terminal_conflict(state, event, "run", event.subagent_run_id)
    if event.task_id is not None and run.task_id != event.task_id:
        return _entity_error(state, event, "task", event.task_id, "subagent_task_run_attribution_mismatch")
    if run.result_id is not None or event.result_id in state.results:
        return _entity_error(state, event, "result", event.result_id, "duplicate_entity_creation")
    submitted_at = _dt(event.created_at)
    result = SubagentResultFact(
        result_id=event.result_id,
        subagent_run_id=event.subagent_run_id,
        task_id=event.task_id,
        status="submitted",
        result_source="explicit",
        summary=event.summary,
        output_preview=event.output_preview,
        final_message_artifact_id=event.result_artifact_id,
        artifact_ids=tuple(event.artifact_ids),
        diagnostics=tuple(dict(item) for item in event.diagnostics),
        token_usage=None,
        tool_call_count=None,
        provenance=_provenance(event, submitted_at),
    )
    results = dict(state.results)
    runs = dict(state.runs)
    results[event.result_id] = result
    runs[event.subagent_run_id] = replace(
        run,
        result_id=event.result_id,
        provenance=_touch(run.provenance, event, submitted_at),
    )
    return replace(state, results=results, runs=runs)


def _result_consumed(state: SubagentGraphState, event: SubagentResultConsumedEvent) -> SubagentGraphState:
    if event.consumption_id in state.consumptions:
        return _entity_error(state, event, "edge", event.consumption_id, "duplicate_entity_creation")
    result = state.results.get(event.result_id or "")
    if event.result_id is not None and result is None:
        return _entity_error(state, event, "result", event.result_id, "orphan_subagent_event_reference")
    task = state.tasks.get(event.task_id or "")
    if event.task_id is not None and task is None:
        return _entity_error(state, event, "task", event.task_id, "orphan_subagent_event_reference")
    run = state.runs.get(event.subagent_run_id or "")
    if event.subagent_run_id is not None and run is None:
        return _entity_error(state, event, "run", event.subagent_run_id, "orphan_subagent_event_reference")
    if task is not None:
        if task.status != event.consumed_status:
            return _entity_error(
                state,
                event,
                "task",
                task.task_id,
                "subagent_consumption_status_mismatch",
            )
        if event.subagent_run_id is not None and task.current_run_id != event.subagent_run_id:
            return _entity_error(
                state,
                event,
                "task",
                task.task_id,
                "subagent_task_run_attribution_mismatch",
            )
        if event.result_id is not None and task.result_id != event.result_id:
            return _entity_error(
                state,
                event,
                "result",
                event.result_id,
                "subagent_task_run_attribution_mismatch",
            )
        if event.result_id is None and event.terminal_event_id != task.provenance.terminal_event_id:
            return _entity_error(
                state,
                event,
                "task",
                task.task_id,
                "subagent_consumption_terminal_ref_mismatch",
            )
    if run is not None and run.status != event.consumed_status:
        return _entity_error(
            state,
            event,
            "run",
            run.subagent_run_id,
            "subagent_consumption_status_mismatch",
        )
    if result is not None:
        if result.status != "completed":
            return _terminal_conflict(state, event, "result", result.result_id)
        if event.subagent_run_id is not None and result.subagent_run_id != event.subagent_run_id:
            return _entity_error(
                state,
                event,
                "result",
                result.result_id,
                "subagent_task_run_attribution_mismatch",
            )
    created_at = _dt(event.created_at)
    consumptions = dict(state.consumptions)
    consumptions[event.consumption_id] = SubagentConsumptionFact(
        consumption_id=event.consumption_id,
        kind=event.kind,
        consumer_tool_call_id=event.consumer_tool_call_id,
        task_id=event.task_id,
        subagent_run_id=event.subagent_run_id,
        result_id=event.result_id,
        consumed_status=event.consumed_status,
        terminal_event_id=event.terminal_event_id,
        diagnostics=tuple(dict(item) for item in event.diagnostics),
        provenance=_provenance(event, created_at),
    )
    return replace(state, consumptions=consumptions)


def _entity_error(
    state: SubagentGraphState,
    event: AgentEvent,
    entity_kind: str,
    entity_id: str | None,
    code: str,
) -> SubagentGraphState:
    return _diagnose(
        state,
        event,
        code=code,
        entity_kind=cast(object, entity_kind),
        entity_id=entity_id,
        message=code.replace("_", " "),
    )


def _run_attribution_state_error(
    state: SubagentGraphState,
    event: AgentEvent,
    run: SubagentRunFact,
    *,
    parent_runtime_session_id: str | None = None,
    child_runtime_session_id: str | None = None,
    reported_child_run_id: str | None = None,
) -> SubagentGraphState | None:
    code = run_attribution_error(
        expected_parent_runtime_session_id=run.parent_runtime_session_id,
        expected_child_runtime_session_id=run.child_runtime_session_id,
        expected_reported_child_run_id=run.reported_child_run_id,
        parent_runtime_session_id=parent_runtime_session_id,
        child_runtime_session_id=child_runtime_session_id,
        reported_child_run_id=reported_child_run_id,
    )
    if code is None:
        return None
    return _entity_error(state, event, "run", run.subagent_run_id, code)


def _terminal_conflict(
    state: SubagentGraphState,
    event: AgentEvent,
    entity_kind: str,
    entity_id: str,
) -> SubagentGraphState:
    return _diagnose(
        state,
        event,
        code="conflicting_terminal_fact",
        entity_kind=cast(object, entity_kind),
        entity_id=entity_id,
        message="Event conflicts with the entity's current or terminal state.",
    )


def _diagnose(
    state: SubagentGraphState,
    event: AgentEvent,
    *,
    code: str,
    entity_kind: object,
    entity_id: str | None,
    message: str,
    severity: str = "error",
    metadata: dict[str, object] | None = None,
) -> SubagentGraphState:
    sequence = event.sequence or state.through_sequence
    diagnostic = SubagentGraphDiagnostic(
        code=code,
        severity=cast(object, severity),
        event_id=event.id,
        sequence=sequence,
        entity_kind=cast(object, entity_kind),
        entity_id=entity_id,
        message=message[:500],
        metadata=dict(metadata or {}),
    )
    return replace(
        state,
        diagnostics=(*state.diagnostics, diagnostic),
        consistent=state.consistent and severity != "error",
    )


def _provenance(
    event: AgentEvent,
    created_at: datetime,
    *,
    terminal: bool = False,
) -> SubagentFactProvenance:
    assert event.sequence is not None
    return SubagentFactProvenance(
        created_event_id=event.id,
        created_sequence=event.sequence,
        last_event_id=event.id,
        last_sequence=event.sequence,
        created_at=created_at,
        updated_at=created_at,
        terminal_event_id=event.id if terminal else None,
        terminal_sequence=event.sequence if terminal else None,
    )


def _touch(
    provenance: SubagentFactProvenance,
    event: AgentEvent,
    updated_at: datetime,
    *,
    terminal: bool = False,
) -> SubagentFactProvenance:
    assert event.sequence is not None
    return replace(
        provenance,
        last_event_id=event.id,
        last_sequence=event.sequence,
        updated_at=updated_at,
        terminal_event_id=event.id if terminal else provenance.terminal_event_id,
        terminal_sequence=event.sequence if terminal else provenance.terminal_sequence,
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
