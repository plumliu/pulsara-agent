from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.entities.memory import ActionBoundary, Observation, Preference
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.jsonld import NodeRef, utc_now
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.ontology import memory
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


def test_preference_round_trips(graph_store) -> None:
    graph, graph_id = graph_store
    preference = Preference(
        id="preference:tabs",
        statement="Prefer tabs over spaces.",
        scope="ctx:user",
        status=memory.NodeStatus.ACTIVE,
        confidence_level=memory.ConfidenceLevel.VERIFIED,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        created_at=utc_now(),
        updated_at=utc_now(),
        gate_reason="accepted",
    )
    graph.put_jsonld(preference.to_jsonld(), graph_id=graph_id)

    doc = graph.get_jsonld("preference:tabs", graph_id=graph_id)
    assert doc == preference.to_jsonld()
    assert doc["@type"] == [memory.PREFERENCE.name]
    assert doc[memory.STATEMENT.name] == "Prefer tabs over spaces."
    assert [d["@id"] for d in graph.find_by_type(memory.PREFERENCE, graph_id=graph_id)] == ["preference:tabs"]


def test_action_boundary_round_trips_with_conditions(graph_store) -> None:
    graph, graph_id = graph_store
    boundary = ActionBoundary(
        id="action-boundary:no-force-push",
        statement="Never force-push to main.",
        scope="ctx:workspace/test_workspace",
        status=memory.NodeStatus.ACTIVE,
        applies_when="branch is main",
        do_not_apply_when="user explicitly authorizes",
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        confidence_level=memory.ConfidenceLevel.VERIFIED,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        created_at=utc_now(),
        updated_at=utc_now(),
        gate_reason="accepted",
    )
    graph.put_jsonld(boundary.to_jsonld(), graph_id=graph_id)

    doc = graph.get_jsonld("action-boundary:no-force-push", graph_id=graph_id)
    assert doc == boundary.to_jsonld()
    assert doc["@type"] == [memory.ACTION_BOUNDARY.name]
    assert doc[memory.APPLIES_WHEN.name] == "branch is main"
    assert doc[memory.DO_NOT_APPLY_WHEN.name] == "user explicitly authorizes"


def test_observation_round_trips(graph_store) -> None:
    graph, graph_id = graph_store
    observation = Observation(
        id="observation:ci-flaky",
        statement="The integration suite is flaky on macOS runners.",
        scope="ctx:workspace/test_project",
        status=memory.NodeStatus.ACTIVE,
        confidence_level=memory.ConfidenceLevel.MEDIUM,
        verification_status=memory.VerificationStatus.INFERRED,
        source_authority=memory.SourceAuthority.CONVERSATION_EVIDENCE,
        created_at=utc_now(),
        updated_at=utc_now(),
        gate_reason="accepted",
        evidence=(NodeRef("evidence:ci-flaky"),),
    )
    graph.put_jsonld(observation.to_jsonld(), graph_id=graph_id)

    doc = graph.get_jsonld("observation:ci-flaky", graph_id=graph_id)
    assert doc == observation.to_jsonld()
    assert doc["@type"] == [memory.OBSERVATION.name]
    assert doc[memory.HAS_EVIDENCE.name] == [{"@id": "evidence:ci-flaky"}]


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def test_preference_gate_rejects_empty_statement() -> None:
    decision = MemoryWriteGate().evaluate_preference(
        statement="   ",
        scope="ctx:user",
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.REJECTED


def test_preference_gate_needs_review_for_model_inference() -> None:
    decision = MemoryWriteGate().evaluate_preference(
        statement="Prefer dark mode.",
        scope="ctx:user",
        source_authority=memory.SourceAuthority.MODEL_INFERENCE,
        verification_status=memory.VerificationStatus.INFERRED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.NEEDS_REVIEW


def test_preference_gate_accepts_user_instruction() -> None:
    decision = MemoryWriteGate().evaluate_preference(
        statement="Prefer tabs over spaces.",
        scope="ctx:user",
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )
    assert decision.accepted
    assert decision.status is memory.NodeStatus.ACTIVE
    assert decision.confidence_level is memory.ConfidenceLevel.VERIFIED


def test_preference_gate_rejects_empty_scope() -> None:
    decision = MemoryWriteGate().evaluate_preference(
        statement="Prefer tabs over spaces.",
        scope="   ",
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.REJECTED


def test_preference_gate_rejects_unknown_or_hierarchical_scope() -> None:
    gate = MemoryWriteGate()

    for scope in ("ctx:乱填", "ctx:project", "ctx:workspace/a/b", "ctx:workspace"):
        decision = gate.evaluate_preference(
            statement="Prefer tabs over spaces.",
            scope=scope,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
        )

        assert not decision.accepted
        assert decision.status is memory.NodeStatus.REJECTED


def test_action_boundary_gate_requires_both_conditions() -> None:
    decision = MemoryWriteGate().evaluate_action_boundary(
        statement="Never force-push to main.",
        scope="ctx:workspace/test_workspace",
        applies_when="branch is main",
        do_not_apply_when="   ",
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.NEEDS_REVIEW


def test_action_boundary_gate_requires_authoritative_source() -> None:
    decision = MemoryWriteGate().evaluate_action_boundary(
        statement="Never force-push to main.",
        scope="ctx:workspace/test_workspace",
        applies_when="branch is main",
        do_not_apply_when="user explicitly authorizes",
        source_authority=memory.SourceAuthority.MODEL_INFERENCE,
        verification_status=memory.VerificationStatus.INFERRED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.NEEDS_REVIEW


def test_action_boundary_gate_accepts_complete_boundary() -> None:
    decision = MemoryWriteGate().evaluate_action_boundary(
        statement="Never force-push to main.",
        scope="ctx:workspace/test_workspace",
        applies_when="branch is main",
        do_not_apply_when="user explicitly authorizes",
        source_authority=memory.SourceAuthority.SYSTEM_RULE,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )
    assert decision.accepted
    assert decision.status is memory.NodeStatus.ACTIVE


def test_action_boundary_gate_rejects_empty_scope() -> None:
    decision = MemoryWriteGate().evaluate_action_boundary(
        statement="Never force-push to main.",
        scope="   ",
        applies_when="branch is main",
        do_not_apply_when="user explicitly authorizes",
        source_authority=memory.SourceAuthority.SYSTEM_RULE,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.REJECTED


def test_observation_gate_requires_evidence_for_conversation_evidence() -> None:
    decision = MemoryWriteGate().evaluate_observation(
        statement="The integration suite is flaky on macOS runners.",
        scope="ctx:workspace/test_project",
        evidence_ids=[],
        source_authority=memory.SourceAuthority.CONVERSATION_EVIDENCE,
        verification_status=memory.VerificationStatus.INFERRED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.NEEDS_REVIEW


def test_observation_gate_accepts_evidence_backed_observation() -> None:
    decision = MemoryWriteGate().evaluate_observation(
        statement="The integration suite is flaky on macOS runners.",
        scope="ctx:workspace/test_project",
        evidence_ids=["evidence:ci-flaky"],
        source_authority=memory.SourceAuthority.CONVERSATION_EVIDENCE,
        verification_status=memory.VerificationStatus.INFERRED,
    )
    assert decision.accepted
    assert decision.status is memory.NodeStatus.ACTIVE
    assert decision.confidence_level is memory.ConfidenceLevel.MEDIUM


def test_observation_gate_rejects_empty_statement() -> None:
    decision = MemoryWriteGate().evaluate_observation(
        statement="",
        scope="ctx:workspace/test_project",
        evidence_ids=["evidence:ci-flaky"],
        source_authority=memory.SourceAuthority.CONVERSATION_EVIDENCE,
        verification_status=memory.VerificationStatus.INFERRED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.REJECTED


def test_observation_gate_rejects_empty_scope() -> None:
    decision = MemoryWriteGate().evaluate_observation(
        statement="The integration suite is flaky on macOS runners.",
        scope="   ",
        evidence_ids=["evidence:ci-flaky"],
        source_authority=memory.SourceAuthority.CONVERSATION_EVIDENCE,
        verification_status=memory.VerificationStatus.INFERRED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.REJECTED


def test_decision_gate_rejects_empty_statement() -> None:
    decision = MemoryWriteGate().evaluate_decision(
        statement="   ",
        scope="ctx:workspace/test_project",
        evidence_ids=["evidence:x"],
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.REJECTED


def test_decision_gate_rejects_empty_scope() -> None:
    decision = MemoryWriteGate().evaluate_decision(
        statement="Adopt JSON-LD for memory.",
        scope="   ",
        evidence_ids=["evidence:x"],
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.REJECTED


def test_decision_gate_needs_review_without_evidence() -> None:
    decision = MemoryWriteGate().evaluate_decision(
        statement="Adopt JSON-LD for memory.",
        scope="ctx:workspace/test_project",
        evidence_ids=[],
        source_authority=memory.SourceAuthority.TOOL_RESULT,
        verification_status=memory.VerificationStatus.TOOL_VERIFIED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.NEEDS_REVIEW


def test_decision_gate_needs_review_without_authoritative_source() -> None:
    decision = MemoryWriteGate().evaluate_decision(
        statement="Adopt JSON-LD for memory.",
        scope="ctx:workspace/test_project",
        evidence_ids=["evidence:x"],
        source_authority=memory.SourceAuthority.TOOL_RESULT,
        verification_status=memory.VerificationStatus.TOOL_VERIFIED,
    )
    assert not decision.accepted
    assert decision.status is memory.NodeStatus.NEEDS_REVIEW


def test_decision_gate_accepts_user_instruction() -> None:
    decision = MemoryWriteGate().evaluate_decision(
        statement="Adopt JSON-LD for memory.",
        scope="ctx:workspace/test_project",
        evidence_ids=[],
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )
    assert decision.accepted
    assert decision.status is memory.NodeStatus.ACTIVE
    assert decision.confidence_level is memory.ConfidenceLevel.VERIFIED


def test_decision_gate_accepts_system_rule_with_evidence() -> None:
    decision = MemoryWriteGate().evaluate_decision(
        statement="Adopt JSON-LD for memory.",
        scope="ctx:workspace/test_project",
        evidence_ids=["evidence:x"],
        source_authority=memory.SourceAuthority.SYSTEM_RULE,
        verification_status=memory.VerificationStatus.TOOL_VERIFIED,
    )
    assert decision.accepted
    assert decision.status is memory.NodeStatus.ACTIVE


def test_decision_gate_is_not_looser_than_action_boundary() -> None:
    gate = MemoryWriteGate()
    boundary = gate.evaluate_action_boundary(
        statement="Never auto-merge.",
        scope="ctx:workspace/test_project",
        applies_when="any branch",
        do_not_apply_when="user authorizes",
        source_authority=memory.SourceAuthority.MODEL_INFERENCE,
        verification_status=memory.VerificationStatus.INFERRED,
    )
    decision = gate.evaluate_decision(
        statement="Never auto-merge.",
        scope="ctx:workspace/test_project",
        evidence_ids=["evidence:x"],
        source_authority=memory.SourceAuthority.MODEL_INFERENCE,
        verification_status=memory.VerificationStatus.INFERRED,
    )
    assert not boundary.accepted
    assert not decision.accepted
