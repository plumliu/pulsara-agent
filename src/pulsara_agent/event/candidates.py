"""Typed memory write candidates.

A ``MemoryCandidate`` is the input contract to the durable-memory write path.
Tools, post-run extractors, and future UI review flows all produce one of these
typed candidates; ``MemoryWriteService`` dispatches by ``kind`` to the matching
``ExecutionEvidenceLedger.submit_*`` method. Modeling each memory type as its
own class keeps type-specific constraints (ActionBoundary requires
``applies_when``/``do_not_apply_when``; Decision carries ``based_on_ids``) at the
schema boundary instead of deferring them to runtime dispatch.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from pulsara_agent.ontology import memory


class MemoryCandidateBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    statement: str
    scope: str
    evidence_ids: tuple[str, ...] = ()
    source_authority: memory.SourceAuthority
    verification_status: memory.VerificationStatus


class ClaimCandidate(MemoryCandidateBase):
    kind: Literal["Claim"] = "Claim"


class PreferenceCandidate(MemoryCandidateBase):
    kind: Literal["Preference"] = "Preference"


class ObservationCandidate(MemoryCandidateBase):
    kind: Literal["Observation"] = "Observation"


class ActionBoundaryCandidate(MemoryCandidateBase):
    kind: Literal["ActionBoundary"] = "ActionBoundary"
    applies_when: str
    do_not_apply_when: str


class DecisionCandidate(MemoryCandidateBase):
    kind: Literal["Decision"] = "Decision"
    based_on_ids: tuple[str, ...] = ()


MemoryCandidate = Annotated[
    ClaimCandidate
    | PreferenceCandidate
    | ObservationCandidate
    | ActionBoundaryCandidate
    | DecisionCandidate,
    Field(discriminator="kind"),
]
