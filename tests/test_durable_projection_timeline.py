from __future__ import annotations

from pulsara_agent.event import (
    EventContext,
    ReplyEndEvent,
    ReplyStartEvent,
)
from pulsara_agent.runtime.projection_jobs.timeline import (
    IncrementalRunTimelineReducer,
)
from tests.support.model_stream import make_text_block_segment_event


_CTX = EventContext(
    run_id="run:incremental-timeline",
    turn_id="turn:incremental-timeline",
    reply_id="reply:incremental-timeline",
)


def _at_sequence(event, sequence: int):
    return event.model_copy(update={"sequence": sequence})


def test_open_items_receive_append_ordinals_only_when_they_close() -> None:
    started = _at_sequence(
        ReplyStartEvent(**_CTX.event_fields(), name="assistant"),
        1,
    )
    text = _at_sequence(
        make_text_block_segment_event(
            **_CTX.event_fields(),
            block_id="text:incremental",
            delta="bounded incremental output",
        ),
        2,
    )
    ended = _at_sequence(
        ReplyEndEvent(
            **_CTX.event_fields(),
            model_terminal_outcome="completed",
        ),
        3,
    )
    reducer = IncrementalRunTimelineReducer(
        runtime_session_id="runtime:incremental-timeline",
        run_id=_CTX.run_id,
    )
    reducer.apply(started)
    reducer.apply(text)
    assert reducer.take_completed_items() == ()
    assert reducer.next_item_ordinal == 0
    open_state = reducer.open_state_payload()
    assert all(
        "absolute_item_ordinal" not in item
        for item in open_state["open_items"].values()
    )

    recovered = IncrementalRunTimelineReducer.restore(
        runtime_session_id="runtime:incremental-timeline",
        run_id=_CTX.run_id,
        payload=open_state,
        next_item_ordinal=0,
        status=reducer.status,
        start_sequence=reducer.start_sequence,
        terminal_sequence=reducer.terminal_sequence,
    )
    recovered.apply(ended)
    completed = recovered.take_completed_items()
    assert tuple(item["absolute_item_ordinal"] for item in completed) == (0, 1)
    assert tuple(item["timeline_item"]["kind"] for item in completed) == (
        "reply",
        "assistant_text",
    )


def test_completed_item_semantics_do_not_depend_on_fold_page_boundaries() -> None:
    events = (
        _at_sequence(
            ReplyStartEvent(**_CTX.event_fields(), name="assistant"),
            1,
        ),
        _at_sequence(
            make_text_block_segment_event(
                **_CTX.event_fields(),
                block_id="text:incremental",
                delta="same source prefix",
            ),
            2,
        ),
        _at_sequence(
            ReplyEndEvent(
                **_CTX.event_fields(),
                model_terminal_outcome="completed",
            ),
            3,
        ),
    )
    eager = IncrementalRunTimelineReducer(
        runtime_session_id="runtime:incremental-timeline",
        run_id=_CTX.run_id,
    )
    eager_items = []
    for event in events:
        eager.apply(event)
        eager_items.extend(eager.take_completed_items())

    batched = IncrementalRunTimelineReducer(
        runtime_session_id="runtime:incremental-timeline",
        run_id=_CTX.run_id,
    )
    for event in events:
        batched.apply(event)
    assert tuple(eager_items) == batched.take_completed_items()
    assert eager.open_state_payload() == batched.open_state_payload()


def test_one_hundred_thousand_event_prefix_keeps_only_bounded_open_state() -> None:
    ignored_terminal = ReplyEndEvent(
        **_CTX.event_fields(),
        model_terminal_outcome="completed",
    )
    reducer = IncrementalRunTimelineReducer(
        runtime_session_id="runtime:incremental-timeline",
        run_id=_CTX.run_id,
    )
    for sequence in range(1, 100_001):
        reducer.apply(_at_sequence(ignored_terminal, sequence))
        assert reducer.take_completed_items() == ()
    assert reducer.next_item_ordinal == 0
    assert reducer.open_state_payload()["open_items"] == {}
