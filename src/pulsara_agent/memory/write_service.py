"""Runtime-agnostic durable memory write service.

Wraps ``ExecutionEvidenceLedger.submit_*`` behind a single path: a typed
``MemoryCandidate`` in, a gate-evaluated ledger record plus the events to emit
out. The service stays pure -- it writes the node (the gate runs once inside the
ledger) and *returns* events; it never emits. The producer emits them at an
agent-loop-safe point, which keeps the service unit-testable and avoids
re-entrant publish in the runtime drain loop.

Boundary: a ledger record means the node landed in the graph (ACTIVE /
NEEDS_REVIEW / REJECTED all carry a ``memory_id``) -> ``MemoryWriteResultEvent``.
A raised exception means the write itself failed (missing reference, store
error) with no reliable ``memory_id`` -> ``MemoryWriteFailedEvent``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter, ValidationError

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    MemoryCandidate,
    MemoryCandidateProposedEvent,
    MemoryWriteFailedEvent,
    MemoryWriteResultEvent,
)
from pulsara_agent.event.candidates import (
    ActionBoundaryCandidate,
    ClaimCandidate,
    DecisionCandidate,
    ObservationCandidate,
    PreferenceCandidate,
)
from pulsara_agent.memory.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.records import ClaimRecord, MemoryWriteRecord


_CANDIDATE_ADAPTER = TypeAdapter(MemoryCandidate)


@dataclass(frozen=True, slots=True)
class MemoryWriteOutcome:
    record: ClaimRecord | MemoryWriteRecord | None
    events: list[AgentEvent]


@dataclass(frozen=True, slots=True)
class MemoryWriteService:
    ledger: ExecutionEvidenceLedger

    def submit(
        self,
        candidate: MemoryCandidate | Mapping[str, Any],
        *,
        event_context: EventContext,
    ) -> MemoryWriteOutcome:
        try:
            normalized = _CANDIDATE_ADAPTER.validate_python(candidate)
        except ValidationError as exc:
            failed = MemoryWriteFailedEvent(
                **event_context.event_fields(),
                candidate_id=_candidate_field(candidate, "candidate_id"),
                memory_type=_candidate_field(candidate, "kind"),
                error_type=type(exc).__name__,
                message=str(exc),
            )
            return MemoryWriteOutcome(record=None, events=[failed])

        proposed = MemoryCandidateProposedEvent(
            **event_context.event_fields(),
            candidate=normalized,
        )
        try:
            record = self._dispatch(normalized)
        except Exception as exc:
            failed = MemoryWriteFailedEvent(
                **event_context.event_fields(),
                candidate_id=normalized.candidate_id,
                memory_type=normalized.kind,
                error_type=type(exc).__name__,
                message=str(exc),
            )
            return MemoryWriteOutcome(record=None, events=[proposed, failed])
        result = MemoryWriteResultEvent(
            **event_context.event_fields(),
            candidate_id=normalized.candidate_id,
            memory_id=_memory_id(record),
            memory_type=normalized.kind,
            status=record.status,
            confidence_level=record.confidence_level,
            verification_status=record.verification_status,
            gate_reason=record.gate_reason,
        )
        return MemoryWriteOutcome(record=record, events=[proposed, result])

    def _dispatch(self, candidate: MemoryCandidate) -> ClaimRecord | MemoryWriteRecord:
        match candidate:
            case ClaimCandidate():
                return self.ledger.submit_claim(
                    statement=candidate.statement,
                    scope=candidate.scope,
                    evidence_ids=list(candidate.evidence_ids),
                    source_authority=candidate.source_authority,
                    verification_status=candidate.verification_status,
                )
            case PreferenceCandidate():
                return self.ledger.submit_preference(
                    statement=candidate.statement,
                    scope=candidate.scope,
                    evidence_ids=list(candidate.evidence_ids),
                    source_authority=candidate.source_authority,
                    verification_status=candidate.verification_status,
                )
            case ObservationCandidate():
                return self.ledger.submit_observation(
                    statement=candidate.statement,
                    scope=candidate.scope,
                    evidence_ids=list(candidate.evidence_ids),
                    source_authority=candidate.source_authority,
                    verification_status=candidate.verification_status,
                )
            case ActionBoundaryCandidate():
                return self.ledger.submit_action_boundary(
                    statement=candidate.statement,
                    scope=candidate.scope,
                    applies_when=candidate.applies_when,
                    do_not_apply_when=candidate.do_not_apply_when,
                    trigger_tools=list(candidate.trigger_tools),
                    trigger_actions=list(candidate.trigger_actions),
                    trigger_file_globs=list(candidate.trigger_file_globs),
                    trigger_scopes=list(candidate.trigger_scopes),
                    trigger_keywords=list(candidate.trigger_keywords),
                    negative_tools=list(candidate.negative_tools),
                    negative_actions=list(candidate.negative_actions),
                    negative_file_globs=list(candidate.negative_file_globs),
                    evidence_ids=list(candidate.evidence_ids),
                    source_authority=candidate.source_authority,
                    verification_status=candidate.verification_status,
                )
            case DecisionCandidate():
                return self.ledger.submit_decision(
                    statement=candidate.statement,
                    scope=candidate.scope,
                    evidence_ids=list(candidate.evidence_ids),
                    source_authority=candidate.source_authority,
                    verification_status=candidate.verification_status,
                    based_on_ids=list(candidate.based_on_ids),
                )
        raise TypeError(f"Unknown memory candidate kind: {candidate!r}")


def _memory_id(record: ClaimRecord | MemoryWriteRecord) -> str:
    if isinstance(record, ClaimRecord):
        return record.claim_id
    return record.memory_id


def _candidate_field(candidate: MemoryCandidate | Mapping[str, Any], name: str) -> str | None:
    if isinstance(candidate, Mapping):
        value = candidate.get(name)
    else:
        value = getattr(candidate, name, None)
    if isinstance(value, str):
        return value
    return None
