"""Pulsara memory ontology."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pulsara_agent.jsonld import Namespace


MEMORY = Namespace("https://pulsara.dev/memory#")

TURN = MEMORY.term("Turn")
TOOL_RESULT = MEMORY.term("ToolResult")
ARTIFACT = MEMORY.term("Artifact")
EVIDENCE = MEMORY.term("Evidence")
CLAIM = MEMORY.term("Claim")
DECISION = MEMORY.term("Decision")

PRODUCED = MEMORY.term("produced")
STORED_AS = MEMORY.term("storedAs")
PROVIDES = MEMORY.term("provides")
SUPPORTS = MEMORY.term("supports")
CONTRADICTS = MEMORY.term("contradicts")
SUPERSEDES = MEMORY.term("supersedes")
BASED_ON = MEMORY.term("basedOn")
CREATED_FROM = MEMORY.term("createdFrom")

CONFIDENCE_LEVEL = MEMORY.term("confidenceLevel")
CREATED_AT = MEMORY.term("createdAt")
GATE_REASON = MEMORY.term("gateReason")
HASH = MEMORY.term("hash")
HAS_EVIDENCE = MEMORY.term("hasEvidence")
INPUT_SUMMARY = MEMORY.term("inputSummary")
OBSERVED_AT = MEMORY.term("observedAt")
OUTPUT_SUMMARY = MEMORY.term("outputSummary")
SCOPE = MEMORY.term("scope")
SOURCE_AUTHORITY = MEMORY.term("sourceAuthority")
SOURCE_TYPE = MEMORY.term("sourceType")
STATEMENT = MEMORY.term("statement")
STATUS = MEMORY.term("status")
STORED_AT = MEMORY.term("storedAt")
SUMMARY = MEMORY.term("summary")
TOOL_NAME = MEMORY.term("toolName")
TRUNCATED = MEMORY.term("truncated")
UPDATED_AT = MEMORY.term("updatedAt")
VERIFICATION_STATUS = MEMORY.term("verificationStatus")

CONTEXT: dict[str, Any] = {
    "mem": MEMORY.base,
    "ctx": "https://pulsara.dev/context#",
    "turn": "https://pulsara.dev/turn/",
    "tool-result": "https://pulsara.dev/tool-result/",
    "artifact": "https://pulsara.dev/artifact/",
    "evidence": "https://pulsara.dev/evidence/",
    "claim": "https://pulsara.dev/claim/",
    "decision": "https://pulsara.dev/decision/",
    TURN.name: TURN.value,
    TOOL_RESULT.name: TOOL_RESULT.value,
    ARTIFACT.name: ARTIFACT.value,
    EVIDENCE.name: EVIDENCE.value,
    CLAIM.name: CLAIM.value,
    DECISION.name: DECISION.value,
    PRODUCED.name: {"@id": PRODUCED.value, "@type": "@id"},
    STORED_AS.name: {"@id": STORED_AS.value, "@type": "@id"},
    PROVIDES.name: {"@id": PROVIDES.value, "@type": "@id"},
    SUPPORTS.name: {"@id": SUPPORTS.value, "@type": "@id"},
    CONTRADICTS.name: {"@id": CONTRADICTS.value, "@type": "@id"},
    SUPERSEDES.name: {"@id": SUPERSEDES.value, "@type": "@id"},
    BASED_ON.name: {"@id": BASED_ON.value, "@type": "@id"},
    CREATED_FROM.name: {"@id": CREATED_FROM.value, "@type": "@id"},
    CONFIDENCE_LEVEL.name: CONFIDENCE_LEVEL.value,
    CREATED_AT.name: CREATED_AT.value,
    GATE_REASON.name: GATE_REASON.value,
    HASH.name: HASH.value,
    HAS_EVIDENCE.name: {"@id": HAS_EVIDENCE.value, "@type": "@id"},
    INPUT_SUMMARY.name: INPUT_SUMMARY.value,
    OBSERVED_AT.name: OBSERVED_AT.value,
    OUTPUT_SUMMARY.name: OUTPUT_SUMMARY.value,
    SCOPE.name: SCOPE.value,
    SOURCE_AUTHORITY.name: SOURCE_AUTHORITY.value,
    SOURCE_TYPE.name: SOURCE_TYPE.value,
    STATEMENT.name: STATEMENT.value,
    STATUS.name: STATUS.value,
    STORED_AT.name: STORED_AT.value,
    SUMMARY.name: SUMMARY.value,
    TOOL_NAME.name: TOOL_NAME.value,
    TRUNCATED.name: TRUNCATED.value,
    UPDATED_AT.name: UPDATED_AT.value,
    VERIFICATION_STATUS.name: VERIFICATION_STATUS.value,
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


class EvidenceSourceType(StrEnum):
    TOOL_RESULT = "tool_result"


class ToolExecutionStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
