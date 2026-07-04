from pulsara_agent.graph import OxigraphGraphStore
from pulsara_agent.ontology import memory


class RecordingOxigraphGraphStore(OxigraphGraphStore):
    def __init__(self) -> None:
        super().__init__("http://example.test")
        self.updates: list[str] = []

    def update(self, sparql: str) -> None:
        self.updates.append(sparql)


def test_oxigraph_put_jsonld_uses_single_update_request() -> None:
    store = RecordingOxigraphGraphStore()
    store.put_jsonld(
        {
            "@context": memory.CONTEXT,
            "@id": "claim:unit-test",
            "@type": [memory.CLAIM.name],
            memory.STATEMENT.name: "single update",
            memory.SCOPE.name: "ctx:test",
        },
        graph_id="graph:test/unit",
    )

    assert len(store.updates) == 1
    assert "DELETE WHERE" in store.updates[0]
    assert "INSERT DATA" in store.updates[0]
    assert "claim/unit-test" in store.updates[0]


def test_oxigraph_put_jsonld_none_graph_id_targets_default_graph() -> None:
    store = RecordingOxigraphGraphStore()

    store.put_jsonld(
        {
            "@context": memory.CONTEXT,
            "@id": "claim:none-graph",
            "@type": [memory.CLAIM.name],
            memory.STATEMENT.name: "default graph",
            memory.SCOPE.name: "ctx:test",
        },
        graph_id=None,
    )

    assert len(store.updates) == 1
    assert "https://pulsara.dev/graph/default" in store.updates[0]


def test_oxigraph_empty_graph_id_is_rejected() -> None:
    store = RecordingOxigraphGraphStore()

    try:
        store.put_jsonld(
            {
                "@context": memory.CONTEXT,
                "@id": "claim:empty-graph",
                "@type": [memory.CLAIM.name],
            },
            graph_id="",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Expected empty graph_id to be rejected")

    assert store.updates == []
