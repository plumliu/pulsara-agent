"""Memory candidate dedupe helpers used by governance."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter

from pulsara_agent.event.candidates import (
    ActionBoundaryCandidate,
    ClaimCandidate,
    DecisionCandidate,
    MemoryCandidate,
    ObservationCandidate,
    PreferenceCandidate,
)
from pulsara_agent.graph import GraphStore
from pulsara_agent.ontology import memory


_CANDIDATE_ADAPTER = TypeAdapter(MemoryCandidate)
_KIND_TO_TERM = {
    "Claim": memory.CLAIM,
    "Preference": memory.PREFERENCE,
    "Observation": memory.OBSERVATION,
    "ActionBoundary": memory.ACTION_BOUNDARY,
    "Decision": memory.DECISION,
}


def candidate_fingerprint(candidate: MemoryCandidate | Mapping[str, Any]) -> str:
    normalized = _CANDIDATE_ADAPTER.validate_python(candidate)
    match normalized:
        case ClaimCandidate() | PreferenceCandidate() | ObservationCandidate():
            parts = (normalized.kind, _normalize(normalized.statement), normalized.scope)
        case ActionBoundaryCandidate():
            parts = (
                normalized.kind,
                _normalize(normalized.statement),
                normalized.scope,
                _normalize(normalized.applies_when),
                _normalize(normalized.do_not_apply_when),
            )
        case DecisionCandidate():
            parts = (
                normalized.kind,
                _normalize(normalized.statement),
                normalized.scope,
                tuple(sorted(normalized.based_on_ids)),
            )
    return json.dumps(parts, ensure_ascii=True, sort_keys=True)


def already_exists(candidate: MemoryCandidate, graph: GraphStore, *, graph_id: str | None = None) -> bool:
    term = _KIND_TO_TERM.get(candidate.kind)
    if term is None:
        return False
    for record in graph.find_by_type(term, graph_id=graph_id):
        if _normalize(record.get(memory.STATEMENT.name)) != _normalize(candidate.statement):
            continue
        if str(record.get(memory.SCOPE.name, "")) != candidate.scope:
            continue
        if str(record.get(memory.STATUS.name, "")) in {
            memory.NodeStatus.ACTIVE.value,
            memory.NodeStatus.NEEDS_REVIEW.value,
        }:
            return True
    return False


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())
