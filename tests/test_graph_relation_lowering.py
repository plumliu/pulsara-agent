from __future__ import annotations

from typing import cast

import pytest

from pulsara_agent.ontology import runtime as rt
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.runtime.projection_jobs.contracts import (
    TurnProducedToolResultRelationFact,
    build_projection_fact,
)
from pulsara_agent.runtime.projection_jobs.graph_relation import (
    InMemoryCanonicalGraphRelationRepository,
    reject_owned_relation_predicates,
)


def test_immutable_relation_rows_survive_parallel_order() -> None:
    repository = InMemoryCanonicalGraphRelationRepository()
    for index in (2, 1):
        relation = cast(
            TurnProducedToolResultRelationFact,
            build_projection_fact(
                TurnProducedToolResultRelationFact,
                schema_version="turn_produced_tool_result_relation.v1",
                relation_document_id=f"relation:{index}",
                graph_id="graph:default",
                turn_id="turn:1",
                predicate_iri=rt.PRODUCED.value,
                tool_result_document_id=f"tool-result:{index}",
                source_tool_result_end_reference_fingerprint=context_fingerprint(
                    "test-tool-result-source:v1", index
                ),
            ),
        )
        repository.put(relation)
    page = repository.read_page(
        graph_id="graph:default",
        source_document_id="turn:1",
    )
    assert tuple(item.relation_id for item in page.ordered_relations) == (
        "relation:1",
        "relation:2",
    )


def test_ordinary_jsonld_write_cannot_own_relation_predicates() -> None:
    with pytest.raises(ValueError, match="immutable graph relation port"):
        reject_owned_relation_predicates(
            {"@id": "turn:1", rt.PRODUCED.name: [{"@id": "tool-result:1"}]}
        )
