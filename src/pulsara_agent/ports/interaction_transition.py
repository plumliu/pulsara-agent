"""Closed suspension and resume transition boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.ports.run_execution import (
    PendingInteractionAuthority,
    PendingInteractionIdentity,
    RunActivationIdentity,
    RunOwnerIdentity,
)


class InteractionSuspensionRequest(FrozenRuntimeStateBase):
    owner_identity: RunOwnerIdentity
    activation_identity: RunActivationIdentity
    authority: PendingInteractionAuthority = Field(
        discriminator="interaction_kind"
    )
    expected_termination_revision: int = Field(ge=0)
    stable_candidate_fingerprint: str = Field(min_length=1)


class InteractionResumeRequest(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    transition_attempt_id: str = Field(min_length=1)
    owner_identity: RunOwnerIdentity
    pending_interaction_identity: PendingInteractionIdentity
    resolution_kind: Literal[
        "approval", "plan_question", "plan_exit", "mcp_input_required"
    ]
    resolution_fingerprint: str = Field(min_length=1)
    expected_authority_head_fingerprint: str = Field(min_length=1)
    expected_termination_revision: int = Field(ge=0)
    stable_candidate_id: str = Field(min_length=1)
    stable_candidate_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "InteractionResumeRequest":
        if self.pending_interaction_identity.owner_identity != self.owner_identity:
            raise ValueError("interaction resume request owner mismatch")
        expected = context_fingerprint(
            "interaction-resume-request:v1",
            self.model_dump(
                mode="json",
                exclude={"stable_candidate_fingerprint"},
            ),
        )
        if self.stable_candidate_fingerprint != expected:
            raise ValueError("interaction resume candidate fingerprint mismatch")
        return self


class InteractionTransitionFull(FrozenRuntimeStateBase):
    disposition: Literal["full"] = "full"
    stable_candidate_id: str = Field(min_length=1)
    stable_candidate_fingerprint: str = Field(min_length=1)
    source_event_references: tuple[ContextEventReferenceFact, ...]
    resulting_authority_fingerprint: str = Field(min_length=1)
    resulting_activation_identity: RunActivationIdentity | None


class InteractionTransitionNone(FrozenRuntimeStateBase):
    disposition: Literal["none"] = "none"
    stable_candidate_id: str = Field(min_length=1)
    stable_candidate_fingerprint: str = Field(min_length=1)


class InteractionTransitionUntrusted(FrozenRuntimeStateBase):
    disposition: Literal["unknown", "conflict"]
    stable_candidate_id: str = Field(min_length=1)
    stable_candidate_fingerprint: str = Field(min_length=1)


InteractionSuspensionOutcome: TypeAlias = (
    InteractionTransitionFull
    | InteractionTransitionNone
    | InteractionTransitionUntrusted
)
InteractionResumeOutcome: TypeAlias = InteractionSuspensionOutcome


@dataclass(frozen=True, slots=True)
class InteractionResumeLinkReceipt:
    owner_identity: RunOwnerIdentity
    previous_activation_identity: RunActivationIdentity
    pending_interaction_identity: PendingInteractionIdentity
    resume_boundary_event_reference: ContextEventReferenceFact
    installed_authority_revision_fingerprint: str
    resumed_by_activation_identity: RunActivationIdentity
    link_fingerprint: str

    def __post_init__(self) -> None:
        expected = context_fingerprint(
            "interaction-resume-link:v1",
            {
                "owner_identity": self.owner_identity.model_dump(mode="json"),
                "previous_activation_identity": (
                    self.previous_activation_identity.model_dump(mode="json")
                ),
                "pending_interaction_identity": (
                    self.pending_interaction_identity.model_dump(mode="json")
                ),
                "resume_boundary_event_reference": (
                    self.resume_boundary_event_reference.model_dump(mode="json")
                ),
                "installed_authority_revision_fingerprint": (
                    self.installed_authority_revision_fingerprint
                ),
                "resumed_by_activation_identity": (
                    self.resumed_by_activation_identity.model_dump(mode="json")
                ),
            },
        )
        if self.link_fingerprint != expected:
            raise ValueError("interaction resume link fingerprint mismatch")


def build_interaction_resume_link_receipt(
    *,
    owner_identity: RunOwnerIdentity,
    previous_activation_identity: RunActivationIdentity,
    pending_interaction_identity: PendingInteractionIdentity,
    resume_boundary_event_reference: ContextEventReferenceFact,
    installed_authority_revision_fingerprint: str,
    resumed_by_activation_identity: RunActivationIdentity,
) -> InteractionResumeLinkReceipt:
    payload = {
        "owner_identity": owner_identity.model_dump(mode="json"),
        "previous_activation_identity": previous_activation_identity.model_dump(
            mode="json"
        ),
        "pending_interaction_identity": pending_interaction_identity.model_dump(
            mode="json"
        ),
        "resume_boundary_event_reference": resume_boundary_event_reference.model_dump(
            mode="json"
        ),
        "installed_authority_revision_fingerprint": (
            installed_authority_revision_fingerprint
        ),
        "resumed_by_activation_identity": resumed_by_activation_identity.model_dump(
            mode="json"
        ),
    }
    return InteractionResumeLinkReceipt(
        owner_identity=owner_identity,
        previous_activation_identity=previous_activation_identity,
        pending_interaction_identity=pending_interaction_identity,
        resume_boundary_event_reference=resume_boundary_event_reference,
        installed_authority_revision_fingerprint=(
            installed_authority_revision_fingerprint
        ),
        resumed_by_activation_identity=resumed_by_activation_identity,
        link_fingerprint=context_fingerprint("interaction-resume-link:v1", payload),
    )


class InteractionTransitionPort(Protocol):
    async def suspend(
        self,
        request: InteractionSuspensionRequest,
        *,
        deadline_monotonic: float,
    ) -> InteractionSuspensionOutcome: ...

    async def resume(
        self,
        request: InteractionResumeRequest,
        *,
        deadline_monotonic: float,
    ) -> InteractionResumeOutcome: ...


__all__ = [
    "InteractionResumeLinkReceipt",
    "build_interaction_resume_link_receipt",
    "InteractionResumeOutcome",
    "InteractionResumeRequest",
    "InteractionSuspensionOutcome",
    "InteractionSuspensionRequest",
    "InteractionTransitionPort",
]
