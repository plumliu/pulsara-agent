"""Small immutable JSON and tool-render contracts used by the Kernel."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import (
    CapabilityDescriptorRenderAttributionFact,
    ContextEventRangeFact,
    ContextEventReferenceFact,
    FrozenContextFact,
    FrozenJsonArrayFact,
    FrozenJsonEntryFact,
    FrozenJsonObjectFact,
    FrozenJsonScalar,
    FrozenJsonValue,
    ToolArgumentsParseErrorCode,
    canonical_json_bytes,
    canonical_utc_timestamp,
    context_fingerprint,
    freeze_json,
    thaw_json,
)


def _validate_fingerprint(model: FrozenContextFact, domain: str, field: str) -> None:
    expected = context_fingerprint(
        domain, model.model_dump(mode="json", exclude={field})
    )
    if getattr(model, field) != expected:
        raise ValueError(f"{field} mismatch")


def _ordered_unique(values: tuple[str, ...], label: str) -> None:
    if values != tuple(dict.fromkeys(values)):
        raise ValueError(f"{label} must be ordered and unique")


class ToolResultEnvelopeRenderPolicyFact(FrozenContextFact):
    full_string_cap_chars: int = Field(ge=0)
    compact_string_cap_chars: int = Field(ge=0)
    minimal_string_cap_chars: int = Field(ge=0)
    ultra_minimal_string_cap_chars: int = Field(ge=0)
    max_process_summaries: int = Field(ge=0)
    compact_process_summaries: int = Field(ge=0)
    process_summary_string_cap_chars: int = Field(ge=0)
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_policy(self) -> "ToolResultEnvelopeRenderPolicyFact":
        caps = (
            self.full_string_cap_chars,
            self.compact_string_cap_chars,
            self.minimal_string_cap_chars,
            self.ultra_minimal_string_cap_chars,
        )
        if tuple(sorted(caps, reverse=True)) != caps:
            raise ValueError("envelope string caps must be non-increasing")
        if self.compact_process_summaries > self.max_process_summaries:
            raise ValueError("compact process summary cap exceeds full cap")
        _validate_fingerprint(
            self, "tool-result-envelope-render-policy:v1", "policy_fingerprint"
        )
        return self


class ToolResultRenderPolicyBasisFact(FrozenContextFact):
    policy_version: Literal["tool-result-render-policy:v2"] = (
        "tool-result-render-policy:v2"
    )
    per_tool_cap_chars: int = Field(gt=0)
    per_message_cap_chars: int = Field(gt=0)
    per_envelope_cap_chars: int = Field(gt=0)
    minimum_essential_envelope_chars: int = Field(ge=1)
    max_artifact_refs_per_unit: int = Field(ge=0)
    max_data_placeholder_chars: int = Field(ge=0)
    envelope_render: ToolResultEnvelopeRenderPolicyFact
    basis_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_basis(self) -> "ToolResultRenderPolicyBasisFact":
        if self.minimum_essential_envelope_chars > self.per_envelope_cap_chars:
            raise ValueError("minimum essential envelope exceeds per-unit cap")
        _validate_fingerprint(
            self, "tool-result-render-policy-basis:v2", "basis_fingerprint"
        )
        return self


class ResolvedToolResultRenderPolicyFact(FrozenContextFact):
    basis: ToolResultRenderPolicyBasisFact
    ordered_unit_ids: tuple[str, ...]
    protected_unit_ids: tuple[str, ...]
    unit_order_fingerprint: str = Field(min_length=1)
    protection_fingerprint: str = Field(min_length=1)
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_resolved(self) -> "ResolvedToolResultRenderPolicyFact":
        _ordered_unique(self.ordered_unit_ids, "tool result unit IDs")
        _ordered_unique(self.protected_unit_ids, "protected tool result unit IDs")
        if not set(self.protected_unit_ids).issubset(self.ordered_unit_ids):
            raise ValueError("protected units are not in ordered units")
        if self.unit_order_fingerprint != context_fingerprint(
            "tool-result-unit-order:v2", self.ordered_unit_ids
        ):
            raise ValueError("tool result unit order fingerprint mismatch")
        if self.protection_fingerprint != context_fingerprint(
            "tool-result-unit-protection:v2", self.protected_unit_ids
        ):
            raise ValueError("tool result protection fingerprint mismatch")
        _validate_fingerprint(
            self, "resolved-tool-result-render-policy:v2", "policy_fingerprint"
        )
        return self


__all__ = [
    "CapabilityDescriptorRenderAttributionFact",
    "ContextEventRangeFact",
    "ContextEventReferenceFact",
    "FrozenContextFact",
    "FrozenJsonArrayFact",
    "FrozenJsonEntryFact",
    "FrozenJsonObjectFact",
    "FrozenJsonScalar",
    "FrozenJsonValue",
    "ResolvedToolResultRenderPolicyFact",
    "ToolArgumentsParseErrorCode",
    "ToolResultEnvelopeRenderPolicyFact",
    "ToolResultRenderPolicyBasisFact",
    "canonical_json_bytes",
    "canonical_utc_timestamp",
    "context_fingerprint",
    "freeze_json",
    "thaw_json",
]
