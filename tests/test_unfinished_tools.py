from __future__ import annotations

from pulsara_agent.event import (
    EventContext,
    RequireUserConfirmEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
)
from pulsara_agent.host.unfinished_tools import (
    ToolSeverity,
    UnfinishedState,
    classify_unfinished_tool_calls,
    render_unfinished_summary,
)
from pulsara_agent.message import ToolCallBlock, ToolResultState


CTX = EventContext(run_id="run:test", turn_id="turn:test", reply_id="reply:test")


def test_classifies_pending_approval_as_not_executed() -> None:
    events = [
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:write", tool_call_name="write_file"),
        RequireUserConfirmEvent(
            **CTX.event_fields(),
            tool_calls=[ToolCallBlock(id="call:write", name="write_file", input='{"path": "x"}')],
        ),
    ]

    unfinished = classify_unfinished_tool_calls(events)

    assert len(unfinished) == 1
    assert unfinished[0].state is UnfinishedState.PENDING_APPROVAL
    assert unfinished[0].severity is ToolSeverity.BOUNDED_WRITE
    assert "pending approval and did not execute" in render_unfinished_summary(
        unfinished,
        run_status="aborted",
    )


def test_started_call_takes_priority_over_pending_approval() -> None:
    events = [
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:terminal", tool_call_name="terminal"),
        RequireUserConfirmEvent(
            **CTX.event_fields(),
            tool_calls=[ToolCallBlock(id="call:terminal", name="terminal", input='{"command": "pytest"}')],
        ),
        ToolResultStartEvent(**CTX.event_fields(), tool_call_id="call:terminal", tool_call_name="terminal"),
    ]

    unfinished = classify_unfinished_tool_calls(events)
    summary = render_unfinished_summary(unfinished, run_status="aborted")

    assert unfinished[0].state is UnfinishedState.STARTED
    assert unfinished[0].severity is ToolSeverity.TERMINAL
    assert "may have partially run and may still be running in the background" in summary
    assert "did not execute" not in summary


def test_name_falls_back_to_confirmation_or_result_start_event() -> None:
    pending_events = [
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:pending", tool_call_name=""),
        RequireUserConfirmEvent(
            **CTX.event_fields(),
            tool_calls=[ToolCallBlock(id="call:pending", name="write_file", input="{}")],
        ),
    ]
    started_events = [
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:started", tool_call_name=""),
        ToolResultStartEvent(**CTX.event_fields(), tool_call_id="call:started", tool_call_name="terminal"),
    ]

    assert classify_unfinished_tool_calls(pending_events)[0].tool_name == "write_file"
    assert classify_unfinished_tool_calls(started_events)[0].tool_name == "terminal"


def test_completed_call_is_not_unfinished_even_when_result_end_is_late() -> None:
    events = [
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:terminal", tool_call_name="terminal"),
        ToolResultStartEvent(**CTX.event_fields(), tool_call_id="call:terminal", tool_call_name="terminal"),
        ToolResultEndEvent(
            **CTX.event_fields(),
            tool_call_id="call:terminal",
            state=ToolResultState.SUCCESS,
            metadata={"tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}},
        ),
    ]

    assert classify_unfinished_tool_calls(events) == []
    assert render_unfinished_summary([], run_status="aborted") == ""


def test_rendering_uses_conservative_wording_and_truncates_tool_names() -> None:
    events = [
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:read", tool_call_name="read_file"),
        ToolResultStartEvent(**CTX.event_fields(), tool_call_id="call:read", tool_call_name="read_file"),
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:write", tool_call_name="write_file"),
        ToolResultStartEvent(**CTX.event_fields(), tool_call_id="call:write", tool_call_name="write_file"),
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:term", tool_call_name="terminal"),
        ToolResultStartEvent(**CTX.event_fields(), tool_call_id="call:term", tool_call_name="terminal"),
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:unknown", tool_call_name="custom_tool"),
    ]

    summary = render_unfinished_summary(classify_unfinished_tool_calls(events), run_status="failed")

    assert "failed turn" in summary
    assert "read_file, write_file, terminal, +1 more" in summary
    assert "may have partially run and may still be running in the background" in summary


def test_read_only_pending_call_is_omitted_from_summary() -> None:
    events = [
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:read", tool_call_name="read_file"),
        RequireUserConfirmEvent(
            **CTX.event_fields(),
            tool_calls=[ToolCallBlock(id="call:read", name="read_file", input="{}")],
        ),
    ]

    unfinished = classify_unfinished_tool_calls(events)

    assert unfinished[0].severity is ToolSeverity.READ_ONLY
    assert render_unfinished_summary(unfinished, run_status="aborted") == ""


def test_terminal_process_is_terminal_severity_without_action_parsing() -> None:
    events = [
        ToolCallStartEvent(
            **CTX.event_fields(),
            tool_call_id="call:process",
            tool_call_name="terminal_process",
        ),
        ToolResultStartEvent(
            **CTX.event_fields(),
            tool_call_id="call:process",
            tool_call_name="terminal_process",
        ),
    ]

    unfinished = classify_unfinished_tool_calls(events)
    summary = render_unfinished_summary(unfinished, run_status="aborted")

    assert unfinished[0].severity is ToolSeverity.TERMINAL
    assert "may have partially run and may still be running in the background" in summary


def test_classifies_known_tool_severities() -> None:
    expected = {
        "terminal": ToolSeverity.TERMINAL,
        "terminal_process": ToolSeverity.TERMINAL,
        "write_file": ToolSeverity.BOUNDED_WRITE,
        "edit_file": ToolSeverity.BOUNDED_WRITE,
        "read_file": ToolSeverity.READ_ONLY,
        "search_files": ToolSeverity.READ_ONLY,
        "artifact_read": ToolSeverity.READ_ONLY,
    }

    for tool_name, severity in expected.items():
        events = [
            ToolCallStartEvent(
                **CTX.event_fields(),
                tool_call_id=f"call:{tool_name}",
                tool_call_name=tool_name,
            )
        ]

        unfinished = classify_unfinished_tool_calls(events)

        assert unfinished[0].severity is severity


def test_unknown_tool_uses_unknown_effect_wording() -> None:
    events = [
        ToolCallStartEvent(
            **CTX.event_fields(),
            tool_call_id="call:custom",
            tool_call_name="custom_side_effect",
        )
    ]

    unfinished = classify_unfinished_tool_calls(events)
    summary = render_unfinished_summary(unfinished, run_status="failed")

    assert unfinished[0].state is UnfinishedState.AMBIGUOUS
    assert unfinished[0].severity is ToolSeverity.UNKNOWN_EFFECT
    assert "effect is unknown; verify before continuing" in summary


def test_workflow_tools_are_omitted_from_unfinished_recovery_summary() -> None:
    events = [
        ToolCallStartEvent(
            **CTX.event_fields(),
            tool_call_id="call:question",
            tool_call_name="ask_plan_question",
        ),
        ToolCallStartEvent(
            **CTX.event_fields(),
            tool_call_id="call:exit",
            tool_call_name="exit_plan",
        ),
    ]

    unfinished = classify_unfinished_tool_calls(events)

    assert unfinished == []
    assert render_unfinished_summary(unfinished, run_status="aborted") == ""


def test_empty_tool_name_uses_unknown_effect_wording() -> None:
    events = [
        ToolCallStartEvent(
            **CTX.event_fields(),
            tool_call_id="call:empty",
            tool_call_name="",
        )
    ]

    unfinished = classify_unfinished_tool_calls(events)
    summary = render_unfinished_summary(unfinished, run_status="failed")

    assert unfinished[0].tool_name == ""
    assert unfinished[0].severity is ToolSeverity.UNKNOWN_EFFECT
    assert "unknown_tool" in summary
    assert "effect is unknown; verify before continuing" in summary


def test_bounded_write_started_wording_does_not_claim_background_running() -> None:
    events = [
        ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:write", tool_call_name="write_file"),
        ToolResultStartEvent(**CTX.event_fields(), tool_call_id="call:write", tool_call_name="write_file"),
    ]

    summary = render_unfinished_summary(classify_unfinished_tool_calls(events), run_status="aborted")

    assert "may have partially run; re-read to verify" in summary
    assert "still be running in the background" not in summary
