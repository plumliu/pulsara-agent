"""Test adapter for process-local provider stream items."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from uuid import uuid4

from pulsara_agent.event import EventContext
from pulsara_agent.llm.raw_provider import (
    RawProviderBlockEnd,
    RawProviderBlockStart,
    RawProviderStreamItem,
    RawProviderTextDelta,
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
    ) -> AsyncIterator[RawProviderStreamItem]:
        del call, context, event_context
        block_id = f"text:{uuid4()}"
        yield RawProviderBlockStart(block_kind="text", block_id=block_id)
        yield RawProviderTextDelta(block_id=block_id, delta=self.text)
        yield RawProviderBlockEnd(block_kind="text", block_id=block_id)
