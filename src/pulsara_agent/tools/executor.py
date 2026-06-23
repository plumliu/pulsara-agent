"""Tool execution boundary that emits AgentEvent results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime.tool_artifacts import ToolResultArtifactService
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.registry import ToolRegistry


@dataclass(slots=True)
class ToolExecutor:
    registry: ToolRegistry
    record_event: Callable[[AgentEvent], AgentEvent] | None = None
    artifact_service: ToolResultArtifactService | None = None

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
            if hasattr(tool, "execute_streaming_with_context"):
                result = tool.execute_streaming_with_context(
                    call,
                    self._tool_delta_emitter(event_context, call.id),
                    event_context=event_context,
                    record_event=self.record_event,
                )
            elif hasattr(tool, "execute_with_context"):
                result = tool.execute_with_context(
                    call,
                    event_context=event_context,
                    record_event=self.record_event,
                )
            elif hasattr(tool, "execute_streaming"):
                result = tool.execute_streaming(call, self._tool_delta_emitter(event_context, call.id))
            else:
                result = tool.execute(call)
        except Exception as exc:
            result = ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.ERROR,
                output=f"[TOOL_ERROR] {type(exc).__name__}: {exc}",
            )
        artifact_refs = ()
        if self.artifact_service is not None:
            result, artifact_refs = self.artifact_service.process_result(
                result,
                event_context=event_context,
                tool_call=call,
            )
        if result.output and not result.metadata.get("streamed_output_complete"):
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
                artifacts=list(artifact_refs),
            )
        )
        return result

    def _append(self, event):
        if self.record_event is not None:
            return self.record_event(event)
        return event

    def _tool_delta_emitter(self, event_context: EventContext, tool_call_id: str) -> Callable[[str], None]:
        def emit(delta: str) -> None:
            if not delta:
                return
            # Streaming terminal readers call this from worker threads; keep all event recording behind
            # RuntimeSession.make_thread_recorder so append/publish ordering stays owned by RuntimeSession.
            self._append(
                ToolResultTextDeltaEvent(
                    **event_context.event_fields(),
                    tool_call_id=tool_call_id,
                    delta=delta,
                )
            )

        return emit
