from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.entities.memory import Preference
from pulsara_agent.entities.runtime import Evidence
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.jsonld import NodeRef, utc_now
from pulsara_agent.memory.index_sync import MemorySearchIndexSync
from pulsara_agent.memory import PostgresMemoryQuery
from pulsara_agent.ontology import memory, runtime as rt
from pulsara_agent.settings import StorageConfig


def test_postgres_graph_store_projects_memory_nodes_and_runtime_source_relations() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test:{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    now = utc_now()

    store.put_jsonld(
        Preference(
            id="preference:test-concise",
            statement="The user prefers concise summaries.",
            scope="ctx:user",
            status=memory.NodeStatus.ACTIVE,
            confidence_level=memory.ConfidenceLevel.HIGH,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=now,
            updated_at=now,
            gate_reason="explicit user preference",
        ).to_jsonld(),
        graph_id=graph_id,
    )
    store.put_jsonld(
        Evidence(
            id="evidence:test-concise",
            statement="User said they prefer concise summaries.",
            source_type=rt.EvidenceSourceType.TOOL_RESULT,
            status=memory.NodeStatus.ACTIVE,
            observed_at=now,
            scope="ctx:user",
            created_from=NodeRef("tool-result:test-concise"),
        ).to_jsonld(),
        graph_id=graph_id,
    )
    evidence_document = store.get_jsonld("evidence:test-concise", graph_id=graph_id)
    evidence_document[memory.SUPPORTS.name] = [{"@id": "preference:test-concise"}]
    store.put_jsonld(evidence_document, graph_id=graph_id)

    fetched = query.fetch_nodes(["preference:test-concise"], graph_id=graph_id)

    assert len(fetched) == 1
    assert fetched[0].id == "preference:test-concise"
    assert fetched[0].evidence_ids == ("evidence:test-concise",)
    assert (memory.SUPPORTS.name, "evidence:test-concise") in fetched[0].incoming
    assert query.lexical_candidates(
        terms=["concise"],
        scopes=["ctx:user"],
        types=["Preference"],
        limit=5,
        graph_id=graph_id,
    ) == [("preference:test-concise", 2.0)]
    MemorySearchIndexSync(dsn=dsn).sync_memory("preference:test-concise", graph_id=graph_id)
    assert query.fts_candidates(
        query_text="concise summaries",
        scopes=["ctx:user"],
        types=["Preference"],
        limit=5,
        graph_id=graph_id,
    )[0][0] == "preference:test-concise"

    store.delete_graph(graph_id)
    assert query.fetch_nodes(["preference:test-concise"], graph_id=graph_id) == []


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")
