"""Test factories for the required governance batch attribution contract."""

from __future__ import annotations

from collections.abc import Iterable

from pulsara_agent.event.candidates import ValidCandidatePayload
from pulsara_agent.memory.candidates.pool import (
    GovernanceDecision,
    GovernanceWriteOutcome,
    MemoryGovernanceDecisionRecord,
    PooledMemoryCandidate,
)
from pulsara_agent.memory.governance.executor import (
    GovernanceDecisionExecutionIdentity,
    governance_decision_identity,
)
from pulsara_agent.primitives.context import context_fingerprint


def make_test_governance_execution_identity(
    *,
    governance_batch_id: str,
    candidates: Iterable[PooledMemoryCandidate],
    decision_index: int = 0,
) -> GovernanceDecisionExecutionIdentity:
    ordered = tuple(candidates)
    scopes = frozenset(
        payload.candidate.scope
        for candidate in ordered
        if isinstance((payload := candidate.payload), ValidCandidatePayload)
    )
    if not scopes:
        scopes = frozenset({"ctx:user"})
    batch_input_fingerprint = context_fingerprint(
        "test-governance-batch-input:v1",
        (
            governance_batch_id,
            tuple(candidate.entry_id for candidate in ordered),
        ),
    )
    return GovernanceDecisionExecutionIdentity(
        batch_input_fingerprint=batch_input_fingerprint,
        batch_input_reference_fingerprint=context_fingerprint(
            "test-governance-batch-input-reference:v1",
            batch_input_fingerprint,
        ),
        governance_model_call_id=(
            "model_call:test-governance:"
            + batch_input_fingerprint.removeprefix("sha256:")[:24]
        ),
        decision_index=decision_index,
        allowed_candidate_entry_ids=frozenset(
            candidate.entry_id for candidate in ordered
        ),
        allowed_scopes=scopes,
    )


def make_test_governance_decision_record(
    *,
    governance_batch_id: str,
    decision: GovernanceDecision,
    write_outcome: GovernanceWriteOutcome,
    candidates: Iterable[PooledMemoryCandidate],
    decision_index: int = 0,
) -> MemoryGovernanceDecisionRecord:
    identity = make_test_governance_execution_identity(
        governance_batch_id=governance_batch_id,
        candidates=candidates,
        decision_index=decision_index,
    )
    decision_id, requested_fingerprint = governance_decision_identity(
        decision=decision,
        execution_identity=identity,
    )
    return MemoryGovernanceDecisionRecord(
        decision_id=decision_id,
        governance_batch_id=governance_batch_id,
        batch_input_fingerprint=identity.batch_input_fingerprint,
        batch_input_reference_fingerprint=(
            identity.batch_input_reference_fingerprint
        ),
        governance_model_call_id=identity.governance_model_call_id,
        decision_index=decision_index,
        requested_decision_payload_fingerprint=requested_fingerprint,
        decision_payload_fingerprint=context_fingerprint(
            "memory-governance-effective-decision-payload:v1",
            decision.model_dump(mode="json"),
        ),
        decision=decision,
        write_outcome=write_outcome,
    )


__all__ = [
    "make_test_governance_decision_record",
    "make_test_governance_execution_identity",
]
