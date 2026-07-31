import asyncio
from pathlib import Path

import pytest
from tests.support.runtime_session import in_memory_runtime_session
from tests.conftest import (
    run_end_contract_fields,
    run_start_permission_fields,
    tool_result_end_contract_fields,
    tool_result_end_candidate,
)
from tests.support import (
    model_call_end_fields,
    model_call_start_fields,
    test_resolved_call,
    test_resolved_call_fact,
)
from tests.support.model_call import prepared_provider_input_bundle_fixture

from tests.support.model_stream import (
    make_text_block_segment_event,
    make_tool_call_arguments_segment_event,
    make_tool_call_end_event,
    make_tool_call_start_event,
)

from pulsara_agent.event import (
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    PlanQuestionAnsweredEvent,
    PlanQuestionAskedEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    RunStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from pulsara_agent.memory import summarize_run_timeline
from pulsara_agent.event import ConfirmResult
from pulsara_agent.message import ToolResultState
from pulsara_agent.message import ToolCallBlock, ToolCallState
from pulsara_agent.replay.timeline import build_run_timeline


CTX = EventContext(
    run_id="run:timeline", turn_id="turn:timeline/001", reply_id="reply:timeline/001"
)


def _run_start() -> RunStartEvent:
    return RunStartEvent(
        **CTX.event_fields(),
        **run_start_permission_fields(
            CTX.run_id,
            user_input="",
            turn_id=CTX.turn_id,
            reply_id=CTX.reply_id,
        ),
        user_input_chars=0,
    )


async def _emit_tool_terminal_projection(
    runtime,
    *,
    tool_call_id: str,
    tool_name: str,
    state: ToolResultState = ToolResultState.SUCCESS,
) -> None:
    candidate = tool_result_end_candidate(
        event_id=f"tool_result_end:{CTX.run_id}:{tool_call_id}",
        run_id=CTX.run_id,
        turn_id=CTX.turn_id,
        reply_id=CTX.reply_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        state=state,
    )
    prepared = await runtime.tool_terminal_projection_service.prepare_batch(
        (candidate,)
    )
    await runtime.emit_many(prepared)


def test_build_run_timeline_summarizes_model_text_and_tool_activity() -> None:
    resolved_call = test_resolved_call_fact()
    events = [
        ReplyStartEvent(**CTX.event_fields(), name="assistant"),
        ModelCallStartEvent(
            **CTX.event_fields(),
            **model_call_start_fields(resolved_call=resolved_call),
        ),
        make_text_block_segment_event(
            **CTX.event_fields(), block_id="text:1", delta="I'll read it."
        ),
        ModelCallEndEvent(
            **CTX.event_fields(),
            **model_call_end_fields(
                input_tokens=1,
                output_tokens=2,
                resolved_call=resolved_call,
            ),
        ),
        make_tool_call_start_event(
            **CTX.event_fields(),
            tool_call_id="call:read",
            tool_call_name="read_file",
        ),
        make_tool_call_arguments_segment_event(
            **CTX.event_fields(),
            tool_call_id="call:read",
            delta='{"path":"note.txt"}',
        ),
        make_tool_call_end_event(**CTX.event_fields(), tool_call_id="call:read"),
        ToolResultStartEvent(
            **CTX.event_fields(),
            tool_call_id="call:read",
            tool_call_name="read_file",
        ),
        ToolResultTextDeltaEvent(
            **CTX.event_fields(), tool_call_id="call:read", delta="hello"
        ),
        ToolResultEndEvent(
            **CTX.event_fields(),
            **tool_result_end_contract_fields("call:read", tool_name="read_file"),
            tool_call_id="call:read",
            state=ToolResultState.SUCCESS,
            metadata={
                "tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}
            },
        ),
        ReplyEndEvent(**CTX.event_fields(), model_terminal_outcome="completed"),
    ]

    timeline = build_run_timeline(
        events,
        runtime_session_id="runtime:timeline:test",
    )

    assert timeline.status == "completed"
    assert [item.kind for item in timeline.items] == [
        "reply",
        "model_call",
        "assistant_text",
        "tool_call",
        "tool_result",
    ]
    assert timeline.items[1].metadata["usage"]["total_tokens"] == 3
    assert timeline.items[2].summary == "I'll read it."
    assert timeline.items[3].metadata["arguments"] == '{"path":"note.txt"}'
    assert timeline.items[4].summary == "hello"
    assert timeline.items[4].status == "success"


def test_build_run_timeline_marks_unresolved_permission_request_waiting_user() -> None:
    runtime = in_memory_runtime_session(Path("."))

    async def run() -> None:
        call = test_resolved_call()
        provider_input = prepared_provider_input_bundle_fixture(
            call.fact,
            context_id="context:timeline",
            model_call_index=1,
            event_context=CTX,
            runtime_session_id=runtime.runtime_session_id,
        )
        start_fields = model_call_start_fields(
            event_id=f"model_call_start:{call.fact.resolved_model_call_id}",
            resolved_call=call.fact,
            context_id="context:timeline",
            model_call_index=1,
        )
        start_fields["provider_input_reference"] = provider_input.committed_reference
        await runtime.emit(ReplyStartEvent(**CTX.event_fields(), name="assistant"))
        await runtime.write_events(
            (
                *provider_input.companion_events,
                ModelCallStartEvent(**CTX.event_fields(), **start_fields),
            )
        )
        for event in [
            make_tool_call_start_event(
                **CTX.event_fields(),
                tool_call_id="call:danger",
                tool_call_name="terminal",
            ),
            make_tool_call_arguments_segment_event(
                **CTX.event_fields(),
                tool_call_id="call:danger",
                delta='{"command":"rm -rf build"}',
            ),
            make_tool_call_end_event(**CTX.event_fields(), tool_call_id="call:danger"),
            ReplyEndEvent(**CTX.event_fields(), model_terminal_outcome="completed"),
            RequireUserConfirmEvent(
                **CTX.event_fields(),
                tool_calls=[
                    ToolCallBlock(
                        id="call:danger",
                        name="terminal",
                        input='{"command":"rm -rf build"}',
                        state=ToolCallState.ASKING,
                    )
                ],
            ),
        ]:
            await runtime.emit(event)

    asyncio.run(run())

    timeline = build_run_timeline(
        runtime.event_log.iter(run_id=CTX.run_id),
        runtime_session_id=runtime.runtime_session_id,
    )

    assert timeline.status == "waiting_user"
    permission_item = next(
        item for item in timeline.items if item.kind == "permission_request"
    )
    assert permission_item.status == "waiting"
    assert permission_item.metadata["tool_call_ids"] == ["call:danger"]


def test_build_run_timeline_clears_waiting_status_after_confirm_result() -> None:
    runtime = in_memory_runtime_session(Path("."))
    tool_call = ToolCallBlock(
        id="call:danger",
        name="terminal",
        input='{"command":"rm -rf build"}',
        state=ToolCallState.ASKING,
    )

    async def run() -> None:
        for event in [
            ReplyStartEvent(**CTX.event_fields(), name="assistant"),
            make_tool_call_start_event(
                **CTX.event_fields(),
                tool_call_id=tool_call.id,
                tool_call_name=tool_call.name,
            ),
            make_tool_call_arguments_segment_event(
                **CTX.event_fields(), tool_call_id=tool_call.id, delta=tool_call.input
            ),
            make_tool_call_end_event(**CTX.event_fields(), tool_call_id=tool_call.id),
            ReplyEndEvent(**CTX.event_fields(), model_terminal_outcome="completed"),
            RequireUserConfirmEvent(**CTX.event_fields(), tool_calls=[tool_call]),
            UserConfirmResultEvent(
                **CTX.event_fields(),
                confirm_results=[ConfirmResult(confirmed=True, tool_call=tool_call)],
            ),
            ToolResultStartEvent(
                **CTX.event_fields(),
                tool_call_id=tool_call.id,
                tool_call_name=tool_call.name,
            ),
            ToolResultTextDeltaEvent(
                **CTX.event_fields(), tool_call_id=tool_call.id, delta="ok"
            ),
        ]:
            await runtime.emit(event)
        await _emit_tool_terminal_projection(
            runtime,
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
        )

    asyncio.run(run())

    timeline = build_run_timeline(
        runtime.event_log.iter(run_id=CTX.run_id),
        runtime_session_id=runtime.runtime_session_id,
    )

    assert timeline.status == "completed"


def test_build_run_timeline_projects_plan_waiting_and_resolution(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)

    async def run() -> None:
        for event in [
            PlanModeEnteredEvent(
                **CTX.event_fields(),
                source="user",
                previous_permission_mode="bypass-permissions",
                previous_permission_policy=run_start_permission_fields(CTX.run_id)[
                    "permission_policy"
                ],
                reason="plan",
            ),
            PlanQuestionAskedEvent(
                **CTX.event_fields(),
                question_id="plan_question:1",
                tool_call_id="call:question",
                question="Scope?",
                options=["small"],
                allow_free_text=True,
            ),
            PlanQuestionAnsweredEvent(
                **CTX.event_fields(),
                question_id="plan_question:1",
                answer_text="small",
                selected_option="small",
            ),
            PlanExitRequestedEvent(
                **CTX.event_fields(),
                exit_request_id="plan_exit:1",
                tool_call_id="call:exit",
                plan_text="draft",
                summary="draft summary",
            ),
            PlanExitResolvedEvent(
                **CTX.event_fields(),
                exit_request_id="plan_exit:1",
                tool_call_id="call:exit",
                decision="approve",
                user_feedback="ok",
            ),
            PlanModeExitedEvent(
                **CTX.event_fields(),
                source="approved_exit_plan",
                exit_request_id="plan_exit:1",
                restored_permission_mode="bypass-permissions",
                restored_permission_policy=run_start_permission_fields(CTX.run_id)[
                    "permission_policy"
                ],
                accepted_plan_summary="draft summary",
                transition_owner="agent_run",
            ),
        ]:
            await runtime.emit(event)

    asyncio.run(run())

    timeline = build_run_timeline(
        runtime.event_log.iter(run_id=CTX.run_id),
        runtime_session_id=runtime.runtime_session_id,
    )

    assert timeline.status == "completed"
    kinds = [item.kind for item in timeline.items]
    assert kinds == ["plan_mode", "plan_question", "plan_exit_request", "plan_mode"]
    question = next(item for item in timeline.items if item.kind == "plan_question")
    exit_request = next(
        item for item in timeline.items if item.kind == "plan_exit_request"
    )
    assert question.status == "answered"
    assert question.metadata["answer_text"] == "small"
    assert exit_request.status == "approve"
    assert exit_request.summary == "draft summary"


def test_run_timeline_summary_separates_multiple_assistant_text_items() -> None:
    timeline = build_run_timeline(
        [
            make_text_block_segment_event(
                **CTX.event_fields(), block_id="text:1", delta="first", sequence=1
            ),
            make_text_block_segment_event(
                run_id=CTX.run_id,
                turn_id="turn:timeline/002",
                reply_id="reply:timeline/002",
                block_id="text:2",
                delta="second",
                sequence=2,
            ),
        ],
        runtime_session_id="runtime:timeline",
    )

    summary = summarize_run_timeline(timeline)

    assert summary.assistant_text == "first\nsecond"


@pytest.mark.parametrize(
    ("session_status", "timeline_status"),
    [
        ("finished", "completed"),
        ("failed", "failed"),
        ("aborted", "aborted"),
    ],
)
def test_run_timeline_preserves_non_success_run_end_status(
    tmp_path,
    session_status: str,
    timeline_status: str,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)

    async def run() -> None:
        await runtime.emit(_run_start())
        await runtime.emit(ReplyStartEvent(**CTX.event_fields(), name="assistant"))
        await runtime.emit(
            RunEndEvent(
                **run_end_contract_fields(
                    CTX.run_id,
                    status=session_status,
                    abort_kind="user_stop" if session_status == "aborted" else None,
                ),
                **CTX.event_fields(),
                status=session_status,
                stop_reason=(
                    "final"
                    if session_status == "finished"
                    else "model_error"
                    if session_status == "failed"
                    else "aborted"
                ),
                abort_kind="user_stop" if session_status == "aborted" else None,
            )
        )

    asyncio.run(run())

    timeline = build_run_timeline(
        runtime.event_log.iter(run_id=CTX.run_id),
        runtime_session_id=runtime.runtime_session_id,
    )

    assert timeline.status == timeline_status
