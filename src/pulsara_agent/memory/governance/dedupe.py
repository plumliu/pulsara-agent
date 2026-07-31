"""Memory candidate dedupe helpers used by governance."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter

from pulsara_agent.primitives.memory_candidate import (
    MemoryCandidate,
    memory_candidate_semantic_fingerprint,
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
    return memory_candidate_semantic_fingerprint(
        kind=normalized.kind,
        statement=normalized.statement,
        scope=normalized.scope,
    )


def already_exists(
    candidate: MemoryCandidate, graph: GraphStore, *, graph_id: str | None = None
) -> bool:
    term = _KIND_TO_TERM.get(candidate.kind)
    if term is None:
        return False
    expected = candidate_fingerprint(candidate)
    for record in graph.find_by_type(term, graph_id=graph_id):
        if str(record.get(memory.STATUS.name, "")) in {
            memory.NodeStatus.ACTIVE.value,
            memory.NodeStatus.NEEDS_REVIEW.value,
        } and _canonical_record_fingerprint(record, kind=candidate.kind) == expected:
            return True
    return False


def _canonical_record_fingerprint(record: Mapping[str, Any], *, kind: str) -> str:
    statement = record.get(memory.STATEMENT.name)
    scope = record.get(memory.SCOPE.name)
    if not isinstance(statement, str) or not isinstance(scope, str):
        raise ValueError("canonical memory lacks semantic identity fields")
    return memory_candidate_semantic_fingerprint(
        kind=kind,
        scope=scope,
        statement=statement,
    )
