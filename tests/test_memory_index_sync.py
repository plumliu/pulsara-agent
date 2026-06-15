from __future__ import annotations

import asyncio
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.entities.memory import Preference
from pulsara_agent.event import EventContext, TextBlockDeltaEvent
from pulsara_agent.event.candidates import PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.graph import InMemoryGraphStore, PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory import (
    CorrectAndSubmitDecision,
    InMemoryArchiveStore,
    MemoryGovernanceExecutor,
    MemoryWriteService,
    MemoryWriteUnitOfWork,
    PooledMemoryCandidate,
    PostgresCandidatePool,
    PostgresMemoryQuery,
)
from pulsara_agent.memory.candidate_pool import CandidateOrigin, WriteSucceededOutcome
from pulsara_agent.memory.index_sync import MemorySearchIndexSync
from pulsara_agent.memory.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.recall import LexicalMemoryRecallService, RecallQuery, RecallStatus
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.ontology import memory
from pulsara_agent.settings import StorageConfig


def test_memory_search_index_rebuild_populates_fts_candidates() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    sync = MemorySearchIndexSync(dsn=dsn)
    try:
        _put_preference(
            store,
            graph_id=graph_id,
            memory_id="preference:index-rebuild",
            statement="The user prefers concise summaries.",
            status=memory.NodeStatus.ACTIVE,
        )
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM memory_search_index WHERE graph_id = %s", (graph_id,))

        assert query.fts_candidates(
            query_text="concise summaries",
            scopes=["ctx:user"],
            types=["Preference"],
            limit=5,
            graph_id=graph_id,
        ) == []

        assert sync.rebuild(graph_id=graph_id) == 1
        assert query.fts_candidates(
            query_text="concise summaries",
            scopes=["ctx:user"],
            types=["Preference"],
            limit=5,
            graph_id=graph_id,
        )[0][0] == "preference:index-rebuild"
    finally:
        store.delete_graph(graph_id)


def test_recall_filters_stale_index_hit_through_canonical_fetch() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    try:
        _put_preference(
            store,
            graph_id=graph_id,
            memory_id="preference:stale-index",
            statement="The user prefers concise summaries.",
            status=memory.NodeStatus.REJECTED,
        )
        _insert_stale_active_index_row(
            dsn,
            graph_id=graph_id,
            memory_id="preference:stale-index",
            statement="The user prefers concise summaries.",
        )

        result = asyncio.run(
            LexicalMemoryRecallService(query).recall(
                RecallQuery(text="concise summaries", scopes=("ctx:user",)),
                graph_id=graph_id,
            )
        )

        assert result.status is RecallStatus.EMPTY
        assert "preference:stale-index" in result.filtered_ids
    finally:
        store.delete_graph(graph_id)


def test_index_sync_consumes_governance_outbox_and_marks_applied(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    source_ctx = EventContext(
        run_id=f"run:source:{uuid4().hex}",
        turn_id=f"turn:source:{uuid4().hex}",
        reply_id=f"reply:source:{uuid4().hex}",
    )
    batch_id = f"governance:test:index-sync:{uuid4().hex}"
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    pool = PostgresCandidatePool(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    try:
        candidate = pool.append_candidate(
            PooledMemoryCandidate(
                payload=ValidCandidatePayload(
                    candidate=_preference_candidate("candidate:index-sync")
                ),
                origin=CandidateOrigin.MAIN_AGENT_TOOL,
                source_session_id=runtime_session_id,
                source_run_id=source_ctx.run_id,
                source_turn_id=source_ctx.turn_id,
                source_reply_id=source_ctx.reply_id,
            )
        )
        executor = MemoryGovernanceExecutor(
            candidate_pool=pool,
            memory_write_service=_service_on(InMemoryGraphStore()),
            event_log=log,
            graph=InMemoryGraphStore(),
            runtime_session_id=runtime_session_id,
            memory_write_uow_factory=lambda: MemoryWriteUnitOfWork(
                dsn=dsn,
                runtime_session_id=runtime_session_id,
                graph_id=graph_id,
                workspace_root=tmp_path,
            ),
        )

        result = executor.apply_decision(
            CorrectAndSubmitDecision(
                target_entry_id=candidate.entry_id,
                candidate=_preference_candidate("candidate:index-sync-corrected"),
                reason="Submit active memory for index sync.",
            ),
            governance_batch_id=batch_id,
        )
        assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
        memory_id = result.decision_record.write_outcome.memory_id
        assert query.fts_candidates(
            query_text="concise summaries",
            scopes=["ctx:user"],
            types=["Preference"],
            limit=5,
            graph_id=graph_id,
        ) == []

        applied = MemorySearchIndexSync(dsn=dsn).consume_outbox(
            graph_id=graph_id,
            governance_batch_id=batch_id,
        )

        assert applied == 1
        assert query.fts_candidates(
            query_text="concise summaries",
            scopes=["ctx:user"],
            types=["Preference"],
            limit=5,
            graph_id=graph_id,
        )[0][0] == memory_id
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status FROM memory_write_outbox WHERE governance_batch_id = %s",
                    (batch_id,),
                )
                assert cursor.fetchall() == [("applied",)]
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM memory_write_outbox WHERE governance_batch_id = %s", (batch_id,))
                cursor.execute(
                    "DELETE FROM memory_governance_decisions WHERE governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute("DELETE FROM graph_documents WHERE graph_id = %s", (graph_id,))
                cursor.execute("DELETE FROM memory_nodes WHERE graph_id = %s", (graph_id,))
                cursor.execute("DELETE FROM memory_relations WHERE graph_id = %s", (graph_id,))
                cursor.execute("DELETE FROM sessions WHERE id = %s", (runtime_session_id,))


def _put_preference(
    store: PostgresGraphStore,
    *,
    graph_id: str,
    memory_id: str,
    statement: str,
    status: memory.NodeStatus,
) -> None:
    now = utc_now()
    store.put_jsonld(
        Preference(
            id=memory_id,
            statement=statement,
            scope="ctx:user",
            status=status,
            confidence_level=memory.ConfidenceLevel.HIGH,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=now,
            updated_at=now,
            gate_reason="test",
        ).to_jsonld(),
        graph_id=graph_id,
    )


def _insert_stale_active_index_row(
    dsn: str,
    *,
    graph_id: str,
    memory_id: str,
    statement: str,
) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_search_index (
                    graph_id,
                    memory_id,
                    memory_type,
                    scope,
                    status,
                    fts,
                    aliases,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    'Preference',
                    'ctx:user',
                    'active',
                    to_tsvector('simple', %s),
                    ARRAY[]::text[],
                    now()
                )
                """,
                (graph_id, memory_id, statement),
            )


def _preference_candidate(candidate_id: str) -> PreferenceCandidate:
    return PreferenceCandidate(
        candidate_id=candidate_id,
        statement="The user prefers concise summaries.",
        scope="ctx:user",
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )


def _service_on(graph: InMemoryGraphStore) -> MemoryWriteService:
    ledger = ExecutionEvidenceLedger(
        graph=graph,
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
    )
    return MemoryWriteService(ledger=ledger)


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")
