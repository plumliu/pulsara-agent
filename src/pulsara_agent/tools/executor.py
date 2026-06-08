"""Tool execution boundary that emits AgentEvent results."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.event import (
    EventContext,
    InMemoryEventLog,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.registry import ToolRegistry


@dataclass(slots=True)
class ToolExecutor:
    registry: ToolRegistry
    event_log: InMemoryEventLog | None = None

    def execute(self, call: ToolCall, *, event_context: EventContext) -> ToolExecutionResult:
        self._append(
            ToolResultStartEvent(
                **event_context.event_fields(),
                tool_call_id=call.id,
                tool_call_name=call.name,
            )
        )
        try:
            tool = self.registry.get(call.name)
            result = tool.execute(call)
        except Exception as exc:
            result = ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.ERROR,
                output=f"[TOOL_ERROR] {type(exc).__name__}: {exc}",
            )
        if result.output:
            self._append(
                ToolResultTextDeltaEvent(
                    **event_context.event_fields(),
                    tool_call_id=call.id,
                    delta=result.output,
                )
            )
        self._append(
            ToolResultEndEvent(
                **event_context.event_fields(),
                tool_call_id=call.id,
                state=result.status,
            )
        )
        return result

    def _append(self, event):
        if self.event_log is not None:
            self.event_log.append(event)
        return event
