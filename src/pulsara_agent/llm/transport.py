"""Public, secret-safe LLM transport protocol."""

from __future__ import annotations

from typing import Protocol

from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall

if False:  # pragma: no cover - typing-only cycle breaker
    from pulsara_agent.llm.sanitizing_transport import (
        SanitizingProviderTransportExecution,
    )


class LLMTransport(Protocol):
    api: str
    binding_id: str
    contract_version: str

    sanitizer_contract_fingerprint: str
    boundary_contract_fingerprint: str

    def open_stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
    ) -> "SanitizingProviderTransportExecution":
        """Create a synchronous, registered secret-safe execution owner."""
