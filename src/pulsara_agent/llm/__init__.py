"""LLM public facade.

The Stage 2 kernel imports the small provider/model contract modules directly.
Keep the legacy ``LLMRuntime`` facade available until its Stage 3 deletion, but
do not make importing any ``pulsara_agent.llm.*`` submodule eagerly load the
old RawProvider/draft/EventLog execution graph.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "EventContext": ("pulsara_agent.event", "EventContext"),
    "LLMConfig": ("pulsara_agent.llm.config", "LLMConfig"),
    "ModelSlotConfig": ("pulsara_agent.llm.config", "ModelSlotConfig"),
    "build_llm_runtime": ("pulsara_agent.llm.factory", "build_llm_runtime"),
    "LLMMessage": ("pulsara_agent.llm.input", "LLMMessage"),
    "LLMToolCall": ("pulsara_agent.llm.input", "LLMToolCall"),
    "MessageRole": ("pulsara_agent.llm.input", "MessageRole"),
    "ToolSpec": ("pulsara_agent.llm.input", "ToolSpec"),
    "ModelProfile": ("pulsara_agent.llm.models", "ModelProfile"),
    "ModelRole": ("pulsara_agent.llm.models", "ModelRole"),
    "ModelIdentityPolicy": (
        "pulsara_agent.llm.provider",
        "ModelIdentityPolicy",
    ),
    "ProviderProfile": ("pulsara_agent.llm.provider", "ProviderProfile"),
    "ThinkingProfile": ("pulsara_agent.llm.provider", "ThinkingProfile"),
    "ThinkingReplayPolicy": (
        "pulsara_agent.llm.provider",
        "ThinkingReplayPolicy",
    ),
    "LLMRetryConfig": ("pulsara_agent.llm.retry", "LLMRetryConfig"),
    "LLMRuntime": ("pulsara_agent.llm.runtime", "LLMRuntime"),
    "ResolvedModelCall": (
        "pulsara_agent.llm.resolution",
        "ResolvedModelCall",
    ),
    "ResolvedModelTarget": (
        "pulsara_agent.llm.resolution",
        "ResolvedModelTarget",
    ),
    "ModelCallPurpose": (
        "pulsara_agent.primitives.model_call",
        "ModelCallPurpose",
    ),
    "ModelContextLimits": (
        "pulsara_agent.primitives.model_call",
        "ModelContextLimits",
    ),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "EventContext",
    "LLMConfig",
    "LLMRetryConfig",
    "LLMMessage",
    "LLMToolCall",
    "LLMRuntime",
    "MessageRole",
    "ModelProfile",
    "ModelCallPurpose",
    "ModelContextLimits",
    "ModelIdentityPolicy",
    "ModelRole",
    "ModelSlotConfig",
    "ProviderProfile",
    "ResolvedModelCall",
    "ResolvedModelTarget",
    "ThinkingProfile",
    "ThinkingReplayPolicy",
    "ToolSpec",
    "build_llm_runtime",
]
