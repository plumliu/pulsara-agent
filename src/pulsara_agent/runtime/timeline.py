"""Business-level run timeline assembled from runtime events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from pulsara_agent.event import (
    AgentEvent,
    ExceedMaxItersEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    RunErrorEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)

TimelineItemKind = Literal[
    "reply",
    "model_call",
    "assistant_text",
    "assistant_thinking",
    "tool_call",
    "tool_result",
    "permission_request",
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
            items=[RunTimelineItem.from_dict(item) for item in payload.get("items", [])],
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
    failed = False
    terminal_status: str | None = None

    for event in ordered:
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
                f"Model call: {event.model_name}",
                event,
                status="running",
                metadata={
                    "model_name": event.model_name,
                    "model_role": event.model_role,
                    "provider": event.provider,
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
                        "input_tokens": event.input_tokens,
                        "output_tokens": event.output_tokens,
                        "total_tokens": event.total_tokens,
                    }
                )
            continue
        if isinstance(event, TextBlockDeltaEvent):
            key = (event.reply_id, event.block_id)
            item = text_blocks.get(key)
            if item is None:
                item = _item("assistant_text", "Assistant text", event)
                text_blocks[key] = item
                items.append(item)
            _append_summary(item, event.delta)
            _finish(item, event)
            continue
        if isinstance(event, ThinkingBlockDeltaEvent):
            key = (event.reply_id, event.block_id)
            item = thinking_blocks.get(key)
            if item is None:
                item = _item("assistant_thinking", "Assistant thinking", event)
                thinking_blocks[key] = item
                items.append(item)
            _append_summary(item, event.delta)
            _finish(item, event)
            continue
        if isinstance(event, ToolCallStartEvent):
            item = _item(
                "tool_call",
                f"Tool call: {event.tool_call_name}",
                event,
                status="running",
                metadata={"tool_call_id": event.tool_call_id, "tool_name": event.tool_call_name, "arguments": ""},
            )
            tool_calls[event.tool_call_id] = item
            items.append(item)
            continue
        if isinstance(event, ToolCallDeltaEvent):
            item = tool_calls.get(event.tool_call_id)
            if item is None:
                item = _item("tool_call", f"Tool call: {event.tool_call_id}", event, status="running")
                tool_calls[event.tool_call_id] = item
                items.append(item)
            item.metadata["arguments"] = str(item.metadata.get("arguments", "")) + event.delta
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
                metadata={"tool_call_id": event.tool_call_id, "tool_name": event.tool_call_name},
            )
            tool_results[event.tool_call_id] = item
            items.append(item)
            continue
        if isinstance(event, ToolResultTextDeltaEvent):
            item = tool_results.get(event.tool_call_id)
            if item is None:
                item = _item("tool_result", f"Tool result: {event.tool_call_id}", event, status="running")
                tool_results[event.tool_call_id] = item
                items.append(item)
            _append_summary(item, event.delta)
            _finish(item, event)
            continue
        if isinstance(event, ToolResultDataDeltaEvent):
            item = tool_results.get(event.tool_call_id)
            if item is None:
                item = _item("tool_result", f"Tool result: {event.tool_call_id}", event, status="running")
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
            if terminal_status == "failed":
                failed = True
            continue
        if isinstance(event, RequireUserConfirmEvent):
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
        if isinstance(event, RunErrorEvent):
            failed = True
            items.append(_item("error", event.code, event, status="error", summary=event.message))
            continue
        if isinstance(event, ExceedMaxItersEvent):
            failed = True
            items.append(_item("error", event.name, event, status="error", summary=f"Exceeded max turns: {event.max_iters}"))
            continue

    start_sequence = min((event.sequence for event in ordered if event.sequence is not None), default=None)
    end_sequence = max((event.sequence for event in ordered if event.sequence is not None), default=None)
    status = terminal_status or ("failed" if failed else "completed")
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


def _finish(item: RunTimelineItem | None, event: AgentEvent, *, status: str | None = None) -> None:
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
