"""Structured failed/aborted recovery semantics shared by host and runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Literal

from pulsara_agent.event import (
    AgentEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
)
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.runtime.tool_taxonomy import (
    FILE_WRITE_TOOL_NAMES,
    PLAN_WORKFLOW_TOOL_NAMES,
    READ_ONLY_RECOVERY_TOOL_NAMES,
    TERMINAL_TOOL_NAMES,
)


class UnfinishedState(StrEnum):
    PENDING_APPROVAL = "pending_approval_not_executed"
    STARTED = "started_no_completed_result"
    AMBIGUOUS = "ambiguous_failed_generation"


class ToolSeverity(StrEnum):
    READ_ONLY = "read_only"
    BOUNDED_WRITE = "bounded_write"
    TERMINAL = "terminal"
    UNKNOWN_EFFECT = "unknown_effect"


class AbortKind(StrEnum):
    USER_STOP = "user_stop"
    HOST_TEARDOWN = "host_teardown"


@dataclass(frozen=True, slots=True)
class StopRequest:
    reason: AbortKind


class InRunRecoveryCause(StrEnum):
    MODEL_FAILURE = "model_failure"
    TOOL_FAILURE = "tool_failure"


@dataclass(frozen=True, slots=True)
class InRunRecoveryState:
    cause: InRunRecoveryCause
    consecutive_failures: int

    def __post_init__(self) -> None:
        if self.consecutive_failures < 1:
            raise ValueError("consecutive_failures must be positive")


class GuidanceKind(StrEnum):
    RUN_FAILED = "run_failed"
    USER_ABORTED = "user_aborted"
    PLAN_ABORTED = "plan_aborted"
    HOST_TEARDOWN = "host_teardown"
    IN_RUN_STEP_FAILED = "in_run_step_failed"


@dataclass(frozen=True, slots=True)
class UnfinishedToolCall:
    tool_call_id: str
    tool_name: str
    state: UnfinishedState
    severity: ToolSeverity


@dataclass(frozen=True, slots=True)
class RecoveryProjection:
    run_status: Literal["failed", "aborted"] | None
    abort_kind: AbortKind | None
    unfinished_tools: tuple[UnfinishedToolCall, ...]
    in_plan_workflow: bool
    guidance_kind: GuidanceKind


FAILURE_NOTE_TEXT = (
    "Pulsara note: the previous turn did not complete because the runtime/provider step "
    "failed. The user's input above was preserved. Any assistant text above from that turn "
    "may be partial or empty; if the user asks to continue, continue from the preserved input."
)

INTERRUPTED_NOTE_TEXT = (
    "Pulsara note: the previous turn was stopped by the user. The user's input from that turn "
    "was preserved. Any assistant text or tool work from that turn may be partial; if the user "
    "asks to continue, continue from the preserved input."
)

PLAN_ABORTED_NOTE_TEXT = (
    "Pulsara note: the previous plan workflow turn was stopped by the user. Planning remains "
    "active and read-only, and the user's input from that turn was preserved. Any assistant text "
    "or tool work from that turn may be partial; if the user asks to continue planning, continue "
    "from the preserved input."
)

HOST_TEARDOWN_NOTE_TEXT = (
    "Pulsara note: the previous host session was closed before that turn completed. "
    "This was a host lifecycle teardown, not a user stop. Any assistant text or tool "
    "work from that turn may be partial; verify external state before continuing."
)

IN_RUN_STEP_FAILED_TRANSCRIPT_TEXT = (
    "Pulsara note: a recoverable step failed. Inspect the latest observation and continue carefully."
)

GUIDANCE_TEXT_FOR_TRANSCRIPT: dict[GuidanceKind, str] = {
    GuidanceKind.RUN_FAILED: FAILURE_NOTE_TEXT,
    GuidanceKind.USER_ABORTED: INTERRUPTED_NOTE_TEXT,
    GuidanceKind.PLAN_ABORTED: PLAN_ABORTED_NOTE_TEXT,
    GuidanceKind.HOST_TEARDOWN: HOST_TEARDOWN_NOTE_TEXT,
    GuidanceKind.IN_RUN_STEP_FAILED: IN_RUN_STEP_FAILED_TRANSCRIPT_TEXT,
}

GUIDANCE_TEXT_FOR_PROMPT: dict[GuidanceKind, str] = {
    GuidanceKind.RUN_FAILED: (
        "The previous run failed. Inspect the latest observation and either retry with corrected "
        "tool arguments or provide a final answer."
    ),
    GuidanceKind.USER_ABORTED: (
        "The previous run was stopped by the user. If the user asks to continue, continue from "
        "the preserved input and verify any partial tool work before proceeding."
    ),
    GuidanceKind.PLAN_ABORTED: (
        "The previous plan turn was stopped by the user, but plan mode is still active and "
        "read-only. Continue planning from the preserved input; do not implement changes until "
        "exit_plan is approved."
    ),
    GuidanceKind.HOST_TEARDOWN: (
        "The previous host session closed before the run completed. Treat any partial tool "
        "work as uncertain and verify external state before retrying or continuing."
    ),
    GuidanceKind.IN_RUN_STEP_FAILED: (
        "The previous model/tool step failed. Recover by inspecting the latest observation and "
        "either retry with corrected tool arguments or provide a final answer."
    ),
}


RECOVERABLE_RUN_STATUSES = frozenset({"failed", "aborted"})
RECOVERY_NOTE_KIND_BY_STATUS: dict[str, Literal["previous_turn_failed", "previous_turn_aborted"]] = {
    "failed": "previous_turn_failed",
    "aborted": "previous_turn_aborted",
}
RECOVERY_NOTE_ID_PREFIX_BY_STATUS = {
    "failed": "failed-run-note",
    "aborted": "aborted-run-note",
}

_MAX_LISTED_TOOLS = 3


def classify_unfinished_tool_calls(events: Iterable[AgentEvent]) -> list[UnfinishedToolCall]:
    proposed: dict[str, str] = {}
    completed: set[str] = set()
    attempted: set[str] = set()
    pending: set[str] = set()

    for event in events:
        if isinstance(event, ToolCallStartEvent):
            proposed.setdefault(event.tool_call_id, event.tool_call_name)
        elif isinstance(event, ToolResultStartEvent):
            attempted.add(event.tool_call_id)
        elif isinstance(event, ToolResultEndEvent):
            completed.add(event.tool_call_id)
        elif isinstance(event, RequireUserConfirmEvent):
            for block in event.tool_calls:
                pending.add(block.id)

    unfinished_ids = [tool_call_id for tool_call_id in proposed if tool_call_id not in completed]
    unfinished: list[UnfinishedToolCall] = []
    for tool_call_id in unfinished_ids:
        tool_name = proposed[tool_call_id]
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


def project_recovery_from_events(events: Iterable[AgentEvent]) -> RecoveryProjection | None:
    from pulsara_agent.runtime.plan import reduce_plan_workflow_state

    all_events = list(events)
    target_run_end = _last_recoverable_run_end(all_events)
    if target_run_end is None:
        return None
    run_events_all = [event for event in all_events if event.run_id == target_run_end.run_id]
    through_target = _events_through_target_run_end(all_events, target_run_end)
    in_plan_workflow = reduce_plan_workflow_state(through_target).active
    abort_kind = _parse_abort_kind(target_run_end.abort_kind)
    if target_run_end.status == "failed":
        guidance_kind = GuidanceKind.RUN_FAILED
    elif abort_kind is AbortKind.HOST_TEARDOWN:
        guidance_kind = GuidanceKind.HOST_TEARDOWN
    elif in_plan_workflow:
        guidance_kind = GuidanceKind.PLAN_ABORTED
    else:
        guidance_kind = GuidanceKind.USER_ABORTED
    return RecoveryProjection(
        run_status=target_run_end.status,  # type: ignore[arg-type]
        abort_kind=abort_kind,
        unfinished_tools=tuple(classify_unfinished_tool_calls(run_events_all)),
        in_plan_workflow=in_plan_workflow,
        guidance_kind=guidance_kind,
    )


def project_recovery_from_state(state: LoopState) -> RecoveryProjection | None:
    if state.in_run_recovery is None:
        return None
    return RecoveryProjection(
        run_status=None,
        abort_kind=state.abort_kind,
        unfinished_tools=(),
        in_plan_workflow=_plan_active_from_state(state),
        guidance_kind=GuidanceKind.IN_RUN_STEP_FAILED,
    )


def render_recovery_text(
    projection: RecoveryProjection,
    *,
    audience: Literal["transcript", "prompt"],
) -> str:
    table = (
        GUIDANCE_TEXT_FOR_TRANSCRIPT
        if audience == "transcript"
        else GUIDANCE_TEXT_FOR_PROMPT
    )
    text = table[projection.guidance_kind]
    if projection.run_status is None:
        return text
    return text + render_unfinished_summary(
        projection.unfinished_tools,
        run_status=projection.run_status,
    )


def _last_recoverable_run_end(events: list[AgentEvent]) -> RunEndEvent | None:
    last_run_end: RunEndEvent | None = None
    for event in events:
        if isinstance(event, RunEndEvent):
            last_run_end = event
    if last_run_end is None or last_run_end.status not in RECOVERABLE_RUN_STATUSES:
        return None
    return last_run_end


def _events_through_target_run_end(
    events: list[AgentEvent],
    target_run_end: RunEndEvent,
) -> list[AgentEvent]:
    if target_run_end.sequence is None:
        target_index = events.index(target_run_end)
        return events[: target_index + 1]
    return [
        event
        for event in events
        if event.sequence is None or event.sequence <= target_run_end.sequence
    ]


def _parse_abort_kind(value: str | None) -> AbortKind | None:
    if value is None:
        return None
    try:
        return AbortKind(value)
    except ValueError:
        return None


def _plan_active_from_state(state: LoopState) -> bool:
    plan_state = state.scratchpad.get("plan_state")
    if plan_state is not None and getattr(plan_state, "active", None) is not None:
        return plan_state.active
    return bool(state.scratchpad.get("plan_active"))


def _classify_state(
    tool_call_id: str,
    *,
    attempted: set[str],
    pending: set[str],
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
    if tool_name in READ_ONLY_RECOVERY_TOOL_NAMES:
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
