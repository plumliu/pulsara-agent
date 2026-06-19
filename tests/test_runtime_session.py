import json
import asyncio

import pytest

from pulsara_agent.event import EventContext, TextBlockDeltaEvent
from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime import RuntimePublishedEvent, RuntimeSession
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.runtime.terminal import TerminalStatus
from pulsara_agent.tools import ToolCall, build_core_tool_registry


CTX = EventContext(run_id="run:runtime", turn_id="turn:runtime", reply_id="reply:runtime")


class RecordingSubscriber:
    def __init__(self) -> None:
        self.events: list[RuntimePublishedEvent] = []

    async def on_published_event(self, published: RuntimePublishedEvent) -> None:
        self.events.append(published)


def test_runtime_session_create_tool_executor_does_not_record_by_default(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)
    executor = runtime.create_tool_executor()

    result = executor.execute(
        ToolCall(id="call:terminal", name="terminal", arguments={"command": "printf ok"}),
        event_context=CTX,
    )

    assert result.status is ToolResultState.SUCCESS
    assert runtime.event_log.iter(reply_id="reply:runtime") == []


def test_runtime_session_keeps_named_terminal_sessions_separate(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    runtime = RuntimeSession(tmp_path)
    executor = runtime.create_tool_executor()

    code_result = executor.execute(
        ToolCall(id="call:code", name="terminal", arguments={"command": "cd src && pwd", "session_id": "code"}),
        event_context=CTX,
    )
    default_result = executor.execute(
        ToolCall(id="call:default", name="terminal", arguments={"command": "pwd"}),
        event_context=CTX,
    )

    code_payload = json.loads(code_result.output)
    default_payload = json.loads(default_result.output)
    assert code_payload["terminal_session_id"] == "code"
    assert code_payload["cwd"] == str(tmp_path / "src")
    assert default_payload["terminal_session_id"] == "default"
    assert default_payload["cwd"] == str(tmp_path)


def test_runtime_session_terminal_session_limit_and_validation(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)
    executor = runtime.create_tool_executor()

    invalid = executor.execute(
        ToolCall(id="call:bad", name="terminal", arguments={"command": "pwd", "session_id": "../bad"}),
        event_context=CTX,
    )
    assert invalid.status is ToolResultState.ERROR
    assert "terminal session_id must be" in json.loads(invalid.output)["error"]

    for name in ["a", "b", "c", "d"]:
        result = executor.execute(
            ToolCall(id=f"call:{name}", name="terminal", arguments={"command": "pwd", "session_id": name}),
            event_context=CTX,
        )
        assert result.status is ToolResultState.SUCCESS

    too_many = executor.execute(
        ToolCall(id="call:e", name="terminal", arguments={"command": "pwd", "session_id": "e"}),
        event_context=CTX,
    )
    assert too_many.status is ToolResultState.ERROR
    assert "terminal session limit reached" in json.loads(too_many.output)["error"]


def test_runtime_session_create_tool_executor_can_explicitly_record_to_shared_event_log(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)
    executor = runtime.create_tool_executor(record_event=runtime.make_thread_recorder())

    result = executor.execute(
        ToolCall(id="call:terminal", name="terminal", arguments={"command": "printf ok"}),
        event_context=CTX,
    )

    assert result.status is ToolResultState.SUCCESS
    assert [event.sequence for event in runtime.event_log.iter(reply_id="reply:runtime")] == [1, 2, 3, 4, 5]


def test_runtime_session_close_kills_background_terminal_process(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)
    executor = runtime.create_tool_executor()
    start = executor.execute(
        ToolCall(id="call:terminal", name="terminal", arguments={"command": "sleep 10", "background": True}),
        event_context=CTX,
    )
    process_id = json.loads(start.output)["process_id"]

    runtime.close()
    status = runtime.terminal_sessions.poll_process(process_id).status

    assert status is TerminalStatus.KILLED


def test_runtime_session_create_tool_executor_rejects_raw_append_recorders(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)

    with pytest.raises(TypeError, match="requires RuntimeSession.make_thread_recorder"):
        runtime.create_tool_executor(record_event=runtime.event_log.append)


def test_build_core_tool_registry_requires_runtime_session(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)

    registry = build_core_tool_registry(runtime)

    assert "terminal" in registry.names()
    with pytest.raises(TypeError, match="requires a RuntimeSession"):
        build_core_tool_registry(tmp_path)


def test_runtime_session_emit_and_emit_many_publish_events(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)
    subscriber = RecordingSubscriber()
    runtime.publisher.subscribe(subscriber)
    state = LoopState(session_id=runtime.runtime_session_id)

    async def run() -> None:
        first = await runtime.emit(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="first"), state=state)
        many = await runtime.emit_many(
            [
                TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:2", delta="second"),
                TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:3", delta="third"),
            ],
            state=state,
        )
        assert first.sequence == 1
        assert [event.sequence for event in many] == [2, 3]

    asyncio.run(run())

    assert [published.event.sequence for published in subscriber.events] == [1, 2, 3]
    assert all(published.state is state for published in subscriber.events)


def test_runtime_session_emit_from_thread_without_bound_loop_only_appends(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)
    subscriber = RecordingSubscriber()
    runtime.publisher.subscribe(subscriber)

    stored = runtime.emit_from_thread(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="thread"))

    assert stored.sequence == 1
    assert [event.sequence for event in runtime.event_log.iter()] == [1]
    assert subscriber.events == []


def test_runtime_session_emit_after_unbound_emit_from_thread_does_not_block(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)
    subscriber = RecordingSubscriber()
    runtime.publisher.subscribe(subscriber)

    dropped = runtime.emit_from_thread(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:0", delta="thread"))

    async def run() -> None:
        stored = await asyncio.wait_for(
            runtime.emit(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="async")),
            timeout=0.5,
        )
        assert stored.sequence == 2

    asyncio.run(run())

    assert dropped.sequence == 1
    assert [event.sequence for event in runtime.event_log.iter()] == [1, 2]
    assert [published.event.sequence for published in subscriber.events] == [2]


def test_runtime_session_emit_rejects_preassigned_sequence(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)

    async def run() -> None:
        with pytest.raises(ValueError, match="sequence=None"):
            await runtime.emit(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="bad", sequence=10))

    asyncio.run(run())


def test_runtime_session_emit_from_thread_rejects_preassigned_sequence(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)

    with pytest.raises(ValueError, match="sequence=None"):
        runtime.emit_from_thread(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="bad", sequence=10))
