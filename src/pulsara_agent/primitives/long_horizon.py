"""Process-local tool policy contracts retained by the Kernel catalog."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulsara_agent.primitives._context_base import context_fingerprint


class FrozenLongHorizonFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _validate_fingerprint(
    model: FrozenLongHorizonFact, *, namespace: str, field_name: str
) -> None:
    expected = context_fingerprint(
        namespace, model.model_dump(mode="json", exclude={field_name})
    )
    if getattr(model, field_name) != expected:
        raise ValueError(f"{field_name} mismatch")


class RolloutPhase(StrEnum):
    EXPLORATION = "exploration"
    WARNING = "warning"
    RESTRICTED = "restricted"
    FINALIZATION_ONLY = "finalization_only"
    EXHAUSTED = "exhausted"
    EMERGENCY_HARD_STOP = "emergency_hard_stop"


class LongHorizonActionClass(StrEnum):
    EVIDENCE_ACQUISITION = "evidence_acquisition"
    EVIDENCE_HYDRATION = "evidence_hydration"
    SYNTHESIS_MUTATION = "synthesis_mutation"
    BOUNDED_VERIFICATION = "bounded_verification"
    USER_INTERACTION = "user_interaction"
    PROCESS_CONTROL = "process_control"
    EXTERNAL_ACTION = "external_action"


class ToolActionClassifierContractFact(FrozenLongHorizonFact):
    schema_version: Literal["tool_action_classifier_contract.v1"] = (
        "tool_action_classifier_contract.v1"
    )
    classifier_id: str = Field(min_length=1)
    classifier_version: str = Field(min_length=1)
    input_schema_fingerprint: str = Field(min_length=1)
    output_schema_fingerprint: str = Field(min_length=1)
    classification_policy_fingerprint: str = Field(min_length=1)
    contract_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _contract(self) -> "ToolActionClassifierContractFact":
        _validate_fingerprint(
            self,
            namespace="tool-action-classifier-contract:v1",
            field_name="contract_fingerprint",
        )
        return self


class LongHorizonToolPolicyFact(FrozenLongHorizonFact):
    schema_version: Literal["long_horizon_tool_policy.v1"] = (
        "long_horizon_tool_policy.v1"
    )
    allowed_action_classes: tuple[LongHorizonActionClass, ...]
    max_rollout_cost_units: int = Field(ge=0)
    allowed_in_phases: tuple[RolloutPhase, ...]
    action_classifier_contract: ToolActionClassifierContractFact
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _policy(self) -> "LongHorizonToolPolicyFact":
        if not self.allowed_action_classes or len(self.allowed_action_classes) != len(
            set(self.allowed_action_classes)
        ):
            raise ValueError("tool policy action classes must be non-empty and unique")
        if not self.allowed_in_phases or len(self.allowed_in_phases) != len(
            set(self.allowed_in_phases)
        ):
            raise ValueError("tool policy phases must be non-empty and unique")
        _validate_fingerprint(
            self,
            namespace="long-horizon-tool-policy:v1",
            field_name="policy_fingerprint",
        )
        return self


class ToolActionClassificationFact(FrozenLongHorizonFact):
    schema_version: Literal["tool_action_classification.v1"] = (
        "tool_action_classification.v1"
    )
    tool_call_id: str = Field(min_length=1)
    descriptor_id: str = Field(min_length=1)
    descriptor_fingerprint: str = Field(min_length=1)
    action_class: LongHorizonActionClass
    rollout_cost_units: int = Field(ge=0)
    normalized_action_fingerprint: str = Field(min_length=1)
    classifier_id: str = Field(min_length=1)
    classifier_version: str = Field(min_length=1)
    classifier_contract_fingerprint: str = Field(min_length=1)
    classification_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _classification(self) -> "ToolActionClassificationFact":
        _validate_fingerprint(
            self,
            namespace="tool-action-classification:v1",
            field_name="classification_fingerprint",
        )
        return self


class ObservationRollupRendererContractFact(FrozenLongHorizonFact):
    schema_version: Literal["observation_rollup_renderer_contract.v1"] = (
        "observation_rollup_renderer_contract.v1"
    )
    renderer_id: str = Field(min_length=1)
    renderer_version: str = Field(min_length=1)
    input_schema_fingerprint: str = Field(min_length=1)
    output_schema_fingerprint: str = Field(min_length=1)
    framing_policy_fingerprint: str = Field(min_length=1)
    placement_contract_fingerprint: str = Field(min_length=1)
    renderer_contract_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _contract(self) -> "ObservationRollupRendererContractFact":
        _validate_fingerprint(
            self,
            namespace="observation-rollup-renderer-contract:v1",
            field_name="renderer_contract_fingerprint",
        )
        return self


def default_observation_rollup_renderer_contract() -> (
    ObservationRollupRendererContractFact
):
    payload = {
        "schema_version": "observation_rollup_renderer_contract.v1",
        "renderer_id": "pulsara.observation_rollup.canonical",
        "renderer_version": "v1",
        "input_schema_fingerprint": "schema:tool-result-rollup-semantics:v1",
        "output_schema_fingerprint": "schema:observation-rollup:v1",
        "framing_policy_fingerprint": context_fingerprint(
            "observation-rollup-framing-policy:v1",
            {"format": "canonical_markdown", "bounded_evidence": True},
        ),
        "placement_contract_fingerprint": context_fingerprint(
            "observation-rollup-placement-policy:v1",
            {"placement": "after_complete_pair_group"},
        ),
    }
    return ObservationRollupRendererContractFact(
        **payload,
        renderer_contract_fingerprint=context_fingerprint(
            "observation-rollup-renderer-contract:v1", payload
        ),
    )


__all__ = [
    "LongHorizonActionClass",
    "LongHorizonToolPolicyFact",
    "ObservationRollupRendererContractFact",
    "RolloutPhase",
    "ToolActionClassificationFact",
    "ToolActionClassifierContractFact",
    "default_observation_rollup_renderer_contract",
]
