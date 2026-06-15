"""Canonical memory lifecycle mutations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pulsara_agent.event import (
    AgentEvent,
    MemoryMaintenanceAppliedEvent,
    MemoryMarkedStaleEvent,
    MemorySupersededEvent,
)
from pulsara_agent.event.candidates import MemoryCandidate
from pulsara_agent.graph import GraphStore, MutableCanonicalMemoryStore
from pulsara_agent.jsonld import Term
from pulsara_agent.memory.candidate_pool import governance_batch_context
from pulsara_agent.ontology import memory


@dataclass(slots=True)
class MemoryLifecycle:
    graph: GraphStore
    mutable: MutableCanonicalMemoryStore

    def supersede(
        self,
        *,
        old_id: str,
        new_id: str,
        governance_batch_id: str,
        graph_id: str | None = None,
    ) -> list[AgentEvent]:
        """Mark ``old_id`` superseded by ``new_id`` and materialize the edge on ``new_id``."""

        updated_at = _now()
        old_doc = self.graph.get_jsonld(old_id, graph_id=graph_id)
        new_doc = self.graph.get_jsonld(new_id, graph_id=graph_id)
        _append_node_ref(new_doc, memory.SUPERSEDES.name, old_id)
        self.graph.put_jsonld(new_doc, graph_id=graph_id)
        self.mutable.set_status(
            old_id,
            memory.NodeStatus.SUPERSEDED,
            updated_at=updated_at,
            graph_id=graph_id,
        )
        ctx = governance_batch_context(governance_batch_id)
        return [
            MemorySupersededEvent(
                **ctx.event_fields(),
                **_memory_event_fields(old_doc),
                memory_id=old_id,
                superseded_by=new_id,
            )
        ]

    def mark_stale(
        self,
        *,
        node_id: str,
        governance_batch_id: str,
        graph_id: str | None = None,
    ) -> list[AgentEvent]:
        updated_at = _now()
        document = self.graph.get_jsonld(node_id, graph_id=graph_id)
        self.mutable.set_status(
            node_id,
            memory.NodeStatus.STALE,
            updated_at=updated_at,
            graph_id=graph_id,
        )
        ctx = governance_batch_context(governance_batch_id)
        return [
            MemoryMarkedStaleEvent(
                **ctx.event_fields(),
                **_memory_event_fields(document),
                memory_id=node_id,
            )
        ]

    def mark_contradicted(
        self,
        *,
        left_id: str,
        right_id: str,
        governance_batch_id: str,
        graph_id: str | None = None,
    ) -> list[AgentEvent]:
        """Materialize a symmetric contradiction and mark both nodes contradicted."""

        updated_at = _now()
        left_doc = self.graph.get_jsonld(left_id, graph_id=graph_id)
        right_doc = self.graph.get_jsonld(right_id, graph_id=graph_id)
        _append_node_ref(left_doc, memory.CONTRADICTS.name, right_id)
        _append_node_ref(right_doc, memory.CONTRADICTS.name, left_id)
        self.graph.put_jsonld(left_doc, graph_id=graph_id)
        self.graph.put_jsonld(right_doc, graph_id=graph_id)
        self.mutable.set_status(
            left_id,
            memory.NodeStatus.CONTRADICTED,
            updated_at=updated_at,
            graph_id=graph_id,
        )
        self.mutable.set_status(
            right_id,
            memory.NodeStatus.CONTRADICTED,
            updated_at=updated_at,
            graph_id=graph_id,
        )
        ctx = governance_batch_context(governance_batch_id)
        return [
            MemoryMaintenanceAppliedEvent(
                **ctx.event_fields(),
                **_memory_event_fields(left_doc),
                proposal_id=f"{governance_batch_id}:contradiction:{left_id}",
                target_memory_id=left_id,
                action=f"mark_contradicted_with:{right_id}",
            ),
            MemoryMaintenanceAppliedEvent(
                **ctx.event_fields(),
                **_memory_event_fields(right_doc),
                proposal_id=f"{governance_batch_id}:contradiction:{right_id}",
                target_memory_id=right_id,
                action=f"mark_contradicted_with:{left_id}",
            ),
        ]

    def supersede_matching_existing(
        self,
        *,
        candidate: MemoryCandidate,
        new_memory_id: str,
        governance_batch_id: str,
        graph_id: str | None = None,
    ) -> list[AgentEvent]:
        """Supersede active memories that share candidate type/scope but differ in statement."""

        events: list[AgentEvent] = []
        for old_id in _matching_existing_memory_ids(self.graph, candidate, graph_id=graph_id):
            if old_id == new_memory_id:
                continue
            events.extend(
                self.supersede(
                    old_id=old_id,
                    new_id=new_memory_id,
                    governance_batch_id=governance_batch_id,
                    graph_id=graph_id,
                )
            )
        return events


def _matching_existing_memory_ids(
    graph: GraphStore,
    candidate: MemoryCandidate,
    *,
    graph_id: str | None,
) -> Sequence[str]:
    type_name = _candidate_type_term(candidate)
    matches: list[str] = []
    normalized_statement = _normalize(candidate.statement)
    for document in graph.find_by_type(type_name, graph_id=graph_id):
        if document.get(memory.STATUS.name) != memory.NodeStatus.ACTIVE.value:
            continue
        if document.get(memory.SCOPE.name) != candidate.scope:
            continue
        if _normalize(str(document.get(memory.STATEMENT.name, ""))) == normalized_statement:
            continue
        node_id = document.get("@id")
        if isinstance(node_id, str):
            matches.append(node_id)
    return tuple(matches)


def _candidate_type_term(candidate: MemoryCandidate) -> Term:
    return {
        "Claim": memory.CLAIM,
        "Preference": memory.PREFERENCE,
        "Observation": memory.OBSERVATION,
        "ActionBoundary": memory.ACTION_BOUNDARY,
        "Decision": memory.DECISION,
    }[candidate.kind]


def _memory_event_fields(document: dict[str, Any]) -> dict[str, str]:
    return {
        "scope": str(document[memory.SCOPE.name]),
        "memory_type": _document_memory_type(document),
        "statement": str(document.get(memory.STATEMENT.name) or ""),
    }


def _document_memory_type(document: dict[str, Any]) -> str:
    types = document.get("@type")
    values = types if isinstance(types, list) else [types]
    for value in values:
        if value in {
            memory.CLAIM.name,
            memory.DECISION.name,
            memory.PREFERENCE.name,
            memory.ACTION_BOUNDARY.name,
            memory.OBSERVATION.name,
        }:
            return str(value)
    raise ValueError(f"document is not a canonical memory node: {document.get('@id')!r}")


def _append_node_ref(document: dict[str, Any], predicate: str, target_id: str) -> None:
    values = _as_list(document.get(predicate))
    node_ref = {"@id": target_id}
    if node_ref not in values:
        values.append(node_ref)
    document[predicate] = values


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _now() -> datetime:
    return datetime.now(timezone.utc)
