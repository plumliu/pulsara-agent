"""Provider-profile configuration for OpenAI-compatible LLM APIs."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ThinkingReplayPolicy(StrEnum):
    """How a chat provider expects previous assistant thinking to be replayed."""

    NEVER = "never"
    WHEN_TOOL_CALLS = "when_tool_calls"
    ALWAYS = "always"


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
    request_defaults: dict[str, Any] = field(default_factory=dict)
    request_extra_body: dict[str, Any] = field(default_factory=dict)
    omit_params_when_thinking: tuple[str, ...] = field(default_factory=tuple)
    supports_tools: bool = True
    supports_reasoning: bool = True
    thinking: ThinkingProfile = field(default_factory=ThinkingProfile)

    def copy_for_api(self, api: str) -> "ProviderProfile":
        return ProviderProfile(
            id=self.id,
            wire_api=api,
            request_defaults=dict(self.request_defaults),
            request_extra_body=dict(self.request_extra_body),
            omit_params_when_thinking=tuple(self.omit_params_when_thinking),
            supports_tools=self.supports_tools,
            supports_reasoning=self.supports_reasoning,
            thinking=self.thinking,
        )
