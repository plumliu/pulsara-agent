"""Managed terminal process action tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime.permission import EffectivePermissionPolicy, TerminalAccess
from pulsara_agent.runtime.terminal import TerminalSessionManager, TerminalStatus
from pulsara_agent.runtime.terminal.process import ProcessInputError
from pulsara_agent.runtime.terminal_risk import is_hardline_terminal_command
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.schemas import (
    DEFAULT_MAX_OUTPUT_CHARS,
    MIN_TERMINAL_OUTPUT_CHARS,
    bounded_int_arg,
    bool_arg,
    object_schema,
    required_str_arg,
)
from pulsara_agent.tools.builtins.terminal import terminal_artifact_candidates, terminal_result_payload
from pulsara_agent.tools.builtins.workspace import WorkspaceTool


_SUPPORTED_ACTIONS = {"list", "log", "poll", "wait", "kill", "write", "submit", "close_stdin"}
_PROCESS_ID_ACTIONS = {"log", "poll", "wait", "kill", "write", "submit", "close_stdin"}
DEFAULT_WAIT_TIMEOUT_SECONDS = 30


@dataclass(slots=True)
class TerminalProcessTool(WorkspaceTool):
    terminal_sessions: TerminalSessionManager | None = None
    owner_host_session_id: str | None = None
    permission_policy: EffectivePermissionPolicy | None = None
    name: str = "terminal_process"
    description: str = (
        "List, inspect, poll, wait for, kill, or send stdin to managed terminal processes returned when terminal yields a process_id. "
        "Use list to see retained tasks, log to inspect output, write or submit for pipe/PTY input, and close_stdin to send EOF."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: object_schema(
            properties={
                "action": {
                    "type": "string",
                    "enum": ["list", "log", "poll", "wait", "kill", "write", "submit", "close_stdin"],
                    "description": "Process action for managed pipe or PTY terminal processes returned by terminal.",
                },
                "process_id": {"type": "string"},
                "data": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": DEFAULT_WAIT_TIMEOUT_SECONDS},
                "max_output_chars": {"type": "integer", "default": DEFAULT_MAX_OUTPUT_CHARS},
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

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        action = required_str_arg(call.arguments, "action").strip()
        process_id = _optional_process_id(call.arguments)
        if self.permission_policy is not None and self.permission_policy.terminal is TerminalAccess.OFF:
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
            return self._error_result(call, process_id=process_id, error=str(exc), status="not_found")
        except ProcessInputError as exc:
            return self._error_result(call, process_id=process_id, error=str(exc), status="blocked")
        return self._process_result(call, result, action=action)

    def _list_result(self, call: ToolCall, processes, *, action: str) -> ToolExecutionResult:
        live_count = self.terminal_sessions.live_process_count(owner_host_session_id=self.owner_host_session_id)
        finished_count = self.terminal_sessions.finished_process_count(owner_host_session_id=self.owner_host_session_id)
        payload = {
            "status": "success",
            "terminal_process_action": action,
            "processes": [process.to_payload() for process in processes],
            "live_process_count": live_count,
            "finished_process_count": finished_count,
        }
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json.dumps(payload, ensure_ascii=False),
            metadata={
                "terminal_process_action": action,
                "live_process_count": live_count,
                "finished_process_count": finished_count,
            },
        )

    def _log_result(self, call: ToolCall, log, *, action: str) -> ToolExecutionResult:
        payload = {
            "status": "success",
            "terminal_process_action": action,
            "process_id": log.process.process_id,
            **log.to_payload(),
        }
        return self._result(
            call,
            status=ToolResultState.SUCCESS,
            output=json.dumps(payload, ensure_ascii=False),
            metadata={
                "process_id": log.process.process_id,
                "terminal_process_action": action,
                "terminal_session_id": log.process.terminal_session_id,
                "backend_type": log.process.backend_type,
                "truncated": log.truncated,
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
                        )
                    )
                )
            ),
        )

    def _process_result(self, call: ToolCall, result, *, action: str) -> ToolExecutionResult:
        payload = terminal_result_payload(
            result,
            terminal_session_id=result.metadata.get("terminal_session_id", "default"),
            backend_type=result.metadata.get("backend_type", "local"),
        )
        payload["terminal_process_action"] = action
        return self._result(
            call,
            status=ToolResultState.SUCCESS if result.status in {TerminalStatus.RUNNING, TerminalStatus.SUCCESS} else ToolResultState.ERROR,
            output=json.dumps(payload, ensure_ascii=False),
            metadata={
                "process_id": result.process_id,
                "exit_code": result.exit_code,
                "cwd": result.cwd,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
                "terminal_process_action": action,
                "terminal_session_id": payload["terminal_session_id"],
                "backend_type": payload["backend_type"],
            },
            artifact_candidates=terminal_artifact_candidates(result),
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
        return self._result(
            call,
            status=ToolResultState.ERROR,
            output=json.dumps(
                {
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
                },
                ensure_ascii=False,
            ),
            metadata={
                "process_id": process_id,
                "exit_code": -1,
                "cwd": str(self.workspace_root),
                "timed_out": False,
                "truncated": False,
                "terminal_session_id": "default",
                "backend_type": "local",
                "policy_code": policy_code,
            },
        )


def _optional_process_id(args: dict[str, Any]) -> str | None:
    raw = args.get("process_id")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


@dataclass(frozen=True, slots=True)
class _LogArtifactResult:
    status: TerminalStatus
    process_id: str | None
    cwd: str
    full_output_text: str | None
