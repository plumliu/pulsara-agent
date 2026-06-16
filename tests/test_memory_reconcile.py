from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pulsara_agent.entities.memory import Preference
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory import PostgresMemoryQuery
from pulsara_agent.memory.canonical.reconcile import PostgresMemoryReconciler
from pulsara_agent.ontology import memory
from pulsara_agent.settings import StorageConfig


def test_reconciler_replays_pending_outbox_into_search_index() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    memory_id = "preference:reconcile-index"
    outbox_id = f"outbox:test:{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    try:
        store.put_jsonld(_preference(memory_id, "The user prefers concise summaries."), graph_id=graph_id)
        _insert_outbox(dsn, graph_id=graph_id, memory_id=memory_id, outbox_id=outbox_id)

        report = PostgresMemoryReconciler(dsn=dsn).reconcile(graph_id=graph_id)

        assert report.outbox_applied_count == 1
        assert query.fts_candidates(
            query_text="concise summaries",
            scopes=["ctx:user"],
            types=["Preference"],
            limit=5,
            graph_id=graph_id,
        )[0][0] == memory_id
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT status FROM memory_write_outbox WHERE outbox_id = %s", (outbox_id,))
                assert cursor.fetchone() == ("applied",)
    finally:
        _delete_outbox(dsn, outbox_id)
        store.delete_graph(graph_id)


def test_reconciler_reports_canonical_node_without_governance_outbox() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    memory_id = "preference:damaged-direct"
    store = PostgresGraphStore(dsn=dsn)
    try:
        store.put_jsonld(_preference(memory_id, "The user prefers concise summaries."), graph_id=graph_id)

        damaged = PostgresMemoryReconciler(dsn=dsn).find_damaged_nodes(graph_id=graph_id)

        assert [(item.graph_id, item.memory_id, item.reason) for item in damaged] == [
            (graph_id, memory_id, "missing_governance_outbox")
        ]
    finally:
        store.delete_graph(graph_id)


def test_reconciler_does_not_report_node_with_matching_governance_outbox() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    memory_id = "preference:not-damaged"
    outbox_id = f"outbox:test:{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    try:
        store.put_jsonld(_preference(memory_id, "The user prefers concise summaries."), graph_id=graph_id)
        _insert_outbox(dsn, graph_id=graph_id, memory_id=memory_id, outbox_id=outbox_id)

        damaged = PostgresMemoryReconciler(dsn=dsn).find_damaged_nodes(graph_id=graph_id)

        assert damaged == ()
    finally:
        _delete_outbox(dsn, outbox_id)
        store.delete_graph(graph_id)


def _insert_outbox(dsn: str, *, graph_id: str, memory_id: str, outbox_id: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_write_outbox (
                    outbox_id,
                    graph_id,
                    governance_batch_id,
                    decision_id,
                    target_entry_key,
                    payload
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    outbox_id,
                    graph_id,
                    f"governance:test:{uuid4().hex}",
                    f"decision:test:{uuid4().hex}",
                    f"pool:test:{uuid4().hex}",
                    Jsonb(
                        {
                            "kind": "memory_governance_decision_committed",
                            "decision_record": {
                                "write_outcome": {
                                    "kind": "write_succeeded",
                                    "memory_id": memory_id,
                                }
                            },
                        }
                    ),
                ),
            )


def _delete_outbox(dsn: str, outbox_id: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM memory_write_outbox WHERE outbox_id = %s", (outbox_id,))


def _preference(memory_id: str, statement: str) -> dict:
    now = utc_now()
    return Preference(
        id=memory_id,
        statement=statement,
        scope="ctx:user",
        status=memory.NodeStatus.ACTIVE,
        confidence_level=memory.ConfidenceLevel.HIGH,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        created_at=now,
        updated_at=now,
        gate_reason="test",
    ).to_jsonld()


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")
