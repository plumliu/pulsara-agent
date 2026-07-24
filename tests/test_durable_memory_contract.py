"""Postgres contract tests for durable memory submit_* flows."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest

from tests.support.postgres import connect_postgres_test_database as _connect_or_skip

from tests.support.postgres import verified_postgres_provider

from pulsara_agent.entities.memory import Decision
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.jsonld import NodeRef, utc_now
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.ontology import memory, runtime as rt
from pulsara_agent.settings import StorageConfig


@pytest.fixture
def graph_store() -> Iterator[tuple[object, str]]:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(connection_provider=verified_postgres_provider(dsn))
    try:
        yield store, graph_id
    finally:
        store.delete_graph(graph_id)


def _ledger(store: object, graph_id: str) -> ExecutionEvidenceLedger:
    return ExecutionEvidenceLedger(
        graph=store,
        archive=PostgresArtifactStore(
            connection_provider=verified_postgres_provider(
                StorageConfig.from_env().postgres_dsn
            )
        ),
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
        scope="ctx:workspace/test_project",
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
    assert [
        d["@id"] for d in store.find_by_type(memory.DECISION, graph_id=graph_id)
    ] == ["decision:single-edge"]


def test_submit_decision_routes_through_gate_and_links_evidence(graph_store) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)
    evidence_id = _seed_evidence(ledger, scope="ctx:workspace/test_project")
    based_on = ledger.submit_claim(
        statement="JSON-LD preserves graph semantics.",
        scope="ctx:workspace/test_project",
        evidence_ids=[evidence_id],
        source_authority=memory.SourceAuthority.TOOL_RESULT,
        verification_status=memory.VerificationStatus.TOOL_VERIFIED,
    )

    record = ledger.submit_decision(
        statement="Adopt JSON-LD for durable memory.",
        scope="ctx:workspace/test_project",
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
    evidence_id = _seed_evidence(ledger, scope="ctx:workspace/test_project")

    record = ledger.submit_decision(
        statement="Adopt JSON-LD for durable memory.",
        scope="ctx:workspace/test_project",
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
    evidence_id = _seed_evidence(ledger, scope="ctx:workspace/test_project")

    record = ledger.submit_observation(
        statement="The integration suite is flaky on macOS runners.",
        scope="ctx:workspace/test_project",
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


def test_submit_observation_with_missing_evidence_does_not_write_partial_node(
    graph_store,
) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)

    with pytest.raises(ValueError, match="missing evidence node"):
        ledger.submit_observation(
            statement="The integration suite is flaky on macOS runners.",
            scope="ctx:workspace/test_project",
            evidence_ids=["evidence:missing"],
            source_authority=memory.SourceAuthority.CONVERSATION_EVIDENCE,
            verification_status=memory.VerificationStatus.INFERRED,
        )

    assert store.find_by_type(memory.OBSERVATION, graph_id=graph_id) == []


def test_submit_decision_with_missing_based_on_does_not_write_partial_node(
    graph_store,
) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)
    evidence_id = _seed_evidence(ledger, scope="ctx:workspace/test_project")

    with pytest.raises(ValueError, match="missing basedOn node"):
        ledger.submit_decision(
            statement="Adopt JSON-LD for durable memory.",
            scope="ctx:workspace/test_project",
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


def test_submit_preference_links_single_evidence(graph_store) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)
    evidence_id = _seed_evidence(ledger, scope="ctx:user")

    record = ledger.submit_preference(
        statement="Prefer concise summaries.",
        scope="ctx:user",
        evidence_ids=[evidence_id],
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )

    assert record.status is memory.NodeStatus.ACTIVE
    doc = store.get_jsonld(record.memory_id, graph_id=graph_id)
    assert doc["@type"] == [memory.PREFERENCE.name]
    assert doc[memory.HAS_EVIDENCE.name] == [{"@id": evidence_id}]
    assert store.get_jsonld(evidence_id, graph_id=graph_id)[memory.SUPPORTS.name] == [
        {"@id": record.memory_id}
    ]


def test_submit_preference_with_missing_evidence_does_not_write_partial_node(
    graph_store,
) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)

    with pytest.raises(ValueError, match="missing evidence node"):
        ledger.submit_preference(
            statement="Prefer concise summaries.",
            scope="ctx:user",
            evidence_ids=["evidence:missing"],
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
        )

    assert store.find_by_type(memory.PREFERENCE, graph_id=graph_id) == []


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
        scope="ctx:workspace/test_workspace",
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


def test_submit_action_boundary_links_single_evidence(graph_store) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)
    evidence_id = _seed_evidence(ledger, scope="ctx:workspace/test_workspace")

    record = ledger.submit_action_boundary(
        statement="Never force-push to main.",
        scope="ctx:workspace/test_workspace",
        applies_when="branch is main",
        do_not_apply_when="user explicitly authorizes",
        evidence_ids=[evidence_id],
        source_authority=memory.SourceAuthority.SYSTEM_RULE,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )

    assert record.status is memory.NodeStatus.ACTIVE
    doc = store.get_jsonld(record.memory_id, graph_id=graph_id)
    assert doc["@type"] == [memory.ACTION_BOUNDARY.name]
    assert doc[memory.HAS_EVIDENCE.name] == [{"@id": evidence_id}]
    assert store.get_jsonld(evidence_id, graph_id=graph_id)[memory.SUPPORTS.name] == [
        {"@id": record.memory_id}
    ]


def test_submit_action_boundary_with_missing_evidence_does_not_write_partial_node(
    graph_store,
) -> None:
    store, graph_id = graph_store
    ledger = _ledger(store, graph_id)

    with pytest.raises(ValueError, match="missing evidence node"):
        ledger.submit_action_boundary(
            statement="Never force-push to main.",
            scope="ctx:workspace/test_workspace",
            applies_when="branch is main",
            do_not_apply_when="user explicitly authorizes",
            evidence_ids=["evidence:missing"],
            source_authority=memory.SourceAuthority.SYSTEM_RULE,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
        )

    assert store.find_by_type(memory.ACTION_BOUNDARY, graph_id=graph_id) == []
