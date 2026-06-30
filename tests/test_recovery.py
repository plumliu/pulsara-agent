from __future__ import annotations

import pytest

from pulsara_agent.event import (
    EventContext,
    PlanModeEnteredEvent,
    ReplyEndEvent,
    RunEndEvent,
    RunStartEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime.context import build_llm_context
from pulsara_agent.runtime.plan import PlanWorkflowState
from pulsara_agent.runtime.recovery import (
    AbortKind,
    GuidanceKind,
    GUIDANCE_TEXT_FOR_PROMPT,
    GUIDANCE_TEXT_FOR_TRANSCRIPT,
    InRunRecoveryCause,
    InRunRecoveryState,
    StopRequest,
    ToolSeverity,
    classify_unfinished_tool_calls,
    project_recovery_from_events,
    project_recovery_from_state,
    render_recovery_text,
)
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.tools.registry import ToolRegistry


CTX = EventContext(run_id="run:recovery", turn_id="turn:recovery", reply_id="reply:recovery")


def test_guidance_tables_cover_same_keys() -> None:
    expected = set(GuidanceKind)
    assert set(GUIDANCE_TEXT_FOR_TRANSCRIPT) == expected
    assert set(GUIDANCE_TEXT_FOR_PROMPT) == expected


def test_project_recovery_from_events_failed_run_uses_run_failed_guidance() -> None:
    events = [
        RunStartEvent(**CTX.event_fields(), user_input_chars=12, metadata={"user_input": "do work"}),
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:write", tool_call_name="write_file"),
        ToolResultStartEvent(**CTX.event_fields(), tool_call_id="call:write", tool_call_name="write_file"),
        RunEndEvent(**CTX.event_fields(), status="failed", stop_reason="tool_error"),
    ]

    projection = project_recovery_from_events(events)

    assert projection is not None
    assert projection.run_status == "failed"
    assert projection.abort_kind is None
    assert projection.guidance_kind is GuidanceKind.RUN_FAILED
    assert projection.in_plan_workflow is False
    assert projection.unfinished_tools[0].severity is ToolSeverity.BOUNDED_WRITE


def test_project_recovery_from_events_aborted_plan_turn_uses_plan_aborted_guidance() -> None:
    events = [
        PlanModeEnteredEvent(
            **CTX.event_fields(),
            source="user",
            previous_permission_mode="bypass-permissions",
            previous_permission_policy={"profile": "trusted_host"},
            reason="plan first",
        ),
        RunStartEvent(**CTX.event_fields(), user_input_chars=4, metadata={"user_input": "ask"}),
        ReplyEndEvent(**CTX.event_fields()),
        RunEndEvent(
            **CTX.event_fields(),
            status="aborted",
            stop_reason="aborted",
            abort_kind=AbortKind.USER_STOP.value,
        ),
    ]

    projection = project_recovery_from_events(events)

    assert projection is not None
    assert projection.run_status == "aborted"
    assert projection.abort_kind is AbortKind.USER_STOP
    assert projection.in_plan_workflow is True
    assert projection.guidance_kind is GuidanceKind.PLAN_ABORTED
    assert "plan workflow turn was stopped by the user" in render_recovery_text(
        projection,
        audience="transcript",
    )


def test_project_recovery_from_events_late_tool_result_preserves_completed_semantics() -> None:
    events = [
        RunStartEvent(**CTX.event_fields(), user_input_chars=10, metadata={"user_input": "run command"}),
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:terminal", tool_call_name="terminal"),
        ToolResultStartEvent(**CTX.event_fields(), tool_call_id="call:terminal", tool_call_name="terminal"),
        RunEndEvent(
            **CTX.event_fields(),
            status="aborted",
            stop_reason="aborted",
            abort_kind=AbortKind.USER_STOP.value,
        ),
        ToolResultEndEvent(**CTX.event_fields(), tool_call_id="call:terminal", state=ToolResultState.SUCCESS),
    ]

    projection = project_recovery_from_events(events)

    assert projection is not None
    assert projection.guidance_kind is GuidanceKind.USER_ABORTED
    assert projection.unfinished_tools == ()


def test_project_recovery_from_state_uses_in_run_step_failed_guidance() -> None:
    state = LoopState(session_id="runtime:test")
    state.in_run_recovery = InRunRecoveryState(
        cause=InRunRecoveryCause.TOOL_FAILURE,
        consecutive_failures=1,
    )
    state.scratchpad["plan_state"] = PlanWorkflowState(active=True)

    projection = project_recovery_from_state(state)

    assert projection is not None
    assert projection.run_status is None
    assert projection.guidance_kind is GuidanceKind.IN_RUN_STEP_FAILED
    assert projection.in_plan_workflow is True
    assert "Recover by inspecting the latest observation" in render_recovery_text(
        projection,
        audience="prompt",
    )


def test_build_llm_context_appends_prompt_text_from_in_run_projection() -> None:
    state = LoopState(session_id="runtime:test")
    state.in_run_recovery = InRunRecoveryState(
        cause=InRunRecoveryCause.MODEL_FAILURE,
        consecutive_failures=1,
    )

    context = build_llm_context(
        state=state,
        registry=ToolRegistry(),
        system_prompt="System",
        budget=state.budget,
    )

    assert context.messages[-1].role.value == "user"
    assert "Recover by inspecting the latest observation" in "\n".join(context.messages[-1].content)


def test_typed_recovery_control_state_rejects_ambiguous_values() -> None:
    request = StopRequest(reason=AbortKind.USER_STOP)
    recovery = InRunRecoveryState(
        cause=InRunRecoveryCause.MODEL_FAILURE,
        consecutive_failures=2,
    )

    assert request.reason is AbortKind.USER_STOP
    assert recovery.cause is InRunRecoveryCause.MODEL_FAILURE
    with pytest.raises(ValueError, match="consecutive_failures"):
        InRunRecoveryState(
            cause=InRunRecoveryCause.TOOL_FAILURE,
            consecutive_failures=0,
        )


def test_classify_unfinished_tool_calls_omits_completed_terminal_after_late_result() -> None:
    events = [
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:terminal", tool_call_name="terminal"),
        ToolResultStartEvent(**CTX.event_fields(), tool_call_id="call:terminal", tool_call_name="terminal"),
        ToolResultEndEvent(**CTX.event_fields(), tool_call_id="call:terminal", state=ToolResultState.SUCCESS),
    ]

    assert classify_unfinished_tool_calls(events) == []
