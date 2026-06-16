from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

import pytest

from pulsara_agent.entities.capability import Plugin, Skill
from pulsara_agent.event import EventContext, ReplyEndEvent, TextBlockDeltaEvent
from pulsara_agent.graph import OxigraphGraphStore
from pulsara_agent.jsonld import NodeRef
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.memory import (
    ExecutionEvidenceLedger,
    InMemoryArchiveStore,
    load_run_timeline,
    summarize_run_timeline,
)
from pulsara_agent.memory.foundation.provenance import RuntimeEventSpan
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.ontology import capability as cap
from pulsara_agent.ontology import memory, runtime as rt
from pulsara_agent.runtime import build_durable_runtime_wiring
from pulsara_agent.settings import PulsaraSettings, StorageConfig


OXIGRAPH_URL = "http://localhost:7878"


def oxigraph_available() -> bool:
    query = urllib.parse.urlencode({"query": "ASK { ?s ?p ?o }"}).encode("utf-8")
    request = urllib.request.Request(
        f"{OXIGRAPH_URL}/query",
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


def test_oxigraph_store_put_get_query_and_delete_named_graph() -> None:
    graph_id = f"graph:test/{uuid4().hex}"
    store = OxigraphGraphStore(OXIGRAPH_URL)
    try:
        store.put_jsonld(
            {
                "@context": memory.CONTEXT,
                "@id": "claim:oxigraph-test",
                "@type": [memory.CLAIM.name],
                memory.STATEMENT.name: "Oxigraph round trip works.",
                memory.SCOPE.name: "ctx:test",
            },
            graph_id=graph_id,
        )

        document = store.get_jsonld("claim:oxigraph-test", graph_id=graph_id)
        rows = store.query(
            """
SELECT ?statement WHERE {
  GRAPH <https://pulsara.dev/graph/test-placeholder> {
    ?s <https://pulsara.dev/memory#statement> ?statement .
  }
}
""".replace("https://pulsara.dev/graph/test-placeholder", f"https://pulsara.dev/graph/test/{graph_id.rsplit('/', 1)[1]}")
        )

        assert document["@id"] == "claim:oxigraph-test"
        assert document["@type"] == [memory.CLAIM.name]
        assert document[memory.STATEMENT.name] == "Oxigraph round trip works."
        assert rows == [{"statement": "Oxigraph round trip works."}]
    finally:
        store.delete_graph(graph_id)
    assert not store.has_jsonld("claim:oxigraph-test", graph_id=graph_id)


def test_oxigraph_store_supports_ledger_provenance_round_trip() -> None:
    graph_id = f"graph:test/{uuid4().hex}"
    store = OxigraphGraphStore(OXIGRAPH_URL)
    ledger = ExecutionEvidenceLedger(
        graph=store,
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
        graph_id=graph_id,
    )
    span = RuntimeEventSpan(
        session_id="runtime:oxigraph",
        run_id="run:oxigraph",
        turn_id="turn:oxigraph",
        reply_id="reply:oxigraph",
        start_sequence=10,
        end_sequence=12,
        source_event_id="event-oxigraph",
    )

    try:
        result = ledger.record_tool_result(
            turn_id="turn:oxigraph",
            tool_name="read_file",
            status=rt.ToolExecutionStatus.SUCCESS,
            input_summary="Read README",
            output="README content",
            scope="ctx:oxigraph",
            event_span=span,
        )

        tool_result = store.get_jsonld(result.tool_result_id, graph_id=graph_id)
        turn = store.get_jsonld("turn:oxigraph", graph_id=graph_id)

        assert tool_result[rt.EVENT_SPAN_PROPERTY.name][rt.SOURCE_EVENT.name] == {
            "@id": "event:event-oxigraph"
        }
        assert tool_result[rt.EVENT_SPAN_PROPERTY.name][rt.START_SEQUENCE.name] == 10
        assert turn[rt.PRODUCED.name] == [{"@id": result.tool_result_id}]
        assert len(store.find_by_type(rt.TOOL_RESULT, graph_id=graph_id)) == 1
    finally:
        store.delete_graph(graph_id)


def test_oxigraph_store_preserves_single_capability_edges_as_lists() -> None:
    graph_id = f"graph:test/{uuid4().hex}"
    store = OxigraphGraphStore(OXIGRAPH_URL)
    try:
        skill = Skill(
            id="skill:oxigraph-single",
            version="1.0.0",
            provides_tool=(NodeRef("tool:rg"),),
            requires=(NodeRef("tool:fd"),),
        )
        plugin = Plugin(
            id="plugin:oxigraph-single",
            version="1.0.0",
            provides_tool=(NodeRef("tool:rg"),),
            provides_skill=(NodeRef("skill:oxigraph-single"),),
        )
        store.put_jsonld(skill.to_jsonld(), graph_id=graph_id)
        store.put_jsonld(plugin.to_jsonld(), graph_id=graph_id)

        skill_doc = store.get_jsonld("skill:oxigraph-single", graph_id=graph_id)
        plugin_doc = store.get_jsonld("plugin:oxigraph-single", graph_id=graph_id)

        assert skill_doc[cap.PROVIDES_TOOL.name] == [{"@id": "tool:rg"}]
        assert skill_doc[cap.REQUIRES.name] == [{"@id": "tool:fd"}]
        assert plugin_doc[cap.PROVIDES_TOOL.name] == [{"@id": "tool:rg"}]
        assert plugin_doc[cap.PROVIDES_SKILL.name] == [{"@id": "skill:oxigraph-single"}]
    finally:
        store.delete_graph(graph_id)


def test_oxigraph_timeline_hook_uses_postgres_event_log_and_artifact_store(tmp_path) -> None:
    storage = StorageConfig.from_env()
    graph_id = f"graph:test/{uuid4().hex}"
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    ctx = EventContext(
        run_id=f"run:oxigraph-timeline:{uuid4().hex}",
        turn_id=f"turn:oxigraph-timeline:{uuid4().hex}",
        reply_id=f"reply:oxigraph-timeline:{uuid4().hex}",
    )
    wiring = build_durable_runtime_wiring(
        _settings_for_storage(storage),
        tmp_path,
        runtime_session_id=runtime_session_id,
        graph_id=graph_id,
    )
    timeline_blob_id: str | None = None

    async def run() -> None:
        await wiring.runtime_session.emit(
            TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:1", delta="hello oxigraph")
        )
        await wiring.runtime_session.emit(ReplyEndEvent(**ctx.event_fields()))

    try:
        import asyncio

        asyncio.run(run())
        records = wiring.graph.find_by_type(rt.RUN_TIMELINE, graph_id=graph_id)
        timeline_blob_id = _artifact_id_from_node_ref(records[0][rt.STORED_AS.name]["@id"])
        timeline = load_run_timeline(
            graph=wiring.graph,
            archive=wiring.archive,
            run_id=ctx.run_id,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
        )
        summary = summarize_run_timeline(timeline)

        assert len(records) == 1
        assert records[0][rt.SOURCE_RUN.name] == ctx.run_id
        assert records[0][rt.SOURCE_SESSION.name] == runtime_session_id
        assert records[0][rt.STATUS.name] == "completed"
        assert timeline_blob_id.startswith(f"timeline:{runtime_session_id}:{ctx.run_id}:")
        assert "hello oxigraph" in wiring.archive.get_text(timeline_blob_id)
        assert summary.assistant_text == "hello oxigraph"
    finally:
        wiring.graph.delete_graph(graph_id)
        _delete_postgres_artifacts_with_prefix(storage.postgres_dsn, f"timeline:{runtime_session_id}:{ctx.run_id}:")
        _delete_postgres_runtime_session(storage.postgres_dsn, runtime_session_id)


def _settings_for_storage(storage: StorageConfig) -> PulsaraSettings:
    return PulsaraSettings(
        llm=LLMConfig(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
        ),
        storage=storage,
    )


def _delete_postgres_runtime_session(dsn: str, runtime_session_id: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def _delete_postgres_artifacts_with_prefix(dsn: str, blob_id_prefix: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from artifacts where id like %s", (f"{blob_id_prefix}%",))


def _artifact_id_from_node_ref(node_id: str) -> str:
    prefix = "urn:pulsara:"
    if node_id.startswith(prefix):
        return urllib.parse.unquote(node_id[len(prefix) :])
    return node_id
