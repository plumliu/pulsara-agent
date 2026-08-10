"""Declarative result-render contracts for the process-local tool catalog."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import FrozenContextFact, context_fingerprint


class ToolResultStateFact(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    DENIED = "denied"


class ToolResultOperationalKind(StrEnum):
    GENERIC = "generic"
    TERMINAL_COMMAND = "terminal_command"
    TERMINAL_COMMAND_ERROR = "terminal_command_error"
    TERMINAL_PROCESS_OBSERVATION = "terminal_process_observation"
    TERMINAL_PROCESS_INVENTORY = "terminal_process_inventory"
    TERMINAL_PROCESS_ERROR = "terminal_process_error"
    ARTIFACT = "artifact"


class ToolResultEssentialEnvelopeKind(StrEnum):
    NONE = "none"
    TERMINAL_COMMAND = "terminal_command"
    TERMINAL_COMMAND_ERROR = "terminal_command_error"
    TERMINAL_PROCESS_OBSERVATION = "terminal_process_observation"
    TERMINAL_PROCESS_INVENTORY = "terminal_process_inventory"
    TERMINAL_PROCESS_ERROR = "terminal_process_error"
    ARTIFACT = "artifact"


class ToolResultRenderVariantCode(StrEnum):
    GENERIC_RESULT = "generic_result"
    GENERIC_DENIED = "generic_denied"
    TERMINAL_COMMAND_EXECUTED = "terminal_command_executed"
    TERMINAL_COMMAND_MALFORMED_ARGUMENTS = "terminal_command_malformed_arguments"
    TERMINAL_COMMAND_DENIED = "terminal_command_denied"
    TERMINAL_COMMAND_ADAPTER_ERROR = "terminal_command_adapter_error"
    TERMINAL_PROCESS_INVENTORY = "terminal_process_inventory"
    TERMINAL_PROCESS_OBSERVATION = "terminal_process_observation"
    TERMINAL_PROCESS_ERROR = "terminal_process_error"
    TERMINAL_PROCESS_ADAPTER_ERROR = "terminal_process_adapter_error"
    EXTERNAL_GENERIC_RESULT = "external_generic_result"
    EXTERNAL_TERMINAL_RESULT = "external_terminal_result"


def _validate_fingerprint(
    model: FrozenContextFact, namespace: str, field_name: str
) -> None:
    expected = context_fingerprint(
        namespace, model.model_dump(mode="json", exclude={field_name})
    )
    if getattr(model, field_name) != expected:
        raise ValueError(f"{field_name} mismatch")


class CapabilityResultRenderVariantFact(FrozenContextFact):
    variant_code: ToolResultRenderVariantCode
    operational_kind: ToolResultOperationalKind
    essential_envelope_kind: ToolResultEssentialEnvelopeKind
    allowed_result_states: tuple[ToolResultStateFact, ...]
    execution_phase: Literal["pre_execution", "executed", "post_execution"]
    terminal_payload_timing_requirement: Literal["required", "optional", "forbidden"]
    variant_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _variant(self) -> "CapabilityResultRenderVariantFact":
        values = tuple(state.value for state in self.allowed_result_states)
        if not values or values != tuple(sorted(set(values))):
            raise ValueError("variant result states must be sorted and unique")
        _validate_fingerprint(
            self, "tool-result-render-variant:v1", "variant_fingerprint"
        )
        return self


class ToolResultSemanticsBuilderContractFact(FrozenContextFact):
    schema_version: Literal["tool-result-semantics-builder-contract:v1"] = (
        "tool-result-semantics-builder-contract:v1"
    )
    builder_id: str = Field(min_length=1)
    builder_version: str = Field(min_length=1)
    input_schema_fingerprints: tuple[str, ...]
    output_schema_fingerprint: str = Field(min_length=1)
    variant_table_fingerprint: str = Field(min_length=1)
    classifier_policy_fingerprint: str = Field(min_length=1)
    normalization_contract_versions: tuple[str, ...]
    contract_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _contract(self) -> "ToolResultSemanticsBuilderContractFact":
        if len(self.input_schema_fingerprints) != 6:
            raise ValueError("semantics builder requires six input schemas")
        if not self.normalization_contract_versions:
            raise ValueError("semantics builder normalization versions are required")
        _validate_fingerprint(
            self, "tool-result-semantics-builder-contract:v1", "contract_fingerprint"
        )
        return self


class CapabilityResultRenderContractFact(FrozenContextFact):
    allowed_operational_kinds: tuple[ToolResultOperationalKind, ...]
    allowed_essential_envelope_kinds: tuple[ToolResultEssentialEnvelopeKind, ...]
    allowed_variants: tuple[CapabilityResultRenderVariantFact, ...]
    semantics_builder_id: str = Field(min_length=1)
    semantics_builder_version: str = Field(min_length=1)
    semantics_builder_contract: ToolResultSemanticsBuilderContractFact
    semantics_builder_contract_fingerprint: str = Field(min_length=1)
    rollup_renderer_id: str = Field(min_length=1)
    rollup_renderer_version: str = Field(min_length=1)
    rollup_renderer_contract_fingerprint: str = Field(min_length=1)
    pre_execution_denial_variant_code: ToolResultRenderVariantCode
    contract_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _contract(self) -> "CapabilityResultRenderContractFact":
        codes = tuple(variant.variant_code for variant in self.allowed_variants)
        if not codes or len(codes) != len(set(codes)):
            raise ValueError("render variants must be non-empty and unique")
        operational = tuple(
            sorted({variant.operational_kind for variant in self.allowed_variants}, key=str)
        )
        essential = tuple(
            sorted(
                {variant.essential_envelope_kind for variant in self.allowed_variants},
                key=str,
            )
        )
        if self.allowed_operational_kinds != operational:
            raise ValueError("allowed operational kinds drifted")
        if self.allowed_essential_envelope_kinds != essential:
            raise ValueError("allowed essential kinds drifted")
        builder = self.semantics_builder_contract
        if (self.semantics_builder_id, self.semantics_builder_version) != (
            builder.builder_id,
            builder.builder_version,
        ):
            raise ValueError("render builder identity mismatch")
        if self.semantics_builder_contract_fingerprint != builder.contract_fingerprint:
            raise ValueError("render builder fingerprint mismatch")
        if builder.variant_table_fingerprint != context_fingerprint(
            "tool-result-variant-table:v1",
            [variant.model_dump(mode="json") for variant in self.allowed_variants],
        ):
            raise ValueError("builder variant table mismatch")
        denial = next(
            (
                variant
                for variant in self.allowed_variants
                if variant.variant_code == self.pre_execution_denial_variant_code
            ),
            None,
        )
        if (
            denial is None
            or denial.execution_phase != "pre_execution"
            or denial.terminal_payload_timing_requirement != "forbidden"
        ):
            raise ValueError("pre-execution denial variant is invalid")
        if not set(denial.allowed_result_states).issubset(
            {ToolResultStateFact.DENIED, ToolResultStateFact.ERROR}
        ):
            raise ValueError("denial variant has an invalid result state")
        _validate_fingerprint(
            self, "capability-result-render-contract:v1", "contract_fingerprint"
        )
        return self


__all__ = [
    "CapabilityResultRenderContractFact",
    "CapabilityResultRenderVariantFact",
    "ToolResultEssentialEnvelopeKind",
    "ToolResultOperationalKind",
    "ToolResultRenderVariantCode",
    "ToolResultSemanticsBuilderContractFact",
    "ToolResultStateFact",
]
