"""Closed production handlers for canonical-mutation V2 surfaces."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from time import monotonic
from typing import Any

from psycopg.rows import dict_row

from pulsara_agent.graph import OxigraphGraphStore
from pulsara_agent.memory.canonical.index_sync import _sync_memory_with_cursor
from pulsara_agent.memory.canonical.vector_index_sync import (
    MemoryVectorIndexSync,
    VectorSyncStatus,
)
from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.retrieval.embedding.protocol import EmbeddingProvider
from pulsara_agent.projection_jobs.contracts import (
    CanonicalGraphRelationRowFact,
    CanonicalMutationKind,
    CanonicalMutationSurface,
)
from pulsara_agent.graph.projection_relations import (
    oxigraph_relation_insert,
)
from pulsara_agent.runtime.projection_jobs.surface import (
    BoundCanonicalMutationSurfaceDelivery,
    CanonicalMutationSurfaceHandler,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


def surface_handlers(
    *,
    connection_provider: VerifiedPostgresConnectionProviderProtocol,
    oxigraph_url: str,
    embedding: EmbeddingProvider | None,
    embedding_provider_name: str,
) -> tuple[CanonicalMutationSurfaceHandler, ...]:
    """Build the closed handler set without probing external providers."""

    handlers: list[CanonicalMutationSurfaceHandler] = [
        PostgresSearchIndexSurfaceHandler(connection_provider),
        OxigraphCanonicalMutationSurfaceHandler(
            connection_provider=connection_provider,
            oxigraph=OxigraphGraphStore(oxigraph_url),
        ),
    ]
    if embedding is not None:
        handlers.insert(
            1,
            VectorCanonicalMutationSurfaceHandler(
                sync=MemoryVectorIndexSync(
                    connection_provider=connection_provider,
                    provider=embedding,
                    provider_name=embedding_provider_name,
                )
            ),
        )
    return tuple(handlers)


@dataclass(slots=True)
class PostgresSearchIndexSurfaceHandler:
    connection_provider: VerifiedPostgresConnectionProviderProtocol
    surface: CanonicalMutationSurface = CanonicalMutationSurface.SEARCH_INDEX

    def apply(
        self,
        delivery: BoundCanonicalMutationSurfaceDelivery,
        *,
        deadline_monotonic: float,
    ) -> tuple[str, str]:
        payload = _payload(delivery)
        memory_ids = _dirty_memory_ids(payload)
        graph_id = delivery.mutation.candidate.mutation_semantic.graph_id
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            with connection.cursor() as cursor:
                for memory_id in memory_ids:
                    _sync_memory_with_cursor(
                        cursor,
                        graph_id=graph_id,
                        memory_id=memory_id,
                    )
        target = context_fingerprint(
            "canonical-mutation-search-index-target:v1",
            {
                "graph_id": graph_id,
                "ordered_memory_ids": memory_ids,
            },
        )
        return target, _applied_fingerprint(delivery, target=target)


@dataclass(slots=True)
class VectorCanonicalMutationSurfaceHandler:
    sync: MemoryVectorIndexSync
    surface: CanonicalMutationSurface = CanonicalMutationSurface.VECTOR_INDEX

    def apply(
        self,
        delivery: BoundCanonicalMutationSurfaceDelivery,
        *,
        deadline_monotonic: float,
    ) -> tuple[str, str]:
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("vector surface deadline exceeded")
        payload = _payload(delivery)
        memory_ids = _dirty_memory_ids(payload)
        graph_id = delivery.mutation.candidate.mutation_semantic.graph_id

        async def sync_all() -> tuple[tuple[str, str | None], ...]:
            results: list[tuple[str, str | None]] = []
            for memory_id in memory_ids:
                if monotonic() >= deadline_monotonic:
                    raise TimeoutError("vector surface deadline exceeded")
                result = await self.sync.sync_memory_inline(
                    memory_id,
                    graph_id=graph_id,
                )
                if result.status is VectorSyncStatus.STALE:
                    raise RuntimeError(
                        "vector source changed during canonical mutation apply"
                    )
                results.append((memory_id, result.embedded_text_hash))
            return tuple(results)

        results = asyncio.run(sync_all())
        target = context_fingerprint(
            "canonical-mutation-vector-index-target:v1",
            {
                "graph_id": graph_id,
                "embedding_fingerprint": self.sync.embedding_fingerprint,
                "ordered_memory_revisions": results,
            },
        )
        return target, _applied_fingerprint(delivery, target=target)


@dataclass(slots=True)
class OxigraphCanonicalMutationSurfaceHandler:
    connection_provider: VerifiedPostgresConnectionProviderProtocol
    oxigraph: OxigraphGraphStore
    surface: CanonicalMutationSurface = CanonicalMutationSurface.OXIGRAPH

    def apply(
        self,
        delivery: BoundCanonicalMutationSurfaceDelivery,
        *,
        deadline_monotonic: float,
    ) -> tuple[str, str]:
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("Oxigraph surface deadline exceeded")
        payload = _payload(delivery)
        graph_id = delivery.mutation.candidate.mutation_semantic.graph_id
        kind = delivery.mutation.candidate.mutation_semantic.mutation_kind
        target_members: tuple[str, ...]
        if kind in {
            CanonicalMutationKind.GRAPH_RESET,
            CanonicalMutationKind.GRAPH_DELETE,
        } or bool(payload.get("graph_reset")):
            self.oxigraph.delete_graph(graph_id)
            target_members = ("graph-reset",)
        elif isinstance(payload.get("relation"), dict):
            row = self._read_relation(
                payload["relation"],
                deadline_monotonic=deadline_monotonic,
            )
            self.oxigraph.update(
                oxigraph_relation_insert(
                    row,
                    default_context=self.oxigraph.default_context or {},
                )
            )
            target_members = (row.row_fingerprint,)
        else:
            documents = _graph_documents(payload)
            for document in documents:
                self.oxigraph.put_jsonld(document, graph_id=graph_id)
            target_members = tuple(
                context_fingerprint(
                    "canonical-mutation-oxigraph-document:v1",
                    document,
                )
                for document in documents
            )
        target = context_fingerprint(
            "canonical-mutation-oxigraph-target:v1",
            {
                "graph_id": graph_id,
                "ordered_members": target_members,
            },
        )
        return target, _applied_fingerprint(delivery, target=target)

    def _read_relation(
        self,
        reference: dict[str, Any],
        *,
        deadline_monotonic: float,
    ) -> CanonicalGraphRelationRowFact:
        relation_id = str(reference.get("relation_id", ""))
        graph_id = str(reference.get("graph_id", ""))
        if not relation_id or not graph_id:
            raise ValueError("Oxigraph relation mutation reference is incomplete")
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            _set_deadline(connection, deadline_monotonic)
            row = connection.execute(
                """
                SELECT relation_payload, relation_fingerprint
                FROM graph_relation_facts
                WHERE graph_id = %s AND relation_id = %s
                """,
                (graph_id, relation_id),
            ).fetchone()
        if row is None:
            raise ValueError("Oxigraph relation source row is absent")
        relation = CanonicalGraphRelationRowFact.model_validate(row["relation_payload"])
        expected_semantic = str(reference.get("relation_semantic_fingerprint", ""))
        if (
            relation.row_fingerprint != str(row["relation_fingerprint"])
            or relation.relation_semantic_fingerprint != expected_semantic
        ):
            raise ValueError("Oxigraph relation source authority drifted")
        return relation


def _payload(
    delivery: BoundCanonicalMutationSurfaceDelivery,
) -> dict[str, Any]:
    semantic = delivery.mutation.candidate.mutation_semantic
    if semantic.mutation_payload.carrier_kind != "inline_json":
        raise ValueError("artifact-backed canonical mutation hydration is unavailable")
    text = semantic.mutation_payload.canonical_json_utf8
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("canonical mutation payload must be an object")
    if canonical_json_bytes(payload).decode("utf-8") != text:
        raise ValueError("canonical mutation payload encoding drifted")
    identity = delivery.lease.delivery_identity
    if (
        identity.surface is not delivery.lease.delivery_identity.surface
        or identity.handler_contract.surface is not identity.surface
        or semantic.mutation_kind
        not in identity.handler_contract.accepted_mutation_kinds
    ):
        raise ValueError("canonical mutation surface contract mismatch")
    return payload


def _dirty_memory_ids(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("dirty_memory_ids", ())
    if not isinstance(raw, (list, tuple)):
        raise ValueError("canonical mutation dirty_memory_ids is invalid")
    values = tuple(str(value) for value in raw)
    if len(values) != len(set(values)):
        raise ValueError("canonical mutation dirty_memory_ids has duplicates")
    return values


def _graph_documents(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    if isinstance(payload.get("document"), dict):
        return (dict(payload["document"]),)
    raw = payload.get("documents", ())
    if not isinstance(raw, (list, tuple)):
        raise ValueError("canonical mutation documents carrier is invalid")
    documents: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("document"), dict):
            raise ValueError("canonical mutation graph document is invalid")
        documents.append(dict(item["document"]))
    if not documents:
        raise ValueError("Oxigraph mutation has no graph document")
    return tuple(documents)


def _applied_fingerprint(
    delivery: BoundCanonicalMutationSurfaceDelivery,
    *,
    target: str,
) -> str:
    return context_fingerprint(
        "canonical-mutation-surface-applied-document:v1",
        {
            "delivery_identity": (
                delivery.lease.delivery_identity.delivery_identity_fingerprint
            ),
            "target_semantic_identity": target,
        },
    )


def _set_deadline(connection, deadline_monotonic: float) -> None:
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        raise TimeoutError("canonical mutation surface deadline exceeded")
    milliseconds = max(1, int(remaining * 1000))
    connection.execute(
        "SELECT pg_catalog.set_config('statement_timeout', %s, true)",
        (str(milliseconds),),
    )
    connection.execute(
        "SELECT pg_catalog.set_config('lock_timeout', %s, true)",
        (str(milliseconds),),
    )


__all__ = [
    "OxigraphCanonicalMutationSurfaceHandler",
    "PostgresSearchIndexSurfaceHandler",
    "VectorCanonicalMutationSurfaceHandler",
    "surface_handlers",
]
