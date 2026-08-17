"""Provider-profile configuration for OpenAI-compatible LLM APIs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pulsara_agent.primitives.context import context_fingerprint


class ThinkingReplayPolicy(StrEnum):
    """How a chat provider expects previous assistant thinking to be replayed."""

    NEVER = "never"
    WHEN_TOOL_CALLS = "when_tool_calls"
    ALWAYS = "always"


class ProviderAssistantReplayCodecKind(StrEnum):
    """Closed process-local codec used for same-epoch assistant replay."""

    NONE = "NONE"
    CHAT_TEXT_REASONING_FIELD = "CHAT_TEXT_REASONING_FIELD"
    CHAT_OPAQUE_REASONING_FIELDS = "CHAT_OPAQUE_REASONING_FIELDS"
    RESPONSES_EXACT_OUTPUT_ITEMS = "RESPONSES_EXACT_OUTPUT_ITEMS"


class ProviderReasoningReplayScope(StrEnum):
    NEVER = "NEVER"
    TOOL_RESPONSES = "TOOL_RESPONSES"
    ALL_COMPLETED_RESPONSES = "ALL_COMPLETED_RESPONSES"


class ProviderChatFieldAccumulationMode(StrEnum):
    TEXT_CONCAT = "TEXT_CONCAT"
    SINGLE_EXACT_VALUE = "SINGLE_EXACT_VALUE"
    ORDERED_ARRAY_APPEND = "ORDERED_ARRAY_APPEND"


@dataclass(frozen=True, slots=True)
class ProviderChatReplayFieldContract:
    field_name: str
    accumulation_mode: ProviderChatFieldAccumulationMode
    required_on_selected_response: bool = True
    final_value_required: bool = False

    def __post_init__(self) -> None:
        if not self.field_name or self.field_name != self.field_name.strip():
            raise ValueError("chat replay field name is invalid")


class ModelIdentityPolicy(StrEnum):
    """How provider-reported model identities relate to the requested route id."""

    ACCEPT_REPORTED = "accept_reported"
    EXACT = "exact"


@dataclass(frozen=True, slots=True)
class ThinkingProfile:
    """Provider-neutral description of thinking/reasoning wire fields."""

    enabled: bool = False
    delta_fields: tuple[str, ...] = ("reasoning_content",)
    message_field: str | None = "reasoning_content"
    replay_policy: ThinkingReplayPolicy = ThinkingReplayPolicy.NEVER


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """Custom provider behavior without making vendor names first-class code paths."""

    id: str = "custom"
    wire_api: str = "openai_responses"
    request_defaults: Mapping[str, Any] = field(default_factory=dict)
    request_extra_body: Mapping[str, Any] = field(default_factory=dict)
    omit_params_when_thinking: tuple[str, ...] = field(default_factory=tuple)
    supports_tools: bool = True
    supports_reasoning: bool = True
    model_identity_policy: ModelIdentityPolicy = ModelIdentityPolicy.ACCEPT_REPORTED
    thinking: ThinkingProfile = field(default_factory=ThinkingProfile)
    configured_chat_replay_fields: tuple[
        ProviderChatReplayFieldContract, ...
    ] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_defaults", _freeze_provider_value(self.request_defaults)
        )
        object.__setattr__(
            self,
            "request_extra_body",
            _freeze_provider_value(self.request_extra_body),
        )
        fields = self.chat_replay_fields
        names = tuple(item.field_name for item in fields)
        if len(names) != len(set(names)):
            raise ValueError("chat replay field contracts are duplicated")
        codec = self.assistant_replay_codec_kind
        scope = self.reasoning_replay_scope
        if (codec is ProviderAssistantReplayCodecKind.NONE) != (
            scope is ProviderReasoningReplayScope.NEVER
        ):
            raise ValueError("provider replay codec/scope union is invalid")
        if codec is ProviderAssistantReplayCodecKind.CHAT_TEXT_REASONING_FIELD:
            if len(fields) != 1 or fields[0].accumulation_mode is not (
                ProviderChatFieldAccumulationMode.TEXT_CONCAT
            ) or fields[0].field_name != self.thinking.message_field:
                raise ValueError("chat text replay requires one concat field")
        if codec is ProviderAssistantReplayCodecKind.CHAT_OPAQUE_REASONING_FIELDS:
            if not fields or any(
                item.accumulation_mode
                is ProviderChatFieldAccumulationMode.TEXT_CONCAT
                for item in fields
            ):
                raise ValueError("opaque chat replay fields are invalid")

    @property
    def reasoning_replay_scope(self) -> ProviderReasoningReplayScope:
        if self.wire_api == "openai_responses":
            return ProviderReasoningReplayScope.ALL_COMPLETED_RESPONSES
        return {
            ThinkingReplayPolicy.NEVER: ProviderReasoningReplayScope.NEVER,
            ThinkingReplayPolicy.WHEN_TOOL_CALLS: (
                ProviderReasoningReplayScope.TOOL_RESPONSES
            ),
            ThinkingReplayPolicy.ALWAYS: (
                ProviderReasoningReplayScope.ALL_COMPLETED_RESPONSES
            ),
        }[self.thinking.replay_policy]

    @property
    def assistant_replay_codec_kind(self) -> ProviderAssistantReplayCodecKind:
        if self.wire_api == "openai_responses":
            return ProviderAssistantReplayCodecKind.RESPONSES_EXACT_OUTPUT_ITEMS
        if self.reasoning_replay_scope is ProviderReasoningReplayScope.NEVER:
            return ProviderAssistantReplayCodecKind.NONE
        field_name = self.thinking.message_field
        if not field_name:
            raise ValueError("chat replay scope requires a message field")
        if field_name == "reasoning_content":
            return ProviderAssistantReplayCodecKind.CHAT_TEXT_REASONING_FIELD
        return ProviderAssistantReplayCodecKind.CHAT_OPAQUE_REASONING_FIELDS

    @property
    def chat_replay_fields(self) -> tuple[ProviderChatReplayFieldContract, ...]:
        if self.wire_api != "openai_chat_completions":
            return ()
        if self.thinking.replay_policy is ThinkingReplayPolicy.NEVER:
            return ()
        if self.configured_chat_replay_fields:
            return self.configured_chat_replay_fields
        field_name = self.thinking.message_field
        if not field_name:
            raise ValueError("chat replay scope requires a message field")
        mode = (
            ProviderChatFieldAccumulationMode.TEXT_CONCAT
            if field_name == "reasoning_content"
            else ProviderChatFieldAccumulationMode.ORDERED_ARRAY_APPEND
        )
        return (ProviderChatReplayFieldContract(field_name, mode),)

    @property
    def assistant_replay_contract_fingerprint(self) -> str:
        return context_fingerprint(
            "pulsara.provider-assistant-replay-profile:v2",
            {
                "profile": self.id,
                "wire_api": self.wire_api,
                "codec": self.assistant_replay_codec_kind.value,
                "scope": self.reasoning_replay_scope.value,
                "fields": tuple(
                    (
                        item.field_name,
                        item.accumulation_mode.value,
                        item.required_on_selected_response,
                        item.final_value_required,
                    )
                    for item in self.chat_replay_fields
                ),
                "responses_items": ("reasoning", "message", "function_call"),
                "responses_message_content": ("output_text", "text"),
                "responses_public_order": (
                    "optional_single_message_before_ordered_function_calls:v1"
                ),
            },
        )

    def copy_for_api(self, api: str) -> "ProviderProfile":
        return ProviderProfile(
            id=self.id,
            wire_api=api,
            request_defaults=self.request_defaults,
            request_extra_body=self.request_extra_body,
            omit_params_when_thinking=tuple(self.omit_params_when_thinking),
            supports_tools=self.supports_tools,
            supports_reasoning=self.supports_reasoning,
            model_identity_policy=self.model_identity_policy,
            thinking=self.thinking,
            configured_chat_replay_fields=self.configured_chat_replay_fields,
        )


def _freeze_provider_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_provider_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_provider_value(item) for item in value)
    return value


def mutable_provider_value(value: Any) -> Any:
    """Return a detached SDK/JSON-shaped copy of immutable provider config."""

    if isinstance(value, Mapping):
        return {str(key): mutable_provider_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [mutable_provider_value(item) for item in value]
    return value
