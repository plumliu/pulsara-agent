"""PostgreSQL-only Stage 2 memory read/index plane."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import math
from time import monotonic
from typing import Mapping, Sequence

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.conversation_kernel.contracts import (
    JobAttemptClaimGuard,
    MemoryQueryDisposition,
    canonical_digest,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.job_catalog import (
    MEMORY_INDEX_REFRESH,
    job_handler_contract,
)
from pulsara_agent.retrieval.embedding.protocol import EmbeddingProvider
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


INDEX_HANDLER_CONTRACT_ID = "postgres-memory-index"
INDEX_HANDLER_CONTRACT_VERSION = 1
MAXIMUM_MEMORY_QUERY_RESULTS = 50


@dataclass(frozen=True, slots=True)
class MemoryQueryRow:
    fact_id: str
    fact_kind: str
    lifecycle: str
    fact_payload: Mapping[str, object]
    semantic_digest: str


@dataclass(frozen=True, slots=True)
class MemoryRelationPath:
    source_fact_id: str
    target_fact_id: str
    relation_kinds: tuple[str, ...]
    hop_count: int


@dataclass(frozen=True, slots=True)
class MemoryQueryResult:
    disposition: MemoryQueryDisposition
    reason: str | None
    desired_generation: int
    applied_generation: int
    facts: tuple[MemoryQueryRow, ...]
    paths: tuple[MemoryRelationPath, ...]
    channels: tuple["MemoryIndexChannelState", ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryIndexChannelState:
    channel: str
    desired_generation: int
    applied_generation: int
    disposition: MemoryQueryDisposition
    reason: str | None


class MemoryIndexCoordinator:
    """Level scanner and exact job-attempt index application owner."""

    def __init__(
        self,
        repository: ConversationKernelRepository,
        *,
        handler_contract_id: str = INDEX_HANDLER_CONTRACT_ID,
        handler_contract_version: int = INDEX_HANDLER_CONTRACT_VERSION,
    ) -> None:
        if handler_contract_version < 1:
            raise ValueError("memory index contract version must be positive")
        self._repository = repository
        self._provider = repository.connection_provider
        self._contract_id = handler_contract_id
        self._contract_version = handler_contract_version

    @property
    def handler_contract_id(self) -> str:
        return self._contract_id

    @property
    def handler_contract_version(self) -> int:
        return self._contract_version

    def scan_lost_wakes(self, *, deadline_monotonic: float) -> int:
        created = 0
        contract = job_handler_contract(MEMORY_INDEX_REFRESH)
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_index_state
                WHERE desired_generation > applied_generation
                  AND desired_handler_contract_id = %s
                  AND desired_handler_contract_version = %s
                ORDER BY workspace_id, channel
                """,
                (self._contract_id, self._contract_version),
            ).fetchall()
            for row in rows:
                key = _index_intent_key(
                    str(row["workspace_id"]),
                    str(row["channel"]),
                    int(row["desired_generation"]),
                    self._contract_id,
                    self._contract_version,
                )
                job_id = "job:" + sha256(key.encode()).hexdigest()
                intent = {
                    "workspace_id": str(row["workspace_id"]),
                    "channel": str(row["channel"]),
                    "target_generation": int(row["desired_generation"]),
                    "handler_contract_id": self._contract_id,
                    "handler_contract_version": self._contract_version,
                }
                inserted = connection.execute(
                    """
                    INSERT INTO pulsara_v3.durable_jobs (
                        id, workspace_id, origin_session_id, handler_type,
                        intent_schema_version, intent_digest, intent_payload,
                        automatic_intent_key, safety_class, status,
                        retry_policy_id, retry_policy_version, maximum_attempts,
                        attempt_timeout_ms, next_eligible_at
                    ) VALUES (
                        %s, %s, NULL, 'MEMORY_INDEX_REFRESH',
                        'memory_index_refresh.v1', %s, %s, %s,
                        %s, 'PENDING', %s, %s, %s, %s, clock_timestamp()
                    ) ON CONFLICT (handler_type, automatic_intent_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        job_id,
                        row["workspace_id"],
                        canonical_digest(
                            "pulsara:job-intent:memory_index_refresh.v1", intent
                        ),
                        Jsonb(intent),
                        key,
                        contract.safety_class.value,
                        contract.retry_policy_id,
                        contract.retry_policy_version,
                        contract.maximum_attempts,
                        contract.attempt_timeout_ms,
                    ),
                ).fetchone()
                created += inserted is not None
        return created

    def apply_fts_refresh(
        self,
        guard: JobAttemptClaimGuard,
        *,
        deadline_monotonic: float,
    ) -> int:
        return self._repository.apply_fts_memory_index(
            guard,
            handler_contract_id=self._contract_id,
            handler_contract_version=self._contract_version,
            deadline_monotonic=deadline_monotonic,
        )


class PostgresMemoryQuery:
    def __init__(
        self,
        connection_provider: VerifiedPostgresConnectionProviderProtocol,
        *,
        handler_contract_id: str = INDEX_HANDLER_CONTRACT_ID,
        handler_contract_version: int = INDEX_HANDLER_CONTRACT_VERSION,
    ) -> None:
        self._provider = connection_provider
        self._contract_id = handler_contract_id
        self._contract_version = handler_contract_version

    def search(
        self,
        *,
        workspace_id: str,
        query: str,
        limit: int = 10,
        max_hops: int = 0,
        query_embedding: Sequence[float] | None = None,
        deadline_monotonic: float,
    ) -> MemoryQueryResult:
        if not query.strip() or not 1 <= limit <= MAXIMUM_MEMORY_QUERY_RESULTS:
            raise ValueError("memory query input is outside its bound")
        if max_hops not in {0, 1, 2}:
            raise ValueError("memory query max_hops must be 0, 1, or 2")
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            states = connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_index_state
                WHERE workspace_id = %s AND channel IN ('FTS', 'VECTOR')
                ORDER BY channel
                """,
                (workspace_id,),
            ).fetchall()
            by_channel = {str(row["channel"]): row for row in states}
            channel_states = tuple(
                self._channel_state(
                    connection,
                    workspace_id=workspace_id,
                    channel=channel,
                    state=by_channel.get(channel),
                )
                for channel in ("FTS", "VECTOR")
            )
            disposition, reason = _combined_disposition(channel_states)
            channel_by_name = {item.channel: item for item in channel_states}
            fts = by_channel.get("FTS")
            desired = 0 if fts is None else int(fts["desired_generation"])
            applied = 0 if fts is None else int(fts["applied_generation"])
            rows = ()
            if (
                channel_by_name["FTS"].disposition
                is not MemoryQueryDisposition.PARTIAL_UNAVAILABLE
            ):
                rows = connection.execute(
                    """
                    SELECT f.*
                    FROM pulsara_v3.memory_search_index AS i
                    JOIN pulsara_v3.memory_facts AS f
                      ON f.workspace_id = i.workspace_id AND f.id = i.fact_id
                    WHERE i.workspace_id = %s AND f.lifecycle = 'ACTIVE'
                      AND i.search_document @@ plainto_tsquery('simple', %s)
                    ORDER BY ts_rank(i.search_document, plainto_tsquery('simple', %s)) DESC,
                             f.id
                    LIMIT %s
                    """,
                    (workspace_id, query, query, limit),
                ).fetchall()
            ordered_rows = list(rows)
            seen = {str(row["id"]) for row in rows}
            if (
                query_embedding is not None
                and channel_by_name["VECTOR"].disposition
                is not MemoryQueryDisposition.PARTIAL_UNAVAILABLE
            ):
                literal = _vector_literal(query_embedding)
                vector_rows = connection.execute(
                    """
                    SELECT f.*
                    FROM pulsara_v3.memory_vector_index AS i
                    JOIN pulsara_v3.memory_facts AS f
                      ON f.workspace_id = i.workspace_id AND f.id = i.fact_id
                    WHERE i.workspace_id = %s AND f.lifecycle = 'ACTIVE'
                    ORDER BY i.embedding <=> %s::public.vector, f.id
                    LIMIT %s
                    """,
                    (workspace_id, literal, limit),
                ).fetchall()
                ordered_rows.extend(
                    row for row in vector_rows if str(row["id"]) not in seen
                )
            facts = tuple(
                MemoryQueryRow(
                    fact_id=str(row["id"]),
                    fact_kind=str(row["fact_kind"]),
                    lifecycle=str(row["lifecycle"]),
                    fact_payload=dict(row["fact_payload"]),
                    semantic_digest=str(row["semantic_digest"]),
                )
                for row in ordered_rows[:limit]
            )
            paths = self._paths(
                connection,
                workspace_id=workspace_id,
                source_ids=tuple(item.fact_id for item in facts),
                max_hops=max_hops,
                limit=limit,
            )
        return MemoryQueryResult(
            disposition,
            reason,
            desired,
            applied,
            facts,
            paths,
            channel_states,
        )

    def _channel_state(
        self,
        connection,
        *,
        workspace_id: str,
        channel: str,
        state,
    ) -> MemoryIndexChannelState:
        desired = 0 if state is None else int(state["desired_generation"])
        applied = 0 if state is None else int(state["applied_generation"])
        contract_current = state is not None and (
            str(state["desired_handler_contract_id"]) == self._contract_id
            and int(state["desired_handler_contract_version"])
            == self._contract_version
            and str(state["applied_handler_contract_id"]) == self._contract_id
            and int(state["applied_handler_contract_version"])
            == self._contract_version
        )
        if state is None or (desired == applied and contract_current):
            return MemoryIndexChannelState(
                channel, desired, applied, MemoryQueryDisposition.COMPLETE, None
            )
        key = _index_intent_key(
            workspace_id,
            channel,
            desired,
            str(state["desired_handler_contract_id"]),
            int(state["desired_handler_contract_version"]),
        )
        job = connection.execute(
            """
            SELECT status, terminal_reason FROM pulsara_v3.durable_jobs
            WHERE handler_type = 'MEMORY_INDEX_REFRESH'
              AND automatic_intent_key = %s
            """,
            (key,),
        ).fetchone()
        if job is not None and job["status"] in {
            "FAILED",
            "CANCELLED",
            "OUTCOME_UNKNOWN",
        }:
            return MemoryIndexChannelState(
                channel,
                desired,
                applied,
                MemoryQueryDisposition.PARTIAL_UNAVAILABLE,
                "INDEX_REFRESH_EXHAUSTED",
            )
        return MemoryIndexChannelState(
            channel,
            desired,
            applied,
            MemoryQueryDisposition.PARTIAL_STALE,
            "INDEX_REFRESH_PENDING",
        )

    @staticmethod
    def _paths(
        connection,
        *,
        workspace_id: str,
        source_ids: tuple[str, ...],
        max_hops: int,
        limit: int,
    ) -> tuple[MemoryRelationPath, ...]:
        if max_hops == 0 or not source_ids:
            return ()
        rows = connection.execute(
            """
            WITH RECURSIVE paths(source_fact_id, target_fact_id, kinds, depth, trail) AS (
                SELECT r.source_fact_id, r.target_fact_id,
                       ARRAY[r.relation_kind]::text[], 1,
                       ARRAY[r.source_fact_id, r.target_fact_id]::text[]
                FROM pulsara_v3.memory_relations AS r
                WHERE r.workspace_id = %s AND r.source_fact_id = ANY(%s)
                UNION ALL
                SELECT p.source_fact_id, r.target_fact_id,
                       p.kinds || r.relation_kind, p.depth + 1,
                       p.trail || r.target_fact_id
                FROM paths AS p
                JOIN pulsara_v3.memory_relations AS r
                  ON r.workspace_id = %s AND r.source_fact_id = p.target_fact_id
                WHERE p.depth < %s AND NOT r.target_fact_id = ANY(p.trail)
            )
            SELECT source_fact_id, target_fact_id, kinds, depth
            FROM paths
            ORDER BY depth, source_fact_id, target_fact_id
            LIMIT %s
            """,
            (workspace_id, list(source_ids), workspace_id, max_hops, limit * 2),
        ).fetchall()
        return tuple(
            MemoryRelationPath(
                str(row["source_fact_id"]),
                str(row["target_fact_id"]),
                tuple(row["kinds"]),
                int(row["depth"]),
            )
            for row in rows
        )


def _index_intent_key(
    workspace_id: str,
    channel: str,
    target_generation: int,
    contract_id: str,
    contract_version: int,
) -> str:
    return (
        f"memory-index:{workspace_id}:{channel}:{target_generation}:"
        f"{contract_id}:{contract_version}"
    )


class PostgresHybridMemoryQuery:
    """Async embed + bounded PostgreSQL FTS/pgvector/two-hop query."""

    def __init__(
        self,
        query: PostgresMemoryQuery,
        embedding: EmbeddingProvider,
    ) -> None:
        self._query = query
        self._embedding = embedding

    async def search(
        self,
        *,
        workspace_id: str,
        query: str,
        limit: int = 10,
        max_hops: int = 0,
        timeout_seconds: float = 20.0,
    ) -> MemoryQueryResult:
        import asyncio

        if timeout_seconds <= 0:
            raise ValueError("memory query timeout must be positive")
        async with asyncio.timeout(timeout_seconds):
            embedding = await self._embedding.embed(query)
            return await asyncio.to_thread(
                self._query.search,
                workspace_id=workspace_id,
                query=query,
                limit=limit,
                max_hops=max_hops,
                query_embedding=embedding,
                deadline_monotonic=monotonic() + timeout_seconds,
            )


def _combined_disposition(
    channels: tuple[MemoryIndexChannelState, ...],
) -> tuple[MemoryQueryDisposition, str | None]:
    if any(
        item.disposition is MemoryQueryDisposition.PARTIAL_UNAVAILABLE
        for item in channels
    ):
        return MemoryQueryDisposition.PARTIAL_UNAVAILABLE, "INDEX_CHANNEL_UNAVAILABLE"
    if any(
        item.disposition is MemoryQueryDisposition.PARTIAL_STALE for item in channels
    ):
        return MemoryQueryDisposition.PARTIAL_STALE, "INDEX_CHANNEL_STALE"
    return MemoryQueryDisposition.COMPLETE, None


def _vector_literal(values: Sequence[float]) -> str:
    vector = tuple(float(value) for value in values)
    if not vector or any(not math.isfinite(value) for value in vector):
        raise ValueError("query embedding is invalid")
    return "[" + ",".join(format(value, ".17g") for value in vector) + "]"


__all__ = [
    "INDEX_HANDLER_CONTRACT_ID",
    "INDEX_HANDLER_CONTRACT_VERSION",
    "MAXIMUM_MEMORY_QUERY_RESULTS",
    "MemoryIndexCoordinator",
    "MemoryIndexChannelState",
    "MemoryQueryResult",
    "MemoryQueryRow",
    "MemoryRelationPath",
    "PostgresMemoryQuery",
    "PostgresHybridMemoryQuery",
]
