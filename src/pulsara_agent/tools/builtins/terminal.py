"""Generic terminal built-in tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime.terminal import TerminalRequest, TerminalSessionManager, TerminalStatus
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.schemas import (
    DEFAULT_MAX_OUTPUT_CHARS,
    bool_arg,
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
            "terminal_session_id": {
                "type": "string",
                "default": "default",
                "description": "Canonical terminal session id. session_id is retained as a compatibility alias.",
            },
            "timeout_seconds": {"type": "integer", "default": 30},
            "max_output_chars": {"type": "integer", "default": DEFAULT_MAX_OUTPUT_CHARS},
            "background": {
                "type": "boolean",
                "default": False,
                "description": "Start a managed background process and return process_id.",
            },
            "tty": {
                "type": "boolean",
                "default": False,
                "description": "Use a POSIX PTY for managed background interactive commands.",
            },
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
        return self._execute(call)

    def execute_streaming(self, call: ToolCall, emit_delta: Callable[[str], None]) -> ToolExecutionResult:
        max_output = int_arg(call.arguments, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)
        builder = _StreamingTerminalJsonBuilder(emit_delta, max_output_chars=max_output)
        result = self._execute(call, output_callback=builder.emit_output_delta)
        return builder.finish(result)

    def _execute(
        self,
        call: ToolCall,
        *,
        output_callback: Callable[[str], None] | None = None,
    ) -> ToolExecutionResult:
        command = required_str_arg(call.arguments, "command")
        workdir = call.arguments.get("workdir")
        if workdir is not None and not isinstance(workdir, str):
            raise TypeError("workdir must be a string")
        session_id = _terminal_session_arg(call.arguments)
        if session_id is not None and not isinstance(session_id, str):
            raise TypeError("terminal_session_id must be a string")
        if "notify_on_complete" in call.arguments:
            return self._blocked_result(
                call,
                command=command,
                session_id=session_id or "default",
                error="notify_on_complete is not supported yet",
                payload_status="not_supported_yet",
            )
        timeout = int_arg(call.arguments, "timeout_seconds", 30)
        max_output = int_arg(call.arguments, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)
        background = bool_arg(call.arguments, "background", False)
        tty = bool_arg(call.arguments, "tty", False)
        if background:
            output_callback = None
        if tty and not background:
            return self._blocked_result(
                call,
                command=command,
                session_id=session_id or "default",
                error="tty mode requires background=true",
            )
        if background and "timeout_seconds" in call.arguments:
            return self._blocked_result(
                call,
                command=command,
                session_id=session_id or "default",
                error="background=true does not support timeout_seconds yet; use terminal_process.wait timeout",
            )
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
                background=background,
                tty=tty,
                metadata={"output_callback": output_callback} if output_callback is not None else {},
            )
        )
        payload = terminal_result_payload(
            result,
            terminal_session_id=terminal_session.session_id,
            backend_type=terminal_session.state.backend_type.value,
        )
        return self._result(
            call,
            status=_tool_result_state(result.status),
            output=json.dumps(payload, ensure_ascii=False),
            metadata={
                "command": command,
                "exit_code": result.exit_code,
                "cwd": result.cwd,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
                "process_id": result.process_id,
                "terminal_session_id": terminal_session.session_id,
                "backend_type": terminal_session.state.backend_type.value,
                "shell": result.metadata.get("shell"),
            },
        )

    def _blocked_result(
        self,
        call: ToolCall,
        *,
        command: str,
        session_id: str,
        error: str,
        payload_status: str = TerminalStatus.BLOCKED.value,
        suggested_args: dict[str, Any] | None = None,
        policy_code: str | None = None,
    ) -> ToolExecutionResult:
        return self._result(
            call,
            status=ToolResultState.ERROR,
            output=json.dumps(
                {
                    "status": payload_status,
                    "output": "",
                    "exit_code": -1,
                    "cwd": str(self.workspace_root),
                    "timed_out": False,
                    "truncated": False,
                    "error": error,
                    "policy_code": policy_code,
                    "suggested_args": suggested_args or {},
                    "process_id": None,
                    "full_output_ref": None,
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
                "process_id": None,
                "terminal_session_id": session_id,
                "backend_type": "local",
            },
        )


def terminal_result_payload(
    result,
    *,
    terminal_session_id: str,
    backend_type: str,
) -> dict[str, Any]:
    payload = {
        "status": result.status.value,
        "output": result.output,
        "exit_code": result.exit_code,
        "cwd": result.cwd,
        "timed_out": result.timed_out,
        "truncated": result.truncated,
        "error": result.error,
        "process_id": result.process_id,
        "terminal_session_id": terminal_session_id,
        "backend_type": backend_type,
        "io_mode": result.metadata.get("io_mode"),
        "full_output_ref": result.full_output_ref,
    }
    if "policy_code" in result.metadata:
        payload["policy_code"] = result.metadata.get("policy_code")
    if "suggested_args" in result.metadata:
        payload["suggested_args"] = result.metadata.get("suggested_args") or {}
    if "shell" in result.metadata:
        payload["shell"] = result.metadata.get("shell")
    return payload


def _tool_result_state(status: TerminalStatus) -> ToolResultState:
    return ToolResultState.SUCCESS if status in {TerminalStatus.SUCCESS, TerminalStatus.RUNNING} else ToolResultState.ERROR


def _terminal_session_arg(args: dict[str, Any]) -> str | None:
    legacy = args.get("session_id")
    canonical = args.get("terminal_session_id")
    if legacy is not None and canonical is not None and legacy != canonical:
        raise ValueError("session_id and terminal_session_id must match when both are provided")
    return canonical if canonical is not None else legacy


class _StreamingTerminalJsonBuilder:
    _TRUNCATION_NOTICE = "\n\n... [OUTPUT TRUNCATED - full redacted output stored in full_output_ref] ...\n\n"

    def __init__(self, emit_delta: Callable[[str], None], *, max_output_chars: int) -> None:
        self._emit_delta = emit_delta
        self._max_output_chars = max_output_chars
        self._started = False
        self._output_chars = 0
        self._preview_parts: list[str] = []
        self._overflowed = False

    def emit_output_delta(self, delta: str) -> None:
        if not delta or self._overflowed:
            return
        if not self._started:
            self._emit_delta('{"output":"')
            self._started = True
        remaining = max(self._max_output_chars - self._output_chars, 0)
        if len(delta) <= remaining:
            self._emit_preview(delta)
            return
        if remaining:
            self._emit_preview(delta[:remaining])
        self._emit_preview(self._TRUNCATION_NOTICE)
        self._overflowed = True

    def finish(self, result: ToolExecutionResult) -> ToolExecutionResult:
        payload = json.loads(result.output)
        if self._started:
            if self._overflowed:
                payload["output"] = "".join(self._preview_parts)
                payload["truncated"] = True
            payload.pop("output", None)
            suffix = json.dumps(payload, ensure_ascii=False)
            self._emit_delta('",' + suffix[1:])
        else:
            self._emit_delta(result.output)
        if self._overflowed:
            final_payload = json.loads(result.output)
            final_payload["output"] = "".join(self._preview_parts)
            final_payload["truncated"] = True
            output = json.dumps(final_payload, ensure_ascii=False)
        else:
            output = result.output
        return ToolExecutionResult(
            call_id=result.call_id,
            tool_name=result.tool_name,
            status=result.status,
            output=output,
            metadata={**result.metadata, "streamed_output_complete": True},
        )

    def _emit_preview(self, text: str) -> None:
        if not text:
            return
        self._preview_parts.append(text)
        self._output_chars += len(text)
        self._emit_delta(_json_string_fragment(text))


def _json_string_fragment(value: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False)
    return encoded[1:-1]
