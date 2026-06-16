from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.entities.memory import Preference
from pulsara_agent.event.candidates import PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory import CandidatePoolProposal, InMemoryCandidatePool, PooledMemoryCandidate, PostgresMemoryQuery
from pulsara_agent.memory.candidates.pool import CandidateOrigin
from pulsara_agent.memory.hooks.durable import DurableMemoryHooks
from pulsara_agent.memory.canonical.index_sync import MemorySearchIndexSync
from pulsara_agent.memory.recall.service import LexicalMemoryRecallService, RecallQuery, RecallStatus, RecallTrigger
from pulsara_agent.memory.recall.trace import PostgresRecallTraceStore
from pulsara_agent.message import AssistantMsg, TextBlock, UserMsg
from pulsara_agent.ontology import memory
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.settings import StorageConfig
from pulsara_agent.tools.base import ToolCall
from pulsara_agent.tools.builtins.memory_query import MemoryGetTool, MemorySearchTool


def test_lexical_recall_returns_active_memory_and_filters_rejected() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    service = LexicalMemoryRecallService(PostgresMemoryQuery(dsn=dsn))
    try:
        _put_preference(
            store,
            graph_id=graph_id,
            memory_id="preference:active-concise",
            statement="The user prefers concise summaries.",
            status=memory.NodeStatus.ACTIVE,
        )
        _put_preference(
            store,
            graph_id=graph_id,
            memory_id="preference:rejected-concise",
            statement="The user rejects concise summaries.",
            status=memory.NodeStatus.REJECTED,
        )

        result = asyncio.run(
            service.recall(
                RecallQuery(text="Please remember my concise summaries preference.", scopes=("ctx:user",)),
                graph_id=graph_id,
            )
        )

        assert result.status is RecallStatus.OK
        assert [item.memory_id for item in result.items] == ["preference:active-concise"]
    finally:
        store.delete_graph(graph_id)


def test_durable_memory_project_builds_recalled_memory_projection() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    try:
        _put_preference(
            store,
            graph_id=graph_id,
            memory_id="preference:project-concise",
            statement="The user prefers concise summaries.",
            status=memory.NodeStatus.ACTIVE,
        )
        hooks = DurableMemoryHooks(
            candidate_pool=InMemoryCandidatePool(),
            sink=MemoryProposalSink(),
            recall=LexicalMemoryRecallService(PostgresMemoryQuery(dsn=dsn)),
            graph_id=graph_id,
        )
        state = LoopState(session_id="runtime:test")
        state.current_scope = "ctx:user"
        state.messages.append(UserMsg(name="user", content=[TextBlock(text="Can you keep this concise?")]))

        projection = asyncio.run(hooks.project(state, token_budget=200))

        assert projection is not None
        assert projection["do_not_write_back"] is True
        assert projection["included_memory_ids"] == ["preference:project-concise"]
        assert "<recalled-memory-projection" in projection["summary"]
        assert "memory_get preference:project-concise" in projection["summary"]
    finally:
        store.delete_graph(graph_id)


def test_candidate_pool_entries_are_not_recall_sources() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    pool = InMemoryCandidatePool()
    pool.append_candidate(
        PooledMemoryCandidate(
            payload=ValidCandidatePayload(
                candidate=PreferenceCandidate(
                    candidate_id="candidate:pool-only",
                    statement="The user prefers concise summaries.",
                    scope="ctx:user",
                    source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                    verification_status=memory.VerificationStatus.USER_CONFIRMED,
                )
            ),
            origin=CandidateOrigin.MAIN_AGENT_TOOL,
            source_session_id="runtime:test",
            source_run_id="run:test",
            source_turn_id="turn:test",
            source_reply_id="reply:test",
        )
    )
    service = LexicalMemoryRecallService(PostgresMemoryQuery(dsn=dsn))
    try:
        result = asyncio.run(
            service.recall(
                RecallQuery(text="concise summaries", scopes=("ctx:user",)),
                graph_id=graph_id,
            )
        )

        assert result.status is RecallStatus.EMPTY
        assert result.items == ()
    finally:
        store.delete_graph(graph_id)


def test_recall_backend_unavailable_enters_short_cooldown() -> None:
    backend = _FailingMemoryQuery()
    service = LexicalMemoryRecallService(backend, unavailable_cooldown_seconds=60)

    first = asyncio.run(service.recall(RecallQuery(text="concise summaries")))
    second = asyncio.run(service.recall(RecallQuery(text="concise summaries")))

    assert first.status is RecallStatus.UNAVAILABLE
    assert first.warnings[0].startswith("recall_backend_unavailable:RuntimeError")
    assert second.status is RecallStatus.UNAVAILABLE
    assert second.warnings == ("recall_backend_cooldown",)
    assert backend.call_count == 1


def test_projection_echo_valid_candidate_is_not_written_back_to_pool() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    pool = InMemoryCandidatePool()
    sink = MemoryProposalSink()
    try:
        _put_preference(
            store,
            graph_id=graph_id,
            memory_id="preference:echo-concise",
            statement="The user prefers concise summaries.",
            status=memory.NodeStatus.ACTIVE,
        )
        hooks = DurableMemoryHooks(
            candidate_pool=pool,
            sink=sink,
            recall=LexicalMemoryRecallService(PostgresMemoryQuery(dsn=dsn)),
            graph_id=graph_id,
        )
        state = LoopState(session_id="runtime:test")
        state.current_scope = "ctx:user"
        state.messages.append(UserMsg(name="user", content="Can you keep this concise?"))

        projection = asyncio.run(hooks.project(state, token_budget=200))
        assert projection is not None
        sink.deposit_valid(
            CandidatePoolProposal(
                payload=ValidCandidatePayload(
                    candidate=PreferenceCandidate(
                        candidate_id="candidate:echo",
                        statement="The user prefers concise summaries.",
                        scope="ctx:user",
                        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                        verification_status=memory.VerificationStatus.USER_CONFIRMED,
                    )
                ),
                origin=CandidateOrigin.MAIN_AGENT_TOOL,
            ),
            intent_fingerprint="intent:echo",
        )

        asyncio.run(hooks.after_model_reply(state, AssistantMsg(name="assistant", content="ok")))

        assert pool.list_candidates() == []
        assert sink.pending_count() == 0
    finally:
        store.delete_graph(graph_id)


def test_memory_search_and_get_tools_return_structured_canonical_results() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    recall = LexicalMemoryRecallService(query)
    try:
        _put_preference(
            store,
            graph_id=graph_id,
            memory_id="preference:tool-concise",
            statement="The user prefers concise summaries.",
            status=memory.NodeStatus.ACTIVE,
        )
        search = MemorySearchTool(recall=recall, graph_id=graph_id)
        get = MemoryGetTool(memory_query=query, graph_id=graph_id)

        search_result = search.execute(
            ToolCall(
                id="call:memory-search",
                name="memory_search",
                arguments={"query": "concise summaries", "scope": "ctx:user", "kind": "Preference"},
            )
        )
        search_payload = json.loads(search_result.output)
        get_result = get.execute(
            ToolCall(
                id="call:memory-get",
                name="memory_get",
                arguments={"memory_id": "preference:tool-concise"},
            )
        )
        get_payload = json.loads(get_result.output)

        assert search_payload["status"] == "ok"
        assert search_payload["results"][0]["memory_id"] == "preference:tool-concise"
        assert search_payload["results"][0]["deep_recall"] == "memory_get preference:tool-concise"
        assert get_payload["status"] == "ok"
        assert get_payload["memory"]["statement"] == "The user prefers concise summaries."
        assert get_payload["memory"]["status"] == "active"
    finally:
        store.delete_graph(graph_id)


def test_recall_trace_records_usage_and_suppresses_recent_auto_injection() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    session_id = f"runtime:test:{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    trace_store = PostgresRecallTraceStore(dsn=dsn)
    service = LexicalMemoryRecallService(
        PostgresMemoryQuery(dsn=dsn),
        trace_store=trace_store,
    )
    try:
        _put_preference(
            store,
            graph_id=graph_id,
            memory_id="preference:trace-concise",
            statement="The user prefers concise summaries.",
            status=memory.NodeStatus.ACTIVE,
        )
        MemorySearchIndexSync(dsn=dsn).sync_memory("preference:trace-concise", graph_id=graph_id)

        first = asyncio.run(
            service.recall(
                RecallQuery(
                    text="Please keep this concise.",
                    scopes=("ctx:user",),
                    session_id=session_id,
                    run_id="run:trace:first",
                    turn_id="turn:trace:first",
                    reply_id="reply:trace:first",
                ),
                graph_id=graph_id,
            )
        )
        second = asyncio.run(
            service.recall(
                RecallQuery(
                    text="Please keep this concise again.",
                    scopes=("ctx:user",),
                    session_id=session_id,
                    run_id="run:trace:second",
                    turn_id="turn:trace:second",
                    reply_id="reply:trace:second",
                ),
                graph_id=graph_id,
            )
        )
        explicit = asyncio.run(
            service.recall(
                RecallQuery(
                    text="Please keep this concise.",
                    scopes=("ctx:user",),
                    trigger=RecallTrigger.EXPLICIT_SEARCH,
                    session_id=session_id,
                    run_id="run:trace:explicit",
                    turn_id="turn:trace:explicit",
                    reply_id="reply:trace:explicit",
                ),
                graph_id=graph_id,
            )
        )

        assert first.status is RecallStatus.OK
        assert [item.memory_id for item in first.items] == ["preference:trace-concise"]
        assert second.status is RecallStatus.EMPTY
        assert "preference:trace-concise" in second.filtered_ids
        assert explicit.status is RecallStatus.OK
        assert [item.memory_id for item in explicit.items] == ["preference:trace-concise"]
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM recall_traces
                    WHERE graph_id = %s AND session_id = %s
                    """,
                    (graph_id, session_id),
                )
                assert cursor.fetchone() == (3,)
                cursor.execute(
                    """
                    SELECT injected, selected_by_tool
                    FROM recall_usages
                    WHERE graph_id = %s AND memory_id = %s
                    ORDER BY injected DESC, selected_by_tool ASC
                    """,
                    (graph_id, "preference:trace-concise"),
                )
                assert cursor.fetchall() == [(True, False), (False, True)]
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM recall_traces
                    WHERE graph_id = %s AND session_id = %s
                    """,
                    (graph_id, session_id),
                )
        store.delete_graph(graph_id)


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


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


class _FailingMemoryQuery:
    def __init__(self) -> None:
        self.call_count = 0

    def lexical_candidates(self, **kwargs):
        self.call_count += 1
        raise RuntimeError("database offline")

    def fts_candidates(self, **kwargs):
        raise AssertionError("fts should not be called after lexical failure")

    def fetch_nodes(self, ids, *, graph_id=None):
        raise AssertionError("fetch should not be called")
