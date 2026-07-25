from __future__ import annotations

from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.ontology import memory


def test_named_graph_supports_put_get_and_delete() -> None:
    graph = InMemoryGraphStore()
    document = {
        "@id": "tool-result:1",
        "@type": ["ToolResult"],
        "scope": "ctx:workspace/test_project",
    }

    graph.put_jsonld(document, graph_id="graph:test")
    assert (
        graph.get_jsonld("tool-result:1", graph_id="graph:test")["@id"]
        == "tool-result:1"
    )
    assert not graph.has_jsonld("tool-result:1")
    assert graph.has_jsonld("tool-result:1", graph_id="graph:test")
    try:
        graph.get_jsonld("tool-result:1")
    except KeyError:
        pass
    else:
        raise AssertionError(
            "Default graph lookup should not search named graphs"
        )
    graph.delete_graph("graph:test")

    try:
        graph.get_jsonld("tool-result:1", graph_id="graph:test")
    except KeyError:
        pass
    else:
        raise AssertionError("Expected deleted graph lookup to fail")


def test_put_jsonld_none_graph_id_writes_to_default_graph() -> None:
    graph = InMemoryGraphStore()
    graph.put_jsonld(
        {
            "@id": "claim:none",
            "@type": [memory.CLAIM.name],
            "statement": "default via none",
        },
        graph_id=None,
    )

    assert None not in graph.graphs
    assert "graph:default" in graph.graphs
    assert graph.has_jsonld("claim:none")
    assert graph.get_jsonld("claim:none")["statement"] == "default via none"


def test_empty_graph_id_is_rejected() -> None:
    graph = InMemoryGraphStore()

    try:
        graph.put_jsonld(
            {"@id": "claim:empty", "@type": [memory.CLAIM.name]},
            graph_id="",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Expected empty graph_id to be rejected")


def test_default_graph_lookup_does_not_return_named_graph_duplicate() -> None:
    graph = InMemoryGraphStore()
    graph.put_jsonld(
        {"@id": "claim:1", "@type": ["Claim"], "statement": "default"}
    )
    graph.put_jsonld(
        {"@id": "claim:1", "@type": ["Claim"], "statement": "named"},
        graph_id="graph:named",
    )

    assert graph.get_jsonld("claim:1")["statement"] == "default"
    assert (
        graph.get_jsonld("claim:1", graph_id="graph:named")["statement"]
        == "named"
    )
    assert graph.has_jsonld("claim:1")
    assert graph.has_jsonld("claim:1", graph_id="graph:named")


def test_default_find_by_type_does_not_scan_named_graphs() -> None:
    graph = InMemoryGraphStore()
    graph.put_jsonld(
        {
            "@id": "claim:default",
            "@type": [memory.CLAIM.name],
            "statement": "default",
        }
    )
    graph.put_jsonld(
        {
            "@id": "claim:named",
            "@type": [memory.CLAIM.name],
            "statement": "named",
        },
        graph_id="graph:named",
    )

    assert [doc["@id"] for doc in graph.find_by_type(memory.CLAIM)] == [
        "claim:default"
    ]
    assert [
        doc["@id"]
        for doc in graph.find_by_type(
            memory.CLAIM,
            graph_id="graph:named",
        )
    ] == ["claim:named"]


def test_find_by_type_returns_defensive_copies() -> None:
    graph = InMemoryGraphStore()
    graph.put_jsonld(
        {
            "@id": "claim:copy",
            "@type": [memory.CLAIM.name],
            "statement": "original",
        }
    )

    docs = graph.find_by_type(memory.CLAIM)
    docs[0]["statement"] = "mutated"

    assert graph.get_jsonld("claim:copy")["statement"] == "original"
