"""Single process-local provider stream boundary used by the Stage 2 kernel.

Adapters decode vendor SDK values inside their own call stack and emit only
the exact provider subset of the formal live-event payload vocabulary, a
typed failure signal, or usage.  The Runtime-owned normalized transport turns
that adapter stream into payloads plus one terminal result.  There is no
per-item envelope, adoption acknowledgement, or second draft vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, AsyncIterator, Protocol, TypeAlias

from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.ports.live_agent_event import ProviderStreamPayload
from pulsara_agent.primitives.model_call import (
    ProviderRetrySummaryFact,
    ProviderSanitizedErrorFact,
)

if TYPE_CHECKING:
    from pulsara_agent.llm.request import LLMContext
    from pulsara_agent.llm.resolution import ResolvedModelCall


@dataclass(frozen=True, slots=True)
class ProviderStreamFailure:
    message: str
    code_hint: str | None = None
    retry_summary: ProviderRetrySummaryFact | None = None

    def __post_init__(self) -> None:
        if not self.message:
            raise ValueError("provider failure message is empty")


ProviderAdapterStreamItem: TypeAlias = (
    ProviderStreamPayload | ProviderStreamFailure | TransportUsageReport
)


class ProviderAdapterTransport(Protocol):
    api: str
    binding_id: str
    contract_version: str

    def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
    ) -> AsyncIterator[ProviderAdapterStreamItem]: ...


@dataclass(frozen=True, slots=True)
class ProviderStreamTerminal:
    outcome: str
    usage: TransportUsageReport
    error: ProviderSanitizedErrorFact | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"COMPLETED", "PROVIDER_ERROR"}:
            raise ValueError("provider stream terminal outcome is unknown")
        if (self.outcome == "PROVIDER_ERROR") != (self.error is not None):
            raise ValueError("provider stream terminal error union is invalid")


ProviderNormalizedStreamItem: TypeAlias = ProviderStreamPayload | ProviderStreamTerminal


class ProviderPhysicalCompletionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class ProviderPhysicalCompletion:
    status: ProviderPhysicalCompletionStatus
    diagnostic_code: str | None


__all__ = [
    "ProviderAdapterStreamItem",
    "ProviderAdapterTransport",
    "ProviderNormalizedStreamItem",
    "ProviderPhysicalCompletion",
    "ProviderPhysicalCompletionStatus",
    "ProviderStreamFailure",
    "ProviderStreamTerminal",
]
