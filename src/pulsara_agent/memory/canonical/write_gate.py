"""MemoryWriteGate for conclusion nodes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pulsara_agent.memory.scope import is_valid_scope
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
        if not is_valid_scope(scope):
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
        if not is_valid_scope(scope):
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
        trigger_tools: Sequence[object] = (),
        trigger_actions: Sequence[object] = (),
        trigger_file_globs: Sequence[object] = (),
        trigger_scopes: Sequence[object] = (),
        trigger_keywords: Sequence[object] = (),
        negative_tools: Sequence[object] = (),
        negative_actions: Sequence[object] = (),
        negative_file_globs: Sequence[object] = (),
        source_authority: memory.SourceAuthority,
        verification_status: memory.VerificationStatus,
    ) -> WriteDecision:
        _assert_enum(source_authority, memory.SourceAuthority)
        _assert_enum(verification_status, memory.VerificationStatus)
        if not statement.strip():
            return WriteDecision(False, memory.NodeStatus.REJECTED, "empty statement")
        if not is_valid_scope(scope):
            return WriteDecision(False, memory.NodeStatus.REJECTED, "action boundary needs scope")
        if not applies_when.strip() or not do_not_apply_when.strip():
            return WriteDecision(
                False, memory.NodeStatus.NEEDS_REVIEW, "action boundary needs appliesWhen and doNotApplyWhen"
            )
        invalid_structured = _invalid_structured_trigger_fields(
            {
                "trigger_tools": trigger_tools,
                "trigger_actions": trigger_actions,
                "trigger_file_globs": trigger_file_globs,
                "trigger_scopes": trigger_scopes,
                "trigger_keywords": trigger_keywords,
                "negative_tools": negative_tools,
                "negative_actions": negative_actions,
                "negative_file_globs": negative_file_globs,
            }
        )
        if invalid_structured:
            return WriteDecision(
                False,
                memory.NodeStatus.REJECTED,
                f"action boundary structured trigger values must be non-empty strings: {', '.join(invalid_structured)}",
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
        if not is_valid_scope(scope):
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
        if not is_valid_scope(scope):
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


def _invalid_structured_trigger_fields(fields: dict[str, Sequence[object]]) -> list[str]:
    invalid: list[str] = []
    for name, values in fields.items():
        if any(not isinstance(value, str) or not value.strip() for value in values):
            invalid.append(name)
    return invalid
