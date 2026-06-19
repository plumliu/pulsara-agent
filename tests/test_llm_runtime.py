from pulsara_agent.event import (
    EventContext,
    EventType,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RunErrorEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.llm.adapters.mock import MockTransport
from pulsara_agent.llm.adapters.openai.chat_completions import (
    ChatToolCallAccumulator,
    OpenAIChatCompletionsTransport,
    build_chat_completions_payload,
    translate_chat_completion_chunk,
)
from pulsara_agent.llm.adapters.openai.client import OPENAI_CHAT_COMPLETIONS_API
from pulsara_agent.llm.adapters.openai.events import AgentEventBuilder
from pulsara_agent.llm.adapters.openai.responses import (
    OpenAIResponsesTransport,
    build_responses_payload,
    response_to_agent_events,
    translate_responses_event,
)
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.factory import build_llm_runtime
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, ToolSpec
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.provider import ProviderProfile, ThinkingProfile, ThinkingReplayPolicy
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.runtime import LLMRuntime


EVENT_CONTEXT = EventContext(run_id="run:test", turn_id="turn:test", reply_id="reply:test")


async def collect_events(runtime: LLMRuntime, role: ModelRole, context: LLMContext):
    return [
        event
        async for event in runtime.stream(
            role=role,
            context=context,
            event_context=EVENT_CONTEXT,
        )
    ]


def test_config_resolves_pro_and_flash_models() -> None:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="provider/pro-model",
        flash_model="provider/flash-model",
    )

    pro = config.model_for(ModelRole.PRO)
    flash = config.model_for(ModelRole.FLASH)

    assert pro.id == "provider/pro-model"
    assert pro.role is ModelRole.PRO
    assert flash.id == "provider/flash-model"
    assert flash.role is ModelRole.FLASH
    assert pro.api == "openai_responses"


def test_runtime_streams_agent_events_through_registered_transport() -> None:
    import asyncio

    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="mock",
    )
    registry = LLMTransportRegistry()
    registry.register(MockTransport(text="hello"))
    runtime = LLMRuntime(config=config, registry=registry)

    events = asyncio.run(
        collect_events(
            runtime,
            ModelRole.FLASH,
            LLMContext(messages=(LLMMessage.user("Say hi"),)),
        )
    )

    assert isinstance(events[0], ReplyStartEvent)
    assert isinstance(events[1], ModelCallStartEvent)
    assert events[1].model_name == "flash"
    assert isinstance(events[2], TextBlockStartEvent)
    assert isinstance(events[3], TextBlockDeltaEvent)
    assert events[3].block_id == events[2].block_id
    assert events[3].delta == "hello"
    assert isinstance(events[4], TextBlockEndEvent)
    assert isinstance(events[5], ModelCallEndEvent)
    assert isinstance(events[6], ReplyEndEvent)


def test_openai_responses_payload_uses_internal_context() -> None:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    context = LLMContext(
        system_prompt="You are Pulsara.",
        messages=(LLMMessage.user("Use the tool."),),
        tools=(
            ToolSpec(
                name="lookup",
                description="Look up a value.",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            ),
        ),
    )

    payload = build_responses_payload(
        model=config.model_for(ModelRole.PRO),
        context=context,
        options=LLMOptions(reasoning_effort="medium", reasoning_summary="auto", max_output_tokens=128),
    )

    assert payload["model"] == "pro"
    assert payload["instructions"] == "You are Pulsara."
    assert payload["input"][0]["role"] == "user"
    assert payload["tools"][0]["name"] == "lookup"
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert payload["max_output_tokens"] == 128


def test_openai_responses_payload_uses_function_call_output_items() -> None:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    context = LLMContext(
        messages=(
            LLMMessage.user("Use lookup."),
            LLMMessage.tool_call(
                tool_call_id="call_responses_123",
                name="lookup",
                arguments='{"q":"pulsara"}',
            ),
            LLMMessage.tool_result("found", tool_call_id="call_responses_123"),
        )
    )

    payload = build_responses_payload(model=config.model_for(ModelRole.PRO), context=context)

    assert payload["input"][0]["role"] == "user"
    assert payload["input"][1] == {
        "type": "function_call",
        "call_id": "call_responses_123",
        "name": "lookup",
        "arguments": '{"q":"pulsara"}',
    }
    assert payload["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_responses_123",
        "output": "found",
    }
    assert all(item.get("role") != "tool" for item in payload["input"])


def test_openai_responses_payload_expands_assistant_turn_tool_calls() -> None:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    context = LLMContext(
        messages=(
            LLMMessage.user("Use lookup."),
            LLMMessage.assistant_turn(
                text="I will call lookup.",
                thinking="private reasoning",
                tool_calls=(
                    LLMToolCall(id="call_responses_123", name="lookup", arguments='{"q":"pulsara"}'),
                ),
            ),
            LLMMessage.tool_result("found", tool_call_id="call_responses_123"),
        )
    )

    payload = build_responses_payload(model=config.model_for(ModelRole.PRO), context=context)

    assert payload["input"][1]["role"] == "assistant"
    assert payload["input"][1]["content"] == [
        {"type": "input_text", "text": "I will call lookup."}
    ]
    assert payload["input"][2] == {
        "type": "function_call",
        "call_id": "call_responses_123",
        "name": "lookup",
        "arguments": '{"q":"pulsara"}',
    }
    assert payload["input"][3] == {
        "type": "function_call_output",
        "call_id": "call_responses_123",
        "output": "found",
    }


def test_openai_responses_events_translate_to_agent_events() -> None:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    builder = transport_builder_for_test(config)

    text_events = translate_responses_event(
        {"type": "response.output_text.delta", "delta": "hello"},
        builder=builder,
    )
    start_events = translate_responses_event(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "name": "lookup",
            },
        },
        builder=builder,
    )
    args_events = translate_responses_event(
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": '{"q"',
        },
        builder=builder,
    )
    done_events = translate_responses_event(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "name": "lookup",
                "arguments": '{"q": "json-ld"}',
            },
        },
        builder=builder,
    )

    assert isinstance(text_events[0], TextBlockStartEvent)
    assert isinstance(text_events[1], TextBlockDeltaEvent)
    assert isinstance(start_events[0], ToolCallStartEvent)
    assert len(args_events) == 1
    assert isinstance(args_events[0], ToolCallDeltaEvent)
    assert args_events[0].tool_call_id == "fc_1"
    assert args_events[0].delta == '{"q"'
    assert len(done_events) == 1
    assert isinstance(done_events[0], ToolCallEndEvent)
    assert done_events[0].tool_call_id == "fc_1"


def test_openai_responses_transport_can_stream_mock_raw_events() -> None:
    import asyncio

    transport = OpenAIResponsesTransport(
        api_key="sk-test",
        _mock_events=[
            {"type": "response.output_text.delta", "delta": "hi"},
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "fc_1", "name": "lookup"},
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_1",
                "delta": "{}",
            },
            {
                "type": "response.output_item.done",
                "item": {"type": "function_call", "id": "fc_1", "name": "lookup"},
            },
        ],
    )
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )

    async def collect():
        return [
            event
            async for event in transport.stream(
                model=config.model_for(ModelRole.PRO),
                context=LLMContext(messages=(LLMMessage.user("hi"),)),
                event_context=EVENT_CONTEXT,
            )
        ]

    events = asyncio.run(collect())

    assert isinstance(events[0], ModelCallStartEvent)
    assert any(isinstance(event, TextBlockDeltaEvent) and event.delta == "hi" for event in events)
    assert any(isinstance(event, ToolCallStartEvent) and event.tool_call_name == "lookup" for event in events)
    assert any(isinstance(event, ToolCallDeltaEvent) and event.delta == "{}" for event in events)
    assert isinstance(events[-1], ModelCallEndEvent)


def test_non_streaming_response_synthesizes_same_event_shape() -> None:
    builder = transport_builder_for_test()
    events = response_to_agent_events(
        response={
            "status": "completed",
            "reasoning": {"summary": [{"text": "brief thinking"}]},
            "output_text": "done",
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "name": "lookup",
                    "arguments": '{"q": "pulsara"}',
                }
            ],
            "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        },
        builder=builder,
    )

    assert isinstance(events[0], ThinkingBlockStartEvent)
    assert isinstance(events[1], ThinkingBlockDeltaEvent)
    assert isinstance(events[2], TextBlockStartEvent)
    assert isinstance(events[3], TextBlockDeltaEvent)
    assert isinstance(events[4], ToolCallStartEvent)
    assert isinstance(events[5], ToolCallDeltaEvent)
    assert isinstance(events[6], ToolCallEndEvent)
    assert any(isinstance(event, TextBlockEndEvent) for event in events)
    assert any(isinstance(event, ThinkingBlockEndEvent) for event in events)
    assert isinstance(events[-1], ModelCallEndEvent)
    assert events[-1].input_tokens == 3
    assert events[-1].output_tokens == 5
    assert events[-1].total_tokens == 8


def test_openai_responses_tool_calls_prefer_call_id_over_item_id() -> None:
    builder = transport_builder_for_test()

    events = response_to_agent_events(
        response={
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_item_1",
                    "call_id": "call_responses_1",
                    "name": "lookup",
                    "arguments": '{"q":"pulsara"}',
                }
            ],
        },
        builder=builder,
    )

    start = next(event for event in events if isinstance(event, ToolCallStartEvent))
    delta = next(event for event in events if isinstance(event, ToolCallDeltaEvent))
    end = next(event for event in events if isinstance(event, ToolCallEndEvent))
    assert start.tool_call_id == "call_responses_1"
    assert delta.tool_call_id == "call_responses_1"
    assert end.tool_call_id == "call_responses_1"


def test_openai_responses_streaming_arguments_map_item_id_to_call_id() -> None:
    builder = transport_builder_for_test()

    events = []
    events.extend(
        translate_responses_event(
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "fc_item_1",
                    "call_id": "call_responses_1",
                    "name": "lookup",
                },
            },
            builder=builder,
        )
    )
    events.extend(
        translate_responses_event(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_item_1",
                "delta": '{"q":"pulsara"}',
            },
            builder=builder,
        )
    )
    events.extend(
        translate_responses_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_item_1",
                    "call_id": "call_responses_1",
                    "name": "lookup",
                },
            },
            builder=builder,
        )
    )

    assert isinstance(events[0], ToolCallStartEvent)
    assert isinstance(events[1], ToolCallDeltaEvent)
    assert isinstance(events[2], ToolCallEndEvent)
    assert events[0].tool_call_id == "call_responses_1"
    assert events[1].tool_call_id == "call_responses_1"
    assert events[2].tool_call_id == "call_responses_1"


def test_openai_responses_error_event_emits_run_error_without_model_end() -> None:
    builder = transport_builder_for_test()

    events = translate_responses_event(
        {"type": "error", "message": "provider exploded", "code": "bad_request"},
        builder=builder,
    )

    assert len(events) == 1
    assert isinstance(events[0], RunErrorEvent)
    assert events[0].message == "provider exploded"
    assert events[0].code == "openai_responses_error"


def test_openai_responses_transport_uses_sdk_stream() -> None:
    import asyncio

    fake_client = FakeOpenAIClient(
        responses_events=[
            {"type": "response.output_text.delta", "delta": "pong"},
            {
                "type": "response.completed",
                "response": {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}},
            },
        ]
    )
    transport = OpenAIResponsesTransport(api_key="sk-test", timeout_seconds=7, _client=fake_client)
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )

    async def collect():
        return [
            event
            async for event in transport.stream(
                model=config.model_for(ModelRole.FLASH),
                context=LLMContext(messages=(LLMMessage.user("ping"),)),
                event_context=EVENT_CONTEXT,
            )
        ]

    events = asyncio.run(collect())

    assert fake_client.responses.calls[0]["model"] == "flash"
    assert fake_client.responses.calls[0]["stream"] is True
    assert isinstance(events[1], TextBlockStartEvent)
    assert events[2].delta == "pong"
    assert isinstance(events[-1], ModelCallEndEvent)
    assert events[-1].input_tokens == 1
    assert events[-1].output_tokens == 2
    assert events[-1].total_tokens == 3


def test_openai_responses_transport_emits_run_error_event() -> None:
    import asyncio

    fake_client = FakeOpenAIClient(responses_error=RuntimeError("boom"))
    transport = OpenAIResponsesTransport(api_key="sk-test", _client=fake_client)
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )

    async def collect():
        return [
            event
            async for event in transport.stream(
                model=config.model_for(ModelRole.PRO),
                context=LLMContext(messages=(LLMMessage.user("ping"),)),
                event_context=EVENT_CONTEXT,
            )
        ]

    events = asyncio.run(collect())

    assert isinstance(events[0], ModelCallStartEvent)
    assert isinstance(events[1], RunErrorEvent)
    assert events[1].type is EventType.RUN_ERROR
    assert events[1].message == "boom"
    assert events[1].code == "openai_responses_error"
    assert events[1].metadata["provider_data"]["type"] == "RuntimeError"


def test_openai_chat_completions_payload_uses_internal_context() -> None:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )
    context = LLMContext(
        system_prompt="You are Pulsara.",
        messages=(
            LLMMessage.user("Use lookup."),
            LLMMessage.tool_call(
                tool_call_id="call_chat_123",
                name="lookup",
                arguments='{"q":"pulsara"}',
            ),
            LLMMessage.tool_result("found", tool_call_id="call_chat_123"),
        ),
        tools=(
            ToolSpec(
                name="lookup",
                description="Look up a value.",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            ),
        ),
    )

    payload = build_chat_completions_payload(
        model=config.model_for(ModelRole.PRO),
        context=context,
        options=LLMOptions(reasoning_effort="medium", max_output_tokens=64),
    )

    assert payload["model"] == "pro"
    assert payload["messages"][0] == {"role": "system", "content": "You are Pulsara."}
    assert payload["messages"][1] == {"role": "user", "content": "Use lookup."}
    assert payload["messages"][2]["role"] == "assistant"
    assert payload["messages"][2]["tool_calls"][0]["id"] == "call_chat_123"
    assert payload["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_chat_123",
        "content": "found",
    }
    assert payload["tools"][0]["function"]["name"] == "lookup"
    assert payload["max_completion_tokens"] == 64
    assert payload["reasoning_effort"] == "medium"
    assert payload["stream_options"] == {"include_usage": True}


def test_openai_chat_completions_payload_groups_adjacent_tool_calls() -> None:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )
    context = LLMContext(
        messages=(
            LLMMessage.user("Use both tools."),
            LLMMessage.tool_call(
                tool_call_id="call_1",
                name="first_tool",
                arguments='{"a":1}',
            ),
            LLMMessage.tool_call(
                tool_call_id="call_2",
                name="second_tool",
                arguments='{"b":2}',
            ),
            LLMMessage.tool_result("first result", tool_call_id="call_1"),
            LLMMessage.tool_result("second result", tool_call_id="call_2"),
        )
    )

    payload = build_chat_completions_payload(model=config.model_for(ModelRole.PRO), context=context)

    assert payload["messages"][0] == {"role": "user", "content": "Use both tools."}
    assert payload["messages"][1]["role"] == "assistant"
    assert [call["id"] for call in payload["messages"][1]["tool_calls"]] == ["call_1", "call_2"]
    assert payload["messages"][2]["role"] == "tool"
    assert payload["messages"][2]["tool_call_id"] == "call_1"
    assert payload["messages"][3]["role"] == "tool"
    assert payload["messages"][3]["tool_call_id"] == "call_2"


def test_openai_chat_completions_payload_replays_assistant_thinking_with_tool_calls() -> None:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
        provider_profile=ProviderProfile(
            wire_api=OPENAI_CHAT_COMPLETIONS_API,
            request_extra_body={"thinking": {"type": "enabled"}},
            omit_params_when_thinking=("temperature",),
            thinking=ThinkingProfile(
                enabled=True,
                replay_policy=ThinkingReplayPolicy.WHEN_TOOL_CALLS,
            ),
        ),
    )
    context = LLMContext(
        messages=(
            LLMMessage.user("Use lookup."),
            LLMMessage.assistant_turn(
                text="I will look that up.",
                thinking="Need a tool result before answering.",
                tool_calls=(LLMToolCall(id="call_1", name="lookup", arguments='{"q":"pulsara"}'),),
            ),
            LLMMessage.tool_result("found", tool_call_id="call_1"),
        )
    )

    payload = build_chat_completions_payload(
        model=config.model_for(ModelRole.PRO),
        context=context,
        options=LLMOptions(temperature=0.2, reasoning_effort="medium"),
    )

    assistant = payload["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "I will look that up."
    assert assistant["reasoning_content"] == "Need a tool result before answering."
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert payload["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "temperature" not in payload
    assert payload["reasoning_effort"] == "medium"


def test_openai_chat_completions_payload_does_not_replay_thinking_by_default() -> None:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )
    context = LLMContext(
        messages=(
            LLMMessage.assistant_turn(
                text="I will look that up.",
                thinking="Provider-private reasoning.",
                tool_calls=(LLMToolCall(id="call_1", name="lookup", arguments="{}"),),
            ),
        )
    )

    payload = build_chat_completions_payload(model=config.model_for(ModelRole.PRO), context=context)

    assert "reasoning_content" not in payload["messages"][0]


def test_openai_chat_completions_transport_can_stream_mock_chunks() -> None:
    import asyncio

    transport = OpenAIChatCompletionsTransport(
        api_key="sk-test",
        _mock_chunks=[
            {"choices": [{"delta": {"content": "hi"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_chat_1",
                                    "function": {"name": "lookup", "arguments": '{"q"'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ':"pulsara"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6},
            },
        ],
    )
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )

    async def collect():
        return [
            event
            async for event in transport.stream(
                model=config.model_for(ModelRole.PRO),
                context=LLMContext(messages=(LLMMessage.user("hi"),)),
                event_context=EVENT_CONTEXT,
            )
        ]

    events = asyncio.run(collect())

    assert isinstance(events[0], ModelCallStartEvent)
    assert any(isinstance(event, TextBlockDeltaEvent) and event.delta == "hi" for event in events)
    assert any(
        isinstance(event, ToolCallStartEvent) and event.tool_call_id == "call_chat_1"
        for event in events
    )
    assert [event.delta for event in events if isinstance(event, ToolCallDeltaEvent)] == [
        '{"q"',
        ':"pulsara"}',
    ]
    assert any(
        isinstance(event, ToolCallEndEvent) and event.tool_call_id == "call_chat_1"
        for event in events
    )
    assert isinstance(events[-1], ModelCallEndEvent)
    assert events[-1].input_tokens == 2
    assert events[-1].output_tokens == 4
    assert events[-1].total_tokens == 6


def test_openai_chat_completions_translates_reasoning_content_delta() -> None:
    builder = transport_builder_for_test()
    accumulator = ChatToolCallAccumulator(builder=builder)

    events = translate_chat_completion_chunk(
        {"choices": [{"delta": {"reasoning_content": "think", "content": "answer"}}]},
        builder=builder,
        accumulator=accumulator,
    )

    assert isinstance(events[0], ThinkingBlockStartEvent)
    assert isinstance(events[1], ThinkingBlockDeltaEvent)
    assert events[1].delta == "think"
    assert isinstance(events[2], TextBlockStartEvent)
    assert isinstance(events[3], TextBlockDeltaEvent)
    assert events[3].delta == "answer"


def test_openai_chat_completions_transport_uses_sdk_stream() -> None:
    import asyncio

    fake_client = FakeOpenAIClient(
        chat_chunks=[
            {"choices": [{"delta": {"content": "pong"}}]},
            {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
        ]
    )
    transport = OpenAIChatCompletionsTransport(api_key="sk-test", _client=fake_client)
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=OPENAI_CHAT_COMPLETIONS_API,
    )

    async def collect():
        return [
            event
            async for event in transport.stream(
                model=config.model_for(ModelRole.FLASH),
                context=LLMContext(messages=(LLMMessage.user("ping"),)),
                event_context=EVENT_CONTEXT,
            )
        ]

    events = asyncio.run(collect())

    assert fake_client.chat.completions.calls[0]["model"] == "flash"
    assert fake_client.chat.completions.calls[0]["stream"] is True
    assert isinstance(events[1], TextBlockStartEvent)
    assert events[2].delta == "pong"
    assert isinstance(events[-1], ModelCallEndEvent)
    assert events[-1].input_tokens == 1
    assert events[-1].output_tokens == 2
    assert events[-1].total_tokens == 3


def test_openai_chat_completions_caches_arguments_until_tool_call_id_arrives() -> None:
    builder = transport_builder_for_test()
    accumulator = ChatToolCallAccumulator(builder=builder)

    first_events = translate_chat_completion_chunk(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"q"'}}
                        ]
                    }
                }
            ]
        },
        builder=builder,
        accumulator=accumulator,
    )
    second_events = translate_chat_completion_chunk(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_late",
                                "function": {"name": "lookup"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        builder=builder,
        accumulator=accumulator,
    )

    assert first_events == []
    assert isinstance(second_events[0], ToolCallStartEvent)
    assert second_events[0].tool_call_id == "call_late"
    assert isinstance(second_events[1], ToolCallDeltaEvent)
    assert second_events[1].tool_call_id == "call_late"
    assert second_events[1].delta == '{"q"'
    assert isinstance(second_events[2], ToolCallEndEvent)


def test_default_llm_runtime_registers_openai_responses_transport() -> None:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )

    runtime = build_llm_runtime(config)

    assert runtime is not None


def transport_builder_for_test(config: LLMConfig | None = None):
    config = config or LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    return AgentEventBuilder(
        model=config.model_for(ModelRole.PRO),
        event_context=EVENT_CONTEXT,
    )


class FakeAsyncStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class FakeResponsesEndpoint:
    def __init__(self, *, events=None, error=None):
        self.events = events or []
        self.error = error
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return FakeAsyncStream(self.events)


class FakeOpenAIClient:
    def __init__(self, *, responses_events=None, responses_error=None, chat_chunks=None, chat_error=None):
        self.responses = FakeResponsesEndpoint(events=responses_events, error=responses_error)
        self.chat = FakeChatNamespace(chunks=chat_chunks, error=chat_error)


class FakeChatCompletionsEndpoint:
    def __init__(self, *, chunks=None, error=None):
        self.chunks = chunks or []
        self.error = error
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return FakeAsyncStream(self.chunks)


class FakeChatNamespace:
    def __init__(self, *, chunks=None, error=None):
        self.completions = FakeChatCompletionsEndpoint(chunks=chunks, error=error)
