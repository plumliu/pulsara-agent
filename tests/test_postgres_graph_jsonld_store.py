from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.ontology import capability as cap
from pulsara_agent.ontology import memory
from pulsara_agent.ontology.registry import CORE_CONTEXT
from pulsara_agent.settings import StorageConfig


@pytest.fixture
def graph_store():
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph = PostgresGraphStore(dsn=dsn)
    graph_id = f"graph:test:{uuid4().hex}"
    try:
        yield graph, graph_id
    finally:
        graph.delete_graph(graph_id)


def test_postgres_graph_store_normalizes_full_iris_to_core_context(graph_store) -> None:
    graph, graph_id = graph_store
    graph.put_jsonld(
        {
            "@context": CORE_CONTEXT,
            "@id": "https://pulsara.dev/skill/search",
            "@type": [cap.SKILL.value],
            cap.PROVIDES_TOOL.value: {"@id": "https://pulsara.dev/tool/rg"},
        },
        graph_id=graph_id,
    )

    document = graph.get_jsonld("skill:search", graph_id=graph_id)

    assert document["@id"] == "skill:search"
    assert document["@type"] == [cap.SKILL.name]
    assert document[cap.PROVIDES_TOOL.name] == [{"@id": "tool:rg"}]
    assert [doc["@id"] for doc in graph.find_by_type(cap.SKILL, graph_id=graph_id)] == ["skill:search"]


def test_postgres_graph_store_preserves_force_list_edges_for_single_values(graph_store) -> None:
    graph, graph_id = graph_store
    graph.put_jsonld(
        {
            "@context": CORE_CONTEXT,
            "@id": "plugin:single",
            "@type": [cap.PLUGIN.name],
            cap.PROVIDES_TOOL.name: {"@id": "tool:rg"},
            cap.PROVIDES_SKILL.name: {"@id": "skill:search"},
        },
        graph_id=graph_id,
    )

    document = graph.get_jsonld("plugin:single", graph_id=graph_id)

    assert document[cap.PROVIDES_TOOL.name] == [{"@id": "tool:rg"}]
    assert document[cap.PROVIDES_SKILL.name] == [{"@id": "skill:search"}]


def test_postgres_graph_store_normalizes_memory_entity_prefixes(graph_store) -> None:
    graph, graph_id = graph_store
    graph.put_jsonld(
        {
            "@context": CORE_CONTEXT,
            "@id": "https://pulsara.dev/preference/concise",
            "@type": [memory.PREFERENCE.value],
            memory.STATEMENT.value: "Prefer concise answers.",
        },
        graph_id=graph_id,
    )

    document = graph.get_jsonld("preference:concise", graph_id=graph_id)

    assert document["@id"] == "preference:concise"
    assert document["@type"] == [memory.PREFERENCE.name]
    assert document[memory.STATEMENT.name] == "Prefer concise answers."


def test_postgres_graph_store_deduplicates_edges_like_rdf_triples(graph_store) -> None:
    graph, graph_id = graph_store
    graph.put_jsonld(
        {
            "@context": CORE_CONTEXT,
            "@id": "skill:dedupe",
            "@type": [cap.SKILL.name],
            cap.PROVIDES_TOOL.name: [{"@id": "tool:rg"}, {"@id": "tool:rg"}],
        },
        graph_id=graph_id,
    )

    document = graph.get_jsonld("skill:dedupe", graph_id=graph_id)

    assert document[cap.PROVIDES_TOOL.name] == [{"@id": "tool:rg"}]


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")
