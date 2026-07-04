"""Role-based LLM runtime."""

from __future__ import annotations

from typing import AsyncIterator

from pulsara_agent.event import AgentEvent, EventContext, ReplyEndEvent, ReplyStartEvent
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.models import ModelProfile, ModelRole
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.transport import LLMTransport


class LLMRuntime:
    def __init__(self, *, config: LLMConfig, registry: LLMTransportRegistry) -> None:
        self._config = config
        self._registry = registry

    def stream(
        self,
        *,
        role: ModelRole,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        model = self._config.model_for(role)
        transport = self._registry.get(model.api)
        return self._stream_reply(
            transport=transport,
            model=model,
            context=context,
            event_context=event_context,
            options=options,
        )

    async def _stream_reply(
        self,
        *,
        transport: LLMTransport,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None,
    ) -> AsyncIterator[AgentEvent]:
        yield ReplyStartEvent(**event_context.event_fields(), name="assistant")
        async for event in transport.stream(
            model=model,
            context=context,
            event_context=event_context,
            options=options,
        ):
            yield event
        yield ReplyEndEvent(**event_context.event_fields())
