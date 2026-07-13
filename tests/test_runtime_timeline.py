import asyncio
import json
import urllib.parse
from pathlib import Path

import pytest
from tests.support.runtime_session import in_memory_runtime_session
from tests.conftest import (
    run_end_contract_fields,
    run_start_permission_fields,
    tool_result_end_contract_fields,
)
from tests.support import (
    model_call_end_fields,
    model_call_start_fields,
    test_resolved_call_fact,
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
    TextBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.memory import (
    InMemoryArchiveStore,
    RunTimelinePersistenceHook,
    load_run_timeline,
    summarize_run_timeline,
)
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.event import ConfirmResult
from pulsara_agent.message import ToolResultState
from pulsara_agent.message import ToolCallBlock, ToolCallState
from pulsara_agent.runtime import build_run_timeline


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


def test_build_run_timeline_summarizes_model_text_and_tool_activity() -> None:
    runtime = in_memory_runtime_session(Path("."))

    async def run() -> None:
        resolved_call = test_resolved_call_fact()
        for event in [
            ReplyStartEvent(**CTX.event_fields(), name="assistant"),
            ModelCallStartEvent(
                **CTX.event_fields(),
                **model_call_start_fields(resolved_call=resolved_call),
            ),
            TextBlockDeltaEvent(
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
            ToolCallStartEvent(
                **CTX.event_fields(),
                tool_call_id="call:read",
                tool_call_name="read_file",
            ),
            ToolCallDeltaEvent(
                **CTX.event_fields(),
                tool_call_id="call:read",
                delta='{"path":"note.txt"}',
            ),
            ToolCallEndEvent(**CTX.event_fields(), tool_call_id="call:read"),
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
            ReplyEndEvent(**CTX.event_fields()),
        ]:
            await runtime.emit(event)

    asyncio.run(run())

    timeline = build_run_timeline(
        runtime.event_log.iter(run_id=CTX.run_id),
        runtime_session_id=runtime.runtime_session_id,
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
        for event in [
            ReplyStartEvent(**CTX.event_fields(), name="assistant"),
            ModelCallStartEvent(**CTX.event_fields(), **model_call_start_fields()),
            ToolCallStartEvent(
                **CTX.event_fields(),
                tool_call_id="call:danger",
                tool_call_name="terminal",
            ),
            ToolCallDeltaEvent(
                **CTX.event_fields(),
                tool_call_id="call:danger",
                delta='{"command":"rm -rf build"}',
            ),
            ToolCallEndEvent(**CTX.event_fields(), tool_call_id="call:danger"),
            ReplyEndEvent(**CTX.event_fields()),
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
            ToolCallStartEvent(
                **CTX.event_fields(),
                tool_call_id=tool_call.id,
                tool_call_name=tool_call.name,
            ),
            ToolCallDeltaEvent(
                **CTX.event_fields(), tool_call_id=tool_call.id, delta=tool_call.input
            ),
            ToolCallEndEvent(**CTX.event_fields(), tool_call_id=tool_call.id),
            ReplyEndEvent(**CTX.event_fields()),
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
            ToolResultEndEvent(
                **CTX.event_fields(),
                **tool_result_end_contract_fields(
                    tool_call.id, tool_name=tool_call.name
                ),
                tool_call_id=tool_call.id,
                state=ToolResultState.SUCCESS,
                metadata={
                    "tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}
                },
            ),
        ]:
            await runtime.emit(event)

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


def test_run_timeline_persistence_hook_archives_and_indexes_completed_run(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    graph = InMemoryGraphStore()
    archive = InMemoryArchiveStore()
    runtime.hook_manager.register_event(
        None,
        RunTimelinePersistenceHook(
            graph=graph,
            archive=archive,
            event_store=runtime.event_log,
        ),
    )

    async def run() -> None:
        await runtime.emit(_run_start())
        await runtime.emit(ReplyStartEvent(**CTX.event_fields(), name="assistant"))
        await runtime.emit(
            TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="done")
        )
        await runtime.emit(ReplyEndEvent(**CTX.event_fields()))
        await runtime.emit(
            RunEndEvent(
                **run_end_contract_fields(CTX.run_id, status="finished"),
                **CTX.event_fields(),
                status="finished",
                stop_reason="final",
            )
        )

    asyncio.run(run())

    records = graph.find_by_type(rt.RUN_TIMELINE)
    assert len(records) == 1
    assert records[0][rt.SOURCE_RUN.name] == CTX.run_id
    assert records[0][rt.STATUS.name] == "completed"
    assert records[0][rt.ITEM_COUNT.name] >= 2

    blob_id = _artifact_id_from_node_ref(records[0][rt.STORED_AS.name]["@id"])
    payload = json.loads(archive.get_text(blob_id))
    assert payload["runtime_session_id"] == runtime.runtime_session_id
    assert payload["run_id"] == CTX.run_id
    assert payload["items"][-1]["kind"] == "assistant_text"
    assert payload["items"][-1]["summary"] == "done"


def test_run_timeline_persistence_preserves_created_at_across_snapshot_updates(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    graph = InMemoryGraphStore()
    archive = InMemoryArchiveStore()
    runtime.hook_manager.register_event(
        None,
        RunTimelinePersistenceHook(
            graph=graph,
            archive=archive,
            event_store=runtime.event_log,
        ),
    )

    async def run() -> None:
        await runtime.emit(_run_start())
        await runtime.emit(ReplyStartEvent(**CTX.event_fields(), name="assistant"))
        await runtime.emit(ReplyEndEvent(**CTX.event_fields()))
        first = graph.find_by_type(rt.RUN_TIMELINE)[0]
        await runtime.emit(
            RunEndEvent(
                **run_end_contract_fields(CTX.run_id, status="finished"),
                **CTX.event_fields(),
                status="finished",
                stop_reason="final",
            )
        )
        second = graph.find_by_type(rt.RUN_TIMELINE)[0]
        assert first[rt.CREATED_AT.name] == second[rt.CREATED_AT.name]
        assert first[rt.UPDATED_AT.name] <= second[rt.UPDATED_AT.name]

    asyncio.run(run())


def test_run_timeline_read_side_loads_summary_and_tool_trace(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    graph = InMemoryGraphStore()
    archive = InMemoryArchiveStore()
    runtime.hook_manager.register_event(
        None,
        RunTimelinePersistenceHook(
            graph=graph,
            archive=archive,
            event_store=runtime.event_log,
        ),
    )

    async def run() -> None:
        for event in [
            _run_start(),
            ReplyStartEvent(**CTX.event_fields(), name="assistant"),
            TextBlockDeltaEvent(
                **CTX.event_fields(), block_id="text:1", delta="Reading now."
            ),
            ToolCallStartEvent(
                **CTX.event_fields(),
                tool_call_id="call:read",
                tool_call_name="read_file",
            ),
            ToolCallDeltaEvent(
                **CTX.event_fields(),
                tool_call_id="call:read",
                delta='{"path":"probe.txt"}',
            ),
            ToolCallEndEvent(**CTX.event_fields(), tool_call_id="call:read"),
            ToolResultStartEvent(
                **CTX.event_fields(),
                tool_call_id="call:read",
                tool_call_name="read_file",
            ),
            ToolResultTextDeltaEvent(
                **CTX.event_fields(), tool_call_id="call:read", delta="PULSARA_TRACE_OK"
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
            ReplyEndEvent(**CTX.event_fields()),
            RunEndEvent(
                **run_end_contract_fields(CTX.run_id, status="finished"),
                **CTX.event_fields(),
                status="finished",
                stop_reason="final",
            ),
        ]:
            await runtime.emit(event)

    asyncio.run(run())

    timeline = load_run_timeline(
        graph=graph,
        archive=archive,
        run_id=CTX.run_id,
        runtime_session_id=runtime.runtime_session_id,
    )
    summary = summarize_run_timeline(timeline)

    assert summary.status == "completed"
    assert summary.assistant_text == "Reading now."
    assert len(summary.tool_traces) == 1
    assert summary.tool_traces[0].tool_call_id == "call:read"
    assert summary.tool_traces[0].tool_name == "read_file"
    assert summary.tool_traces[0].arguments == '{"path":"probe.txt"}'
    assert summary.tool_traces[0].status == "success"
    assert summary.tool_traces[0].result_summary == "PULSARA_TRACE_OK"


def test_run_timeline_summary_separates_multiple_assistant_text_items() -> None:
    timeline = build_run_timeline(
        [
            TextBlockDeltaEvent(
                **CTX.event_fields(), block_id="text:1", delta="first", sequence=1
            ),
            TextBlockDeltaEvent(
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


def _artifact_id_from_node_ref(node_id: str) -> str:
    prefix = "urn:pulsara:"
    if node_id.startswith(prefix):
        return urllib.parse.unquote(node_id[len(prefix) :])
    return node_id


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
