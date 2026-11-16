"""Shared lexical/dense retrieval of current canonical memory objects."""

from __future__ import annotations
from dataclasses import dataclass, replace
from enum import StrEnum
import math
from psycopg.rows import dict_row
from pulsara_agent.conversation_kernel.memory.contracts import MemoryRecord
from pulsara_agent.retrieval.pg_search import lexical_predicate, lexical_parameters
from pulsara_agent.retrieval.config import (
    MEMORY_DENSE_ELIGIBILITY_POLICY,
    DenseRecallPurpose,
)
from pulsara_agent.retrieval.embedding.validation import freeze_v1_embedding_vector
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

RRF_K = 60


class MemoryRetrievalDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    NO_MATCH = "NO_MATCH"


class MemoryDenseCandidateDisposition(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    BOUNDED_TOP_K = "BOUNDED_TOP_K"
    EXHAUSTED_VISIBLE_SET = "EXHAUSTED_VISIBLE_SET"
    PARTIAL_BOUNDED_SCAN = "PARTIAL_BOUNDED_SCAN"
    NO_ELIGIBLE_MATCH = "NO_ELIGIBLE_MATCH"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class MemoryQueryRow(MemoryRecord):
    sparse_rank: int | None = None
    dense_rank: int | None = None
    fused_score: float = 0.0


@dataclass(frozen=True, slots=True)
class MemoryDenseCandidateBatch:
    facts: tuple[MemoryQueryRow, ...]
    disposition: MemoryDenseCandidateDisposition


@dataclass(frozen=True, slots=True)
class MemoryQueryResult:
    disposition: MemoryRetrievalDisposition
    facts: tuple
    sparse_available: bool = True
    dense_available: bool = False
    dense_disposition: MemoryDenseCandidateDisposition = (
        MemoryDenseCandidateDisposition.NOT_REQUESTED
    )
    rerank_disposition: str = "NOT_APPLIED"


class PostgresMemoryQuery:
    def __init__(self, provider):
        self._provider = provider

    def _connection(self, deadline):
        return self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline,
        )

    def get(self, *, read_binding, fact_id, deadline_monotonic):
        from pulsara_agent.identity import require_id

        require_id(fact_id, "memory")
        with self._connection(deadline_monotonic) as c:
            row = c.execute(
                "SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND id=%s AND context_id=ANY(%s::text[])",
                (
                    read_binding.memory_domain_id,
                    fact_id,
                    list(read_binding.readable_context_ids),
                ),
            ).fetchone()
        return None if row is None else MemoryQueryRow.from_row(row)

    def current_records(self, *, read_binding, fact_ids, deadline_monotonic):
        if not fact_ids:
            return {}
        with self._connection(deadline_monotonic) as c:
            rows = c.execute(
                "SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND id=ANY(%s::text[])",
                (read_binding.memory_domain_id, list(fact_ids)),
            ).fetchall()
        # This internal lookup distinguishes a known old memory moving out of
        # scope from deletion. Its new body is never projected without can_read.
        return {row["id"]: MemoryQueryRow.from_row(row) for row in rows}

    def response_preference_snapshot(self, *, read_binding, deadline_monotonic):
        with self._connection(deadline_monotonic) as c:
            rows = c.execute(
                "SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND context_id=ANY(%s::text[]) AND fact_kind='RESPONSE_PREFERENCE' ORDER BY updated_at DESC,id ASC LIMIT 8",
                (
                    read_binding.memory_domain_id,
                    list(read_binding.readable_context_ids),
                ),
            ).fetchall()
        return tuple(MemoryQueryRow.from_row(row) for row in rows)

    def sparse_candidates(
        self, *, read_binding, query, limit, automatic, deadline_monotonic
    ):
        if not query.strip():
            return ()
        predicate = lexical_predicate(
            "search_text", analysis_columns=("statement_en", "statement_tech")
        )
        with self._connection(deadline_monotonic) as c:
            rows = c.execute(
                f"""SELECT *,paradedb.score(id) AS rank FROM pulsara_v3.memory_facts
                WHERE {predicate} AND memory_domain_id=%s AND context_id=ANY(%s::text[])
                AND (%s=false OR fact_kind<>'RESPONSE_PREFERENCE')
                ORDER BY paradedb.score(id) DESC,id ASC LIMIT %s""",
                (
                    *lexical_parameters(query),
                    read_binding.memory_domain_id,
                    list(read_binding.readable_context_ids),
                    automatic,
                    limit,
                ),
            ).fetchall()
        return tuple(
            MemoryQueryRow.from_row(row, sparse_rank=index + 1)
            for index, row in enumerate(rows)
        )

    def dense_candidates(
        self,
        *,
        read_binding,
        vector,
        embedding_contract,
        limit,
        purpose,
        automatic,
        deadline_monotonic,
    ):
        values = freeze_v1_embedding_vector(vector)
        literal = "[" + ",".join(format(value, ".17g") for value in values) + "]"
        floor = MEMORY_DENSE_ELIGIBILITY_POLICY.minimum_similarity(
            DenseRecallPurpose(purpose)
        )
        with self._connection(deadline_monotonic) as c:
            c.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            c.execute("SET LOCAL hnsw.max_scan_tuples = 20000")
            rows = c.execute(
                """SELECT f.*,e.embedding <=> %s::public.vector AS distance
                FROM pulsara_v3.memory_embeddings e JOIN pulsara_v3.memory_facts f
                ON f.memory_domain_id=e.memory_domain_id AND f.id=e.fact_id
                AND e.fact_semantic_digest='sha256:' || encode(sha256(convert_to(f.search_text,'UTF8')),'hex')
                WHERE f.memory_domain_id=%s AND f.context_id=ANY(%s::text[]) AND e.embedding_contract_id=%s
                AND (%s=false OR f.fact_kind<>'RESPONSE_PREFERENCE')
                ORDER BY e.embedding <=> %s::public.vector ASC LIMIT %s""",
                (
                    literal,
                    read_binding.memory_domain_id,
                    list(read_binding.readable_context_ids),
                    embedding_contract.contract_id,
                    automatic,
                    literal,
                    limit * 4,
                ),
            ).fetchall()
        eligible = [
            row
            for row in rows
            if math.isfinite(float(row["distance"]))
            and 1 - float(row["distance"]) >= floor
        ]
        eligible.sort(key=lambda row: (float(row["distance"]), row["id"]))
        selected = eligible[:limit]
        disposition = (
            MemoryDenseCandidateDisposition.BOUNDED_TOP_K
            if len(selected) >= limit
            else MemoryDenseCandidateDisposition.NO_ELIGIBLE_MATCH
            if rows and not eligible
            else MemoryDenseCandidateDisposition.PARTIAL_BOUNDED_SCAN
            if eligible
            else MemoryDenseCandidateDisposition.EXHAUSTED_VISIBLE_SET
        )
        return MemoryDenseCandidateBatch(
            tuple(
                MemoryQueryRow.from_row(row, dense_rank=i + 1)
                for i, row in enumerate(selected)
            ),
            disposition,
        )

    @staticmethod
    def fuse_candidates(sparse, dense):
        by_id = {}
        for values, field in ((sparse, "sparse_rank"), (dense, "dense_rank")):
            for index, item in enumerate(values):
                prior = by_id.get(item.fact_id, item)
                by_id[item.fact_id] = replace(prior, **{field: index + 1})
        scored = [
            replace(
                item,
                fused_score=sum(
                    1 / (RRF_K + rank)
                    for rank in (item.sparse_rank, item.dense_rank)
                    if rank is not None
                ),
            )
            for item in by_id.values()
        ]
        return tuple(sorted(scored, key=lambda item: (-item.fused_score, item.fact_id)))

    def canonical_refetch(
        self, *, read_binding, ranked, automatic=False, deadline_monotonic
    ):
        current = self.current_records(
            read_binding=read_binding,
            fact_ids=[item.fact_id for item in ranked],
            deadline_monotonic=deadline_monotonic,
        )
        return tuple(
            item
            for item in ranked
            if item.fact_id in current
            and read_binding.can_read(current[item.fact_id].context_id)
            and (
                not automatic
                or current[item.fact_id].fact_kind != "RESPONSE_PREFERENCE"
            )
            and _same_record(item, current[item.fact_id])
        )

    def search(
        self,
        *,
        read_binding,
        query,
        limit=5,
        query_embedding=None,
        embedding_contract=None,
        automatic=False,
        deadline_monotonic,
    ):
        if type(limit) is not int or limit < 1:
            raise ValueError("记忆查询条数须为正整数。")
        sparse = self.sparse_candidates(
            read_binding=read_binding,
            query=query,
            limit=limit,
            automatic=automatic,
            deadline_monotonic=deadline_monotonic,
        )
        dense = ()
        if query_embedding is not None:
            dense = self.dense_candidates(
                read_binding=read_binding,
                vector=query_embedding,
                embedding_contract=embedding_contract,
                limit=limit,
                purpose=DenseRecallPurpose.AUTOMATIC_ROOT
                if automatic
                else DenseRecallPurpose.EXPLICIT_SEARCH,
                automatic=automatic,
                deadline_monotonic=deadline_monotonic,
            ).facts
        selected = self.canonical_refetch(
            read_binding=read_binding,
            ranked=self.fuse_candidates(sparse, dense)[:limit],
            automatic=automatic,
            deadline_monotonic=deadline_monotonic,
        )
        return MemoryQueryResult(
            MemoryRetrievalDisposition.COMPLETE
            if selected
            else MemoryRetrievalDisposition.NO_MATCH,
            selected,
        )


def _same_record(left, right):
    return (
        left.title,
        left.statement,
        left.context_id,
        left.updated_at,
        left.fact_kind,
    ) == (
        right.title,
        right.statement,
        right.context_id,
        right.updated_at,
        right.fact_kind,
    )
