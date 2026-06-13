"""Cross-store contract tests for durable memory submit_* flows.

Each test runs against both InMemoryGraphStore and (when available) a live
Oxigraph server, to guard against list/dict degradation of edge properties
on round-trip.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from uuid import uuid4

import pytest

from pulsara_agent.entities.memory import Decision
from pulsara_agent.graph import InMemoryGraphStore, OxigraphGraphStore
from pulsara_agent.jsonld import NodeRef, utc_now
from pulsara_agent.memory.archive import InMemoryArchiveStore
from pulsara_agent.memory.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.ontology import memory, runtime as rt


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


@pytest.fixture(
    params=[
        "in_memory",
        pytest.param(
            "oxigraph",
            marks=pytest.mark.skipif(
                not oxigraph_available(),
                reason="Oxigraph is not running at http://localhost:7878",
            ),
        ),
    ]
)
def graph_store(request) -> Iterator[tuple[object, str]]:
    graph_id = f"graph:test/{uuid4().hex}"
    if request.param == "in_memory":
        yield InMemoryGraphStore(), graph_id
        return
    store = OxigraphGraphStore(OXIGRAPH_URL)
    try:
        yield store, graph_id
    finally:
        store.delete_graph(graph_id)


def _ledger(store: object, graph_id: str) -> ExecutionEvidenceLedger:
    return ExecutionEvidenceLedger(
        graph=store,
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
        graph_id=graph_id,
    )


def _seed_evidence(ledger: ExecutionEvidenceLedger, *, scope: str) -> str:
    result = ledger.record_tool_result(
        turn_id=f"turn:{uuid4().hex}",
        tool_name="rg",
        status=rt.ToolExecutionStatus.SUCCESS,
        input_summary="search",
        output="match found",
        scope=scope,
    )
    evidence = ledger.create_evidence_from_tool_result(
        result.tool_result_id,
        statement="The search found a match.",
        scope=scope,
    )
    return evidence.evidence_id


def test_decision_single_element_edges_round_trip(graph_store) -> None:
    store, graph_id = graph_store
    decision = Decision(
        id="decision:single-edge",
        statement="Adopt JSON-LD for durable memory.",
        scope="ctx:project",
        status=memory.NodeStatus.ACTIVE,
        confidence_level=memory.ConfidenceLevel.VERIFIED,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        created_at=utc_now(),
        updated_at=utc_now(),
        gate_reason="accepted",
        evidence=(NodeRef("evidence:one"),),
        based_on=(NodeRef("claim:one"),),
    )
    store.put_jsonld(decision.to_jsonld(), graph_id=graph_id)

    doc = store.get_jsonld("decision:single-edge", graph_id=graph_id)

    assert doc["@type"] == [memory.DECISION.name]
    assert doc[memory.HAS_EVIDENCE.name] == [{"@id": "evidence:one"}]
    assert doc[memory.BASED_ON.name] == [{"@id": "claim:one"}]
    assert doc[memory.STATEMENT.name] == "Adopt JSON-LD for durable memory."
    assert [d["@id"] for d in store.find_by_type(memory.DECISION, graph_id=graph_id)] == [
        "decision:single-edge"
    ]


def test_submit_decision_routes_through_gate_and_links_evidence(graph_store) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)
    evidence_id = _seed_evidence(ledger, scope="ctx:project")
    based_on = ledger.submit_claim(
        statement="JSON-LD preserves graph semantics.",
        scope="ctx:project",
        evidence_ids=[evidence_id],
        source_authority=memory.SourceAuthority.TOOL_RESULT,
        verification_status=memory.VerificationStatus.TOOL_VERIFIED,
    )

    record = ledger.submit_decision(
        statement="Adopt JSON-LD for durable memory.",
        scope="ctx:project",
        evidence_ids=[evidence_id],
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        based_on_ids=[based_on.claim_id],
    )

    assert record.status is memory.NodeStatus.ACTIVE
    doc = store.get_jsonld(record.memory_id, graph_id=graph_id)
    assert doc[memory.HAS_EVIDENCE.name] == [{"@id": evidence_id}]
    assert doc[memory.BASED_ON.name] == [{"@id": based_on.claim_id}]
    supports = store.get_jsonld(evidence_id, graph_id=graph_id)[memory.SUPPORTS.name]
    assert {"@id": based_on.claim_id} in supports
    assert {"@id": record.memory_id} in supports


def test_submit_decision_without_authoritative_source_needs_review(graph_store) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)
    evidence_id = _seed_evidence(ledger, scope="ctx:project")

    record = ledger.submit_decision(
        statement="Adopt JSON-LD for durable memory.",
        scope="ctx:project",
        evidence_ids=[evidence_id],
        source_authority=memory.SourceAuthority.TOOL_RESULT,
        verification_status=memory.VerificationStatus.TOOL_VERIFIED,
    )

    assert record.status is memory.NodeStatus.NEEDS_REVIEW
    doc = store.get_jsonld(record.memory_id, graph_id=graph_id)
    assert doc[memory.STATUS.name] == memory.NodeStatus.NEEDS_REVIEW.value


def test_submit_observation_links_single_evidence(graph_store) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)
    evidence_id = _seed_evidence(ledger, scope="ctx:task")

    record = ledger.submit_observation(
        statement="The integration suite is flaky on macOS runners.",
        scope="ctx:task",
        evidence_ids=[evidence_id],
        source_authority=memory.SourceAuthority.CONVERSATION_EVIDENCE,
        verification_status=memory.VerificationStatus.INFERRED,
    )

    assert record.status is memory.NodeStatus.ACTIVE
    doc = store.get_jsonld(record.memory_id, graph_id=graph_id)
    assert doc["@type"] == [memory.OBSERVATION.name]
    assert doc[memory.HAS_EVIDENCE.name] == [{"@id": evidence_id}]
    assert store.get_jsonld(evidence_id, graph_id=graph_id)[memory.SUPPORTS.name] == [
        {"@id": record.memory_id}
    ]


def test_submit_observation_with_missing_evidence_does_not_write_partial_node(graph_store) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)

    with pytest.raises(ValueError, match="missing evidence node"):
        ledger.submit_observation(
            statement="The integration suite is flaky on macOS runners.",
            scope="ctx:task",
            evidence_ids=["evidence:missing"],
            source_authority=memory.SourceAuthority.CONVERSATION_EVIDENCE,
            verification_status=memory.VerificationStatus.INFERRED,
        )

    assert store.find_by_type(memory.OBSERVATION, graph_id=graph_id) == []


def test_submit_decision_with_missing_based_on_does_not_write_partial_node(graph_store) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)
    evidence_id = _seed_evidence(ledger, scope="ctx:project")

    with pytest.raises(ValueError, match="missing basedOn node"):
        ledger.submit_decision(
            statement="Adopt JSON-LD for durable memory.",
            scope="ctx:project",
            evidence_ids=[evidence_id],
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            based_on_ids=["claim:missing"],
        )

    assert store.find_by_type(memory.DECISION, graph_id=graph_id) == []


def test_submit_preference_persists_without_edges(graph_store) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)

    record = ledger.submit_preference(
        statement="Prefer tabs over spaces.",
        scope="ctx:user",
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )

    assert record.status is memory.NodeStatus.ACTIVE
    doc = store.get_jsonld(record.memory_id, graph_id=graph_id)
    assert doc["@type"] == [memory.PREFERENCE.name]
    assert doc[memory.STATEMENT.name] == "Prefer tabs over spaces."
    assert memory.HAS_EVIDENCE.name not in doc


def test_submit_preference_with_empty_scope_is_rejected(graph_store) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)

    record = ledger.submit_preference(
        statement="Prefer tabs over spaces.",
        scope="   ",
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )

    assert record.status is memory.NodeStatus.REJECTED
    doc = store.get_jsonld(record.memory_id, graph_id=graph_id)
    assert doc[memory.STATUS.name] == memory.NodeStatus.REJECTED.value


def test_submit_action_boundary_persists_conditions(graph_store) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)

    record = ledger.submit_action_boundary(
        statement="Never force-push to main.",
        scope="ctx:workspace",
        applies_when="branch is main",
        do_not_apply_when="user explicitly authorizes",
        source_authority=memory.SourceAuthority.SYSTEM_RULE,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )

    assert record.status is memory.NodeStatus.ACTIVE
    doc = store.get_jsonld(record.memory_id, graph_id=graph_id)
    assert doc["@type"] == [memory.ACTION_BOUNDARY.name]
    assert doc[memory.APPLIES_WHEN.name] == "branch is main"
    assert doc[memory.DO_NOT_APPLY_WHEN.name] == "user explicitly authorizes"
