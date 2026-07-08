"""Generic terminal built-in tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from pulsara_agent.event import AgentEvent, EventContext
from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime.permission import PermissionState, TerminalAccess
from pulsara_agent.runtime.terminal import TerminalRequest, TerminalSessionManager, TerminalStatus
from pulsara_agent.runtime.tool_artifacts import (
    ToolResultArtifactOptions,
    build_adaptive_preview,
    effective_terminal_output_cap,
)
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult, ToolResultArtifactCandidate, ToolRuntimeContext
from pulsara_agent.tools.builtins.schemas import (
    DEFAULT_MAX_OUTPUT_CHARS,
    MIN_TERMINAL_OUTPUT_CHARS,
    bounded_int_arg,
    bool_arg,
    int_arg,
    object_schema,
    required_str_arg,
    str_arg,
)
from pulsara_agent.tools.builtins.workspace import WorkspaceTool


@dataclass(slots=True)
class TerminalTool(WorkspaceTool):
    terminal_sessions: TerminalSessionManager | None = None
    owner_host_session_id: str | None = None
    owner_conversation_id: str | None = None
    permission_state: PermissionState | None = None
    name: str = "terminal"
    description: str = (
        "Run a shell command inside workspace_root. "
        "The inline output is a bounded preview, not the complete retained output; "
        "when artifacts[] is present, use artifact_read for the full retained tool output. "
        "If it is still running after yield_time_ms, return a process_id for terminal_process while the command keeps running. "
        "Use terminal_process log to inspect retained output for yielded/background processes. "
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
            "terminal_session_id": {
                "type": "string",
                "default": "default",
                "description": "Terminal session id. Use short names like default, frontend, or tests.",
            },
            "yield_time_ms": {
                "type": "integer",
                "default": 10_000,
                "description": (
                    "Wait up to this many milliseconds for the command to finish. "
                    "If it is still running after this window, return process_id; the command is not killed."
                ),
            },
            "tty": {
                "type": "boolean",
                "default": False,
                "description": "Allocate a POSIX PTY for interactive commands.",
            },
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
        return self._execute(call)

    def execute_streaming(self, call: ToolCall, emit_delta: Callable[[str], None]) -> ToolExecutionResult:
        return self._execute_streaming(call, emit_delta)

    def execute_with_context(
        self,
        call: ToolCall,
        *,
        event_context: EventContext,
        record_event: Callable[[AgentEvent], AgentEvent] | None = None,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> ToolExecutionResult:
        return self._execute(
            call,
            event_context=event_context,
            record_event=record_event,
            runtime_context=runtime_context,
        )

    def execute_streaming_with_context(
        self,
        call: ToolCall,
        emit_delta: Callable[[str], None],
        *,
        event_context: EventContext,
        record_event: Callable[[AgentEvent], AgentEvent] | None = None,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> ToolExecutionResult:
        return self._execute_streaming(
            call,
            emit_delta,
            event_context=event_context,
            record_event=record_event,
            runtime_context=runtime_context,
        )

    def _execute_streaming(
        self,
        call: ToolCall,
        emit_delta: Callable[[str], None],
        *,
        event_context: EventContext | None = None,
        record_event: Callable[[AgentEvent], AgentEvent] | None = None,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> ToolExecutionResult:
        max_output = _max_output_chars_arg(call.arguments)
        builder = _StreamingTerminalJsonBuilder(emit_delta, max_output_chars=max_output)
        result = self._execute(
            call,
            output_callback=builder.emit_output_delta,
            event_context=event_context,
            record_event=record_event,
            runtime_context=runtime_context,
        )
        return builder.finish(result)

    def _execute(
        self,
        call: ToolCall,
        *,
        output_callback: Callable[[str], None] | None = None,
        event_context: EventContext | None = None,
        record_event: Callable[[AgentEvent], AgentEvent] | None = None,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> ToolExecutionResult:
        command = required_str_arg(call.arguments, "command")
        workdir = str_arg(call.arguments, "workdir")
        session_id = str_arg(call.arguments, "terminal_session_id") or "default"
        if _terminal_access_off(runtime_context=runtime_context, permission_state=self.permission_state):
            return self._blocked_result(
                call,
                command=command,
                session_id=session_id,
                error="terminal is disabled by permission policy",
                policy_code="terminal_access_off",
            )
        if "max_lifetime_seconds" in call.arguments:
            return self._blocked_result(
                call,
                command=command,
                session_id=session_id,
                error=(
                    "max_lifetime_seconds is runtime-only and is not model-facing; "
                    "use terminal_process.kill to stop a yielded process"
                ),
            )
        removed_args = sorted(
            {"background", "timeout_seconds", "session_id", "notify_on_complete"} & call.arguments.keys()
        )
        if removed_args:
            return self._blocked_result(
                call,
                command=command,
                session_id=session_id,
                error=(
                    f"terminal arguments are no longer supported: {', '.join(removed_args)}; "
                    "use yield_time_ms and terminal_process instead"
                ),
            )
        yield_time_ms = int_arg(call.arguments, "yield_time_ms", 10_000)
        max_output = _max_output_chars_arg(call.arguments)
        tty = bool_arg(call.arguments, "tty", False)
        try:
            terminal_session = self.terminal_sessions.get_or_create(
                session_id,
                owner_host_session_id=self.owner_host_session_id,
                owner_conversation_id=self.owner_conversation_id,
            )
        except ValueError as exc:
            return self._blocked_result(call, command=command, session_id=session_id, error=str(exc))

        metadata: dict[str, Any] = {}
        if output_callback is not None:
            metadata["output_callback"] = output_callback
        if event_context is not None and record_event is not None:
            metadata["origin_event_context"] = event_context
            metadata["tool_call_id"] = call.id
            metadata["record_event"] = record_event

        result = terminal_session.execute(
            TerminalRequest(
                command=command,
                workdir=workdir,
                yield_time_ms=yield_time_ms,
                max_output_chars=max_output,
                tty=tty,
                metadata=metadata,
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
                "env": result.metadata.get("env"),
            },
            artifact_candidates=terminal_artifact_candidates(result),
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
                    "yielded_to_background": False,
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
        "yielded_to_background": result.status is TerminalStatus.RUNNING and result.process_id is not None,
        "terminal_session_id": terminal_session_id,
        "backend_type": backend_type,
        "io_mode": result.metadata.get("io_mode"),
    }
    if "command" in result.metadata:
        payload["command"] = result.metadata.get("command")
    if "duration_seconds" in result.metadata:
        payload["duration_seconds"] = result.metadata.get("duration_seconds")
    if "stdin_closed" in result.metadata:
        payload["stdin_closed"] = result.metadata.get("stdin_closed")
    if "policy_code" in result.metadata:
        payload["policy_code"] = result.metadata.get("policy_code")
    if "suggested_args" in result.metadata:
        payload["suggested_args"] = result.metadata.get("suggested_args") or {}
    if "shell" in result.metadata:
        payload["shell"] = result.metadata.get("shell")
    if "env" in result.metadata:
        payload["env"] = result.metadata.get("env") or {}
    return payload


def terminal_artifact_candidates(result) -> tuple[ToolResultArtifactCandidate, ...]:
    text = getattr(result, "full_output_text", None)
    if text is None:
        return ()
    return (
        ToolResultArtifactCandidate(
            role="combined_output",
            media_type="text/plain; charset=utf-8",
            text=text,
            redacted=True,
            stored_complete=True,
            metadata={
                "terminal_status": result.status.value,
                "process_id": result.process_id,
                "cwd": result.cwd,
            },
        ),
    )


def _terminal_access_off(
    *,
    runtime_context: ToolRuntimeContext | None,
    permission_state: PermissionState | None,
) -> bool:
    if runtime_context is not None and isinstance(runtime_context.permission_policy, dict):
        return runtime_context.permission_policy.get("terminal_access") == TerminalAccess.OFF.value
    return permission_state is not None and permission_state.policy.terminal is TerminalAccess.OFF


def _tool_result_state(status: TerminalStatus) -> ToolResultState:
    return ToolResultState.SUCCESS if status in {TerminalStatus.SUCCESS, TerminalStatus.RUNNING} else ToolResultState.ERROR


def _max_output_chars_arg(args: dict[str, Any]) -> int:
    return bounded_int_arg(
        args,
        "max_output_chars",
        default=DEFAULT_MAX_OUTPUT_CHARS,
        minimum=MIN_TERMINAL_OUTPUT_CHARS,
        maximum=DEFAULT_MAX_OUTPUT_CHARS,
    )


class _StreamingTerminalJsonBuilder:
    _TRUNCATION_NOTICE = "\n\n... [OUTPUT TRUNCATED - full redacted output available via artifact_read when an artifact is present] ...\n\n"

    def __init__(self, emit_delta: Callable[[str], None], *, max_output_chars: int) -> None:
        self._emit_delta = emit_delta
        self._max_output_chars = effective_terminal_output_cap(max_output_chars) or DEFAULT_MAX_OUTPUT_CHARS
        default_options = ToolResultArtifactOptions()
        huge_preview = min(default_options.huge_preview_chars, self._max_output_chars)
        options_seed = ToolResultArtifactOptions(
            archive_threshold_bytes=default_options.effective_archive_threshold_bytes,
            complete_preview_body_chars=min(default_options.complete_preview_body_chars, self._max_output_chars),
            large_preview_chars=min(default_options.effective_large_preview_chars, self._max_output_chars),
            huge_output_chars=default_options.huge_output_chars,
            huge_preview_chars=huge_preview,
            streaming_live_head_cap_chars=1,
            tool_result_message_context_chars=default_options.effective_tool_result_message_context_chars,
        )
        huge_head_cap = build_adaptive_preview("x" * (default_options.huge_output_chars + 1), options_seed).visible_head_chars
        self._options = ToolResultArtifactOptions(
            archive_threshold_bytes=default_options.effective_archive_threshold_bytes,
            complete_preview_body_chars=min(default_options.complete_preview_body_chars, self._max_output_chars),
            large_preview_chars=min(default_options.effective_large_preview_chars, self._max_output_chars),
            huge_output_chars=default_options.huge_output_chars,
            huge_preview_chars=huge_preview,
            streaming_live_head_cap_chars=max(1, min(default_options.streaming_live_head_cap_chars, huge_head_cap)),
            tool_result_message_context_chars=default_options.effective_tool_result_message_context_chars,
        )
        self._live_head_cap_chars = min(self._max_output_chars, self._options.streaming_live_head_cap_chars)
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
        remaining = max(self._live_head_cap_chars - self._output_chars, 0)
        if len(delta) <= remaining:
            self._emit_preview(delta)
            return
        if remaining:
            self._emit_preview(delta[:remaining])
        self._overflowed = True

    def finish(self, result: ToolExecutionResult) -> ToolExecutionResult:
        payload = json.loads(result.output)
        payload_output = str(payload.get("output") or "")
        full_output = _primary_text_artifact_candidate(result) or str(payload.get("output") or "")
        preview = build_adaptive_preview(full_output, self._options)
        display_output = payload_output if preview.policy == "full" else preview.text
        emitted_head = "".join(self._preview_parts)
        output_suffix = ""
        if self._started:
            if preview.policy == "full":
                output_suffix = payload_output[len(emitted_head):]
            else:
                output_suffix = preview.text[len(emitted_head):] if preview.text.startswith(emitted_head) else (
                    self._TRUNCATION_NOTICE + preview.text[-preview.visible_tail_chars:]
                )
            if output_suffix:
                self._emit_delta(_json_string_fragment(output_suffix))
            payload["output"] = emitted_head + output_suffix
            payload["truncated"] = preview.omitted_middle_chars > 0 or bool(payload.get("truncated"))
            payload["preview_policy"] = preview.policy
            payload["output_preview_chars"] = len(payload["output"])
            payload["output_original_chars"] = preview.original_chars
            payload["output_original_bytes"] = preview.original_bytes
            payload["omitted_middle_chars"] = preview.omitted_middle_chars
            payload["visible_head_chars"] = preview.visible_head_chars
            payload["visible_tail_chars"] = preview.visible_tail_chars
            payload["preview"] = preview.to_metadata().model_dump()
            payload.pop("output", None)
            suffix_payload = json.dumps(payload, ensure_ascii=False)
            self._emit_delta('",' + suffix_payload[1:])
        else:
            if preview.policy != "full":
                payload["output"] = preview.text
                payload["truncated"] = True
                payload["preview_policy"] = preview.policy
                payload["output_preview_chars"] = preview.preview_chars
                payload["output_original_chars"] = preview.original_chars
                payload["output_original_bytes"] = preview.original_bytes
                payload["omitted_middle_chars"] = preview.omitted_middle_chars
                payload["visible_head_chars"] = preview.visible_head_chars
                payload["visible_tail_chars"] = preview.visible_tail_chars
                payload["preview"] = preview.to_metadata().model_dump()
                self._emit_delta(json.dumps(payload, ensure_ascii=False))
            else:
                self._emit_delta(result.output)

        final_payload = json.loads(result.output)
        final_payload["output"] = display_output
        final_payload["truncated"] = preview.omitted_middle_chars > 0 or bool(final_payload.get("truncated"))
        final_payload["preview_policy"] = preview.policy
        final_payload["output_preview_chars"] = preview.preview_chars
        final_payload["output_original_chars"] = preview.original_chars
        final_payload["output_original_bytes"] = preview.original_bytes
        final_payload["omitted_middle_chars"] = preview.omitted_middle_chars
        final_payload["visible_head_chars"] = preview.visible_head_chars
        final_payload["visible_tail_chars"] = preview.visible_tail_chars
        final_payload["preview"] = preview.to_metadata().model_dump()
        output = json.dumps(final_payload, ensure_ascii=False)
        return ToolExecutionResult(
            call_id=result.call_id,
            tool_name=result.tool_name,
            status=result.status,
            output=output,
            metadata={**result.metadata, "streamed_output_complete": True},
            artifact_candidates=result.artifact_candidates,
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


def _primary_text_artifact_candidate(result: ToolExecutionResult) -> str | None:
    for candidate in result.artifact_candidates:
        if candidate.text is not None and candidate.role in {"combined_output", "output"}:
            return candidate.text
    for candidate in result.artifact_candidates:
        if candidate.text is not None:
            return candidate.text
    return None
