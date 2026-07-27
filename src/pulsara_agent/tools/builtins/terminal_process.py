"""Managed terminal process action tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import ValidationError

from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_result_semantics import (
    FrozenToolResultSemanticsRuntimeInput,
    unbounded_error_preview,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.tool_result import (
    TerminalProcessErrorDomainSubmissionFact,
    TerminalProcessInventoryDomainSubmissionFact,
    TerminalProcessObservationDomainSubmissionFact,
    TerminalProcessSummaryFact,
    ToolResultRenderVariantCode,
)
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolExecutionResult,
    ToolRuntimeContext,
)
from pulsara_agent.ports.terminal import (
    TerminalProcessInventoryOutcome,
    TerminalProcessKilledOutcome,
    TerminalProcessLogOutcome,
    TerminalProcessObservationOutcome,
    TerminalProcessPort,
    TerminalProcessRejectedOutcome,
    TerminalStatus,
    build_terminal_port_invocation_owner,
    parse_terminal_process_input,
)
from pulsara_agent.tools.builtins.terminal import (
    terminal_artifact_candidates,
    freeze_tool_display_payload,
    terminal_payload_timing_fact,
    terminal_result_metadata,
    terminal_result_payload,
    terminal_timing_payload,
)
from pulsara_agent.tools.builtins.workspace import WorkspaceTool


@dataclass(slots=True)
class TerminalProcessTool(WorkspaceTool):
    _SUPPORTED_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"list", "log", "poll", "wait", "write", "submit", "close_stdin", "kill"}
    )
    process_port: TerminalProcessPort
    name: str = "terminal_process"

    def __post_init__(self) -> None:
        WorkspaceTool.__post_init__(self)

    def execute(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> ToolExecutionResult:
        try:
            request = parse_terminal_process_input(call.arguments)
        except ValidationError as exc:
            return self._error_result(
                call,
                process_id=_optional_process_id(call.arguments),
                error=_validation_error_text(exc),
                status="malformed_arguments",
                policy_code="terminal_process_malformed_arguments",
            )
        if runtime_context is None:
            return self._error_result(
                call,
                process_id=getattr(request, "process_id", None),
                error="terminal_process requires typed runtime invocation authority",
                status="blocked",
                policy_code="terminal_process_owner_unavailable",
            )
        owner = build_terminal_port_invocation_owner(
            runtime_session_id=runtime_context.runtime_session_id,
            tool_call_id=call.id,
            tool_name="terminal_process",
            event_context=runtime_context.event_context,
            owner_kind=runtime_context.owner_kind,
            permission=runtime_context.permission,
        )
        outcome = self.process_port.execute(request=request, owner=owner)
        if isinstance(outcome, TerminalProcessRejectedOutcome):
            return self._error_result(
                call,
                process_id=outcome.process_id,
                error=outcome.sanitized_message,
                status=outcome.status,
                policy_code=outcome.reject_code.value,
            )
        if isinstance(outcome, TerminalProcessInventoryOutcome):
            return self._list_result(call, outcome)
        if isinstance(outcome, TerminalProcessLogOutcome):
            return self._log_result(call, outcome)
        if isinstance(outcome, TerminalProcessKilledOutcome):
            return self._process_result(
                call,
                outcome.result,
                action="kill",
                observation_receipt=outcome.completion_observation_receipt,
            )
        if isinstance(outcome, TerminalProcessObservationOutcome):
            return self._process_result(
                call,
                outcome.result,
                action=outcome.action,
                observation_receipt=outcome.observation_receipt,
            )
        raise AssertionError(type(outcome))

    def execute_with_context(
        self,
        call: ToolCall,
        *,
        event_context=None,
        record_event=None,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> ToolExecutionResult:
        return self.execute(call, runtime_context=runtime_context)

    def _list_result(self, call: ToolCall, outcome) -> ToolExecutionResult:
        processes = outcome.processes
        live_count = outcome.live_process_count
        finished_count = outcome.finished_process_count
        action = "list"
        timing = terminal_timing_payload(freshness="background_process_observation")
        payload = {
            "status": "success",
            "terminal_process_action": action,
            "processes": [process.to_payload() for process in processes],
            "live_process_count": live_count,
            "finished_process_count": finished_count,
            "timing": timing,
        }
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json.dumps(payload, ensure_ascii=False),
            display_payload=freeze_tool_display_payload(payload),
            metadata={
                "terminal_process_action": action,
                "live_process_count": live_count,
                "finished_process_count": finished_count,
                "timing": timing,
            },
            semantics_input=FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_PROCESS_INVENTORY,
                domain_submission=TerminalProcessInventoryDomainSubmissionFact(
                    status="success",
                    live_process_count=live_count,
                    finished_process_count=finished_count,
                    process_summaries=tuple(
                        _process_summary_fact(process) for process in processes
                    ),
                    omitted_process_count=0,
                    summaries_truncated=False,
                ),
            ),
            terminal_payload_timing=terminal_payload_timing_fact(timing),
        )

    def _log_result(self, call: ToolCall, outcome) -> ToolExecutionResult:
        log = outcome.log
        action = "log"
        timing = terminal_timing_payload(
            duration_seconds=log.process.duration_seconds,
            freshness="background_process_observation",
        )
        payload = {
            "status": "success",
            "terminal_process_action": action,
            "process_id": log.process.process_id,
            "timing": timing,
            **log.to_payload(),
        }
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json.dumps(payload, ensure_ascii=False),
            display_payload=freeze_tool_display_payload(payload),
            metadata={
                "process_id": log.process.process_id,
                "terminal_process_action": action,
                "terminal_session_id": log.process.terminal_session_id,
                "backend_type": log.process.backend_type,
                "truncated": log.truncated,
                "timing": timing,
            },
            artifact_candidates=(
                ()
                if log.full_output_text is None
                else (
                    terminal_artifact_candidates(
                        _LogArtifactResult(
                            status=TerminalStatus(log.process.status),
                            process_id=log.process.process_id,
                            cwd=log.process.cwd,
                            full_output_text=log.full_output_text,
                            metadata={"timing": timing},
                        )
                    )
                )
            ),
            semantics_input=FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_PROCESS_OBSERVATION,
                domain_submission=TerminalProcessObservationDomainSubmissionFact(
                    action=action,
                    process_id=log.process.process_id,
                    status="success",
                    exit_code=log.process.exit_code,
                    command=log.process.command,
                    cwd=log.process.cwd,
                    timed_out=log.process.timed_out,
                    output_truncated=log.truncated,
                    error=None,
                    yielded_to_background=log.process.status
                    == TerminalStatus.RUNNING.value,
                    terminal_session_id=log.process.terminal_session_id,
                    backend_type=log.process.backend_type,
                    io_mode=log.process.io_mode,
                    stdin_closed=log.process.stdin_closed,
                    policy_code=None,
                    duration_seconds=log.process.duration_seconds,
                ),
            ),
            terminal_payload_timing=terminal_payload_timing_fact(timing),
            terminal_process_observation_receipt=outcome.observation_receipt,
        )

    def _process_result(
        self,
        call: ToolCall,
        result,
        *,
        action: str,
        observation_receipt=None,
    ) -> ToolExecutionResult:
        if (
            result.status in {TerminalStatus.RUNNING, TerminalStatus.SUCCESS}
            and not result.process_id
        ):
            raise ValueError(
                "successful terminal_process observation requires process_id"
            )
        result_metadata = terminal_result_metadata(result)
        timing = terminal_timing_payload(
            duration_seconds=result_metadata.get("duration_seconds"),
            freshness="background_process_observation",
        )
        payload = terminal_result_payload(
            result,
            terminal_session_id=result_metadata.get("terminal_session_id", "default"),
            backend_type=result_metadata.get("backend_type", "local"),
            timing=timing,
        )
        payload["terminal_process_action"] = action
        return self._result(
            call,
            status=ToolResultState.SUCCESS
            if result.status in {TerminalStatus.RUNNING, TerminalStatus.SUCCESS}
            else ToolResultState.ERROR,
            output=json.dumps(payload, ensure_ascii=False),
            display_payload=freeze_tool_display_payload(payload),
            metadata={
                "process_id": result.process_id,
                "exit_code": result.exit_code,
                "cwd": result.cwd,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
                "terminal_process_action": action,
                "terminal_session_id": payload["terminal_session_id"],
                "backend_type": payload["backend_type"],
                "timing": timing,
            },
            artifact_candidates=terminal_artifact_candidates(result, timing=timing),
            semantics_input=FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=(
                    ToolResultRenderVariantCode.TERMINAL_PROCESS_OBSERVATION
                    if result.status in {TerminalStatus.RUNNING, TerminalStatus.SUCCESS}
                    else ToolResultRenderVariantCode.TERMINAL_PROCESS_ADAPTER_ERROR
                ),
                domain_submission=(
                    TerminalProcessObservationDomainSubmissionFact(
                        action=action,
                        process_id=str(result.process_id),
                        status=result.status.value,
                        exit_code=result.exit_code,
                        command=(
                            str(result_metadata["command"])
                            if result_metadata.get("command") is not None
                            else None
                        ),
                        cwd=result.cwd,
                        timed_out=result.timed_out,
                        output_truncated=result.truncated,
                        error=(
                            unbounded_error_preview(result.error)
                            if result.error
                            else None
                        ),
                        yielded_to_background=result.status is TerminalStatus.RUNNING,
                        terminal_session_id=str(payload["terminal_session_id"]),
                        backend_type=str(payload["backend_type"]),
                        io_mode=(
                            str(result_metadata["io_mode"])
                            if result_metadata.get("io_mode") is not None
                            else None
                        ),
                        stdin_closed=(
                            result_metadata.get("stdin_closed")
                            if isinstance(result_metadata.get("stdin_closed"), bool)
                            else None
                        ),
                        policy_code=(
                            str(result_metadata["policy_code"])
                            if result_metadata.get("policy_code") is not None
                            else None
                        ),
                        duration_seconds=(
                            float(result_metadata["duration_seconds"])
                            if isinstance(
                                result_metadata.get("duration_seconds"), int | float
                            )
                            and not isinstance(
                                result_metadata.get("duration_seconds"), bool
                            )
                            else None
                        ),
                    )
                    if result.status in {TerminalStatus.RUNNING, TerminalStatus.SUCCESS}
                    else TerminalProcessErrorDomainSubmissionFact(
                        requested_action=action,
                        process_id=result.process_id,
                        status=result.status.value,
                        error=unbounded_error_preview(
                            result.error or "terminal process action failed"
                        ),
                        policy_code=(
                            str(result_metadata["policy_code"])
                            if result_metadata.get("policy_code") is not None
                            else None
                        ),
                        terminal_session_id=str(payload["terminal_session_id"]),
                        backend_type=str(payload["backend_type"]),
                    )
                ),
            ),
            terminal_payload_timing=(
                terminal_payload_timing_fact(timing)
                if result.status in {TerminalStatus.RUNNING, TerminalStatus.SUCCESS}
                else terminal_payload_timing_fact(timing)
            ),
            terminal_process_observation_receipt=observation_receipt,
        )

    def _error_result(
        self,
        call: ToolCall,
        *,
        process_id: str | None,
        error: str,
        status: str = "error",
        policy_code: str | None = None,
    ) -> ToolExecutionResult:
        timing = terminal_timing_payload(freshness="background_process_observation")
        payload = {
            "status": status,
            "output": "",
            "exit_code": -1,
            "cwd": str(self.workspace_root),
            "timed_out": False,
            "truncated": False,
            "error": error,
            "process_id": process_id,
            "terminal_session_id": "default",
            "backend_type": "local",
            "policy_code": policy_code,
            "timing": timing,
        }
        return self._result(
            call,
            status=ToolResultState.ERROR,
            output=json.dumps(payload, ensure_ascii=False),
            display_payload=freeze_tool_display_payload(payload),
            metadata={
                "process_id": process_id,
                "exit_code": -1,
                "cwd": str(self.workspace_root),
                "timed_out": False,
                "truncated": False,
                "terminal_session_id": "default",
                "backend_type": "local",
                "policy_code": policy_code,
                "timing": timing,
            },
            semantics_input=FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_PROCESS_ERROR,
                domain_submission=TerminalProcessErrorDomainSubmissionFact(
                    requested_action=str(call.arguments.get("action") or "unknown"),
                    process_id=process_id,
                    status=status,
                    error=unbounded_error_preview(error),
                    policy_code=policy_code,
                    terminal_session_id=None,
                    backend_type=None,
                ),
            ),
            terminal_payload_timing=None,
        )


def _optional_process_id(args: dict[str, Any]) -> str | None:
    raw = args.get("process_id")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _validation_error_text(error: ValidationError) -> str:
    first = error.errors(include_url=False)[0]
    location = ".".join(str(item) for item in first.get("loc", ()))
    message = str(first.get("msg") or "invalid arguments")
    return f"{location}: {message}" if location else message


def _process_summary_fact(process) -> TerminalProcessSummaryFact:
    payload = {
        "process_id": process.process_id,
        "status": process.status,
        "exit_code": process.exit_code,
        "command": process.command,
        "cwd": process.cwd,
        "terminal_session_id": process.terminal_session_id,
        "backend_type": process.backend_type,
        "io_mode": process.io_mode,
        "timed_out": process.timed_out,
        "stdin_closed": process.stdin_closed,
        "duration_seconds": process.duration_seconds,
    }
    return TerminalProcessSummaryFact(
        **payload,
        summary_fingerprint=context_fingerprint("terminal-process-summary:v1", payload),
    )


@dataclass(frozen=True, slots=True)
class _LogArtifactResult:
    status: TerminalStatus
    process_id: str | None
    cwd: str
    full_output_text: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
