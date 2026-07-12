"""LLM transport protocol."""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from pulsara_agent.event import AgentEvent, EventContext
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.result import TransportUsageReport


class LLMTransport(Protocol):
    api: str
    binding_id: str
    contract_version: str

    def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent | TransportUsageReport]:
        """Translate one provider turn into Pulsara Agent events."""
