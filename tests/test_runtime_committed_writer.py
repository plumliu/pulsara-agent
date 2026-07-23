from __future__ import annotations

import asyncio
import threading

import pytest

from tests.support.model_stream import (
    make_text_block_segment_event,
)

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    SubagentRunFailedEvent,
    TextBlockSegmentEvent,
)
from pulsara_agent.runtime import (
    EventPublicationAfterCommitError,
    EventReconciliationRequired,
    EventWriteConflict,
    RuntimePublishedEvent,
)
from pulsara_agent.runtime.subagent.store import (
    SubagentGraphStateStore,
    SubagentReducerApplyError,
)
from tests.support.runtime_session import in_memory_runtime_session


CTX = EventContext(run_id="run:writer", turn_id="turn:writer", reply_id="reply:writer")


def _event(label: str) -> TextBlockSegmentEvent:
    return make_text_block_segment_event(**CTX.event_fields(), block_id=f"text:{label}", delta=label)


class _RecordingSubscriber:
    def __init__(self) -> None:
        self.events: list[RuntimePublishedEvent] = []

    async def on_published_event(self, published: RuntimePublishedEvent) -> None:
        self.events.append(published)


class _FailingSubscriber:
    async def on_published_event(self, published: RuntimePublishedEvent) -> None:
        raise RuntimeError(f"observer failed at {published.event.sequence}")


class _FailFirstSubscriber:
    def __init__(self) -> None:
        self.calls = 0

    async def on_published_event(self, published: RuntimePublishedEvent) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError(f"first observer failure at {published.event.sequence}")


def test_reducer_catches_missing_interval_before_current_batch(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    applied: list[tuple[int, ...]] = []
    runtime.register_committed_reducer(
        reducer_id="test:reducer",
        through_sequence=0,
        apply_committed=lambda events: applied.append(tuple(_sequences(events))),
    )
    runtime.event_log.append(_event("offline"))

    async def run() -> None:
        result = await runtime.write_event(_event("current"))
        assert result.require_reduced("test:reducer") == result.committed_events

    asyncio.run(run())
    assert applied == [(1,), (2,)]


def test_reducer_and_publisher_catch_up_from_independent_high_water_marks(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    applied: list[int] = []
    recording = _RecordingSubscriber()
    runtime.publisher.subscribe(recording)
    runtime.register_committed_reducer(
        reducer_id="test:reducer",
        through_sequence=0,
        apply_committed=lambda events: applied.extend(_sequences(events)),
    )
    runtime.event_log.append(_event("offline"))

    async def run() -> None:
        await runtime.write_event(_event("current"))

    asyncio.run(run())
    assert applied == [1, 2]
    assert [item.event.sequence for item in recording.events] == [1, 2]


def test_write_conflict_catches_reducer_and_publisher_to_actual_high_water(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    applied: list[int] = []
    recording = _RecordingSubscriber()
    runtime.publisher.subscribe(recording)
    runtime.register_committed_reducer(
        reducer_id="test:reducer",
        through_sequence=0,
        apply_committed=lambda events: applied.extend(_sequences(events)),
    )
    runtime.event_log.append(_event("external"))

    async def run() -> None:
        with pytest.raises(EventWriteConflict) as captured:
            await runtime.write_event(
                _event("stale"),
                expected_last_sequence=0,
            )
        assert captured.value.actual_last_sequence == 1
        await asyncio.sleep(0)

    asyncio.run(run())
    assert applied == [1]
    assert [item.event.sequence for item in recording.events] == [1]
    assert runtime.publisher.enqueued_through_sequence == 1
    assert [event.text for event in runtime.event_log.iter()] == ["external"]
    assert not runtime.reconciliation_required


def test_async_and_thread_writes_share_session_write_coordinator(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    applied: list[int] = []
    runtime.register_committed_reducer(
        reducer_id="test:reducer",
        through_sequence=0,
        apply_committed=lambda events: applied.extend(_sequences(events)),
    )

    async def run() -> None:
        await runtime.write_event(_event("bind"))
        threads = [
            threading.Thread(
                target=lambda label=label: runtime.write_events_from_thread((_event(label),))
            )
            for label in ("thread-a", "thread-b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert applied == [1, 2, 3]
    assert [event.sequence for event in runtime.event_log.iter()] == [1, 2, 3]


def test_event_write_applies_reducer_before_observer(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    applied: list[int] = []
    runtime.register_committed_reducer(
        reducer_id="test:reducer",
        through_sequence=0,
        apply_committed=lambda events: applied.extend(_sequences(events)),
    )

    class AssertReducedSubscriber:
        async def on_published_event(self, published: RuntimePublishedEvent) -> None:
            assert published.event.sequence in applied

    runtime.publisher.subscribe(AssertReducedSubscriber())
    asyncio.run(runtime.write_event(_event("ordered")))


def test_event_write_returns_committed_truth_when_observer_fails(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    recording = _RecordingSubscriber()
    runtime.publisher.subscribe(_FailingSubscriber())
    runtime.publisher.subscribe(recording)

    result = asyncio.run(runtime.write_event(_event("committed")))

    assert result.commit_status == "committed"
    assert result.publication_status == "completed"
    assert [event.sequence for event in result.committed_events] == [1]
    assert len(result.publication_errors) == 1
    assert [item.event.sequence for item in recording.events] == [1]


def test_event_batch_observer_failure_does_not_skip_later_sequences(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    failing = _FailFirstSubscriber()
    recording = _RecordingSubscriber()
    runtime.publisher.subscribe(failing)
    runtime.publisher.subscribe(recording)

    async def run() -> None:
        result = await runtime.write_events((_event("one"), _event("two")))
        assert len(result.publication_errors) == 1

    asyncio.run(run())
    assert failing.calls == 2
    assert [item.event.sequence for item in recording.events] == [1, 2]


def test_emit_compat_error_carries_event_write_result(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    runtime.publisher.subscribe(_FailingSubscriber())

    async def run() -> None:
        with pytest.raises(EventPublicationAfterCommitError) as captured:
            await runtime.emit(_event("compat"))
        assert captured.value.result.committed_events[0].sequence == 1
        assert runtime.event_log.next_sequence() == 2

    asyncio.run(run())


def test_committed_reducer_failure_requires_reconciliation(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)

    def fail(_events: tuple[AgentEvent, ...]) -> None:
        raise RuntimeError("synthetic reducer failure")

    runtime.register_committed_reducer(
        reducer_id="test:broken",
        through_sequence=0,
        apply_committed=fail,
    )

    async def run() -> None:
        result = await runtime.write_event(_event("committed"))
        assert result.reconciliation_required is True
        assert result.reducer_errors[0].reducer_id == "test:broken"
        with pytest.raises(EventReconciliationRequired):
            await runtime.write_event(_event("blocked"))

    asyncio.run(run())
    assert [event.sequence for event in runtime.event_log.iter()] == [1]


def test_initial_reducer_catch_up_failure_remains_registered_for_reconciliation(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    runtime.event_log.append(_event("before-registration"))
    allow_apply = False
    applied: list[int] = []

    def apply(events: tuple[AgentEvent, ...]) -> None:
        if not allow_apply:
            raise RuntimeError("synthetic initial catch-up failure")
        applied.extend(_sequences(events))

    with pytest.raises(RuntimeError, match="initial catch-up failure"):
        runtime.register_committed_reducer(
            reducer_id="test:failed-registration",
            through_sequence=0,
            apply_committed=apply,
        )

    assert "test:failed-registration" in runtime._committed_reducers
    assert runtime.reconciliation_required is True
    with pytest.raises(EventReconciliationRequired):
        asyncio.run(runtime.write_event(_event("blocked")))

    allow_apply = True
    runtime.reconcile_committed_reducer("test:failed-registration")
    assert applied == [1]
    assert runtime.reconciliation_required is False


def test_committed_reducer_reconciliation_uses_full_rebuild_callback(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    applied: list[int] = []

    def fail_after_mutation(events: tuple[AgentEvent, ...]) -> None:
        applied.extend(_sequences(events))
        raise RuntimeError("synthetic incremental failure")

    def rebuild(events: tuple[AgentEvent, ...]) -> None:
        applied[:] = _sequences(events)

    runtime.register_committed_reducer(
        reducer_id="test:rebuild",
        through_sequence=0,
        apply_committed=fail_after_mutation,
        rebuild_committed=rebuild,
    )
    result = asyncio.run(runtime.write_event(_event("committed")))
    assert result.reconciliation_required is True

    runtime.reconcile_committed_reducer("test:rebuild")

    assert applied == [1]
    assert runtime.reconciliation_required is False


def test_inconsistent_full_rebuild_keeps_runtime_reconciliation_required(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    store = SubagentGraphStateStore()
    runtime.event_log.append(
        SubagentRunFailedEvent(
            **CTX.event_fields(),
            subagent_run_id="subagent_run:orphan",
            parent_runtime_session_id=runtime.runtime_session_id,
            child_runtime_session_id="runtime:child:orphan",
            reason_code="orphan_failure",
        )
    )

    with pytest.raises(SubagentReducerApplyError):
        runtime.register_committed_reducer(
            reducer_id=store.reducer_id,
            through_sequence=0,
            apply_committed=store.apply_committed,
            rebuild_committed=store.rebuild,
        )

    with pytest.raises(SubagentReducerApplyError, match="remain inconsistent"):
        runtime.reconcile_committed_reducer(store.reducer_id)

    assert store.state.consistent is False
    assert store.reconciliation_required is True
    assert runtime._committed_reducers[store.reducer_id].reconciliation_required is True
    assert runtime.reconciliation_required is True


def test_runtime_session_close_unregisters_committed_reducers(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    runtime.register_committed_reducer(
        reducer_id="test:close",
        through_sequence=0,
        apply_committed=lambda _events: None,
    )

    runtime.close()

    assert runtime._committed_reducers == {}


def test_reducer_registration_catches_up_commit_race(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    runtime.event_log.append(_event("before-registration"))
    applied: list[int] = []
    runtime.register_committed_reducer(
        reducer_id="test:late",
        through_sequence=0,
        apply_committed=lambda events: applied.extend(_sequences(events)),
    )
    assert applied == [1]


def test_thread_write_applies_reducer_when_live_publisher_unavailable(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    applied: list[int] = []
    runtime.register_committed_reducer(
        reducer_id="test:thread",
        through_sequence=0,
        apply_committed=lambda events: applied.extend(_sequences(events)),
    )
    result = runtime.write_events_from_thread((_event("thread"),))
    assert result.publication_status == "unavailable"
    assert applied == [1]
    assert result.reducer_high_waters["test:thread"] == 1


def test_publisher_missing_committed_interval_returns_unavailable_without_hanging(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    # Corrupt only the pytest fake to simulate a durable ledger whose declared
    # high-water cannot supply sequence 1. Production corruption is exercised
    # against PostgreSQL separately.
    runtime.event_log._next_sequence = 2  # type: ignore[attr-defined]

    result = asyncio.run(runtime.write_event(_event("after-gap")))

    assert result.commit_status == "committed"
    assert result.committed_events[0].sequence == 2
    assert result.publication_status == "unavailable"
    assert result.publication_errors[0].error_type == "PublisherSequenceGapError"
    assert runtime.publisher.enqueued_through_sequence == 0


def _sequences(events: tuple[AgentEvent, ...]) -> list[int]:
    return [event.sequence for event in events if event.sequence is not None]
