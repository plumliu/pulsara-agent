"""MemoryWriteGate for conclusion nodes."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    MemoryCandidateProposedEvent,
    MemoryWriteAcceptedEvent,
    MemoryWriteRejectedEvent,
)
from pulsara_agent.ontology import memory


@dataclass(frozen=True, slots=True)
class WriteDecision:
    accepted: bool
    status: memory.NodeStatus
    reason: str
    confidence_level: memory.ConfidenceLevel = memory.ConfidenceLevel.LOW


class MemoryWriteGate:
    """Conservative gate for claims and decisions.

    Runtime provenance can be appended directly. Claim/Decision nodes must pass
    this gate before becoming active.
    """

    def evaluate_claim(
        self,
        *,
        statement: str,
        scope: str,
        evidence_ids: list[str],
        source_authority: memory.SourceAuthority,
        verification_status: memory.VerificationStatus,
    ) -> WriteDecision:
        _assert_enum(source_authority, memory.SourceAuthority)
        _assert_enum(verification_status, memory.VerificationStatus)
        if not statement.strip():
            return WriteDecision(False, memory.NodeStatus.REJECTED, "empty statement")
        if not scope.strip():
            return WriteDecision(False, memory.NodeStatus.REJECTED, "claim needs scope")
        if not evidence_ids and source_authority is not memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION:
            return WriteDecision(False, memory.NodeStatus.NEEDS_REVIEW, "claim needs evidence")
        confidence = _confidence_for(source_authority, verification_status)
        return WriteDecision(True, memory.NodeStatus.ACTIVE, "accepted", confidence)

    def evaluate_preference(
        self,
        *,
        statement: str,
        scope: str,
        source_authority: memory.SourceAuthority,
        verification_status: memory.VerificationStatus,
    ) -> WriteDecision:
        _assert_enum(source_authority, memory.SourceAuthority)
        _assert_enum(verification_status, memory.VerificationStatus)
        if not statement.strip():
            return WriteDecision(False, memory.NodeStatus.REJECTED, "empty statement")
        if not scope.strip():
            return WriteDecision(False, memory.NodeStatus.REJECTED, "preference needs scope")
        if source_authority is memory.SourceAuthority.MODEL_INFERENCE:
            return WriteDecision(False, memory.NodeStatus.NEEDS_REVIEW, "preference needs user or tool authority")
        confidence = _confidence_for(source_authority, verification_status)
        return WriteDecision(True, memory.NodeStatus.ACTIVE, "accepted", confidence)

    def evaluate_action_boundary(
        self,
        *,
        statement: str,
        scope: str,
        applies_when: str,
        do_not_apply_when: str,
        source_authority: memory.SourceAuthority,
        verification_status: memory.VerificationStatus,
    ) -> WriteDecision:
        _assert_enum(source_authority, memory.SourceAuthority)
        _assert_enum(verification_status, memory.VerificationStatus)
        if not statement.strip():
            return WriteDecision(False, memory.NodeStatus.REJECTED, "empty statement")
        if not scope.strip():
            return WriteDecision(False, memory.NodeStatus.REJECTED, "action boundary needs scope")
        if not applies_when.strip() or not do_not_apply_when.strip():
            return WriteDecision(
                False, memory.NodeStatus.NEEDS_REVIEW, "action boundary needs appliesWhen and doNotApplyWhen"
            )
        if source_authority not in {
            memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            memory.SourceAuthority.SYSTEM_RULE,
        }:
            return WriteDecision(False, memory.NodeStatus.NEEDS_REVIEW, "action boundary needs authoritative source")
        confidence = _confidence_for(source_authority, verification_status)
        return WriteDecision(True, memory.NodeStatus.ACTIVE, "accepted", confidence)

    def evaluate_observation(
        self,
        *,
        statement: str,
        scope: str,
        evidence_ids: list[str],
        source_authority: memory.SourceAuthority,
        verification_status: memory.VerificationStatus,
    ) -> WriteDecision:
        _assert_enum(source_authority, memory.SourceAuthority)
        _assert_enum(verification_status, memory.VerificationStatus)
        if not statement.strip():
            return WriteDecision(False, memory.NodeStatus.REJECTED, "empty statement")
        if not scope.strip():
            return WriteDecision(False, memory.NodeStatus.REJECTED, "observation needs scope")
        if not evidence_ids and source_authority not in {
            memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            memory.SourceAuthority.SYSTEM_RULE,
        }:
            return WriteDecision(False, memory.NodeStatus.NEEDS_REVIEW, "observation needs evidence")
        confidence = _confidence_for(source_authority, verification_status)
        return WriteDecision(True, memory.NodeStatus.ACTIVE, "accepted", confidence)

    def evaluate_decision(
        self,
        *,
        statement: str,
        scope: str,
        evidence_ids: list[str],
        source_authority: memory.SourceAuthority,
        verification_status: memory.VerificationStatus,
    ) -> WriteDecision:
        _assert_enum(source_authority, memory.SourceAuthority)
        _assert_enum(verification_status, memory.VerificationStatus)
        if not statement.strip():
            return WriteDecision(False, memory.NodeStatus.REJECTED, "empty statement")
        if not scope.strip():
            return WriteDecision(False, memory.NodeStatus.REJECTED, "decision needs scope")
        if not evidence_ids and source_authority is not memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION:
            return WriteDecision(False, memory.NodeStatus.NEEDS_REVIEW, "decision needs evidence")
        if source_authority not in {
            memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            memory.SourceAuthority.SYSTEM_RULE,
        }:
            return WriteDecision(False, memory.NodeStatus.NEEDS_REVIEW, "decision needs authoritative source")
        confidence = _confidence_for(source_authority, verification_status)
        return WriteDecision(True, memory.NodeStatus.ACTIVE, "accepted", confidence)

    def evaluate_claim_with_events(
        self,
        *,
        event_context: EventContext,
        candidate_id: str,
        memory_id: str,
        statement: str,
        scope: str,
        evidence_ids: list[str],
        source_authority: memory.SourceAuthority,
        verification_status: memory.VerificationStatus,
        memory_type: str = "Claim",
    ) -> tuple[WriteDecision, list[AgentEvent]]:
        decision = self.evaluate_claim(
            statement=statement,
            scope=scope,
            evidence_ids=evidence_ids,
            source_authority=source_authority,
            verification_status=verification_status,
        )
        events: list[AgentEvent] = [
            MemoryCandidateProposedEvent(
                **event_context.event_fields(),
                candidate_id=candidate_id,
                scope=scope,
                memory_type=memory_type,
                statement=statement,
                evidence_ids=evidence_ids,
                source_authority=source_authority,
                verification_status=verification_status,
            )
        ]
        event_cls = MemoryWriteAcceptedEvent if decision.accepted else MemoryWriteRejectedEvent
        kwargs = {
            **event_context.event_fields(),
            "scope": scope,
            "memory_type": memory_type,
            "statement": statement,
            "evidence_ids": evidence_ids,
            "source_authority": source_authority,
            "verification_status": verification_status,
            "gate_reason": decision.reason,
        }
        if decision.accepted:
            events.append(event_cls(**kwargs, memory_id=memory_id))
        else:
            events.append(event_cls(**kwargs, candidate_id=candidate_id))
        return decision, events


def _confidence_for(
    source_authority: memory.SourceAuthority,
    verification_status: memory.VerificationStatus,
) -> memory.ConfidenceLevel:
    if source_authority is memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION:
        return memory.ConfidenceLevel.VERIFIED
    if verification_status is memory.VerificationStatus.TOOL_VERIFIED:
        return memory.ConfidenceLevel.HIGH
    if source_authority in {memory.SourceAuthority.TOOL_RESULT, memory.SourceAuthority.DOCUMENT_SOURCE}:
        return memory.ConfidenceLevel.HIGH
    if source_authority is memory.SourceAuthority.CONVERSATION_EVIDENCE:
        return memory.ConfidenceLevel.MEDIUM
    return memory.ConfidenceLevel.LOW


def _assert_enum(value: object, enum_type: type) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"Expected {enum_type.__name__}, got {type(value).__name__}")
