"""Capability-scoped context preparation port for one model step."""

from __future__ import annotations

from typing import Literal, Protocol, TypeAlias

from pydantic import Field

from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.ports.run_execution import (
    PendingInteractionAuthority,
    RunActivationIdentity,
    RunOwnerIdentity,
    RunProgressSnapshot,
)


class ContextPreparationRequest(FrozenRuntimeStateBase):
    owner_identity: RunOwnerIdentity
    activation_identity: RunActivationIdentity
    authority_revision: int = Field(ge=1)
    authority_fingerprint: str = Field(min_length=1)
    progress: RunProgressSnapshot
    expected_provider_input_generation_revision: int = Field(ge=0)
    model_step_ordinal: int = Field(ge=1)


class PreparedContext(FrozenRuntimeStateBase):
    outcome_kind: Literal["prepared"] = "prepared"
    context_reference: ContextEventReferenceFact
    context_fingerprint: str = Field(min_length=1)
    provider_input_generation_revision: int = Field(ge=1)


class ContextReplanRequired(FrozenRuntimeStateBase):
    outcome_kind: Literal["replan_required"] = "replan_required"
    reason_code: Literal[
        "authority_revision_stale",
        "provider_input_generation_stale",
        "runtime_notification_superseded",
    ]


class ContextInteractionRequired(FrozenRuntimeStateBase):
    outcome_kind: Literal["interaction_required"] = "interaction_required"
    authority: PendingInteractionAuthority


class ContextReconciliationRequired(FrozenRuntimeStateBase):
    outcome_kind: Literal["reconciliation_required"] = "reconciliation_required"
    stable_owner_fingerprint: str = Field(min_length=1)
    diagnostic_code: str = Field(min_length=1, max_length=128)


ContextPreparationOutcome: TypeAlias = (
    PreparedContext
    | ContextReplanRequired
    | ContextInteractionRequired
    | ContextReconciliationRequired
)


class ContextPreparationPort(Protocol):
    async def prepare(
        self,
        request: ContextPreparationRequest,
        *,
        deadline_monotonic: float,
    ) -> ContextPreparationOutcome: ...


__all__ = [
    "ContextInteractionRequired",
    "ContextPreparationOutcome",
    "ContextPreparationPort",
    "ContextPreparationRequest",
    "ContextReconciliationRequired",
    "ContextReplanRequired",
    "PreparedContext",
]
