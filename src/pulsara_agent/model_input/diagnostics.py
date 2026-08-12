"""Bounded public projection for structured model-input observations.

This module deliberately projects only closed codes, counts, and opaque
fingerprints.  Provider-visible text, tool arguments, paths, and source
semantic fingerprints never cross this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from pulsara_agent.model_input.contracts import (
    ContextPublicDiagnosticCode,
    FrozenCompiledModelInput,
    StructuredModelInputLimits,
    STRUCTURED_MODEL_INPUT_LIMITS,
)


class CompileObservationDisposition(StrEnum):
    COMPILED = "COMPILED"


class CompileDecisionSampleKind(StrEnum):
    SOURCE = "SOURCE"
    TOOL_RESULT = "TOOL_RESULT"


@dataclass(frozen=True, slots=True)
class CompileDecisionSample:
    decision_kind: CompileDecisionSampleKind
    source_kind: str | None
    current_turn: bool | None
    selected_mode: str | None
    included: bool | None
    reason_code: str

    def __post_init__(self) -> None:
        if not self.reason_code:
            raise ValueError("compile decision sample reason is empty")
        if self.decision_kind is CompileDecisionSampleKind.SOURCE:
            if self.source_kind is None or self.current_turn is not None:
                raise ValueError("source compile decision sample is malformed")
        elif (
            self.source_kind is not None
            or self.current_turn is None
            or self.included is not None
        ):
            raise ValueError("tool-result compile decision sample is malformed")

    def public_value(self) -> Mapping[str, object]:
        if self.decision_kind is CompileDecisionSampleKind.SOURCE:
            return {
                "decision_kind": self.decision_kind.value,
                "source_kind": self.source_kind or "",
                "selected_mode": self.selected_mode,
                "included": bool(self.included),
                "reason_code": self.reason_code,
            }
        return {
            "decision_kind": self.decision_kind.value,
            "current_turn": bool(self.current_turn),
            "selected_mode": self.selected_mode or "",
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ModelInputCompileOperationalProjection:
    disposition: CompileObservationDisposition
    compiler_contract_version: str
    model_call_index: int
    target_fingerprint: str
    tool_surface_fingerprint: str
    effective_input_budget_tokens: int
    total_input_tokens: int
    system_tokens: int
    message_tokens: int
    tool_tokens: int
    envelope_tokens: int
    degraded_source_count: int
    omitted_source_count: int
    degraded_tool_result_count: int
    omitted_tool_result_body_count: int
    diagnostic_codes: tuple[ContextPublicDiagnosticCode, ...]
    decision_digest: str
    decision_samples: tuple[CompileDecisionSample, ...]
    decision_omitted_count: int

    def __post_init__(self) -> None:
        if (
            self.model_call_index < 1
            or min(
                self.effective_input_budget_tokens,
                self.total_input_tokens,
                self.system_tokens,
                self.message_tokens,
                self.tool_tokens,
                self.envelope_tokens,
                self.degraded_source_count,
                self.omitted_source_count,
                self.degraded_tool_result_count,
                self.omitted_tool_result_body_count,
                self.decision_omitted_count,
            )
            < 0
        ):
            raise ValueError("compile observation contains an invalid count")
        if (
            not self.compiler_contract_version
            or not self.target_fingerprint
            or not self.tool_surface_fingerprint
            or not self.decision_digest
        ):
            raise ValueError("compile observation identity is incomplete")

    def public_payload(self) -> Mapping[str, object]:
        return {
            "disposition": self.disposition.value,
            "compiler_contract_version": self.compiler_contract_version,
            "model_call_index": self.model_call_index,
            "target_fingerprint": self.target_fingerprint,
            "tool_surface_fingerprint": self.tool_surface_fingerprint,
            "effective_input_budget_tokens": self.effective_input_budget_tokens,
            "total_input_tokens": self.total_input_tokens,
            "system_tokens": self.system_tokens,
            "message_tokens": self.message_tokens,
            "tool_tokens": self.tool_tokens,
            "envelope_tokens": self.envelope_tokens,
            "degraded_source_count": self.degraded_source_count,
            "omitted_source_count": self.omitted_source_count,
            "degraded_tool_result_count": self.degraded_tool_result_count,
            "omitted_tool_result_body_count": self.omitted_tool_result_body_count,
            "diagnostic_codes": tuple(item.value for item in self.diagnostic_codes),
            "decision_digest": self.decision_digest,
            "decision_sample_count": len(self.decision_samples),
            "decision_omitted_count": self.decision_omitted_count,
            "decision_samples": tuple(
                item.public_value() for item in self.decision_samples
            ),
        }


def project_model_input_compile_observation(
    *,
    model_call_index: int,
    compiled: FrozenCompiledModelInput,
    limits: StructuredModelInputLimits = STRUCTURED_MODEL_INPUT_LIMITS,
) -> ModelInputCompileOperationalProjection:
    """Project one compiled input without exposing provider-visible content."""

    public_samples_list: list[CompileDecisionSample] = []
    for item in compiled.source_decisions:
        if len(public_samples_list) >= limits.maximum_decision_samples:
            break
        public_samples_list.append(
            CompileDecisionSample(
                decision_kind=CompileDecisionSampleKind.SOURCE,
                source_kind=item.source_kind.value,
                current_turn=None,
                selected_mode=(
                    None if item.selected_mode is None else item.selected_mode.value
                ),
                included=item.included,
                reason_code=item.reason_code,
            )
        )
    if len(public_samples_list) < limits.maximum_decision_samples:
        for item in compiled.tool_result_decisions:
            if len(public_samples_list) >= limits.maximum_decision_samples:
                break
            public_samples_list.append(
                CompileDecisionSample(
                    decision_kind=CompileDecisionSampleKind.TOOL_RESULT,
                    source_kind=None,
                    current_turn=item.current_turn,
                    selected_mode=item.selected_mode.value,
                    included=None,
                    reason_code=item.reason_code,
                )
            )
    public_samples = tuple(public_samples_list)
    total_decisions = len(compiled.source_decisions) + len(
        compiled.tool_result_decisions
    )
    report = compiled.budget_report
    return ModelInputCompileOperationalProjection(
        disposition=CompileObservationDisposition.COMPILED,
        compiler_contract_version=report.compiler_contract_version,
        model_call_index=model_call_index,
        target_fingerprint=report.target_fingerprint,
        tool_surface_fingerprint=report.tool_surface_fingerprint,
        effective_input_budget_tokens=report.effective_input_budget_tokens,
        total_input_tokens=report.total_input_tokens,
        system_tokens=report.system_tokens,
        message_tokens=report.message_tokens,
        tool_tokens=report.tool_tokens,
        envelope_tokens=report.envelope_tokens,
        degraded_source_count=report.degraded_source_count,
        omitted_source_count=report.omitted_source_count,
        degraded_tool_result_count=report.degraded_tool_result_count,
        omitted_tool_result_body_count=report.omitted_tool_result_body_count,
        diagnostic_codes=compiled.diagnostic_codes,
        decision_digest=report.decision_digest,
        decision_samples=public_samples,
        decision_omitted_count=total_decisions - len(public_samples),
    )


__all__ = [
    "CompileDecisionSample",
    "CompileDecisionSampleKind",
    "CompileObservationDisposition",
    "ModelInputCompileOperationalProjection",
    "project_model_input_compile_observation",
]
