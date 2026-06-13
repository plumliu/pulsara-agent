"""Pulsara durable semantic memory ontology."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pulsara_agent.jsonld import Namespace


MEMORY = Namespace("https://pulsara.dev/memory#")

CLAIM = MEMORY.term("Claim")
DECISION = MEMORY.term("Decision")
PREFERENCE = MEMORY.term("Preference")
ACTION_BOUNDARY = MEMORY.term("ActionBoundary")
OBSERVATION = MEMORY.term("Observation")

STATEMENT = MEMORY.term("statement")
SUMMARY = MEMORY.term("summary")
SCOPE = MEMORY.term("scope")
STATUS = MEMORY.term("status")
HAS_EVIDENCE = MEMORY.term("hasEvidence")
SUPPORTS = MEMORY.term("supports")
CONTRADICTS = MEMORY.term("contradicts")
SUPERSEDES = MEMORY.term("supersedes")
DERIVED_FROM = MEMORY.term("derivedFrom")
BASED_ON = MEMORY.term("basedOn")
CONFIDENCE_LEVEL = MEMORY.term("confidenceLevel")
VERIFICATION_STATUS = MEMORY.term("verificationStatus")
SOURCE_AUTHORITY = MEMORY.term("sourceAuthority")
GATE_REASON = MEMORY.term("gateReason")
APPLIES_WHEN = MEMORY.term("appliesWhen")
DO_NOT_APPLY_WHEN = MEMORY.term("doNotApplyWhen")
STALE_AFTER = MEMORY.term("staleAfter")
EXPIRES_AT = MEMORY.term("expiresAt")
CREATED_AT = MEMORY.term("createdAt")
UPDATED_AT = MEMORY.term("updatedAt")

CONTEXT: dict[str, Any] = {
    "mem": MEMORY.base,
    "ctx": "https://pulsara.dev/context#",
    "claim": "https://pulsara.dev/claim/",
    "decision": "https://pulsara.dev/decision/",
    CLAIM.name: CLAIM.value,
    DECISION.name: DECISION.value,
    PREFERENCE.name: PREFERENCE.value,
    ACTION_BOUNDARY.name: ACTION_BOUNDARY.value,
    OBSERVATION.name: OBSERVATION.value,
    STATEMENT.name: STATEMENT.value,
    SUMMARY.name: SUMMARY.value,
    SCOPE.name: SCOPE.value,
    STATUS.name: STATUS.value,
    HAS_EVIDENCE.name: {"@id": HAS_EVIDENCE.value, "@type": "@id"},
    SUPPORTS.name: {"@id": SUPPORTS.value, "@type": "@id"},
    CONTRADICTS.name: {"@id": CONTRADICTS.value, "@type": "@id"},
    SUPERSEDES.name: {"@id": SUPERSEDES.value, "@type": "@id"},
    DERIVED_FROM.name: {"@id": DERIVED_FROM.value, "@type": "@id"},
    BASED_ON.name: {"@id": BASED_ON.value, "@type": "@id"},
    CONFIDENCE_LEVEL.name: CONFIDENCE_LEVEL.value,
    VERIFICATION_STATUS.name: VERIFICATION_STATUS.value,
    SOURCE_AUTHORITY.name: SOURCE_AUTHORITY.value,
    GATE_REASON.name: GATE_REASON.value,
    APPLIES_WHEN.name: APPLIES_WHEN.value,
    DO_NOT_APPLY_WHEN.name: DO_NOT_APPLY_WHEN.value,
    STALE_AFTER.name: STALE_AFTER.value,
    EXPIRES_AT.name: EXPIRES_AT.value,
    CREATED_AT.name: CREATED_AT.value,
    UPDATED_AT.name: UPDATED_AT.value,
}


class NodeStatus(StrEnum):
    ACTIVE = "active"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
    STALE = "stale"
    SUPERSEDED = "superseded"
    CONTRADICTED = "contradicted"
    ARCHIVED = "archived"
    DELETED = "deleted"


class SourceAuthority(StrEnum):
    EXPLICIT_USER_INSTRUCTION = "explicit_user_instruction"
    TOOL_RESULT = "tool_result"
    DOCUMENT_SOURCE = "document_source"
    CONVERSATION_EVIDENCE = "conversation_evidence"
    MODEL_INFERENCE = "model_inference"
    SYSTEM_RULE = "system_rule"


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    INFERRED = "inferred"
    USER_CONFIRMED = "user_confirmed"
    TOOL_VERIFIED = "tool_verified"
    CONTRADICTED = "contradicted"
    STALE = "stale"


class ConfidenceLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERIFIED = "verified"
