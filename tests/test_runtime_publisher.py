import asyncio
import threading

from pulsara_agent.event import EventContext, TextBlockDeltaEvent
from pulsara_agent.runtime import RuntimePublishedEvent, RuntimeSession
from pulsara_agent.runtime.state import LoopState


CTX = EventContext(run_id="run:publisher", turn_id="turn:publisher", reply_id="reply:publisher")


class RecordingSubscriber:
    def __init__(self) -> None:
        self.events: list[RuntimePublishedEvent] = []

    async def on_published_event(self, published: RuntimePublishedEvent) -> None:
        self.events.append(published)


def test_runtime_publisher_orders_thread_events_by_canonical_sequence(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)
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
    runtime = RuntimeSession(tmp_path)
    subscriber = RecordingSubscriber()
    runtime.publisher.subscribe(subscriber)
    state = LoopState(session_id=runtime.runtime_session_id)

    async def run() -> None:
        await runtime.emit(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:0", delta="bind"), state=state)
        runtime.emit_from_thread(TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="thread"), state=state)
        await asyncio.sleep(0.05)

    asyncio.run(run())

    assert subscriber.events[-1].state is state
