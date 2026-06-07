"""Generic terminal built-in tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime.terminal import TerminalRequest, TerminalSessionManager, TerminalStatus
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.schemas import (
    DEFAULT_MAX_OUTPUT_CHARS,
    int_arg,
    object_schema,
    required_str_arg,
)
from pulsara_agent.tools.builtins.workspace import WorkspaceTool


@dataclass(slots=True)
class TerminalTool(WorkspaceTool):
    terminal_sessions: TerminalSessionManager | None = None
    name: str = "terminal"
    description: str = (
        "Run a foreground shell command inside workspace_root. "
        "Use read_file/search_files/write_file/edit_file for file operations; "
        "reserve terminal for builds, tests, git, package managers, scripts, network commands, and external CLIs."
    )
    parameters: dict[str, Any] = field(default_factory=lambda: object_schema(
        properties={
            "command": {"type": "string", "description": "Shell command to run."},
            "workdir": {
                "type": "string",
                "description": "Optional working directory inside workspace_root. Relative paths resolve from workspace_root.",
            },
            "session_id": {
                "type": "string",
                "default": "default",
                "description": "Optional terminal session id. Use short names like default, frontend, or tests.",
            },
            "timeout_seconds": {"type": "integer", "default": 30},
            "max_output_chars": {"type": "integer", "default": DEFAULT_MAX_OUTPUT_CHARS},
        },
        required=["command"],
    ))
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __post_init__(self) -> None:
        WorkspaceTool.__post_init__(self)
        if self.terminal_sessions is None:
            self.terminal_sessions = TerminalSessionManager(self.workspace_root)

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        command = required_str_arg(call.arguments, "command")
        workdir = call.arguments.get("workdir")
        if workdir is not None and not isinstance(workdir, str):
            raise TypeError("workdir must be a string")
        session_id = call.arguments.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise TypeError("session_id must be a string")
        timeout = int_arg(call.arguments, "timeout_seconds", 30)
        max_output = int_arg(call.arguments, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)
        try:
            terminal_session = self.terminal_sessions.get_or_create(session_id)
        except ValueError as exc:
            return self._blocked_result(call, command=command, session_id=session_id or "default", error=str(exc))

        result = terminal_session.execute(
            TerminalRequest(
                command=command,
                workdir=workdir,
                timeout_seconds=timeout,
                max_output_chars=max_output,
            )
        )
        return self._result(
            call,
            status=ToolResultState.SUCCESS if result.status is TerminalStatus.SUCCESS else ToolResultState.ERROR,
            output=json.dumps(
                {
                    "status": result.status.value,
                    "output": result.output,
                    "exit_code": result.exit_code,
                    "cwd": result.cwd,
                    "timed_out": result.timed_out,
                    "truncated": result.truncated,
                    "error": result.error,
                    "terminal_session_id": terminal_session.session_id,
                    "backend_type": terminal_session.state.backend_type.value,
                },
                ensure_ascii=False,
            ),
            metadata={
                "command": command,
                "exit_code": result.exit_code,
                "cwd": result.cwd,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
                "terminal_session_id": terminal_session.session_id,
                "backend_type": terminal_session.state.backend_type.value,
            },
        )

    def _blocked_result(
        self,
        call: ToolCall,
        *,
        command: str,
        session_id: str,
        error: str,
    ) -> ToolExecutionResult:
        return self._result(
            call,
            status=ToolResultState.ERROR,
            output=json.dumps(
                {
                    "status": TerminalStatus.BLOCKED.value,
                    "output": "",
                    "exit_code": -1,
                    "cwd": str(self.workspace_root),
                    "timed_out": False,
                    "truncated": False,
                    "error": error,
                    "terminal_session_id": session_id,
                    "backend_type": "local",
                },
                ensure_ascii=False,
            ),
            metadata={
                "command": command,
                "exit_code": -1,
                "cwd": str(self.workspace_root),
                "timed_out": False,
                "truncated": False,
                "terminal_session_id": session_id,
                "backend_type": "local",
            },
        )
