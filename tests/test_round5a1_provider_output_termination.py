"""Round 5A.1 provider-neutral output terminal and replay contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog
from pulsara_agent.conversation_kernel.auxiliary_model import (
    DirectKernelAuxiliaryJsonModel,
)
from pulsara_agent.conversation_kernel.assistant_settlement import (
    assistant_settlement_candidate_fingerprint,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.direct_model import (
    CompletedProviderModelExecution,
)
from pulsara_agent.llm.adapters.openai.chat_completions import (
    ChatCompletionAccumulator,
    OpenAIChatCompletionsTransport,
    chat_tool_wire_items,
)
from pulsara_agent.llm.adapters.openai.events import ProviderLiveItemBuilder
from pulsara_agent.llm.adapters.openai.events import sdk_event_to_dict
from pulsara_agent.llm.adapters.openai.responses import (
    OpenAIResponsesTransport,
    ResponsesCompletionAccumulator,
    responses_tool_wire_items,
)
from pulsara_agent.llm.adapters.openai.function_tools import (
    OpenAIFunctionSchemaIncompatible,
    lower_openai_function_parameters,
)
from pulsara_agent.llm.adapters.openai.client import (
    OPENAI_CHAT_COMPLETIONS_API,
    OPENAI_RESPONSES_API,
    OpenAITransportTimeoutPolicy,
)
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.llm.normalized_transport import (
    NormalizedLLMTransport,
    NormalizedLLMTransportRegistry,
    NormalizedProviderTransportExecution,
)
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, ToolSpec
from pulsara_agent.llm.provider import (
    ProviderAssistantReplayCodecKind,
    ProviderChatFieldAccumulationMode,
    ProviderChatReplayFieldContract,
    ProviderProfile,
    ThinkingProfile,
    ThinkingReplayPolicy,
)
from pulsara_agent.llm.provider_replay import (
    ProviderReplayDisposition,
    build_provider_replay_target_compatibility,
)
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextStartPayload,
    ThinkingDeltaPayload,
    ThinkingEndPayload,
    ThinkingStartPayload,
)
from pulsara_agent.ports.provider_stream import (
    ProviderAdapterTerminal,
    ProviderAdapterTerminalKind,
    ProviderModelOutputIncomplete,
    ProviderNormalizedTerminalKind,
    ProviderOutputIncompleteReason,
    ProviderStreamFailure,
    ProviderStreamTerminal,
)
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.model_input.contracts import (
    ModelInputScopeKind,
    PreparedProviderInputCut,
)
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.llm.request import (
    LLMContext,
    LLMOptions,
    provider_assistant_public_projection_fingerprint,
)
from pulsara_agent.llm.resolution import resolve_model_call, resolve_model_target
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.llm.stream_limits import (
    MAX_CHAT_REASONING_REPLAY_AGGREGATE_BYTES,
    MAX_CHAT_REASONING_REPLAY_ITEMS_PER_RESPONSE,
    MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES,
)
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ProviderModelStreamErrorCode,
)
from tests.support.model_config import test_llm_config


def _chat_profile(
    *,
    message_field: str = "reasoning_content",
    fields: tuple[ProviderChatReplayFieldContract, ...] = (),
    replay_policy: ThinkingReplayPolicy = ThinkingReplayPolicy.ALWAYS,
) -> ProviderProfile:
    return ProviderProfile(
        id=f"test:{message_field}",
        wire_api="openai_chat_completions",
        thinking=ThinkingProfile(
            enabled=True,
            message_field=message_field,
            replay_policy=replay_policy,
        ),
        configured_chat_replay_fields=fields,
    )


def _chat_chunk(
    delta: dict[str, object],
    finish_reason: str | None = None,
    *,
    message: dict[str, object] | None = None,
) -> dict[str, object]:
    choice: dict[str, object] = {
        "index": 0,
        "delta": delta,
        "finish_reason": finish_reason,
    }
    if message is not None:
        choice["message"] = message
    return {"choices": [choice]}


def test_openai_function_tools_share_one_explicit_non_strict_wire_contract() -> None:
    for catalog_entry in builtin_tool_catalog():
        descriptor = catalog_entry.descriptor
        if not descriptor.is_model_callable or descriptor.input_schema is None:
            continue
        canonical_schema = thaw_tool_json_object(descriptor.input_schema)
        tool = ToolSpec(
            descriptor.name,
            descriptor.description,
            canonical_schema,
        )
        chat = chat_tool_wire_items((tool,))[0]
        responses = responses_tool_wire_items((tool,))[0]
        assert chat["type"] == responses["type"] == "function"
        assert chat["function"] == {
            key: responses[key]
            for key in ("name", "description", "parameters", "strict")
        }
        assert chat["function"]["strict"] is False
        assert responses["strict"] is False
        assert chat["function"]["parameters"]["type"] == "object"
        assert responses["parameters"] == chat["function"]["parameters"]
        assert canonical_schema == thaw_tool_json_object(descriptor.input_schema)

    monitor = next(
        item
        for item in builtin_tool_catalog()
        if item.descriptor.name == "terminal_monitor"
    )
    monitor_schema = thaw_tool_json_object(monitor.descriptor.input_schema)
    monitor_wire = responses_tool_wire_items(
        (
            ToolSpec(
                monitor.descriptor.name,
                monitor.descriptor.description,
                monitor_schema,
            ),
        )
    )[0]
    monitor_parameters = monitor_wire["parameters"]
    assert "oneOf" not in monitor_parameters
    assert "anyOf" not in monitor_parameters
    assert "discriminator" not in monitor_parameters
    assert monitor_parameters["required"] == ["action"]
    assert monitor_parameters["properties"]["action"]["enum"] == [
        "register",
        "list",
        "cancel",
    ]
    assert "process_id" in monitor_parameters["properties"]
    assert "monitor_id" in monitor_parameters["properties"]
    assert "When action is 'register'" in monitor_parameters["description"]


def test_openai_function_tool_declares_object_root_without_mutating_schema() -> None:
    schema = {
        "oneOf": [
            {
                "type": "object",
                "properties": {"action": {"const": "one"}},
                "required": ["action"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"action": {"const": "two"}},
                "required": ["action"],
                "additionalProperties": False,
            },
        ]
    }
    tool = ToolSpec("union_tool", "A closed union.", schema)
    chat = chat_tool_wire_items((tool,))[0]["function"]
    responses = responses_tool_wire_items((tool,))[0]
    assert "type" not in schema
    assert chat["parameters"]["type"] == "object"
    assert responses["parameters"] == chat["parameters"]
    assert chat["strict"] is responses["strict"] is False
    assert chat["parameters"]["properties"]["action"]["enum"] == ["one", "two"]
    assert chat["parameters"]["required"] == ["action"]
    assert "oneOf" not in chat["parameters"]


def test_openai_function_tool_rejects_non_object_argument_root() -> None:
    tool = ToolSpec("invalid", "Invalid function arguments.", {"type": "string"})
    with pytest.raises(ValueError, match="object root"):
        chat_tool_wire_items((tool,))
    with pytest.raises(ValueError, match="object root"):
        responses_tool_wire_items((tool,))


def test_openai_function_root_union_inherits_base_object_contract() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer"},
        },
        "oneOf": [
            {"required": ["a"]},
            {"required": ["b"]},
        ],
        "additionalProperties": True,
    }

    lowered = lower_openai_function_parameters(schema)

    assert lowered["type"] == "object"
    assert lowered["properties"] == schema["properties"]
    assert lowered["required"] == []
    assert lowered["additionalProperties"] is True
    assert schema["oneOf"] == [{"required": ["a"]}, {"required": ["b"]}]


def test_openai_function_lowering_rejects_intersecting_nested_unions() -> None:
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "oneOf": [{"type": "string"}],
                "anyOf": [{"minLength": 1}],
            }
        },
    }

    with pytest.raises(
        OpenAIFunctionSchemaIncompatible,
        match="cannot combine oneOf and anyOf",
    ):
        lower_openai_function_parameters(schema)


def test_assistant_settlement_identity_covers_scope_and_epoch() -> None:
    cut = PreparedProviderInputCut("session:test", "turn:test", "revision:test", 1)
    content = InlineContent.from_bytes(b"assistant")
    occurred_at = datetime(2026, 8, 17, tzinfo=timezone.utc)
    root = ProviderInputContinuityScope(
        "session:test", ModelInputScopeKind.ROOT, None
    )
    child = ProviderInputContinuityScope(
        "session:test", ModelInputScopeKind.SUBAGENT_TASK, "task:test"
    )

    def fingerprint(
        scope: ProviderInputContinuityScope, nonce: str, revision: int
    ) -> str:
        return assistant_settlement_candidate_fingerprint(
            cut=cut,
            entry_id="entry:test",
            parent_content=content,
            blocks=(),
            complete_turn=True,
            occurred_at=occurred_at,
            actor_id="model:test",
            continuity_scope=scope,
            continuity_epoch_nonce=nonce,
            continuity_epoch_revision=revision,
            provider_wire_api="openai_chat_completions",
            provider_replay_disposition=(
                ProviderReplayDisposition.PUBLIC_SEMANTIC_ONLY
            ),
            provider_replay=None,
        )

    baseline = fingerprint(root, "epoch:root", 1)
    assert fingerprint(root, "epoch:other", 1) != baseline
    assert fingerprint(root, "epoch:root", 2) != baseline
    assert fingerprint(child, "epoch:root", 1) != baseline


def test_chat_completed_text_reasoning_replay_is_explicit_and_exact() -> None:
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=_chat_profile()
    )
    accumulator.apply(_chat_chunk({"role": "assistant"}))
    accumulator.apply(_chat_chunk({"reasoning_content": "rea"}))
    accumulator.apply(_chat_chunk({"reasoning_content": "son"}))
    accumulator.apply(_chat_chunk({"content": "answer"}, "stop"))

    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.COMPLETED
    assert terminal.completed_replay_payload is not None
    assert terminal.completed_replay_payload.codec_kind is (
        ProviderAssistantReplayCodecKind.CHAT_CLOSED_REASONING_FIELDS
    )
    assert thaw_json(terminal.completed_replay_payload.ordered_items[0]) == {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "reason",
    }


def test_chat_null_reasoning_deltas_do_not_erase_or_forge_a_replay_carrier() -> None:
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=_chat_profile()
    )
    assert accumulator.apply(_chat_chunk({"reasoning_content": None})) == []
    accumulator.apply(_chat_chunk({"reasoning_content": "exact"}))
    accumulator.apply(
        _chat_chunk(
            {"content": "", "reasoning_content": None},
            "tool_calls",
            message={
                "role": "assistant",
                "content": "",
                "reasoning_content": None,
            },
        )
    )
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.completed_replay_payload is not None
    replay = thaw_json(terminal.completed_replay_payload.ordered_items[0])
    assert replay["reasoning_content"] == "exact"


def test_chat_observed_known_reasoning_is_retained_despite_legacy_policy() -> None:
    profile = ProviderProfile(
        id="test:live-thinking-only",
        wire_api="openai_chat_completions",
        thinking=ThinkingProfile(
            enabled=True,
            delta_fields=("reasoning_content",),
            message_field="reasoning_content",
            replay_policy=ThinkingReplayPolicy.NEVER,
        ),
    )
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    events = accumulator.apply(_chat_chunk({"reasoning_content": "private"}))
    events.extend(accumulator.apply(_chat_chunk({"content": "public"}, "stop")))
    assert any(isinstance(item, ThinkingStartPayload) for item in events)
    assert any(isinstance(item, ThinkingDeltaPayload) for item in events)
    assert any(isinstance(item, ThinkingEndPayload) for item in events)
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.completed_replay_payload is not None


def test_chat_reasoning_registry_is_closed_and_provider_neutral() -> None:
    profile = _chat_profile()
    assert tuple(
        (item.field_name, item.accumulation_mode)
        for item in profile.chat_replay_fields
    ) == (
        ("reasoning_content", ProviderChatFieldAccumulationMode.TEXT_CONCAT),
        ("reasoning", ProviderChatFieldAccumulationMode.TEXT_CONCAT),
        (
            "reasoning_details",
            ProviderChatFieldAccumulationMode.ORDERED_ARRAY_APPEND,
        ),
    )
    with pytest.raises(ValueError, match="outside the closed registry"):
        _chat_profile(
            fields=(
                ProviderChatReplayFieldContract(
                    "reasoning_signature",
                    ProviderChatFieldAccumulationMode.ORDERED_ARRAY_APPEND,
                ),
            )
        )
    with pytest.raises(ValueError, match="message field must be textual"):
        ThinkingProfile(message_field="reasoning_details")


def test_chat_closed_field_accumulation_and_final_reconciliation() -> None:
    profile = _chat_profile()
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    accumulator.apply(
        _chat_chunk(
            {
                "reasoning_content": "private-",
                "reasoning": "normalized-",
                "reasoning_details": [{"type": "a", "value": 1}],
            }
        )
    )
    accumulator.apply(
        _chat_chunk(
            {
                "reasoning_content": "text",
                "reasoning": "text",
                "reasoning_details": [{"type": "b", "value": 2}],
            }
        )
    )
    accumulator.apply(
        _chat_chunk(
            {"content": "ok"},
            "stop",
            message={
                "role": "assistant",
                "content": "ok",
                "reasoning_content": "private-text",
                "reasoning": "normalized-text",
                "reasoning_details": [
                    {"type": "a", "value": 1},
                    {"type": "b", "value": 2},
                ],
            },
        )
    )
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.completed_replay_payload is not None
    message = thaw_json(terminal.completed_replay_payload.ordered_items[0])
    assert message["reasoning_details"] == [
        {"type": "a", "value": 1},
        {"type": "b", "value": 2},
    ]
    assert message["reasoning_content"] == "private-text"
    assert message["reasoning"] == "normalized-text"


def test_chat_opaque_replay_item_limit_fails_before_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pulsara_agent.llm.adapters.openai.chat_completions."
        "MAX_CHAT_REASONING_REPLAY_ITEMS_PER_RESPONSE",
        1_000,
    )
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=_chat_profile()
    )
    for index in range(1_000):
        assert accumulator.apply(
            _chat_chunk({"reasoning_details": [{"ordinal": index}]})
        ) == []
    assert len(accumulator._array_field_items["reasoning_details"]) == 1_000

    with pytest.raises(LLMTransportContractError) as captured:
        accumulator.apply(
            _chat_chunk({"reasoning_details": [{"ordinal": 1_000}]})
        )
    assert captured.value.reason_code == "transport_source_item_limit_exceeded"
    assert accumulator.terminal is None
    assert accumulator._array_field_items == {}


def test_chat_reasoning_replay_bounds_are_physical_headroom() -> None:
    assert (
        MAX_CHAT_REASONING_REPLAY_AGGREGATE_BYTES
        == MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES
    )
    assert MAX_CHAT_REASONING_REPLAY_ITEMS_PER_RESPONSE == 65_536


def test_chat_text_reasoning_accumulates_chunks_without_repeated_concat() -> None:
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=_chat_profile()
    )
    for _index in range(128):
        accumulator.apply(_chat_chunk({"reasoning_content": "x"}))
    assert accumulator._text_field_chunks["reasoning_content"] == ["x"] * 128
    accumulator.apply(
        _chat_chunk(
            {"content": "done"},
            "stop",
            message={
                "role": "assistant",
                "content": "done",
                "reasoning_content": "x" * 128,
            },
        )
    )
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.completed_replay_payload is not None
    assert accumulator._text_field_chunks == {}


def test_chat_replay_byte_overflow_is_typed_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pulsara_agent.llm.adapters.openai.chat_completions."
        "MAX_CHAT_REASONING_REPLAY_AGGREGATE_BYTES",
        64,
    )

    class FakeCompletions:
        calls = 0

        async def create(self, **_kwargs: object):
            self.calls += 1

            async def chunks():
                yield _chat_chunk(
                    {"reasoning_details": [{"opaque": "x" * 128}]}
                )

            return chunks()

    class FakeChat:
        def __init__(self, completions: FakeCompletions) -> None:
            self.completions = completions

    class FakeClient:
        def __init__(self, completions: FakeCompletions) -> None:
            self.chat = FakeChat(completions)

    completions = FakeCompletions()
    adapter = OpenAIChatCompletionsTransport(
        api_key="test",
        timeout_policy=OpenAITransportTimeoutPolicy(1, 1, 1, 1, None),
        retry_config=LLMRetryConfig(enabled=True, attempts=3),
    )
    adapter._client = FakeClient(completions)
    registry = NormalizedLLMTransportRegistry()
    registry.register(NormalizedLLMTransport(adapter))
    profile = _chat_profile()
    config = test_llm_config(
        api_key="test",
        base_url="https://example.invalid/v1",
        pro_model="test-model",
        flash_model="test-model",
        api=OPENAI_CHAT_COMPLETIONS_API,
        provider_profile=profile,
    )
    target = resolve_model_target(
        config=config,
        registry=registry,
        role=ModelRole.PRO,
        requested_options=LLMOptions(),
    )
    call = resolve_model_call(
        target=target,
        purpose=ModelCallPurpose.MEMORY_HINT_REVIEW,
    )
    context = LLMContext(
        messages=(LLMMessage.user("bounded"),),
        context_id="round5a1:opaque-overflow",
        resolved_model_call_id=call.resolved_model_call_id,
        target_fingerprint=call.target.fact.target_fingerprint,
        model_call_index=None,
    )

    async def exercise() -> list[object]:
        return [item async for item in adapter.stream(call=call, context=context)]

    output = asyncio.run(exercise())
    assert completions.calls == 1
    assert len(output) == 1
    failure = output[0]
    assert isinstance(failure, ProviderStreamFailure)
    assert failure.code_hint == "transport_source_payload_limit_exceeded"
    assert failure.retry_summary is not None
    assert failure.retry_summary.final_attempt == 1
    assert failure.retry_summary.skipped_reason == "unknown_non_retryable"


def test_chat_conflicting_array_final_and_incomplete_never_complete() -> None:
    profile = _chat_profile()
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    accumulator.apply(_chat_chunk({"reasoning_details": [{"v": 1}]}))
    with pytest.raises(LLMTransportContractError):
        accumulator.apply(
            _chat_chunk(
                {},
                "stop",
                message={
                    "role": "assistant",
                    "content": None,
                    "reasoning_details": [{"v": 2}],
                },
            )
        )

    incomplete = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    incomplete.apply(
        _chat_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call:test",
                        "function": {"name": "virtual", "arguments": "{\"x\":"},
                    }
                ]
            }
        )
    )
    incomplete.apply(_chat_chunk({}, "length"))
    terminal = incomplete.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.OUTPUT_INCOMPLETE
    assert terminal.incomplete_reason is ProviderOutputIncompleteReason.OUTPUT_TOKEN_LIMIT
    assert terminal.completed_replay_payload is None


def test_chat_tool_response_without_reasoning_carrier_needs_no_replay() -> None:
    profile = ProviderProfile(
        wire_api="openai_chat_completions",
        thinking=ThinkingProfile(
            enabled=True,
            replay_policy=ThinkingReplayPolicy.WHEN_TOOL_CALLS,
        ),
    )
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    accumulator.apply(
        _chat_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call:no-reasoning",
                        "type": "function",
                        "function": {"name": "virtual", "arguments": "{}"},
                    }
                ]
            },
            "tool_calls",
        )
    )
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.COMPLETED
    assert terminal.completed_replay_payload is None
    completion = CompletedProviderModelExecution(
        terminal=ProviderStreamTerminal(
            terminal_kind=ProviderNormalizedTerminalKind.COMPLETED,
            usage=TransportUsageReport(usage_status="missing", usage=None),
        ),
        replay_payload=None,
        replay_target=build_provider_replay_target_compatibility(
            wire_api="openai_chat_completions",
            endpoint_identity_fingerprint="sha256:" + "1" * 64,
            normalized_model_identifier="test-model",
            transport_binding_id="openai_chat_completions",
        ),
    )
    assert (
        completion.bind_assistant_entry(
            assistant_entry_id="entry:no-reasoning",
            public_projection_fingerprint="sha256:" + "3" * 64,
            has_tool_calls=True,
        )
        is None
    )


def test_chat_structured_reasoning_tool_response_and_terminal_echo_round_trip() -> None:
    profile = ProviderProfile(
        wire_api="openai_chat_completions",
        thinking=ThinkingProfile(
            enabled=True,
            replay_policy=ThinkingReplayPolicy.WHEN_TOOL_CALLS,
        ),
    )
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    accumulator.apply(
        _chat_chunk(
            {
                "role": "assistant",
                "content": "",
                "reasoning": None,
                "reasoning_details": [
                    {"type": "reasoning.text", "text": "opaque-test"}
                ],
            }
        )
    )
    accumulator.apply(
        _chat_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call:structured",
                        "type": "function",
                        "function": {"name": "virtual", "arguments": "{}"},
                    }
                ]
            },
            "tool_calls",
        )
    )
    assert accumulator.apply(
        _chat_chunk(
            {"content": "", "reasoning": None, "reasoning_details": []},
            "tool_calls",
        )
    ) == []
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.completed_replay_payload is not None
    replay = thaw_json(terminal.completed_replay_payload.ordered_items[0])
    assert replay["reasoning_details"] == [
        {"type": "reasoning.text", "text": "opaque-test"}
    ]
    assert "reasoning" not in replay


def test_chat_unknown_empty_carriers_are_ignorable() -> None:
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=_chat_profile()
    )
    accumulator.apply(
        _chat_chunk(
            {
                "future_null": None,
                "future_text": "",
                "future_array": [],
                "future_object": {},
                "content": "answer",
            },
            "stop",
        )
    )
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.COMPLETED


def test_chat_unknown_nonempty_final_carrier_is_not_replayed() -> None:
    profile = ProviderProfile(
        wire_api="openai_chat_completions",
        thinking=ThinkingProfile(
            enabled=True,
            replay_policy=ThinkingReplayPolicy.WHEN_TOOL_CALLS,
        ),
    )
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    accumulator.apply(
        _chat_chunk(
            {
                "future_reasoning_carrier": {"opaque": "bounded"},
                "content": "supported answer",
            },
            "stop",
        )
    )
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.COMPLETED
    assert terminal.completed_replay_payload is None


def test_chat_unknown_nonempty_tool_carrier_fails_before_terminal() -> None:
    profile = ProviderProfile(
        wire_api="openai_chat_completions",
        thinking=ThinkingProfile(
            enabled=True,
            replay_policy=ThinkingReplayPolicy.WHEN_TOOL_CALLS,
        ),
    )
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    with pytest.raises(LLMTransportContractError) as captured:
        accumulator.apply(
            _chat_chunk(
                {
                    "future_reasoning_carrier": {"opaque": "bounded"},
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call:unknown-carrier",
                            "type": "function",
                            "function": {"name": "virtual", "arguments": "{}"},
                        }
                    ],
                },
                "tool_calls",
            )
        )
    assert captured.value.reason_code == "transport_chat_replay_field_unsupported"
    assert accumulator.terminal is None


def test_chat_unknown_nonempty_carrier_cannot_be_the_only_output() -> None:
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=_chat_profile()
    )
    with pytest.raises(LLMTransportContractError) as captured:
        accumulator.apply(
            _chat_chunk(
                {"future_reasoning_carrier": {"opaque": "bounded"}},
                "stop",
            )
        )
    assert captured.value.reason_code == "transport_chat_replay_field_unsupported"
    assert accumulator.terminal is None


def test_chat_eof_and_terminal_followed_by_semantic_chunk_fail_closed() -> None:
    profile = ProviderProfile(wire_api="openai_chat_completions")
    eof = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    eof.apply(_chat_chunk({"content": "partial"}))
    assert isinstance(eof.finish(), ProviderStreamFailure)

    ended = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    ended.apply(_chat_chunk({"content": "done"}, "stop"))
    with pytest.raises(LLMTransportContractError):
        ended.apply(_chat_chunk({"content": "late"}))


def test_chat_exact_empty_terminal_echo_is_idempotent_usage_metadata() -> None:
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(),
        provider_profile=ProviderProfile(wire_api="openai_chat_completions"),
    )
    accumulator.apply(_chat_chunk({"content": "done"}, "stop"))
    echo = {
        "usage": {
            "prompt_tokens": 101,
            "completion_tokens": 2,
            "total_tokens": 103,
        },
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": "stop",
            }
        ],
    }

    assert accumulator.apply(echo) == []
    assert accumulator.apply(echo) == []
    assert accumulator.usage_report is not None
    assert accumulator.usage_report.usage_status == "reported"
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.COMPLETED


def test_chat_terminal_echo_must_not_change_reason_or_carry_semantics() -> None:
    profile = ProviderProfile(wire_api="openai_chat_completions")
    changed_reason = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    changed_reason.apply(_chat_chunk({"content": "done"}, "stop"))
    with pytest.raises(
        LLMTransportContractError,
        match="semantic data after its terminal",
    ):
        changed_reason.apply(_chat_chunk({}, "tool_calls"))

    changed_body = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    changed_body.apply(_chat_chunk({"content": "done"}, "stop"))
    with pytest.raises(
        LLMTransportContractError,
        match="semantic data after its terminal",
    ):
        changed_body.apply(_chat_chunk({"content": "late"}, "stop"))


def test_chat_tool_terminal_usage_echo_does_not_duplicate_tool_semantics() -> None:
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(),
        provider_profile=ProviderProfile(wire_api="openai_chat_completions"),
    )
    events = accumulator.apply(
        _chat_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call:echo",
                        "type": "function",
                        "function": {"name": "virtual", "arguments": "{}"},
                    }
                ]
            },
            "tool_calls",
        )
    )
    event_count = len(events)
    assert accumulator.apply(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "total_tokens": 105,
            },
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": "tool_calls",
                }
            ],
        }
    ) == []
    assert len(events) == event_count
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.COMPLETED


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    (
        ("length", ProviderOutputIncompleteReason.OUTPUT_TOKEN_LIMIT),
        ("content_filter", ProviderOutputIncompleteReason.CONTENT_FILTERED),
        ("function_call", ProviderOutputIncompleteReason.UNKNOWN_PROVIDER_INCOMPLETE),
        ("future_reason", ProviderOutputIncompleteReason.UNKNOWN_PROVIDER_INCOMPLETE),
    ),
)
def test_chat_incomplete_finish_reason_matrix_is_closed(
    finish_reason: str,
    expected: ProviderOutputIncompleteReason,
) -> None:
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(),
        provider_profile=ProviderProfile(wire_api="openai_chat_completions"),
    )
    accumulator.apply(_chat_chunk({"content": "partial"}))
    final_events = accumulator.apply(_chat_chunk({}, finish_reason))
    terminal = accumulator.finish()

    assert final_events == []
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.OUTPUT_INCOMPLETE
    assert terminal.incomplete_reason is expected


def test_chat_choice_and_completed_tool_argument_contracts_fail_closed() -> None:
    profile = ProviderProfile(wire_api="openai_chat_completions")
    multiple = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    with pytest.raises(LLMTransportContractError, match="exactly one choice"):
        multiple.apply(
            {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": None},
                    {"index": 1, "delta": {}, "finish_reason": None},
                ]
            }
        )

    partial_tool = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    partial_tool.apply(
        _chat_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call:partial",
                        "type": "function",
                        "function": {"name": "virtual", "arguments": '{"x":'},
                    }
                ]
            }
        )
    )
    with pytest.raises(LLMTransportContractError, match="complete JSON"):
        partial_tool.apply(_chat_chunk({}, "tool_calls"))


def test_chat_replay_field_absence_empty_and_final_contract_are_distinct() -> None:
    profile = _chat_profile(
        fields=(
            ProviderChatReplayFieldContract(
                "reasoning_content",
                ProviderChatFieldAccumulationMode.TEXT_CONCAT,
                final_value_required=True,
            ),
        )
    )
    missing = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    with pytest.raises(LLMTransportContractError, match="required final message"):
        missing.apply(_chat_chunk({"content": "answer"}, "stop"))

    present_empty = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    present_empty.apply(_chat_chunk({"reasoning_content": ""}))
    present_empty.apply(
        _chat_chunk(
            {"content": "answer"},
            "stop",
            message={
                "role": "assistant",
                "content": "answer",
                "reasoning_content": "",
            },
        )
    )
    terminal = present_empty.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert thaw_json(terminal.completed_replay_payload.ordered_items[0])[  # type: ignore[union-attr]
        "reasoning_content"
    ] == ""

    mismatch = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    mismatch.apply(_chat_chunk({"reasoning_content": "first"}))
    with pytest.raises(LLMTransportContractError, match="differs from its deltas"):
        mismatch.apply(
            _chat_chunk(
                {},
                "stop",
                message={
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "second",
                },
            )
        )


def test_chat_final_value_required_is_independent_of_legacy_replay_policy() -> None:
    profile = _chat_profile(
        replay_policy=ThinkingReplayPolicy.NEVER,
        fields=(
            ProviderChatReplayFieldContract(
                "reasoning_content",
                ProviderChatFieldAccumulationMode.TEXT_CONCAT,
                final_value_required=True,
            ),
        ),
    )
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    accumulator.apply(_chat_chunk({"reasoning_content": "sealed carrier"}))
    accumulator.apply(
        _chat_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call:never-final",
                        "type": "function",
                        "function": {"name": "virtual", "arguments": "{}"},
                    }
                ]
            }
        )
    )

    with pytest.raises(
        LLMTransportContractError,
        match="lacks a required replay field",
    ):
        accumulator.apply(
            _chat_chunk(
                {},
                "tool_calls",
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call:never-final",
                            "type": "function",
                            "function": {"name": "virtual", "arguments": "{}"},
                        }
                    ],
                },
            )
        )
    assert accumulator.terminal is None


def _completed_response(output: list[dict[str, object]]) -> dict[str, object]:
    return {
        "type": "response.completed",
        "response": {"id": "response:test", "output": output},
    }


def test_responses_completed_replay_preserves_all_allowed_items_in_order() -> None:
    output = [
        {
            "type": "reasoning",
            "id": "reasoning:1",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": "summary"}],
            "encrypted_content": "opaque-test-value",
        },
        {
            "type": "message",
            "id": "message:1",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "answer"}],
        },
        {
            "type": "function_call",
            "id": "item:1",
            "status": "completed",
            "call_id": "call:1",
            "name": "virtual",
            "arguments": "{}",
        },
        {
            "type": "function_call",
            "id": "item:2",
            "status": "completed",
            "call_id": "call:2",
            "name": "virtual_two",
            "arguments": "{\"ok\":true}",
        },
    ]
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    accumulator.apply(_completed_response(output))
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.COMPLETED
    assert terminal.completed_replay_payload is not None
    assert [
        thaw_json(item) for item in terminal.completed_replay_payload.ordered_items
    ] == output


@pytest.mark.parametrize(
    ("reason", "expected"),
    (
        ("max_output_tokens", ProviderOutputIncompleteReason.OUTPUT_TOKEN_LIMIT),
        (
            "model_context_window_exceeded",
            ProviderOutputIncompleteReason.CONTEXT_WINDOW_LIMIT_DURING_GENERATION,
        ),
        ("content_filter", ProviderOutputIncompleteReason.CONTENT_FILTERED),
        ("future_reason", ProviderOutputIncompleteReason.UNKNOWN_PROVIDER_INCOMPLETE),
        (None, ProviderOutputIncompleteReason.UNKNOWN_PROVIDER_INCOMPLETE),
    ),
)
def test_responses_incomplete_reason_is_closed(
    reason: str | None, expected: ProviderOutputIncompleteReason
) -> None:
    response: dict[str, object] = {"output": []}
    if reason is not None:
        response["incomplete_details"] = {"reason": reason}
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    accumulator.apply({"type": "response.incomplete", "response": response})
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.OUTPUT_INCOMPLETE
    assert terminal.incomplete_reason is expected
    assert terminal.completed_replay_payload is None


def test_responses_completed_tool_then_incomplete_remains_atomic() -> None:
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    item = {
        "type": "function_call",
        "id": "item:1",
        "status": "completed",
        "call_id": "call:1",
        "name": "virtual",
        "arguments": "{}",
    }
    accumulator.apply({"type": "response.output_item.done", "item": item})
    accumulator.apply(
        {
            "type": "response.incomplete",
            "response": {
                "output": [item],
                "incomplete_details": {"reason": "max_output_tokens"},
            },
        }
    )
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.OUTPUT_INCOMPLETE
    assert terminal.completed_replay_payload is None


@pytest.mark.parametrize(
    "bad_item",
    (
        {"type": "computer_call", "id": "bad"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "refusal", "refusal": "no"}],
        },
        {"type": "hosted_tool_call", "id": "bad"},
    ),
)
def test_responses_unknown_or_effect_bearing_output_fails_closed(
    bad_item: dict[str, object],
) -> None:
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    with pytest.raises(LLMTransportContractError):
        accumulator.apply(_completed_response([bad_item]))
    assert accumulator.terminal is None


def test_responses_stream_projection_exactly_joins_its_output_index() -> None:
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    accumulator.apply(
        {
            "type": "response.output_text.delta",
            "output_index": 1,
            "content_index": 0,
            "delta": "second",
        }
    )
    accumulator.apply(
        {
            "type": "response.output_text.done",
            "output_index": 1,
            "content_index": 0,
            "text": "second",
        }
    )
    output = [
        {
            "type": "reasoning",
            "id": "reasoning:1",
            "status": "completed",
            "summary": [],
        },
        {
            "type": "message",
            "id": "message:1",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "second"}],
        },
    ]
    accumulator.apply(_completed_response(output))
    assert isinstance(accumulator.finish(), ProviderAdapterTerminal)


def test_responses_reasoning_summary_and_content_are_separate_exact_streams() -> None:
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    accumulator.apply(
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "delta": "summary",
        }
    )
    accumulator.apply(
        {
            "type": "response.reasoning_summary_text.done",
            "output_index": 0,
            "text": "summary",
        }
    )
    accumulator.apply(
        {
            "type": "response.reasoning_text.delta",
            "output_index": 0,
            "delta": "reasoning",
        }
    )
    accumulator.apply(
        {
            "type": "response.reasoning_text.done",
            "output_index": 0,
            "text": "reasoning",
        }
    )
    accumulator.apply(
        _completed_response(
            [
                {
                    "type": "reasoning",
                    "id": "reasoning:1",
                    "status": "completed",
                    "summary": [{"type": "summary_text", "text": "summary"}],
                    "content": [{"type": "reasoning_text", "text": "reasoning"}],
                }
            ]
        )
    )
    assert isinstance(accumulator.finish(), ProviderAdapterTerminal)


def test_responses_exact_reasoning_text_to_summary_alias_is_provider_neutral() -> None:
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    accumulator.apply(
        {
            "type": "response.reasoning_text.delta",
            "output_index": 0,
            "delta": "public summary",
        }
    )
    accumulator.apply(
        {
            "type": "response.reasoning_text.done",
            "output_index": 0,
            "text": "public summary",
        }
    )
    accumulator.apply(
        _completed_response(
            [
                {
                    "type": "reasoning",
                    "id": "reasoning:alias",
                    "status": "completed",
                    "summary": [
                        {"type": "summary_text", "text": "public summary"}
                    ],
                }
            ]
        )
    )
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.completed_replay_payload is not None
    assert thaw_json(terminal.completed_replay_payload.ordered_items[0]) == {
        "type": "reasoning",
        "id": "reasoning:alias",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": "public summary"}],
    }


def test_responses_reasoning_text_to_summary_alias_must_be_exact() -> None:
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    accumulator.apply(
        {
            "type": "response.reasoning_text.delta",
            "output_index": 0,
            "delta": "streamed",
        }
    )
    accumulator.apply(
        {
            "type": "response.reasoning_text.done",
            "output_index": 0,
            "text": "streamed",
        }
    )
    with pytest.raises(
        LLMTransportContractError,
        match="reasoning content differs",
    ):
        accumulator.apply(
            _completed_response(
                [
                    {
                        "type": "reasoning",
                        "id": "reasoning:alias-conflict",
                        "status": "completed",
                        "summary": [
                            {"type": "summary_text", "text": "different"}
                        ],
                    }
                ]
            )
        )


@pytest.mark.parametrize(
    "bad_output",
    (
        {
            "type": "function_call",
            "id": "item:missing-arguments",
            "status": "completed",
            "call_id": "call:missing-arguments",
            "name": "virtual",
        },
        {
            "type": "function_call",
            "id": "item:non-wire-arguments",
            "status": "completed",
            "call_id": "call:non-wire-arguments",
            "name": "virtual",
            "arguments": {},
        },
        {
            "type": "function_call",
            "id": "item:partial-arguments",
            "status": "completed",
            "call_id": "call:partial-arguments",
            "name": "virtual",
            "arguments": "{",
        },
        {
            "type": "function_call",
            "id": "item:non-object-arguments",
            "status": "completed",
            "call_id": "call:non-object-arguments",
            "name": "virtual",
            "arguments": "[]",
        },
        {
            "type": "message",
            "id": "message:running",
            "status": "in_progress",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "not complete"}],
        },
    ),
)
def test_responses_completed_output_requires_complete_closed_fields(
    bad_output: dict[str, object],
) -> None:
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    with pytest.raises(LLMTransportContractError):
        accumulator.apply(_completed_response([bad_output]))


def test_responses_completed_event_rejects_noncompleted_response_status() -> None:
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    with pytest.raises(LLMTransportContractError, match="non-completed response"):
        accumulator.apply(
            {
                "type": "response.completed",
                "response": {
                    "status": "incomplete",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "partial"}
                            ],
                        }
                    ],
                },
            }
        )


def test_responses_current_closed_phase_and_reasoning_format_replay_exactly() -> None:
    output = [
        {
            "type": "reasoning",
            "id": "reasoning:1",
            "status": "completed",
            "format": "openai-responses-v1",
            "summary": [],
        },
        {
            "type": "message",
            "id": "message:1",
            "status": "completed",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "done"}],
        },
    ]
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    accumulator.apply(_completed_response(output))
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.completed_replay_payload is not None
    assert tuple(
        thaw_json(item) for item in terminal.completed_replay_payload.ordered_items
    ) == tuple(output)


@pytest.mark.parametrize(
    "item",
    (
        {
            "type": "reasoning",
            "format": "future-format",
            "summary": [],
        },
        {
            "type": "message",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "not-final"}],
        },
    ),
)
def test_responses_unknown_reasoning_format_or_message_phase_fails_closed(
    item: dict[str, object],
) -> None:
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    with pytest.raises(LLMTransportContractError, match="unsupported"):
        accumulator.apply(_completed_response([item]))


def test_responses_output_item_event_rejects_unknown_effect_bearing_type() -> None:
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    with pytest.raises(LLMTransportContractError, match="unsupported output item"):
        accumulator.apply(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "computer_call", "id": "computer:1"},
            }
        )


def test_normalized_transport_requires_eof_after_explicit_terminal() -> None:
    async def stream():
        yield ProviderAdapterTerminal(ProviderAdapterTerminalKind.COMPLETED)
        yield TextStartPayload("late")

    async def exercise() -> ProviderStreamTerminal:
        execution = NormalizedProviderTransportExecution(stream())
        result = await execution.read_next()
        assert isinstance(result, ProviderStreamTerminal)
        return result

    result = asyncio.run(exercise())
    assert result.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR


def test_normalized_incomplete_does_not_synthesize_open_block_end() -> None:
    async def stream():
        yield TextStartPayload("text:1")
        yield TextDeltaPayload("text:1", "partial")
        yield ProviderAdapterTerminal(
            ProviderAdapterTerminalKind.OUTPUT_INCOMPLETE,
            incomplete_reason=ProviderOutputIncompleteReason.OUTPUT_TOKEN_LIMIT,
        )

    async def exercise() -> list[object]:
        execution = NormalizedProviderTransportExecution(stream())
        results: list[object] = []
        while (item := await execution.read_next()) is not None:
            results.append(item)
        return results

    results = asyncio.run(exercise())
    assert [type(item) for item in results] == [
        TextStartPayload,
        TextDeltaPayload,
        ProviderStreamTerminal,
    ]
    terminal = results[-1]
    assert isinstance(terminal, ProviderStreamTerminal)
    assert terminal.terminal_kind is ProviderNormalizedTerminalKind.OUTPUT_INCOMPLETE


def test_normalized_transport_does_not_reclassify_caller_cancellation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stream():
        entered.set()
        await release.wait()
        yield ProviderAdapterTerminal(ProviderAdapterTerminalKind.COMPLETED)

    async def exercise() -> None:
        execution = NormalizedProviderTransportExecution(stream())
        task = asyncio.create_task(execution.read_next())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


def test_normalized_transport_keeps_adapter_contract_failure_typed() -> None:
    async def stream():
        raise LLMTransportContractError(
            "unsupported replay carrier",
            reason_code="transport_chat_replay_field_unsupported",
        )
        yield  # pragma: no cover - retains the async-generator shape

    async def exercise() -> ProviderStreamTerminal:
        execution = NormalizedProviderTransportExecution(stream())
        item = await execution.read_next()
        assert isinstance(item, ProviderStreamTerminal)
        return item

    terminal = asyncio.run(exercise())
    assert terminal.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
    assert terminal.error is not None
    assert terminal.error.code is ProviderModelStreamErrorCode.TRANSPORT_PROTOCOL_ERROR


def test_completed_replay_and_live_payload_share_one_aggregate_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _chat_profile()
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=profile
    )
    live_items = accumulator.apply(_chat_chunk({"content": "public"}))
    accumulator.apply(_chat_chunk({"reasoning_content": "opaque"}))
    live_items.extend(accumulator.apply(_chat_chunk({}, "stop")))
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)

    async def stream():
        for item in live_items:
            yield item
        yield terminal

    async def exercise() -> ProviderStreamTerminal:
        execution = NormalizedProviderTransportExecution(stream())
        terminal_result: ProviderStreamTerminal | None = None
        while (item := await execution.read_next()) is not None:
            if isinstance(item, ProviderStreamTerminal):
                terminal_result = item
        assert terminal_result is not None
        return terminal_result

    monkeypatch.setattr(
        "pulsara_agent.llm.normalized_transport."
        "MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES",
        terminal.completed_replay_payload.logical_utf8_bytes,  # type: ignore[union-attr]
    )
    result = asyncio.run(exercise())
    assert result.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
    assert result.completed_replay_payload is None


def test_sdk_decoded_json_shape_is_bounded_before_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pulsara_agent.llm.adapters.openai.events."
        "MAX_PROVIDER_DECODED_JSON_DEPTH",
        2,
    )
    with pytest.raises(LLMTransportContractError):
        sdk_event_to_dict({"outer": {"too_deep": "value"}})


def test_sdk_normalization_preserves_wire_presence_not_model_defaults() -> None:
    class _SdkEvent:
        def model_dump(self, **kwargs: object) -> dict[str, object]:
            assert kwargs == {"mode": "python", "exclude_unset": True}
            # An explicit null remains present; an SDK default which was not
            # set is deliberately absent from this wire-presence projection.
            return {"type": "response.test", "explicit_null": None}

    assert sdk_event_to_dict(_SdkEvent()) == {
        "type": "response.test",
        "explicit_null": None,
    }


def test_completed_replay_must_exactly_match_public_projection() -> None:
    accumulator = ChatCompletionAccumulator(
        builder=ProviderLiveItemBuilder(), provider_profile=_chat_profile()
    )
    accumulator.apply(_chat_chunk({"reasoning_content": "opaque"}))
    accumulator.apply(_chat_chunk({"content": "actual"}, "stop"))
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    payload = terminal.completed_replay_payload
    assert payload is not None
    completion = CompletedProviderModelExecution(
        terminal=ProviderStreamTerminal(
            terminal_kind=ProviderNormalizedTerminalKind.COMPLETED,
            usage=TransportUsageReport(usage_status="missing", usage=None),
            completed_replay_payload=payload,
        ),
        replay_payload=payload,
        replay_target=build_provider_replay_target_compatibility(
            wire_api="openai_chat_completions",
            endpoint_identity_fingerprint="sha256:" + "1" * 64,
            normalized_model_identifier="test-model",
            transport_binding_id="openai_chat_completions",
        ),
    )
    with pytest.raises(RuntimeError, match="public projection"):
        completion.bind_assistant_entry(
            assistant_entry_id="entry:test",
            public_projection_fingerprint="sha256:" + "3" * 64,
            has_tool_calls=False,
        )


def test_responses_rejects_output_order_canonical_blocks_cannot_round_trip() -> None:
    output = [
        {
            "type": "function_call",
            "id": "item:1",
            "status": "completed",
            "call_id": "call:1",
            "name": "virtual",
            "arguments": "{}",
        },
        {
            "type": "message",
            "id": "message:1",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "after tool"}],
        },
    ]
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    with pytest.raises(
        LLMTransportContractError,
        match="cannot round-trip through canonical blocks",
    ) as captured:
        accumulator.apply(_completed_response(output))
    assert (
        captured.value.reason_code
        == "transport_responses_output_order_unrepresentable"
    )


def test_responses_accepts_message_before_ordered_function_calls() -> None:
    output = [
        {
            "type": "message",
            "id": "message:1",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "before tools"}],
        },
        {
            "type": "function_call",
            "id": "item:1",
            "status": "completed",
            "call_id": "call:1",
            "name": "virtual",
            "arguments": "{}",
        },
        {
            "type": "function_call",
            "id": "item:2",
            "status": "completed",
            "call_id": "call:2",
            "name": "virtual_two",
            "arguments": "{}",
        },
    ]
    accumulator = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    accumulator.apply(_completed_response(output))
    terminal = accumulator.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    payload = terminal.completed_replay_payload
    assert payload is not None
    completion = CompletedProviderModelExecution(
        terminal=ProviderStreamTerminal(
            terminal_kind=ProviderNormalizedTerminalKind.COMPLETED,
            usage=TransportUsageReport(usage_status="missing", usage=None),
            completed_replay_payload=payload,
        ),
        replay_payload=payload,
        replay_target=build_provider_replay_target_compatibility(
            wire_api="openai_responses",
            endpoint_identity_fingerprint="sha256:" + "4" * 64,
            normalized_model_identifier="test-model",
            transport_binding_id="openai_responses",
        ),
    )
    calls = (
        LLMToolCall("call:1", "virtual", "{}"),
        LLMToolCall("call:2", "virtual_two", "{}"),
    )
    projection = provider_assistant_public_projection_fingerprint(
        text="before tools",
        tool_calls=calls,
        ordered_blocks=(
            ("TEXT", "before tools"),
            ("TOOL_CALL", "call:1", "virtual", "{}"),
            ("TOOL_CALL", "call:2", "virtual_two", "{}"),
        ),
    )
    fragment = completion.bind_assistant_entry(
        assistant_entry_id="entry:ordered",
        public_projection_fingerprint=projection,
        has_tool_calls=True,
    )
    assert fragment is not None


def test_auxiliary_valid_partial_json_is_not_parsed_after_incomplete() -> None:
    auxiliary = DirectKernelAuxiliaryJsonModel(
        test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="openai_chat_completions",
        )
    )
    prepared = auxiliary.prepare_json_call(
        purpose=ModelCallPurpose.MEMORY_HINT_REVIEW,
        prompt="return a bounded JSON object",
        maximum_input_tokens=1024,
        maximum_output_tokens=32,
        timeout_policy=OpenAITransportTimeoutPolicy(1, 1, 1, 1, 5),
    )
    prepared.call.target.transport._adapter._mock_chunks = [  # type: ignore[attr-defined]
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": '{"would_parse":true}'},
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "length"}
            ]
        },
    ]
    with pytest.raises(ProviderModelOutputIncomplete) as captured:
        asyncio.run(auxiliary.complete_prepared_json(prepared))
    assert captured.value.reason is ProviderOutputIncompleteReason.OUTPUT_TOKEN_LIMIT


class _Round5A1SSEHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        size = int(self.headers.get("content-length", "0"))
        self.rfile.read(size)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for frame in self.server.frames:  # type: ignore[attr-defined]
            self.wfile.write(frame)
            self.wfile.flush()
        self.close_connection = True


def _sse_data(value: dict[str, object]) -> bytes:
    return f"data: {json.dumps(value, separators=(',', ':'))}\n\n".encode()


def _sse_event(name: str, value: dict[str, object]) -> bytes:
    return (
        f"event: {name}\ndata: {json.dumps(value, separators=(',', ':'))}\n\n"
    ).encode()


def _local_chat_chunk(
    *, content: str = "", finish_reason: str | None = None
) -> dict[str, object]:
    return {
        "id": "chatcmpl_round5a1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": finish_reason,
            }
        ],
    }


def _local_response(
    *, status: str, output: list[dict[str, object]]
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": "resp_round5a1",
        "object": "response",
        "created_at": 1.0,
        "model": "test-model",
        "status": status,
        "output": output,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }
    if status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
    return result


async def _consume_provider_shaped_sse(
    *, api: str, base_url: str
) -> list[object]:
    timeout = OpenAITransportTimeoutPolicy(1, 1, 1, 1, None)
    profile = ProviderProfile(
        id="test:provider-shaped-sse",
        wire_api=api,
        thinking=ThinkingProfile(
            enabled=False,
            replay_policy=ThinkingReplayPolicy.NEVER,
        ),
    )
    adapter = (
        OpenAIChatCompletionsTransport(api_key="test", timeout_policy=timeout)
        if api == OPENAI_CHAT_COMPLETIONS_API
        else OpenAIResponsesTransport(api_key="test", timeout_policy=timeout)
    )
    registry = NormalizedLLMTransportRegistry()
    registry.register(NormalizedLLMTransport(adapter))
    config = test_llm_config(
        api_key="test",
        base_url=base_url,
        pro_model="test-model",
        flash_model="test-model",
        api=api,
        provider_profile=profile,
    )
    target = resolve_model_target(
        config=config,
        registry=registry,
        role=ModelRole.PRO,
        requested_options=LLMOptions(),
    )
    call = resolve_model_call(
        target=target, purpose=ModelCallPurpose.MEMORY_HINT_REVIEW
    )
    context = LLMContext(
        messages=(LLMMessage.user("bounded local fixture"),),
        context_id="round5a1:local-sse",
        resolved_model_call_id=call.resolved_model_call_id,
        target_fingerprint=call.target.fact.target_fingerprint,
        model_call_index=None,
    )
    return [item async for item in adapter.stream(call=call, context=context)]


@pytest.mark.parametrize(
    ("api", "frames"),
    (
        (
            OPENAI_CHAT_COMPLETIONS_API,
            (
                _sse_data(_local_chat_chunk(content="partial")),
                _sse_data(_local_chat_chunk(finish_reason="length")),
                b"data: [DONE]\n\n",
            ),
        ),
        (
            OPENAI_RESPONSES_API,
            (
                _sse_event(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "sequence_number": 1,
                        "output_index": 0,
                        "content_index": 0,
                        "item_id": "message:1",
                        "delta": "partial",
                        "logprobs": [],
                    },
                ),
                _sse_event(
                    "response.incomplete",
                    {
                        "type": "response.incomplete",
                        "sequence_number": 2,
                        "response": _local_response(status="incomplete", output=[]),
                    },
                ),
            ),
        ),
    ),
)
def test_provider_shaped_local_sse_reports_incomplete_not_completed(
    api: str, frames: tuple[bytes, ...]
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Round5A1SSEHandler)
    server.frames = frames  # type: ignore[attr-defined]
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        items = asyncio.run(
            _consume_provider_shaped_sse(
                api=api,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    terminal = next(
        item for item in items if isinstance(item, ProviderAdapterTerminal)
    )
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.OUTPUT_INCOMPLETE
    assert terminal.completed_replay_payload is None


def test_provider_shaped_local_sse_eof_before_chat_terminal_fails() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Round5A1SSEHandler)
    server.frames = (_sse_data(_local_chat_chunk(content="partial")),)  # type: ignore[attr-defined]
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        items = asyncio.run(
            _consume_provider_shaped_sse(
                api=OPENAI_CHAT_COMPLETIONS_API,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert any(isinstance(item, ProviderStreamFailure) for item in items)
    assert not any(isinstance(item, ProviderAdapterTerminal) for item in items)


def test_provider_shaped_responses_completed_uses_wire_present_sdk_fields() -> None:
    output = [
        {
            "type": "message",
            "id": "message:complete",
            "status": "completed",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "complete", "annotations": []}
            ],
        }
    ]
    frames = (
        _sse_event(
            "response.completed",
            {
                "type": "response.completed",
                "sequence_number": 1,
                "response": _local_response(status="completed", output=output),
            },
        ),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Round5A1SSEHandler)
    server.frames = frames  # type: ignore[attr-defined]
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        items = asyncio.run(
            _consume_provider_shaped_sse(
                api=OPENAI_RESPONSES_API,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    terminal = next(
        item for item in items if isinstance(item, ProviderAdapterTerminal)
    )
    assert terminal.terminal_kind is ProviderAdapterTerminalKind.COMPLETED
    assert terminal.completed_replay_payload is not None


def test_provider_shaped_responses_event_after_terminal_fails_closed() -> None:
    output = [
        {
            "type": "message",
            "id": "message:complete",
            "status": "completed",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "complete", "annotations": []}
            ],
        }
    ]
    frames = (
        _sse_event(
            "response.completed",
            {
                "type": "response.completed",
                "sequence_number": 1,
                "response": _local_response(status="completed", output=output),
            },
        ),
        _sse_event(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "sequence_number": 2,
                "output_index": 0,
                "content_index": 0,
                "item_id": "message:complete",
                "delta": "late",
                "logprobs": [],
            },
        ),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Round5A1SSEHandler)
    server.frames = frames  # type: ignore[attr-defined]
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        items = asyncio.run(
            _consume_provider_shaped_sse(
                api=OPENAI_RESPONSES_API,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert any(isinstance(item, ProviderStreamFailure) for item in items)
    assert not any(isinstance(item, ProviderAdapterTerminal) for item in items)
