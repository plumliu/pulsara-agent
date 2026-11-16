"""Derived embedding maintenance over the current canonical memory inputs."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Sequence

from psycopg.rows import dict_row

from pulsara_agent.memory.scope import FrozenMemoryReadContextBinding
from pulsara_agent.retrieval.embedding.validation import freeze_v1_embedding_vector
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .memory_management import _MemoryManagementOperations


def _embedding_input_digest(body: str) -> str:
    # This is the cross-asynchronous-call content check for one derived vector,
    # not a memory identity or deduplication key. search_text already projects
    # the complete title and statement in their specified field order.
    return "sha256:" + sha256(body.encode("utf-8")).hexdigest()


class _MemoryOperations(_MemoryManagementOperations):
    def list_unembedded_memory_facts(
        self,
        *,
        read_binding: FrozenMemoryReadContextBinding,
        limit: int,
        deadline_monotonic: float,
        embedding_contract,
    ) -> tuple[tuple[str, str, str], ...]:
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = connection.execute(
                """
                SELECT f.id, f.search_text AS body
                FROM pulsara_v3.memory_facts AS f
                LEFT JOIN pulsara_v3.memory_embeddings AS e
                  ON e.memory_domain_id=f.memory_domain_id AND e.fact_id=f.id
                 AND e.embedding_contract_id=%s
                WHERE f.memory_domain_id=%s AND f.context_id=ANY(%s::text[])
                  AND (e.fact_id IS NULL OR e.fact_semantic_digest <>
                       'sha256:' || encode(sha256(convert_to(f.search_text,'UTF8')), 'hex'))
                ORDER BY f.accepted_at, f.id LIMIT %s
                """,
                (
                    embedding_contract.contract_id,
                    read_binding.memory_domain_id,
                    list(read_binding.readable_context_ids),
                    max(1, min(limit, 100)),
                ),
            ).fetchall()
        return tuple(
            (str(row["id"]), _embedding_input_digest(row["body"]), row["body"])
            for row in rows
        )

    def upsert_memory_embedding(
        self,
        *,
        read_binding: FrozenMemoryReadContextBinding,
        fact_id: str,
        fact_semantic_digest: str,
        vector: Sequence[float],
        embedded_at: datetime,
        deadline_monotonic: float,
        embedding_contract,
    ) -> bool:
        values = freeze_v1_embedding_vector(vector)
        literal = "[" + ",".join(format(value, ".17g") for value in values) + "]"
        with self._provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            source = connection.execute(
                """
                SELECT f.id, f.search_text AS body
                FROM pulsara_v3.memory_facts AS f
                WHERE f.memory_domain_id=%s AND f.id=%s
                  AND f.context_id=ANY(%s::text[])
                FOR UPDATE
                """,
                (
                    read_binding.memory_domain_id,
                    fact_id,
                    list(read_binding.readable_context_ids),
                ),
            ).fetchone()
            if (
                source is None
                or _embedding_input_digest(source["body"]) != fact_semantic_digest
            ):
                return False
            # The source row stays locked through this write. A late vector
            # cannot overwrite a changed input, resurrect a deleted memory, or
            # cross the current read scope while its async calculation finishes.
            connection.execute(
                """
                INSERT INTO pulsara_v3.memory_embeddings (
                    memory_domain_id, fact_id, fact_semantic_digest,
                    embedding_contract_id, embedding, embedded_at
                ) VALUES (%s,%s,%s,%s,%s::public.vector,%s)
                ON CONFLICT (memory_domain_id, fact_id) DO UPDATE
                SET fact_semantic_digest=EXCLUDED.fact_semantic_digest,
                    embedding_contract_id=EXCLUDED.embedding_contract_id,
                    embedding=EXCLUDED.embedding, embedded_at=EXCLUDED.embedded_at
                """,
                (
                    read_binding.memory_domain_id,
                    fact_id,
                    fact_semantic_digest,
                    embedding_contract.contract_id,
                    literal,
                    embedded_at,
                ),
            )
            return True

    def read_memory_result_timestamps(
        self, *, session_id, fact_ids, deadline_monotonic
    ):
        """Find concurrency bases in complete accepted results of this session.

        The server cursor streams the existing history; its batches do not cap
        the number of results or acquire any durable read/qualification state.
        """
        from pulsara_agent.conversation_kernel.memory.contracts import memory_timestamp
        from pulsara_agent.conversation_kernel.content_parts import read_content_parts

        wanted = set(fact_ids)
        found = {}
        if not wanted:
            return found

        def accept(value):
            if not isinstance(value, dict) and not hasattr(value, "get"):
                return
            target = value.get("target")
            if (
                not hasattr(target, "get")
                or target.get("kind") != "MEMORY"
                or target.get("memory_id") not in wanted
            ):
                return
            stamp = value.get("updated_at")
            if stamp is None:
                meta = value.get("meta")
                stamp = meta.get("updated_at") if hasattr(meta, "get") else None
            if stamp is not None:
                instant = memory_timestamp(stamp)
                identity = target["memory_id"]
                if identity not in found or found[identity] < instant:
                    found[identity] = instant

        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as c:
            with c.cursor(name="memory_saved_results") as cursor:
                cursor.execute(
                    """SELECT r.public_fields,r.result_entry_id FROM pulsara_v3.tool_results r
                    JOIN pulsara_v3.content_parts call ON call.session_id=r.session_id
                      AND call.entry_id=r.tool_call_entry_id AND call.tool_call_id=r.tool_call_id
                    WHERE r.session_id=%s AND r.result_record_kind='EXECUTED'
                      AND r.output_unavailability_reason IS NULL
                      AND call.type='tool_call' AND call.tool_name IN ('remember','query_memory','manage_memory')
                    ORDER BY r.observed_at,r.id""",
                    (session_id,),
                )
                for row in cursor:
                    accept(row["public_fields"])
                    for part in read_content_parts(
                        c, session_id=session_id, entry_id=row["result_entry_id"]
                    ):
                        if part.type == "object":
                            accept(part.value)
        return found
