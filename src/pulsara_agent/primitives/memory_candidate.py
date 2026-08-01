"""Typed memory write candidates.

A ``MemoryCandidate`` is the input contract to the durable-memory write path.
Tools, post-run extractors, and future UI review flows all produce one of these
typed candidates; ``MemoryWriteService`` dispatches by ``kind`` to the matching
``CanonicalMemoryLedger.submit_*`` method. Modeling each memory type as its
own class keeps type-specific constraints (ActionBoundary requires
``applies_when``/``do_not_apply_when``; Decision carries ``based_on_ids``) at the
schema boundary instead of deferring them to runtime dispatch.
"""

from __future__ import annotations

import unicodedata
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pulsara_agent.ontology import memory
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    build_frozen_fact,
    register_durable_fact,
)


class MemoryCandidateSemanticFact(FrozenFactBase):
    schema_version: Literal["memory_candidate_semantic.v2"] = (
        "memory_candidate_semantic.v2"
    )
    kind: Literal["Claim", "Preference", "Observation", "ActionBoundary", "Decision"]
    scope: str = Field(min_length=1)
    normalized_statement: str = Field(min_length=1)
    semantic_fingerprint: str = Field(min_length=1)


register_durable_fact(
    schema_version="memory_candidate_semantic.v2",
    own_fingerprint_field="semantic_fingerprint",
    domain_separator="memory-candidate-semantic:v2",
)


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
    trigger_tools: tuple[str, ...] = ()
    trigger_actions: tuple[str, ...] = ()
    trigger_file_globs: tuple[str, ...] = ()
    trigger_scopes: tuple[str, ...] = ()
    trigger_keywords: tuple[str, ...] = ()
    negative_tools: tuple[str, ...] = ()
    negative_actions: tuple[str, ...] = ()
    negative_file_globs: tuple[str, ...] = ()


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


class ValidCandidatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_kind: Literal["valid"] = "valid"
    candidate: MemoryCandidate


class InvalidAttemptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_kind: Literal["invalid"] = "invalid"
    attempted_tool_name: str
    attempted_kind: str | None = None
    raw_arguments: dict[str, Any]
    validation_error: str


CandidatePayload = Annotated[
    ValidCandidatePayload | InvalidAttemptPayload,
    Field(discriminator="payload_kind"),
]


_CANDIDATE_PAYLOAD_ADAPTER = TypeAdapter(CandidatePayload)


def normalize_memory_candidate_statement(statement: str) -> str:
    normalized = unicodedata.normalize(
        "NFC", statement.replace("\r\n", "\n").replace("\r", "\n")
    )
    return normalized.strip()


def build_memory_candidate_semantic(
    *,
    kind: str,
    scope: str,
    statement: str,
) -> MemoryCandidateSemanticFact:
    normalized = normalize_memory_candidate_statement(statement)
    if not normalized:
        raise ValueError("memory candidate semantic statement is empty")
    return build_frozen_fact(
        MemoryCandidateSemanticFact,
        schema_version="memory_candidate_semantic.v2",
        kind=kind,
        scope=scope,
        normalized_statement=normalized,
    )


def memory_candidate_semantic_fingerprint(
    *,
    kind: str,
    scope: str,
    statement: str,
) -> str:
    """Return the sole exact-duplicate identity for a candidate-shaped fact."""

    return build_memory_candidate_semantic(
        kind=kind,
        scope=scope,
        statement=statement,
    ).semantic_fingerprint


def candidate_payload_semantic(
    payload: CandidatePayload | dict[str, Any],
) -> MemoryCandidateSemanticFact | None:
    parsed = _CANDIDATE_PAYLOAD_ADAPTER.validate_python(payload)
    if not isinstance(parsed, ValidCandidatePayload):
        return None
    candidate = parsed.candidate
    return build_memory_candidate_semantic(
        kind=candidate.kind,
        scope=candidate.scope,
        statement=candidate.statement,
    )
