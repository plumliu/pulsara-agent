import json
import asyncio

import pytest
from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.event import EventContext, TextBlockDeltaEvent
from pulsara_agent.event_log import EventIdConflict, InMemoryEventLog
from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime import (
    EventReconciliationRequired,
    RuntimePublishedEvent,
    RuntimeSession,
)
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.runtime.terminal import TerminalStatus
from pulsara_agent.tools import ToolCall, build_core_tool_registry


CTX = EventContext(run_id="run:runtime", turn_id="turn:runtime", reply_id="reply:runtime")


def test_runtime_session_has_no_implicit_in_memory_storage(tmp_path) -> None:
    with pytest.raises(TypeError, match="event_log"):
        RuntimeSession(tmp_path)


class RecordingSubscriber:
    def __init__(self) -> None:
        self.events: list[RuntimePublishedEvent] = []

    async def on_published_event(self, published: RuntimePublishedEvent) -> None:
        self.events.append(published)


class RecordingExtendEventLog(InMemoryEventLog):
    def __init__(self) -> None:
        super().__init__()
        self.extend_calls = 0

    def extend(self, events, *, expected_last_sequence=None):
        self.extend_calls += 1
        return super().extend(events, expected_last_sequence=expected_last_sequence)


class CommitThenRaiseExtendEventLog(InMemoryEventLog):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    def extend(self, events, *, expected_last_sequence=None):
        stored = super().extend(events, expected_last_sequence=expected_last_sequence)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated lost commit acknowledgement")
        return stored


def test_runtime_session_create_tool_executor_does_not_record_by_default(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    executor = runtime.create_tool_executor()

    result = executor.execute(
        ToolCall(id="call:terminal", name="terminal", arguments={"command": "printf ok"}),
        event_context=CTX,
    )

    assert result.status is ToolResultState.SUCCESS
    assert runtime.event_log.iter(reply_id="reply:runtime") == []


def test_runtime_session_confirms_uncertain_commit_and_catches_up_reducer_and_publisher(
    tmp_path,
) -> None:
    async def run() -> None:
        event_log = CommitThenRaiseExtendEventLog()
        runtime = in_memory_runtime_session(tmp_path, event_log=event_log)
        subscriber = RecordingSubscriber()
        runtime.publisher.subscribe(subscriber)
        reduced: list[str] = []
        runtime.register_committed_reducer(
            reducer_id="test:uncertain-commit",
            through_sequence=0,
            apply_committed=lambda events: reduced.extend(event.id for event in events),
        )
        candidate = TextBlockDeltaEvent(
            **CTX.event_fields(),
            block_id="text:uncertain",
            delta="committed",
        )

        stored = await runtime.emit(candidate)

        assert stored.id == candidate.id
        assert stored.sequence == 1
        assert [event.id for event in event_log.iter()] == [candidate.id]
        assert reduced == [candidate.id]
        assert [item.event.id for item in subscriber.events] == [candidate.id]

    asyncio.run(run())


def test_runtime_session_cas_confirmation_catches_up_through_conflict_high_water(
    tmp_path,
) -> None:
    async def run() -> None:
        event_log = InMemoryEventLog()
        runtime = in_memory_runtime_session(tmp_path, event_log=event_log)
        reduced: list[int] = []
        runtime.register_committed_reducer(
            reducer_id="test:cas-confirmation-high-water",
            through_sequence=0,
            apply_committed=lambda events: reduced.extend(
                event.sequence for event in events if event.sequence is not None
            ),
        )
        candidate = TextBlockDeltaEvent(
            **CTX.event_fields(),
            block_id="text:confirmed",
            delta="confirmed",
        )
        later = TextBlockDeltaEvent(
            **CTX.event_fields(),
            block_id="text:later",
            delta="later",
        )
        first = event_log.append(candidate)
        second = event_log.append(later)

        result = await runtime.write_event(candidate, expected_last_sequence=0)

        assert first.sequence == 1
        assert second.sequence == 2
        assert result.committed_events == (first,)
        assert result.reducer_high_waters["test:cas-confirmation-high-water"] == 2
        assert reduced == [1, 2]
        assert result.publisher_enqueued_through_sequence == 2

    asyncio.run(run())


def test_runtime_session_partial_batch_confirmation_latches_reconciliation(
    tmp_path,
) -> None:
    async def run() -> None:
        event_log = InMemoryEventLog()
        runtime = in_memory_runtime_session(tmp_path, event_log=event_log)
        rebuilt: list[str] = []
        runtime.register_committed_reducer(
            reducer_id="test:partial-confirmation",
            through_sequence=0,
            apply_committed=lambda events: rebuilt.extend(event.id for event in events),
            rebuild_committed=lambda events: rebuilt.__setitem__(
                slice(None),
                [event.id for event in events],
            ),
        )
        first = TextBlockDeltaEvent(
            **CTX.event_fields(),
            block_id="text:partial-first",
            delta="first",
        )
        missing = TextBlockDeltaEvent(
            **CTX.event_fields(),
            block_id="text:partial-missing",
            delta="missing",
        )
        event_log.append(first)

        with pytest.raises(EventReconciliationRequired):
            await runtime.write_events((first, missing))

        assert runtime.reconciliation_required is True
        assert runtime.ledger_reconciliation_required is True
        runtime.reconcile_committed_reducer("test:partial-confirmation")
        assert runtime.reconciliation_required is True
        assert runtime.ledger_reconciliation_required is True
        with pytest.raises(EventReconciliationRequired):
            await runtime.write_event(
                TextBlockDeltaEvent(
                    **CTX.event_fields(),
                    block_id="text:after-partial",
                    delta="must fail closed",
                )
            )

    asyncio.run(run())


def test_runtime_session_event_id_payload_conflict_preserves_stable_error_type(
    tmp_path,
) -> None:
    async def run() -> None:
        event_log = InMemoryEventLog()
        runtime = in_memory_runtime_session(tmp_path, event_log=event_log)
        candidate = TextBlockDeltaEvent(
            **CTX.event_fields(),
            block_id="text:id-conflict",
            delta="canonical",
        )
        event_log.append(candidate)
        conflicting = candidate.model_copy(update={"delta": "different"})

        with pytest.raises(EventIdConflict):
            await runtime.write_event(conflicting)

        assert runtime.reconciliation_required is False

    asyncio.run(run())


def test_runtime_session_keeps_named_terminal_sessions_separate(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    runtime = in_memory_runtime_session(tmp_path)
    executor = runtime.create_tool_executor()

    code_result = executor.execute(
        ToolCall(id="call:code", name="terminal", arguments={"command": "cd src && pwd", "terminal_session_id": "code"}),
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
    runtime = in_memory_runtime_session(tmp_path)
    executor = runtime.create_tool_executor()

    invalid = executor.execute(
        ToolCall(id="call:bad", name="terminal", arguments={"command": "pwd", "terminal_session_id": "../bad"}),
        event_context=CTX,
    )
    assert invalid.status is ToolResultState.ERROR
    assert "terminal session_id must be" in json.loads(invalid.output)["error"]

    for name in ["a", "b", "c", "d"]:
        result = executor.execute(
            ToolCall(id=f"call:{name}", name="terminal", arguments={"command": "pwd", "terminal_session_id": name}),
            event_context=CTX,
        )
        assert result.status is ToolResultState.SUCCESS

    too_many = executor.execute(
        ToolCall(id="call:e", name="terminal", arguments={"command": "pwd", "terminal_session_id": "e"}),
        event_context=CTX,
    )
    assert too_many.status is ToolResultState.ERROR
    assert "terminal session limit reached" in json.loads(too_many.output)["error"]


def test_runtime_session_create_tool_executor_can_explicitly_record_to_shared_event_log(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    executor = runtime.create_tool_executor(record_event=runtime.make_thread_recorder())

    result = executor.execute(
        ToolCall(id="call:terminal", name="terminal", arguments={"command": "printf ok"}),
        event_context=CTX,
    )

    assert result.status is ToolResultState.SUCCESS
    assert [event.sequence for event in runtime.event_log.iter(reply_id="reply:runtime")] == [1, 2, 3, 4, 5]


def test_runtime_session_close_kills_background_terminal_process(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    executor = runtime.create_tool_executor()
    start = executor.execute(
        ToolCall(id="call:terminal", name="terminal", arguments={"command": "sleep 10", "yield_time_ms": 0}),
        event_context=CTX,
    )
    process_id = json.loads(start.output)["process_id"]

    runtime.close()
    status = runtime.terminal_sessions.poll_process(process_id).status

    assert status is TerminalStatus.KILLED


def test_runtime_session_create_tool_executor_rejects_raw_append_recorders(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)

    with pytest.raises(TypeError, match="requires RuntimeSession.make_thread_recorder"):
        runtime.create_tool_executor(record_event=runtime.event_log.append)


def test_build_core_tool_registry_requires_runtime_session(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)

    registry = build_core_tool_registry(runtime)

    assert "terminal" in registry.names()
    with pytest.raises(TypeError, match="requires a RuntimeSession"):
        build_core_tool_registry(tmp_path)


def test_runtime_session_emit_and_emit_many_publish_events(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
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


def test_runtime_session_emit_many_uses_event_log_batch_extend(tmp_path) -> None:
    event_log = RecordingExtendEventLog()
    runtime = RuntimeSession(
        tmp_path,
        event_log=event_log,
        archive=in_memory_runtime_session(tmp_path).archive,
        tool_result_artifacts=in_memory_runtime_session(tmp_path).tool_result_artifacts,
    )

    async def run() -> None:
        stored = await runtime.emit_many(
            [
                TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="one"),
                TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:2", delta="two"),
            ]
        )
        assert [event.sequence for event in stored] == [1, 2]

    asyncio.run(run())

    assert event_log.extend_calls == 1


def test_runtime_session_emit_from_thread_without_bound_loop_only_appends(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    subscriber = RecordingSubscriber()
    runtime.publisher.subscribe(subscriber)

    stored = runtime.emit_from_thread(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="thread"))

    assert stored.sequence == 1
    assert [event.sequence for event in runtime.event_log.iter()] == [1]
    assert subscriber.events == []


def test_runtime_session_emit_after_unbound_emit_from_thread_does_not_block(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
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


def test_runtime_session_publish_stored_events_bridges_direct_event_log_writes(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    subscriber = RecordingSubscriber()
    runtime.publisher.subscribe(subscriber)

    async def run() -> None:
        await runtime.emit(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="bind"))
        stored = runtime.event_log.append(
            TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:2", delta="direct")
        )
        assert stored.sequence == 2

        runtime.publish_stored_events([stored])
        final = await asyncio.wait_for(
            runtime.emit(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:3", delta="after")),
            timeout=0.5,
        )
        assert final.sequence == 3

    asyncio.run(run())

    assert [event.sequence for event in runtime.event_log.iter()] == [1, 2, 3]
    assert [published.event.sequence for published in subscriber.events] == [1, 2, 3]


def test_runtime_session_emit_rejects_preassigned_sequence(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)

    async def run() -> None:
        with pytest.raises(ValueError, match="sequence=None"):
            await runtime.emit(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="bad", sequence=10))

    asyncio.run(run())


def test_runtime_session_emit_from_thread_rejects_preassigned_sequence(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)

    with pytest.raises(ValueError, match="sequence=None"):
        runtime.emit_from_thread(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="bad", sequence=10))


def test_runtime_session_default_event_metadata_is_merged_on_emit(tmp_path) -> None:
    runtime = in_memory_runtime_session(
        tmp_path,
        default_event_metadata={
            "subagent": {
                "subagent_run_id": "subagent:1",
                "parent_runtime_session_id": "runtime:parent",
            },
            "scope": "child",
        },
    )

    async def run() -> None:
        stored = await runtime.emit(
            TextBlockDeltaEvent(
                **CTX.event_fields(),
                block_id="text:1",
                delta="child",
                metadata={"subagent": {"capability_profile_id": "profile:1"}, "local": "value"},
            )
        )
        assert stored.metadata == {
            "subagent": {
                "subagent_run_id": "subagent:1",
                "parent_runtime_session_id": "runtime:parent",
                "capability_profile_id": "profile:1",
            },
            "scope": "child",
            "local": "value",
        }

    asyncio.run(run())


def test_runtime_session_default_event_metadata_is_merged_on_emit_from_thread(tmp_path) -> None:
    runtime = in_memory_runtime_session(
        tmp_path,
        default_event_metadata={"subagent": {"subagent_run_id": "subagent:thread"}},
    )

    stored = runtime.emit_from_thread(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="thread"))

    assert stored.metadata["subagent"]["subagent_run_id"] == "subagent:thread"
