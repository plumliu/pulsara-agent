import asyncio
import threading
import time
import types

import pytest
from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.event import EventContext, TextBlockDeltaEvent
from pulsara_agent.runtime import RuntimeEventPublisher, RuntimePublishedEvent
from pulsara_agent.runtime.state import LoopState


CTX = EventContext(run_id="run:publisher", turn_id="turn:publisher", reply_id="reply:publisher")


class RecordingSubscriber:
    def __init__(self) -> None:
        self.events: list[RuntimePublishedEvent] = []

    async def on_published_event(self, published: RuntimePublishedEvent) -> None:
        self.events.append(published)


class SlowSubscriber:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.events: list[RuntimePublishedEvent] = []

    async def on_published_event(self, published: RuntimePublishedEvent) -> None:
        await asyncio.sleep(self.delay)
        self.events.append(published)


class FailingSubscriber:
    async def on_published_event(self, published: RuntimePublishedEvent) -> None:
        raise RuntimeError(f"subscriber failed for sequence {published.event.sequence}")


def test_runtime_publisher_orders_thread_events_by_canonical_sequence(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    subscriber = RecordingSubscriber()
    runtime.publisher.subscribe(subscriber)
    ready = threading.Event()
    release = threading.Event()

    async def run() -> tuple[int | None, list[int | None]]:
        await runtime.emit(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:0", delta="bind"))

        def second_thread() -> None:
            ready.set()
            release.wait(timeout=1)
            runtime.emit_from_thread(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:2", delta="second"))

        worker = threading.Thread(target=second_thread)
        worker.start()
        ready.wait(timeout=1)
        first = runtime.emit_from_thread(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="first"))
        release.set()
        worker.join(timeout=1)
        await asyncio.sleep(0.05)
        return first.sequence, [published.event.sequence for published in subscriber.events]

    first_sequence, sequences = asyncio.run(run())

    assert first_sequence is not None
    assert sequences == sorted(sequences)
    assert runtime.event_log.iter()[-2].sequence == first_sequence


def test_emit_from_thread_preserves_loop_state_for_subscribers(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    subscriber = RecordingSubscriber()
    runtime.publisher.subscribe(subscriber)
    state = LoopState(session_id=runtime.runtime_session_id)

    async def run() -> None:
        await runtime.emit(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:0", delta="bind"), state=state)
        runtime.emit_from_thread(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="thread"), state=state)
        await asyncio.sleep(0.05)

    asyncio.run(run())

    assert subscriber.events[-1].state is state


def test_emit_from_thread_does_not_wait_for_slow_subscribers(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    slow = SlowSubscriber(delay=0.2)
    runtime.publisher.subscribe(slow)

    async def run() -> float:
        await runtime.emit(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:0", delta="bind"))

        elapsed = 0.0

        def worker() -> None:
            nonlocal elapsed
            started = time.monotonic()
            runtime.emit_from_thread(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="thread"))
            elapsed = time.monotonic() - started

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
        return elapsed

    elapsed = asyncio.run(run())

    assert elapsed < 0.05


def test_emit_from_thread_eventually_publishes_after_slow_subscriber(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    slow = SlowSubscriber(delay=0.05)
    runtime.publisher.subscribe(slow)

    async def run() -> list[int | None]:
        await runtime.emit(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:0", delta="bind"))
        runtime.emit_from_thread(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="thread"))

        deadline = time.monotonic() + 1
        while len(slow.events) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return [published.event.sequence for published in slow.events]

    sequences = asyncio.run(run())

    assert sequences == [1, 2]


def test_runtime_publisher_reschedules_if_mailbox_receives_item_while_drainer_exits() -> None:
    publisher = RuntimeEventPublisher(runtime_session_id="runtime:publisher")
    subscriber = RecordingSubscriber()
    publisher.subscribe(subscriber)
    reached_empty = threading.Event()
    release_exit = threading.Event()

    async def run() -> tuple[list[int | None], int]:
        async def patched_drain_mailbox(self: RuntimeEventPublisher) -> None:
            assert self._mailbox is not None
            while True:
                try:
                    item = self._mailbox.get_nowait()
                except asyncio.QueueEmpty:
                    reached_empty.set()
                    await asyncio.to_thread(release_exit.wait, 1)
                    self._drain_task = None
                    break
                self._store_pending(item)
                await self._drain_pending()

        publisher._drain_mailbox = types.MethodType(patched_drain_mailbox, publisher)

        await publisher.publish(
            RuntimePublishedEvent(
                runtime_session_id="runtime:publisher",
                event=TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="first", sequence=1),
            )
        )
        release_exit.set()
        while publisher._drain_task is not None:
            await asyncio.sleep(0.01)

        reached_empty.clear()
        release_exit.clear()
        publisher._ensure_drain_task()
        assert await asyncio.to_thread(reached_empty.wait, 1)

        def worker() -> None:
            publisher.publish_from_thread(
                RuntimePublishedEvent(
                    runtime_session_id="runtime:publisher",
                    event=TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:2", delta="second", sequence=2),
                )
            )

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()

        release_exit.set()
        deadline = time.monotonic() + 1
        while len(subscriber.events) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        mailbox_size = 0 if publisher._mailbox is None else publisher._mailbox.qsize()
        return [event.event.sequence for event in subscriber.events], mailbox_size

    sequences, mailbox_size = asyncio.run(run())

    assert sequences == [1, 2]
    assert mailbox_size == 0


def test_runtime_publisher_publish_raises_when_subscriber_fails_but_continues_delivery() -> None:
    publisher = RuntimeEventPublisher(runtime_session_id="runtime:publisher")
    failing = FailingSubscriber()
    recording = RecordingSubscriber()
    publisher.subscribe(failing)
    publisher.subscribe(recording)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="subscriber failed for sequence 1"):
            await publisher.publish(
                RuntimePublishedEvent(
                    runtime_session_id="runtime:publisher",
                    event=TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="first", sequence=1),
                )
            )

    asyncio.run(run())

    assert [event.event.sequence for event in recording.events] == [1]
    assert [type(error) for error in publisher.errors] == [RuntimeError]
