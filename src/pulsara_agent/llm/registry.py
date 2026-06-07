"""Transport registry keyed by provider API format."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.llm.transport import LLMTransport


@dataclass(slots=True)
class LLMTransportRegistry:
    _transports: dict[str, LLMTransport] = field(default_factory=dict)

    def register(self, transport: LLMTransport) -> None:
        if transport.api in self._transports:
            raise ValueError(f"LLM transport already registered: {transport.api}")
        self._transports[transport.api] = transport

    def get(self, api: str) -> LLMTransport:
        try:
            return self._transports[api]
        except KeyError as exc:
            raise KeyError(f"No LLM transport registered for api: {api}") from exc

    def apis(self) -> list[str]:
        return sorted(self._transports)
