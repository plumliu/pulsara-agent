"""Provider-profile configuration for OpenAI-compatible LLM APIs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pulsara_agent.llm.provider_replay import (
    ProviderAssistantReplayCodecKind,
    provider_replay_contract_fingerprint,
)
from pulsara_agent.primitives.context import context_fingerprint


class ThinkingReplayPolicy(StrEnum):
    """How a chat provider expects previous assistant thinking to be replayed."""

    NEVER = "never"
    WHEN_TOOL_CALLS = "when_tool_calls"
    ALWAYS = "always"


class ProviderReasoningReplayScope(StrEnum):
    NEVER = "NEVER"
    TOOL_RESPONSES = "TOOL_RESPONSES"
    ALL_COMPLETED_RESPONSES = "ALL_COMPLETED_RESPONSES"


class ProviderChatFieldAccumulationMode(StrEnum):
    TEXT_CONCAT = "TEXT_CONCAT"
    ORDERED_ARRAY_APPEND = "ORDERED_ARRAY_APPEND"


@dataclass(frozen=True, slots=True)
class ProviderChatReplayFieldContract:
    field_name: str
    accumulation_mode: ProviderChatFieldAccumulationMode
    required_on_selected_response: bool = False
    final_value_required: bool = False

    def __post_init__(self) -> None:
        if not self.field_name or self.field_name != self.field_name.strip():
            raise ValueError("chat replay field name is invalid")


# This registry describes wire shapes, not providers.  An OpenAI-compatible
# Chat endpoint may emit any subset and Pulsara replays only the fields that
# were actually observed in one completed response.  Native non-OpenAI wire
# protocols are deliberately outside this contract.
CHAT_CLOSED_REASONING_FIELD_CONTRACTS = (
    ProviderChatReplayFieldContract(
        "reasoning_content", ProviderChatFieldAccumulationMode.TEXT_CONCAT
    ),
    ProviderChatReplayFieldContract(
        "reasoning", ProviderChatFieldAccumulationMode.TEXT_CONCAT
    ),
    ProviderChatReplayFieldContract(
        "reasoning_details",
        ProviderChatFieldAccumulationMode.ORDERED_ARRAY_APPEND,
    ),
)
_CHAT_TEXT_REASONING_FIELD_NAMES = frozenset({"reasoning_content", "reasoning"})
_CHAT_CLOSED_REASONING_FIELD_BY_NAME = MappingProxyType(
    {item.field_name: item for item in CHAT_CLOSED_REASONING_FIELD_CONTRACTS}
)


class ModelIdentityPolicy(StrEnum):
    """How provider-reported model identities relate to the requested route id."""

    ACCEPT_REPORTED = "accept_reported"
    EXACT = "exact"


@dataclass(frozen=True, slots=True)
class ThinkingProfile:
    """Provider-neutral description of thinking/reasoning wire fields."""

    enabled: bool = False
    delta_fields: tuple[str, ...] = ("reasoning_content", "reasoning")
    message_field: str | None = "reasoning_content"
    replay_policy: ThinkingReplayPolicy = ThinkingReplayPolicy.NEVER

    def __post_init__(self) -> None:
        if len(self.delta_fields) != len(set(self.delta_fields)) or any(
            field_name not in _CHAT_TEXT_REASONING_FIELD_NAMES
            for field_name in self.delta_fields
        ):
            raise ValueError("live Chat thinking fields are outside the closed registry")
        if self.message_field is not None and (
            self.message_field not in _CHAT_TEXT_REASONING_FIELD_NAMES
        ):
            raise ValueError("legacy Chat thinking message field must be textual")


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
        configured_names = tuple(
            item.field_name for item in self.configured_chat_replay_fields
        )
        if len(configured_names) != len(set(configured_names)):
            raise ValueError("chat replay field overrides are duplicated")
        if self.configured_chat_replay_fields and self.wire_api != (
            "openai_chat_completions"
        ):
            raise ValueError("chat replay field overrides require Chat Completions")
        for item in self.configured_chat_replay_fields:
            frozen = _CHAT_CLOSED_REASONING_FIELD_BY_NAME.get(item.field_name)
            if frozen is None or frozen.accumulation_mode is not item.accumulation_mode:
                raise ValueError("chat replay field override is outside the closed registry")

        fields = self.chat_replay_fields
        names = tuple(item.field_name for item in fields)
        if len(names) != len(set(names)):
            raise ValueError("chat replay field contracts are duplicated")
        codec = self.assistant_replay_codec_kind
        if codec is ProviderAssistantReplayCodecKind.CHAT_CLOSED_REASONING_FIELDS:
            if self.wire_api != "openai_chat_completions" or names != tuple(
                item.field_name for item in CHAT_CLOSED_REASONING_FIELD_CONTRACTS
            ):
                raise ValueError("closed Chat replay registry is invalid")

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
        if self.wire_api == "openai_chat_completions":
            return ProviderAssistantReplayCodecKind.CHAT_CLOSED_REASONING_FIELDS
        return ProviderAssistantReplayCodecKind.NONE

    @property
    def chat_replay_fields(self) -> tuple[ProviderChatReplayFieldContract, ...]:
        if self.wire_api != "openai_chat_completions":
            return ()
        overrides = {
            item.field_name: item for item in self.configured_chat_replay_fields
        }
        return tuple(
            overrides.get(item.field_name, item)
            for item in CHAT_CLOSED_REASONING_FIELD_CONTRACTS
        )

    @property
    def assistant_replay_contract_fingerprint(self) -> str:
        codec = self.assistant_replay_codec_kind
        if codec is ProviderAssistantReplayCodecKind.NONE:
            return context_fingerprint(
                "pulsara.provider-replay-contract:unsupported:v1", self.wire_api
            )
        return provider_replay_contract_fingerprint(codec)

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
