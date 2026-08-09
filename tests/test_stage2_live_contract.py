from __future__ import annotations

from pulsara_agent.conversation_kernel.assembler import (
    CompletedTextBlock,
    CompletedToolCallBlock,
    ProviderStreamAssembler,
)
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live import LiveBlockKind, LiveChannelKind
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    live_digest,
)
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType


def _assembler() -> ProviderStreamAssembler:
    return ProviderStreamAssembler(
        session_id="session:test",
        turn_id="turn:test",
        live_bus=LiveAgentEventBus(),
        proposed_entry_id="entry:assistant",
    )


def test_interleaved_text_and_tool_blocks_freeze_start_order() -> None:
    assembler = _assembler()
    stream = (
        TextStartPayload("text:first"),
        ToolCallStartPayload(
            block_identity="call:second",
            tool_call_id="call:second",
            tool_name="read_file",
        ),
        ToolCallDeltaPayload(
            block_identity="call:second",
            tool_call_id="call:second",
            delta='{"path":"README.md"}',
        ),
        ToolCallEndPayload(
            block_identity="call:second",
            tool_call_id="call:second",
            tool_name="read_file",
            arguments_json='{"path":"README.md"}',
            utf8_bytes=20,
            digest=live_digest('{"path":"README.md"}'),
        ),
        TextDeltaPayload("text:first", "first"),
        TextEndPayload("text:first", "first", 5, live_digest("first")),
    )
    for item in stream:
        assembler.apply(item)
    completed = assembler.complete()
    assert [type(block) for block in completed.blocks] == [
        CompletedTextBlock,
        CompletedToolCallBlock,
    ]
    assert completed.public_text == "first"


def test_interleaved_multiple_tool_calls_freeze_start_order() -> None:
    assembler = _assembler()
    stream = (
        ToolCallStartPayload(
            block_identity="call:first",
            tool_call_id="call:first",
            tool_name="read_file",
        ),
        ToolCallStartPayload(
            block_identity="call:second",
            tool_call_id="call:second",
            tool_name="search_files",
        ),
        ToolCallDeltaPayload(
            block_identity="call:second",
            tool_call_id="call:second",
            delta='{"query":"second"}',
        ),
        ToolCallEndPayload(
            block_identity="call:second",
            tool_call_id="call:second",
            tool_name="search_files",
            arguments_json='{"query":"second"}',
            utf8_bytes=18,
            digest=live_digest('{"query":"second"}'),
        ),
        ToolCallDeltaPayload(
            block_identity="call:first",
            tool_call_id="call:first",
            delta='{"path":"first"}',
        ),
        ToolCallEndPayload(
            block_identity="call:first",
            tool_call_id="call:first",
            tool_name="read_file",
            arguments_json='{"path":"first"}',
            utf8_bytes=16,
            digest=live_digest('{"path":"first"}'),
        ),
    )
    for item in stream:
        assembler.apply(item)
    completed = assembler.complete()
    assert [block.tool_call_id for block in completed.blocks] == [
        "call:first",
        "call:second",
    ]


def test_live_snapshot_has_an_independent_byte_bound_and_exact_suffix_cut() -> None:
    bus = LiveAgentEventBus(maximum_events=8, maximum_payload_bytes=1 << 20)
    for ordinal in range(3):
        assert (
            bus.offer_nowait(
                event_type=LiveEventType.TEXT_DELTA,
                session_id="session:test",
                turn_id="turn:test",
                draft_identity="entry:assistant",
                payload=TextDeltaPayload("block:text", str(ordinal) * 200_000),
                channel_kind=LiveChannelKind.MODEL_OUTPUT,
                generation_id="live-generation:test",
                proposed_entry_id="entry:assistant",
                block_id="block:text",
                block_ordinal=0,
                block_kind=LiveBlockKind.TEXT,
            )
            is not None
        )
    observer_id, snapshot = bus.subscribe_with_snapshot(
        maximum_events=8,
        maximum_bytes=250_000,
    )
    assert observer_id
    assert snapshot.truncated_before
    assert snapshot.retained_from_revision == 3
    assert snapshot.through_revision == 3
    assert [event.revision for event in snapshot.events] == [3]
