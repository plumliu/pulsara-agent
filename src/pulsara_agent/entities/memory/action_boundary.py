"""ActionBoundary entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.jsonld import JsonLdEntity, NodeRef, Term
from pulsara_agent.ontology import memory
from pulsara_agent.ontology.registry import CORE_CONTEXT


@dataclass(frozen=True, slots=True)
class ActionBoundary(JsonLdEntity):
    CONTEXT: ClassVar[dict[str, Any]] = CORE_CONTEXT
    TYPE: ClassVar[Term] = memory.ACTION_BOUNDARY

    statement: str
    scope: str
    status: memory.NodeStatus
    applies_when: str
    do_not_apply_when: str
    source_authority: memory.SourceAuthority
    confidence_level: memory.ConfidenceLevel
    verification_status: memory.VerificationStatus
    created_at: str
    updated_at: str
    gate_reason: str
    evidence: tuple[NodeRef, ...] = ()
    trigger_tools: tuple[str, ...] = ()
    trigger_actions: tuple[str, ...] = ()
    trigger_file_globs: tuple[str, ...] = ()
    trigger_scopes: tuple[str, ...] = ()
    trigger_keywords: tuple[str, ...] = ()
    negative_tools: tuple[str, ...] = ()
    negative_actions: tuple[str, ...] = ()
    negative_file_globs: tuple[str, ...] = ()

    def properties(self) -> dict[Any, Any]:
        values: dict[Any, Any] = {
            memory.STATEMENT: self.statement,
            memory.SCOPE: self.scope,
            memory.STATUS: self.status,
            memory.APPLIES_WHEN: self.applies_when,
            memory.DO_NOT_APPLY_WHEN: self.do_not_apply_when,
            memory.SOURCE_AUTHORITY: self.source_authority,
            memory.CONFIDENCE_LEVEL: self.confidence_level,
            memory.VERIFICATION_STATUS: self.verification_status,
            memory.CREATED_AT: self.created_at,
            memory.UPDATED_AT: self.updated_at,
            memory.GATE_REASON: self.gate_reason,
        }
        if self.evidence:
            values[memory.HAS_EVIDENCE] = list(self.evidence)
        if self.trigger_tools:
            values[memory.TRIGGER_TOOLS] = list(self.trigger_tools)
        if self.trigger_actions:
            values[memory.TRIGGER_ACTIONS] = list(self.trigger_actions)
        if self.trigger_file_globs:
            values[memory.TRIGGER_FILE_GLOBS] = list(self.trigger_file_globs)
        if self.trigger_scopes:
            values[memory.TRIGGER_SCOPES] = list(self.trigger_scopes)
        if self.trigger_keywords:
            values[memory.TRIGGER_KEYWORDS] = list(self.trigger_keywords)
        if self.negative_tools:
            values[memory.NEGATIVE_TOOLS] = list(self.negative_tools)
        if self.negative_actions:
            values[memory.NEGATIVE_ACTIONS] = list(self.negative_actions)
        if self.negative_file_globs:
            values[memory.NEGATIVE_FILE_GLOBS] = list(self.negative_file_globs)
        return values
