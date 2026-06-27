"""Classify unfinished tool calls for failed/aborted recovery notes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from pulsara_agent.event import (
    AgentEvent,
    RequireUserConfirmEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
)
from pulsara_agent.runtime.plan import PLAN_WORKFLOW_TOOL_NAMES
from pulsara_agent.runtime.permission import FILE_WRITE_TOOL_NAMES, TERMINAL_TOOL_NAMES


class UnfinishedState(StrEnum):
    PENDING_APPROVAL = "pending_approval_not_executed"
    STARTED = "started_no_completed_result"
    AMBIGUOUS = "ambiguous_failed_generation"


class ToolSeverity(StrEnum):
    READ_ONLY = "read_only"
    BOUNDED_WRITE = "bounded_write"
    TERMINAL = "terminal"
    UNKNOWN_EFFECT = "unknown_effect"


@dataclass(frozen=True, slots=True)
class UnfinishedToolCall:
    tool_call_id: str
    tool_name: str
    state: UnfinishedState
    severity: ToolSeverity


_READ_ONLY_TOOL_NAMES = frozenset({"read_file", "search_files", "artifact_read"})
_MAX_LISTED_TOOLS = 3


def classify_unfinished_tool_calls(events: Iterable[AgentEvent]) -> list[UnfinishedToolCall]:
    proposed: dict[str, str] = {}
    completed: set[str] = set()
    attempted: set[str] = set()
    pending: dict[str, str] = {}
    result_start_names: dict[str, str] = {}

    for event in events:
        if isinstance(event, ToolCallStartEvent):
            if event.tool_call_id not in proposed or not proposed[event.tool_call_id]:
                proposed[event.tool_call_id] = event.tool_call_name
        elif isinstance(event, ToolResultStartEvent):
            attempted.add(event.tool_call_id)
            if event.tool_call_name:
                result_start_names[event.tool_call_id] = event.tool_call_name
        elif isinstance(event, ToolResultEndEvent):
            completed.add(event.tool_call_id)
        elif isinstance(event, RequireUserConfirmEvent):
            for block in event.tool_calls:
                pending[block.id] = block.name

    unfinished_ids = [tool_call_id for tool_call_id in proposed if tool_call_id not in completed]
    unfinished: list[UnfinishedToolCall] = []
    for tool_call_id in unfinished_ids:
        tool_name = _resolve_name(
            tool_call_id,
            proposed=proposed,
            pending=pending,
            result_start_names=result_start_names,
        )
        if tool_name in PLAN_WORKFLOW_TOOL_NAMES:
            continue
        state = _classify_state(tool_call_id, attempted=attempted, pending=pending)
        unfinished.append(
            UnfinishedToolCall(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                state=state,
                severity=_classify_severity(tool_name),
            )
        )
    return unfinished


def render_unfinished_summary(
    unfinished: Iterable[UnfinishedToolCall],
    *,
    run_status: str,
) -> str:
    visible = [call for call in unfinished if _wording_for(call) is not None]
    if not visible:
        return ""
    wording = _most_conservative_wording(visible)
    tools = _format_tool_names(visible)
    status_label = "failed" if run_status == "failed" else "interrupted"
    return f" Unfinished tool call(s) from the {status_label} turn: {tools}. {wording}"


def _resolve_name(
    tool_call_id: str,
    *,
    proposed: dict[str, str],
    pending: dict[str, str],
    result_start_names: dict[str, str],
) -> str:
    for candidate in (
        proposed.get(tool_call_id, ""),
        pending.get(tool_call_id, ""),
        result_start_names.get(tool_call_id, ""),
    ):
        if candidate:
            return candidate
    return ""


def _classify_state(
    tool_call_id: str,
    *,
    attempted: set[str],
    pending: dict[str, str],
) -> UnfinishedState:
    if tool_call_id in attempted:
        return UnfinishedState.STARTED
    if tool_call_id in pending:
        return UnfinishedState.PENDING_APPROVAL
    return UnfinishedState.AMBIGUOUS


def _classify_severity(tool_name: str) -> ToolSeverity:
    if not tool_name:
        return ToolSeverity.UNKNOWN_EFFECT
    if tool_name in TERMINAL_TOOL_NAMES:
        return ToolSeverity.TERMINAL
    if tool_name in FILE_WRITE_TOOL_NAMES:
        return ToolSeverity.BOUNDED_WRITE
    if tool_name in _READ_ONLY_TOOL_NAMES:
        return ToolSeverity.READ_ONLY
    return ToolSeverity.UNKNOWN_EFFECT


def _format_tool_names(unfinished: list[UnfinishedToolCall]) -> str:
    names = [call.tool_name or "unknown_tool" for call in unfinished]
    listed = ", ".join(names[:_MAX_LISTED_TOOLS])
    remaining = len(names) - _MAX_LISTED_TOOLS
    if remaining <= 0:
        return listed
    return f"{listed}, +{remaining} more"


def _most_conservative_wording(unfinished: list[UnfinishedToolCall]) -> str:
    ranked = sorted(
        ((_wording_rank(call), _wording_for(call)) for call in unfinished),
        key=lambda item: item[0],
        reverse=True,
    )
    wording = ranked[0][1]
    assert wording is not None
    return wording


def _wording_rank(call: UnfinishedToolCall) -> int:
    if call.state is UnfinishedState.STARTED and call.severity is ToolSeverity.TERMINAL:
        return 100
    if call.state is UnfinishedState.STARTED and call.severity is ToolSeverity.BOUNDED_WRITE:
        return 90
    if call.severity is ToolSeverity.UNKNOWN_EFFECT:
        return 80
    if call.state is UnfinishedState.AMBIGUOUS and call.severity is ToolSeverity.TERMINAL:
        return 75
    if call.state is UnfinishedState.AMBIGUOUS and call.severity is ToolSeverity.BOUNDED_WRITE:
        return 70
    if call.state is UnfinishedState.PENDING_APPROVAL:
        return 60
    if call.state is UnfinishedState.STARTED and call.severity is ToolSeverity.READ_ONLY:
        return 50
    return 0


def _wording_for(call: UnfinishedToolCall) -> str | None:
    if call.severity is ToolSeverity.UNKNOWN_EFFECT:
        return "The previous turn proposed a tool call whose effect is unknown; verify before continuing."
    if call.state is UnfinishedState.PENDING_APPROVAL:
        if call.severity is ToolSeverity.READ_ONLY:
            return None
        return "It was pending approval and did not execute."
    if call.state is UnfinishedState.STARTED:
        if call.severity is ToolSeverity.TERMINAL:
            return "It may have partially run and may still be running in the background; verify before continuing."
        if call.severity is ToolSeverity.BOUNDED_WRITE:
            return "It may have partially run; re-read to verify."
        if call.severity is ToolSeverity.READ_ONLY:
            return "It may not have completed; inspect or retry if needed."
    if call.state is UnfinishedState.AMBIGUOUS:
        if call.severity is ToolSeverity.TERMINAL:
            return "It was proposed; uncertain whether it ran; verify before continuing."
        if call.severity is ToolSeverity.BOUNDED_WRITE:
            return "It was proposed but uncertain; re-evaluate before continuing."
    return None
