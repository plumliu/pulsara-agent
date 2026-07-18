from tests.support.model_stream import (
    make_data_block_end_event,
    make_data_block_segment_event,
    make_data_block_start_event,
    make_text_block_end_event,
    make_text_block_segment_event,
    make_text_block_start_event,
    make_thinking_block_end_event,
    make_thinking_block_segment_event,
    make_thinking_block_start_event,
    make_tool_call_arguments_segment_event,
    make_tool_call_end_event,
    make_tool_call_start_event,
)

from pulsara_agent.event import (
    EventContext,
    ExternalExecutionResultEvent,
    HintBlockEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.message import (
    Base64Source,
    DataBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolResultState,
    URLSource,
)
from pulsara_agent.message.assembler import (
    BlockAssembler,
    completed_tool_result_from_events,
)
from tests.conftest import (
    external_terminal_projection_references,
    external_tool_result_ingress_fact,
    tool_result_end_contract_fields,
)


CTX = EventContext(run_id="run:block", turn_id="turn:block", reply_id="reply:block")
CTX_A = EventContext(run_id="run:block", turn_id="turn:block", reply_id="reply:a")
CTX_B = EventContext(run_id="run:block", turn_id="turn:block", reply_id="reply:b")


def test_block_assembler_completes_text_thinking_and_tool_call_blocks() -> None:
    assembler = BlockAssembler()
    completions = []
    started = []
    text_start = make_text_block_start_event(
        **CTX.event_fields(), block_id="text:1", sequence=1
    )
    text_end = make_text_block_end_event(**CTX.event_fields(), block_id="text:1", sequence=4)

    for event in [
        text_start,
        make_text_block_segment_event(
            **CTX.event_fields(), block_id="text:1", delta="hello ", sequence=2
        ),
        make_text_block_segment_event(
            **CTX.event_fields(), block_id="text:1", delta="world", sequence=3
        ),
        text_end,
        make_thinking_block_start_event(
            **CTX.event_fields(), block_id="thinking:1", sequence=5
        ),
        make_thinking_block_segment_event(
            **CTX.event_fields(), block_id="thinking:1", delta="plan", sequence=6
        ),
        make_thinking_block_end_event(**CTX.event_fields(), block_id="thinking:1", sequence=7),
        make_tool_call_start_event(
            **CTX.event_fields(),
            tool_call_id="call:1",
            tool_call_name="lookup",
            sequence=8,
        ),
        make_tool_call_arguments_segment_event(
            **CTX.event_fields(), tool_call_id="call:1", delta='{"q"', sequence=9
        ),
        make_tool_call_arguments_segment_event(
            **CTX.event_fields(), tool_call_id="call:1", delta=':"x"}', sequence=10
        ),
        make_tool_call_end_event(**CTX.event_fields(), tool_call_id="call:1", sequence=11),
    ]:
        update = assembler.append(event)
        started.extend(update.started)
        completions.extend(update.completed)

    assert [block.type for block in started] == ["text", "thinking", "tool_call"]
    assert len(completions) == 3
    assert isinstance(completions[0].block, TextBlock)
    assert completions[0].block.text == "hello world"
    assert completions[0].start_sequence == 1
    assert completions[0].end_sequence == 4
    assert completions[0].start_event_id == text_start.id
    assert completions[0].end_event_id == text_end.id
    assert isinstance(completions[1].block, ThinkingBlock)
    assert completions[1].block.thinking == "plan"
    assert completions[2].block.input == '{"q":"x"}'


def test_block_assembler_isolates_active_blocks_by_reply_id() -> None:
    assembler = BlockAssembler()
    completions = []

    for event in [
        make_text_block_start_event(**CTX_A.event_fields(), block_id="text:1", sequence=1),
        make_text_block_segment_event(
            **CTX_A.event_fields(), block_id="text:1", delta="A", sequence=2
        ),
        make_text_block_start_event(**CTX_B.event_fields(), block_id="text:1", sequence=3),
        make_text_block_segment_event(
            **CTX_B.event_fields(), block_id="text:1", delta="B", sequence=4
        ),
        make_text_block_end_event(**CTX_A.event_fields(), block_id="text:1", sequence=5),
        make_text_block_end_event(**CTX_B.event_fields(), block_id="text:1", sequence=6),
        make_tool_call_start_event(
            **CTX_A.event_fields(),
            tool_call_id="call:1",
            tool_call_name="lookup",
            sequence=7,
        ),
        make_tool_call_arguments_segment_event(
            **CTX_A.event_fields(),
            tool_call_id="call:1",
            delta='{"reply":"a"}',
            sequence=8,
        ),
        make_tool_call_start_event(
            **CTX_B.event_fields(),
            tool_call_id="call:1",
            tool_call_name="lookup",
            sequence=9,
        ),
        make_tool_call_arguments_segment_event(
            **CTX_B.event_fields(),
            tool_call_id="call:1",
            delta='{"reply":"b"}',
            sequence=10,
        ),
        make_tool_call_end_event(**CTX_A.event_fields(), tool_call_id="call:1", sequence=11),
        make_tool_call_end_event(**CTX_B.event_fields(), tool_call_id="call:1", sequence=12),
    ]:
        completions.extend(assembler.append(event).completed)

    assert [
        (completion.reply_id, completion.block_type) for completion in completions
    ] == [
        ("reply:a", "text"),
        ("reply:b", "text"),
        ("reply:a", "tool_call"),
        ("reply:b", "tool_call"),
    ]
    assert isinstance(completions[0].block, TextBlock)
    assert completions[0].block.text == "A"
    assert isinstance(completions[1].block, TextBlock)
    assert completions[1].block.text == "B"
    assert completions[2].block.input == '{"reply":"a"}'
    assert completions[3].block.input == '{"reply":"b"}'


def test_block_assembler_can_discard_unfinished_blocks_for_reply() -> None:
    assembler = BlockAssembler()
    assembler.append(
        make_text_block_start_event(**CTX_A.event_fields(), block_id="text:1", sequence=1)
    )
    assembler.append(
        make_text_block_segment_event(
            **CTX_A.event_fields(), block_id="text:1", delta="A", sequence=2
        )
    )
    assembler.append(
        make_text_block_start_event(**CTX_B.event_fields(), block_id="text:1", sequence=3)
    )
    assembler.append(
        make_text_block_segment_event(
            **CTX_B.event_fields(), block_id="text:1", delta="B", sequence=4
        )
    )

    assert assembler.active_count() == 2
    assert assembler.discard_reply("reply:a") == 1
    assert assembler.active_count("reply:a") == 0
    assert assembler.active_count("reply:b") == 1

    assert (
        assembler.append(
            make_text_block_end_event(**CTX_A.event_fields(), block_id="text:1", sequence=5)
        ).completed
        == []
    )
    completions = assembler.append(
        make_text_block_end_event(**CTX_B.event_fields(), block_id="text:1", sequence=6)
    ).completed

    assert len(completions) == 1
    assert isinstance(completions[0].block, TextBlock)
    assert completions[0].block.text == "B"


def test_block_assembler_completes_data_and_hint_blocks() -> None:
    assembler = BlockAssembler()
    completions = []
    started = []
    hint = HintBlockEvent(
        **CTX.event_fields(), block_id="hint:1", hint="remember this", sequence=2
    )

    for event in [
        make_text_block_segment_event(
            **CTX.event_fields(), block_id="text:missing", delta="orphan", sequence=1
        ),
        hint,
        make_data_block_start_event(
            **CTX.event_fields(), block_id="data:1", media_type="image/png", sequence=3
        ),
        make_data_block_segment_event(
            **CTX.event_fields(),
            block_id="data:1",
            data="abc",
            media_type="image/png",
            sequence=4,
        ),
        make_data_block_segment_event(
            **CTX.event_fields(),
            block_id="data:1",
            data="def",
            media_type="image/png",
            sequence=5,
        ),
        make_data_block_end_event(**CTX.event_fields(), block_id="data:1", sequence=6),
    ]:
        update = assembler.append(event)
        started.extend(update.started)
        completions.extend(update.completed)

    assert [block.type for block in started] == ["data"]
    assert len(completions) == 2
    assert completions[0].block.type == "hint"
    assert completions[0].start_sequence == 2
    assert completions[0].end_sequence == 2
    assert completions[0].start_event_id == hint.id
    assert completions[0].end_event_id == hint.id
    assert completions[1].block.type == "data"
    assert isinstance(completions[1].block.source, Base64Source)
    assert completions[1].block.source.data == "abcdef"


def test_completed_tool_result_from_events_folds_text_and_data_blocks() -> None:
    events = [
        ToolResultStartEvent(
            **CTX.event_fields(),
            tool_call_id="call:tool",
            tool_call_name="lookup",
            sequence=1,
        ),
        ToolResultTextDeltaEvent(
            **CTX.event_fields(), tool_call_id="call:tool", delta="hello ", sequence=2
        ),
        ToolResultTextDeltaEvent(
            **CTX.event_fields(), tool_call_id="call:tool", delta="world", sequence=3
        ),
        ToolResultDataDeltaEvent(
            **CTX.event_fields(),
            tool_call_id="call:tool",
            media_type="text/plain",
            data="Zm9v",
            sequence=4,
        ),
        ToolResultDataDeltaEvent(
            **CTX.event_fields(),
            tool_call_id="call:tool",
            media_type="text/uri-list",
            url="https://example.com",
            sequence=5,
        ),
        ToolResultEndEvent(
            **CTX.event_fields(),
            **tool_result_end_contract_fields("call:tool", tool_name="lookup"),
            tool_call_id="call:tool",
            state=ToolResultState.SUCCESS,
            sequence=6,
            metadata={
                "tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}
            },
        ),
    ]

    block = completed_tool_result_from_events(events, "call:tool")

    assert isinstance(block, ToolResultBlock)
    assert block.name == "lookup"
    assert block.state is ToolResultState.SUCCESS
    assert isinstance(block.output[0], TextBlock)
    assert block.output[0].text == "hello world"
    assert isinstance(block.output[1], DataBlock)
    assert isinstance(block.output[1].source, Base64Source)
    assert block.output[1].source.data == "Zm9v"
    assert isinstance(block.output[2].source, URLSource)
    assert str(block.output[2].source.url) == "https://example.com"


def test_completed_tool_result_from_events_is_strict_for_malformed_slice() -> None:
    try:
        completed_tool_result_from_events([], "call:missing")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for missing slice")

    for events in [
        [
            ToolResultTextDeltaEvent(
                **CTX.event_fields(),
                tool_call_id="call:bad",
                delta="orphan",
                sequence=1,
            )
        ],
        [
            ToolResultEndEvent(
                **CTX.event_fields(),
                **tool_result_end_contract_fields(
                    "call:bad", tool_name="lookup", state=ToolResultState.ERROR
                ),
                tool_call_id="call:bad",
                state=ToolResultState.ERROR,
                sequence=1,
                metadata={
                    "tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}
                },
            )
        ],
        [
            ToolResultStartEvent(
                **CTX.event_fields(),
                tool_call_id="call:bad",
                tool_call_name="lookup",
                sequence=1,
            )
        ],
    ]:
        try:
            completed_tool_result_from_events(events, "call:bad")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for malformed tool result slice")


def test_block_assembler_external_execution_result_completes_tool_result_blocks() -> (
    None
):
    assembler = BlockAssembler()
    result = ToolResultBlock(
        id="call:ext",
        name="external_lookup",
        output=[TextBlock(text="done")],
        state=ToolResultState.SUCCESS,
    )

    ingress = external_tool_result_ingress_fact(result)
    external_result = ExternalExecutionResultEvent(
        **CTX.event_fields(),
        external_results=(ingress,),
        terminal_projections=external_terminal_projection_references((ingress,)),
        sequence=10,
    )
    update = assembler.append(external_result)

    completions = update.completed
    assert update.started == []
    assert len(completions) == 1
    assert isinstance(completions[0].block, ToolResultBlock)
    assert completions[0].block.id == "call:ext"
    assert completions[0].start_sequence == 10
    assert completions[0].end_sequence == 10
    assert completions[0].start_event_id == external_result.id
    assert completions[0].end_event_id == external_result.id
