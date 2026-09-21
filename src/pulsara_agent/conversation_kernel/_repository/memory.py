"""Direct canonical advisory-memory writes and embedding maintenance reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from time import monotonic
from typing import Sequence

from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.memory.contracts import (
    MemoryRelationKind,
    MemorySupersedeMode,
    memory_basis_context_allowed,
    memory_relation_id,
    normalize_memory_text,
)
from pulsara_agent.conversation_kernel.memory.recall import (
    MEMORY_EMBEDDING_CONTRACT_ID,
    MEMORY_EMBEDDING_CONTRACT_VERSION,
)
from pulsara_agent.conversation_kernel.memory.writes import (
    PreparedMemoryMutation,
    PreparedMemoryRelationWrite,
    PreparedRememberWrite,
)
from pulsara_agent.memory.scope import (
    CTX_GLOBAL,
    FrozenMemoryReadContextBinding,
    workspace_context_id,
)
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.retrieval.embedding.validation import freeze_v1_embedding_vector
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .contracts import ConversationKernelConflict, PreparedToolResultAcceptance
from .memory_management import _MemoryManagementOperations


@dataclass(frozen=True, slots=True)
class DirectMemoryOutcome:
    result_state: str
    canonical_body: str
    visible_memory_fact_ids: tuple[str, ...]


class _MemoryInputRejected(ValueError):
    """Known application rejection before any canonical memory side effect."""


def _body(value: dict[str, object]) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(body.encode("utf-8")) > 65_536:
        raise _MemoryInputRejected("memory ToolResult exceeds inline byte bound")
    return body


def _fact_id(session_id: str, result_id: str) -> str:
    return "memory:" + sha256((session_id + "\0" + result_id).encode()).hexdigest()


class _MemoryOperations(_MemoryManagementOperations):
    def _settle_direct_memory_mutation(
        self,
        connection,
        *,
        candidate: PreparedToolResultAcceptance,
        mutation: PreparedMemoryMutation,
        deadline_monotonic: float,
    ) -> DirectMemoryOutcome:
        if candidate.result_state != "SUCCESS" or candidate.attempt_id is None:
            raise ConversationKernelConflict("memory mutation needs a successful tool attempt")
        owner = connection.execute(
            "SELECT memory_domain_id, workspace_id, workspace_kind, workspace_root "
            "FROM pulsara_v3.sessions WHERE id=%s",
            (candidate.session_id,),
        ).fetchone()
        if owner is None or (
            str(owner["memory_domain_id"]) != mutation.memory_domain_id
            or str(owner["workspace_id"]) != candidate.workspace_id
        ):
            raise ConversationKernelConflict("memory mutation owner domain drifted")
        call = connection.execute(
            """
            SELECT b.tool_name, b.tool_arguments, t.conversation_scope_kind
            FROM pulsara_v3.assistant_message_blocks AS b
            JOIN pulsara_v3.turns AS t
              ON t.session_id=b.session_id AND t.id=%s
            WHERE b.session_id=%s AND b.assistant_entry_id=%s
              AND b.tool_call_id=%s AND b.block_kind='TOOL_CALL'
            """,
            (
                candidate.turn_id, candidate.session_id,
                candidate.assistant_entry_id, candidate.tool_call_id,
            ),
        ).fetchone()
        if call is None or str(call["conversation_scope_kind"]) != "ROOT":
            raise ConversationKernelConflict("memory mutation is not owned by a root tool call")
        if candidate.actor_id != call["tool_name"]:
            raise ConversationKernelConflict("memory mutation actor differs from its tool call")
        arguments = call["tool_arguments"]
        if not isinstance(arguments, dict):
            raise ConversationKernelConflict("memory tool arguments are not canonical")
        if isinstance(mutation, PreparedRememberWrite):
            raw_statement = arguments.get("statement")
            expected_target = (
                "GLOBAL" if mutation.context_id == CTX_GLOBAL else "CURRENT_PROJECT"
            )
            if (
                call["tool_name"] != "remember"
                or not isinstance(raw_statement, str)
                or normalize_memory_text(raw_statement) != mutation.statement
                or arguments.get("kind") != mutation.kind.value
                or arguments.get("context_target") != expected_target
                or "kind_hint" in arguments
                or arguments.get("based_on_memory_ids", [])
                != [ref.target_fact_id for ref in mutation.basis_refs]
                or set(arguments)
                - {"statement", "context_target", "kind", "based_on_memory_ids"}
            ):
                raise ConversationKernelConflict("remember write does not exact-join its tool call")
        elif (
            call["tool_name"] != "mark_memory_relation"
            or arguments.get("source_memory_id") != mutation.source_memory_id
            or arguments.get("target_memory_id") != mutation.target_memory_id
            or arguments.get("relation_kind") != mutation.relation_kind.value
        ):
            raise ConversationKernelConflict("relation write does not exact-join its tool call")
        try:
            if isinstance(mutation, PreparedRememberWrite):
                return self._settle_remember(
                    connection,
                    candidate=candidate,
                    write=mutation,
                    deadline_monotonic=deadline_monotonic,
                    owner=owner,
                )
            return self._settle_memory_relation(
                connection, candidate=candidate, write=mutation
            )
        except _MemoryInputRejected as exc:
            return DirectMemoryOutcome(
                "APPLICATION_ERROR",
                _body({"error": str(exc), "advisory": True}),
                (),
            )

    @staticmethod
    def _lock_remember_basis(connection, write: PreparedRememberWrite) -> tuple[dict, ...]:
        ids = tuple(item.target_fact_id for item in write.basis_refs)
        if not ids:
            return ()
        rows = connection.execute(
            """
            SELECT * FROM pulsara_v3.memory_facts
            WHERE memory_domain_id=%s AND id=ANY(%s::text[])
            ORDER BY id FOR UPDATE
            """,
            (write.memory_domain_id, list(ids)),
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        for ref in write.basis_refs:
            row = by_id.get(ref.target_fact_id)
            if (
                row is None
                or str(row["context_id"]) != ref.target_context_id
                or str(row["lifecycle"]) != "ACTIVE"
                or not memory_basis_context_allowed(
                    write.context_id, ref.target_context_id
                )
            ):
                raise _MemoryInputRejected("based_on memory is no longer active or visible")
        return tuple(by_id[ref.target_fact_id] for ref in write.basis_refs)

    def _settle_remember(
        self,
        connection,
        *,
        candidate: PreparedToolResultAcceptance,
        write: PreparedRememberWrite,
        deadline_monotonic: float,
        owner,
    ) -> DirectMemoryOutcome:
        if write.context_id != CTX_GLOBAL and (
            str(owner["workspace_kind"]) != "project"
            or write.context_id != workspace_context_id(str(owner["workspace_root"]))
        ):
            raise _MemoryInputRejected("remember crosses current project context")
        basis = self._lock_remember_basis(connection, write)
        new_id = _fact_id(candidate.session_id, candidate.result_id)
        accepted_at = candidate.observed_at
        related_preview = [
            {
                "memory_id": item.memory_id,
                "kind": item.kind.value,
                "context_id": item.context_id,
                "statement": item.statement,
                "recorded_at": item.recorded_at,
            }
            for item in write.related
        ][:3]
        # Reject an oversized final body before the first fact/relationship write.
        _body({
            "status": "ALREADY_PRESENT",
            "memory_id": new_id,
            "related_memories": related_preview,
            "retrieval_summary": thaw_json(write.retrieval_summary),
            "advisory": True,
            "relationship_labels_not_inferred": True,
        })
        while monotonic() < deadline_monotonic:
            inserted = connection.execute(
                """
                INSERT INTO pulsara_v3.memory_facts (
                    id, memory_domain_id, context_id,
                    source_session_id, source_tool_result_id,
                    lifecycle, fact_kind, statement, fact_semantic_digest,
                    accepted_at, updated_at, search_contract_id,
                    search_contract_version, search_terms
                ) VALUES (
                    %s,%s,%s,%s,%s,'ACTIVE',%s,%s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT (memory_domain_id, context_id, fact_semantic_digest)
                    WHERE lifecycle='ACTIVE' DO NOTHING
                RETURNING id
                """,
                (
                    new_id, write.memory_domain_id, write.context_id,
                    candidate.session_id, candidate.result_id,
                    write.kind.value, write.statement, write.semantic_digest,
                    accepted_at, accepted_at, *write.search_contract,
                    list(write.search_terms),
                ),
            ).fetchone()
            if inserted is not None:
                memory_id, status = new_id, "SAVED"
                break
            winner = connection.execute(
                """
                SELECT id, fact_kind, statement
                FROM pulsara_v3.memory_facts
                WHERE memory_domain_id=%s AND context_id=%s
                  AND fact_semantic_digest=%s AND lifecycle='ACTIVE'
                FOR UPDATE
                """,
                (write.memory_domain_id, write.context_id, write.semantic_digest),
            ).fetchone()
            if winner is not None:
                if (
                    str(winner["fact_kind"]) != write.kind.value
                    or str(winner["statement"]) != write.statement
                ):
                    raise ConversationKernelConflict("active semantic digest collision")
                memory_id, status = str(winner["id"]), "ALREADY_PRESENT"
                break
        else:
            raise TimeoutError("memory uniqueness arbitration exceeded writer deadline")
        if status == "SAVED":
            for ref, target in zip(write.basis_refs, basis, strict=True):
                relation_id = memory_relation_id(
                    memory_domain_id=write.memory_domain_id,
                    source_context_id=write.context_id,
                    source_fact_id=memory_id,
                    relation_kind=MemoryRelationKind.BASED_ON,
                    target_context_id=ref.target_context_id,
                    target_fact_id=ref.target_fact_id,
                    supersede_mode=None,
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.memory_relations (
                        id, memory_domain_id, owner_session_id, owner_tool_result_id,
                        source_context_id, source_fact_id, source_fact_kind,
                        relation_kind, target_context_id, target_fact_id,
                        target_fact_kind, supersede_mode, ordinal, accepted_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,'BASED_ON',%s,%s,%s,NULL,%s,%s)
                    """,
                    (
                        relation_id, write.memory_domain_id, candidate.session_id,
                        candidate.result_id, write.context_id, memory_id, write.kind.value,
                        ref.target_context_id, ref.target_fact_id,
                        str(target["fact_kind"]), ref.ordinal, accepted_at,
                    ),
                )
        related = [item for item in related_preview if item["memory_id"] != memory_id]
        body = _body({
            "status": status,
            "memory_id": memory_id,
            "related_memories": related,
            "retrieval_summary": thaw_json(write.retrieval_summary),
            "advisory": True,
            "relationship_labels_not_inferred": True,
        })
        ids = tuple(dict.fromkeys((memory_id, *(item["memory_id"] for item in related))))
        return DirectMemoryOutcome("SUCCESS", body, ids)

    def _settle_memory_relation(
        self,
        connection,
        *,
        candidate: PreparedToolResultAcceptance,
        write: PreparedMemoryRelationWrite,
    ) -> DirectMemoryOutcome:
        source_id, target_id, kind = (
            write.source_memory_id, write.target_memory_id, write.relation_kind
        )
        def require_existing_owner(existing):
            owner = connection.execute(
                """
                SELECT r.result_record_kind, r.result_state,
                       b.tool_name, b.tool_arguments
                FROM pulsara_v3.tool_results AS r
                JOIN pulsara_v3.assistant_message_blocks AS b
                  ON b.session_id=r.session_id
                 AND b.assistant_entry_id=r.tool_call_entry_id
                 AND b.tool_call_id=r.tool_call_id
                WHERE r.session_id=%s AND r.id=%s
                """,
                (existing["owner_session_id"], existing["owner_tool_result_id"]),
            ).fetchone()
            arguments = None if owner is None else owner["tool_arguments"]
            if (
                owner is None
                or owner["result_record_kind"] != "EXECUTED"
                or owner["result_state"] != "SUCCESS"
                or owner["tool_name"] != "mark_memory_relation"
                or not isinstance(arguments, dict)
                or arguments.get("source_memory_id") != existing["source_fact_id"]
                or arguments.get("target_memory_id") != existing["target_fact_id"]
                or arguments.get("relation_kind") != kind.value
            ):
                raise ConversationKernelConflict("existing memory relation owner drifted")

        def existing_relation():
            if kind is MemoryRelationKind.CONTRADICTS:
                return connection.execute(
                    """
                    SELECT * FROM pulsara_v3.memory_relations
                    WHERE memory_domain_id=%s AND relation_kind='CONTRADICTS'
                      AND least(source_fact_id,target_fact_id)=least(%s,%s)
                      AND greatest(source_fact_id,target_fact_id)=greatest(%s,%s)
                    FOR UPDATE
                    """,
                    (write.memory_domain_id, source_id, target_id, source_id, target_id),
                ).fetchone()
            return connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_relations
                WHERE memory_domain_id=%s AND relation_kind='SUPERSEDES'
                  AND source_fact_id=%s AND target_fact_id=%s FOR UPDATE
                """,
                (write.memory_domain_id, source_id, target_id),
            ).fetchone()

        existing = existing_relation()
        if existing is not None:
            require_existing_owner(existing)
            if kind is MemoryRelationKind.SUPERSEDES:
                target = connection.execute(
                    "SELECT lifecycle FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s AND id=%s",
                    (write.memory_domain_id, target_id),
                ).fetchone()
                if target is None or str(target["lifecycle"]) != "SUPERSEDED":
                    raise ConversationKernelConflict("existing supersede lifecycle drifted")
            return DirectMemoryOutcome(
                "SUCCESS",
                _body({
                    "status": "ALREADY_PRESENT",
                    "relation_id": str(existing["id"]),
                    "source_memory_id": source_id,
                    "target_memory_id": target_id,
                    "relation_kind": kind.value,
                    "advisory": True,
                }),
                (source_id, target_id),
            )
        rows = connection.execute(
            """
            SELECT * FROM pulsara_v3.memory_facts
            WHERE memory_domain_id=%s AND id=ANY(%s::text[])
            ORDER BY id FOR UPDATE
            """,
            (write.memory_domain_id, [source_id, target_id]),
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        source, target = by_id.get(source_id), by_id.get(target_id)
        # The first probe preceded endpoint locks. A concurrent writer may
        # have installed this relation while we waited for those locks.
        existing = existing_relation()
        if existing is not None:
            require_existing_owner(existing)
            if kind is MemoryRelationKind.SUPERSEDES and (
                target is None or str(target["lifecycle"]) != "SUPERSEDED"
            ):
                raise ConversationKernelConflict("existing supersede lifecycle drifted")
            return DirectMemoryOutcome(
                "SUCCESS",
                _body({
                    "status": "ALREADY_PRESENT",
                    "relation_id": str(existing["id"]),
                    "source_memory_id": source_id,
                    "target_memory_id": target_id,
                    "relation_kind": kind.value,
                    "advisory": True,
                }),
                (source_id, target_id),
            )
        if source is None or target is None:
            raise _MemoryInputRejected("relation memory is absent or outside this domain")
        if (
            str(source["lifecycle"]) != "ACTIVE"
            or str(target["lifecycle"]) != "ACTIVE"
            or str(source["context_id"]) != str(target["context_id"])
        ):
            raise _MemoryInputRejected("new relation requires active same-context memories")
        if kind is MemoryRelationKind.CONTRADICTS and (
            str(source["fact_kind"]) != str(target["fact_kind"])
        ):
            raise _MemoryInputRejected("contradiction requires the same memory kind")
        mode = (
            None if kind is MemoryRelationKind.CONTRADICTS else
            (MemorySupersedeMode.SAME_KIND_REPLACEMENT
             if str(source["fact_kind"]) == str(target["fact_kind"])
             else MemorySupersedeMode.TAXONOMY_CORRECTION)
        )
        relation_id = memory_relation_id(
            memory_domain_id=write.memory_domain_id,
            source_context_id=str(source["context_id"]),
            source_fact_id=source_id, relation_kind=kind,
            target_context_id=str(target["context_id"]),
            target_fact_id=target_id, supersede_mode=mode,
        )
        result_body = _body({
            "status": "SAVED", "relation_id": relation_id,
            "source_memory_id": source_id, "target_memory_id": target_id,
            "relation_kind": kind.value, "advisory": True,
        })
        connection.execute(
            """
            INSERT INTO pulsara_v3.memory_relations (
                id, memory_domain_id, owner_session_id, owner_tool_result_id,
                source_context_id, source_fact_id, source_fact_kind, relation_kind,
                target_context_id, target_fact_id, target_fact_kind,
                supersede_mode, ordinal, accepted_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s)
            """,
            (
                relation_id, write.memory_domain_id, candidate.session_id,
                candidate.result_id, str(source["context_id"]), source_id,
                str(source["fact_kind"]), kind.value, str(target["context_id"]),
                target_id, str(target["fact_kind"]),
                None if mode is None else mode.value, candidate.observed_at,
            ),
        )
        if kind is MemoryRelationKind.SUPERSEDES:
            connection.execute(
                """
                UPDATE pulsara_v3.memory_facts
                SET lifecycle='SUPERSEDED', updated_at=%s
                WHERE memory_domain_id=%s AND id=%s
                """,
                (candidate.observed_at, write.memory_domain_id, target_id),
            )
        return DirectMemoryOutcome(
            "SUCCESS", result_body,
            (source_id, target_id),
        )

    def list_unembedded_memory_facts(
        self, *, read_binding: FrozenMemoryReadContextBinding,
        limit: int, deadline_monotonic: float,
    ) -> tuple[tuple[str, str, str], ...]:
        with self._provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = connection.execute(
                """
                SELECT f.id, f.fact_semantic_digest, f.statement AS body
                FROM pulsara_v3.memory_facts AS f
                LEFT JOIN pulsara_v3.memory_embeddings AS e
                  ON e.memory_domain_id=f.memory_domain_id AND e.fact_id=f.id
                 AND e.fact_semantic_digest=f.fact_semantic_digest
                WHERE f.memory_domain_id=%s AND f.lifecycle='ACTIVE'
                  AND f.context_id=ANY(%s::text[]) AND e.fact_id IS NULL
                ORDER BY f.accepted_at, f.id LIMIT %s
                """,
                (
                    read_binding.memory_domain_id,
                    list(read_binding.readable_context_ids),
                    max(1, min(limit, 100)),
                ),
            ).fetchall()
        return tuple(
            (str(row["id"]), str(row["fact_semantic_digest"]), str(row["body"]))
            for row in rows
        )

    def upsert_memory_embedding(
        self, *, read_binding: FrozenMemoryReadContextBinding,
        fact_id: str, fact_semantic_digest: str, vector: Sequence[float],
        embedded_at: datetime, deadline_monotonic: float,
    ) -> bool:
        values = freeze_v1_embedding_vector(vector)
        literal = "[" + ",".join(format(value, ".17g") for value in values) + "]"
        with self._provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                INSERT INTO pulsara_v3.memory_embeddings (
                    memory_domain_id, fact_id, fact_semantic_digest,
                    embedding_contract_id, embedding_contract_version,
                    embedding, embedded_at
                )
                SELECT f.memory_domain_id, f.id, f.fact_semantic_digest,
                       %s, %s, %s::public.vector, %s
                FROM pulsara_v3.memory_facts AS f
                WHERE f.memory_domain_id=%s AND f.id=%s
                  AND f.context_id=ANY(%s::text[])
                  AND f.lifecycle='ACTIVE' AND f.fact_semantic_digest=%s
                ON CONFLICT (memory_domain_id, fact_id) DO UPDATE
                SET fact_semantic_digest=EXCLUDED.fact_semantic_digest,
                    embedding_contract_id=EXCLUDED.embedding_contract_id,
                    embedding_contract_version=EXCLUDED.embedding_contract_version,
                    embedding=EXCLUDED.embedding, embedded_at=EXCLUDED.embedded_at
                RETURNING fact_id
                """,
                (
                    MEMORY_EMBEDDING_CONTRACT_ID,
                    MEMORY_EMBEDDING_CONTRACT_VERSION,
                    literal, embedded_at, read_binding.memory_domain_id, fact_id,
                    list(read_binding.readable_context_ids), fact_semantic_digest,
                ),
            ).fetchone()
            return row is not None
