from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
import urllib.error
import urllib.parse
import urllib.request

from pulsara_agent.entities.memory import Preference
from pulsara_agent.graph import OxigraphGraphStore, PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory import PostgresMemoryQuery
from pulsara_agent.memory.canonical.reconcile import PostgresMemoryReconciler
from pulsara_agent.ontology import memory
from pulsara_agent.settings import StorageConfig


def oxigraph_available() -> bool:
    query = urllib.parse.urlencode({"query": "ASK { ?s ?p ?o }"}).encode("utf-8")
    request = urllib.request.Request(
        "http://localhost:7878/query",
        data=query,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1):
            return True
    except (OSError, urllib.error.URLError):
        return False


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
                assert cursor.fetchone() == ("partial",)
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


def test_postgres_delete_graph_clears_pending_outbox_for_graph() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    memory_id = "preference:delete-graph-outbox"
    outbox_id = f"outbox:test:{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    try:
        store.put_jsonld(_preference(memory_id, "The user prefers concise summaries."), graph_id=graph_id)
        _insert_outbox(dsn, graph_id=graph_id, memory_id=memory_id, outbox_id=outbox_id)

        store.delete_graph(graph_id)

        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM memory_write_outbox WHERE graph_id = %s", (graph_id,))
                assert cursor.fetchone() == (0,)
                cursor.execute("SELECT count(*) FROM memory_search_index WHERE graph_id = %s", (graph_id,))
                assert cursor.fetchone() == (0,)
    finally:
        _delete_outbox(dsn, outbox_id)


@pytest.mark.skipif(
    not oxigraph_available(),
    reason="Oxigraph is not running at http://localhost:7878",
)
def test_reconciler_can_replay_outbox_into_oxigraph() -> None:
    storage = StorageConfig.from_env()
    dsn = storage.postgres_dsn
    graph_id = f"graph:test/{uuid4().hex}"
    memory_id = "preference:reconcile-oxigraph"
    outbox_id = f"outbox:test:{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    oxigraph = OxigraphGraphStore(storage.oxigraph_url)
    try:
        document = _preference(memory_id, "The user prefers concise summaries.")
        store.put_jsonld(document, graph_id=graph_id)
        _insert_outbox(dsn, graph_id=graph_id, memory_id=memory_id, outbox_id=outbox_id, document=document)

        report = PostgresMemoryReconciler(dsn=dsn, oxigraph=oxigraph).reconcile(graph_id=graph_id)

        assert report.outbox_applied_count == 1
        assert report.oxigraph_gaps == ()
        fetched = oxigraph.get_jsonld(memory_id, graph_id=graph_id)
        assert fetched[memory.STATEMENT.name] == "The user prefers concise summaries."
    finally:
        oxigraph.delete_graph(graph_id)
        _delete_outbox(dsn, outbox_id)
        store.delete_graph(graph_id)


@pytest.mark.skipif(
    not oxigraph_available(),
    reason="Oxigraph is not running at http://localhost:7878",
)
def test_reconciler_reports_missing_oxigraph_node_as_parity_gap() -> None:
    storage = StorageConfig.from_env()
    dsn = storage.postgres_dsn
    graph_id = f"graph:test/{uuid4().hex}"
    memory_id = "preference:parity-gap"
    store = PostgresGraphStore(dsn=dsn)
    oxigraph = OxigraphGraphStore(storage.oxigraph_url)
    try:
        document = _preference(memory_id, "The user prefers concise summaries.")
        store.put_jsonld(document, graph_id=graph_id)

        report = PostgresMemoryReconciler(dsn=dsn, oxigraph=oxigraph).reconcile(graph_id=graph_id, outbox_limit=0)

        assert report.outbox_applied_count == 0
        assert [(gap.graph_id, gap.node_id, gap.reason) for gap in report.oxigraph_gaps] == [
            (graph_id, memory_id, "missing_in_oxigraph")
        ]
    finally:
        oxigraph.delete_graph(graph_id)
        store.delete_graph(graph_id)


@pytest.mark.skipif(
    not oxigraph_available(),
    reason="Oxigraph is not running at http://localhost:7878",
)
def test_reconciler_reports_stale_oxigraph_node_as_parity_gap() -> None:
    storage = StorageConfig.from_env()
    dsn = storage.postgres_dsn
    graph_id = f"graph:test/{uuid4().hex}"
    memory_id = "preference:stale-oxigraph"
    oxigraph = OxigraphGraphStore(storage.oxigraph_url)
    try:
        oxigraph.put_jsonld(_preference(memory_id, "The user prefers concise summaries."), graph_id=graph_id)

        report = PostgresMemoryReconciler(dsn=dsn, oxigraph=oxigraph).reconcile(graph_id=graph_id, outbox_limit=0)

        assert [(gap.graph_id, gap.node_id, gap.reason) for gap in report.oxigraph_gaps] == [
            (graph_id, memory_id, "stale_in_oxigraph")
        ]
    finally:
        oxigraph.delete_graph(graph_id)


def _insert_outbox(
    dsn: str,
    *,
    graph_id: str,
    memory_id: str,
    outbox_id: str,
    document: dict | None = None,
) -> None:
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
                            "kind": "canonical_mutation",
                            "mutation_lane": "governed_memory",
                            "dirty_memory_ids": [memory_id],
                            "documents": (
                                [{"node_id": memory_id, "document": document}] if document is not None else []
                            ),
                            "surface_apply_status": {
                                "search_index": "pending",
                                "oxigraph": "pending",
                            },
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
