"""Tool-loop helpers for AgentRuntime."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Callable

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    utc_now,
)
from pulsara_agent.memory.foundation.provenance import runtime_event_span_from_events
from pulsara_agent.message import Msg, ToolCallBlock, ToolResultBlock, ToolResultState
from pulsara_agent.message.assembler import completed_tool_result_from_events
from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.runtime.publisher import (
    RuntimeEventSubscriber,
    RuntimePublishedEvent,
)
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.runtime.terminal_projection import ToolResultEndCandidate
from pulsara_agent.primitives.tool_result import ToolResultExecutionSemanticsFact
from pulsara_agent.primitives.tool_result import ToolResultStateFact
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.capability.result_semantics import (
    build_unknown_result_semantics,
)
from pulsara_agent.tools import ToolCall, ToolExecutor
from pulsara_agent.tools.executor import synthetic_tool_observation_timing


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
    tool_observation_timing_seed: dict[str, Any] | None = None,
    semantics: ToolResultExecutionSemanticsFact | None = None,
    semantics_factory: Callable[
        [ToolObservationTimingFact], ToolResultExecutionSemanticsFact
    ]
    | None = None,
    existing_start: ToolResultStartEvent | None = None,
) -> list[AgentEvent | ToolResultEndCandidate]:
    if semantics is not None and semantics_factory is not None:
        raise ValueError("tool result semantics and factory are mutually exclusive")
    if existing_start is not None:
        if (
            existing_start.run_id != event_context.run_id
            or existing_start.tool_call_id != tool_call_id
            or existing_start.tool_call_name != tool_call_name
        ):
            raise ValueError("existing tool-result start identity mismatch")
        start = existing_start
    else:
        start = ToolResultStartEvent(
            id=f"tool_result_start:{event_context.run_id}:{tool_call_id}",
            **event_context.event_fields(),
            tool_call_id=tool_call_id,
            tool_call_name=tool_call_name,
        )
    text = ToolResultTextDeltaEvent(
        id=f"tool_result_text:{event_context.run_id}:{tool_call_id}",
        **event_context.event_fields(),
        tool_call_id=tool_call_id,
        delta=message,
    )
    end_created_at = utc_now()
    timing = _synthetic_timing_fact(
        start=start,
        end_created_at=end_created_at,
        tool_call_id=tool_call_id,
        tool_call_name=tool_call_name,
        seed=tool_observation_timing_seed,
    )
    actual_semantics = (
        semantics_factory(timing)
        if semantics_factory is not None
        else semantics
        or build_unknown_result_semantics(result_state=ToolResultStateFact(state.value))
    )
    end = ToolResultEndCandidate(
        id=f"tool_result_end:{event_context.run_id}:{tool_call_id}",
        run_id=event_context.run_id,
        turn_id=event_context.turn_id,
        reply_id=event_context.reply_id,
        created_at=end_created_at,
        metadata={},
        tool_call_id=tool_call_id,
        state=state,
        artifacts=(),
        observation_timing=timing,
        execution_semantics=actual_semantics,
    )
    return [*(() if existing_start is not None else (start,)), text, end]


def _synthetic_timing_fact(
    *,
    start: ToolResultStartEvent,
    end_created_at: str,
    tool_call_id: str,
    tool_call_name: str,
    seed: dict[str, Any] | None,
) -> ToolObservationTimingFact:
    if not seed:
        return synthetic_tool_observation_timing(
            start_event=start,
            end_created_at=end_created_at,
            call_id=tool_call_id,
            tool_name=tool_call_name,
            tool_origin="unknown",
        )
    source_started_at = str(seed.get("source_started_at") or start.created_at)
    return ToolObservationTimingFact(
        observed_at_utc=end_created_at,
        source_started_at_utc=source_started_at,
        source_ended_at_utc=end_created_at,
        observation_duration_seconds=_duration_seconds(
            source_started_at, end_created_at
        ),
        freshness="suspended_tool_observation",
        clock_source="mixed",
        tool_origin=str(seed.get("tool_origin") or "unknown"),  # type: ignore[arg-type]
        tool_name=tool_call_name,
        tool_call_id=tool_call_id,
        suspended_at_utc=str(seed.get("suspended_at"))
        if seed.get("suspended_at") is not None
        else None,
        resumed_at_utc=str(seed.get("resumed_at"))
        if seed.get("resumed_at") is not None
        else None,
    )


def _duration_seconds(start: str | None, end: str | None) -> float | None:
    start_dt = _parse_datetime(start)
    end_dt = _parse_datetime(end)
    if start_dt is None or end_dt is None:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_tool_call(block: ToolCallBlock) -> ToolCall:
    try:
        parsed = json.loads(block.input or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Malformed JSON arguments for tool {block.name}: {exc.msg}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Tool arguments for {block.name} must be a JSON object")
    return ToolCall(id=block.id, name=block.name, arguments=parsed)


def _tool_call_blocks(message: Msg) -> list[ToolCallBlock]:
    return [block for block in message.content if isinstance(block, ToolCallBlock)]


def _tool_batches(
    calls: list[ToolCall],
    executor: ToolExecutor,
    *,
    exposure: CapabilityExposurePlan | None = None,
) -> list[list[ToolCall]]:
    batches: list[list[ToolCall]] = []
    current_readonly: list[ToolCall] = []
    for call in calls:
        if _call_can_run_concurrently(call, executor, exposure=exposure):
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


def _call_can_run_concurrently(
    call: ToolCall,
    executor: ToolExecutor,
    *,
    exposure: CapabilityExposurePlan | None = None,
) -> bool:
    if exposure is not None:
        descriptor = exposure.descriptors_by_name.get(call.name)
        return bool(
            descriptor and descriptor.is_read_only and descriptor.is_concurrency_safe
        )
    try:
        tool = executor.registry.get(call.name)
    except KeyError:
        return False
    return bool(tool.is_read_only and tool.is_concurrency_safe)


def _remember_tool_result_event_span(
    state: LoopState, events: list[AgentEvent], tool_call_id: str
) -> None:
    try:
        span = runtime_event_span_from_events(
            events, tool_call_id, session_id=state.session_id
        )
    except KeyError:
        return
    spans = state.scratchpad.setdefault("tool_result_event_spans", {})
    spans[tool_call_id] = span


def _tool_result_from_event_slice(
    events: list[AgentEvent], tool_call_id: str
) -> ToolResultBlock:
    return completed_tool_result_from_events(events, tool_call_id)
