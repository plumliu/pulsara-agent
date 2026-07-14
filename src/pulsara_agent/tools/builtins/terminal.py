"""Generic terminal built-in tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from pulsara_agent.event import AgentEvent, EventContext
from pulsara_agent.message import ToolResultState
from pulsara_agent.capability.result_semantics import (
    FrozenToolResultSemanticsRuntimeInput,
    build_terminal_payload_timing,
    unbounded_error_preview,
)
from pulsara_agent.primitives.tool_result import (
    TerminalCommandDomainSubmissionFact,
    TerminalCommandErrorDomainSubmissionFact,
    ToolResultRenderVariantCode,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)
from pulsara_agent.runtime.permission import PermissionState, TerminalAccess
from pulsara_agent.runtime.terminal import (
    TerminalRequest,
    TerminalSessionManager,
    TerminalStatus,
)
from pulsara_agent.runtime.tool_artifacts import (
    ToolResultArtifactOptions,
    build_adaptive_preview,
    effective_terminal_output_cap,
)
from pulsara_agent.tools.base import (
    ToolCall,
    ToolExecutionResult,
    ToolResultArtifactCandidate,
    ToolRuntimeContext,
)
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
    parameters: dict[str, Any] = field(
        default_factory=lambda: object_schema(
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
                "max_output_chars": {
                    "type": "integer",
                    "default": DEFAULT_MAX_OUTPUT_CHARS,
                },
            },
            required=["command"],
        )
    )
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __post_init__(self) -> None:
        WorkspaceTool.__post_init__(self)
        if self.terminal_sessions is None:
            self.terminal_sessions = TerminalSessionManager(self.workspace_root)

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        return self._execute(call)

    def execute_streaming(
        self, call: ToolCall, emit_delta: Callable[[str], None]
    ) -> ToolExecutionResult:
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
        if _terminal_access_off(
            runtime_context=runtime_context, permission_state=self.permission_state
        ):
            return self._blocked_result(
                call,
                command=command,
                session_id=session_id,
                error="terminal is disabled by permission policy",
                policy_code="terminal_access_off",
                variant_code=ToolResultRenderVariantCode.TERMINAL_COMMAND_DENIED,
                failure_stage="permission_denied",
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
                variant_code=ToolResultRenderVariantCode.TERMINAL_COMMAND_MALFORMED_ARGUMENTS,
                failure_stage="malformed_arguments",
            )
        removed_args = sorted(
            {"background", "timeout_seconds", "session_id", "notify_on_complete"}
            & call.arguments.keys()
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
                variant_code=ToolResultRenderVariantCode.TERMINAL_COMMAND_MALFORMED_ARGUMENTS,
                failure_stage="malformed_arguments",
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
            return self._blocked_result(
                call,
                command=command,
                session_id=session_id,
                error=str(exc),
                variant_code=ToolResultRenderVariantCode.TERMINAL_COMMAND_ADAPTER_ERROR,
                failure_stage="adapter_initialization",
            )

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
        timing = terminal_timing_payload(
            duration_seconds=_float_or_none(result.metadata.get("duration_seconds")),
            freshness=(
                "background_process_observation"
                if result.status is TerminalStatus.RUNNING
                and result.process_id is not None
                else "current_tool_observation"
            ),
        )
        payload = terminal_result_payload(
            result,
            terminal_session_id=terminal_session.session_id,
            backend_type=terminal_session.state.backend_type.value,
            timing=timing,
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
                "timing": timing,
            },
            artifact_candidates=terminal_artifact_candidates(result, timing=timing),
            display_payload=freeze_tool_display_payload(payload),
            semantics_input=FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_COMMAND_EXECUTED,
                domain_submission=TerminalCommandDomainSubmissionFact(
                    command=command,
                    status=result.status.value,
                    exit_code=result.exit_code,
                    cwd=result.cwd,
                    timed_out=result.timed_out,
                    output_truncated=result.truncated,
                    error=(
                        unbounded_error_preview(result.error) if result.error else None
                    ),
                    process_id=result.process_id,
                    yielded_to_background=(
                        result.status is TerminalStatus.RUNNING
                        and result.process_id is not None
                    ),
                    terminal_session_id=terminal_session.session_id,
                    backend_type=terminal_session.state.backend_type.value,
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
                    duration_seconds=_float_or_none(
                        result.metadata.get("duration_seconds")
                    ),
                ),
            ),
            terminal_payload_timing=terminal_payload_timing_fact(timing),
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
        variant_code: ToolResultRenderVariantCode = (
            ToolResultRenderVariantCode.TERMINAL_COMMAND_ADAPTER_ERROR
        ),
        failure_stage: str = "adapter_initialization",
    ) -> ToolExecutionResult:
        timing = terminal_timing_payload(freshness="current_tool_observation")
        payload = {
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
            "timing": timing,
        }
        return self._result(
            call,
            status=ToolResultState.ERROR,
            output=json.dumps(payload, ensure_ascii=False),
            metadata={
                "command": command,
                "exit_code": -1,
                "cwd": str(self.workspace_root),
                "timed_out": False,
                "truncated": False,
                "process_id": None,
                "terminal_session_id": session_id,
                "backend_type": "local",
                "timing": timing,
            },
            display_payload=freeze_tool_display_payload(payload),
            semantics_input=FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=variant_code,
                domain_submission=TerminalCommandErrorDomainSubmissionFact(
                    requested_command=command,
                    failure_stage=failure_stage,
                    status=payload_status,
                    error=unbounded_error_preview(error),
                    policy_code=policy_code,
                    observed_cwd=None,
                    terminal_session_id=None,
                    backend_type=None,
                    io_mode=None,
                ),
            ),
            terminal_payload_timing=None,
        )


def terminal_timing_payload(
    *,
    observed_at: str | None = None,
    duration_seconds: float | int | None = None,
    freshness: str,
    clock_source: str = "tool_payload",
    command_started_at: str | None = None,
    process_started_at: str | None = None,
    last_output_at: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "observed_at": observed_at or _utc_now_z(),
        "freshness": freshness,
        "clock_source": clock_source,
    }
    duration = _float_or_none(duration_seconds)
    if duration is not None:
        payload["duration_seconds"] = duration
    if command_started_at:
        payload["command_started_at"] = command_started_at
    if process_started_at:
        payload["process_started_at"] = process_started_at
    if last_output_at:
        payload["last_output_at"] = last_output_at
    return payload


def freeze_tool_display_payload(payload: dict[str, Any]) -> FrozenJsonObjectFact:
    frozen = freeze_json(payload)
    if not isinstance(frozen, FrozenJsonObjectFact):
        raise AssertionError("terminal display payload must freeze as an object")
    return frozen


def terminal_timing_metadata_subset(timing: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(timing, dict):
        return {}
    return {
        key: timing[key]
        for key in ("observed_at", "duration_seconds", "freshness", "clock_source")
        if key in timing
    }


def terminal_payload_timing_fact(timing: dict[str, Any]):
    return build_terminal_payload_timing(
        observed_at_utc=str(timing["observed_at"]),
        duration_seconds=_float_or_none(timing.get("duration_seconds")),
        freshness=str(timing["freshness"]),
        clock_source=str(timing["clock_source"]),
        command_started_at_utc=(
            str(timing["command_started_at"])
            if timing.get("command_started_at") is not None
            else None
        ),
        process_started_at_utc=(
            str(timing["process_started_at"])
            if timing.get("process_started_at") is not None
            else None
        ),
        last_output_at_utc=(
            str(timing["last_output_at"])
            if timing.get("last_output_at") is not None
            else None
        ),
    )


def terminal_result_payload(
    result,
    *,
    terminal_session_id: str,
    backend_type: str,
    timing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result_timing = timing or _metadata_timing(getattr(result, "metadata", {}))
    payload = {
        "status": result.status.value,
        "output": result.output,
        "exit_code": result.exit_code,
        "cwd": result.cwd,
        "timed_out": result.timed_out,
        "truncated": result.truncated,
        "error": result.error,
        "process_id": result.process_id,
        "yielded_to_background": result.status is TerminalStatus.RUNNING
        and result.process_id is not None,
        "terminal_session_id": terminal_session_id,
        "backend_type": backend_type,
        "io_mode": result.metadata.get("io_mode"),
    }
    if result_timing is not None:
        payload["timing"] = dict(result_timing)
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


def terminal_artifact_candidates(
    result,
    *,
    timing: dict[str, Any] | None = None,
) -> tuple[ToolResultArtifactCandidate, ...]:
    text = getattr(result, "full_output_text", None)
    if text is None:
        return ()
    result_timing = timing or _metadata_timing(getattr(result, "metadata", {}))
    metadata: dict[str, Any] = {
        "terminal_status": result.status.value,
        "process_id": result.process_id,
        "cwd": result.cwd,
    }
    timing_subset = terminal_timing_metadata_subset(result_timing)
    if timing_subset:
        metadata["timing"] = timing_subset
    return (
        ToolResultArtifactCandidate(
            role="combined_output",
            media_type="text/plain; charset=utf-8",
            text=text,
            redacted=True,
            stored_complete=True,
            metadata=metadata,
        ),
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


def _tool_result_state(status: TerminalStatus) -> ToolResultState:
    return (
        ToolResultState.SUCCESS
        if status in {TerminalStatus.SUCCESS, TerminalStatus.RUNNING}
        else ToolResultState.ERROR
    )


def _max_output_chars_arg(args: dict[str, Any]) -> int:
    return bounded_int_arg(
        args,
        "max_output_chars",
        default=DEFAULT_MAX_OUTPUT_CHARS,
        minimum=MIN_TERMINAL_OUTPUT_CHARS,
        maximum=DEFAULT_MAX_OUTPUT_CHARS,
    )


def _utc_now_z() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _metadata_timing(metadata: object) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    timing = metadata.get("timing")
    return dict(timing) if isinstance(timing, dict) else None


class _StreamingTerminalJsonBuilder:
    _TRUNCATION_NOTICE = "\n\n... [OUTPUT TRUNCATED - full redacted output available via artifact_read when an artifact is present] ...\n\n"

    def __init__(
        self, emit_delta: Callable[[str], None], *, max_output_chars: int
    ) -> None:
        self._emit_delta = emit_delta
        self._max_output_chars = (
            effective_terminal_output_cap(max_output_chars) or DEFAULT_MAX_OUTPUT_CHARS
        )
        default_options = ToolResultArtifactOptions()
        huge_preview = min(default_options.huge_preview_chars, self._max_output_chars)
        options_seed = ToolResultArtifactOptions(
            archive_threshold_bytes=default_options.effective_archive_threshold_bytes,
            complete_preview_body_chars=min(
                default_options.complete_preview_body_chars, self._max_output_chars
            ),
            large_preview_chars=min(
                default_options.effective_large_preview_chars, self._max_output_chars
            ),
            huge_output_chars=default_options.huge_output_chars,
            huge_preview_chars=huge_preview,
            streaming_live_head_cap_chars=1,
        )
        huge_head_cap = build_adaptive_preview(
            "x" * (default_options.huge_output_chars + 1), options_seed
        ).visible_head_chars
        self._options = ToolResultArtifactOptions(
            archive_threshold_bytes=default_options.effective_archive_threshold_bytes,
            complete_preview_body_chars=min(
                default_options.complete_preview_body_chars, self._max_output_chars
            ),
            large_preview_chars=min(
                default_options.effective_large_preview_chars, self._max_output_chars
            ),
            huge_output_chars=default_options.huge_output_chars,
            huge_preview_chars=huge_preview,
            streaming_live_head_cap_chars=max(
                1, min(default_options.streaming_live_head_cap_chars, huge_head_cap)
            ),
        )
        self._live_head_cap_chars = min(
            self._max_output_chars, self._options.streaming_live_head_cap_chars
        )
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
        if result.display_payload is None:
            raise ValueError("streaming terminal result requires typed display payload")
        payload = thaw_json(result.display_payload)
        payload_output = str(payload.get("output") or "")
        full_output = _primary_text_artifact_candidate(result) or str(
            payload.get("output") or ""
        )
        preview = build_adaptive_preview(full_output, self._options)
        display_output = payload_output if preview.policy == "full" else preview.text
        emitted_head = "".join(self._preview_parts)
        output_suffix = ""
        if self._started:
            if preview.policy == "full":
                output_suffix = payload_output[len(emitted_head) :]
            else:
                output_suffix = (
                    preview.text[len(emitted_head) :]
                    if preview.text.startswith(emitted_head)
                    else (
                        self._TRUNCATION_NOTICE
                        + preview.text[-preview.visible_tail_chars :]
                    )
                )
            if output_suffix:
                self._emit_delta(_json_string_fragment(output_suffix))
            payload["output"] = emitted_head + output_suffix
            payload["truncated"] = preview.omitted_middle_chars > 0 or bool(
                payload.get("truncated")
            )
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

        final_payload = thaw_json(result.display_payload)
        final_payload["output"] = display_output
        final_payload["truncated"] = preview.omitted_middle_chars > 0 or bool(
            final_payload.get("truncated")
        )
        final_payload["preview_policy"] = preview.policy
        final_payload["output_preview_chars"] = preview.preview_chars
        final_payload["output_original_chars"] = preview.original_chars
        final_payload["output_original_bytes"] = preview.original_bytes
        final_payload["omitted_middle_chars"] = preview.omitted_middle_chars
        final_payload["visible_head_chars"] = preview.visible_head_chars
        final_payload["visible_tail_chars"] = preview.visible_tail_chars
        final_payload["preview"] = preview.to_metadata().model_dump()
        output = json.dumps(final_payload, ensure_ascii=False)
        semantics_input = result.semantics_input
        if isinstance(
            semantics_input, FrozenToolResultSemanticsRuntimeInput
        ) and isinstance(
            semantics_input.domain_submission,
            TerminalCommandDomainSubmissionFact,
        ):
            semantics_input = replace(
                semantics_input,
                domain_submission=semantics_input.domain_submission.model_copy(
                    update={
                        "output_truncated": (
                            preview.omitted_middle_chars > 0
                            or semantics_input.domain_submission.output_truncated
                        )
                    }
                ),
            )
        return ToolExecutionResult(
            call_id=result.call_id,
            tool_name=result.tool_name,
            status=result.status,
            output=output,
            metadata={**result.metadata, "streamed_output_complete": True},
            artifact_candidates=result.artifact_candidates,
            display_payload=freeze_tool_display_payload(final_payload),
            semantics_input=semantics_input,
            terminal_payload_timing=result.terminal_payload_timing,
            semantics=result.semantics,
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
        if candidate.text is not None and candidate.role in {
            "combined_output",
            "output",
        }:
            return candidate.text
    for candidate in result.artifact_candidates:
        if candidate.text is not None:
            return candidate.text
    return None
