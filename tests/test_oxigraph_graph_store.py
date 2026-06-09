from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

import pytest

from pulsara_agent.graph import OxigraphGraphStore
from pulsara_agent.memory import ExecutionEvidenceLedger, InMemoryArchiveStore
from pulsara_agent.memory.provenance import RuntimeEventSpan
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.ontology import memory


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
            status=memory.ToolExecutionStatus.SUCCESS,
            input_summary="Read README",
            output="README content",
            scope="ctx:oxigraph",
            event_span=span,
        )

        tool_result = store.get_jsonld(result.tool_result_id, graph_id=graph_id)
        turn = store.get_jsonld("turn:oxigraph", graph_id=graph_id)

        assert tool_result[memory.EVENT_SPAN.name][memory.SOURCE_EVENT.name] == {
            "@id": "event:event-oxigraph"
        }
        assert tool_result[memory.EVENT_SPAN.name][memory.START_SEQUENCE.name] == 10
        assert turn[memory.PRODUCED.name] == [{"@id": result.tool_result_id}]
        assert len(store.find_by_type(memory.TOOL_RESULT, graph_id=graph_id)) == 1
    finally:
        store.delete_graph(graph_id)
