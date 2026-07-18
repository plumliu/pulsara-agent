from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tests.support.model_stream import (
    make_text_block_segment_event,
)

from pulsara_agent.event import EventContext
from pulsara_agent.event_log import EventLogWriteConflict, InMemoryEventLog


CTX = EventContext(run_id="run:event-log", turn_id="turn:event-log", reply_id="reply:event-log")


def _events(prefix: str, count: int = 3):
    return [
        make_text_block_segment_event(
            **CTX.event_fields(),
            block_id=f"{prefix}:{index}",
            delta=prefix,
        )
        for index in range(count)
    ]


def test_in_memory_event_log_extend_allocates_contiguous_atomic_batch() -> None:
    log = InMemoryEventLog()
    barrier = Barrier(2)

    def write(prefix: str):
        barrier.wait(timeout=2)
        return log.extend(_events(prefix))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write, "a")
        second = executor.submit(write, "b")
        batches = (first.result(), second.result())

    for batch in batches:
        sequences = [event.sequence for event in batch]
        assert sequences == list(range(sequences[0], sequences[0] + len(batch)))
    assert [event.sequence for event in log.iter()] == list(range(1, 7))


def test_in_memory_event_log_conditional_extend_conflict_writes_nothing() -> None:
    log = InMemoryEventLog()
    log.append(_events("seed", 1)[0])
    with pytest.raises(EventLogWriteConflict) as captured:
        log.extend(_events("stale"), expected_last_sequence=0)
    assert captured.value.actual_last_sequence == 1
    assert [event.sequence for event in log.iter()] == [1]


def test_in_memory_event_log_batch_validation_failure_leaves_no_partial_events() -> None:
    log = InMemoryEventLog()
    first = _events("duplicate", 1)[0]
    duplicated = first.model_copy(update={"sequence": None})
    with pytest.raises(ValueError, match="unique"):
        log.extend((first, duplicated))
    assert log.iter() == []
    assert log.next_sequence() == 1
