"""Producer-declared terminal business snapshots, without display truncation."""

from __future__ import annotations

from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolBusinessValue, ToolExecutionResult
from pulsara_agent.primitives.tool_observation import (
    TrustedToolObservationSupplement,
    normalize_observation_duration,
)
from pulsara_agent.terminal_process import TerminalResult


def _terminal_process_payload(native):
    return {
        "target": {"kind": "PROCESS", "process_id": native.process_id},
        "command": native.command,
        "cwd": native.cwd,
        "process_status": "RUNNING" if native.exit_code is None else "EXITED",
        "exit_code": native.exit_code,
        "stdin_closed": native.stdin_closed,
        "duration_seconds": native.duration_seconds,
    }


def _terminal_execution_result(
    call,
    result: TerminalResult,
    *,
    action="start",
    acceptance_succeeded=None,
    control_text=None,
):
    fields = {}
    if result.process_id is not None:
        fields["target"] = {"kind": "PROCESS", "process_id": result.process_id}
    unavailable = result.output_disposition.value == "UNAVAILABLE"
    complete = result.process_id is not None and not unavailable
    success = complete and not result.cancelled
    parts = []
    if success:
        fields["meta"] = {
            "process_status": "RUNNING"
            if result.status.value == "running"
            else "EXITED",
            "exit_code": None if result.status.value == "running" else result.exit_code,
            "cwd": result.cwd,
        }
        if action in {"close_stdin", "kill"}:
            parts.append(ResultPart("text", control_text or "进程控制操作已完成。"))
        elif result.output:
            parts.append(ResultPart("text", result.output))
    else:
        if result.output:
            parts.append(ResultPart("text", result.output))
        reason = (
            "公开日志暂存或处理失败，未取得完整观察；命令不会自动重做。"
            if unavailable
            else "本次启动观察已取消，进程已按实际取消路径结算。"
            if result.cancelled
            else result.error or "无法启动进程。"
        )
        if result.process_id is not None:
            reason += f" 工作目录：{result.cwd}；进程状态：{result.status.value}；实际退出码：{result.exit_code}。"
        parts.append(ResultPart("text", reason))
    duration = (
        normalize_observation_duration(result.trusted_process_duration_microseconds)
        if action in {"start", "wait"}
        else None
    )
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=ToolResultState.INTERRUPTED
        if result.cancelled
        else ToolResultState.SUCCESS
        if success
        else ToolResultState.ERROR,
        business_value=ToolBusinessValue(fields, tuple(parts)),
        metadata={"process_id": result.process_id} if result.process_id else {},
        acceptance_succeeded=acceptance_succeeded
        if complete and action in {"start", "wait", "write"}
        else None,
        trusted_observation=TrustedToolObservationSupplement(duration)
        if duration is not None
        else None,
    )


def _success(call, parts):
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=ToolResultState.SUCCESS,
        business_value=ToolBusinessValue({}, tuple(parts)),
    )
