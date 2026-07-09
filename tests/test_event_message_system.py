import pytest

from pulsara_agent.event import (
    DataBlockDeltaEvent,
    DataBlockEndEvent,
    DataBlockStartEvent,
    EventContext,
    ExternalExecutionResultEvent,
    ModelCallEndEvent,
    ProjectionReadyEvent,
    RequireExternalExecutionEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolObservationTiming,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.event_log.serialization import dump_agent_event, load_agent_event
from pulsara_agent.message import (
    Base64Source,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultArtifactRef,
    ToolResultBlock,
    ToolResultState,
)


CTX = EventContext(run_id="run:test", turn_id="turn:test", reply_id="reply:test")


def _external_timing(tool_call_id: str, tool_name: str) -> dict[str, object]:
    return {
        "observed_at": "2026-07-09T00:00:00+00:00",
        "source_started_at": "2026-07-09T00:00:00+00:00",
        "source_ended_at": "2026-07-09T00:00:00+00:00",
        "freshness": "current_tool_observation",
        "clock_source": "tool_runtime_metadata",
        "tool_origin": "unknown",
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
    }


def test_message_reducer_replays_text_thinking_tool_events() -> None:
    event_log = InMemoryEventLog()
    event_log.extend(
        [
            ReplyStartEvent(**CTX.event_fields(), name="assistant"),
            TextBlockStartEvent(**CTX.event_fields(), block_id="text:1"),
            TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="hello "),
            TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="world"),
            TextBlockEndEvent(**CTX.event_fields(), block_id="text:1"),
            ThinkingBlockStartEvent(**CTX.event_fields(), block_id="thinking:1"),
            ThinkingBlockDeltaEvent(**CTX.event_fields(), block_id="thinking:1", delta="plan"),
            ThinkingBlockEndEvent(**CTX.event_fields(), block_id="thinking:1"),
            DataBlockStartEvent(**CTX.event_fields(), block_id="data:1", media_type="image/png"),
            DataBlockDeltaEvent(**CTX.event_fields(), block_id="data:1", data="abc", media_type="image/png"),
            DataBlockEndEvent(**CTX.event_fields(), block_id="data:1"),
            ToolCallStartEvent(
                **CTX.event_fields(),
                tool_call_id="call:1",
                tool_call_name="lookup",
            ),
            ToolCallDeltaEvent(**CTX.event_fields(), tool_call_id="call:1", delta='{"q"'),
            ToolCallDeltaEvent(**CTX.event_fields(), tool_call_id="call:1", delta=':"x"}'),
            ToolCallEndEvent(**CTX.event_fields(), tool_call_id="call:1"),
            ToolResultStartEvent(
                **CTX.event_fields(),
                tool_call_id="call:1",
                tool_call_name="lookup",
            ),
            ToolResultTextDeltaEvent(**CTX.event_fields(), tool_call_id="call:1", delta="found"),
            ToolResultEndEvent(
                **CTX.event_fields(),
                tool_call_id="call:1",
                state=ToolResultState.SUCCESS,
                metadata={"tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}},
            ),
            ExternalExecutionResultEvent(
                **CTX.event_fields(),
                metadata={
                    "tool_observation_timing_by_call_id": {
                        "call:external": _external_timing("call:external", "external_lookup")
                    }
                },
                execution_results=[
                    ToolResultBlock(
                        id="call:external",
                        name="external_lookup",
                        output=[TextBlock(text="external result")],
                        state=ToolResultState.SUCCESS,
                    )
                ],
            ),
            ModelCallEndEvent(**CTX.event_fields(), input_tokens=3, output_tokens=4, total_tokens=7),
            ModelCallEndEvent(**CTX.event_fields(), input_tokens=1, output_tokens=2, total_tokens=3),
            ReplyEndEvent(**CTX.event_fields()),
        ]
    )

    msg = event_log.replay("reply:test")

    assert msg.id == "reply:test"
    assert msg.content[0].type == "text"
    assert msg.content[0].text == "hello world"
    assert msg.content[1].type == "thinking"
    assert msg.content[1].thinking == "plan"
    assert msg.content[2].type == "data"
    assert isinstance(msg.content[2].source, Base64Source)
    assert msg.content[2].source.data == "abc"
    assert msg.content[3].type == "tool_call"
    assert msg.content[3].input == '{"q":"x"}'
    assert msg.content[4].type == "tool_result"
    assert msg.content[4].output[0].text == "found"
    assert msg.content[4].state is ToolResultState.SUCCESS
    assert msg.content[5].type == "tool_result"
    assert msg.content[5].output[0].text == "external result"
    assert (
        msg.metadata["tool_observation_timing_by_call_id"]["call:external"]["observed_at"]
        == "2026-07-09T00:00:00Z"
    )
    assert msg.usage is not None
    assert msg.usage.input_tokens == 4
    assert msg.usage.output_tokens == 6
    assert msg.usage.total_tokens == 10
    assert msg.finished_at is not None


def test_tool_result_end_event_artifacts_round_trip_into_block() -> None:
    artifact = ToolResultArtifactRef(
        artifact_id="artifact:tool-result:run-test:call-1:output:0",
        role="output",
        media_type="text/plain; charset=utf-8",
        size_bytes=123,
    )
    event = ToolResultEndEvent(
        **CTX.event_fields(),
        tool_call_id="call:1",
        state=ToolResultState.SUCCESS,
        artifacts=[artifact],
        metadata={"tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}},
    )
    loaded = load_agent_event(dump_agent_event(event))
    assert isinstance(loaded, ToolResultEndEvent)
    assert loaded.artifacts == [artifact]

    event_log = InMemoryEventLog()
    event_log.extend(
        [
            ToolResultStartEvent(**CTX.event_fields(), tool_call_id="call:1", tool_call_name="lookup"),
            ToolResultTextDeltaEvent(**CTX.event_fields(), tool_call_id="call:1", delta="preview"),
            loaded,
        ]
    )

    msg = event_log.replay("reply:test")
    assert isinstance(msg.content[0], ToolResultBlock)
    assert msg.content[0].artifacts == [artifact]


def test_external_execution_result_requires_tool_observation_timing_map() -> None:
    with pytest.raises(ValueError, match="tool_observation_timing_by_call_id"):
        ExternalExecutionResultEvent(
            **CTX.event_fields(),
            execution_results=[
                ToolResultBlock(
                    id="call:external",
                    name="external_lookup",
                    output=[TextBlock(text="external result")],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )

    with pytest.raises(ValueError, match="ExternalExecutionResultEvent timing"):
        ExternalExecutionResultEvent(
            **CTX.event_fields(),
            metadata={"tool_observation_timing_by_call_id": {"call:external": {}}},
            execution_results=[
                ToolResultBlock(
                    id="call:external",
                    name="external_lookup",
                    output=[TextBlock(text="external result")],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )

    with pytest.raises(ValueError, match="unknown tool result ids"):
        ExternalExecutionResultEvent(
            **CTX.event_fields(),
            metadata={
                "tool_observation_timing_by_call_id": {
                    "call:external": _external_timing("call:external", "external_lookup"),
                    "junk": {},
                }
            },
            execution_results=[
                ToolResultBlock(
                    id="call:external",
                    name="external_lookup",
                    output=[TextBlock(text="external result")],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )

    with pytest.raises(ValueError, match="tool_call_id mismatch"):
        ExternalExecutionResultEvent(
            **CTX.event_fields(),
            metadata={
                "tool_observation_timing_by_call_id": {
                    "call:external": _external_timing("call:other", "external_lookup")
                }
            },
            execution_results=[
                ToolResultBlock(
                    id="call:external",
                    name="external_lookup",
                    output=[TextBlock(text="external result")],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )


def test_tool_observation_timing_requires_utc_iso_and_non_negative_duration() -> None:
    normalized = ToolObservationTiming(observed_at="2026-07-09T08:00:00+08:00")
    assert normalized.observed_at == "2026-07-09T00:00:00Z"

    with pytest.raises(ValueError, match="UTC offset"):
        ToolObservationTiming(observed_at="2026-07-09T00:00:00")

    with pytest.raises(ValueError, match="duration must be finite and non-negative"):
        ToolObservationTiming(
            observed_at="2026-07-09T00:00:00Z",
            observation_duration_seconds=-0.1,
        )

    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="duration must be finite and non-negative"):
            ToolObservationTiming(
                observed_at="2026-07-09T00:00:00Z",
                observation_duration_seconds=value,
            )


def test_tool_result_end_timing_tool_call_id_must_match_carrier() -> None:
    with pytest.raises(ValueError, match="tool_call_id mismatch"):
        ToolResultEndEvent(
            **CTX.event_fields(),
            tool_call_id="call:a",
            state=ToolResultState.SUCCESS,
            metadata={
                "tool_observation_timing": {
                    "observed_at": "2026-07-09T00:00:00Z",
                    "tool_call_id": "call:b",
                }
            },
        )


def test_external_execution_result_rejects_duplicate_result_ids() -> None:
    with pytest.raises(ValueError, match="duplicate ids"):
        ExternalExecutionResultEvent(
            **CTX.event_fields(),
            metadata={"tool_observation_timing_by_call_id": {"call:external": _external_timing("call:external", "external_lookup")}},
            execution_results=[
                ToolResultBlock(
                    id="call:external",
                    name="external_lookup",
                    output=[TextBlock(text="first")],
                    state=ToolResultState.SUCCESS,
                ),
                ToolResultBlock(
                    id="call:external",
                    name="external_lookup",
                    output=[TextBlock(text="second")],
                    state=ToolResultState.SUCCESS,
                ),
            ],
        )


def test_message_reducer_preserves_block_start_order_for_interleaved_events() -> None:
    event_log = InMemoryEventLog()
    event_log.extend(
        [
            ReplyStartEvent(**CTX.event_fields(), name="assistant"),
            TextBlockStartEvent(**CTX.event_fields(), block_id="text:first"),
            TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:first", delta="before tool"),
            ToolCallStartEvent(**CTX.event_fields(), tool_call_id="call:later", tool_call_name="lookup"),
            ToolCallDeltaEvent(**CTX.event_fields(), tool_call_id="call:later", delta="{}"),
            ToolCallEndEvent(**CTX.event_fields(), tool_call_id="call:later"),
            TextBlockEndEvent(**CTX.event_fields(), block_id="text:first"),
            ReplyEndEvent(**CTX.event_fields()),
        ]
    )

    msg = event_log.replay("reply:test")

    assert [block.type for block in msg.content] == ["text", "tool_call"]
    assert msg.content[0].text == "before tool"
    assert msg.content[1].input == "{}"


def test_message_reducer_marks_external_tool_call_finished_when_result_arrives() -> None:
    event_log = InMemoryEventLog()
    tool_call = ToolCallBlock(id="call:external", name="external_lookup", input="{}")
    event_log.extend(
        [
            ReplyStartEvent(**CTX.event_fields(), name="assistant"),
            ToolCallStartEvent(
                **CTX.event_fields(),
                tool_call_id=tool_call.id,
                tool_call_name=tool_call.name,
            ),
            ToolCallDeltaEvent(**CTX.event_fields(), tool_call_id=tool_call.id, delta=tool_call.input),
            ToolCallEndEvent(**CTX.event_fields(), tool_call_id=tool_call.id),
            RequireExternalExecutionEvent(**CTX.event_fields(), tool_calls=[tool_call]),
            ExternalExecutionResultEvent(
                **CTX.event_fields(),
                metadata={
                    "tool_observation_timing_by_call_id": {
                        tool_call.id: _external_timing(tool_call.id, tool_call.name)
                    }
                },
                execution_results=[
                    ToolResultBlock(
                        id=tool_call.id,
                        name=tool_call.name,
                        output=[TextBlock(text="external result")],
                        state=ToolResultState.SUCCESS,
                    )
                ],
            ),
            ReplyEndEvent(**CTX.event_fields()),
        ]
    )

    msg = event_log.replay("reply:test")

    call = msg.content[0]
    result = msg.content[1]
    assert isinstance(call, ToolCallBlock)
    assert call.state is ToolCallState.FINISHED
    assert isinstance(result, ToolResultBlock)
    assert result.id == "call:external"
    assert result.state is ToolResultState.SUCCESS
    assert result.output[0].text == "external result"


def test_projection_events_are_not_written_as_canonical_memory() -> None:
    event_log = InMemoryEventLog()
    event = event_log.append(
        ProjectionReadyEvent(
            **CTX.event_fields(),
            projection_id="projection:1",
            role="DA",
            scope="ctx:test",
            token_budget=1000,
            included_memory_ids=["claim:1"],
            filtered_memory_ids=["claim:stale"],
            summary="Projection summary.",
        )
    )

    assert event.sequence == 1
    assert event_log.iter(reply_id="reply:test")[0].type == "PROJECTION_READY"
