"""Tool execution boundary that emits AgentEvent results."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    utc_now,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.capability.result_semantics import (
    build_adapter_failure_runtime_input,
    build_execution_semantics,
    build_default_tool_result_semantics_registry,
    build_unknown_result_semantics,
    default_essential_capture_policy,
    ToolResultSemanticsBuilderRegistry,
)
from pulsara_agent.primitives.context import CapabilityDescriptorRenderAttributionFact
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.primitives.tool_result import (
    ToolResultEssentialCapturePolicyFact,
    ToolResultStateFact,
)
from pulsara_agent.runtime.tool_artifacts import ToolResultArtifactService
from pulsara_agent.tools.base import (
    PreparedToolTerminalResult,
    ToolCall,
    ToolExecutionResult,
    ToolExecutionSuspended,
    ToolRuntimeContext,
)
from pulsara_agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from pulsara_agent.capability.descriptor import CapabilityDescriptor


def _cancelled_execution_result(
    call: ToolCall,
    *,
    descriptor: CapabilityDescriptor | None,
) -> ToolExecutionResult:
    result = ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=ToolResultState.INTERRUPTED,
        output="[TOOL_INTERRUPTED] tool execution cancelled",
    )
    if (
        descriptor is not None
        and descriptor.result_render_contract.semantics_builder_id
        != "tool-result-semantics:generic"
    ):
        # Terminal-family builders require a typed error carrier when
        # cancellation happens before the adapter can return its ordinary
        # executed-domain payload.
        return replace(
            result,
            status=ToolResultState.ERROR,
            semantics_input=build_adapter_failure_runtime_input(
                contract=descriptor.result_render_contract,
                call=call,
                error_text=result.output,
                state=ToolResultStateFact.ERROR,
            ),
        )
    return result


@dataclass(slots=True)
class ToolExecutor:
    registry: ToolRegistry
    record_event: Callable[[AgentEvent], AgentEvent] | None = None
    artifact_service: ToolResultArtifactService | None = None
    runtime_session_id: str | None = None
    essential_capture_policy: ToolResultEssentialCapturePolicyFact = (
        default_essential_capture_policy()
    )
    semantics_registry: ToolResultSemanticsBuilderRegistry = field(
        default_factory=build_default_tool_result_semantics_registry
    )

    def is_async(self, call: ToolCall) -> bool:
        try:
            tool = self.registry.get(call.name)
        except KeyError:
            return False
        return hasattr(tool, "execute_async")

    def execute(
        self,
        call: ToolCall,
        *,
        event_context: EventContext,
        descriptor: CapabilityDescriptor | None = None,
        descriptor_attribution: CapabilityDescriptorRenderAttributionFact | None = None,
        context_id: str | None = None,
        model_call_index: int | None = None,
        permission_snapshot_id: str | None = None,
        permission_mode: str | None = None,
        permission_policy: dict | None = None,
    ) -> ToolExecutionResult | ToolExecutionSuspended:
        start_event = self._append(
            ToolResultStartEvent(
                **event_context.event_fields(),
                tool_call_id=call.id,
                tool_call_name=call.name,
            )
        )
        try:
            tool = self.registry.get(call.name)
            runtime_context = (
                ToolRuntimeContext(
                    runtime_session_id=self.runtime_session_id,
                    event_context=event_context,
                    context_id=context_id,
                    model_call_index=model_call_index,
                    permission_snapshot_id=permission_snapshot_id,
                    permission_mode=permission_mode,
                    permission_policy=permission_policy,
                )
                if self.runtime_session_id is not None
                else None
            )
            if hasattr(tool, "execute_streaming_with_context"):
                result = tool.execute_streaming_with_context(
                    call,
                    self._tool_delta_emitter(event_context, call.id),
                    event_context=event_context,
                    record_event=self.record_event,
                    runtime_context=runtime_context,
                )
            elif hasattr(tool, "execute_with_context"):
                result = tool.execute_with_context(
                    call,
                    event_context=event_context,
                    record_event=self.record_event,
                    runtime_context=runtime_context,
                )
            elif hasattr(tool, "execute_streaming"):
                result = tool.execute_streaming(
                    call, self._tool_delta_emitter(event_context, call.id)
                )
            else:
                result = tool.execute(call)
        except asyncio.CancelledError:
            result = _cancelled_execution_result(call, descriptor=descriptor)
        except Exception as exc:
            result = ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.ERROR,
                output=f"[TOOL_ERROR] {type(exc).__name__}: {exc}",
            )
            if descriptor is not None:
                result = replace(
                    result,
                    semantics_input=build_adapter_failure_runtime_input(
                        contract=descriptor.result_render_contract,
                        call=call,
                        error_text=result.output,
                        state=ToolResultStateFact.ERROR,
                    ),
                )
        if isinstance(result, ToolExecutionSuspended):
            return _with_tool_observation_timing_seed(
                result,
                start_event=start_event,
                descriptor=descriptor,
                context_id=context_id,
                model_call_index=model_call_index,
            )
        return self._finalize_result(
            call,
            event_context=event_context,
            result=result,
            descriptor=descriptor,
            descriptor_attribution=descriptor_attribution,
            start_event=start_event,
        )

    async def execute_async(
        self,
        call: ToolCall,
        *,
        event_context: EventContext,
        descriptor: CapabilityDescriptor | None = None,
        descriptor_attribution: CapabilityDescriptorRenderAttributionFact | None = None,
        context_id: str | None = None,
        model_call_index: int | None = None,
        permission_snapshot_id: str | None = None,
        permission_mode: str | None = None,
        permission_policy: dict | None = None,
    ) -> ToolExecutionResult | ToolExecutionSuspended:
        start_event = self._append(
            ToolResultStartEvent(
                **event_context.event_fields(),
                tool_call_id=call.id,
                tool_call_name=call.name,
            )
        )
        try:
            tool = self.registry.get(call.name)
            execute_async = getattr(tool, "execute_async", None)
            if execute_async is None:
                raise TypeError(f"Tool {call.name!r} does not implement execute_async")
            if self.runtime_session_id is None:
                raise RuntimeError("Async tool execution requires runtime_session_id")
            result = await execute_async(
                call,
                runtime_context=ToolRuntimeContext(
                    runtime_session_id=self.runtime_session_id,
                    event_context=event_context,
                    context_id=context_id,
                    model_call_index=model_call_index,
                    permission_snapshot_id=permission_snapshot_id,
                    permission_mode=permission_mode,
                    permission_policy=permission_policy,
                ),
            )
            if isinstance(result, ToolExecutionSuspended):
                return _with_tool_observation_timing_seed(
                    result,
                    start_event=start_event,
                    descriptor=descriptor,
                    context_id=context_id,
                    model_call_index=model_call_index,
                )
        except asyncio.CancelledError:
            result = _cancelled_execution_result(call, descriptor=descriptor)
        except Exception as exc:
            result = ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.ERROR,
                output=f"[TOOL_ERROR] {type(exc).__name__}: {exc}",
            )
            if descriptor is not None:
                result = replace(
                    result,
                    semantics_input=build_adapter_failure_runtime_input(
                        contract=descriptor.result_render_contract,
                        call=call,
                        error_text=result.output,
                        state=ToolResultStateFact.ERROR,
                    ),
                )
        return self._finalize_result(
            call,
            event_context=event_context,
            result=result,
            descriptor=descriptor,
            descriptor_attribution=descriptor_attribution,
            start_event=start_event,
        )

    def _finalize_result(
        self,
        call: ToolCall,
        *,
        event_context: EventContext,
        result: ToolExecutionResult,
        descriptor: CapabilityDescriptor | None = None,
        descriptor_attribution: CapabilityDescriptorRenderAttributionFact | None = None,
        start_event: AgentEvent,
    ) -> ToolExecutionResult:
        artifact_refs = ()
        if self.artifact_service is not None:
            result, artifact_refs = self.artifact_service.process_result(
                result,
                event_context=event_context,
                tool_call=call,
                descriptor=descriptor,
            )
        if result.output and not result.metadata.get("streamed_output_complete"):
            self._append(
                ToolResultTextDeltaEvent(
                    **event_context.event_fields(),
                    tool_call_id=call.id,
                    delta=result.output,
                )
            )
        end_created_at = utc_now()
        timing = build_tool_observation_timing(
            start_event=start_event,
            end_created_at=end_created_at,
            call_id=call.id,
            tool_name=call.name,
            result_metadata=result.metadata,
            descriptor=descriptor,
        )
        timing_payload = timing.to_message_projection_payload()
        semantics = None
        if descriptor is not None or descriptor_attribution is not None:
            if descriptor is None or descriptor_attribution is None:
                raise ValueError(
                    "tool result semantics requires descriptor and attribution together"
                )
            semantics = build_execution_semantics(
                descriptor=descriptor,
                descriptor_attribution=descriptor_attribution,
                call=call,
                result=result,
                observation_timing=timing,
                capture_policy=self.essential_capture_policy,
                registry=self.semantics_registry,
            )
        else:
            semantics = build_unknown_result_semantics(
                result_state=ToolResultStateFact(result.status.value)
            )
        result = replace(
            result,
            metadata={
                **result.metadata,
                "tool_observation_timing": timing_payload,
            },
            semantics=semantics,
        )
        prepared_terminal = PreparedToolTerminalResult(
            tool_call_id=call.id,
            state=result.status,
            created_at=end_created_at,
            artifacts=tuple(artifact_refs),
            observation_timing=timing,
            semantics=semantics,
        )
        return replace(result, prepared_terminal_result=prepared_terminal)

    def _append(self, event):
        if self.record_event is not None:
            return self.record_event(event)
        return event

    def _tool_delta_emitter(
        self, event_context: EventContext, tool_call_id: str
    ) -> Callable[[str], None]:
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


def build_tool_observation_timing(
    *,
    start_event: AgentEvent,
    end_event: AgentEvent | None = None,
    end_created_at: str | None = None,
    call_id: str,
    tool_name: str,
    result_metadata: dict | None = None,
    descriptor: CapabilityDescriptor | None = None,
) -> ToolObservationTimingFact:
    metadata = result_metadata or {}
    observed_at = end_event.created_at if end_event is not None else end_created_at
    if observed_at is None:
        raise ValueError(
            "build_tool_observation_timing requires end_event or end_created_at"
        )
    return ToolObservationTimingFact(
        observed_at_utc=observed_at,
        source_started_at_utc=start_event.created_at,
        source_ended_at_utc=observed_at,
        observation_duration_seconds=_duration_seconds(
            start_event.created_at, observed_at
        ),
        tool_reported_duration_seconds=_trusted_tool_reported_duration_seconds(
            tool_name, metadata
        ),
        freshness=_tool_observation_freshness(tool_name, metadata),
        clock_source="tool_result_events",
        tool_origin=_tool_origin_from_descriptor(descriptor),
        tool_name=tool_name,
        tool_call_id=call_id,
    )


def synthetic_tool_observation_timing(
    *,
    start_event: AgentEvent,
    end_event: AgentEvent | None = None,
    end_created_at: str | None = None,
    call_id: str,
    tool_name: str,
    tool_origin: str = "unknown",
) -> ToolObservationTimingFact:
    observed_at = end_event.created_at if end_event is not None else end_created_at
    if observed_at is None:
        raise ValueError(
            "synthetic_tool_observation_timing requires end_event or end_created_at"
        )
    return ToolObservationTimingFact(
        observed_at_utc=observed_at,
        source_started_at_utc=start_event.created_at,
        source_ended_at_utc=observed_at,
        observation_duration_seconds=_duration_seconds(
            start_event.created_at, observed_at
        ),
        freshness="current_tool_observation",
        clock_source="tool_result_events",
        tool_origin=tool_origin,  # type: ignore[arg-type]
        tool_name=tool_name,
        tool_call_id=call_id,
    )


def _with_tool_observation_timing_seed(
    suspended: ToolExecutionSuspended,
    *,
    start_event: AgentEvent,
    descriptor: CapabilityDescriptor | None,
    context_id: str | None,
    model_call_index: int | None,
) -> ToolExecutionSuspended:
    payload = dict(suspended.payload)
    seed = dict(payload.get("tool_observation_timing_seed") or {})
    seed.update(
        {
            "tool_call_id": suspended.tool_call_id,
            "tool_name": suspended.tool_name,
            "tool_origin": _tool_origin_from_descriptor(descriptor),
            "source_started_at": start_event.created_at,
            "suspended_at": seed.get("suspended_at") or utc_now(),
            "start_event_id": start_event.id,
            "start_event_sequence": start_event.sequence,
            "source_context_id": context_id,
            "source_model_call_index": model_call_index,
        }
    )
    payload["tool_observation_timing_seed"] = seed
    return replace(suspended, payload=payload)


def _tool_origin_from_descriptor(descriptor: CapabilityDescriptor | None) -> str:
    if descriptor is None:
        return "unknown"
    if descriptor.permission_category == "terminal":
        return "terminal"
    provider_kind = getattr(
        descriptor.provider_kind, "value", str(descriptor.provider_kind)
    )
    if provider_kind == "mcp":
        return "mcp"
    if provider_kind == "workflow":
        if descriptor.permission_category == "subagent_runtime":
            return "subagent_system"
        return "workflow"
    if provider_kind in {"builtin", "memory", "skill"}:
        return "builtin"
    return "custom"


def _tool_observation_freshness(tool_name: str, metadata: dict) -> str:
    if tool_name == "terminal_process":
        return "background_process_observation"
    if tool_name == "terminal" and metadata.get("process_id"):
        return "background_process_observation"
    return "current_tool_observation"


def _trusted_tool_reported_duration_seconds(
    tool_name: str, metadata: dict
) -> float | None:
    if tool_name not in {"terminal", "terminal_process"}:
        return None
    timing = metadata.get("timing")
    if isinstance(timing, dict):
        return _float_or_none(timing.get("duration_seconds"))
    return _float_or_none(metadata.get("duration_seconds"))


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _duration_seconds(start: str | None, end: str | None) -> float | None:
    start_dt = _parse_datetime(start)
    end_dt = _parse_datetime(end)
    if start_dt is None or end_dt is None:
        return None
    duration = (end_dt - start_dt).total_seconds()
    return max(0.0, duration)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
