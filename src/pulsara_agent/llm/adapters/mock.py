"""Test adapter for Pulsara LLM events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
)
from pulsara_agent.llm.models import ModelProfile
from pulsara_agent.llm.request import LLMContext, LLMOptions


@dataclass(frozen=True, slots=True)
class MockTransport:
    text: str
    api: str = "mock"

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        block_id = f"text:{uuid4()}"
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
        )
        yield TextBlockStartEvent(**event_context.event_fields(), block_id=block_id)
        yield TextBlockDeltaEvent(
            **event_context.event_fields(),
            block_id=block_id,
            delta=self.text,
        )
        yield TextBlockEndEvent(**event_context.event_fields(), block_id=block_id)
        yield ModelCallEndEvent(
            **event_context.event_fields(),
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
        )
