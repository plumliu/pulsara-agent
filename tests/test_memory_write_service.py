"""Unit tests for MemoryWriteService candidate dispatch + event production.

These are pure unit tests against InMemoryGraphStore. Cross-store edge round-trip
is covered by test_durable_memory_contract.py; here we assert the service's
contract: a landed node -> [proposed, result] with a memory_id; a write failure
-> [proposed, failed] with no node and no memory_id.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from pulsara_agent.event import (
    EventContext,
    EventType,
    MemoryCandidateProposedEvent,
    MemoryWriteFailedEvent,
)
from pulsara_agent.event.candidates import (
    ActionBoundaryCandidate,
    ClaimCandidate,
    DecisionCandidate,
    ObservationCandidate,
    PreferenceCandidate,
)
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.memory.archive import InMemoryArchiveStore
from pulsara_agent.memory.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.memory.write_service import MemoryWriteService
from pulsara_agent.ontology import memory, runtime as rt


CTX = EventContext(run_id="run:test", turn_id="turn:test", reply_id="reply:test")


def _service() -> tuple[MemoryWriteService, ExecutionEvidenceLedger, InMemoryGraphStore]:
    graph = InMemoryGraphStore()
    ledger = ExecutionEvidenceLedger(
        graph=graph,
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
    )
    return MemoryWriteService(ledger=ledger), ledger, graph


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


def test_submit_claim_candidate_active_emits_proposed_and_result() -> None:
    service, ledger, graph = _service()
    evidence_id = _seed_evidence(ledger, scope="ctx:project")
    candidate = ClaimCandidate(
        candidate_id="candidate:claim",
        statement="JSON-LD preserves graph semantics.",
        scope="ctx:project",
        evidence_ids=(evidence_id,),
        source_authority=memory.SourceAuthority.TOOL_RESULT,
        verification_status=memory.VerificationStatus.TOOL_VERIFIED,
    )

    outcome = service.submit(candidate, event_context=CTX)

    assert outcome.record is not None
    assert outcome.record.status is memory.NodeStatus.ACTIVE
    assert [event.type for event in outcome.events] == [
        EventType.MEMORY_CANDIDATE_PROPOSED,
        EventType.MEMORY_WRITE_RESULT,
    ]
    proposed, result = outcome.events
    assert isinstance(proposed, MemoryCandidateProposedEvent)
    assert proposed.candidate.candidate_id == "candidate:claim"
    assert result.candidate_id == "candidate:claim"
    assert result.memory_id == outcome.record.claim_id
    assert result.memory_type == "Claim"
    assert result.status is memory.NodeStatus.ACTIVE
    assert result.gate_reason == "accepted"
    assert graph.has_jsonld(result.memory_id)


def test_submit_decision_candidate_without_authority_lands_needs_review() -> None:
    service, ledger, _ = _service()
    evidence_id = _seed_evidence(ledger, scope="ctx:project")
    candidate = DecisionCandidate(
        candidate_id="candidate:decision",
        statement="Adopt JSON-LD for durable memory.",
        scope="ctx:project",
        evidence_ids=(evidence_id,),
        source_authority=memory.SourceAuthority.TOOL_RESULT,
        verification_status=memory.VerificationStatus.TOOL_VERIFIED,
    )

    outcome = service.submit(candidate, event_context=CTX)

    assert outcome.record is not None
    result = outcome.events[1]
    assert result.type is EventType.MEMORY_WRITE_RESULT
    assert result.status is memory.NodeStatus.NEEDS_REVIEW
    assert result.memory_id == outcome.record.memory_id
    assert result.memory_type == "Decision"


def test_submit_preference_candidate_empty_scope_lands_rejected_with_memory_id() -> None:
    service, _, graph = _service()
    candidate = PreferenceCandidate(
        candidate_id="candidate:pref",
        statement="Prefer tabs over spaces.",
        scope="   ",
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )

    outcome = service.submit(candidate, event_context=CTX)

    assert outcome.record is not None
    result = outcome.events[1]
    assert result.type is EventType.MEMORY_WRITE_RESULT
    assert result.status is memory.NodeStatus.REJECTED
    assert result.memory_id == outcome.record.memory_id
    assert graph.has_jsonld(result.memory_id)


def test_submit_observation_with_missing_evidence_emits_failed_and_writes_no_node() -> None:
    service, _, graph = _service()
    candidate = ObservationCandidate(
        candidate_id="candidate:obs",
        statement="The integration suite is flaky on macOS runners.",
        scope="ctx:task",
        evidence_ids=("evidence:missing",),
        source_authority=memory.SourceAuthority.CONVERSATION_EVIDENCE,
        verification_status=memory.VerificationStatus.INFERRED,
    )

    outcome = service.submit(candidate, event_context=CTX)

    assert outcome.record is None
    assert [event.type for event in outcome.events] == [
        EventType.MEMORY_CANDIDATE_PROPOSED,
        EventType.MEMORY_WRITE_FAILED,
    ]
    failed = outcome.events[1]
    assert failed.candidate_id == "candidate:obs"
    assert failed.memory_type == "Observation"
    assert failed.error_type == "ValueError"
    assert "missing evidence node" in failed.message
    assert graph.find_by_type(memory.OBSERVATION) == []


def test_submit_decision_with_missing_based_on_emits_failed() -> None:
    service, ledger, graph = _service()
    evidence_id = _seed_evidence(ledger, scope="ctx:project")
    candidate = DecisionCandidate(
        candidate_id="candidate:decision",
        statement="Adopt JSON-LD for durable memory.",
        scope="ctx:project",
        evidence_ids=(evidence_id,),
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        based_on_ids=("claim:missing",),
    )

    outcome = service.submit(candidate, event_context=CTX)

    assert outcome.record is None
    failed = outcome.events[1]
    assert failed.type is EventType.MEMORY_WRITE_FAILED
    assert "missing basedOn node" in failed.message
    assert graph.find_by_type(memory.DECISION) == []


def test_action_boundary_candidate_requires_conditions_at_construction() -> None:
    with pytest.raises(ValidationError):
        ActionBoundaryCandidate(
            candidate_id="candidate:boundary",
            statement="Never force-push to main.",
            scope="ctx:workspace",
            source_authority=memory.SourceAuthority.SYSTEM_RULE,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
        )


def test_action_boundary_structured_trigger_values_must_be_non_empty() -> None:
    service, _, _ = _service()
    candidate = ActionBoundaryCandidate(
        candidate_id="candidate:boundary",
        statement="Use uv when running project tests.",
        scope="ctx:project",
        applies_when="working on this repository",
        do_not_apply_when="the user asks not to run tests",
        trigger_keywords=("pytest", "  "),
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )

    outcome = service.submit(candidate, event_context=CTX)

    assert outcome.record is not None
    assert outcome.record.status is memory.NodeStatus.REJECTED
    result = outcome.events[1]
    assert result.status is memory.NodeStatus.REJECTED
    assert "trigger_keywords" in result.gate_reason


def test_preference_candidate_forbids_action_boundary_fields() -> None:
    with pytest.raises(ValidationError):
        PreferenceCandidate.model_validate(
            {
                "kind": "Preference",
                "candidate_id": "candidate:pref",
                "statement": "Prefer concise summaries.",
                "scope": "ctx:user",
                "applies_when": "reviewing code",
                "do_not_apply_when": "user asks for detail",
                "source_authority": memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION.value,
                "verification_status": memory.VerificationStatus.USER_CONFIRMED.value,
            }
        )


def test_proposed_event_rejects_wrong_kind_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryCandidateProposedEvent.model_validate(
            {
                **CTX.event_fields(),
                "type": EventType.MEMORY_CANDIDATE_PROPOSED.value,
                "candidate": {
                    "kind": "Preference",
                    "candidate_id": "candidate:pref",
                    "statement": "Prefer concise summaries.",
                    "scope": "ctx:user",
                    "applies_when": "reviewing code",
                    "do_not_apply_when": "user asks for detail",
                    "source_authority": memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION.value,
                    "verification_status": memory.VerificationStatus.USER_CONFIRMED.value,
                },
            }
        )


def test_proposed_event_round_trips_candidate_discriminator() -> None:
    event = MemoryCandidateProposedEvent(
        **CTX.event_fields(),
        candidate=ActionBoundaryCandidate(
            candidate_id="candidate:boundary",
            statement="Never force-push to main.",
            scope="ctx:workspace",
            applies_when="branch is main",
            do_not_apply_when="user explicitly authorizes",
            source_authority=memory.SourceAuthority.SYSTEM_RULE,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
        ),
    )

    restored = MemoryCandidateProposedEvent.model_validate(event.model_dump())

    assert isinstance(restored.candidate, ActionBoundaryCandidate)
    assert restored.candidate.applies_when == "branch is main"
    assert restored.candidate.do_not_apply_when == "user explicitly authorizes"


def test_submit_raw_action_boundary_payload_normalizes_candidate() -> None:
    service, _, graph = _service()

    outcome = service.submit(
        {
            "kind": "ActionBoundary",
            "candidate_id": "candidate:boundary",
            "statement": "Never force-push to main.",
            "scope": "ctx:workspace",
            "applies_when": "branch is main",
            "do_not_apply_when": "user explicitly authorizes",
            "source_authority": memory.SourceAuthority.SYSTEM_RULE.value,
            "verification_status": memory.VerificationStatus.USER_CONFIRMED.value,
        },
        event_context=CTX,
    )

    assert outcome.record is not None
    assert [event.type for event in outcome.events] == [
        EventType.MEMORY_CANDIDATE_PROPOSED,
        EventType.MEMORY_WRITE_RESULT,
    ]
    proposed, result = outcome.events
    assert isinstance(proposed, MemoryCandidateProposedEvent)
    assert isinstance(proposed.candidate, ActionBoundaryCandidate)
    assert result.memory_type == "ActionBoundary"
    assert graph.has_jsonld(result.memory_id)


def test_submit_raw_preference_payload_with_extra_field_fails_without_proposal() -> None:
    service, _, graph = _service()

    outcome = service.submit(
        {
            "kind": "Preference",
            "candidate_id": "candidate:pref",
            "statement": "Prefer concise summaries.",
            "scope": "ctx:user",
            "applies_when": "reviewing code",
            "source_authority": memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION.value,
            "verification_status": memory.VerificationStatus.USER_CONFIRMED.value,
        },
        event_context=CTX,
    )

    assert outcome.record is None
    assert len(outcome.events) == 1
    failed = outcome.events[0]
    assert isinstance(failed, MemoryWriteFailedEvent)
    assert failed.candidate_id == "candidate:pref"
    assert failed.memory_type == "Preference"
    assert failed.error_type == "ValidationError"
    assert "Extra inputs are not permitted" in failed.message
    assert graph.find_by_type(memory.PREFERENCE) == []


def test_submit_preference_with_evidence_links_provenance() -> None:
    service, ledger, graph = _service()
    evidence_id = _seed_evidence(ledger, scope="ctx:user")
    candidate = PreferenceCandidate(
        candidate_id="candidate:pref",
        statement="Prefer concise summaries.",
        scope="ctx:user",
        evidence_ids=(evidence_id,),
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )

    outcome = service.submit(candidate, event_context=CTX)

    assert outcome.record is not None
    doc = graph.get_jsonld(outcome.record.memory_id)
    assert doc[memory.HAS_EVIDENCE.name] == [{"@id": evidence_id}]
    assert graph.get_jsonld(evidence_id)[memory.SUPPORTS.name] == [
        {"@id": outcome.record.memory_id}
    ]


def test_submit_action_boundary_with_evidence_links_provenance() -> None:
    service, ledger, graph = _service()
    evidence_id = _seed_evidence(ledger, scope="ctx:workspace")
    candidate = ActionBoundaryCandidate(
        candidate_id="candidate:boundary",
        statement="Never force-push to main.",
        scope="ctx:workspace",
        evidence_ids=(evidence_id,),
        applies_when="branch is main",
        do_not_apply_when="user explicitly authorizes",
        source_authority=memory.SourceAuthority.SYSTEM_RULE,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )

    outcome = service.submit(candidate, event_context=CTX)

    assert outcome.record is not None
    doc = graph.get_jsonld(outcome.record.memory_id)
    assert doc[memory.HAS_EVIDENCE.name] == [{"@id": evidence_id}]
    assert graph.get_jsonld(evidence_id)[memory.SUPPORTS.name] == [
        {"@id": outcome.record.memory_id}
    ]


@pytest.mark.parametrize(
    "candidate,expected_type",
    [
        (
            PreferenceCandidate(
                candidate_id="candidate:pref",
                statement="Prefer concise summaries.",
                scope="ctx:user",
                evidence_ids=("evidence:missing",),
                source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                verification_status=memory.VerificationStatus.USER_CONFIRMED,
            ),
            memory.PREFERENCE,
        ),
        (
            ActionBoundaryCandidate(
                candidate_id="candidate:boundary",
                statement="Never force-push to main.",
                scope="ctx:workspace",
                evidence_ids=("evidence:missing",),
                applies_when="branch is main",
                do_not_apply_when="user explicitly authorizes",
                source_authority=memory.SourceAuthority.SYSTEM_RULE,
                verification_status=memory.VerificationStatus.USER_CONFIRMED,
            ),
            memory.ACTION_BOUNDARY,
        ),
    ],
)
def test_submit_candidate_with_missing_evidence_fails_without_partial_node(
    candidate: PreferenceCandidate | ActionBoundaryCandidate,
    expected_type,
) -> None:
    service, _, graph = _service()

    outcome = service.submit(candidate, event_context=CTX)

    assert outcome.record is None
    assert [event.type for event in outcome.events] == [
        EventType.MEMORY_CANDIDATE_PROPOSED,
        EventType.MEMORY_WRITE_FAILED,
    ]
    failed = outcome.events[1]
    assert failed.candidate_id == candidate.candidate_id
    assert failed.memory_type == candidate.kind
    assert "missing evidence node" in failed.message
    assert graph.find_by_type(expected_type) == []
