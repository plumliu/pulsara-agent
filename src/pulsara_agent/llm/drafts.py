"""Versioned provider transport drafts consumed by :mod:`llm.runtime`.

Provider adapters are deliberately kept outside the durable event schema.  A
sanitizing transport converts their process-local output into this closed,
versioned union before LLMRuntime can observe it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulsara_agent.primitives.model_call import (
    ModelTokenUsageFact,
    ProviderSanitizedErrorFact,
    sha256_fingerprint,
)


class ProviderSemanticDraftBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["provider_transport_semantic_draft.v1"] = (
        "provider_transport_semantic_draft.v1"
    )
    transport_sequence_index: int = Field(ge=0)
    draft_fingerprint: str = Field(min_length=1)

    def _expected_fingerprint(self) -> str:
        return sha256_fingerprint(
            "provider-transport-semantic-draft:v1",
            self.model_dump(mode="json", exclude={"draft_fingerprint"}),
        )

    @model_validator(mode="after")
    def _validate_fingerprint(self) -> "ProviderSemanticDraftBase":
        if self.draft_fingerprint != self._expected_fingerprint():
            raise ValueError("provider semantic draft fingerprint mismatch")
        return self


class ProviderTextBlockStartDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["text_block_start"] = "text_block_start"
    block_id: str = Field(min_length=1, max_length=128)


class ProviderTextBlockDeltaDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["text_block_delta"] = "text_block_delta"
    block_id: str = Field(min_length=1, max_length=128)
    delta: str = Field(min_length=1)


class ProviderTextBlockEndDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["text_block_end"] = "text_block_end"
    block_id: str = Field(min_length=1, max_length=128)


class ProviderThinkingBlockStartDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["thinking_block_start"] = "thinking_block_start"
    block_id: str = Field(min_length=1, max_length=128)


class ProviderThinkingBlockDeltaDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["thinking_block_delta"] = "thinking_block_delta"
    block_id: str = Field(min_length=1, max_length=128)
    delta: str = Field(min_length=1)


class ProviderThinkingBlockEndDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["thinking_block_end"] = "thinking_block_end"
    block_id: str = Field(min_length=1, max_length=128)


class ProviderDataBlockStartDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["data_block_start"] = "data_block_start"
    block_id: str = Field(min_length=1, max_length=128)
    media_type: str = Field(min_length=1, max_length=256)


class ProviderDataBlockDeltaDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["data_block_delta"] = "data_block_delta"
    block_id: str = Field(min_length=1, max_length=128)
    media_type: str = Field(min_length=1, max_length=256)
    data: str = Field(min_length=1)


class ProviderDataBlockEndDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["data_block_end"] = "data_block_end"
    block_id: str = Field(min_length=1, max_length=128)


class ProviderToolCallStartDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["tool_call_start"] = "tool_call_start"
    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_call_name: str = Field(min_length=1, max_length=256)


class ProviderToolCallDeltaDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["tool_call_delta"] = "tool_call_delta"
    tool_call_id: str = Field(min_length=1, max_length=128)
    delta: str = Field(min_length=1)


class ProviderToolCallEndDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["tool_call_end"] = "tool_call_end"
    tool_call_id: str = Field(min_length=1, max_length=128)


class ProviderErrorDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["provider_error"] = "provider_error"
    error: ProviderSanitizedErrorFact


ProviderTransportSemanticDraft: TypeAlias = Annotated[
    ProviderTextBlockStartDraft
    | ProviderTextBlockDeltaDraft
    | ProviderTextBlockEndDraft
    | ProviderThinkingBlockStartDraft
    | ProviderThinkingBlockDeltaDraft
    | ProviderThinkingBlockEndDraft
    | ProviderDataBlockStartDraft
    | ProviderDataBlockDeltaDraft
    | ProviderDataBlockEndDraft
    | ProviderToolCallStartDraft
    | ProviderToolCallDeltaDraft
    | ProviderToolCallEndDraft
    | ProviderErrorDraft,
    Field(discriminator="draft_kind"),
]


class ProviderTransportTerminalDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["provider_transport_terminal_draft.v2"] = (
        "provider_transport_terminal_draft.v2"
    )
    outcome: Literal["completed", "provider_error"]
    usage: ModelTokenUsageFact | None
    usage_status: Literal["reported", "missing"]
    reported_model_id: str | None
    semantic_item_count: int = Field(ge=0)
    semantic_source_accumulator: str = Field(min_length=1)
    terminal_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_terminal(self) -> "ProviderTransportTerminalDraft":
        if (self.usage_status == "reported") != (self.usage is not None):
            raise ValueError("provider terminal usage status/payload mismatch")
        expected = sha256_fingerprint(
            "provider-transport-terminal-draft:v2",
            self.model_dump(mode="json", exclude={"terminal_fingerprint"}),
        )
        if self.terminal_fingerprint != expected:
            raise ValueError("provider terminal draft fingerprint mismatch")
        return self


@dataclass(frozen=True, slots=True)
class SanitizedProviderSemanticEnvelope:
    """One validated provider item awaiting coordinator adoption."""

    envelope_id: str
    draft: ProviderTransportSemanticDraft
    proposed_transport_sequence_index: int
    source_accumulator_before: str
    source_accumulator_after: str
    accepted_at_monotonic_ns: int
    adapter_source_payload_bytes: int
    counts_as_adapter_source_item: bool


ProviderTransportStreamItem: TypeAlias = (
    SanitizedProviderSemanticEnvelope | ProviderTransportTerminalDraft
)


def build_semantic_draft(draft_type: type[ProviderSemanticDraftBase], **payload: object):
    """Build a semantic draft with its canonical fingerprint."""

    provisional = draft_type.model_construct(draft_fingerprint="pending", **payload)
    canonical = provisional.model_dump(mode="json", exclude={"draft_fingerprint"})
    return draft_type(
        **canonical,
        draft_fingerprint=sha256_fingerprint(
            "provider-transport-semantic-draft:v1", canonical
        ),
    )


def build_terminal_draft(**payload: object) -> ProviderTransportTerminalDraft:
    provisional = ProviderTransportTerminalDraft.model_construct(
        terminal_fingerprint="pending", **payload
    )
    canonical = provisional.model_dump(
        mode="json", exclude={"terminal_fingerprint"}
    )
    fingerprint = sha256_fingerprint(
        "provider-transport-terminal-draft:v2",
        canonical,
    )
    return ProviderTransportTerminalDraft(
        **canonical,
        terminal_fingerprint=fingerprint,
    )


__all__ = [
    "ProviderDataBlockDeltaDraft",
    "ProviderDataBlockEndDraft",
    "ProviderDataBlockStartDraft",
    "ProviderErrorDraft",
    "ProviderSemanticDraftBase",
    "ProviderTextBlockDeltaDraft",
    "ProviderTextBlockEndDraft",
    "ProviderTextBlockStartDraft",
    "ProviderThinkingBlockDeltaDraft",
    "ProviderThinkingBlockEndDraft",
    "ProviderThinkingBlockStartDraft",
    "ProviderToolCallDeltaDraft",
    "ProviderToolCallEndDraft",
    "ProviderToolCallStartDraft",
    "ProviderTransportSemanticDraft",
    "ProviderTransportStreamItem",
    "ProviderTransportTerminalDraft",
    "SanitizedProviderSemanticEnvelope",
    "build_semantic_draft",
    "build_terminal_draft",
]
