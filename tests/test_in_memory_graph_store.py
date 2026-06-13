from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.ontology import capability as cap
from pulsara_agent.ontology import memory
from pulsara_agent.ontology.registry import CORE_CONTEXT


def test_in_memory_graph_store_normalizes_full_iris_to_core_context() -> None:
    graph = InMemoryGraphStore()
    graph.put_jsonld(
        {
            "@context": CORE_CONTEXT,
            "@id": "https://pulsara.dev/skill/search",
            "@type": [cap.SKILL.value],
            cap.PROVIDES_TOOL.value: {"@id": "https://pulsara.dev/tool/rg"},
        }
    )

    document = graph.get_jsonld("skill:search")

    assert document["@id"] == "skill:search"
    assert document["@type"] == [cap.SKILL.name]
    assert document[cap.PROVIDES_TOOL.name] == [{"@id": "tool:rg"}]
    assert [doc["@id"] for doc in graph.find_by_type(cap.SKILL)] == ["skill:search"]


def test_in_memory_graph_store_preserves_force_list_edges_for_single_values() -> None:
    graph = InMemoryGraphStore()
    graph.put_jsonld(
        {
            "@context": CORE_CONTEXT,
            "@id": "plugin:single",
            "@type": [cap.PLUGIN.name],
            cap.PROVIDES_TOOL.name: {"@id": "tool:rg"},
            cap.PROVIDES_SKILL.name: {"@id": "skill:search"},
        }
    )

    document = graph.get_jsonld("plugin:single")

    assert document[cap.PROVIDES_TOOL.name] == [{"@id": "tool:rg"}]
    assert document[cap.PROVIDES_SKILL.name] == [{"@id": "skill:search"}]


def test_in_memory_graph_store_normalizes_memory_entity_prefixes() -> None:
    graph = InMemoryGraphStore()
    graph.put_jsonld(
        {
            "@context": CORE_CONTEXT,
            "@id": "https://pulsara.dev/preference/concise",
            "@type": [memory.PREFERENCE.value],
            memory.STATEMENT.value: "Prefer concise answers.",
        }
    )

    document = graph.get_jsonld("preference:concise")

    assert document["@id"] == "preference:concise"
    assert document["@type"] == [memory.PREFERENCE.name]
    assert document[memory.STATEMENT.name] == "Prefer concise answers."


def test_in_memory_graph_store_deduplicates_edges_like_rdf_triples() -> None:
    graph = InMemoryGraphStore()
    graph.put_jsonld(
        {
            "@context": CORE_CONTEXT,
            "@id": "skill:dedupe",
            "@type": [cap.SKILL.name],
            cap.PROVIDES_TOOL.name: [{"@id": "tool:rg"}, {"@id": "tool:rg"}],
        }
    )

    document = graph.get_jsonld("skill:dedupe")

    assert document[cap.PROVIDES_TOOL.name] == [{"@id": "tool:rg"}]
