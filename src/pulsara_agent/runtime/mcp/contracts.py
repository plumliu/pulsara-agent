"""Process-local MCP SDK generation and raw result carriers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Mapping

from pulsara_agent.primitives._context_base import (
    FrozenJsonObjectFact,
    FrozenJsonValue,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.primitives.mcp_protocol import (
    McpFinalDiscoverWireReceiptFact,
    McpLegacyInitializeWireReceiptFact,
    McpNegotiationWireReceiptFact,
    McpServerProtocolSemanticFact,
)


@dataclass(frozen=True, slots=True)
class McpSdkNegotiatedProtocolBinding:
    sdk_client_generation_id: str
    transport_generation: int
    client: object
    negotiation_wire_receipt: McpNegotiationWireReceiptFact
    protocol_semantic: McpServerProtocolSemanticFact
    client_capability_policy_fingerprint: str

    def __post_init__(self) -> None:
        receipt = self.negotiation_wire_receipt
        if receipt.sdk_client_generation_id != self.sdk_client_generation_id:
            raise ValueError("MCP negotiation receipt generation mismatch")
        if receipt.exact_protocol_revision != self.protocol_semantic.protocol_revision:
            raise ValueError("MCP negotiation receipt protocol mismatch")
        if (
            receipt.client_capability_policy_fingerprint
            != self.client_capability_policy_fingerprint
        ):
            raise ValueError("MCP negotiation capability policy mismatch")
        era = self.protocol_semantic.behavior_era.value
        if era == "stateless_per_request" and not isinstance(
            receipt, McpFinalDiscoverWireReceiptFact
        ):
            raise ValueError("stateless MCP binding requires final discover receipt")
        if era == "handshake_sessionful" and not isinstance(
            receipt, McpLegacyInitializeWireReceiptFact
        ):
            raise ValueError("handshake MCP binding requires initialize receipt")


@dataclass(frozen=True, slots=True)
class McpSdkProtocolBinding(McpSdkNegotiatedProtocolBinding):
    complete_listing_accumulator: str

    def __post_init__(self) -> None:
        McpSdkNegotiatedProtocolBinding.__post_init__(self)
        if not self.complete_listing_accumulator:
            raise ValueError("MCP protocol binding requires complete listing authority")


class McpSdkConcurrencyMode(StrEnum):
    BOUNDED_PARALLEL = "bounded_parallel"
    SERIALIZED = "serialized"


@dataclass(frozen=True, slots=True)
class McpSdkConformedClientGeneration:
    generation_id: str
    sdk_protocol_binding: McpSdkProtocolBinding
    final_negotiation_wire_receipt: McpNegotiationWireReceiptFact
    client: object
    snapshot_id: str
    snapshot_semantic_fingerprint: str
    snapshot_authority_fingerprint: str
    complete_tool_listing_accumulator: str
    ordered_tool_attribution_fingerprints: tuple[str, ...]
    accepting_operations: bool

    def __post_init__(self) -> None:
        if self.sdk_protocol_binding.sdk_client_generation_id != self.generation_id:
            raise ValueError("MCP client generation/binding mismatch")
        if self.sdk_protocol_binding.client is not self.client:
            raise ValueError("MCP client generation must own the bound SDK client")
        if (
            self.sdk_protocol_binding.negotiation_wire_receipt
            is not self.final_negotiation_wire_receipt
        ):
            raise ValueError("MCP final negotiation receipt must have one owner")
        if tuple(sorted(self.ordered_tool_attribution_fingerprints)) != (
            self.ordered_tool_attribution_fingerprints
        ):
            raise ValueError("MCP tool attribution fingerprints must be ordered")
        if (
            self.sdk_protocol_binding.complete_listing_accumulator
            != self.complete_tool_listing_accumulator
        ):
            raise ValueError("MCP client generation listing authority mismatch")
        if not self.complete_tool_listing_accumulator:
            raise ValueError("MCP client generation requires a complete listing")
        if (
            not self.snapshot_id
            or not self.snapshot_semantic_fingerprint
            or not self.snapshot_authority_fingerprint
        ):
            raise ValueError("MCP client generation requires exact snapshot authority")


@dataclass(frozen=True, slots=True)
class McpFreshnessRevalidationReceipt:
    physical_operation_id: str
    server_id: str
    sdk_client_generation_id: str
    installed_snapshot_id: str
    installed_snapshot_semantic_fingerprint: str
    installed_snapshot_authority_fingerprint: str
    refreshed_snapshot_id: str
    refreshed_snapshot_semantic_fingerprint: str
    refreshed_snapshot_authority_fingerprint: str
    refreshed_page_set_accumulator: str
    request_count: int
    page_count: int
    observed_at_utc: str
    receipt_fingerprint: str

    def __post_init__(self) -> None:
        required = (
            self.physical_operation_id,
            self.server_id,
            self.sdk_client_generation_id,
            self.installed_snapshot_id,
            self.installed_snapshot_semantic_fingerprint,
            self.installed_snapshot_authority_fingerprint,
            self.refreshed_snapshot_id,
            self.refreshed_snapshot_semantic_fingerprint,
            self.refreshed_snapshot_authority_fingerprint,
            self.refreshed_page_set_accumulator,
            self.observed_at_utc,
        )
        if any(not value for value in required):
            raise ValueError("MCP freshness receipt identity is incomplete")
        if self.request_count < 0 or self.page_count < 0:
            raise ValueError("MCP freshness receipt counts must be non-negative")
        if self.installed_snapshot_id != self.refreshed_snapshot_id:
            raise ValueError("MCP freshness receipt snapshot identity changed")
        if (
            self.installed_snapshot_semantic_fingerprint
            != self.refreshed_snapshot_semantic_fingerprint
        ):
            raise ValueError("MCP freshness receipt surface semantic changed")
        payload = {
            "physical_operation_id": self.physical_operation_id,
            "server_id": self.server_id,
            "sdk_client_generation_id": self.sdk_client_generation_id,
            "installed_snapshot_id": self.installed_snapshot_id,
            "installed_snapshot_semantic_fingerprint": (
                self.installed_snapshot_semantic_fingerprint
            ),
            "installed_snapshot_authority_fingerprint": (
                self.installed_snapshot_authority_fingerprint
            ),
            "refreshed_snapshot_id": self.refreshed_snapshot_id,
            "refreshed_snapshot_semantic_fingerprint": (
                self.refreshed_snapshot_semantic_fingerprint
            ),
            "refreshed_snapshot_authority_fingerprint": (
                self.refreshed_snapshot_authority_fingerprint
            ),
            "refreshed_page_set_accumulator": self.refreshed_page_set_accumulator,
            "request_count": self.request_count,
            "page_count": self.page_count,
            "observed_at_utc": self.observed_at_utc,
        }
        if self.receipt_fingerprint != context_fingerprint(
            "mcp-freshness-revalidation-receipt:v1",
            payload,
        ):
            raise ValueError("MCP freshness receipt fingerprint mismatch")


def build_mcp_freshness_revalidation_receipt(
    *,
    physical_operation_id: str,
    server_id: str,
    sdk_client_generation_id: str,
    installed_snapshot_id: str,
    installed_snapshot_semantic_fingerprint: str,
    installed_snapshot_authority_fingerprint: str,
    refreshed_snapshot_id: str,
    refreshed_snapshot_semantic_fingerprint: str,
    refreshed_snapshot_authority_fingerprint: str,
    refreshed_page_set_accumulator: str,
    request_count: int,
    page_count: int,
    observed_at_utc: str,
) -> McpFreshnessRevalidationReceipt:
    payload = {
        "physical_operation_id": physical_operation_id,
        "server_id": server_id,
        "sdk_client_generation_id": sdk_client_generation_id,
        "installed_snapshot_id": installed_snapshot_id,
        "installed_snapshot_semantic_fingerprint": (
            installed_snapshot_semantic_fingerprint
        ),
        "installed_snapshot_authority_fingerprint": (
            installed_snapshot_authority_fingerprint
        ),
        "refreshed_snapshot_id": refreshed_snapshot_id,
        "refreshed_snapshot_semantic_fingerprint": (
            refreshed_snapshot_semantic_fingerprint
        ),
        "refreshed_snapshot_authority_fingerprint": (
            refreshed_snapshot_authority_fingerprint
        ),
        "refreshed_page_set_accumulator": refreshed_page_set_accumulator,
        "request_count": request_count,
        "page_count": page_count,
        "observed_at_utc": observed_at_utc,
    }
    return McpFreshnessRevalidationReceipt(
        **payload,
        receipt_fingerprint=context_fingerprint(
            "mcp-freshness-revalidation-receipt:v1",
            payload,
        ),
    )


@dataclass(slots=True)
class McpBindingDispatchBorrow:
    """One physical-dispatch admission bound to an exact installed authority."""

    operation_id: str
    binding_lease_id: str
    slot_id: str
    server_id: str
    snapshot_id: str
    snapshot_semantic_fingerprint: str
    snapshot_authority_fingerprint: str
    config_epoch: int
    discovery_generation: int
    sdk_client_generation_id: str
    transport_generation: int
    protocol_semantic_fingerprint: str
    endpoint_attribution_fingerprint: str
    auth_attribution_fingerprint: str
    target_kind: str
    target_semantic_fingerprint: str
    dirty_signal_generation: int
    freshness_generation: int
    freshness_revalidation_receipt_fingerprint: str | None
    borrow_identity_fingerprint: str
    _active: bool = field(default=True, init=False, repr=False)

    def __post_init__(self) -> None:
        required = (
            self.operation_id,
            self.binding_lease_id,
            self.slot_id,
            self.server_id,
            self.snapshot_id,
            self.snapshot_semantic_fingerprint,
            self.snapshot_authority_fingerprint,
            self.sdk_client_generation_id,
            self.protocol_semantic_fingerprint,
            self.endpoint_attribution_fingerprint,
            self.auth_attribution_fingerprint,
            self.target_kind,
            self.target_semantic_fingerprint,
        )
        if any(not value for value in required):
            raise ValueError("MCP dispatch borrow identity is incomplete")
        if (
            min(
                self.config_epoch,
                self.discovery_generation,
                self.transport_generation,
                self.dirty_signal_generation,
                self.freshness_generation,
            )
            < 0
        ):
            raise ValueError("MCP dispatch borrow generations must be non-negative")
        expected = context_fingerprint(
            "mcp-binding-dispatch-borrow:v2",
            {
                "operation_id": self.operation_id,
                "binding_lease_id": self.binding_lease_id,
                "slot_id": self.slot_id,
                "server_id": self.server_id,
                "snapshot_id": self.snapshot_id,
                "snapshot_semantic_fingerprint": self.snapshot_semantic_fingerprint,
                "snapshot_authority_fingerprint": self.snapshot_authority_fingerprint,
                "config_epoch": self.config_epoch,
                "discovery_generation": self.discovery_generation,
                "sdk_client_generation_id": self.sdk_client_generation_id,
                "transport_generation": self.transport_generation,
                "protocol_semantic_fingerprint": self.protocol_semantic_fingerprint,
                "endpoint_attribution_fingerprint": self.endpoint_attribution_fingerprint,
                "auth_attribution_fingerprint": self.auth_attribution_fingerprint,
                "target_kind": self.target_kind,
                "target_semantic_fingerprint": self.target_semantic_fingerprint,
                "dirty_signal_generation": self.dirty_signal_generation,
                "freshness_generation": self.freshness_generation,
                "freshness_revalidation_receipt_fingerprint": (
                    self.freshness_revalidation_receipt_fingerprint
                ),
            },
        )
        if self.borrow_identity_fingerprint != expected:
            raise ValueError("MCP dispatch borrow fingerprint mismatch")

    @property
    def active(self) -> bool:
        return self._active

    def require_active(self, *, operation_id: str) -> None:
        if not self._active or operation_id != self.operation_id:
            raise RuntimeError("MCP physical dispatch borrow is not active")

    def release(self) -> None:
        if not self._active:
            raise RuntimeError("MCP physical dispatch borrow was already released")
        self._active = False


def build_mcp_binding_dispatch_borrow(
    *,
    operation_id: str,
    binding_lease_id: str,
    slot_id: str,
    server_id: str,
    snapshot_id: str,
    snapshot_semantic_fingerprint: str,
    snapshot_authority_fingerprint: str,
    config_epoch: int,
    discovery_generation: int,
    sdk_client_generation_id: str,
    transport_generation: int,
    protocol_semantic_fingerprint: str,
    endpoint_attribution_fingerprint: str,
    auth_attribution_fingerprint: str,
    target_kind: str,
    target_semantic_fingerprint: str,
    dirty_signal_generation: int,
    freshness_generation: int,
    freshness_revalidation_receipt_fingerprint: str | None,
) -> McpBindingDispatchBorrow:
    payload = {
        "operation_id": operation_id,
        "binding_lease_id": binding_lease_id,
        "slot_id": slot_id,
        "server_id": server_id,
        "snapshot_id": snapshot_id,
        "snapshot_semantic_fingerprint": snapshot_semantic_fingerprint,
        "snapshot_authority_fingerprint": snapshot_authority_fingerprint,
        "config_epoch": config_epoch,
        "discovery_generation": discovery_generation,
        "sdk_client_generation_id": sdk_client_generation_id,
        "transport_generation": transport_generation,
        "protocol_semantic_fingerprint": protocol_semantic_fingerprint,
        "endpoint_attribution_fingerprint": endpoint_attribution_fingerprint,
        "auth_attribution_fingerprint": auth_attribution_fingerprint,
        "target_kind": target_kind,
        "target_semantic_fingerprint": target_semantic_fingerprint,
        "dirty_signal_generation": dirty_signal_generation,
        "freshness_generation": freshness_generation,
        "freshness_revalidation_receipt_fingerprint": (
            freshness_revalidation_receipt_fingerprint
        ),
    }
    return McpBindingDispatchBorrow(
        **payload,
        borrow_identity_fingerprint=context_fingerprint(
            "mcp-binding-dispatch-borrow:v2",
            payload,
        ),
    )


class McpRawToolCallResultCarrier(FrozenRuntimeStateBase):
    operation_id: str
    sdk_client_generation_id: str
    tool_semantic_fingerprint: str
    result_kind: Literal["complete", "input_required"]
    frozen_protocol_result_without_structured_content: FrozenJsonObjectFact
    structured_content_present: bool
    structured_content: FrozenJsonValue
    carrier_fingerprint: str


def build_raw_tool_call_result_carrier(
    *,
    result: object,
    operation_id: str,
    sdk_client_generation_id: str,
    tool_semantic_fingerprint: str,
) -> McpRawToolCallResultCarrier:
    """Freeze an SDK result while preserving absent versus explicit null."""

    fields_set = getattr(result, "model_fields_set", set())
    dump = getattr(result, "model_dump", None)
    if not callable(dump):
        raise TypeError("MCP raw result must be an SDK Pydantic model")
    raw = dump(mode="json", by_alias=True, exclude_unset=True)
    if not isinstance(raw, Mapping):
        raise TypeError("MCP raw result must lower to a JSON object")
    base = dict(raw)
    alias_present = "structuredContent" in base
    name_present = "structured_content" in fields_set
    structured_present = alias_present or name_present
    structured = base.pop("structuredContent", None)
    if any(key.casefold().replace("_", "") == "structuredcontent" for key in base):
        raise ValueError("MCP raw result base retained structuredContent")
    frozen_base = freeze_json(base)
    if not isinstance(frozen_base, FrozenJsonObjectFact):
        raise TypeError("MCP raw result base must be a JSON object")
    frozen_structured = freeze_json(structured if structured_present else None)
    result_kind = raw.get("resultType", "complete")
    if result_kind not in {"complete", "input_required"}:
        raise ValueError(f"unsupported MCP resultType: {result_kind!r}")
    payload = {
        "operation_id": operation_id,
        "sdk_client_generation_id": sdk_client_generation_id,
        "tool_semantic_fingerprint": tool_semantic_fingerprint,
        "result_kind": result_kind,
        "frozen_protocol_result_without_structured_content": frozen_base,
        "structured_content_present": structured_present,
        "structured_content": frozen_structured,
    }
    return McpRawToolCallResultCarrier(
        **payload,
        carrier_fingerprint=context_fingerprint(
            "mcp-raw-tool-call-result-carrier:v1",
            payload,
        ),
    )


__all__ = [
    "McpBindingDispatchBorrow",
    "McpFreshnessRevalidationReceipt",
    "McpRawToolCallResultCarrier",
    "McpSdkConcurrencyMode",
    "McpSdkConformedClientGeneration",
    "McpSdkNegotiatedProtocolBinding",
    "McpSdkProtocolBinding",
    "build_mcp_binding_dispatch_borrow",
    "build_mcp_freshness_revalidation_receipt",
    "build_raw_tool_call_result_carrier",
]
