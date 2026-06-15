"""Grounded memory explanations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pulsara_agent.memory.query import CanonicalNodeView
from pulsara_agent.ontology import memory


class ClaimKind(StrEnum):
    EVIDENCE_SUPPORT = "evidence_support"
    SUPERSEDED_BY = "superseded_by"
    SUPERSEDES = "supersedes"
    CONTRADICTED_BY = "contradicted_by"
    SCOPE_MATCH = "scope_match"
    TYPE_MATCH = "type_match"
    FTS_HIT = "fts_hit"
    LEXICAL_HIT = "lexical_hit"


@dataclass(frozen=True, slots=True)
class ExplanationClaim:
    text: str
    kind: ClaimKind
    grounded_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Explanation:
    memory_id: str
    claims: tuple[ExplanationClaim, ...]


def explain_memory(
    view: CanonicalNodeView,
    *,
    signals: tuple[str, ...] = (),
) -> Explanation:
    claims: list[ExplanationClaim] = []
    signal_set = set(signals)
    if "lexical" in signal_set:
        claims.append(
            ExplanationClaim(
                text="Lexical recall matched this memory.",
                kind=ClaimKind.LEXICAL_HIT,
                grounded_on=("signal:lexical",),
            )
        )
    if "fts" in signal_set:
        claims.append(
            ExplanationClaim(
                text="Full-text recall matched this memory.",
                kind=ClaimKind.FTS_HIT,
                grounded_on=("signal:fts",),
            )
        )
    claims.append(
        ExplanationClaim(
            text=f"Memory scope is {view.scope}.",
            kind=ClaimKind.SCOPE_MATCH,
            grounded_on=(f"field:{view.id}:scope",),
        )
    )
    claims.append(
        ExplanationClaim(
            text=f"Memory type is {view.memory_type}.",
            kind=ClaimKind.TYPE_MATCH,
            grounded_on=(f"field:{view.id}:memory_type",),
        )
    )
    for predicate, source_id in view.incoming:
        if predicate == memory.SUPPORTS.name:
            claims.append(
                ExplanationClaim(
                    text="This memory has materialized supporting evidence.",
                    kind=ClaimKind.EVIDENCE_SUPPORT,
                    grounded_on=(_edge_id(source_id, predicate, view.id),),
                )
            )
        elif predicate == memory.SUPERSEDES.name:
            claims.append(
                ExplanationClaim(
                    text="This memory is superseded by a newer materialized memory.",
                    kind=ClaimKind.SUPERSEDED_BY,
                    grounded_on=(_edge_id(source_id, predicate, view.id),),
                )
            )
        elif predicate == memory.CONTRADICTS.name:
            claims.append(
                ExplanationClaim(
                    text="This memory has a materialized contradiction edge.",
                    kind=ClaimKind.CONTRADICTED_BY,
                    grounded_on=(_edge_id(source_id, predicate, view.id),),
                )
            )
    for predicate, target_id in view.outgoing:
        if predicate == memory.SUPERSEDES.name:
            claims.append(
                ExplanationClaim(
                    text="This memory supersedes an older materialized memory.",
                    kind=ClaimKind.SUPERSEDES,
                    grounded_on=(_edge_id(view.id, predicate, target_id),),
                )
            )
        elif predicate == memory.CONTRADICTS.name:
            claims.append(
                ExplanationClaim(
                    text="This memory has a materialized contradiction edge.",
                    kind=ClaimKind.CONTRADICTED_BY,
                    grounded_on=(_edge_id(view.id, predicate, target_id),),
                )
            )
    explanation = Explanation(memory_id=view.id, claims=tuple(claims))
    validate_explanation(explanation, view=view, signals=signals)
    return explanation


def validate_explanation(
    explanation: Explanation,
    *,
    view: CanonicalNodeView,
    signals: tuple[str, ...] = (),
) -> None:
    allowed = {
        f"field:{view.id}:scope",
        f"field:{view.id}:memory_type",
        *(f"signal:{signal}" for signal in signals),
    }
    allowed.update(_edge_id(source_id, predicate, view.id) for predicate, source_id in view.incoming)
    allowed.update(_edge_id(view.id, predicate, target_id) for predicate, target_id in view.outgoing)
    for claim in explanation.claims:
        missing = [grounding for grounding in claim.grounded_on if grounding not in allowed]
        if missing:
            raise ValueError(f"ungrounded explanation claim for {explanation.memory_id}: {missing}")


def explanation_to_payload(explanation: Explanation) -> dict:
    return {
        "memory_id": explanation.memory_id,
        "claims": [
            {
                "text": claim.text,
                "kind": claim.kind.value,
                "grounded_on": list(claim.grounded_on),
            }
            for claim in explanation.claims
        ],
    }


def _edge_id(source_id: str, predicate: str, target_id: str) -> str:
    return f"rel:{source_id}|{predicate}|{target_id}"
