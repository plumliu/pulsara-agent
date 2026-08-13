"""Stage 2 memory tools over the PostgreSQL-only canonical memory plane."""

from __future__ import annotations

import json
from time import monotonic
from typing import Mapping
from uuid import uuid4

from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.memory import (
    MAXIMUM_MEMORY_QUERY_RESULTS,
    PostgresMemoryQuery,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.runner import (
    KernelMemoryProposal,
    KernelToolResult,
)
from pulsara_agent.retrieval.config import EmbeddingBackendConfig
from pulsara_agent.retrieval.embedding.factory import build_embedding_provider
from pulsara_agent.retrieval.embedding.protocol import EmbeddingProvider
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane


MEMORY_READ_TOOL_NAMES = frozenset({"memory_search", "memory_get", "memory_explain"})
MEMORY_WRITE_TOOL_NAMES = frozenset(
    {
        "remember_claim",
        "remember_preference",
        "remember_observation",
        "remember_action_boundary",
        "remember_decision",
    }
)
MEMORY_TOOL_NAMES = MEMORY_READ_TOOL_NAMES | MEMORY_WRITE_TOOL_NAMES
MAXIMUM_MEMORY_TOOL_OUTPUT_BYTES = 256 * 1024
MEMORY_POINT_READ_TIMEOUT_SECONDS = 10.0
MEMORY_SEARCH_TIMEOUT_SECONDS = 20.0


class KernelMemoryToolPort:
    """Host-scoped adapter; proposals become durable only with ToolResult."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        workspace_id: str,
        embedding_config: EmbeddingBackendConfig,
        embedding_provider: EmbeddingProvider | None = None,
        io_owner: KernelSessionIO,
    ) -> None:
        self._repository = repository
        self._workspace_id = workspace_id
        self._query = PostgresMemoryQuery(repository.connection_provider)
        self._embedding_config = embedding_config
        self._io = io_owner
        self._embedding = embedding_provider
        self._owns_embedding = embedding_provider is None
        self._closed = False

    @property
    def tool_names(self) -> frozenset[str]:
        return MEMORY_TOOL_NAMES

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        assistant_entry_id: str,
    ) -> KernelToolResult:
        if self._closed:
            return _json_result("TOOL_UNAVAILABLE", {"error": "memory owner is closed"})
        if tool_name == "memory_search":
            return await self._search(arguments)
        if tool_name in {"memory_get", "memory_explain"}:
            return await self._io.run(
                self._get,
                arguments,
                explain=tool_name == "memory_explain",
                deadline_monotonic=monotonic() + MEMORY_POINT_READ_TIMEOUT_SECONDS,
            )
        if tool_name in MEMORY_WRITE_TOOL_NAMES:
            return self._prepare_proposal(
                tool_name,
                arguments,
                assistant_entry_id=assistant_entry_id,
            )
        raise KeyError(tool_name)

    async def _search(self, arguments: Mapping[str, object]) -> KernelToolResult:
        query = str(arguments.get("query") or "").strip()
        limit = max(
            1,
            min(int(arguments.get("limit", 5)), MAXIMUM_MEMORY_QUERY_RESULTS),
        )
        max_hops = int(arguments.get("max_hops", 0))
        embedding = await self._embedding_provider()
        deadline = monotonic() + MEMORY_SEARCH_TIMEOUT_SECONDS
        if embedding is None:
            result = await self._io.run(
                self._query.search,
                workspace_id=self._workspace_id,
                query=query,
                limit=limit,
                max_hops=max_hops,
                deadline_monotonic=deadline,
            )
        else:
            query_embedding = await embedding.embed(query)
            result = await self._io.run(
                self._query.search,
                workspace_id=self._workspace_id,
                query=query,
                limit=limit,
                max_hops=max_hops,
                query_embedding=query_embedding,
                deadline_monotonic=deadline,
            )
        requested_scope = arguments.get("scope")
        requested_kind = arguments.get("kind")
        facts = [
            {
                "memory_id": item.fact_id,
                "kind": item.fact_kind,
                "lifecycle": item.lifecycle,
                "payload": dict(item.fact_payload),
                "semantic_digest": item.semantic_digest,
            }
            for item in result.facts
            if (
                requested_scope is None
                or item.fact_payload.get("scope") == requested_scope
            )
            and (requested_kind is None or item.fact_kind == requested_kind)
        ]
        return _json_result(
            "SUCCESS",
            {
                "disposition": result.disposition.value,
                "reason": result.reason,
                "channels": [
                    {
                        "channel": item.channel,
                        "desired_generation": item.desired_generation,
                        "applied_generation": item.applied_generation,
                        "disposition": item.disposition.value,
                        "reason": item.reason,
                    }
                    for item in result.channels
                ],
                "memories": facts,
                "paths": [
                    {
                        "source_memory_id": item.source_fact_id,
                        "target_memory_id": item.target_fact_id,
                        "relation_kinds": list(item.relation_kinds),
                        "hop_count": item.hop_count,
                    }
                    for item in result.paths
                ],
            },
        )

    def _get(
        self,
        arguments: Mapping[str, object],
        *,
        explain: bool,
        deadline_monotonic: float,
    ) -> KernelToolResult:
        memory_id = str(arguments.get("memory_id") or "")
        with self._repository.connection_provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            fact = connection.execute(
                """
                SELECT id, fact_kind, lifecycle, fact_payload, semantic_digest,
                       governance_decision_id, accepted_at, updated_at
                FROM pulsara_v3.memory_facts
                WHERE workspace_id = %s AND id = %s
                """,
                (self._workspace_id, memory_id),
            ).fetchone()
            if fact is None:
                return _json_result("APPLICATION_ERROR", {"error": "memory not found"})
            relations = connection.execute(
                """
                SELECT id, source_fact_id, target_fact_id, relation_kind
                FROM pulsara_v3.memory_relations
                WHERE workspace_id = %s
                  AND (source_fact_id = %s OR target_fact_id = %s)
                ORDER BY relation_kind, source_fact_id, target_fact_id
                LIMIT 100
                """,
                (self._workspace_id, memory_id, memory_id),
            ).fetchall()
            payload: dict[str, object] = {
                "memory_id": str(fact["id"]),
                "kind": str(fact["fact_kind"]),
                "lifecycle": str(fact["lifecycle"]),
                "payload": dict(fact["fact_payload"]),
                "semantic_digest": str(fact["semantic_digest"]),
                "relations": [
                    {
                        "relation_id": str(item["id"]),
                        "source_memory_id": str(item["source_fact_id"]),
                        "target_memory_id": str(item["target_fact_id"]),
                        "kind": str(item["relation_kind"]),
                    }
                    for item in relations
                ],
            }
            if explain:
                payload["governance_decision_id"] = str(fact["governance_decision_id"])
                payload["accepted_at"] = fact["accepted_at"].isoformat()
                payload["updated_at"] = fact["updated_at"].isoformat()
        return _json_result("SUCCESS", payload)

    def _prepare_proposal(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        assistant_entry_id: str,
    ) -> KernelToolResult:
        candidate_id = f"memory-candidate:{uuid4().hex}"
        job_id = f"job:{uuid4().hex}"
        proposal_kind = {
            "remember_preference": "PREFERENCE",
            "remember_action_boundary": "LIFECYCLE",
        }.get(tool_name, "FACT")
        proposal_payload = {
            "memory_kind": tool_name.removeprefix("remember_").upper(),
            "source_entry_id": assistant_entry_id,
            **dict(arguments),
        }
        return _json_result(
            "SUCCESS",
            {
                "status": "proposed",
                "candidate_id": candidate_id,
                "governance_job_id": job_id,
            },
            memory_proposal=KernelMemoryProposal(
                candidate_id=candidate_id,
                proposal_kind=proposal_kind,
                proposal_payload=proposal_payload,
                governance_job_id=job_id,
            ),
        )

    async def _embedding_provider(self) -> EmbeddingProvider | None:
        if self._embedding is None and self._embedding_config.api_key:
            self._embedding = build_embedding_provider(self._embedding_config)
        return self._embedding

    async def aclose(self) -> None:
        self._closed = True
        if self._embedding is not None and self._owns_embedding:
            await self._embedding.aclose()


def _json_result(
    state: str,
    payload: Mapping[str, object],
    *,
    memory_proposal: KernelMemoryProposal | None = None,
) -> KernelToolResult:
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAXIMUM_MEMORY_TOOL_OUTPUT_BYTES:
        return KernelToolResult(
            state="SYSTEM_ERROR",
            content=b'{"error":"memory tool output exceeded its bound"}',
        )
    return KernelToolResult(
        state=state, content=encoded, memory_proposal=memory_proposal
    )


__all__ = [
    "KernelMemoryToolPort",
    "MEMORY_READ_TOOL_NAMES",
    "MEMORY_TOOL_NAMES",
    "MEMORY_WRITE_TOOL_NAMES",
]
