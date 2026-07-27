"""Process-local receipts joining current ToolResult replay authority."""

from __future__ import annotations

from pydantic import Field, model_validator

from pulsara_agent.message.blocks import ToolResultBlock
from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    context_fingerprint,
)
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.primitives.runtime_event_vocabulary import (
    MAX_TOOL_RESULT_RECEIPT_ITEMS,
    ordered_fingerprint_accumulator,
)


class CurrentToolResultReceiptItem(FrozenRuntimeStateBase):
    result_block: ToolResultBlock
    tool_result_end_reference: ContextEventReferenceFact
    terminal_projection_reference: ContextEventReferenceFact
    tool_call_id: str
    result_semantic_fingerprint: str
    item_fingerprint: str

    @model_validator(mode="after")
    def _identity(self) -> "CurrentToolResultReceiptItem":
        if (
            self.result_block.id != self.tool_call_id
            or self.tool_result_end_reference.event_type != "TOOL_RESULT_END"
            or self.terminal_projection_reference.event_type
            != "TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED"
        ):
            raise ValueError("current ToolResult receipt identity mismatch")
        if (
            self.tool_result_end_reference.runtime_session_id
            != self.terminal_projection_reference.runtime_session_id
            or self.terminal_projection_reference.sequence
            >= self.tool_result_end_reference.sequence
        ):
            raise ValueError("current ToolResult receipt reference ordering mismatch")
        expected = context_fingerprint(
            "current-tool-result-receipt-item:v1",
            self.model_dump(mode="json", exclude={"item_fingerprint"}),
        )
        if self.item_fingerprint != expected:
            raise ValueError("current ToolResult receipt fingerprint mismatch")
        return self


class CurrentToolResultBatchReceipt(FrozenRuntimeStateBase):
    ordered_items: tuple[CurrentToolResultReceiptItem, ...] = Field(
        min_length=1,
        max_length=MAX_TOOL_RESULT_RECEIPT_ITEMS,
    )
    ordered_item_fingerprints_accumulator: str

    @model_validator(mode="after")
    def _ordered(self) -> "CurrentToolResultBatchReceipt":
        expected = ordered_fingerprint_accumulator(
            "current-tool-result-batch:v1",
            tuple(item.item_fingerprint for item in self.ordered_items),
        )
        if self.ordered_item_fingerprints_accumulator != expected:
            raise ValueError("current ToolResult batch accumulator mismatch")
        call_ids = tuple(item.tool_call_id for item in self.ordered_items)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("current ToolResult batch contains duplicate calls")
        return self


__all__ = ["CurrentToolResultBatchReceipt", "CurrentToolResultReceiptItem"]
