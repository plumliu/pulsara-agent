"""Tool-loop helpers for AgentRuntime."""

from __future__ import annotations

import asyncio
import json

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.memory.foundation.provenance import runtime_event_span_from_events
from pulsara_agent.message import Msg, ToolCallBlock, ToolResultBlock, ToolResultState
from pulsara_agent.message.assembler import completed_tool_result_from_events
from pulsara_agent.runtime.publisher import RuntimeEventSubscriber, RuntimePublishedEvent
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.tools import ToolCall, ToolExecutor


class _ToolBatchTap(RuntimeEventSubscriber):
    def __init__(self, tool_call_ids: set[str]) -> None:
        self._tool_call_ids = tool_call_ids
        self.queue: asyncio.Queue[AgentEvent] = asyncio.Queue()

    async def on_published_event(self, published: RuntimePublishedEvent) -> None:
        event = published.event
        tool_call_id = getattr(event, "tool_call_id", None)
        if tool_call_id not in self._tool_call_ids:
            return
        if isinstance(
            event,
            (
                ToolResultStartEvent,
                ToolResultTextDeltaEvent,
                ToolResultDataDeltaEvent,
                ToolResultEndEvent,
            ),
        ):
            await self.queue.put(event)


def build_tool_result_error_events(
    event_context: EventContext,
    *,
    tool_call_id: str,
    tool_call_name: str,
    message: str,
    state: ToolResultState = ToolResultState.ERROR,
) -> list[AgentEvent]:
    return [
        ToolResultStartEvent(
            **event_context.event_fields(),
            tool_call_id=tool_call_id,
            tool_call_name=tool_call_name,
        ),
        ToolResultTextDeltaEvent(
            **event_context.event_fields(),
            tool_call_id=tool_call_id,
            delta=message,
        ),
        ToolResultEndEvent(
            **event_context.event_fields(),
            tool_call_id=tool_call_id,
            state=state,
        ),
    ]


def _parse_tool_call(block: ToolCallBlock) -> ToolCall:
    try:
        parsed = json.loads(block.input or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON arguments for tool {block.name}: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Tool arguments for {block.name} must be a JSON object")
    return ToolCall(id=block.id, name=block.name, arguments=parsed)


def _tool_call_blocks(message: Msg) -> list[ToolCallBlock]:
    return [block for block in message.content if isinstance(block, ToolCallBlock)]


def _tool_batches(calls: list[ToolCall], executor: ToolExecutor) -> list[list[ToolCall]]:
    batches: list[list[ToolCall]] = []
    current_readonly: list[ToolCall] = []
    for call in calls:
        if _call_can_run_concurrently(call, executor):
            current_readonly.append(call)
            continue
        if current_readonly:
            batches.append(current_readonly)
            current_readonly = []
        batches.append([call])
    if current_readonly:
        batches.append(current_readonly)
    return batches


def _duplicate_tool_call_ids(calls: list[ToolCall]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for call in calls:
        if call.id in seen:
            duplicates.add(call.id)
            continue
        seen.add(call.id)
    return duplicates


def _call_can_run_concurrently(call: ToolCall, executor: ToolExecutor) -> bool:
    try:
        tool = executor.registry.get(call.name)
    except KeyError:
        return False
    return bool(tool.is_read_only and tool.is_concurrency_safe)


def _remember_tool_result_event_span(state: LoopState, events: list[AgentEvent], tool_call_id: str) -> None:
    try:
        span = runtime_event_span_from_events(events, tool_call_id, session_id=state.session_id)
    except KeyError:
        return
    spans = state.scratchpad.setdefault("tool_result_event_spans", {})
    spans[tool_call_id] = span


def _tool_result_from_event_slice(events: list[AgentEvent], tool_call_id: str) -> ToolResultBlock:
    return completed_tool_result_from_events(events, tool_call_id)
