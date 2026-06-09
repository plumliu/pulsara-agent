from pulsara_agent.event import (
    DataBlockDeltaEvent,
    DataBlockEndEvent,
    DataBlockStartEvent,
    EventContext,
    ExternalExecutionResultEvent,
    InMemoryEventLog,
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
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.message import Base64Source, TextBlock, ToolCallBlock, ToolCallState, ToolResultBlock, ToolResultState
from pulsara_agent.ontology import memory


CTX = EventContext(run_id="run:test", turn_id="turn:test", reply_id="reply:test")


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
            ),
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
    assert msg.usage is not None
    assert msg.usage.input_tokens == 4
    assert msg.usage.output_tokens == 6
    assert msg.usage.total_tokens == 10
    assert msg.finished_at is not None


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


def test_memory_write_gate_emits_memory_events() -> None:
    gate = MemoryWriteGate()

    decision, events = gate.evaluate_claim_with_events(
        event_context=CTX,
        candidate_id="candidate:1",
        memory_id="claim:1",
        statement="Use JSON-LD for semantic memory.",
        scope="ctx:test",
        evidence_ids=["evidence:1"],
        source_authority=memory.SourceAuthority.TOOL_RESULT,
        verification_status=memory.VerificationStatus.TOOL_VERIFIED,
    )

    assert decision.accepted
    assert events[0].type == "MEMORY_CANDIDATE_PROPOSED"
    assert events[1].type == "MEMORY_WRITE_ACCEPTED"
    assert events[1].memory_id == "claim:1"
    assert events[1].gate_reason == "accepted"


def test_memory_write_gate_rejects_unsupported_claim_with_events() -> None:
    gate = MemoryWriteGate()

    decision, events = gate.evaluate_claim_with_events(
        event_context=CTX,
        candidate_id="candidate:2",
        memory_id="claim:2",
        statement="Weak inferred claim.",
        scope="ctx:test",
        evidence_ids=[],
        source_authority=memory.SourceAuthority.MODEL_INFERENCE,
        verification_status=memory.VerificationStatus.INFERRED,
    )

    assert not decision.accepted
    assert events[0].type == "MEMORY_CANDIDATE_PROPOSED"
    assert events[1].type == "MEMORY_WRITE_REJECTED"
    assert events[1].candidate_id == "candidate:2"


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
