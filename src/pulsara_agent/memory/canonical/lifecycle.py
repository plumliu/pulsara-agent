"""Canonical memory lifecycle mutations.

This module now models lifecycle changes in two phases:

1. build the final JSON-LD document set for a logical mutation
2. apply that final-doc set to the underlying graph inside the caller's UoW

That keeps the external API stable while making the write shape closer to the
mutation-journal / final-doc-set architecture described in the migration plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pulsara_agent.event import (
    AgentEvent,
    MemoryContradictionLinkedEvent,
    MemoryMarkedStaleEvent,
    MemorySupersededEvent,
)
from pulsara_agent.graph import GraphStore, MutableCanonicalMemoryStore
from pulsara_agent.memory.candidates.pool import governance_batch_context
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
        mutation = self.build_supersede_mutation(
            old_id=old_id,
            new_id=new_id,
            updated_at=updated_at,
            graph_id=graph_id,
        )
        self.apply_mutation(mutation, graph_id=graph_id)
        ctx = governance_batch_context(governance_batch_id)
        return [
            MemorySupersededEvent(
                **ctx.event_fields(),
                **_memory_event_fields(mutation.event_documents[old_id]),
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
        mutation = self.build_mark_stale_mutation(
            node_id=node_id,
            updated_at=updated_at,
            graph_id=graph_id,
        )
        self.apply_mutation(mutation, graph_id=graph_id)
        ctx = governance_batch_context(governance_batch_id)
        return [
            MemoryMarkedStaleEvent(
                **ctx.event_fields(),
                **_memory_event_fields(mutation.event_documents[node_id]),
                memory_id=node_id,
            )
        ]

    def link_contradiction(
        self,
        *,
        left_id: str,
        right_id: str,
        governance_batch_id: str,
        graph_id: str | None = None,
    ) -> list[AgentEvent]:
        """Materialize a symmetric contradiction edge without changing node status."""

        mutation = self.build_contradiction_mutation(
            left_id=left_id,
            right_id=right_id,
            graph_id=graph_id,
        )
        self.apply_mutation(mutation, graph_id=graph_id)
        ctx = governance_batch_context(governance_batch_id)
        return [
            MemoryContradictionLinkedEvent(
                **ctx.event_fields(),
                **_memory_event_fields(mutation.event_documents[left_id]),
                memory_id=left_id,
                contradicts=right_id,
            ),
            MemoryContradictionLinkedEvent(
                **ctx.event_fields(),
                **_memory_event_fields(mutation.event_documents[right_id]),
                memory_id=right_id,
                contradicts=left_id,
            ),
        ]

    def build_supersede_mutation(
        self,
        *,
        old_id: str,
        new_id: str,
        updated_at: datetime,
        graph_id: str | None = None,
    ) -> "LifecycleMutation":
        old_doc = self.graph.get_jsonld(old_id, graph_id=graph_id)
        new_doc = self.graph.get_jsonld(new_id, graph_id=graph_id)
        old_final = _clone_doc(old_doc)
        new_final = _clone_doc(new_doc)
        _append_node_ref(new_final, memory.SUPERSEDES.name, old_id)
        old_final[memory.STATUS.name] = memory.NodeStatus.SUPERSEDED.value
        old_final[memory.UPDATED_AT.name] = updated_at.isoformat()
        return LifecycleMutation(
            final_documents={
                old_id: old_final,
                new_id: new_final,
            },
            event_documents={
                old_id: old_doc,
                new_id: new_doc,
            },
        )

    def build_mark_stale_mutation(
        self,
        *,
        node_id: str,
        updated_at: datetime,
        graph_id: str | None = None,
    ) -> "LifecycleMutation":
        original = self.graph.get_jsonld(node_id, graph_id=graph_id)
        final = _clone_doc(original)
        final[memory.STATUS.name] = memory.NodeStatus.STALE.value
        final[memory.UPDATED_AT.name] = updated_at.isoformat()
        return LifecycleMutation(
            final_documents={node_id: final},
            event_documents={node_id: original},
        )

    def build_contradiction_mutation(
        self,
        *,
        left_id: str,
        right_id: str,
        graph_id: str | None = None,
    ) -> "LifecycleMutation":
        left_doc = self.graph.get_jsonld(left_id, graph_id=graph_id)
        right_doc = self.graph.get_jsonld(right_id, graph_id=graph_id)
        left_final = _clone_doc(left_doc)
        right_final = _clone_doc(right_doc)
        _append_node_ref(left_final, memory.CONTRADICTS.name, right_id)
        _append_node_ref(right_final, memory.CONTRADICTS.name, left_id)
        return LifecycleMutation(
            final_documents={
                left_id: left_final,
                right_id: right_final,
            },
            event_documents={
                left_id: left_doc,
                right_id: right_doc,
            },
        )

    def apply_mutation(self, mutation: "LifecycleMutation", *, graph_id: str | None = None) -> None:
        for node_id, document in mutation.final_documents.items():
            self.graph.put_jsonld(document, graph_id=graph_id)


@dataclass(frozen=True, slots=True)
class LifecycleMutation:
    final_documents: dict[str, dict[str, Any]]
    event_documents: dict[str, dict[str, Any]]

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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clone_doc(document: dict[str, Any]) -> dict[str, Any]:
    cloned: dict[str, Any] = {}
    for key, value in document.items():
        if isinstance(value, list):
            cloned[key] = [item.copy() if isinstance(item, dict) else item for item in value]
        elif isinstance(value, dict):
            cloned[key] = value.copy()
        else:
            cloned[key] = value
    return cloned
