"""Convenience constructors for Pulsara LLM runtime."""

from __future__ import annotations

from pulsara_agent.llm.adapters.openai.responses import OpenAIResponsesTransport
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.runtime import LLMRuntime


def build_llm_runtime(config: LLMConfig) -> LLMRuntime:
    """Build the default MVP runtime from user-facing config.

    The MVP only registers the OpenAI Responses-compatible adapter. Additional
    provider wire formats should add their own adapter package and be registered
    here or by a higher-level application bootstrapper.
    """

    registry = LLMTransportRegistry()
    registry.register(OpenAIResponsesTransport(api_key=config.api_key))
    return LLMRuntime(config=config, registry=registry)
