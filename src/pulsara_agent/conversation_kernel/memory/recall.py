"""Bounded advisory-memory recall over canonical PostgreSQL rows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Sequence

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.memory.scope import FrozenMemoryReadScopeBinding, MemoryScopeKind
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
class MemorySearchStageResult:
    ordinal: int
    scope: str
    kind: str
    new_results: int


@dataclass(frozen=True, slots=True)
class MemoryQueryRow:
    fact_id: str
    memory_domain_id: str
    scope_kind: str
    scope_id: str
    fact_kind: str
    lifecycle: str
    statement: str
    applies_when: str | None
    do_not_apply_when: tuple[str, ...]
    fact_semantic_digest: str
    sparse_rank: int | None = None
    dense_rank: int | None = None
    fused_score: float = 0.0
    match_tier: int = 0

    @property
    def fact_payload(self) -> dict[str, object]:
        return {
            "scope": self.scope_kind,
            "scope_id": self.scope_id,
            "statement": self.statement,
            "applies_when": self.applies_when,
            "do_not_apply_when": list(self.do_not_apply_when),
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
    """One bounded RR cut of active preferences and their contradictions."""

    facts: tuple[MemoryQueryRow, ...]
    contradictions: tuple[MemoryRelationRow, ...]


@dataclass(frozen=True, slots=True)
class MemoryRelationDecisionProjection:
    relation_id: str
    provenance_disposition: str
    decision_kind: str
    decision_reason_code: str | None
    decision_public_summary: str | None


@dataclass(frozen=True, slots=True)
class MemoryProvenanceProjection:
    provenance_disposition: str
    producer_kind: str
    decision_kind: str
    decision_reason_code: str | None
    decision_public_summary: str | None
    producer_session_id: str | None
    producer_turn_id: str | None
    producer_entry_id: str | None
    producer_tool_call_id: str | None
    tool_result_ids: tuple[str, ...]
    relation_decisions: tuple[MemoryRelationDecisionProjection, ...]


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
        read_binding: FrozenMemoryReadScopeBinding,
        query: str,
        limit: int = 5,
        requested_scope: MemoryScopeKind | str | None = None,
        requested_kind: str | None = None,
        query_embedding: Sequence[float] | None = None,
        automatic: bool = False,
        deadline_monotonic: float,
    ) -> MemoryQueryResult:
        limit = max(1, min(int(limit), MAXIMUM_MEMORY_QUERY_RESULTS))
        terms = self._tokenizer.tokenize(query)
        stages = _filter_stages(read_binding, requested_scope, requested_kind)
        gathered: list[MemoryQueryRow] = []
        seen: set[str] = set()
        attempted: list[MemorySearchStageResult] = []
        relaxed: list[str] = []
        sparse_ok = True
        dense_ok = query_embedding is not None
        dense_dispositions: list[MemoryDenseCandidateDisposition] = []
        for ordinal, (scope_filter, kind_filter, label, relaxed_field) in enumerate(stages):
            if relaxed_field is not None:
                relaxed.append(relaxed_field)
            try:
                sparse = self._sparse(
                    read_binding=read_binding,
                    terms=terms,
                    scope_filter=scope_filter,
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
                        scope_filter=scope_filter,
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
                            "fact_id", "memory_domain_id", "scope_kind", "scope_id",
                            "fact_kind", "lifecycle", "statement", "applies_when",
                            "do_not_apply_when", "fact_semantic_digest", "sparse_rank",
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
        read_binding: FrozenMemoryReadScopeBinding,
        requested_scope: MemoryScopeKind | str | None,
        requested_kind: str | None,
    ):
        return _filter_stages(read_binding, requested_scope, requested_kind)

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
        read_binding: FrozenMemoryReadScopeBinding,
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
                SELECT id, memory_domain_id, scope_kind, scope_id, fact_kind,
                       lifecycle, statement, applies_when, do_not_apply_when,
                       fact_semantic_digest
                FROM pulsara_v3.memory_facts
                WHERE memory_domain_id = %s AND id = %s
                  AND (scope_kind, scope_id) IN (
                    SELECT * FROM unnest(%s::text[], %s::text[])
                  )
                """,
                (
                    read_binding.memory_domain_id,
                    fact_id,
                    [scope.kind.value for scope in read_binding.readable_scopes],
                    [scope.scope_id for scope in read_binding.readable_scopes],
                ),
            ).fetchone()
        return None if row is None else _row(row)

    def response_preferences(
        self,
        *,
        read_binding: FrozenMemoryReadScopeBinding,
        deadline_monotonic: float,
    ) -> tuple[MemoryQueryRow, ...]:
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return self._response_preferences_in_connection(
                connection, read_binding=read_binding
            )

    def response_preference_snapshot(
        self,
        *,
        read_binding: FrozenMemoryReadScopeBinding,
        relation_limit: int = 240,
        deadline_monotonic: float,
    ) -> MemoryResponsePreferenceSnapshot:
        """Read the complete preference head from one repeatable-read cut."""

        bounded_limit = max(1, min(int(relation_limit), 256))
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            facts = self._response_preferences_in_connection(
                connection, read_binding=read_binding
            )
            contradictions = self._active_contradictions_in_connection(
                connection,
                read_binding=read_binding,
                fact_ids=tuple(item.fact_id for item in facts),
                bounded_limit=bounded_limit,
            )
        return MemoryResponsePreferenceSnapshot(facts, contradictions)

    @staticmethod
    def _response_preferences_in_connection(
        connection,
        *,
        read_binding: FrozenMemoryReadScopeBinding,
    ) -> tuple[MemoryQueryRow, ...]:
        conditions, parameters = _visibility_sql(
            read_binding, None, "RESPONSE_PREFERENCE"
        )
        rows = connection.execute(
            f"""
            SELECT id, memory_domain_id, scope_kind, scope_id, fact_kind,
                   lifecycle, statement, applies_when, do_not_apply_when,
                   fact_semantic_digest
            FROM pulsara_v3.memory_facts
            WHERE memory_domain_id=%s AND lifecycle='ACTIVE' AND ({conditions})
            ORDER BY CASE scope_kind WHEN 'USER' THEN 0 ELSE 1 END,
                     fact_semantic_digest, id
            LIMIT 33
            """,
            (read_binding.memory_domain_id, *parameters),
        ).fetchall()
        return tuple(_row(row) for row in rows)

    def find_active_semantic(
        self,
        *,
        read_binding: FrozenMemoryReadScopeBinding,
        scope_kind: MemoryScopeKind,
        scope_id: str,
        fact_semantic_digest: str,
        deadline_monotonic: float,
    ) -> MemoryQueryRow | None:
        """Return the sole ACTIVE exact-semantic winner in one visible scope."""

        if not any(
            item.kind is scope_kind and item.scope_id == scope_id
            for item in read_binding.readable_scopes
        ):
            return None
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT id, memory_domain_id, scope_kind, scope_id, fact_kind,
                       lifecycle, statement, applies_when, do_not_apply_when,
                       fact_semantic_digest
                FROM pulsara_v3.memory_facts
                WHERE memory_domain_id=%s AND scope_kind=%s AND scope_id=%s
                  AND fact_semantic_digest=%s AND lifecycle='ACTIVE'
                """,
                (
                    read_binding.memory_domain_id,
                    scope_kind.value,
                    scope_id,
                    fact_semantic_digest,
                ),
            ).fetchone()
        return None if row is None else _row(row)

    def governance_related(
        self,
        *,
        read_binding: FrozenMemoryReadScopeBinding,
        scope_kind: MemoryScopeKind,
        scope_id: str,
        query: str,
        query_embedding: Sequence[float] | None,
        exclude_fact_id: str | None,
        limit: int = 8,
        deadline_monotonic: float,
    ) -> tuple[MemoryQueryRow, ...]:
        """Bounded exact-scope relatedness; never relax scope or call rerank."""

        if not any(
            item.kind is scope_kind and item.scope_id == scope_id
            for item in read_binding.readable_scopes
        ):
            return ()
        bounded_limit = max(1, min(int(limit), 8))
        terms = self._tokenizer.tokenize(query)
        sparse = self._sparse(
            read_binding=read_binding,
            terms=terms,
            scope_filter=scope_kind,
            kind_filter=None,
            limit=20,
            automatic=False,
            deadline_monotonic=deadline_monotonic,
        )
        dense: tuple[MemoryQueryRow, ...] = ()
        if query_embedding is not None:
            dense = self._dense(
                read_binding=read_binding,
                vector=query_embedding,
                scope_filter=scope_kind,
                kind_filter=None,
                limit=20,
                purpose=DenseRecallPurpose.GOVERNANCE_RELATEDNESS,
                automatic=False,
                deadline_monotonic=deadline_monotonic,
            ).facts
        exact_scope = tuple(
            item
            for item in _rrf(sparse, dense)
            if item.scope_kind == scope_kind.value
            and item.scope_id == scope_id
            and item.fact_id != exclude_fact_id
        )[:bounded_limit]
        return self._canonical_refetch(
            read_binding=read_binding,
            ranked=exact_scope,
            automatic=False,
            deadline_monotonic=deadline_monotonic,
        )

    def governance_sparse_candidates(
        self,
        *,
        read_binding: FrozenMemoryReadScopeBinding,
        scope_kind: MemoryScopeKind,
        query: str,
        deadline_monotonic: float,
    ) -> tuple[MemoryQueryRow, ...]:
        return self._sparse(
            read_binding=read_binding,
            terms=self._tokenizer.tokenize(query),
            scope_filter=scope_kind,
            kind_filter=None,
            limit=30,
            automatic=False,
            deadline_monotonic=deadline_monotonic,
        )

    def governance_dense_candidates(
        self,
        *,
        read_binding: FrozenMemoryReadScopeBinding,
        scope_kind: MemoryScopeKind,
        query_embedding: Sequence[float],
        deadline_monotonic: float,
    ) -> MemoryDenseCandidateBatch:
        return self._dense(
            read_binding=read_binding,
            vector=query_embedding,
            scope_filter=scope_kind,
            kind_filter=None,
            limit=30,
            purpose=DenseRecallPurpose.GOVERNANCE_RELATEDNESS,
            automatic=False,
            deadline_monotonic=deadline_monotonic,
        )

    def finalize_governance_related(
        self,
        *,
        read_binding: FrozenMemoryReadScopeBinding,
        scope_kind: MemoryScopeKind,
        scope_id: str,
        sparse: Sequence[MemoryQueryRow],
        dense: Sequence[MemoryQueryRow],
        exclude_fact_id: str | None,
        limit: int,
        deadline_monotonic: float,
    ) -> tuple[MemoryQueryRow, ...]:
        bounded_limit = max(1, min(int(limit), 8))
        exact_scope = tuple(
            item
            for item in _rrf(sparse, dense)
            if item.scope_kind == scope_kind.value
            and item.scope_id == scope_id
            and item.fact_id != exclude_fact_id
        )[:bounded_limit]
        return self._canonical_refetch(
            read_binding=read_binding,
            ranked=exact_scope,
            automatic=False,
            deadline_monotonic=deadline_monotonic,
        )

    def active_contradictions(
        self,
        *,
        read_binding: FrozenMemoryReadScopeBinding,
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
        read_binding: FrozenMemoryReadScopeBinding,
        fact_ids: Sequence[str],
        bounded_limit: int,
    ) -> tuple[MemoryRelationRow, ...]:
        if not fact_ids:
            return ()
        scopes = read_binding.readable_scopes
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
              AND (r.source_scope_kind, r.source_scope_id) IN (
                SELECT * FROM unnest(%s::text[], %s::text[])
              )
              AND (r.target_scope_kind, r.target_scope_id) IN (
                SELECT * FROM unnest(%s::text[], %s::text[])
              )
            ORDER BY r.source_fact_id, r.target_fact_id LIMIT %s
            """,
            (
                read_binding.memory_domain_id,
                list(fact_ids),
                list(fact_ids),
                [scope.kind.value for scope in scopes],
                [scope.scope_id for scope in scopes],
                [scope.kind.value for scope in scopes],
                [scope.scope_id for scope in scopes],
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
        read_binding: FrozenMemoryReadScopeBinding,
        fact_id: str,
        limit: int = 100,
        deadline_monotonic: float,
    ) -> tuple[MemoryRelationRow, ...]:
        scopes = read_binding.readable_scopes
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
                  AND (source_scope_kind, source_scope_id) IN (
                    SELECT * FROM unnest(%s::text[], %s::text[])
                  )
                  AND (target_scope_kind, target_scope_id) IN (
                    SELECT * FROM unnest(%s::text[], %s::text[])
                  )
                ORDER BY relation_kind, source_fact_id, target_fact_id
                LIMIT %s
                """,
                (
                    read_binding.memory_domain_id,
                    fact_id,
                    fact_id,
                    [scope.kind.value for scope in scopes],
                    [scope.scope_id for scope in scopes],
                    [scope.kind.value for scope in scopes],
                    [scope.scope_id for scope in scopes],
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
        read_binding: FrozenMemoryReadScopeBinding,
        fact_id: str,
        relation_ids: Sequence[str] = (),
        deadline_monotonic: float,
    ) -> MemoryProvenanceProjection | None:
        """Project producer/decision lineage with an exact workspace fence."""

        scopes = read_binding.readable_scopes
        bounded_relation_ids = tuple(dict.fromkeys(relation_ids))[:100]
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT c.origin_workspace_id, c.origin_session_id,
                       c.producer_kind, c.producer_entry_id,
                       c.producer_tool_call_id, c.trigger_user_entry_id,
                       c.decision_kind, c.decision_reason_code,
                       c.decision_public_summary, e.turn_id AS producer_turn_id,
                       f.scope_kind
                FROM pulsara_v3.memory_facts AS f
                JOIN pulsara_v3.memory_candidates AS c
                  ON c.id=f.source_candidate_id
                LEFT JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id=c.origin_session_id
                 AND e.id=coalesce(c.producer_entry_id, c.trigger_user_entry_id)
                WHERE f.memory_domain_id=%s AND f.id=%s
                  AND (f.scope_kind, f.scope_id) IN (
                    SELECT * FROM unnest(%s::text[], %s::text[])
                  )
                """,
                (
                    read_binding.memory_domain_id,
                    fact_id,
                    [scope.kind.value for scope in scopes],
                    [scope.scope_id for scope in scopes],
                ),
            ).fetchone()
            if row is None:
                return None
            same_origin = (
                str(row["origin_workspace_id"])
                == read_binding.host_workspace_id
            )
            if str(row["scope_kind"]) == "WORKSPACE" and not same_origin:
                # A WORKSPACE fact cannot legitimately cross its producer
                # workspace.  Treat inconsistent identity as invisible.
                return None
            citations = ()
            if same_origin:
                citations = tuple(
                    str(item["tool_result_id"])
                    for item in connection.execute(
                        """
                        SELECT tool_result_id
                        FROM pulsara_v3.memory_candidate_tool_result_refs
                        WHERE candidate_id=(
                            SELECT source_candidate_id
                            FROM pulsara_v3.memory_facts
                            WHERE memory_domain_id=%s AND id=%s
                        )
                        ORDER BY ordinal LIMIT 8
                        """,
                        (read_binding.memory_domain_id, fact_id),
                    ).fetchall()
                )
            relation_decisions: list[MemoryRelationDecisionProjection] = []
            if bounded_relation_ids:
                decision_rows = connection.execute(
                    """
                    SELECT r.id, c.origin_workspace_id, c.decision_kind,
                           c.decision_reason_code, c.decision_public_summary
                    FROM pulsara_v3.memory_relations AS r
                    JOIN pulsara_v3.memory_candidates AS c
                      ON c.memory_domain_id=r.memory_domain_id
                     AND c.id=r.decision_candidate_id
                    WHERE r.memory_domain_id=%s AND r.id=ANY(%s)
                      AND (r.source_scope_kind, r.source_scope_id) IN (
                        SELECT * FROM unnest(%s::text[], %s::text[])
                      )
                      AND (r.target_scope_kind, r.target_scope_id) IN (
                        SELECT * FROM unnest(%s::text[], %s::text[])
                      )
                    ORDER BY r.id
                    """,
                    (
                        read_binding.memory_domain_id,
                        list(bounded_relation_ids),
                        [scope.kind.value for scope in scopes],
                        [scope.scope_id for scope in scopes],
                        [scope.kind.value for scope in scopes],
                        [scope.scope_id for scope in scopes],
                    ),
                ).fetchall()
                relation_decisions.extend(
                    MemoryRelationDecisionProjection(
                        relation_id=str(item["id"]),
                        provenance_disposition=(
                            "SAME_ORIGIN"
                            if str(item["origin_workspace_id"])
                            == read_binding.host_workspace_id
                            else "CROSS_ORIGIN_REDACTED"
                        ),
                        decision_kind=str(item["decision_kind"]),
                        decision_reason_code=(
                            None
                            if item["decision_reason_code"] is None
                            else str(item["decision_reason_code"])
                        ),
                        decision_public_summary=(
                            None
                            if item["decision_public_summary"] is None
                            else str(item["decision_public_summary"])
                        ),
                    )
                    for item in decision_rows
                )
        return MemoryProvenanceProjection(
            provenance_disposition=(
                "SAME_ORIGIN" if same_origin else "CROSS_ORIGIN_REDACTED"
            ),
            producer_kind=str(row["producer_kind"]),
            decision_kind=str(row["decision_kind"]),
            decision_reason_code=(
                None
                if row["decision_reason_code"] is None
                else str(row["decision_reason_code"])
            ),
            decision_public_summary=(
                None
                if row["decision_public_summary"] is None
                else str(row["decision_public_summary"])
            ),
            producer_session_id=(
                str(row["origin_session_id"]) if same_origin else None
            ),
            producer_turn_id=(
                None
                if not same_origin or row["producer_turn_id"] is None
                else str(row["producer_turn_id"])
            ),
            producer_entry_id=(
                None
                if not same_origin
                else str(
                    row["producer_entry_id"] or row["trigger_user_entry_id"]
                )
            ),
            producer_tool_call_id=(
                None
                if not same_origin or row["producer_tool_call_id"] is None
                else str(row["producer_tool_call_id"])
            ),
            tool_result_ids=citations,
            relation_decisions=tuple(relation_decisions),
        )

    def _sparse(self, *, read_binding, terms, scope_filter, kind_filter, limit, automatic, deadline_monotonic):
        if not terms:
            return ()
        conditions, parameters = _visibility_sql(read_binding, scope_filter, kind_filter)
        with self._provider.connection(lane=PostgresConnectionLane.MEMORY_QUERY, row_factory=dict_row, deadline_monotonic=deadline_monotonic) as connection:
            rows = connection.execute(
                f"""
                SELECT id, memory_domain_id, scope_kind, scope_id, fact_kind,
                       lifecycle, statement, applies_when, do_not_apply_when,
                       fact_semantic_digest,
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
        scope_filter,
        kind_filter,
        limit,
        purpose,
        automatic,
        deadline_monotonic,
    ) -> MemoryDenseCandidateBatch:
        frozen_vector = freeze_v1_embedding_vector(vector)
        minimum_similarity = MEMORY_DENSE_ELIGIBILITY_POLICY.minimum_similarity(
            DenseRecallPurpose(purpose)
        )
        literal = "[" + ",".join(format(value, ".17g") for value in frozen_vector) + "]"
        conditions, parameters = _visibility_sql(read_binding, scope_filter, kind_filter, alias="f")
        overfetch = min(int(limit) * 4, 120)
        with self._provider.connection(lane=PostgresConnectionLane.MEMORY_QUERY, row_factory=dict_row, deadline_monotonic=deadline_monotonic) as connection:
            connection.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            connection.execute("SET LOCAL hnsw.max_scan_tuples = 20000")
            rows = connection.execute(
                f"""
                SELECT f.id, f.memory_domain_id, f.scope_kind, f.scope_id,
                       f.fact_kind, f.lifecycle, f.statement, f.applies_when,
                       f.do_not_apply_when, f.fact_semantic_digest,
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
        conditions, parameters = _visibility_sql(read_binding, None, None)
        with self._provider.connection(lane=PostgresConnectionLane.MEMORY_QUERY, row_factory=dict_row, deadline_monotonic=deadline_monotonic) as connection:
            rows = connection.execute(
                f"""
                SELECT id, memory_domain_id, scope_kind, scope_id, fact_kind,
                       lifecycle, statement, applies_when, do_not_apply_when,
                       fact_semantic_digest
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


def _filter_stages(binding, requested_scope, requested_kind):
    scope = None if requested_scope is None else MemoryScopeKind(requested_scope)
    if scope is not None and not any(item.kind is scope for item in binding.readable_scopes):
        raise ValueError("requested memory scope is not visible")
    stages = [(scope, requested_kind, "EXACT", None)]
    if requested_kind is not None:
        stages.append((scope, None, "RELAX_KIND", "kind"))
    if scope is not None:
        stages.append((None, requested_kind, "RELAX_SCOPE", "scope"))
    if scope is not None and requested_kind is not None:
        stages.append((None, None, "RELAX_SCOPE_AND_KIND", "scope+kind"))
    return tuple(dict.fromkeys(stages))


def _stage_result(
    ordinal: int, label: str, *, new_results: int
) -> MemorySearchStageResult:
    scope = "REQUESTED" if label in {"EXACT", "RELAX_KIND"} else "ALL_VISIBLE"
    kind = "REQUESTED" if label in {"EXACT", "RELAX_SCOPE"} else "ANY"
    return MemorySearchStageResult(
        ordinal=ordinal,
        scope=scope,
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


def _visibility_sql(binding, scope_filter, kind_filter, alias=""):
    prefix = f"{alias}." if alias else ""
    parts = []
    parameters = []
    visible = binding.readable_scopes
    if scope_filter is not None:
        visible = tuple(item for item in visible if item.kind is scope_filter)
    for scope in visible:
        parts.append(f"({prefix}scope_kind=%s AND {prefix}scope_id=%s)")
        parameters.extend((scope.kind.value, scope.scope_id))
    if not parts:
        return "false", []
    expression = " OR ".join(parts)
    if kind_filter is not None:
        expression = f"({expression}) AND {prefix}fact_kind=%s"
        parameters.append(str(kind_filter))
    return expression, parameters


def _rrf(sparse, dense):
    by_id = {}
    for item in (*sparse, *dense):
        existing = by_id.get(item.fact_id, item)
        by_id[item.fact_id] = MemoryQueryRow(
            fact_id=item.fact_id, memory_domain_id=item.memory_domain_id,
            scope_kind=item.scope_kind, scope_id=item.scope_id, fact_kind=item.fact_kind,
            lifecycle=item.lifecycle, statement=item.statement, applies_when=item.applies_when,
            do_not_apply_when=item.do_not_apply_when, fact_semantic_digest=item.fact_semantic_digest,
            sparse_rank=item.sparse_rank or existing.sparse_rank,
            dense_rank=item.dense_rank or existing.dense_rank,
        )
    fused = []
    for item in by_id.values():
        score = sum(1.0 / (RRF_K + rank) for rank in (item.sparse_rank, item.dense_rank) if rank is not None)
        fused.append(MemoryQueryRow(**{field: getattr(item, field) for field in (
            "fact_id", "memory_domain_id", "scope_kind", "scope_id", "fact_kind",
            "lifecycle", "statement", "applies_when", "do_not_apply_when",
            "fact_semantic_digest", "sparse_rank", "dense_rank")}, fused_score=score))
    return tuple(sorted(fused, key=lambda item: (-item.fused_score, item.fact_id)))


def _row(row, **extra):
    return MemoryQueryRow(
        fact_id=str(row["id"]), memory_domain_id=str(row["memory_domain_id"]),
        scope_kind=str(row["scope_kind"]), scope_id=str(row["scope_id"]),
        fact_kind=str(row["fact_kind"]), lifecycle=str(row["lifecycle"]),
        statement=str(row["statement"]), applies_when=None if row["applies_when"] is None else str(row["applies_when"]),
        do_not_apply_when=tuple(str(x) for x in row["do_not_apply_when"]),
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
