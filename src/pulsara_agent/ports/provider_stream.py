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

from pulsara_agent.llm.provider import ProviderAssistantReplayCodecKind
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.ports.live_agent_event import ProviderStreamPayload
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    thaw_json,
)
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


class ProviderOutputIncompleteReason(StrEnum):
    OUTPUT_TOKEN_LIMIT = "OUTPUT_TOKEN_LIMIT"
    CONTEXT_WINDOW_LIMIT_DURING_GENERATION = (
        "CONTEXT_WINDOW_LIMIT_DURING_GENERATION"
    )
    CONTENT_FILTERED = "CONTENT_FILTERED"
    UNKNOWN_PROVIDER_INCOMPLETE = "UNKNOWN_PROVIDER_INCOMPLETE"


class ProviderAdapterTerminalKind(StrEnum):
    COMPLETED = "COMPLETED"
    OUTPUT_INCOMPLETE = "OUTPUT_INCOMPLETE"


@dataclass(frozen=True, slots=True)
class ProviderAdapterCompletedReplayPayload:
    """Bounded, provider-shaped replay payload produced only at COMPLETED."""

    codec_kind: ProviderAssistantReplayCodecKind
    ordered_items: tuple[FrozenJsonObjectFact, ...]
    logical_utf8_bytes: int
    local_fingerprint: str

    def __post_init__(self) -> None:
        if self.codec_kind is ProviderAssistantReplayCodecKind.NONE:
            raise ValueError("completed replay payload cannot use the NONE codec")
        if not self.ordered_items:
            raise ValueError("completed replay payload is empty")
        logical = sum(
            len(canonical_json_bytes(thaw_json(item))) for item in self.ordered_items
        )
        if logical != self.logical_utf8_bytes or logical > (16 << 20):
            raise ValueError("completed replay payload size is invalid")
        expected = context_fingerprint(
            "pulsara.provider-adapter-completed-replay:v1",
            {
                "codec": self.codec_kind.value,
                "items": tuple(thaw_json(item) for item in self.ordered_items),
                "bytes": logical,
            },
        )
        if self.local_fingerprint != expected:
            raise ValueError("completed replay payload fingerprint mismatch")


def freeze_provider_adapter_completed_replay_payload(
    *,
    codec_kind: ProviderAssistantReplayCodecKind,
    ordered_items: tuple[FrozenJsonObjectFact, ...],
) -> ProviderAdapterCompletedReplayPayload:
    logical = sum(len(canonical_json_bytes(thaw_json(item))) for item in ordered_items)
    fingerprint = context_fingerprint(
        "pulsara.provider-adapter-completed-replay:v1",
        {
            "codec": codec_kind.value,
            "items": tuple(thaw_json(item) for item in ordered_items),
            "bytes": logical,
        },
    )
    return ProviderAdapterCompletedReplayPayload(
        codec_kind=codec_kind,
        ordered_items=ordered_items,
        logical_utf8_bytes=logical,
        local_fingerprint=fingerprint,
    )


@dataclass(frozen=True, slots=True)
class ProviderAdapterTerminal:
    terminal_kind: ProviderAdapterTerminalKind
    incomplete_reason: ProviderOutputIncompleteReason | None = None
    completed_replay_payload: ProviderAdapterCompletedReplayPayload | None = None

    def __post_init__(self) -> None:
        completed = self.terminal_kind is ProviderAdapterTerminalKind.COMPLETED
        if completed == (self.incomplete_reason is not None):
            raise ValueError("provider adapter terminal reason union is invalid")
        if not completed and self.completed_replay_payload is not None:
            raise ValueError("incomplete provider terminal owns a replay payload")


ProviderAdapterStreamItem: TypeAlias = (
    ProviderStreamPayload
    | ProviderStreamFailure
    | TransportUsageReport
    | ProviderAdapterTerminal
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


class ProviderNormalizedTerminalKind(StrEnum):
    COMPLETED = "COMPLETED"
    OUTPUT_INCOMPLETE = "OUTPUT_INCOMPLETE"
    PROVIDER_ERROR = "PROVIDER_ERROR"


@dataclass(frozen=True, slots=True)
class ProviderStreamTerminal:
    terminal_kind: ProviderNormalizedTerminalKind
    usage: TransportUsageReport
    incomplete_reason: ProviderOutputIncompleteReason | None = None
    error: ProviderSanitizedErrorFact | None = None
    completed_replay_payload: ProviderAdapterCompletedReplayPayload | None = None

    def __post_init__(self) -> None:
        incomplete = self.terminal_kind is ProviderNormalizedTerminalKind.OUTPUT_INCOMPLETE
        failed = self.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
        if incomplete != (self.incomplete_reason is not None):
            raise ValueError("provider stream incomplete union is invalid")
        if failed != (self.error is not None):
            raise ValueError("provider stream terminal error union is invalid")
        if self.terminal_kind is not ProviderNormalizedTerminalKind.COMPLETED and (
            self.completed_replay_payload is not None
        ):
            raise ValueError("non-completed terminal owns a replay payload")

    @property
    def outcome(self) -> str:
        """Compatibility view; callers must branch on the closed enum."""

        return self.terminal_kind.value


class ProviderModelOutputIncomplete(RuntimeError):
    def __init__(self, reason: ProviderOutputIncompleteReason) -> None:
        self.reason = reason
        super().__init__(f"provider model output incomplete: {reason.value}")


class ProviderModelExecutionFailed(RuntimeError):
    def __init__(self, error: ProviderSanitizedErrorFact) -> None:
        self.error = error
        super().__init__(f"provider model execution failed: {error.code.value}")


ProviderNormalizedStreamItem: TypeAlias = ProviderStreamPayload | ProviderStreamTerminal


class ProviderPhysicalCompletionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class ProviderPhysicalCompletion:
    status: ProviderPhysicalCompletionStatus
    diagnostic_code: str | None


__all__ = [
    "ProviderAdapterCompletedReplayPayload",
    "ProviderAdapterStreamItem",
    "ProviderAdapterTerminal",
    "ProviderAdapterTerminalKind",
    "ProviderAdapterTransport",
    "ProviderModelExecutionFailed",
    "ProviderModelOutputIncomplete",
    "ProviderNormalizedStreamItem",
    "ProviderNormalizedTerminalKind",
    "ProviderOutputIncompleteReason",
    "ProviderPhysicalCompletion",
    "ProviderPhysicalCompletionStatus",
    "ProviderStreamFailure",
    "ProviderStreamTerminal",
    "freeze_provider_adapter_completed_replay_payload",
]
