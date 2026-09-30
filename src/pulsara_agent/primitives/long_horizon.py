"""Process-local observation rollup renderer contract."""

from __future__ import annotations

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
    "ObservationRollupRendererContractFact",
    "default_observation_rollup_renderer_contract",
]
