"""Model-facing terminal monitor parser and renderer."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError

from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.terminal import (
    TerminalMonitorCancelledOutcome,
    TerminalMonitorInventoryOutcome,
    TerminalMonitorPort,
    TerminalMonitorRegisteredOutcome,
    TerminalMonitorRejectedOutcome,
    build_terminal_port_invocation_owner,
    parse_terminal_monitor_input,
)
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolExecutionResult,
    ToolRuntimeContext,
)
from pulsara_agent.ports.tool_result_semantics import (
    FrozenToolResultSemanticsRuntimeInput,
    unbounded_error_preview,
)
from pulsara_agent.primitives.tool_result import (
    TerminalMonitorCancellationDomainSubmissionFact,
    TerminalMonitorErrorDomainSubmissionFact,
    TerminalMonitorInventoryDomainSubmissionFact,
    TerminalMonitorRegistrationDomainSubmissionFact,
    TerminalMonitorSummaryFact,
    ToolResultRenderVariantCode,
)
from pulsara_agent.tools.builtins.terminal import (
    freeze_tool_display_payload,
    terminal_artifact_candidates,
    terminal_payload_timing_fact,
    terminal_result_metadata,
    terminal_result_payload,
    terminal_timing_payload,
)
from pulsara_agent.tools.builtins.workspace import WorkspaceTool


@dataclass(slots=True)
class TerminalMonitorTool(WorkspaceTool):
    monitor_port: TerminalMonitorPort
    name: str = "terminal_monitor"

    def execute(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> ToolExecutionResult:
        try:
            request = parse_terminal_monitor_input(call.arguments)
        except ValidationError as exc:
            return self._error_result(
                call,
                requested_action=_requested_action(call.arguments),
                process_id=_optional_string(call.arguments, "process_id"),
                monitor_id=_optional_string(call.arguments, "monitor_id"),
                error=_validation_error_text(exc),
                status="malformed_arguments",
                policy_code="terminal_monitor_malformed_arguments",
            )
        if runtime_context is None:
            return self._error_result(
                call,
                requested_action=request.action,
                process_id=getattr(request, "process_id", None),
                monitor_id=getattr(request, "monitor_id", None),
                error="terminal_monitor requires typed runtime invocation authority",
                status="blocked",
                policy_code="terminal_monitor_owner_unavailable",
            )
        owner = build_terminal_port_invocation_owner(
            runtime_session_id=runtime_context.runtime_session_id,
            tool_call_id=call.id,
            tool_name="terminal_monitor",
            event_context=runtime_context.event_context,
            owner_kind=runtime_context.owner_kind,
            permission=runtime_context.permission,
        )
        outcome = self.monitor_port.execute(request=request, owner=owner)
        if isinstance(outcome, TerminalMonitorRejectedOutcome):
            return self._error_result(
                call,
                requested_action=outcome.requested_action,
                process_id=outcome.process_id,
                monitor_id=outcome.monitor_id,
                error=outcome.sanitized_message,
                status=outcome.status,
                policy_code=outcome.reject_code.value,
            )
        if isinstance(outcome, TerminalMonitorRegisteredOutcome):
            return self._registration_result(call, outcome)
        if isinstance(outcome, TerminalMonitorInventoryOutcome):
            return self._inventory_result(call, outcome)
        if isinstance(outcome, TerminalMonitorCancelledOutcome):
            return self._cancellation_result(call, outcome)
        raise AssertionError(type(outcome))

    def execute_with_context(
        self,
        call: ToolCall,
        *,
        event_context=None,
        record_event=None,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> ToolExecutionResult:
        del event_context, record_event
        return self.execute(call, runtime_context=runtime_context)

    def _registration_result(
        self,
        call: ToolCall,
        outcome: TerminalMonitorRegisteredOutcome,
    ) -> ToolExecutionResult:
        prepared = outcome.prepared_registration
        result = prepared.initial_observation_result
        result_metadata = terminal_result_metadata(result)
        process_id = prepared.registration_semantic.initial_baseline_cursor.stream_identity.process_id
        timing = terminal_timing_payload(
            duration_seconds=result_metadata.get("duration_seconds"),
            freshness="background_process_observation",
        )
        terminal_session_id = str(
            result_metadata.get("terminal_session_id") or "default"
        )
        backend_type = str(result_metadata.get("backend_type") or "local")
        payload = terminal_result_payload(
            result,
            terminal_session_id=terminal_session_id,
            backend_type=backend_type,
            timing=timing,
        )
        payload.update(
            {
                "terminal_monitor_action": "register",
                "monitor_id": prepared.registration_semantic.monitor_id,
                "monitor_status": "registered",
                "expires_at_utc": prepared.registration_attribution.expires_at_utc,
            }
        )
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json.dumps(payload, ensure_ascii=False),
            display_payload=freeze_tool_display_payload(payload),
            metadata={
                "terminal_monitor_action": "register",
                "process_id": process_id,
                "monitor_id": prepared.registration_semantic.monitor_id,
                "monitor_status": "registered",
                "expires_at_utc": prepared.registration_attribution.expires_at_utc,
                "terminal_session_id": terminal_session_id,
                "backend_type": backend_type,
                "timing": timing,
            },
            artifact_candidates=terminal_artifact_candidates(result, timing=timing),
            semantics_input=FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=(
                    ToolResultRenderVariantCode.TERMINAL_MONITOR_REGISTRATION
                ),
                domain_submission=TerminalMonitorRegistrationDomainSubmissionFact(
                    process_id=process_id,
                    monitor_id=prepared.registration_semantic.monitor_id,
                    expires_at_utc=prepared.registration_attribution.expires_at_utc,
                    status=result.status.value,
                    exit_code=result.exit_code,
                    output_truncated=result.truncated,
                    terminal_session_id=terminal_session_id,
                    backend_type=backend_type,
                ),
            ),
            terminal_payload_timing=terminal_payload_timing_fact(timing),
            prepared_terminal_monitor_registration=prepared,
            prepared_terminal_notification_reservation=(
                prepared.notification_reservation
            ),
        )

    def _inventory_result(
        self,
        call: ToolCall,
        outcome: TerminalMonitorInventoryOutcome,
    ) -> ToolExecutionResult:
        summaries = tuple(
            TerminalMonitorSummaryFact(
                monitor_id=item.monitor_id,
                process_id=item.process_id,
                lifecycle_state=item.lifecycle_state,
                observation_ordinal=item.observation_ordinal,
                has_pending_observation=item.has_pending_observation,
            )
            for item in outcome.items
        )
        payload = {
            "status": "success",
            "terminal_monitor_action": "list",
            "monitors": [
                {
                    "monitor_id": item.monitor_id,
                    "process_id": item.process_id,
                    "lifecycle_state": item.lifecycle_state,
                    "observation_ordinal": item.observation_ordinal,
                    "has_pending_observation": item.has_pending_observation,
                }
                for item in summaries
            ],
            "omitted_monitor_count": outcome.omitted_monitor_count,
            "summaries_truncated": outcome.omitted_monitor_count > 0,
        }
        timing = terminal_timing_payload(freshness="background_process_observation")
        payload["timing"] = timing
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json.dumps(payload, ensure_ascii=False),
            display_payload=freeze_tool_display_payload(payload),
            metadata={"terminal_monitor_action": "list", "timing": timing},
            semantics_input=FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_MONITOR_INVENTORY,
                domain_submission=TerminalMonitorInventoryDomainSubmissionFact(
                    status="success",
                    monitor_summaries=summaries,
                    omitted_monitor_count=outcome.omitted_monitor_count,
                    summaries_truncated=outcome.omitted_monitor_count > 0,
                ),
            ),
            terminal_payload_timing=terminal_payload_timing_fact(timing),
        )

    def _cancellation_result(
        self,
        call: ToolCall,
        outcome: TerminalMonitorCancelledOutcome,
    ) -> ToolExecutionResult:
        cancellation = outcome.prepared_cancellation
        timing = terminal_timing_payload(freshness="background_process_observation")
        payload = {
            "status": "success",
            "terminal_monitor_action": "cancel",
            "monitor_id": cancellation.monitor_id,
            "monitor_status": cancellation.outcome,
            "timing": timing,
        }
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json.dumps(payload, ensure_ascii=False),
            display_payload=freeze_tool_display_payload(payload),
            metadata=payload,
            semantics_input=FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=(
                    ToolResultRenderVariantCode.TERMINAL_MONITOR_CANCELLATION
                ),
                domain_submission=TerminalMonitorCancellationDomainSubmissionFact(
                    monitor_id=cancellation.monitor_id,
                    outcome=cancellation.outcome,
                ),
            ),
            terminal_payload_timing=terminal_payload_timing_fact(timing),
            prepared_terminal_monitor_cancellation=cancellation,
        )

    def _error_result(
        self,
        call: ToolCall,
        *,
        requested_action: str,
        process_id: str | None,
        monitor_id: str | None,
        error: str,
        status: str,
        policy_code: str | None = None,
    ) -> ToolExecutionResult:
        action = (
            requested_action
            if requested_action in {"register", "list", "cancel"}
            else "unknown"
        )
        if action == "register":
            monitor_id = None
        elif action == "list":
            process_id = None
            monitor_id = None
        elif action == "cancel":
            process_id = None
        else:
            process_id = None
            monitor_id = None
        payload = {
            "status": status,
            "terminal_monitor_action": action,
            "process_id": process_id,
            "monitor_id": monitor_id,
            "error": error,
            "policy_code": policy_code,
        }
        return self._result(
            call,
            status=ToolResultState.ERROR,
            output=json.dumps(payload, ensure_ascii=False),
            display_payload=freeze_tool_display_payload(payload),
            metadata=payload,
            semantics_input=FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_MONITOR_ERROR,
                domain_submission=TerminalMonitorErrorDomainSubmissionFact(
                    requested_action=action,
                    process_id=process_id,
                    monitor_id=monitor_id,
                    status=status,
                    error=unbounded_error_preview(error),
                    policy_code=policy_code,
                ),
            ),
            terminal_payload_timing=None,
        )


def _requested_action(arguments: object) -> str:
    if not isinstance(arguments, dict):
        return "unknown"
    action = arguments.get("action")
    return action if isinstance(action, str) and action else "unknown"


def _optional_string(arguments: object, key: str) -> str | None:
    if not isinstance(arguments, dict):
        return None
    value = arguments.get(key)
    return value if isinstance(value, str) and value else None


def _validation_error_text(error: ValidationError) -> str:
    first = error.errors(include_url=False)[0]
    location = ".".join(str(item) for item in first.get("loc", ()))
    message = str(first.get("msg") or "invalid arguments")
    return f"{location}: {message}" if location else message


__all__ = ["TerminalMonitorTool"]
