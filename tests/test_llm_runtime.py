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
from pulsara_agent.llm.adapters.openai.responses import (
    OpenAIResponsesTransport,
    build_responses_payload,
    response_to_agent_events,
    translate_responses_event,
)
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.factory import build_llm_runtime
from pulsara_agent.llm.input import LLMMessage, ToolSpec
from pulsara_agent.llm.models import ModelRole
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


def test_openai_responses_events_translate_to_agent_events() -> None:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    transport = OpenAIResponsesTransport(api_key="sk-test")
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


def test_openai_responses_transport_posts_to_configured_base_url(monkeypatch) -> None:
    import asyncio
    from pulsara_agent.llm.adapters.openai import responses

    captured = {}

    def fake_post_responses(*, base_url, api_key, payload, timeout_seconds):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        captured["payload"] = payload
        captured["timeout_seconds"] = timeout_seconds
        return {"status": "completed", "output_text": "pong"}

    monkeypatch.setattr(responses, "_post_responses", fake_post_responses)
    transport = OpenAIResponsesTransport(api_key="sk-test", timeout_seconds=7)
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

    assert captured["base_url"] == "https://example.test/v1"
    assert captured["api_key"] == "sk-test"
    assert captured["payload"]["model"] == "flash"
    assert captured["timeout_seconds"] == 7
    assert isinstance(events[1], TextBlockStartEvent)
    assert events[2].delta == "pong"
    assert isinstance(events[-1], ModelCallEndEvent)


def test_openai_responses_transport_emits_run_error_event(monkeypatch) -> None:
    import asyncio
    from pulsara_agent.llm.adapters.openai import responses

    def fake_post_responses(*, base_url, api_key, payload, timeout_seconds):
        raise responses.OpenAIResponsesError("boom", {"status": 500})

    monkeypatch.setattr(responses, "_post_responses", fake_post_responses)
    transport = OpenAIResponsesTransport(api_key="sk-test")
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
    assert events[1].metadata == {"provider_data": {"status": 500}}


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
    from pulsara_agent.llm.adapters.openai.responses import _AgentEventBuilder

    config = config or LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
    )
    return _AgentEventBuilder(
        model=config.model_for(ModelRole.PRO),
        event_context=EVENT_CONTEXT,
    )
