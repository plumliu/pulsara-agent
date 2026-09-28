"""Non-secret execution choice saved on a task; no runtime clients or credentials."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from pulsara_agent.llm.model_catalog import (
    ReasoningControlContract, ReasoningFixedOn, ReasoningProviderDefault,
    ReasoningSelectableControls, ReasoningUnavailable,
)
from pulsara_agent.primitives.model_call import ResolvedModelTargetFact


class FrozenReasoningContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["selectable", "fixed_on", "unavailable", "provider_default"]
    controls: ReasoningSelectableControls | None = None

    @model_validator(mode="after")
    def _closed_variant(self):
        if (self.kind == "selectable") != (self.controls is not None):
            raise ValueError("only selectable reasoning carries controls")
        return self

    @classmethod
    def freeze(cls, value: ReasoningControlContract) -> "FrozenReasoningContract":
        if isinstance(value, ReasoningSelectableControls):
            return cls(kind="selectable", controls=value)
        if isinstance(value, ReasoningFixedOn):
            return cls(kind="fixed_on")
        if isinstance(value, ReasoningUnavailable):
            return cls(kind="unavailable")
        if isinstance(value, ReasoningProviderDefault):
            return cls(kind="provider_default")
        raise TypeError("unsupported reasoning contract")


class FrozenSubagentModelTarget(ResolvedModelTargetFact):
    # The ordinary target fact records the wire profile, but not the model's
    # reasoning controls. Both values must survive queueing and restart.
    reasoning_contract: FrozenReasoningContract

    @classmethod
    def freeze(
        cls, fact: ResolvedModelTargetFact, reasoning: ReasoningControlContract,
    ) -> "FrozenSubagentModelTarget":
        return cls(**fact.model_dump(), reasoning_contract=FrozenReasoningContract.freeze(reasoning))
