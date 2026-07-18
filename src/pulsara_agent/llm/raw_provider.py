"""Adapter-private provider stream items.

These values are process-local transport input. They are deliberately not
AgentEvent subclasses and must never enter the durable serialization registry.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Annotated,
    AsyncIterator,
    Literal,
    Protocol,
    TypeAlias,
    TypeGuard,
)

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulsara_agent.event import EventContext
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.primitives.model_call import ProviderRetrySummaryFact

if TYPE_CHECKING:
    from pulsara_agent.llm.request import LLMContext
    from pulsara_agent.llm.resolution import ResolvedModelCall


class RawProviderItemBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RawProviderBlockStart(RawProviderItemBase):
    raw_kind: Literal["block_start"] = "block_start"
    block_kind: Literal["text", "thinking", "data", "tool_call"]
    block_id: str = Field(min_length=1, max_length=128)
    media_type: str | None = Field(default=None, max_length=256)
    tool_call_name: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> "RawProviderBlockStart":
        if (self.block_kind == "data") != (self.media_type is not None):
            raise ValueError("raw data block start requires exactly one media type")
        if self.block_kind == "tool_call":
            if not self.tool_call_name:
                raise ValueError("raw tool-call start requires a non-empty tool name")
        elif self.tool_call_name is not None:
            raise ValueError("non-tool raw block cannot carry a tool name")
        if self.media_type is not None and not self.media_type:
            raise ValueError("raw data media type must be non-empty")
        return self


class RawProviderTextDelta(RawProviderItemBase):
    raw_kind: Literal["text_delta"] = "text_delta"
    block_id: str = Field(min_length=1, max_length=128)
    delta: str = Field(min_length=1)


class RawProviderThinkingDelta(RawProviderItemBase):
    raw_kind: Literal["thinking_delta"] = "thinking_delta"
    block_id: str = Field(min_length=1, max_length=128)
    delta: str = Field(min_length=1)


class RawProviderDataDelta(RawProviderItemBase):
    raw_kind: Literal["data_delta"] = "data_delta"
    block_id: str = Field(min_length=1, max_length=128)
    media_type: str = Field(min_length=1, max_length=256)
    data: str = Field(min_length=1)


class RawProviderToolCallDelta(RawProviderItemBase):
    raw_kind: Literal["tool_call_delta"] = "tool_call_delta"
    tool_call_id: str = Field(min_length=1, max_length=128)
    delta: str = Field(min_length=1)


class RawProviderBlockEnd(RawProviderItemBase):
    raw_kind: Literal["block_end"] = "block_end"
    block_kind: Literal["text", "thinking", "data", "tool_call"]
    block_id: str = Field(min_length=1, max_length=128)


class RawProviderFailure(RawProviderItemBase):
    raw_kind: Literal["failure"] = "failure"
    message: str = Field(min_length=1)
    code_hint: str | None = None
    retry_summary: ProviderRetrySummaryFact | None = None


RawProviderStreamItem: TypeAlias = Annotated[
    RawProviderBlockStart
    | RawProviderTextDelta
    | RawProviderThinkingDelta
    | RawProviderDataDelta
    | RawProviderToolCallDelta
    | RawProviderBlockEnd
    | RawProviderFailure,
    Field(discriminator="raw_kind"),
]

_RAW_PROVIDER_ITEM_TYPES = (
    RawProviderBlockStart,
    RawProviderTextDelta,
    RawProviderThinkingDelta,
    RawProviderDataDelta,
    RawProviderToolCallDelta,
    RawProviderBlockEnd,
    RawProviderFailure,
)


def is_raw_provider_stream_item(value: object) -> TypeGuard[RawProviderStreamItem]:
    return isinstance(value, _RAW_PROVIDER_ITEM_TYPES)


class RawLLMTransport(Protocol):
    api: str
    binding_id: str
    contract_version: str

    def stream(
        self,
        *,
        call: "ResolvedModelCall",
        context: "LLMContext",
        event_context: EventContext,
    ) -> AsyncIterator[RawProviderStreamItem | TransportUsageReport]: ...


__all__ = [
    "RawProviderBlockEnd",
    "RawProviderBlockStart",
    "RawProviderDataDelta",
    "RawProviderFailure",
    "RawLLMTransport",
    "RawProviderStreamItem",
    "RawProviderTextDelta",
    "RawProviderThinkingDelta",
    "RawProviderToolCallDelta",
    "is_raw_provider_stream_item",
]
