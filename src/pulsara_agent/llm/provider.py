"""Provider-profile configuration for OpenAI-compatible LLM APIs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ThinkingReplayPolicy(StrEnum):
    """How a chat provider expects previous assistant thinking to be replayed."""

    NEVER = "never"
    WHEN_TOOL_CALLS = "when_tool_calls"
    ALWAYS = "always"


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

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_defaults", _freeze_provider_value(self.request_defaults)
        )
        object.__setattr__(
            self,
            "request_extra_body",
            _freeze_provider_value(self.request_extra_body),
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
