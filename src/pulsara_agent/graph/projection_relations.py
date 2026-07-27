"""Immutable graph relation lowering and relation-aware read support."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from threading import RLock
from typing import Iterable, Iterator, cast

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.graph.jsonld_codec import (
    expand_graph_id,
    expand_id,
    graph_key,
    iri_token,
)
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.frozen import (
    DURABLE_FACT_FINGERPRINT_REGISTRY,
)
from pulsara_agent.projection_jobs.contracts import (
    CanonicalGraphNodeReadViewFact,
    CanonicalGraphRelationLoweringContractFact,
    CanonicalGraphRelationReadPageFact,
    CanonicalGraphRelationRowFact,
    ToolResultArtifactRelationFact,
    TurnProducedToolResultRelationFact,
    build_projection_fact,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


OWNED_RELATION_PREDICATES = (
    rt.PRODUCED.value,
    rt.PROVIDES.value,
)


def build_graph_relation_lowering_contract() -> (
    CanonicalGraphRelationLoweringContractFact
):
    accepted = tuple(
        DURABLE_FACT_FINGERPRINT_REGISTRY.resolve(version).domain_separator
        for version in (
            "turn_produced_tool_result_relation.v1",
            "tool_result_artifact_relation.v1",
        )
    )
    return cast(
        CanonicalGraphRelationLoweringContractFact,
        build_projection_fact(
            CanonicalGraphRelationLoweringContractFact,
            schema_version="canonical_graph_relation_lowering_contract.v1",
            contract_id="canonical-graph-relation-lowering.v1",
            accepted_relation_schema_fingerprints=accepted,
            postgres_relation_schema_fingerprint=context_fingerprint(
                "canonical-graph-relation-postgres-row:v1", {}
            ),
            rdf_named_graph_codec_fingerprint=context_fingerprint(
                "canonical-graph-relation-rdf-codec:v1", {}
            ),
            jsonld_read_merge_contract_fingerprint=context_fingerprint(
                "canonical-graph-relation-jsonld-read-merge:v1", {}
            ),
            owned_predicate_iris=OWNED_RELATION_PREDICATES,
        ),
    )


GRAPH_RELATION_LOWERING_CONTRACT = build_graph_relation_lowering_contract()


def lower_graph_relation(
    relation: TurnProducedToolResultRelationFact | ToolResultArtifactRelationFact,
    *,
    contract: CanonicalGraphRelationLoweringContractFact = (
        GRAPH_RELATION_LOWERING_CONTRACT
    ),
) -> CanonicalGraphRelationRowFact:
    if relation.predicate_iri not in contract.owned_predicate_iris:
        raise ValueError("relation predicate is not owned by the lowering registry")
    if isinstance(relation, TurnProducedToolResultRelationFact):
        kind = "turn_produced_tool_result"
        source_id = relation.turn_id
        target_id = relation.tool_result_document_id
        authority = relation.source_tool_result_end_reference_fingerprint
    else:
        kind = "tool_result_provides_artifact"
        source_id = relation.tool_result_document_id
        target_id = relation.artifact_document_id
        authority = relation.artifact_semantic_reference_fingerprint
    return cast(
        CanonicalGraphRelationRowFact,
        build_projection_fact(
            CanonicalGraphRelationRowFact,
            schema_version="canonical_graph_relation_row.v1",
            relation_id=relation.relation_document_id,
            graph_id=relation.graph_id,
            relation_kind=kind,
            source_document_id=source_id,
            predicate_iri=relation.predicate_iri,
            target_document_id=target_id,
            relation_semantic_fingerprint=relation.relation_semantic_fingerprint,
            source_authority_fingerprint=authority,
            lowering_contract_fingerprint=contract.contract_fingerprint,
        ),
    )


def reject_owned_relation_predicates(document: dict[str, object]) -> None:
    for key in (rt.PRODUCED.name, rt.PROVIDES.name):
        if key in document:
            raise ValueError(f"{key} is owned by the immutable graph relation port")


class InMemoryCanonicalGraphRelationRepository:
    def __init__(self) -> None:
        self._lock = RLock()
        self._rows: dict[tuple[str, str], CanonicalGraphRelationRowFact] = {}

    def put(
        self,
        relation: TurnProducedToolResultRelationFact | ToolResultArtifactRelationFact,
    ) -> CanonicalGraphRelationRowFact:
        row = lower_graph_relation(relation)
        key = (row.graph_id, row.relation_id)
        with self._lock:
            existing = self._rows.get(key)
            if existing is not None and existing != row:
                raise ValueError("graph relation identity conflict")
            self._rows[key] = row
        return row

    def read_page(
        self,
        *,
        graph_id: str,
        source_document_id: str,
        predicate_iri: str | None = None,
        after_relation_id: str | None = None,
        limit: int = 256,
    ) -> CanonicalGraphRelationReadPageFact:
        if limit < 1 or limit > 256:
            raise ValueError("graph relation read limit must be in 1..256")
        with self._lock:
            candidates = tuple(
                row
                for (row_graph, _), row in sorted(self._rows.items())
                if row_graph == graph_id
                and row.source_document_id == source_document_id
                and (predicate_iri is None or row.predicate_iri == predicate_iri)
                and (after_relation_id is None or row.relation_id > after_relation_id)
            )
        selected = candidates[:limit]
        return cast(
            CanonicalGraphRelationReadPageFact,
            build_projection_fact(
                CanonicalGraphRelationReadPageFact,
                schema_version="canonical_graph_relation_read_page.v1",
                graph_id=graph_id,
                source_document_id=source_document_id,
                predicate_iri=predicate_iri,
                after_relation_id=after_relation_id,
                ordered_relations=selected,
                relation_count=len(selected),
                relation_accumulator=context_fingerprint(
                    "canonical-graph-relation-read-page-root:v1",
                    tuple(item.row_fingerprint for item in selected),
                ),
                has_more=len(candidates) > len(selected),
                next_after_relation_id=(
                    selected[-1].relation_id
                    if len(candidates) > len(selected) and selected
                    else None
                ),
            ),
        )

    def merge_read_view(
        self,
        *,
        graph_id: str,
        node_id: str,
        base_document: dict[str, object],
        base_document_semantic_fingerprint: str,
    ) -> CanonicalGraphNodeReadViewFact:
        reject_owned_relation_predicates(base_document)
        page = self.read_page(
            graph_id=graph_id,
            source_document_id=node_id,
        )
        if page.has_more:
            raise ValueError("relation_view_requires_paged_hydration")
        return _build_read_view(
            graph_id=graph_id,
            node_id=node_id,
            base_document=base_document,
            base_document_semantic_fingerprint=(base_document_semantic_fingerprint),
            page=page,
        )

    def rows(self) -> tuple[CanonicalGraphRelationRowFact, ...]:
        with self._lock:
            return tuple(self._rows[key] for key in sorted(self._rows))


@dataclass(slots=True)
class PostgresCanonicalGraphRelationRepository:
    """Immutable relation rows with relation-aware bounded reads."""

    connection_provider: VerifiedPostgresConnectionProviderProtocol | None = None
    connection: Connection | None = None

    def __post_init__(self) -> None:
        if (self.connection_provider is None) == (self.connection is None):
            raise ValueError("graph relation repository requires one connection owner")

    def put(
        self,
        relation: TurnProducedToolResultRelationFact | ToolResultArtifactRelationFact,
    ) -> CanonicalGraphRelationRowFact:
        row = lower_graph_relation(relation)
        with self._cursor(row_factory=dict_row) as cursor:
            self._put_row(cursor.connection, row)
        return row

    @staticmethod
    def put_in_transaction(
        connection: Connection,
        relation: TurnProducedToolResultRelationFact | ToolResultArtifactRelationFact,
    ) -> CanonicalGraphRelationRowFact:
        row = lower_graph_relation(relation)
        PostgresCanonicalGraphRelationRepository._put_row(connection, row)
        return row

    @staticmethod
    def _put_row(
        connection: Connection,
        row: CanonicalGraphRelationRowFact,
    ) -> None:
        inserted = connection.execute(
            """
            INSERT INTO graph_relation_facts (
                graph_id, relation_id, source_document_id, predicate_iri,
                target_document_id, relation_payload, relation_fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (graph_id, relation_id) DO NOTHING
            RETURNING relation_id
            """,
            (
                row.graph_id,
                row.relation_id,
                row.source_document_id,
                row.predicate_iri,
                row.target_document_id,
                Jsonb(row.model_dump(mode="json")),
                row.row_fingerprint,
            ),
        ).fetchone()
        if inserted is not None:
            return
        existing = connection.execute(
            """
            SELECT relation_payload, relation_fingerprint
            FROM graph_relation_facts
            WHERE graph_id = %s AND relation_id = %s
            """,
            (row.graph_id, row.relation_id),
        ).fetchone()
        if existing is None:
            raise ValueError("graph relation disappeared during confirmation")
        payload = (
            existing["relation_payload"] if isinstance(existing, dict) else existing[0]
        )
        fingerprint = (
            existing["relation_fingerprint"]
            if isinstance(existing, dict)
            else existing[1]
        )
        if (
            CanonicalGraphRelationRowFact.model_validate(payload) != row
            or str(fingerprint) != row.row_fingerprint
        ):
            raise ValueError("graph relation identity conflict")

    def read_page(
        self,
        *,
        graph_id: str,
        source_document_id: str,
        predicate_iri: str | None = None,
        after_relation_id: str | None = None,
        limit: int = 256,
    ) -> CanonicalGraphRelationReadPageFact:
        if limit < 1 or limit > 256:
            raise ValueError("graph relation read limit must be in 1..256")
        clauses = [
            "graph_id = %s",
            "source_document_id = %s",
        ]
        params: list[object] = [graph_id, source_document_id]
        if predicate_iri is not None:
            clauses.append("predicate_iri = %s")
            params.append(predicate_iri)
        if after_relation_id is not None:
            clauses.append("relation_id > %s")
            params.append(after_relation_id)
        params.append(limit + 1)
        query = (
            "SELECT relation_payload, relation_fingerprint "
            "FROM graph_relation_facts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY relation_id LIMIT %s"
        )
        with self._cursor(row_factory=dict_row) as cursor:
            raw_rows = cursor.execute(query, tuple(params)).fetchall()
        rows = tuple(
            CanonicalGraphRelationRowFact.model_validate(item["relation_payload"])
            for item in raw_rows[:limit]
        )
        for raw, row in zip(raw_rows, rows, strict=False):
            if str(raw["relation_fingerprint"]) != row.row_fingerprint:
                raise ValueError("graph relation row fingerprint drifted")
        has_more = len(raw_rows) > limit
        return cast(
            CanonicalGraphRelationReadPageFact,
            build_projection_fact(
                CanonicalGraphRelationReadPageFact,
                schema_version="canonical_graph_relation_read_page.v1",
                graph_id=graph_id,
                source_document_id=source_document_id,
                predicate_iri=predicate_iri,
                after_relation_id=after_relation_id,
                ordered_relations=rows,
                relation_count=len(rows),
                relation_accumulator=context_fingerprint(
                    "canonical-graph-relation-read-page-root:v1",
                    tuple(item.row_fingerprint for item in rows),
                ),
                has_more=has_more,
                next_after_relation_id=(
                    rows[-1].relation_id if has_more and rows else None
                ),
            ),
        )

    def merge_jsonld(
        self,
        *,
        graph_id: str,
        node_id: str,
        base_document: dict[str, object],
    ) -> dict[str, object]:
        reject_owned_relation_predicates(base_document)
        merged = dict(base_document)
        after: str | None = None
        grouped: dict[str, list[dict[str, str]]] = {}
        while True:
            page = self.read_page(
                graph_id=graph_id,
                source_document_id=node_id,
                after_relation_id=after,
            )
            for row in page.ordered_relations:
                key = (
                    rt.PRODUCED.name
                    if row.predicate_iri == rt.PRODUCED.value
                    else rt.PROVIDES.name
                )
                grouped.setdefault(key, []).append({"@id": row.target_document_id})
            if not page.has_more:
                break
            after = page.next_after_relation_id
        merged.update(grouped)
        return merged

    def merge_read_view(
        self,
        *,
        graph_id: str,
        node_id: str,
        base_document: dict[str, object],
        base_document_semantic_fingerprint: str,
    ) -> CanonicalGraphNodeReadViewFact:
        reject_owned_relation_predicates(base_document)
        page = self.read_page(
            graph_id=graph_id,
            source_document_id=node_id,
        )
        if page.has_more:
            raise ValueError("relation_view_requires_paged_hydration")
        return _build_read_view(
            graph_id=graph_id,
            node_id=node_id,
            base_document=base_document,
            base_document_semantic_fingerprint=(base_document_semantic_fingerprint),
            page=page,
        )

    @contextmanager
    def _cursor(self, *, row_factory=None) -> Iterator:
        if self.connection is not None:
            with self.connection.cursor(row_factory=row_factory) as cursor:
                yield cursor
            return
        assert self.connection_provider is not None
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=row_factory,
            deadline_monotonic=monotonic() + 20.0,
        ) as connection:
            with connection.cursor() as cursor:
                yield cursor


def oxigraph_relation_insert(
    row: CanonicalGraphRelationRowFact,
    *,
    default_context: dict[str, object],
) -> str:
    """Return the only SPARQL lowering allowed for an immutable relation."""

    if row.predicate_iri not in GRAPH_RELATION_LOWERING_CONTRACT.owned_predicate_iris:
        raise ValueError("unknown relation predicate")
    graph = iri_token(expand_graph_id(graph_key(row.graph_id), default_context))
    source = iri_token(expand_id(row.source_document_id, default_context))
    target = iri_token(expand_id(row.target_document_id, default_context))
    predicate = iri_token(row.predicate_iri)
    return f"INSERT DATA {{ GRAPH {graph} {{ {source} {predicate} {target} . }} }}"


def _build_read_view(
    *,
    graph_id: str,
    node_id: str,
    base_document: dict[str, object],
    base_document_semantic_fingerprint: str,
    page: CanonicalGraphRelationReadPageFact,
) -> CanonicalGraphNodeReadViewFact:
    merged = dict(base_document)
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in page.ordered_relations:
        key = (
            rt.PRODUCED.name
            if row.predicate_iri == rt.PRODUCED.value
            else rt.PROVIDES.name
        )
        grouped.setdefault(key, []).append({"@id": row.target_document_id})
    merged.update(grouped)
    canonical = json.dumps(
        merged,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return cast(
        CanonicalGraphNodeReadViewFact,
        build_projection_fact(
            CanonicalGraphNodeReadViewFact,
            schema_version="canonical_graph_node_read_view.v1",
            graph_id=graph_id,
            node_id=node_id,
            base_document_semantic_fingerprint=(base_document_semantic_fingerprint),
            ordered_relation_semantic_accumulator=context_fingerprint(
                "canonical-graph-node-relation-root:v1",
                tuple(
                    item.relation_semantic_fingerprint
                    for item in page.ordered_relations
                ),
            ),
            merged_relation_count=len(page.ordered_relations),
            merged_canonical_json_utf8=canonical,
            merged_canonical_json_sha256=context_fingerprint(
                "canonical-graph-node-json:v1", merged
            ),
            jsonld_read_merge_contract_fingerprint=(
                GRAPH_RELATION_LOWERING_CONTRACT.jsonld_read_merge_contract_fingerprint
            ),
        ),
    )


def relation_accumulator(
    rows: Iterable[CanonicalGraphRelationRowFact],
) -> str:
    return context_fingerprint(
        "canonical-graph-relation-ordered-root:v1",
        tuple(row.row_fingerprint for row in rows),
    )


__all__ = [
    "GRAPH_RELATION_LOWERING_CONTRACT",
    "InMemoryCanonicalGraphRelationRepository",
    "OWNED_RELATION_PREDICATES",
    "PostgresCanonicalGraphRelationRepository",
    "build_graph_relation_lowering_contract",
    "lower_graph_relation",
    "oxigraph_relation_insert",
    "reject_owned_relation_predicates",
    "relation_accumulator",
]
