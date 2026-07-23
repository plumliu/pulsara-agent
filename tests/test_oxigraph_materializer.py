from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

import pytest

from tests.support.postgres import (
    connect_postgres_test_database as _connect_or_skip,
    verified_postgres_provider,
)

from pulsara_agent.entities.memory import Preference
from pulsara_agent.graph import OxigraphGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory import OxigraphMaterializer
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


pytestmark = pytest.mark.skipif(
    not oxigraph_available(),
    reason="Oxigraph is not running at http://localhost:7878",
)


def test_oxigraph_materializer_applies_pending_surface_and_marks_outbox_partial() -> None:
    storage = StorageConfig.from_env()
    dsn = storage.postgres_dsn
    graph_id = f"graph:test:{uuid4().hex}"
    outbox_id = f"outbox:test:{uuid4().hex}"
    memory_id = f"preference:test:{uuid4().hex}"
    now = utc_now()
    document = Preference(
        id=memory_id,
        statement="The user prefers concise summaries.",
        scope="ctx:user",
        status=memory.NodeStatus.ACTIVE,
        confidence_level=memory.ConfidenceLevel.HIGH,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        created_at=now,
        updated_at=now,
        gate_reason="test",
    ).to_jsonld()
    store = OxigraphGraphStore(storage.oxigraph_url)
    try:
        _insert_outbox(dsn, graph_id=graph_id, outbox_id=outbox_id, memory_id=memory_id, document=document)

        applied = OxigraphMaterializer(
            oxigraph=store,
            connection_provider=verified_postgres_provider(dsn),
        ).consume_outbox(graph_id=graph_id)

        assert applied == 1
        fetched = store.get_jsonld(memory_id, graph_id=graph_id)
        assert fetched[memory.STATEMENT.name] == "The user prefers concise summaries."
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT status, payload FROM memory_write_outbox WHERE outbox_id = %s", (outbox_id,))
                status, payload = cursor.fetchone()
                assert status == "partial"
                assert payload["surface_apply_status"]["oxigraph"] == "applied"
                assert payload["surface_apply_status"]["search_index"] == "pending"
    finally:
        store.delete_graph(graph_id)
        _delete_outbox(dsn, outbox_id)


def test_oxigraph_materializer_retries_failed_surface_once_oxigraph_recovers() -> None:
    storage = StorageConfig.from_env()
    dsn = storage.postgres_dsn
    graph_id = f"graph:test:{uuid4().hex}"
    outbox_id = f"outbox:test:{uuid4().hex}"
    memory_id = f"preference:test:{uuid4().hex}"
    now = utc_now()
    document = Preference(
        id=memory_id,
        statement="The user prefers concise summaries.",
        scope="ctx:user",
        status=memory.NodeStatus.ACTIVE,
        confidence_level=memory.ConfidenceLevel.HIGH,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        created_at=now,
        updated_at=now,
        gate_reason="test",
    ).to_jsonld()
    good_store = OxigraphGraphStore(storage.oxigraph_url)
    bad_store = OxigraphGraphStore("http://127.0.0.1:1")
    try:
        _insert_outbox(dsn, graph_id=graph_id, outbox_id=outbox_id, memory_id=memory_id, document=document)

        applied = OxigraphMaterializer(
            oxigraph=bad_store,
            connection_provider=verified_postgres_provider(dsn),
        ).consume_outbox(graph_id=graph_id)
        assert applied == 0

        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status, payload, attempt_count, last_error FROM memory_write_outbox WHERE outbox_id = %s",
                    (outbox_id,),
                )
                status, payload, attempt_count, last_error = cursor.fetchone()
                assert status == "failed"
                assert payload["surface_apply_status"]["oxigraph"] == "failed"
                assert attempt_count == 1
                assert isinstance(last_error, str) and last_error

        applied = OxigraphMaterializer(
            oxigraph=good_store,
            connection_provider=verified_postgres_provider(dsn),
        ).consume_outbox(graph_id=graph_id)

        assert applied == 1
        fetched = good_store.get_jsonld(memory_id, graph_id=graph_id)
        assert fetched[memory.STATEMENT.name] == "The user prefers concise summaries."
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status, payload, attempt_count, last_error FROM memory_write_outbox WHERE outbox_id = %s",
                    (outbox_id,),
                )
                status, payload, attempt_count, last_error = cursor.fetchone()
                assert status == "partial"
                assert payload["surface_apply_status"]["oxigraph"] == "applied"
                assert payload["surface_apply_status"]["search_index"] == "pending"
                assert attempt_count == 2
                assert last_error is None
    finally:
        good_store.delete_graph(graph_id)
        _delete_outbox(dsn, outbox_id)


def test_oxigraph_materializer_replays_graph_reset_tombstone() -> None:
    storage = StorageConfig.from_env()
    dsn = storage.postgres_dsn
    graph_id = f"graph:test:{uuid4().hex}"
    outbox_id = f"outbox:test:{uuid4().hex}"
    memory_id = f"preference:test:{uuid4().hex}"
    now = utc_now()
    document = Preference(
        id=memory_id,
        statement="The user prefers concise summaries.",
        scope="ctx:user",
        status=memory.NodeStatus.ACTIVE,
        confidence_level=memory.ConfidenceLevel.HIGH,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        created_at=now,
        updated_at=now,
        gate_reason="test",
    ).to_jsonld()
    store = OxigraphGraphStore(storage.oxigraph_url)
    try:
        store.put_jsonld(document, graph_id=graph_id)
        _insert_graph_reset_outbox(dsn, graph_id=graph_id, outbox_id=outbox_id)

        applied = OxigraphMaterializer(
            oxigraph=store,
            connection_provider=verified_postgres_provider(dsn),
        ).consume_outbox(graph_id=graph_id)

        assert applied == 1
        assert not store.has_jsonld(memory_id, graph_id=graph_id)
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT status, payload FROM memory_write_outbox WHERE outbox_id = %s", (outbox_id,))
                status, payload = cursor.fetchone()
                assert status == "applied"
                assert payload["surface_apply_status"]["oxigraph"] == "applied"
                assert payload["graph_reset"] is True
    finally:
        store.delete_graph(graph_id)
        _delete_outbox(dsn, outbox_id)


def _insert_outbox(
    dsn: str,
    *,
    graph_id: str,
    outbox_id: str,
    memory_id: str,
    document: dict,
) -> None:
    from psycopg.types.json import Jsonb

    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_write_outbox (
                    outbox_id,
                    graph_id,
                    governance_batch_id,
                    decision_id,
                    mutation_lane,
                    sequence_key,
                    target_entry_key,
                    dirty_memory_ids,
                    payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, to_jsonb(%s::text[]), %s)
                """,
                (
                    outbox_id,
                    graph_id,
                    f"governance:test:{uuid4().hex}",
                    f"decision:test:{uuid4().hex}",
                    "governed_memory",
                    graph_id,
                    f"pool:test:{uuid4().hex}",
                    [memory_id],
                    Jsonb(
                        {
                            "kind": "canonical_mutation",
                            "mutation_lane": "governed_memory",
                            "dirty_memory_ids": [memory_id],
                            "documents": [{"node_id": memory_id, "document": document}],
                            "surface_apply_status": {
                                "search_index": "pending",
                                "oxigraph": "pending",
                            },
                        }
                    ),
                ),
            )


def _insert_graph_reset_outbox(
    dsn: str,
    *,
    graph_id: str,
    outbox_id: str,
) -> None:
    from psycopg.types.json import Jsonb

    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_write_outbox (
                    outbox_id,
                    graph_id,
                    mutation_lane,
                    sequence_key,
                    target_entry_key,
                    dirty_memory_ids,
                    payload
                )
                VALUES (%s, %s, %s, %s, %s, to_jsonb(%s::text[]), %s)
                """,
                (
                    outbox_id,
                    graph_id,
                    "graph_reset",
                    graph_id,
                    f"graph-reset:{graph_id}",
                    [],
                    Jsonb(
                        {
                            "kind": "canonical_mutation",
                            "mutation_lane": "graph_reset",
                            "dirty_memory_ids": [],
                            "documents": [],
                            "surface_apply_status": {
                                "oxigraph": "pending",
                            },
                            "graph_reset": True,
                        }
                    ),
                ),
            )


def _delete_outbox(dsn: str, outbox_id: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM memory_write_outbox WHERE outbox_id = %s", (outbox_id,))
