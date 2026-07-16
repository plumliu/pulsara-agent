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
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.event_log.serialization import dump_agent_event, load_agent_event
from pulsara_agent.message import (
    AssistantMsg,
    Base64Source,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultArtifactRef,
    ToolResultBlock,
    ToolResultState,
)
from pulsara_agent.message.reducer import MessageReducer
from tests.support import model_call_end_fields
from tests.conftest import (
    external_terminal_projection_references,
    external_tool_call_requirement_fact,
    external_tool_result_ingress_fact,
    tool_result_end_contract_fields,
)
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact


CTX = EventContext(run_id="run:test", turn_id="turn:test", reply_id="reply:test")


def _external_result_event(*ingresses, sequence=None):
    values = tuple(ingresses)
    return ExternalExecutionResultEvent(
        **CTX.event_fields(),
        external_results=values,
        terminal_projections=external_terminal_projection_references(values),
        sequence=sequence,
    )


def _reduce_message_events(event_log: InMemoryEventLog):
    events = event_log.iter(reply_id=CTX.reply_id)
    start = next(
        (event for event in events if isinstance(event, ReplyStartEvent)), None
    )
    reducer = MessageReducer(
        AssistantMsg(
            id=CTX.reply_id,
            name=start.name if start is not None else "assistant",
            content=[],
            created_at=start.created_at if start is not None else None,
        )
    )
    for event in events:
        reducer.append(event)
    return reducer.message


def test_terminal_projection_carriers_are_required_by_durable_schema() -> None:
    model_payload = ModelCallEndEvent(
        **CTX.event_fields(),
        **model_call_end_fields(),
    ).model_dump(mode="json")
    model_payload.pop("terminal_projection")
    with pytest.raises(ValueError, match="terminal_projection"):
        ModelCallEndEvent.model_validate(model_payload)

    tool_payload = ToolResultEndEvent(
        **CTX.event_fields(),
        **tool_result_end_contract_fields("call:required", tool_name="lookup"),
        tool_call_id="call:required",
        state=ToolResultState.SUCCESS,
    ).model_dump(mode="json")
    tool_payload.pop("terminal_projection")
    with pytest.raises(ValueError, match="terminal_projection"):
        ToolResultEndEvent.model_validate(tool_payload)

    ingress = external_tool_result_ingress_fact(
        ToolResultBlock(
            id="call:external-required",
            name="external_lookup",
            output=[TextBlock(text="done")],
            state=ToolResultState.SUCCESS,
        )
    )
    external_payload = _external_result_event(ingress).model_dump(mode="json")
    external_payload.pop("terminal_projections")
    with pytest.raises(ValueError, match="terminal_projections"):
        ExternalExecutionResultEvent.model_validate(external_payload)
    with pytest.raises(ValueError, match="terminal projections differ"):
        ExternalExecutionResultEvent.model_validate(
            {**external_payload, "terminal_projections": []}
        )


def test_message_reducer_replays_text_thinking_tool_events() -> None:
    event_log = InMemoryEventLog()
    event_log.extend(
        [
            ReplyStartEvent(**CTX.event_fields(), name="assistant"),
            TextBlockStartEvent(**CTX.event_fields(), block_id="text:1"),
            TextBlockDeltaEvent(
                **CTX.event_fields(), block_id="text:1", delta="hello "
            ),
            TextBlockDeltaEvent(**CTX.event_fields(), block_id="text:1", delta="world"),
            TextBlockEndEvent(**CTX.event_fields(), block_id="text:1"),
            ThinkingBlockStartEvent(**CTX.event_fields(), block_id="thinking:1"),
            ThinkingBlockDeltaEvent(
                **CTX.event_fields(), block_id="thinking:1", delta="plan"
            ),
            ThinkingBlockEndEvent(**CTX.event_fields(), block_id="thinking:1"),
            DataBlockStartEvent(
                **CTX.event_fields(), block_id="data:1", media_type="image/png"
            ),
            DataBlockDeltaEvent(
                **CTX.event_fields(),
                block_id="data:1",
                data="abc",
                media_type="image/png",
            ),
            DataBlockEndEvent(**CTX.event_fields(), block_id="data:1"),
            ToolCallStartEvent(
                **CTX.event_fields(),
                tool_call_id="call:1",
                tool_call_name="lookup",
            ),
            ToolCallDeltaEvent(
                **CTX.event_fields(), tool_call_id="call:1", delta='{"q"'
            ),
            ToolCallDeltaEvent(
                **CTX.event_fields(), tool_call_id="call:1", delta=':"x"}'
            ),
            ToolCallEndEvent(**CTX.event_fields(), tool_call_id="call:1"),
            ToolResultStartEvent(
                **CTX.event_fields(),
                tool_call_id="call:1",
                tool_call_name="lookup",
            ),
            ToolResultTextDeltaEvent(
                **CTX.event_fields(), tool_call_id="call:1", delta="found"
            ),
            ToolResultEndEvent(
                **CTX.event_fields(),
                **tool_result_end_contract_fields("call:1", tool_name="lookup"),
                tool_call_id="call:1",
                state=ToolResultState.SUCCESS,
                metadata={
                    "tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}
                },
            ),
            _external_result_event(
                    external_tool_result_ingress_fact(
                        ToolResultBlock(
                            id="call:external",
                            name="external_lookup",
                            output=[TextBlock(text="external result")],
                            state=ToolResultState.SUCCESS,
                        )
                    )
            ),
            ModelCallEndEvent(
                **CTX.event_fields(),
                **model_call_end_fields(input_tokens=3, output_tokens=4),
            ),
            ModelCallEndEvent(
                **CTX.event_fields(),
                **model_call_end_fields(input_tokens=1, output_tokens=2),
            ),
            ReplyEndEvent(**CTX.event_fields(), model_terminal_outcome="completed"),
        ]
    )

    msg = _reduce_message_events(event_log)

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
        msg.metadata["tool_observation_timing_by_call_id"]["call:external"][
            "observed_at"
        ]
        == "2026-07-09T00:00:00.000000Z"
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
        **tool_result_end_contract_fields("call:1", tool_name="lookup"),
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
            ToolResultStartEvent(
                **CTX.event_fields(), tool_call_id="call:1", tool_call_name="lookup"
            ),
            ToolResultTextDeltaEvent(
                **CTX.event_fields(), tool_call_id="call:1", delta="preview"
            ),
            loaded,
        ]
    )

    msg = _reduce_message_events(event_log)
    assert isinstance(msg.content[0], ToolResultBlock)
    assert msg.content[0].artifacts == [artifact]


def test_external_execution_result_requires_typed_non_empty_ingress() -> None:
    with pytest.raises(ValueError, match="requires external results"):
        ExternalExecutionResultEvent(
            **CTX.event_fields(),
            external_results=(),
            terminal_projections=(),
        )


def test_tool_observation_timing_requires_utc_iso_and_non_negative_duration() -> None:
    normalized = ToolObservationTimingFact(observed_at_utc="2026-07-09T08:00:00+08:00")
    assert normalized.observed_at_utc == "2026-07-09T00:00:00.000000Z"

    with pytest.raises(ValueError, match="timezone-aware"):
        ToolObservationTimingFact(observed_at_utc="2026-07-09T00:00:00")

    with pytest.raises(ValueError):
        ToolObservationTimingFact(
            observed_at_utc="2026-07-09T00:00:00Z",
            observation_duration_seconds=-0.1,
        )

    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            ToolObservationTimingFact(
                observed_at_utc="2026-07-09T00:00:00Z",
                observation_duration_seconds=value,
            )


def test_tool_result_end_timing_tool_call_id_must_match_carrier() -> None:
    fields = tool_result_end_contract_fields("call:a", tool_name="lookup")
    fields["observation_timing"] = ToolObservationTimingFact(
        observed_at_utc="2026-07-09T00:00:00Z",
        tool_call_id="call:b",
    )
    with pytest.raises(ValueError, match="tool_call_id mismatch"):
        ToolResultEndEvent(
            **CTX.event_fields(),
            **fields,
            tool_call_id="call:a",
            state=ToolResultState.SUCCESS,
        )


def test_external_execution_result_rejects_duplicate_result_ids() -> None:
    ingress = external_tool_result_ingress_fact(
        ToolResultBlock(
            id="call:external",
            name="external_lookup",
            output=[TextBlock(text="first")],
            state=ToolResultState.SUCCESS,
        )
    )
    with pytest.raises(ValueError, match="duplicate ids"):
        ExternalExecutionResultEvent(
            **CTX.event_fields(),
            external_results=(ingress, ingress),
            terminal_projections=external_terminal_projection_references(
                (ingress, ingress)
            ),
        )


def test_message_reducer_preserves_block_start_order_for_interleaved_events() -> None:
    event_log = InMemoryEventLog()
    event_log.extend(
        [
            ReplyStartEvent(**CTX.event_fields(), name="assistant"),
            TextBlockStartEvent(**CTX.event_fields(), block_id="text:first"),
            TextBlockDeltaEvent(
                **CTX.event_fields(), block_id="text:first", delta="before tool"
            ),
            ToolCallStartEvent(
                **CTX.event_fields(), tool_call_id="call:later", tool_call_name="lookup"
            ),
            ToolCallDeltaEvent(
                **CTX.event_fields(), tool_call_id="call:later", delta="{}"
            ),
            ToolCallEndEvent(**CTX.event_fields(), tool_call_id="call:later"),
            TextBlockEndEvent(**CTX.event_fields(), block_id="text:first"),
            ReplyEndEvent(**CTX.event_fields(), model_terminal_outcome="completed"),
        ]
    )

    msg = _reduce_message_events(event_log)

    assert [block.type for block in msg.content] == ["text", "tool_call"]
    assert msg.content[0].text == "before tool"
    assert msg.content[1].input == "{}"


def test_message_reducer_marks_external_tool_call_finished_when_result_arrives() -> (
    None
):
    event_log = InMemoryEventLog()
    tool_call = ToolCallBlock(id="call:external", name="external_lookup", input="{}")
    requirement = external_tool_call_requirement_fact(
        tool_call.id, tool_name=tool_call.name
    )
    require_event_id = "require-external:message-reducer"
    event_log.extend(
        [
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
            RequireExternalExecutionEvent(
                id=require_event_id,
                **CTX.event_fields(),
                external_tool_calls=(requirement,),
            ),
            _external_result_event(
                    external_tool_result_ingress_fact(
                        ToolResultBlock(
                            id=tool_call.id,
                            name=tool_call.name,
                            output=[TextBlock(text="external result")],
                            state=ToolResultState.SUCCESS,
                        ),
                        requirement=requirement,
                        require_event_id=require_event_id,
                    )
            ),
            ReplyEndEvent(**CTX.event_fields(), model_terminal_outcome="completed"),
        ]
    )

    msg = _reduce_message_events(event_log)

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
            projection_kind="memory",
            included_memory_ids=["claim:1"],
            filtered_memory_ids=["claim:stale"],
            summary="Projection summary.",
        )
    )

    assert event.sequence == 1
    assert event_log.iter(reply_id="reply:test")[0].type == "PROJECTION_READY"
