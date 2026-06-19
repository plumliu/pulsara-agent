"""Convenience constructors for Pulsara LLM runtime."""

from __future__ import annotations

from pulsara_agent.llm.adapters.openai.chat_completions import OpenAIChatCompletionsTransport
from pulsara_agent.llm.adapters.openai.responses import OpenAIResponsesTransport
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.runtime import LLMRuntime


def build_llm_runtime(config: LLMConfig) -> LLMRuntime:
    """Build the default MVP runtime from user-facing config.

    The default runtime registers both OpenAI wire formats. ``ModelProfile.api``
    selects the concrete transport for each role.
    """

    registry = LLMTransportRegistry()
    registry.register(
        OpenAIResponsesTransport(
            api_key=config.api_key,
            retry_config=config.retry,
            openai_sdk_max_retries=config.openai_sdk_max_retries,
        )
    )
    registry.register(
        OpenAIChatCompletionsTransport(
            api_key=config.api_key,
            retry_config=config.retry,
            openai_sdk_max_retries=config.openai_sdk_max_retries,
        )
    )
    return LLMRuntime(config=config, registry=registry)
