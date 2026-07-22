"""Host-owned terminal monitor registration, inventory, and cancellation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import ValidationError

from pulsara_agent.capability.result_semantics import (
    FrozenToolResultSemanticsRuntimeInput,
    unbounded_error_preview,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.primitives.tool_result import (
    TerminalMonitorCancellationDomainSubmissionFact,
    TerminalMonitorErrorDomainSubmissionFact,
    TerminalMonitorInventoryDomainSubmissionFact,
    TerminalMonitorRegistrationDomainSubmissionFact,
    TerminalMonitorSummaryFact,
    ToolResultRenderVariantCode,
)
from pulsara_agent.runtime.permission import PermissionState, TerminalAccess
from pulsara_agent.runtime.terminal import TerminalSessionManager
from pulsara_agent.runtime.terminal.notification import (
    TerminalNotificationCapacityError,
)
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult, ToolRuntimeContext
from pulsara_agent.tools.builtins.terminal import (
    freeze_tool_display_payload,
    terminal_artifact_candidates,
    terminal_payload_timing_fact,
    terminal_result_payload,
    terminal_timing_payload,
)
from pulsara_agent.terminal_public_api import (
    TERMINAL_MONITOR_TOOL_DESCRIPTION,
    TerminalMonitorCancelInput,
    TerminalMonitorListInput,
    TerminalMonitorRegisterInput,
    parse_terminal_monitor_input,
    resolve_terminal_monitor_public_policy,
    terminal_monitor_input_schema,
)
from pulsara_agent.tools.builtins.workspace import WorkspaceTool

if TYPE_CHECKING:
    from pulsara_agent.runtime.terminal.monitor import TerminalMonitorCoordinator


@dataclass(slots=True)
class TerminalMonitorTool(WorkspaceTool):
    _SUPPORTED_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"register", "list", "cancel"}
    )
    terminal_sessions: TerminalSessionManager | None = None
    owner_host_session_id: str | None = None
    permission_state: PermissionState | None = None
    terminal_monitor_coordinator: "TerminalMonitorCoordinator | None" = None
    name: str = "terminal_monitor"
    description: str = TERMINAL_MONITOR_TOOL_DESCRIPTION
    parameters: dict[str, Any] = field(default_factory=terminal_monitor_input_schema)
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __post_init__(self) -> None:
        WorkspaceTool.__post_init__(self)
        if self.terminal_sessions is None:
            self.terminal_sessions = TerminalSessionManager(self.workspace_root)

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
        if _terminal_access_off(
            runtime_context=runtime_context,
            permission_state=self.permission_state,
        ):
            return self._error_result(
                call,
                requested_action=request.action,
                process_id=(
                    request.process_id
                    if isinstance(request, TerminalMonitorRegisterInput)
                    else None
                ),
                monitor_id=(
                    request.monitor_id
                    if isinstance(request, TerminalMonitorCancelInput)
                    else None
                ),
                error="terminal_monitor is disabled by permission policy",
                status="blocked",
                policy_code="terminal_access_off",
            )
        try:
            if isinstance(request, TerminalMonitorRegisterInput):
                return self._registration_result(
                    call,
                    request=request,
                    runtime_context=runtime_context,
                )
            if isinstance(request, TerminalMonitorListInput):
                return self._inventory_result(call)
            if isinstance(request, TerminalMonitorCancelInput):
                return self._cancellation_result(
                    call,
                    request=request,
                    runtime_context=runtime_context,
                )
            raise AssertionError(type(request))
        except TerminalNotificationCapacityError as exc:
            return self._error_result(
                call,
                requested_action=request.action,
                process_id=(
                    request.process_id
                    if isinstance(request, TerminalMonitorRegisterInput)
                    else None
                ),
                monitor_id=(
                    request.monitor_id
                    if isinstance(request, TerminalMonitorCancelInput)
                    else None
                ),
                error=str(exc),
                status="blocked",
                policy_code=exc.reason_code,
            )
        except (KeyError, ValueError) as exc:
            return self._error_result(
                call,
                requested_action=request.action,
                process_id=(
                    request.process_id
                    if isinstance(request, TerminalMonitorRegisterInput)
                    else None
                ),
                monitor_id=(
                    request.monitor_id
                    if isinstance(request, TerminalMonitorCancelInput)
                    else None
                ),
                error=str(exc),
                status="blocked",
            )

    def execute_with_context(
        self,
        call: ToolCall,
        *,
        event_context=None,
        record_event=None,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> ToolExecutionResult:
        return self.execute(call, runtime_context=runtime_context)

    def _registration_result(
        self,
        call: ToolCall,
        *,
        request: TerminalMonitorRegisterInput,
        runtime_context: ToolRuntimeContext | None,
    ) -> ToolExecutionResult:
        if self.terminal_monitor_coordinator is None or runtime_context is None:
            return self._error_result(
                call,
                requested_action="register",
                process_id=request.process_id,
                monitor_id=None,
                error="terminal monitor requires a Host runtime owner",
                status="blocked",
                policy_code="terminal_monitor_owner_unavailable",
            )
        resolved_policy = resolve_terminal_monitor_public_policy(request)
        prepared = self.terminal_monitor_coordinator.prepare_registration(
            process_id=request.process_id,
            origin_tool_call_id=call.id,
            runtime_context=runtime_context,
            conditions=resolved_policy.conditions,
            delivery=resolved_policy.delivery,
            lifetime=resolved_policy.lifetime,
        )
        try:
            result = prepared.initial_observation_result
            timing = terminal_timing_payload(
                duration_seconds=result.metadata.get("duration_seconds"),
                freshness="background_process_observation",
            )
            payload = terminal_result_payload(
                result,
                terminal_session_id=result.metadata.get(
                    "terminal_session_id", "default"
                ),
                backend_type=result.metadata.get("backend_type", "local"),
                timing=timing,
            )
            payload.update(
                {
                    "terminal_monitor_action": "register",
                    "monitor_id": prepared.registration_semantic.monitor_id,
                    "monitor_status": "registered",
                    "expires_at_utc": (
                        prepared.registration_attribution.expires_at_utc
                    ),
                }
            )
            return self._result(
                call,
                status=ToolResultState.SUCCESS,
                output=json.dumps(payload, ensure_ascii=False),
                display_payload=freeze_tool_display_payload(payload),
                metadata={
                    "terminal_monitor_action": "register",
                    "process_id": request.process_id,
                    "monitor_id": prepared.registration_semantic.monitor_id,
                    "monitor_status": "registered",
                    "expires_at_utc": (
                        prepared.registration_attribution.expires_at_utc
                    ),
                    "terminal_session_id": payload["terminal_session_id"],
                    "backend_type": payload["backend_type"],
                    "timing": timing,
                },
                artifact_candidates=terminal_artifact_candidates(result, timing=timing),
                semantics_input=FrozenToolResultSemanticsRuntimeInput(
                    semantics_input_kind=(
                        ToolResultRenderVariantCode.TERMINAL_MONITOR_REGISTRATION
                    ),
                    domain_submission=TerminalMonitorRegistrationDomainSubmissionFact(
                        process_id=request.process_id,
                        monitor_id=prepared.registration_semantic.monitor_id,
                        expires_at_utc=(
                            prepared.registration_attribution.expires_at_utc
                        ),
                        status=result.status.value,
                        exit_code=result.exit_code,
                        output_truncated=result.truncated,
                        terminal_session_id=str(payload["terminal_session_id"]),
                        backend_type=str(payload["backend_type"]),
                    ),
                ),
                terminal_payload_timing=terminal_payload_timing_fact(timing),
                prepared_terminal_monitor_registration=prepared,
                prepared_terminal_notification_reservation=(
                    prepared.notification_reservation
                ),
            )
        except BaseException:
            self.terminal_monitor_coordinator.discard_prepared_registration(prepared)
            raise

    def _inventory_result(self, call: ToolCall) -> ToolExecutionResult:
        if self.terminal_monitor_coordinator is None:
            return self._error_result(
                call,
                requested_action="list",
                process_id=None,
                monitor_id=None,
                error="terminal monitor owner is unavailable",
                status="blocked",
                policy_code="terminal_monitor_owner_unavailable",
            )
        monitors, omitted_monitor_count = (
            self.terminal_monitor_coordinator.list_current_snapshots(maximum_items=8)
        )
        summaries = tuple(
            TerminalMonitorSummaryFact(
                monitor_id=item.registration_event.registration_semantic.monitor_id,
                process_id=item.registration_event.registration_semantic.initial_baseline_cursor.stream_identity.process_id,
                lifecycle_state=item.core_state.lifecycle_state,
                observation_ordinal=item.core_state.last_committed_observation_ordinal,
                has_pending_observation=item.pending_observation_event is not None,
            )
            for item in monitors
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
            "omitted_monitor_count": omitted_monitor_count,
            "summaries_truncated": omitted_monitor_count > 0,
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
                    omitted_monitor_count=omitted_monitor_count,
                    summaries_truncated=omitted_monitor_count > 0,
                ),
            ),
            terminal_payload_timing=terminal_payload_timing_fact(timing),
        )

    def _cancellation_result(
        self,
        call: ToolCall,
        *,
        request: TerminalMonitorCancelInput,
        runtime_context: ToolRuntimeContext | None,
    ) -> ToolExecutionResult:
        if self.terminal_monitor_coordinator is None or runtime_context is None:
            return self._error_result(
                call,
                requested_action="cancel",
                process_id=None,
                monitor_id=request.monitor_id,
                error="terminal monitor owner is unavailable",
                status="blocked",
                policy_code="terminal_monitor_owner_unavailable",
            )
        cancellation = self.terminal_monitor_coordinator.prepare_cancellation(
            monitor_id=request.monitor_id,
            origin_tool_call_id=call.id,
            runtime_context=runtime_context,
        )
        timing = terminal_timing_payload(freshness="background_process_observation")
        payload = {
            "status": "success",
            "terminal_monitor_action": "cancel",
            "monitor_id": request.monitor_id,
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
                    monitor_id=request.monitor_id,
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
        normalized_action = requested_action if requested_action else "unknown"
        if normalized_action not in {"register", "list", "cancel"}:
            process_id = None
            monitor_id = None
        elif normalized_action == "register":
            monitor_id = None
        elif normalized_action == "list":
            process_id = None
            monitor_id = None
        else:
            process_id = None
        payload = {
            "status": status,
            "terminal_monitor_action": normalized_action,
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
                    requested_action=normalized_action,
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


def _terminal_access_off(
    *,
    runtime_context: ToolRuntimeContext | None,
    permission_state: PermissionState | None,
) -> bool:
    if runtime_context is not None and isinstance(
        runtime_context.permission_policy, dict
    ):
        return (
            runtime_context.permission_policy.get("terminal_access")
            == TerminalAccess.OFF.value
        )
    return (
        permission_state is not None
        and permission_state.policy.terminal is TerminalAccess.OFF
    )


__all__ = ["TerminalMonitorTool"]
