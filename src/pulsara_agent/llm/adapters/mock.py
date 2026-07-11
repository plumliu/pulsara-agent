"""Test adapter for Pulsara LLM events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall


@dataclass(frozen=True, slots=True)
class MockTransport:
    text: str
    api: str = "mock"
    binding_id: str = "test.mock"
    contract_version: str = "v1"

    async def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        block_id = f"text:{uuid4()}"
        yield TextBlockStartEvent(**event_context.event_fields(), block_id=block_id)
        yield TextBlockDeltaEvent(
            **event_context.event_fields(),
            block_id=block_id,
            delta=self.text,
        )
        yield TextBlockEndEvent(**event_context.event_fields(), block_id=block_id)
