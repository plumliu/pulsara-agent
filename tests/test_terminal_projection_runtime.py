from __future__ import annotations

import asyncio

import pytest

from pulsara_agent.event import (
    EventType,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTerminalProjectionCommittedEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.llm.terminal_projection import hydrate_terminal_projection
from pulsara_agent.message import ToolResultState
from pulsara_agent.primitives.terminal_projection import (
    CanonicalToolResultDataBlockSemanticFact,
)
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from tests.conftest import tool_result_end_candidate
from tests.support.runtime_session import in_memory_runtime_session


def _tool_events():
    common = {"run_id": "run:tool", "turn_id": "turn:tool", "reply_id": "reply:tool"}
    return (
        ToolResultStartEvent(
            id="tool-start",
            **common,
            tool_call_id="call:tool",
            tool_call_name="read_file",
        ),
        ToolResultTextDeltaEvent(
            id="tool-text-1",
            **common,
            tool_call_id="call:tool",
            delta="hello ",
        ),
        ToolResultTextDeltaEvent(
            id="tool-text-2",
            **common,
            tool_call_id="call:tool",
            delta="world",
        ),
        ToolResultDataDeltaEvent(
            id="tool-data",
            **common,
            tool_call_id="call:tool",
            block_id="data:1",
            media_type="APPLICATION/JSON; CHARSET=UTF-8",
            data='{"ok":true}',
        ),
        tool_result_end_candidate(
            event_id="tool-end",
            **common,
            tool_call_id="call:tool",
            tool_name="read_file",
            state=ToolResultState.SUCCESS,
        ),
    )


def test_tool_terminal_projection_is_atomic_and_independently_hydratable(
    tmp_path,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)

    committed = asyncio.run(runtime_session.emit_many(_tool_events()))

    projection = next(
        event
        for event in committed
        if isinstance(event, ToolResultTerminalProjectionCommittedEvent)
    )
    terminal = next(event for event in committed if isinstance(event, ToolResultEndEvent))
    assert [event.type for event in committed][-2:] == [
        EventType.TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED,
        EventType.TOOL_RESULT_END,
    ]
    assert terminal.terminal_projection is not None
    assert (
        terminal.terminal_projection.projection_reference
        == projection.projection_reference
    )

    document = asyncio.run(
        hydrate_terminal_projection(runtime_session, projection.projection_reference)
    )
    block = document.payload.canonical_result_block
    assert block.content_blocks[0].content.text == "hello world"  # type: ignore[union-attr]
    data = block.content_blocks[1]
    assert isinstance(data.semantic_identity, CanonicalToolResultDataBlockSemanticFact)
    assert data.semantic_identity.media_type == "application/json; charset=utf-8"
    assert data.content.text == '{"ok":true}'  # type: ignore[union-attr]
    assert document.source_fact.source_delta_count == 3  # type: ignore[union-attr]


def test_tool_terminal_projection_same_batch_identity_is_fail_closed(tmp_path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    prepared = asyncio.run(
        runtime_session.tool_terminal_projection_service.prepare_batch(_tool_events())
    )
    projection_index = next(
        index
        for index, event in enumerate(prepared)
        if isinstance(event, ToolResultTerminalProjectionCommittedEvent)
    )
    drifted = list(prepared)
    projection = drifted[projection_index]
    drifted[projection_index] = projection.model_copy(
        update={"metadata": {**projection.metadata, "drift": True}}
    )

    with pytest.raises(ValueError, match="identity drifted"):
        runtime_session.write_events_from_thread(tuple(drifted))

    assert tuple(runtime_session.event_log.iter()) == ()


def test_tool_terminal_projection_rejects_end_timing_drift_before_commit(
    tmp_path,
) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    prepared = asyncio.run(
        runtime_session.tool_terminal_projection_service.prepare_batch(_tool_events())
    )
    terminal_index = next(
        index
        for index, event in enumerate(prepared)
        if isinstance(event, ToolResultEndEvent)
    )
    terminal = prepared[terminal_index]
    assert isinstance(terminal, ToolResultEndEvent)
    drifted_terminal = ToolResultEndEvent.model_validate(
        {
            **terminal.model_dump(mode="json"),
            "observation_timing": ToolObservationTimingFact(
                observed_at_utc="2035-01-01T00:00:00Z",
                source_started_at_utc="2035-01-01T00:00:00Z",
                source_ended_at_utc="2035-01-01T00:00:00Z",
                observation_duration_seconds=0,
                freshness="current_tool_observation",
                clock_source="tool_result_events",
                tool_origin="unknown",
                tool_name="read_file",
                tool_call_id="call:tool",
            ).model_dump(mode="json"),
        }
    )
    drifted = (*prepared[:terminal_index], drifted_terminal)

    with pytest.raises(ValueError, match="document drifted from End facts"):
        runtime_session.write_events_from_thread(drifted)

    assert tuple(runtime_session.event_log.iter()) == ()
