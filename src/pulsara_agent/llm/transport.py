"""LLM transport protocol."""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from pulsara_agent.event import AgentEvent, EventContext
from pulsara_agent.llm.models import ModelProfile
from pulsara_agent.llm.request import LLMContext, LLMOptions


class LLMTransport(Protocol):
    api: str

    def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Translate one provider turn into Pulsara Agent events."""
