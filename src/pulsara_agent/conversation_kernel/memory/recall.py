"""Bounded advisory-memory recall over canonical PostgreSQL rows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Sequence

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.memory.contracts import (
    canonical_memory_recorded_at,
)
from pulsara_agent.memory.scope import FrozenMemoryReadContextBinding
from pulsara_agent.retrieval.config import (
    MEMORY_DENSE_ELIGIBILITY_POLICY,
    MEMORY_EMBEDDING_CONTRACT,
    DenseRecallPurpose,
)
from pulsara_agent.retrieval.tokenizer import MemoryRetrievalTokenizerV1
from pulsara_agent.retrieval.embedding.validation import (
    freeze_v1_embedding_vector,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane


MAXIMUM_MEMORY_QUERY_RESULTS = 50
MAXIMUM_AUTOMATIC_MEMORY_RESULTS = 5
RRF_K = 60
MEMORY_EMBEDDING_CONTRACT_ID = MEMORY_EMBEDDING_CONTRACT.contract_id
MEMORY_EMBEDDING_CONTRACT_VERSION = MEMORY_EMBEDDING_CONTRACT.contract_version


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
class MemoryDenseCandidateBatch:
    facts: tuple["MemoryQueryRow", ...]
    disposition: MemoryDenseCandidateDisposition


@dataclass(frozen=True, slots=True)
class MemoryRelatedSearchResult:
    facts: tuple["MemoryQueryRow", ...]
    dense_disposition: MemoryDenseCandidateDisposition


@dataclass(frozen=True, slots=True)
class MemorySearchStageResult:
    ordinal: int
    context_coverage: str
    kind: str
    new_results: int


@dataclass(frozen=True, slots=True)
class MemoryQueryRow:
    fact_id: str
    memory_domain_id: str
    context_id: str
    fact_kind: str
    lifecycle: str
    statement: str
    recorded_at: str
    fact_semantic_digest: str
    user_edited_at: str | None = None
    sparse_rank: int | None = None
    dense_rank: int | None = None
    fused_score: float = 0.0
    match_tier: int = 0

    @property
    def fact_payload(self) -> dict[str, object]:
        return {
            "context_id": self.context_id,
            "statement": self.statement,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class MemoryRelationRow:
    relation_id: str
    source_fact_id: str
    target_fact_id: str
    relation_kind: str
    supersede_mode: str | None


@dataclass(frozen=True, slots=True)
class MemoryResponsePreferenceSnapshot:
    """One bounded RR cut of effective active preferences."""

    facts: tuple[MemoryQueryRow, ...]
    selection_incomplete: bool
    conflicts_omitted: bool


@dataclass(frozen=True, slots=True)
class MemoryRelationOwnerProjection:
    relation_id: str
    provenance_disposition: str
    write_tool: str
    owner_session_id: str | None
    owner_entry_id: str | None
    owner_tool_call_id: str | None


@dataclass(frozen=True, slots=True)
class MemoryProvenanceProjection:
    provenance_disposition: str
    write_tool: str
    producer_session_id: str | None
    producer_turn_id: str | None
    producer_entry_id: str | None
    producer_tool_call_id: str | None
    relation_owners: tuple[MemoryRelationOwnerProjection, ...]


@dataclass(frozen=True, slots=True)
class MemoryQueryResult:
    disposition: MemoryRetrievalDisposition
    facts: tuple[MemoryQueryRow, ...]
    attempted_stages: tuple[MemorySearchStageResult, ...]
    relaxed_fields: tuple[str, ...] = ()
    sparse_available: bool = True
    dense_available: bool = False
    dense_disposition: MemoryDenseCandidateDisposition = (
        MemoryDenseCandidateDisposition.NOT_REQUESTED
    )
    rerank_disposition: str = "NOT_CONFIGURED"


class PostgresMemoryQuery:
    def __init__(self, provider, *, tokenizer: MemoryRetrievalTokenizerV1 | None = None) -> None:
        self._provider = provider
        self._tokenizer = tokenizer or MemoryRetrievalTokenizerV1()

    def search(
        self,
        *,
        read_binding: FrozenMemoryReadContextBinding,
        query: str,
        limit: int = 5,
        requested_kind: str | None = None,
        query_embedding: Sequence[float] | None = None,
        automatic: bool = False,
        deadline_monotonic: float,
    ) -> MemoryQueryResult:
        limit = max(1, min(int(limit), MAXIMUM_MEMORY_QUERY_RESULTS))
        terms = self._tokenizer.tokenize(query)
        stages = _filter_stages(read_binding, requested_kind)
        gathered: list[MemoryQueryRow] = []
        seen: set[str] = set()
        attempted: list[MemorySearchStageResult] = []
        relaxed: list[str] = []
        sparse_ok = True
        dense_ok = query_embedding is not None
        dense_dispositions: list[MemoryDenseCandidateDisposition] = []
        for ordinal, (kind_filter, label, relaxed_field) in enumerate(stages):
            if relaxed_field is not None:
                relaxed.append(relaxed_field)
            try:
                sparse = self._sparse(
                    read_binding=read_binding,
                    terms=terms,
                    kind_filter=kind_filter,
                    limit=40 if not automatic else 20,
                    automatic=automatic,
                    deadline_monotonic=deadline_monotonic,
                )
            except Exception:
                sparse_ok = False
                sparse = ()
            dense: tuple[MemoryQueryRow, ...] = ()
            if query_embedding is not None:
                try:
                    dense_batch = self._dense(
                        read_binding=read_binding,
                        vector=query_embedding,
                        kind_filter=kind_filter,
                        limit=30 if not automatic else 20,
                        purpose=(
                            DenseRecallPurpose.AUTOMATIC_ROOT
                            if automatic
                            else DenseRecallPurpose.EXPLICIT_SEARCH
                        ),
                        automatic=automatic,
                        deadline_monotonic=deadline_monotonic,
                    )
                    dense = dense_batch.facts
                    dense_dispositions.append(dense_batch.disposition)
                except Exception:
                    dense_ok = False
                    dense_dispositions.append(
                        MemoryDenseCandidateDisposition.UNAVAILABLE
                    )
            fused = _rrf(sparse, dense)
            prior_count = len(gathered)
            for item in fused:
                if item.fact_id in seen:
                    continue
                seen.add(item.fact_id)
                gathered.append(
                    MemoryQueryRow(
                        **{field: getattr(item, field) for field in (
                            "fact_id", "memory_domain_id", "context_id",
                            "fact_kind", "lifecycle", "statement", "recorded_at",
                            "fact_semantic_digest", "sparse_rank",
                            "dense_rank", "fused_score"
                        )},
                        match_tier=ordinal,
                    )
                )
            attempted.append(
                _stage_result(
                    ordinal,
                    label,
                    new_results=len(gathered) - prior_count,
                )
            )
            if len(gathered) >= min(limit, 3):
                break
        final = self._canonical_refetch(
            read_binding=read_binding,
            ranked=gathered[:limit],
            automatic=automatic,
            deadline_monotonic=deadline_monotonic,
        )
        if final:
            disposition = (
                MemoryRetrievalDisposition.COMPLETE
                if sparse_ok and (query_embedding is None or dense_ok)
                else MemoryRetrievalDisposition.PARTIAL
            )
        elif not sparse_ok and query_embedding is not None and not dense_ok:
            disposition = MemoryRetrievalDisposition.UNAVAILABLE
        else:
            disposition = MemoryRetrievalDisposition.NO_MATCH
        return MemoryQueryResult(
            disposition=disposition,
            facts=final,
            attempted_stages=tuple(attempted),
            relaxed_fields=tuple(dict.fromkeys(relaxed)),
            sparse_available=sparse_ok,
            dense_available=dense_ok,
            dense_disposition=_aggregate_dense_disposition(
                query_embedding is not None, dense_dispositions
            ),
        )

    def tokenize_query(self, query: str) -> tuple[str, ...]:
        """Apply the same sealed lexical contract used by accepted facts."""

        return self._tokenizer.tokenize(query)

    @staticmethod
    def filter_stages(
        read_binding: FrozenMemoryReadContextBinding,
        requested_kind: str | None,
    ):
        return _filter_stages(read_binding, requested_kind)

    def sparse_candidates(self, **kwargs) -> tuple[MemoryQueryRow, ...]:
        return self._sparse(**kwargs)

    def dense_candidates(self, **kwargs) -> MemoryDenseCandidateBatch:
        return self._dense(**kwargs)

    @staticmethod
    def fuse_candidates(
        sparse: Sequence[MemoryQueryRow], dense: Sequence[MemoryQueryRow]
    ) -> tuple[MemoryQueryRow, ...]:
        return _rrf(sparse, dense)

    def canonical_refetch(self, **kwargs) -> tuple[MemoryQueryRow, ...]:
        return self._canonical_refetch(**kwargs)

    def get(
        self,
        *,
        read_binding: FrozenMemoryReadContextBinding,
        fact_id: str,
        deadline_monotonic: float,
    ) -> MemoryQueryRow | None:
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT id, memory_domain_id, context_id, fact_kind,
                       lifecycle, statement, accepted_at, fact_semantic_digest,
                       user_edited_at
                FROM pulsara_v3.memory_facts
                WHERE memory_domain_id = %s AND id = %s
                  AND context_id=ANY(%s::text[])
                """,
                (
                    read_binding.memory_domain_id,
                    fact_id,
                    list(read_binding.readable_context_ids),
                ),
            ).fetchone()
        return None if row is None else _row(
            row, user_edited_at=row["user_edited_at"].isoformat()
            if row["user_edited_at"] is not None else None,
        )

    def response_preference_snapshot(
        self,
        *,
        read_binding: FrozenMemoryReadContextBinding,
        deadline_monotonic: float,
    ) -> MemoryResponsePreferenceSnapshot:
        """Select a stable bounded head without imposing a storage total cap."""

        from pulsara_agent.conversation_kernel.memory.contracts import (
            memory_response_preference_item_payload,
        )
        from pulsara_agent.primitives.context import canonical_json_bytes

        visible = sorted(
            read_binding.readable_context_ids,
            key=lambda value: (value != "ctx:global", value),
        )
        chosen: list[MemoryQueryRow] = []
        incomplete = False
        conflicts_omitted = False
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            for context_id in visible:
                eligible = connection.execute(
                    """
                    SELECT f.id, f.memory_domain_id, f.context_id, f.fact_kind,
                           f.lifecycle, f.statement, f.accepted_at,
                           f.fact_semantic_digest
                    FROM pulsara_v3.memory_facts AS f
                    WHERE f.memory_domain_id=%s AND f.context_id=%s
                      AND f.fact_kind='RESPONSE_PREFERENCE' AND f.lifecycle='ACTIVE'
                      AND NOT EXISTS (
                          SELECT 1 FROM pulsara_v3.memory_relations AS r
                          JOIN pulsara_v3.memory_facts AS other
                            ON other.memory_domain_id=r.memory_domain_id
                           AND other.id=CASE WHEN r.source_fact_id=f.id
                                             THEN r.target_fact_id
                                             ELSE r.source_fact_id END
                          WHERE r.memory_domain_id=f.memory_domain_id
                            AND r.relation_kind='CONTRADICTS'
                            AND (r.source_fact_id=f.id OR r.target_fact_id=f.id)
                            AND other.lifecycle='ACTIVE'
                            AND other.context_id=f.context_id
                            AND other.fact_kind='RESPONSE_PREFERENCE'
                      )
                    ORDER BY f.accepted_at DESC, f.id DESC LIMIT 17
                    """,
                    (read_binding.memory_domain_id, context_id),
                ).fetchall()
                selected: list[MemoryQueryRow] = []
                for row in eligible[:16]:
                    item = _row(row)
                    proposed = (*selected, item)
                    serialized = tuple(
                        memory_response_preference_item_payload(
                            memory_id=value.fact_id,
                            context_id=value.context_id,
                            statement=value.statement,
                            recorded_at=value.recorded_at,
                        )
                        for value in proposed
                    )
                    if len(canonical_json_bytes(serialized)) > 7 * 1024:
                        incomplete = True
                        break
                    selected.append(item)
                incomplete = incomplete or len(eligible) > len(selected)
                chosen.extend(selected)
                conflicted = connection.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pulsara_v3.memory_facts AS f
                        JOIN pulsara_v3.memory_relations AS r
                          ON r.memory_domain_id=f.memory_domain_id
                         AND r.relation_kind='CONTRADICTS'
                         AND (r.source_fact_id=f.id OR r.target_fact_id=f.id)
                        JOIN pulsara_v3.memory_facts AS other
                          ON other.memory_domain_id=r.memory_domain_id
                         AND other.id=CASE WHEN r.source_fact_id=f.id
                                           THEN r.target_fact_id
                                           ELSE r.source_fact_id END
                        WHERE f.memory_domain_id=%s AND f.context_id=%s
                          AND f.fact_kind='RESPONSE_PREFERENCE'
                          AND f.lifecycle='ACTIVE'
                          AND other.lifecycle='ACTIVE'
                          AND other.context_id=f.context_id
                          AND other.fact_kind='RESPONSE_PREFERENCE'
                    ) AS found
                    """,
                    (read_binding.memory_domain_id, context_id),
                ).fetchone()
                conflicts_omitted = conflicts_omitted or bool(conflicted["found"])
        return MemoryResponsePreferenceSnapshot(
            tuple(chosen), incomplete, conflicts_omitted
        )

    def find_active_semantic(
        self,
        *,
        read_binding: FrozenMemoryReadContextBinding,
        context_id: str,
        fact_semantic_digest: str,
        deadline_monotonic: float,
    ) -> MemoryQueryRow | None:
        """Return the sole ACTIVE exact-semantic winner in one visible context."""

        if context_id not in read_binding.readable_context_ids:
            return None
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT id, memory_domain_id, context_id, fact_kind,
                       lifecycle, statement, accepted_at, fact_semantic_digest
                FROM pulsara_v3.memory_facts
                WHERE memory_domain_id=%s AND context_id=%s
                  AND fact_semantic_digest=%s AND lifecycle='ACTIVE'
                """,
                (
                    read_binding.memory_domain_id,
                    context_id,
                    fact_semantic_digest,
                ),
            ).fetchone()
        return None if row is None else _row(row)

    def related_candidates(
        self,
        *,
        read_binding: FrozenMemoryReadContextBinding,
        context_id: str,
        query: str,
        query_embedding: Sequence[float] | None,
        exclude_fact_id: str | None,
        limit: int = 20,
        deadline_monotonic: float,
    ) -> MemoryRelatedSearchResult:
        """Bounded exact-context recall for a direct remember result."""

        if context_id not in read_binding.readable_context_ids:
            return MemoryRelatedSearchResult(
                (), MemoryDenseCandidateDisposition.NOT_REQUESTED
            )
        bounded_limit = max(1, min(int(limit), 20))
        terms = self._tokenizer.tokenize(query)
        sparse = self._sparse(
            read_binding=read_binding,
            terms=terms,
            context_filter=context_id,
            kind_filter=None,
            limit=20,
            automatic=False,
            deadline_monotonic=deadline_monotonic,
        )
        dense: tuple[MemoryQueryRow, ...] = ()
        dense_disposition = MemoryDenseCandidateDisposition.NOT_REQUESTED
        if query_embedding is not None:
            try:
                dense_batch = self._dense(
                    read_binding=read_binding,
                    vector=query_embedding,
                    context_filter=context_id,
                    kind_filter=None,
                    limit=20,
                    purpose=DenseRecallPurpose.RELATION_CANDIDATES,
                    automatic=False,
                    deadline_monotonic=deadline_monotonic,
                )
            except Exception:
                dense_disposition = MemoryDenseCandidateDisposition.UNAVAILABLE
            else:
                dense = dense_batch.facts
                dense_disposition = dense_batch.disposition
        exact_context = tuple(
            item
            for item in _rrf(sparse, dense)
            if item.context_id == context_id and item.fact_id != exclude_fact_id
        )[:bounded_limit]
        return MemoryRelatedSearchResult(
            facts=self._canonical_refetch(
                read_binding=read_binding,
                ranked=exact_context,
                automatic=False,
                deadline_monotonic=deadline_monotonic,
            ),
            dense_disposition=dense_disposition,
        )

    def active_contradictions(
        self,
        *,
        read_binding: FrozenMemoryReadContextBinding,
        fact_ids: Sequence[str],
        limit: int = 64,
        deadline_monotonic: float,
    ) -> tuple[MemoryRelationRow, ...]:
        if not fact_ids:
            return ()
        bounded_limit = max(1, min(int(limit), 256))
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return self._active_contradictions_in_connection(
                connection,
                read_binding=read_binding,
                fact_ids=fact_ids,
                bounded_limit=bounded_limit,
            )

    @staticmethod
    def _active_contradictions_in_connection(
        connection,
        *,
        read_binding: FrozenMemoryReadContextBinding,
        fact_ids: Sequence[str],
        bounded_limit: int,
    ) -> tuple[MemoryRelationRow, ...]:
        if not fact_ids:
            return ()
        contexts = read_binding.readable_context_ids
        rows = connection.execute(
            """
            SELECT r.id, r.source_fact_id, r.target_fact_id,
                   r.relation_kind, r.supersede_mode
            FROM pulsara_v3.memory_relations AS r
            JOIN pulsara_v3.memory_facts AS s
              ON s.memory_domain_id=r.memory_domain_id AND s.id=r.source_fact_id
            JOIN pulsara_v3.memory_facts AS t
              ON t.memory_domain_id=r.memory_domain_id AND t.id=r.target_fact_id
            WHERE r.memory_domain_id=%s AND r.relation_kind='CONTRADICTS'
              AND s.lifecycle='ACTIVE' AND t.lifecycle='ACTIVE'
              AND (r.source_fact_id=ANY(%s) OR r.target_fact_id=ANY(%s))
              AND r.source_context_id=ANY(%s::text[])
              AND r.target_context_id=ANY(%s::text[])
            ORDER BY r.source_fact_id, r.target_fact_id LIMIT %s
            """,
            (
                read_binding.memory_domain_id,
                list(fact_ids),
                list(fact_ids),
                list(contexts),
                list(contexts),
                bounded_limit,
            ),
        ).fetchall()
        return tuple(
            MemoryRelationRow(
                str(row["id"]),
                str(row["source_fact_id"]),
                str(row["target_fact_id"]),
                str(row["relation_kind"]),
                None if row["supersede_mode"] is None else str(row["supersede_mode"]),
            )
            for row in rows
        )

    def direct_relations(
        self,
        *,
        read_binding: FrozenMemoryReadContextBinding,
        fact_id: str,
        limit: int = 100,
        deadline_monotonic: float,
    ) -> tuple[MemoryRelationRow, ...]:
        contexts = read_binding.readable_context_ids
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = connection.execute(
                """
                SELECT id, source_fact_id, target_fact_id, relation_kind, supersede_mode
                FROM pulsara_v3.memory_relations
                WHERE memory_domain_id = %s
                  AND (source_fact_id = %s OR target_fact_id = %s)
                  AND source_context_id=ANY(%s::text[])
                  AND target_context_id=ANY(%s::text[])
                ORDER BY relation_kind, source_fact_id, target_fact_id
                LIMIT %s
                """,
                (
                    read_binding.memory_domain_id,
                    fact_id,
                    fact_id,
                    list(contexts),
                    list(contexts),
                    max(1, min(limit, 100)),
                ),
            ).fetchall()
        return tuple(
            MemoryRelationRow(
                str(row["id"]), str(row["source_fact_id"]), str(row["target_fact_id"]),
                str(row["relation_kind"]), None if row["supersede_mode"] is None else str(row["supersede_mode"]),
            )
            for row in rows
        )

    def provenance(
        self,
        *,
        read_binding: FrozenMemoryReadContextBinding,
        fact_id: str,
        relation_ids: Sequence[str] = (),
        deadline_monotonic: float,
    ) -> MemoryProvenanceProjection | None:
        """Project direct ToolResult owners without exposing cross-workspace history."""

        contexts = read_binding.readable_context_ids
        bounded_relation_ids = tuple(dict.fromkeys(relation_ids))[:100]
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT s.workspace_id AS origin_workspace_id,
                       s.lifecycle AS source_session_lifecycle,
                       f.source_session_id,
                       tr.tool_call_entry_id, tr.tool_call_id,
                       e.turn_id AS producer_turn_id
                FROM pulsara_v3.memory_facts AS f
                JOIN pulsara_v3.tool_results AS tr
                  ON tr.session_id=f.source_session_id
                 AND tr.id=f.source_tool_result_id
                JOIN pulsara_v3.sessions AS s ON s.id=f.source_session_id
                LEFT JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id=tr.session_id AND e.id=tr.tool_call_entry_id
                 AND e.entry_owner_kind='EXECUTED_TURN'
                WHERE f.memory_domain_id=%s AND f.id=%s
                  AND f.context_id=ANY(%s::text[])
                """,
                (read_binding.memory_domain_id, fact_id, list(contexts)),
            ).fetchone()
            if row is None:
                return None
            same_origin = (
                str(row["origin_workspace_id"]) == read_binding.host_workspace_id
            )
            locator_visible = same_origin and row["source_session_lifecycle"] == "OPEN"
            relation_owners: list[MemoryRelationOwnerProjection] = []
            if bounded_relation_ids:
                owners = connection.execute(
                    """
                    SELECT r.id, s.workspace_id, s.lifecycle,
                           tr.session_id, tr.tool_call_entry_id, tr.tool_call_id,
                           b.tool_name
                    FROM pulsara_v3.memory_relations AS r
                    JOIN pulsara_v3.tool_results AS tr
                      ON tr.session_id=r.owner_session_id
                     AND tr.id=r.owner_tool_result_id
                    JOIN pulsara_v3.sessions AS s ON s.id=tr.session_id
                    JOIN pulsara_v3.assistant_message_blocks AS b
                      ON b.session_id=tr.session_id
                     AND b.assistant_entry_id=tr.tool_call_entry_id
                     AND b.tool_call_id=tr.tool_call_id
                    WHERE r.memory_domain_id=%s AND r.id=ANY(%s::text[])
                      AND r.source_context_id=ANY(%s::text[])
                      AND r.target_context_id=ANY(%s::text[])
                    ORDER BY r.id
                    """,
                    (
                        read_binding.memory_domain_id,
                        list(bounded_relation_ids),
                        list(contexts),
                        list(contexts),
                    ),
                ).fetchall()
                for item in owners:
                    visible = (
                        str(item["workspace_id"]) == read_binding.host_workspace_id
                        and item["lifecycle"] == "OPEN"
                    )
                    relation_owners.append(
                        MemoryRelationOwnerProjection(
                            relation_id=str(item["id"]),
                            provenance_disposition=(
                                "SAME_ORIGIN" if visible else "CROSS_ORIGIN_REDACTED"
                            ),
                            write_tool=str(item["tool_name"]),
                            owner_session_id=str(item["session_id"]) if visible else None,
                            owner_entry_id=(
                                str(item["tool_call_entry_id"]) if visible else None
                            ),
                            owner_tool_call_id=(
                                str(item["tool_call_id"]) if visible else None
                            ),
                        )
                    )
        return MemoryProvenanceProjection(
            provenance_disposition=(
                "SAME_ORIGIN" if locator_visible else "CROSS_ORIGIN_REDACTED"
            ),
            write_tool="remember",
            producer_session_id=(
                str(row["source_session_id"]) if locator_visible else None
            ),
            producer_turn_id=(
                str(row["producer_turn_id"])
                if locator_visible and row["producer_turn_id"] is not None
                else None
            ),
            producer_entry_id=(
                str(row["tool_call_entry_id"]) if locator_visible else None
            ),
            producer_tool_call_id=(
                str(row["tool_call_id"]) if locator_visible else None
            ),
            relation_owners=tuple(relation_owners),
        )

    def _sparse(
        self,
        *,
        read_binding,
        terms,
        kind_filter,
        limit,
        automatic,
        deadline_monotonic,
        context_filter=None,
    ):
        if not terms:
            return ()
        conditions, parameters = _visibility_sql(
            read_binding, kind_filter, context_filter=context_filter
        )
        with self._provider.connection(lane=PostgresConnectionLane.MEMORY_QUERY, row_factory=dict_row, deadline_monotonic=deadline_monotonic) as connection:
            rows = connection.execute(
                f"""
                SELECT id, memory_domain_id, context_id, fact_kind,
                       lifecycle, statement, accepted_at, fact_semantic_digest,
                       ts_rank_cd(search_document, pulsara_v3.memory_terms_to_tsquery(%s::text[])) AS rank
                FROM pulsara_v3.memory_facts
                WHERE memory_domain_id = %s AND lifecycle = 'ACTIVE'
                  AND ({conditions})
                  AND search_terms && %s::text[]
                  AND search_document @@ pulsara_v3.memory_terms_to_tsquery(%s::text[])
                  AND (%s = false OR fact_kind <> 'RESPONSE_PREFERENCE')
                ORDER BY cardinality(ARRAY(
                           SELECT unnest(search_terms)
                           INTERSECT SELECT unnest(%s::text[])
                         )) DESC,
                         rank DESC, id ASC LIMIT %s
                """,
                (
                    list(terms),
                    read_binding.memory_domain_id,
                    *parameters,
                    list(terms),
                    list(terms),
                    automatic,
                    list(terms),
                    limit,
                ),
            ).fetchall()
        return tuple(_row(row, sparse_rank=index + 1) for index, row in enumerate(rows))

    def _dense(
        self,
        *,
        read_binding,
        vector,
        kind_filter,
        limit,
        purpose,
        automatic,
        deadline_monotonic,
        context_filter=None,
    ) -> MemoryDenseCandidateBatch:
        frozen_vector = freeze_v1_embedding_vector(vector)
        minimum_similarity = MEMORY_DENSE_ELIGIBILITY_POLICY.minimum_similarity(
            DenseRecallPurpose(purpose)
        )
        literal = "[" + ",".join(format(value, ".17g") for value in frozen_vector) + "]"
        conditions, parameters = _visibility_sql(
            read_binding, kind_filter, alias="f", context_filter=context_filter
        )
        overfetch = min(int(limit) * 4, 120)
        with self._provider.connection(lane=PostgresConnectionLane.MEMORY_QUERY, row_factory=dict_row, deadline_monotonic=deadline_monotonic) as connection:
            connection.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            connection.execute("SET LOCAL hnsw.max_scan_tuples = 20000")
            rows = connection.execute(
                f"""
                SELECT f.id, f.memory_domain_id, f.context_id,
                       f.fact_kind, f.lifecycle, f.statement, f.accepted_at,
                       f.fact_semantic_digest,
                       e.embedding <=> %s::public.vector AS distance
                FROM pulsara_v3.memory_embeddings e
                JOIN pulsara_v3.memory_facts f
                  ON f.memory_domain_id=e.memory_domain_id AND f.id=e.fact_id
                 AND f.fact_semantic_digest=e.fact_semantic_digest
                WHERE f.memory_domain_id=%s AND f.lifecycle='ACTIVE'
                  AND e.embedding_contract_id=%s
                  AND e.embedding_contract_version=%s AND ({conditions})
                  AND (%s = false OR f.fact_kind <> 'RESPONSE_PREFERENCE')
                ORDER BY e.embedding <=> %s::public.vector ASC LIMIT %s
                """,
                (
                    literal,
                    read_binding.memory_domain_id,
                    MEMORY_EMBEDDING_CONTRACT_ID,
                    MEMORY_EMBEDDING_CONTRACT_VERSION,
                    *parameters,
                    automatic,
                    literal,
                    overfetch,
                ),
            ).fetchall()
            eligible = [
                row
                for row in rows
                if math.isfinite(float(row["distance"]))
                and 1.0 - float(row["distance"]) >= minimum_similarity
            ]
            eligible.sort(key=lambda row: (float(row["distance"]), str(row["id"])))
            selected = eligible[:limit]
            if len(selected) >= limit:
                disposition = MemoryDenseCandidateDisposition.BOUNDED_TOP_K
            elif not eligible and rows:
                # strict distance order proves every later neighbour is no
                # better than the closest bounded rows already below floor.
                disposition = MemoryDenseCandidateDisposition.NO_ELIGIBLE_MATCH
            elif eligible:
                # An iterative HNSW scan that returns fewer than K eligible
                # rows cannot distinguish an exhausted visible set from a
                # filter/scan-bound underfill without issuing a second,
                # potentially unbounded sequential probe.  Keep the bounded
                # result and report that uncertainty honestly.
                disposition = MemoryDenseCandidateDisposition.PARTIAL_BOUNDED_SCAN
            else:
                disposition = MemoryDenseCandidateDisposition.EXHAUSTED_VISIBLE_SET
        return MemoryDenseCandidateBatch(
            facts=tuple(
                _row(row, dense_rank=index + 1)
                for index, row in enumerate(selected)
            ),
            disposition=disposition,
        )

    def _canonical_refetch(self, *, read_binding, ranked, automatic, deadline_monotonic):
        if not ranked:
            return ()
        ids = [row.fact_id for row in ranked]
        conditions, parameters = _visibility_sql(read_binding, None)
        with self._provider.connection(lane=PostgresConnectionLane.MEMORY_QUERY, row_factory=dict_row, deadline_monotonic=deadline_monotonic) as connection:
            rows = connection.execute(
                f"""
                SELECT id, memory_domain_id, context_id, fact_kind,
                       lifecycle, statement, accepted_at, fact_semantic_digest
                FROM pulsara_v3.memory_facts
                WHERE memory_domain_id=%s AND id=ANY(%s) AND lifecycle='ACTIVE'
                  AND ({conditions})
                  AND (%s = false OR fact_kind <> 'RESPONSE_PREFERENCE')
                """,
                (read_binding.memory_domain_id, ids, *parameters, automatic),
            ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        final = []
        for ranked_item in ranked:
            raw = by_id.get(ranked_item.fact_id)
            if raw is None or str(raw["fact_semantic_digest"]) != ranked_item.fact_semantic_digest:
                continue
            final.append(_row(raw, sparse_rank=ranked_item.sparse_rank, dense_rank=ranked_item.dense_rank, fused_score=ranked_item.fused_score, match_tier=ranked_item.match_tier))
        return tuple(final)


def _filter_stages(binding, requested_kind):
    del binding
    stages = [(requested_kind, "EXACT", None)]
    if requested_kind is not None:
        stages.append((None, "RELAX_KIND", "kind"))
    return tuple(stages)


def _stage_result(
    ordinal: int, label: str, *, new_results: int
) -> MemorySearchStageResult:
    kind = "REQUESTED" if label == "EXACT" else "ANY"
    return MemorySearchStageResult(
        ordinal=ordinal,
        context_coverage="ALL_READABLE",
        kind=kind,
        new_results=new_results,
    )


def _aggregate_dense_disposition(
    requested: bool,
    values: Sequence[MemoryDenseCandidateDisposition],
) -> MemoryDenseCandidateDisposition:
    if not requested:
        return MemoryDenseCandidateDisposition.NOT_REQUESTED
    if not values or all(
        value is MemoryDenseCandidateDisposition.UNAVAILABLE for value in values
    ):
        return MemoryDenseCandidateDisposition.UNAVAILABLE
    successful = tuple(
        value
        for value in values
        if value is not MemoryDenseCandidateDisposition.UNAVAILABLE
    )
    if any(
        value is MemoryDenseCandidateDisposition.PARTIAL_BOUNDED_SCAN
        for value in successful
    ):
        return MemoryDenseCandidateDisposition.PARTIAL_BOUNDED_SCAN
    if any(
        value is MemoryDenseCandidateDisposition.BOUNDED_TOP_K
        for value in successful
    ):
        return MemoryDenseCandidateDisposition.BOUNDED_TOP_K
    if any(
        value is MemoryDenseCandidateDisposition.EXHAUSTED_VISIBLE_SET
        for value in successful
    ):
        return MemoryDenseCandidateDisposition.EXHAUSTED_VISIBLE_SET
    return MemoryDenseCandidateDisposition.NO_ELIGIBLE_MATCH


def _visibility_sql(binding, kind_filter, alias="", context_filter=None):
    prefix = f"{alias}." if alias else ""
    visible = binding.readable_context_ids
    if context_filter is not None:
        visible = tuple(item for item in visible if item == context_filter)
    if not visible:
        return "false", []
    expression = f"{prefix}context_id=ANY(%s::text[])"
    parameters = [list(visible)]
    if kind_filter is not None:
        expression += f" AND {prefix}fact_kind=%s"
        parameters.append(str(kind_filter))
    return expression, parameters


def _rrf(sparse, dense):
    by_id = {}
    for item in (*sparse, *dense):
        existing = by_id.get(item.fact_id, item)
        by_id[item.fact_id] = MemoryQueryRow(
            fact_id=item.fact_id, memory_domain_id=item.memory_domain_id,
            context_id=item.context_id, fact_kind=item.fact_kind,
            lifecycle=item.lifecycle, statement=item.statement,
            recorded_at=item.recorded_at, fact_semantic_digest=item.fact_semantic_digest,
            sparse_rank=item.sparse_rank or existing.sparse_rank,
            dense_rank=item.dense_rank or existing.dense_rank,
        )
    fused = []
    for item in by_id.values():
        score = sum(1.0 / (RRF_K + rank) for rank in (item.sparse_rank, item.dense_rank) if rank is not None)
        fused.append(MemoryQueryRow(**{field: getattr(item, field) for field in (
            "fact_id", "memory_domain_id", "context_id", "fact_kind",
            "lifecycle", "statement", "recorded_at",
            "fact_semantic_digest", "sparse_rank", "dense_rank")}, fused_score=score))
    return tuple(sorted(fused, key=lambda item: (-item.fused_score, item.fact_id)))


def _row(row, **extra):
    return MemoryQueryRow(
        fact_id=str(row["id"]), memory_domain_id=str(row["memory_domain_id"]),
        context_id=str(row["context_id"]),
        fact_kind=str(row["fact_kind"]), lifecycle=str(row["lifecycle"]),
        statement=str(row["statement"]),
        recorded_at=canonical_memory_recorded_at(row["accepted_at"]),
        fact_semantic_digest=str(row["fact_semantic_digest"]), **extra,
    )


__all__ = [
    "MAXIMUM_MEMORY_QUERY_RESULTS",
    "MEMORY_EMBEDDING_CONTRACT_ID",
    "MEMORY_EMBEDDING_CONTRACT_VERSION",
    "MemoryDenseCandidateBatch",
    "MemoryDenseCandidateDisposition",
    "MemoryQueryResult",
    "MemoryQueryRow",
    "MemoryRelationRow",
    "MemoryRetrievalDisposition",
    "MemorySearchStageResult",
    "PostgresMemoryQuery",
]
