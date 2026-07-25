from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

import pytest

from pulsara_agent.entities.capability import Plugin, Skill
from pulsara_agent.graph import OxigraphGraphStore
from pulsara_agent.jsonld import NodeRef
from pulsara_agent.ontology import capability as cap
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
""".replace(
                "https://pulsara.dev/graph/test-placeholder",
                f"https://pulsara.dev/graph/test/{graph_id.rsplit('/', 1)[1]}",
            )
        )

        assert document["@id"] == "claim:oxigraph-test"
        assert document["@type"] == [memory.CLAIM.name]
        assert document[memory.STATEMENT.name] == "Oxigraph round trip works."
        assert rows == [{"statement": "Oxigraph round trip works."}]
    finally:
        store.delete_graph(graph_id)
    assert not store.has_jsonld("claim:oxigraph-test", graph_id=graph_id)


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
