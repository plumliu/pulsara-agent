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
    object_schema,
    required_str_arg,
)
from pulsara_agent.tools.builtins.terminal import terminal_result_payload
from pulsara_agent.tools.builtins.workspace import WorkspaceTool


_SUPPORTED_ACTIONS = {"poll", "wait", "kill", "write", "submit", "close_stdin"}
DEFAULT_WAIT_TIMEOUT_SECONDS = 30


@dataclass(slots=True)
class TerminalProcessTool(WorkspaceTool):
    terminal_sessions: TerminalSessionManager | None = None
    owner_host_session_id: str | None = None
    permission_policy: EffectivePermissionPolicy | None = None
    name: str = "terminal_process"
    description: str = (
        "Poll, wait for, kill, or send stdin to a managed terminal process returned when terminal yields a process_id. "
        "Use write or submit for both pipe and PTY processes; use close_stdin to send EOF."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: object_schema(
            properties={
                "action": {
                    "type": "string",
                    "enum": ["poll", "wait", "kill", "write", "submit", "close_stdin"],
                    "description": "Process action for managed pipe or PTY terminal processes returned by terminal.",
                },
                "process_id": {"type": "string"},
                "data": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": DEFAULT_WAIT_TIMEOUT_SECONDS},
                "max_output_chars": {"type": "integer", "default": DEFAULT_MAX_OUTPUT_CHARS},
            },
            required=["action", "process_id"],
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
        process_id = required_str_arg(call.arguments, "process_id").strip()
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
        try:
            if action == "poll":
                result = self.terminal_sessions.poll_process(
                    process_id,
                    max_output_chars=max_output,
                    owner_host_session_id=self.owner_host_session_id,
                )
            elif action == "wait":
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
                result = self.terminal_sessions.kill_process(
                    process_id,
                    max_output_chars=max_output,
                    owner_host_session_id=self.owner_host_session_id,
                )
            elif action in {"write", "submit"}:
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
        )

    def _error_result(
        self,
        call: ToolCall,
        *,
        process_id: str,
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
                    "full_output_ref": None,
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
