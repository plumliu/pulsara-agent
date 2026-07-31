"""Capability-scoped model execution contracts."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field

from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.primitives.model_call import ResolvedModelTargetFact
from pulsara_agent.ports.run_execution import RunActivationIdentity, RunOwnerIdentity


class ModelExecutionRequest(FrozenRuntimeStateBase):
    owner_identity: RunOwnerIdentity
    activation_identity: RunActivationIdentity
    prepared_context_reference: ContextEventReferenceFact
    prepared_context_fingerprint: str = Field(min_length=1)
    model_target: ResolvedModelTargetFact
    provider_input_generation_revision: int = Field(ge=1)
    model_step_ordinal: int = Field(ge=1)
    model_control_guard_fingerprint: str = Field(min_length=1)
    purpose: Literal["main_agent_step"] = "main_agent_step"


class CommittedModelResultHandle(Protocol):
    @property
    def model_call_id(self) -> str: ...

    @property
    def activation_identity(self) -> RunActivationIdentity: ...

    async def wait_committed_result(self) -> object: ...

    def release(self) -> None: ...


class ModelExecutionPort(Protocol):
    async def dispatch(
        self,
        request: ModelExecutionRequest,
        *,
        deadline_monotonic: float,
    ) -> CommittedModelResultHandle: ...


__all__ = [
    "CommittedModelResultHandle",
    "ModelExecutionPort",
    "ModelExecutionRequest",
]
