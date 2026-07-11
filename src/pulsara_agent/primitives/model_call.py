"""Event-safe model target, call, budget, and usage contracts.

This module deliberately has no dependency on events, transports, runtime
objects, credentials, or provider SDKs.  The values defined here are safe to
persist in the event ledger and use as cross-layer identity contracts.
"""

from __future__ import annotations

import hashlib
import json
import math
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelCallPurpose(StrEnum):
    AGENT_MODEL_LOOP = "agent_model_loop"
    CONTEXT_COMPACTION_SUMMARY = "context_compaction_summary"
    MEMORY_GOVERNANCE = "memory_governance"
    MEMORY_REFLECTION = "memory_reflection"


class ModelContextMode(StrEnum):
    COMPILED = "compiled"
    DIRECT = "direct"


def canonical_json_bytes(value: object) -> bytes:
    """Serialize a fingerprint payload deterministically and strictly."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_fingerprint(namespace: str, value: object) -> str:
    digest = hashlib.sha256()
    digest.update(namespace.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(value))
    return f"sha256:{digest.hexdigest()}"


class ModelContextLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total_context_tokens: int = Field(ge=2)
    max_input_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    default_output_tokens: int = Field(ge=1)
    input_safety_margin_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_limits(self) -> "ModelContextLimits":
        if self.max_input_tokens > self.total_context_tokens:
            raise ValueError("max_input_tokens exceeds total_context_tokens")
        if self.max_output_tokens > self.total_context_tokens:
            raise ValueError("max_output_tokens exceeds total_context_tokens")
        if self.default_output_tokens > self.max_output_tokens:
            raise ValueError("default_output_tokens exceeds max_output_tokens")
        default_input = (
            min(
                self.max_input_tokens,
                self.total_context_tokens - self.default_output_tokens,
            )
            - self.input_safety_margin_tokens
        )
        if default_input < 1:
            raise ValueError("default model input budget is non-positive")
        return self


class ResolvedModelOptionsFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reasoning_effort: str | None
    options_fingerprint: str

    @model_validator(mode="after")
    def _validate_options(self) -> "ResolvedModelOptionsFact":
        expected = resolved_model_options_fingerprint(
            reasoning_effort=self.reasoning_effort,
        )
        if self.options_fingerprint != expected:
            raise ValueError("options_fingerprint does not match effective options")
        return self


def resolved_model_options_fingerprint(
    *,
    reasoning_effort: str | None,
) -> str:
    return sha256_fingerprint(
        "resolved-model-options:v2",
        {
            "reasoning_effort": reasoning_effort,
        },
    )


class TokenEstimatorFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    estimator_id: str = Field(min_length=1)
    estimator_version: str = Field(min_length=1)
    estimator_fingerprint: str = Field(min_length=1)


class ResolvedModelContextBudgetFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    effective_output_tokens: int = Field(ge=1)
    pre_margin_input_tokens: int = Field(ge=1)
    safety_margin_tokens: int = Field(ge=0)
    input_budget_tokens: int = Field(ge=1)


class ModelCallDiagnosticFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=96)
    message: str = Field(default="", max_length=512)
    attributes: tuple[tuple[str, str | int | float | bool | None], ...] = ()

    @model_validator(mode="after")
    def _validate_attributes(self) -> "ModelCallDiagnosticFact":
        if len(self.attributes) > 16:
            raise ValueError("diagnostic attributes exceed 16 entries")
        keys = [item[0] for item in self.attributes]
        if len(set(keys)) != len(keys):
            raise ValueError("diagnostic attribute keys must be unique")
        if keys != sorted(keys):
            raise ValueError("diagnostic attributes must be sorted by key")
        for key, value in self.attributes:
            if not key or len(key) > 96:
                raise ValueError("diagnostic attribute key is invalid")
            if isinstance(value, str) and len(value) > 256:
                raise ValueError("diagnostic string attribute exceeds 256 characters")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("diagnostic float attribute must be finite")
        return self


class ModelTokenUsageFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_usage(self) -> "ModelTokenUsageFact":
        if (
            self.cached_input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("cached_input_tokens exceeds input_tokens")
        if (
            self.reasoning_output_tokens is not None
            and self.reasoning_output_tokens > self.output_tokens
        ):
            raise ValueError("reasoning_output_tokens exceeds output_tokens")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


class ResolvedModelTargetFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["resolved-model-target:v2"] = "resolved-model-target:v2"
    target_fingerprint: str
    model_id: str = Field(min_length=1)
    model_role: Literal["pro", "flash"]
    provider: str = Field(min_length=1)
    api: str = Field(min_length=1)
    endpoint_origin: str = Field(min_length=1)
    endpoint_fingerprint: str = Field(min_length=1)
    provider_profile_id: str = Field(min_length=1)
    provider_request_shape_fingerprint: str = Field(min_length=1)
    transport_binding_id: str = Field(min_length=1)
    transport_contract_version: str = Field(min_length=1)
    model_identity_policy: Literal["accept_reported", "exact"]
    supports_tools: bool
    supports_reasoning: bool
    limits: ModelContextLimits
    effective_options: ResolvedModelOptionsFact
    context_budget: ResolvedModelContextBudgetFact
    token_estimator: TokenEstimatorFact

    @model_validator(mode="after")
    def _validate_target(self) -> "ResolvedModelTargetFact":
        expected_pre_margin = min(
            self.limits.max_input_tokens,
            self.limits.total_context_tokens
            - self.context_budget.effective_output_tokens,
        )
        if self.context_budget.effective_output_tokens > self.limits.max_output_tokens:
            raise ValueError("effective output exceeds model maximum")
        if (
            self.context_budget.effective_output_tokens
            != self.limits.default_output_tokens
        ):
            raise ValueError("effective output must equal model slot default output")
        if self.context_budget.pre_margin_input_tokens != expected_pre_margin:
            raise ValueError("pre-margin input budget is inconsistent")
        if (
            self.context_budget.safety_margin_tokens
            != self.limits.input_safety_margin_tokens
        ):
            raise ValueError("safety margin is inconsistent with model limits")
        expected_input = expected_pre_margin - self.limits.input_safety_margin_tokens
        if self.context_budget.input_budget_tokens != expected_input:
            raise ValueError("input budget is inconsistent with model limits")
        expected_fingerprint = resolved_model_target_fingerprint(
            self.model_dump(mode="json", exclude={"target_fingerprint"})
        )
        if self.target_fingerprint != expected_fingerprint:
            raise ValueError("target_fingerprint does not match target contract")
        return self


def resolved_model_target_fingerprint(
    payload_without_fingerprint: dict[str, Any],
) -> str:
    return sha256_fingerprint("resolved-model-target:v2", payload_without_fingerprint)


class ResolvedModelCallFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["resolved-model-call:v1"] = "resolved-model-call:v1"
    resolved_model_call_id: str = Field(pattern=r"^model_call:[0-9a-f]{32}$")
    purpose: ModelCallPurpose
    context_mode: ModelContextMode
    target: ResolvedModelTargetFact

    @model_validator(mode="after")
    def _validate_mode(self) -> "ResolvedModelCallFact":
        expected_mode = (
            ModelContextMode.COMPILED
            if self.purpose is ModelCallPurpose.AGENT_MODEL_LOOP
            else ModelContextMode.DIRECT
        )
        if self.context_mode is not expected_mode:
            raise ValueError(
                f"{self.purpose.value} requires context_mode={expected_mode.value}"
            )
        return self


MeasurementStage = Literal["tool_result_render", "section_allocation", "final_payload"]


class ContextBudgetReportEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target_fingerprint: str
    resolved_model_call_id: str
    measurement_stage: MeasurementStage
    total_context_tokens: int = Field(ge=1)
    max_input_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    effective_output_tokens: int = Field(ge=1)
    safety_margin_tokens: int = Field(ge=0)
    input_budget_tokens: int = Field(ge=1)
    sections_estimated_tokens: int | None = Field(default=None, ge=0)
    tools_estimated_tokens: int | None = Field(default=None, ge=0)
    envelope_estimated_tokens: int | None = Field(default=None, ge=0)
    allocation_estimated_tokens: int | None = Field(default=None, ge=0)
    final_payload_estimated_tokens: int | None = Field(default=None, ge=0)
    non_transcript_baseline_tokens: int | None = Field(default=None, ge=0)
    transcript_estimated_tokens: int | None = Field(default=None, ge=0)
    estimator: TokenEstimatorFact

    @model_validator(mode="after")
    def _validate_measurements(self) -> "ContextBudgetReportEvent":
        measurements = (
            self.sections_estimated_tokens,
            self.tools_estimated_tokens,
            self.envelope_estimated_tokens,
            self.allocation_estimated_tokens,
            self.final_payload_estimated_tokens,
            self.non_transcript_baseline_tokens,
            self.transcript_estimated_tokens,
        )
        if self.measurement_stage == "tool_result_render":
            if any(value is not None for value in measurements):
                raise ValueError(
                    "tool_result_render stage cannot contain aggregate measurements"
                )
        elif self.measurement_stage == "section_allocation":
            required = (
                self.sections_estimated_tokens,
                self.tools_estimated_tokens,
                self.allocation_estimated_tokens,
            )
            unavailable = (
                self.envelope_estimated_tokens,
                self.final_payload_estimated_tokens,
                self.non_transcript_baseline_tokens,
                self.transcript_estimated_tokens,
            )
            if any(value is None for value in required) or any(
                value is not None for value in unavailable
            ):
                raise ValueError("section_allocation measurements are incomplete")
        elif any(value is None for value in measurements):
            raise ValueError("final_payload stage requires every measurement")

        if (
            self.allocation_estimated_tokens is not None
            and self.sections_estimated_tokens is not None
            and self.tools_estimated_tokens is not None
            and self.allocation_estimated_tokens
            != self.sections_estimated_tokens + self.tools_estimated_tokens
        ):
            raise ValueError("allocation estimate must equal sections + tools")
        if (
            self.final_payload_estimated_tokens is not None
            and self.non_transcript_baseline_tokens is not None
            and self.transcript_estimated_tokens is not None
            and self.final_payload_estimated_tokens
            != self.non_transcript_baseline_tokens + self.transcript_estimated_tokens
        ):
            raise ValueError("final payload estimate must equal baseline + transcript")
        return self


class CompactionTargetEstimateFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    estimate_scope: Literal["compiled_context_baseline", "transcript_only"]
    basis_context_id: str | None
    basis_context_compiled_sequence: int | None = Field(default=None, ge=1)
    target_fingerprint: str
    non_transcript_baseline_tokens: int | None = Field(default=None, ge=0)
    transcript_tokens_before: int = Field(ge=0)
    estimated_tokens_before: int = Field(ge=0)
    summary_tokens_reserved: int = Field(ge=0)
    retained_transcript_tokens: int = Field(ge=0)
    protected_transcript_tokens: int = Field(ge=0)
    summary_tokens_actual: int | None = Field(default=None, ge=0)
    transcript_tokens_after: int | None = Field(default=None, ge=0)
    estimated_tokens_after: int | None = Field(default=None, ge=0)
    predicted_post_target_reached: bool | None

    @model_validator(mode="after")
    def _validate_scope(self) -> "CompactionTargetEstimateFact":
        if self.estimate_scope == "compiled_context_baseline":
            if (
                self.basis_context_id is None
                or self.basis_context_compiled_sequence is None
                or self.non_transcript_baseline_tokens is None
            ):
                raise ValueError("compiled baseline attribution is required")
            if self.estimated_tokens_before != (
                self.non_transcript_baseline_tokens + self.transcript_tokens_before
            ):
                raise ValueError("compiled baseline pre-estimate is inconsistent")
            if self.transcript_tokens_after is not None:
                expected_after = (
                    self.non_transcript_baseline_tokens + self.transcript_tokens_after
                )
                if self.estimated_tokens_after != expected_after:
                    raise ValueError("compiled baseline post-estimate is inconsistent")
        else:
            if (
                self.basis_context_id is not None
                or self.basis_context_compiled_sequence is not None
                or self.non_transcript_baseline_tokens is not None
            ):
                raise ValueError(
                    "transcript-only estimate cannot contain compiled attribution"
                )
            if self.estimated_tokens_before != self.transcript_tokens_before:
                raise ValueError("transcript-only pre-estimate is inconsistent")
            if self.predicted_post_target_reached is not None:
                raise ValueError(
                    "transcript-only estimate cannot claim full target success"
                )
            if (
                self.transcript_tokens_after is not None
                and self.estimated_tokens_after != self.transcript_tokens_after
            ):
                raise ValueError("transcript-only post-estimate is inconsistent")
        if (self.transcript_tokens_after is None) != (
            self.estimated_tokens_after is None
        ):
            raise ValueError(
                "post-compaction estimates must be both present or both absent"
            )
        if (self.summary_tokens_actual is None) != (
            self.transcript_tokens_after is None
        ):
            raise ValueError(
                "actual summary and post-compaction estimates must appear together"
            )
        if (
            self.summary_tokens_actual is not None
            and self.transcript_tokens_after
            != self.summary_tokens_actual
            + self.retained_transcript_tokens
            + self.protected_transcript_tokens
        ):
            raise ValueError(
                "post-compaction transcript must equal summary + retained + protected"
            )
        if (
            self.summary_tokens_actual is not None
            and self.summary_tokens_actual > self.summary_tokens_reserved
        ):
            raise ValueError("actual summary tokens exceed the planning reservation")
        if (
            self.transcript_tokens_after is None
            and self.predicted_post_target_reached is not None
        ):
            raise ValueError("pre-compaction estimate cannot claim post-target success")
        if (
            self.estimate_scope == "compiled_context_baseline"
            and self.transcript_tokens_after is not None
            and self.predicted_post_target_reached is None
        ):
            raise ValueError(
                "compiled baseline post-estimate requires a target prediction"
            )
        return self


class CompactionObservedAfterMeasurementFact(BaseModel):
    """Observed post-summary values that explain a failed invariant.

    Successful target estimates remain strict.  This separate fact preserves
    measurements that are themselves evidence of why summary validation
    failed, without making an invalid success estimate representable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary_tokens_actual: int = Field(ge=0)
    retained_transcript_tokens: int = Field(ge=0)
    protected_transcript_tokens: int = Field(ge=0)
    transcript_tokens_after: int = Field(ge=0)
    estimated_tokens_after: int = Field(ge=0)
    predicted_post_target_reached: bool | None
    violation_code: Literal["summary_tokens_exceed_reservation"]

    @model_validator(mode="after")
    def _validate_observed_after(self) -> "CompactionObservedAfterMeasurementFact":
        if self.transcript_tokens_after != (
            self.summary_tokens_actual
            + self.retained_transcript_tokens
            + self.protected_transcript_tokens
        ):
            raise ValueError(
                "observed transcript must equal summary + retained + protected"
            )
        return self
