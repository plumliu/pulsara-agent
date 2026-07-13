"""Managed terminal process action tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.message import ToolResultState
from pulsara_agent.capability.result_semantics import (
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
from pulsara_agent.runtime.permission import PermissionState, TerminalAccess
from pulsara_agent.runtime.terminal import TerminalSessionManager, TerminalStatus
from pulsara_agent.runtime.terminal.process import ProcessInputError
from pulsara_agent.runtime.terminal_risk import is_hardline_terminal_command
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult, ToolRuntimeContext
from pulsara_agent.tools.builtins.schemas import (
    DEFAULT_MAX_OUTPUT_CHARS,
    MIN_TERMINAL_OUTPUT_CHARS,
    bounded_int_arg,
    bool_arg,
    object_schema,
    required_str_arg,
)
from pulsara_agent.tools.builtins.terminal import (
    terminal_artifact_candidates,
    freeze_tool_display_payload,
    terminal_payload_timing_fact,
    terminal_result_payload,
    terminal_timing_payload,
)
from pulsara_agent.tools.builtins.workspace import WorkspaceTool


_SUPPORTED_ACTIONS = {
    "list",
    "log",
    "poll",
    "wait",
    "kill",
    "write",
    "submit",
    "close_stdin",
}
_PROCESS_ID_ACTIONS = {"log", "poll", "wait", "kill", "write", "submit", "close_stdin"}
DEFAULT_WAIT_TIMEOUT_SECONDS = 30


@dataclass(slots=True)
class TerminalProcessTool(WorkspaceTool):
    terminal_sessions: TerminalSessionManager | None = None
    owner_host_session_id: str | None = None
    permission_state: PermissionState | None = None
    name: str = "terminal_process"
    description: str = (
        "List, inspect, poll, wait for, kill, or send stdin to managed terminal processes returned when terminal yields a process_id. "
        "Use list to see retained tasks. Use log to inspect retained output; its inline output is a bounded preview and may include artifacts[] "
        "for the complete retained log. Use poll or wait for current lifecycle state and bounded output snapshots. "
        "Use write or submit for pipe/PTY input, close_stdin to send EOF, and kill only when you intend to stop the process."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: object_schema(
            properties={
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "log",
                        "poll",
                        "wait",
                        "kill",
                        "write",
                        "submit",
                        "close_stdin",
                    ],
                    "description": "Process action for managed pipe or PTY terminal processes returned by terminal.",
                },
                "process_id": {"type": "string"},
                "data": {"type": "string"},
                "timeout_seconds": {
                    "type": "integer",
                    "default": DEFAULT_WAIT_TIMEOUT_SECONDS,
                },
                "max_output_chars": {
                    "type": "integer",
                    "default": DEFAULT_MAX_OUTPUT_CHARS,
                },
                "include_finished": {"type": "boolean", "default": True},
                "include_running": {"type": "boolean", "default": True},
            },
            required=["action"],
        )
    )
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
        action = required_str_arg(call.arguments, "action").strip()
        process_id = _optional_process_id(call.arguments)
        if _terminal_access_off(
            runtime_context=runtime_context, permission_state=self.permission_state
        ):
            return self._error_result(
                call,
                process_id=process_id,
                error="terminal_process is disabled by permission policy",
                status="blocked",
                policy_code="terminal_access_off",
            )
        max_output = bounded_int_arg(
            call.arguments,
            "max_output_chars",
            default=DEFAULT_MAX_OUTPUT_CHARS,
            minimum=MIN_TERMINAL_OUTPUT_CHARS,
            maximum=DEFAULT_MAX_OUTPUT_CHARS,
        )
        if action not in _SUPPORTED_ACTIONS:
            return self._error_result(
                call,
                process_id=process_id,
                error=f"terminal_process action is not supported yet: {action}",
                status="not_supported_yet",
            )
        if action in _PROCESS_ID_ACTIONS and not process_id:
            return self._error_result(
                call,
                process_id=process_id,
                error=f"process_id is required for terminal_process action: {action}",
                status="blocked",
            )
        try:
            if action == "list":
                include_finished = bool_arg(call.arguments, "include_finished", True)
                include_running = bool_arg(call.arguments, "include_running", True)
                processes = self.terminal_sessions.list_processes(
                    owner_host_session_id=self.owner_host_session_id,
                    include_finished=include_finished,
                    include_running=include_running,
                )
                return self._list_result(call, processes, action=action)
            if action == "log":
                assert process_id is not None
                log = self.terminal_sessions.log_process(
                    process_id,
                    max_output_chars=max_output,
                    owner_host_session_id=self.owner_host_session_id,
                )
                return self._log_result(call, log, action=action)
            if action == "poll":
                assert process_id is not None
                result = self.terminal_sessions.poll_process(
                    process_id,
                    max_output_chars=max_output,
                    owner_host_session_id=self.owner_host_session_id,
                )
            elif action == "wait":
                assert process_id is not None
                timeout = bounded_int_arg(
                    call.arguments,
                    "timeout_seconds",
                    default=DEFAULT_WAIT_TIMEOUT_SECONDS,
                    minimum=1,
                    maximum=DEFAULT_WAIT_TIMEOUT_SECONDS,
                )
                result = self.terminal_sessions.wait_process(
                    process_id,
                    timeout_seconds=timeout,
                    max_output_chars=max_output,
                    owner_host_session_id=self.owner_host_session_id,
                )
            elif action == "kill":
                assert process_id is not None
                result = self.terminal_sessions.kill_process(
                    process_id,
                    max_output_chars=max_output,
                    owner_host_session_id=self.owner_host_session_id,
                )
            elif action in {"write", "submit"}:
                assert process_id is not None
                data = call.arguments.get("data")
                if not isinstance(data, str):
                    return self._error_result(
                        call,
                        process_id=process_id,
                        error="data must be a string",
                        status="blocked",
                    )
                if is_hardline_terminal_command(data):
                    return self._error_result(
                        call,
                        process_id=process_id,
                        error="terminal process input blocked by hardline permission policy",
                        status="blocked",
                        policy_code="hardline_terminal_process_input",
                    )
                result = self.terminal_sessions.write_process(
                    process_id,
                    data,
                    append_newline=action == "submit",
                    max_output_chars=max_output,
                    owner_host_session_id=self.owner_host_session_id,
                )
            elif action == "close_stdin":
                assert process_id is not None
                result = self.terminal_sessions.close_process_stdin(
                    process_id,
                    max_output_chars=max_output,
                    owner_host_session_id=self.owner_host_session_id,
                )
            else:
                raise AssertionError(action)
        except KeyError as exc:
            return self._error_result(
                call, process_id=process_id, error=str(exc), status="not_found"
            )
        except ProcessInputError as exc:
            return self._error_result(
                call, process_id=process_id, error=str(exc), status="blocked"
            )
        return self._process_result(call, result, action=action)

    def execute_with_context(
        self,
        call: ToolCall,
        *,
        event_context=None,
        record_event=None,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> ToolExecutionResult:
        return self.execute(call, runtime_context=runtime_context)

    def _list_result(
        self, call: ToolCall, processes, *, action: str
    ) -> ToolExecutionResult:
        live_count = self.terminal_sessions.live_process_count(
            owner_host_session_id=self.owner_host_session_id
        )
        finished_count = self.terminal_sessions.finished_process_count(
            owner_host_session_id=self.owner_host_session_id
        )
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

    def _log_result(self, call: ToolCall, log, *, action: str) -> ToolExecutionResult:
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
        )

    def _process_result(
        self, call: ToolCall, result, *, action: str
    ) -> ToolExecutionResult:
        if (
            result.status in {TerminalStatus.RUNNING, TerminalStatus.SUCCESS}
            and not result.process_id
        ):
            raise ValueError(
                "successful terminal_process observation requires process_id"
            )
        timing = terminal_timing_payload(
            duration_seconds=result.metadata.get("duration_seconds"),
            freshness="background_process_observation",
        )
        payload = terminal_result_payload(
            result,
            terminal_session_id=result.metadata.get("terminal_session_id", "default"),
            backend_type=result.metadata.get("backend_type", "local"),
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
                            str(result.metadata["command"])
                            if result.metadata.get("command") is not None
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
                            str(result.metadata["io_mode"])
                            if result.metadata.get("io_mode") is not None
                            else None
                        ),
                        stdin_closed=(
                            result.metadata.get("stdin_closed")
                            if isinstance(result.metadata.get("stdin_closed"), bool)
                            else None
                        ),
                        policy_code=(
                            str(result.metadata["policy_code"])
                            if result.metadata.get("policy_code") is not None
                            else None
                        ),
                        duration_seconds=(
                            float(result.metadata["duration_seconds"])
                            if isinstance(
                                result.metadata.get("duration_seconds"), int | float
                            )
                            and not isinstance(
                                result.metadata.get("duration_seconds"), bool
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
                            str(result.metadata["policy_code"])
                            if result.metadata.get("policy_code") is not None
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


@dataclass(frozen=True, slots=True)
class _LogArtifactResult:
    status: TerminalStatus
    process_id: str | None
    cwd: str
    full_output_text: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
