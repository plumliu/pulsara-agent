"""Business-level run timeline assembled from runtime events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from pulsara_agent.event import (
    AgentEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    PlanQuestionAnsweredEvent,
    PlanQuestionAskedEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    RunErrorEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
    TextBlockSegmentEvent,
    ThinkingBlockSegmentEvent,
    ToolCallArgumentsSegmentEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)

TimelineItemKind = Literal[
    "run_boundary",
    "continuation_boundary",
    "reply",
    "model_call",
    "assistant_text",
    "assistant_thinking",
    "tool_call",
    "tool_result",
    "permission_request",
    "plan_mode",
    "plan_question",
    "plan_exit_request",
    "error",
]


@dataclass(slots=True)
class RunTimelineItem:
    kind: TimelineItemKind
    title: str
    run_id: str
    turn_id: str
    reply_id: str
    start_sequence: int | None
    end_sequence: int | None
    status: str | None = None
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "reply_id": self.reply_id,
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "status": self.status,
            "summary": self.summary,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunTimelineItem":
        return cls(
            kind=payload["kind"],
            title=payload["title"],
            run_id=payload["run_id"],
            turn_id=payload["turn_id"],
            reply_id=payload["reply_id"],
            start_sequence=payload.get("start_sequence"),
            end_sequence=payload.get("end_sequence"),
            status=payload.get("status"),
            summary=payload.get("summary", ""),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(slots=True)
class RunTimeline:
    runtime_session_id: str
    run_id: str
    status: str
    start_sequence: int | None
    end_sequence: int | None
    items: list[RunTimelineItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_session_id": self.runtime_session_id,
            "run_id": self.run_id,
            "status": self.status,
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunTimeline":
        return cls(
            runtime_session_id=payload["runtime_session_id"],
            run_id=payload["run_id"],
            status=payload["status"],
            start_sequence=payload.get("start_sequence"),
            end_sequence=payload.get("end_sequence"),
            items=[
                RunTimelineItem.from_dict(item) for item in payload.get("items", [])
            ],
        )


def build_run_timeline(
    events: Iterable[AgentEvent],
    *,
    runtime_session_id: str,
    run_id: str | None = None,
) -> RunTimeline:
    ordered = sorted(
        list(events),
        key=lambda event: event.sequence if event.sequence is not None else 0,
    )
    if run_id is not None:
        ordered = [event for event in ordered if event.run_id == run_id]
    if not ordered:
        raise ValueError("Cannot build a run timeline without events")

    run_id = run_id or ordered[0].run_id
    items: list[RunTimelineItem] = []
    replies: dict[str, RunTimelineItem] = {}
    model_calls: dict[str, RunTimelineItem] = {}
    text_blocks: dict[tuple[str, str], RunTimelineItem] = {}
    thinking_blocks: dict[tuple[str, str], RunTimelineItem] = {}
    tool_calls: dict[str, RunTimelineItem] = {}
    tool_results: dict[str, RunTimelineItem] = {}
    plan_questions: dict[str, RunTimelineItem] = {}
    plan_exits: dict[str, RunTimelineItem] = {}
    failed = False
    waiting_user = False
    terminal_status: str | None = None

    for event in ordered:
        if isinstance(event, RunStartEvent):
            if event.new_run_boundary is not None:
                boundary = event.new_run_boundary
                metadata = {
                    "run_entry_kind": "host",
                    "boundary_id": boundary.identity.boundary_id,
                    "boundary_kind": boundary.identity.kind.value,
                    "permission_snapshot_id": boundary.permission_snapshot_id,
                    "model_target_fingerprint": boundary.model_target_fingerprint,
                    "mcp_installation_id": boundary.mcp_installation_id,
                    "capability_basis_fingerprint": (
                        boundary.capability_basis.basis_fingerprint
                    ),
                }
                title = "Host run boundary"
            else:
                entry = event.subagent_run_entry
                metadata = {
                    "run_entry_kind": "subagent_child",
                    "subagent_run_id": (
                        entry.subagent_run_id if entry is not None else None
                    ),
                    "subagent_task_id": (
                        entry.subagent_task_id if entry is not None else None
                    ),
                }
                title = "Subagent run entry"
            item = _item(
                "run_boundary",
                title,
                event,
                status="committed",
                metadata=metadata,
            )
            _finish(item, event, status="committed")
            items.append(item)
            continue
        if isinstance(event, RunInteractionResumeBoundaryEvent):
            boundary = event.boundary
            item = _item(
                "continuation_boundary",
                f"Interaction resume: {boundary.interaction_kind}",
                event,
                status="committed",
                metadata={
                    "boundary_id": boundary.identity.boundary_id,
                    "interaction_id": boundary.interaction_id,
                    "interaction_kind": boundary.interaction_kind,
                    "exposure_transition": boundary.exposure_transition,
                    "source_exposure_id": boundary.source_exposure_id,
                    "effective_exposure_id": boundary.effective_exposure_id,
                    "mcp_installation_id": boundary.mcp_installation_id,
                },
            )
            _finish(item, event, status="committed")
            items.append(item)
            continue
        if isinstance(event, ReplyStartEvent):
            item = _item(
                "reply",
                "Assistant reply",
                event,
                status="running",
                metadata={"name": event.name, "role": event.role},
            )
            replies[event.reply_id] = item
            items.append(item)
            continue
        if isinstance(event, ReplyEndEvent):
            _finish(replies.get(event.reply_id), event, status="completed")
            continue
        if isinstance(event, ModelCallStartEvent):
            item = _item(
                "model_call",
                f"Model call: {event.resolved_call.target.model_id}",
                event,
                status="running",
                metadata={
                    "resolved_model_call_id": event.resolved_call.resolved_model_call_id,
                    "target_fingerprint": event.resolved_call.target.target_fingerprint,
                    "model_id": event.resolved_call.target.model_id,
                    "model_role": event.resolved_call.target.model_role,
                    "provider": event.resolved_call.target.provider,
                    "context_id": event.context_id,
                    "model_call_index": event.model_call_index,
                },
            )
            model_calls[event.reply_id] = item
            items.append(item)
            continue
        if isinstance(event, ModelCallEndEvent):
            item = model_calls.get(event.reply_id)
            _finish(item, event, status="completed")
            if item is not None:
                item.metadata.update(
                    {
                        "resolved_model_call_id": event.resolved_model_call_id,
                        "target_fingerprint": event.target_fingerprint,
                        "outcome": event.outcome,
                        "usage_status": event.usage_status,
                        "usage": event.usage.model_dump(mode="json")
                        if event.usage is not None
                        else None,
                        "estimated_input_tokens": event.estimated_input_tokens,
                    }
                )
            continue
        if isinstance(event, TextBlockSegmentEvent):
            key = (event.reply_id, event.block_id)
            item = text_blocks.get(key)
            if item is None:
                item = _item("assistant_text", "Assistant text", event)
                text_blocks[key] = item
                items.append(item)
            _append_summary(item, event.text)
            _finish(item, event)
            continue
        if isinstance(event, ThinkingBlockSegmentEvent):
            key = (event.reply_id, event.block_id)
            item = thinking_blocks.get(key)
            if item is None:
                item = _item("assistant_thinking", "Assistant thinking", event)
                thinking_blocks[key] = item
                items.append(item)
            _append_summary(item, event.thinking)
            _finish(item, event)
            continue
        if isinstance(event, ToolCallStartEvent):
            item = _item(
                "tool_call",
                f"Tool call: {event.tool_call_name}",
                event,
                status="running",
                metadata={
                    "tool_call_id": event.tool_call_id,
                    "tool_name": event.tool_call_name,
                    "arguments": "",
                },
            )
            tool_calls[event.tool_call_id] = item
            items.append(item)
            continue
        if isinstance(event, ToolCallArgumentsSegmentEvent):
            item = tool_calls.get(event.tool_call_id)
            if item is None:
                item = _item(
                    "tool_call",
                    f"Tool call: {event.tool_call_id}",
                    event,
                    status="running",
                )
                tool_calls[event.tool_call_id] = item
                items.append(item)
            item.metadata["arguments"] = (
                str(item.metadata.get("arguments", ""))
                + event.arguments_json_fragment
            )
            _finish(item, event)
            continue
        if isinstance(event, ToolCallEndEvent):
            _finish(tool_calls.get(event.tool_call_id), event, status="completed")
            continue
        if isinstance(event, ToolResultStartEvent):
            item = _item(
                "tool_result",
                f"Tool result: {event.tool_call_name}",
                event,
                status="running",
                metadata={
                    "tool_call_id": event.tool_call_id,
                    "tool_name": event.tool_call_name,
                },
            )
            tool_results[event.tool_call_id] = item
            items.append(item)
            continue
        if isinstance(event, ToolResultTextDeltaEvent):
            item = tool_results.get(event.tool_call_id)
            if item is None:
                item = _item(
                    "tool_result",
                    f"Tool result: {event.tool_call_id}",
                    event,
                    status="running",
                )
                tool_results[event.tool_call_id] = item
                items.append(item)
            _append_summary(item, event.delta)
            _finish(item, event)
            continue
        if isinstance(event, ToolResultDataDeltaEvent):
            item = tool_results.get(event.tool_call_id)
            if item is None:
                item = _item(
                    "tool_result",
                    f"Tool result: {event.tool_call_id}",
                    event,
                    status="running",
                )
                tool_results[event.tool_call_id] = item
                items.append(item)
            item.metadata.setdefault("data_blocks", 0)
            item.metadata["data_blocks"] += 1
            _finish(item, event)
            continue
        if isinstance(event, ToolResultEndEvent):
            status = event.state.value
            _finish(tool_results.get(event.tool_call_id), event, status=status)
            continue
        if isinstance(event, RunEndEvent):
            terminal_status = _timeline_status_from_run_status(event.status)
            waiting_user = False
            if terminal_status == "failed":
                failed = True
            continue
        if isinstance(event, RequireUserConfirmEvent):
            waiting_user = True
            items.append(
                _item(
                    "permission_request",
                    "Permission request",
                    event,
                    status="waiting",
                    metadata={"tool_call_ids": [call.id for call in event.tool_calls]},
                )
            )
            continue
        if isinstance(event, UserConfirmResultEvent):
            waiting_user = False
            continue
        if isinstance(event, RunErrorEvent):
            failed = True
            items.append(
                _item("error", event.code, event, status="error", summary=event.message)
            )
            continue
        if isinstance(event, PlanModeEnteredEvent):
            items.append(
                _item(
                    "plan_mode",
                    "Plan mode entered",
                    event,
                    status="active",
                    summary=event.reason,
                    metadata={
                        "source": event.source,
                        "previous_permission_mode": event.previous_permission_mode,
                    },
                )
            )
            continue
        if isinstance(event, PlanQuestionAskedEvent):
            waiting_user = True
            item = _item(
                "plan_question",
                "Plan question",
                event,
                status="waiting",
                summary=event.question,
                metadata={
                    "question_id": event.question_id,
                    "tool_call_id": event.tool_call_id,
                    "options": [option.model_dump() for option in event.options],
                    "allow_free_text": event.allow_free_text,
                },
            )
            plan_questions[event.question_id] = item
            items.append(item)
            continue
        if isinstance(event, PlanQuestionAnsweredEvent):
            waiting_user = False
            item = plan_questions.get(event.question_id)
            _finish(item, event, status="answered")
            if item is not None:
                item.metadata["answer_text"] = event.answer_text
                item.metadata["selected_option"] = event.selected_option
            continue
        if isinstance(event, PlanExitRequestedEvent):
            waiting_user = True
            item = _item(
                "plan_exit_request",
                "Plan exit requested",
                event,
                status="waiting",
                summary=event.summary,
                metadata={
                    "exit_request_id": event.exit_request_id,
                    "tool_call_id": event.tool_call_id,
                    "plan_artifact_id": event.plan_artifact_id,
                },
            )
            plan_exits[event.exit_request_id] = item
            items.append(item)
            continue
        if isinstance(event, PlanExitResolvedEvent):
            waiting_user = False
            item = plan_exits.get(event.exit_request_id)
            _finish(item, event, status=event.decision)
            if item is not None:
                item.metadata["user_feedback"] = event.user_feedback
            continue
        if isinstance(event, PlanModeExitedEvent):
            items.append(
                _item(
                    "plan_mode",
                    "Plan mode exited",
                    event,
                    status="completed",
                    summary=event.accepted_plan_summary,
                    metadata={
                        "source": event.source,
                        "exit_request_id": event.exit_request_id,
                        "restored_permission_mode": event.restored_permission_mode,
                        "accepted_plan_artifact_id": event.accepted_plan_artifact_id,
                    },
                )
            )
            continue

    start_sequence = min(
        (event.sequence for event in ordered if event.sequence is not None),
        default=None,
    )
    end_sequence = max(
        (event.sequence for event in ordered if event.sequence is not None),
        default=None,
    )
    status = terminal_status or (
        "failed" if failed else "waiting_user" if waiting_user else "completed"
    )
    return RunTimeline(
        runtime_session_id=runtime_session_id,
        run_id=run_id,
        status=status,
        start_sequence=start_sequence,
        end_sequence=end_sequence,
        items=items,
    )


def _item(
    kind: TimelineItemKind,
    title: str,
    event: AgentEvent,
    *,
    status: str | None = None,
    summary: str = "",
    metadata: dict[str, Any] | None = None,
) -> RunTimelineItem:
    return RunTimelineItem(
        kind=kind,
        title=title,
        run_id=event.run_id,
        turn_id=event.turn_id,
        reply_id=event.reply_id,
        start_sequence=event.sequence,
        end_sequence=event.sequence,
        status=status,
        summary=summary,
        metadata=metadata or {},
    )


def _finish(
    item: RunTimelineItem | None, event: AgentEvent, *, status: str | None = None
) -> None:
    if item is None:
        return
    item.end_sequence = event.sequence
    if status is not None:
        item.status = status


def _append_summary(item: RunTimelineItem, text: str, *, limit: int = 500) -> None:
    if len(item.summary) >= limit:
        return
    item.summary = (item.summary + text)[:limit]


def _timeline_status_from_run_status(status: str) -> str:
    if status == "finished":
        return "completed"
    return status
