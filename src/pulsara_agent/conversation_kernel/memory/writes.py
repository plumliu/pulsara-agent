"""Frozen inputs for memory mutations settled with a canonical ToolResult.

The model may request a write, but only the conversation repository's ToolResult
transaction decides whether it becomes a fact or relation.  These values are
process-local inputs, not a second durable proposal ledger.
"""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.conversation_kernel.memory.contracts import (
    MAXIMUM_MEMORY_REFERENCE_ITEMS,
    MAXIMUM_MEMORY_STATEMENT_BYTES,
    MAXIMUM_RESPONSE_PREFERENCE_STATEMENT_BYTES,
    MemoryFactKind,
    MemoryRelationKind,
    PreparedMemoryBasisReference,
    memory_fact_semantic_digest,
    normalize_memory_text,
)
from pulsara_agent.memory.scope import is_valid_context_id
from pulsara_agent.primitives.context import FrozenJsonObjectFact
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.retrieval.tokenizer import (
    MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_ID,
    MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_VERSION,
)


@dataclass(frozen=True, slots=True)
class MemoryRelatedPreview:
    memory_id: str
    kind: MemoryFactKind
    context_id: str
    statement: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class PreparedRememberWrite:
    memory_domain_id: str
    context_id: str
    kind: MemoryFactKind
    statement: str
    basis_refs: tuple[PreparedMemoryBasisReference, ...]
    related: tuple[MemoryRelatedPreview, ...]
    retrieval_summary: FrozenJsonObjectFact
    search_terms: tuple[str, ...]

    def __post_init__(self) -> None:
        statement = normalize_memory_text(self.statement)
        object.__setattr__(self, "statement", statement)
        if not self.memory_domain_id or not is_valid_context_id(self.context_id):
            raise ValueError("memory write scope is invalid")
        if not isinstance(self.kind, MemoryFactKind):
            raise TypeError("remember requires a final memory kind")
        maximum = (
            MAXIMUM_RESPONSE_PREFERENCE_STATEMENT_BYTES
            if self.kind is MemoryFactKind.RESPONSE_PREFERENCE
            else MAXIMUM_MEMORY_STATEMENT_BYTES
        )
        if not 1 <= len(statement.encode("utf-8")) <= maximum:
            raise ValueError("memory statement exceeds its kind's byte bound")
        if (
            len(self.basis_refs) > MAXIMUM_MEMORY_REFERENCE_ITEMS
            or len(self.related) > 3
        ):
            raise ValueError("remember references exceed their product bound")
        if tuple(ref.ordinal for ref in self.basis_refs) != tuple(
            range(len(self.basis_refs))
        ):
            raise ValueError("remember reference ordinals are not contiguous")
        if len({ref.target_fact_id for ref in self.basis_refs}) != len(self.basis_refs):
            raise ValueError("remember basis IDs are duplicated")
        if len({item.memory_id for item in self.related}) != len(self.related):
            raise ValueError("related memory IDs are duplicated")
        if not isinstance(self.retrieval_summary, FrozenJsonObjectFact):
            raise TypeError("remember retrieval coverage must be frozen")
        if len(self.search_terms) > 256:
            raise ValueError("remember search terms are invalid")

    @property
    def semantic_digest(self) -> str:
        return memory_fact_semantic_digest(kind=self.kind, statement=self.statement)

    @property
    def search_contract(self) -> tuple[str, int]:
        return (
            MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_ID,
            MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_VERSION,
        )


@dataclass(frozen=True, slots=True)
class PreparedMemoryRelationWrite:
    memory_domain_id: str
    source_memory_id: str
    target_memory_id: str
    relation_kind: MemoryRelationKind

    def __post_init__(self) -> None:
        if not all(
            (self.memory_domain_id, self.source_memory_id, self.target_memory_id)
        ) or self.source_memory_id == self.target_memory_id:
            raise ValueError("memory relation endpoints are invalid")
        if self.relation_kind not in {
            MemoryRelationKind.CONTRADICTS,
            MemoryRelationKind.SUPERSEDES,
        }:
            raise ValueError("relation tool only marks conflict or replacement")


PreparedMemoryMutation = PreparedRememberWrite | PreparedMemoryRelationWrite


def memory_mutation_manifest(value: PreparedMemoryMutation) -> dict[str, object]:
    """Expose the complete frozen input to the existing acceptance identity."""

    if isinstance(value, PreparedMemoryRelationWrite):
        return {
            "kind": "RELATION",
            "memory_domain_id": value.memory_domain_id,
            "source_memory_id": value.source_memory_id,
            "target_memory_id": value.target_memory_id,
            "relation_kind": value.relation_kind.value,
        }
    return {
        "kind": "REMEMBER",
        "memory_domain_id": value.memory_domain_id,
        "context_id": value.context_id,
        "fact_kind": value.kind.value,
        "statement": value.statement,
        "basis_refs": tuple(
            (ref.target_fact_id, ref.target_context_id, ref.ordinal)
            for ref in value.basis_refs
        ),
        "related": tuple(
            (
                item.memory_id,
                item.kind.value,
                item.context_id,
                item.statement,
                item.recorded_at,
            )
            for item in value.related
        ),
        "retrieval_summary": thaw_json(value.retrieval_summary),
        "search_terms": value.search_terms,
    }
