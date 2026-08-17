"""Small process-local model target, budget, usage, and failure contracts."""

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
    CONTEXT_WINDOW_COMPACTION_SUMMARY = "context_window_compaction_summary"
    MEMORY_GOVERNANCE = "memory_governance"
    MEMORY_HINT_REVIEW = "memory_hint_review"
    COMPACTION_MEMORY_EXTRACTION = "compaction_memory_extraction"


class ModelContextMode(StrEnum):
    COMPILED = "compiled"
    DIRECT = "direct"


def canonical_json_bytes(value: object) -> bytes:
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
            reasoning_effort=self.reasoning_effort
        )
        if self.options_fingerprint != expected:
            raise ValueError("options_fingerprint does not match effective options")
        return self


def resolved_model_options_fingerprint(*, reasoning_effort: str | None) -> str:
    return sha256_fingerprint(
        "resolved-model-options:v2", {"reasoning_effort": reasoning_effort}
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
        if len(set(keys)) != len(keys) or keys != sorted(keys):
            raise ValueError("diagnostic attribute keys must be unique and sorted")
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

    contract_version: Literal["resolved-model-target:v4"] = "resolved-model-target:v4"
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


def resolved_model_target_fingerprint(payload_without_fingerprint: dict[str, Any]) -> str:
    return sha256_fingerprint("resolved-model-target:v4", payload_without_fingerprint)


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


class ProviderModelStreamErrorCode(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMITED = "rate_limited"
    PROVIDER_OVERLOADED = "provider_overloaded"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    CONTENT_FILTERED = "content_filtered"
    TRANSPORT_PROTOCOL_ERROR = "transport_protocol_error"
    TRANSPORT_SOURCE_ITEM_LIMIT_EXCEEDED = "transport_source_item_limit_exceeded"
    TRANSPORT_SOURCE_PAYLOAD_LIMIT_EXCEEDED = "transport_source_payload_limit_exceeded"
    UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"


class ProviderErrorSanitizationContractFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["provider_error_sanitization_contract.v1"] = (
        "provider_error_sanitization_contract.v1"
    )
    contract_id: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    stable_code_mapping_fingerprint: str = Field(min_length=1)
    sensitive_key_policy_fingerprint: str = Field(min_length=1)
    secret_pattern_policy_fingerprint: str = Field(min_length=1)
    url_redaction_policy_fingerprint: str = Field(min_length=1)
    diagnostic_attribute_allowlist_fingerprint: str = Field(min_length=1)
    max_message_chars: int = Field(ge=1)
    max_diagnostic_count: int = Field(ge=0)
    max_diagnostic_attribute_chars: int = Field(ge=1)
    contract_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_fingerprint(self) -> "ProviderErrorSanitizationContractFact":
        expected = sha256_fingerprint(
            "provider-error-sanitization-contract:v1",
            self.model_dump(mode="json", exclude={"contract_fingerprint"}),
        )
        if self.contract_fingerprint != expected:
            raise ValueError("provider error sanitization contract fingerprint mismatch")
        return self


class ProviderSanitizedDiagnosticKind(StrEnum):
    PROVIDER_STATUS = "provider_status"
    PROVIDER_CODE = "provider_code"
    PROVIDER_REQUEST_ID = "provider_request_id"
    RETRY_AFTER = "retry_after"
    TRANSPORT_ENDPOINT = "transport_endpoint"
    ADAPTER_CONTEXT = "adapter_context"


class ProviderSanitizedDiagnosticFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    diagnostic_kind: ProviderSanitizedDiagnosticKind
    attributes: dict[str, str]
    redaction_count: int = Field(ge=0)
    truncated: bool
    diagnostic_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_fingerprint(self) -> "ProviderSanitizedDiagnosticFact":
        expected = sha256_fingerprint(
            "provider-sanitized-diagnostic:v1",
            self.model_dump(mode="json", exclude={"diagnostic_fingerprint"}),
        )
        if self.diagnostic_fingerprint != expected:
            raise ValueError("provider sanitized diagnostic fingerprint mismatch")
        return self


class ProviderRetryAttemptSummaryFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt: int = Field(ge=1, le=32)
    max_attempts: int = Field(ge=1, le=32)
    reason: str = Field(min_length=1, max_length=96)
    status_code: int | None = Field(default=None, ge=100, le=599)
    delay_millis: int | None = Field(default=None, ge=0, le=600_000)
    retry_after_millis: int | None = Field(default=None, ge=0, le=600_000)
    retry_after_exceeded: bool
    attempt_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_attempt(self) -> "ProviderRetryAttemptSummaryFact":
        if self.attempt > self.max_attempts:
            raise ValueError("provider retry attempt exceeds max attempts")
        expected = sha256_fingerprint(
            "provider-retry-attempt-summary:v1",
            self.model_dump(mode="json", exclude={"attempt_fingerprint"}),
        )
        if self.attempt_fingerprint != expected:
            raise ValueError("provider retry attempt summary fingerprint mismatch")
        return self


class ProviderRetrySummaryFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["provider_retry_summary.v1"] = "provider_retry_summary.v1"
    enabled: bool
    final_attempt: int = Field(ge=1, le=32)
    max_attempts: int = Field(ge=1, le=32)
    retry_count: int = Field(ge=0, le=31)
    exhausted: bool
    has_semantic_output: bool
    skipped_reason: str | None = Field(default=None, max_length=96)
    final_reason: str = Field(min_length=1, max_length=96)
    final_status_code: int | None = Field(default=None, ge=100, le=599)
    retry_after_exceeded: bool
    attempts: tuple[ProviderRetryAttemptSummaryFact, ...]
    summary_contract_fingerprint: str = Field(min_length=1)
    summary_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_summary(self) -> "ProviderRetrySummaryFact":
        if self.final_attempt > self.max_attempts:
            raise ValueError("provider retry final attempt exceeds max attempts")
        if self.retry_count != len(self.attempts):
            raise ValueError("provider retry summary count mismatch")
        if tuple(item.attempt for item in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("provider retry summary attempts are not contiguous")
        if any(item.max_attempts != self.max_attempts for item in self.attempts):
            raise ValueError("provider retry summary max attempts drifted")
        expected_contract = sha256_fingerprint(
            "provider-retry-summary-contract:v1",
            {
                "max_attempts": 32,
                "fields": (
                    "attempt",
                    "reason",
                    "status_code",
                    "delay_millis",
                    "retry_after_millis",
                    "retry_after_exceeded",
                ),
                "excluded": (
                    "exception_message",
                    "exception_repr",
                    "provider_data",
                    "url",
                    "secret",
                ),
            },
        )
        if self.summary_contract_fingerprint != expected_contract:
            raise ValueError("provider retry summary contract mismatch")
        expected = sha256_fingerprint(
            "provider-retry-summary:v1",
            self.model_dump(mode="json", exclude={"summary_fingerprint"}),
        )
        if self.summary_fingerprint != expected:
            raise ValueError("provider retry summary fingerprint mismatch")
        return self


class ProviderSanitizedErrorFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["provider_sanitized_error.v2"] = (
        "provider_sanitized_error.v2"
    )
    code: ProviderModelStreamErrorCode
    message: str
    diagnostics: tuple[ProviderSanitizedDiagnosticFact, ...]
    redaction_count: int = Field(ge=0)
    truncated: bool
    sanitization_contract: ProviderErrorSanitizationContractFact
    retry_summary: ProviderRetrySummaryFact | None = None
    error_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_error(self) -> "ProviderSanitizedErrorFact":
        if len(self.message) > self.sanitization_contract.max_message_chars:
            raise ValueError("provider sanitized error message exceeds contract cap")
        if len(self.diagnostics) > self.sanitization_contract.max_diagnostic_count:
            raise ValueError("provider sanitized diagnostics exceed contract cap")
        if self.redaction_count != sum(
            diagnostic.redaction_count for diagnostic in self.diagnostics
        ):
            raise ValueError("provider sanitized error redaction count mismatch")
        expected = sha256_fingerprint(
            "provider-sanitized-error:v2",
            self.model_dump(mode="json", exclude={"error_fingerprint"}),
        )
        if self.error_fingerprint != expected:
            raise ValueError("provider sanitized error fingerprint mismatch")
        return self


__all__ = [
    "ModelCallDiagnosticFact",
    "ModelCallPurpose",
    "ModelContextLimits",
    "ModelContextMode",
    "ModelTokenUsageFact",
    "ProviderErrorSanitizationContractFact",
    "ProviderModelStreamErrorCode",
    "ProviderRetryAttemptSummaryFact",
    "ProviderRetrySummaryFact",
    "ProviderSanitizedDiagnosticFact",
    "ProviderSanitizedDiagnosticKind",
    "ProviderSanitizedErrorFact",
    "ResolvedModelCallFact",
    "ResolvedModelContextBudgetFact",
    "ResolvedModelOptionsFact",
    "ResolvedModelTargetFact",
    "TokenEstimatorFact",
    "canonical_json_bytes",
    "resolved_model_options_fingerprint",
    "resolved_model_target_fingerprint",
    "sha256_fingerprint",
]
