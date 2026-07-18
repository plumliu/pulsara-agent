import asyncio

from tests.support.model_stream import (
    make_text_block_end_event,
    make_text_block_segment_event,
    make_text_block_start_event,
)

from pulsara_agent.event import (
    EventContext,
    EventType,
    ReplyStartEvent,
    ReplyEndEvent,
    RunErrorEvent,
    TextBlockSegmentEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.message import TextBlock, ToolResultBlock, ToolResultState
from pulsara_agent.runtime import (
    ControlHookResult,
    HookContext,
    HookDecision,
    RuntimeHookManager,
)
from tests.conftest import tool_result_end_contract_fields


CTX = EventContext(run_id="run:hooks", turn_id="turn:hooks", reply_id="reply:hooks")


def _ctx(reply_id: str = "reply:hooks") -> HookContext:
    return HookContext(
        runtime_session_id="runtime:hooks",
        run_id="run:hooks",
        turn_id="turn:hooks",
        reply_id=reply_id,
    )


def test_event_hooks_support_all_and_specific_subscriptions() -> None:
    manager = RuntimeHookManager()
    calls: list[tuple[str, EventType]] = []

    def all_events(context, event) -> None:
        calls.append(("all", event.type))

    def text_delta(context, event) -> None:
        calls.append(("delta", event.type))

    manager.register_event(None, all_events)
    manager.register_event(EventType.TEXT_BLOCK_SEGMENT, text_delta)

    asyncio.run(
        manager.dispatch_observer_event(
            _ctx(),
            make_text_block_segment_event(**CTX.event_fields(), block_id="text:1", delta="hi"),
        )
    )
    asyncio.run(
        manager.dispatch_observer_event(
            _ctx(), ReplyStartEvent(**CTX.event_fields(), name="assistant")
        )
    )

    assert calls == [
        ("all", EventType.TEXT_BLOCK_SEGMENT),
        ("delta", EventType.TEXT_BLOCK_SEGMENT),
        ("all", EventType.REPLY_START),
    ]


def test_hooks_support_sync_async_order_and_nonfatal_errors() -> None:
    manager = RuntimeHookManager()
    calls: list[str] = []

    def first(context, event) -> None:
        calls.append("first")

    async def second(context, event) -> None:
        calls.append("second")

    def failing(context, event) -> None:
        calls.append("failing")
        raise RuntimeError("hook boom")

    def after_failure(context, event) -> ControlHookResult:
        calls.append("after")
        return ControlHookResult(HookDecision.DENY, reason="ignored")

    manager.register_event(None, first)
    manager.register_event(None, second)
    manager.register_event(None, failing)
    manager.register_event(None, after_failure)

    asyncio.run(
        manager.dispatch_observer_event(
            _ctx(),
            make_text_block_segment_event(
                **CTX.event_fields(), block_id="text:orphan", delta="hi"
            ),
        )
    )

    assert calls == ["first", "second", "failing", "after"]
    assert len(manager.errors) == 1
    assert manager.errors[0].hook_kind == "event"
    assert manager.errors[0].error_type == "RuntimeError"
    assert manager.errors[0].message == "hook boom"


def test_event_hooks_receive_isolated_event_copies() -> None:
    manager = RuntimeHookManager()
    observed_deltas: list[str] = []
    completed_text: list[str] = []

    def mutating_hook(context, event) -> None:
        if isinstance(event, TextBlockSegmentEvent):
            event.text = "MUTATED"

    def observing_hook(context, event) -> None:
        if isinstance(event, TextBlockSegmentEvent):
            observed_deltas.append(event.text)

    manager.register_event(None, mutating_hook)
    manager.register_event(None, observing_hook)
    manager.register_block(
        "text", lambda context, completion: completed_text.append(completion.block.text)
    )

    delta_event = make_text_block_segment_event(
        **CTX.event_fields(), block_id="text:1", delta="original"
    )
    for event in [
        make_text_block_start_event(**CTX.event_fields(), block_id="text:1"),
        delta_event,
        make_text_block_end_event(**CTX.event_fields(), block_id="text:1"),
    ]:
        asyncio.run(manager.dispatch_observer_event(_ctx(), event))

    assert delta_event.text == "original"
    assert observed_deltas == ["original"]
    assert completed_text == ["original"]


def test_block_hooks_fire_on_completed_text_and_tool_result_blocks() -> None:
    manager = RuntimeHookManager()
    completions: list[tuple[str, object]] = []

    def all_blocks(context, completion) -> None:
        completions.append(("all", completion.block))

    def text_blocks(context, completion) -> None:
        completions.append(("text", completion.block))

    manager.register_block(None, all_blocks)
    manager.register_block("text", text_blocks)

    for event in [
        make_text_block_start_event(**CTX.event_fields(), block_id="text:1"),
        make_text_block_segment_event(**CTX.event_fields(), block_id="text:1", delta="hello"),
        make_text_block_end_event(**CTX.event_fields(), block_id="text:1"),
        ToolResultStartEvent(
            **CTX.event_fields(), tool_call_id="call:1", tool_call_name="lookup"
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
    ]:
        asyncio.run(manager.dispatch_observer_event(_ctx(), event))

    assert len(completions) == 3
    assert completions[0][0] == "all"
    assert isinstance(completions[0][1], TextBlock)
    assert completions[0][1].text == "hello"
    assert completions[1][0] == "text"
    assert isinstance(completions[1][1], TextBlock)
    assert completions[2][0] == "all"
    assert isinstance(completions[2][1], ToolResultBlock)
    assert completions[2][1].output[0].text == "found"


def test_orphan_events_do_not_trigger_block_hooks() -> None:
    manager = RuntimeHookManager()
    completions = []
    manager.register_block(
        None, lambda context, completion: completions.append(completion)
    )

    asyncio.run(
        manager.dispatch_observer_event(
            _ctx(),
            make_text_block_segment_event(
                **CTX.event_fields(), block_id="text:missing", delta="orphan"
            ),
        )
    )
    asyncio.run(
        manager.dispatch_observer_event(
            _ctx(),
            make_text_block_end_event(**CTX.event_fields(), block_id="text:missing"),
        )
    )

    assert completions == []


def test_block_hooks_isolate_reused_block_ids_across_replies() -> None:
    manager = RuntimeHookManager()
    completions: list[tuple[str, str]] = []

    def record(context, completion) -> None:
        assert isinstance(completion.block, TextBlock)
        completions.append((completion.reply_id, completion.block.text))

    manager.register_block("text", record)
    ctx_a = EventContext(run_id="run:hooks", turn_id="turn:hooks", reply_id="reply:a")
    ctx_b = EventContext(run_id="run:hooks", turn_id="turn:hooks", reply_id="reply:b")

    for context, event in [
        (
            _ctx("reply:a"),
            make_text_block_start_event(**ctx_a.event_fields(), block_id="text:1"),
        ),
        (
            _ctx("reply:a"),
            make_text_block_segment_event(**ctx_a.event_fields(), block_id="text:1", delta="A"),
        ),
        (
            _ctx("reply:b"),
            make_text_block_start_event(**ctx_b.event_fields(), block_id="text:1"),
        ),
        (
            _ctx("reply:b"),
            make_text_block_segment_event(**ctx_b.event_fields(), block_id="text:1", delta="B"),
        ),
        (_ctx("reply:a"), make_text_block_end_event(**ctx_a.event_fields(), block_id="text:1")),
        (_ctx("reply:b"), make_text_block_end_event(**ctx_b.event_fields(), block_id="text:1")),
    ]:
        asyncio.run(manager.dispatch_observer_event(context, event))

    assert completions == [("reply:a", "A"), ("reply:b", "B")]


def test_runtime_hook_manager_cleans_unfinished_blocks_on_run_error() -> None:
    manager = RuntimeHookManager()
    completed_text: list[str] = []
    manager.register_block(
        "text", lambda context, completion: completed_text.append(completion.block.text)
    )

    for event in [
        make_text_block_start_event(**CTX.event_fields(), block_id="text:1"),
        make_text_block_segment_event(**CTX.event_fields(), block_id="text:1", delta="partial"),
        RunErrorEvent(**CTX.event_fields(), message="stream failed"),
        make_text_block_end_event(**CTX.event_fields(), block_id="text:1"),
    ]:
        asyncio.run(manager.dispatch_observer_event(_ctx(), event))

    assert completed_text == []


def test_runtime_hook_manager_cleans_only_finished_reply_on_reply_end() -> None:
    manager = RuntimeHookManager()
    completions: list[tuple[str, str]] = []

    def record(context, completion) -> None:
        assert isinstance(completion.block, TextBlock)
        completions.append((completion.reply_id, completion.block.text))

    manager.register_block("text", record)
    ctx_a = EventContext(run_id="run:hooks", turn_id="turn:hooks", reply_id="reply:a")
    ctx_b = EventContext(run_id="run:hooks", turn_id="turn:hooks", reply_id="reply:b")

    for context, event in [
        (
            _ctx("reply:a"),
            make_text_block_start_event(**ctx_a.event_fields(), block_id="text:1"),
        ),
        (
            _ctx("reply:a"),
            make_text_block_segment_event(**ctx_a.event_fields(), block_id="text:1", delta="A"),
        ),
        (
            _ctx("reply:b"),
            make_text_block_start_event(**ctx_b.event_fields(), block_id="text:1"),
        ),
        (
            _ctx("reply:b"),
            make_text_block_segment_event(**ctx_b.event_fields(), block_id="text:1", delta="B"),
        ),
        (_ctx("reply:a"), ReplyEndEvent(**ctx_a.event_fields(), model_terminal_outcome="completed")),
        (_ctx("reply:a"), make_text_block_end_event(**ctx_a.event_fields(), block_id="text:1")),
        (_ctx("reply:b"), make_text_block_end_event(**ctx_b.event_fields(), block_id="text:1")),
    ]:
        asyncio.run(manager.dispatch_observer_event(context, event))

    assert completions == [("reply:b", "B")]
