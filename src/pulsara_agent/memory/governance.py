"""Governance executor for memory candidate-pool decisions."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.event import AgentEvent, MemoryWriteFailedEvent, MemoryWriteResultEvent
from pulsara_agent.event.candidates import ValidCandidatePayload
from pulsara_agent.event_log import EventLog
from pulsara_agent.graph import GraphStore
from pulsara_agent.memory.candidate_pool import (
    CandidateOrigin,
    CandidatePool,
    CorrectAndSubmitDecision,
    GovernanceDecision,
    MemoryGovernanceDecisionRecord,
    MergeAndSubmitDecision,
    NoWriteOutcome,
    PooledMemoryCandidate,
    SkipDecision,
    SubmitAsIsDecision,
    WriteFailedOutcome,
    WriteSucceededOutcome,
    governance_batch_context,
    new_governance_batch_id,
)
from pulsara_agent.memory.dedupe import already_exists
from pulsara_agent.memory.write_service import MemoryWriteOutcome, MemoryWriteService


@dataclass(frozen=True, slots=True)
class MemoryGovernanceApplyResult:
    decision_record: MemoryGovernanceDecisionRecord
    events: list[AgentEvent]


@dataclass(slots=True)
class MemoryGovernanceExecutor:
    candidate_pool: CandidatePool
    memory_write_service: MemoryWriteService
    event_log: EventLog
    graph: GraphStore
    runtime_session_id: str
    graph_id: str | None = None

    def apply_decision(
        self,
        decision: GovernanceDecision,
        *,
        governance_batch_id: str | None = None,
    ) -> MemoryGovernanceApplyResult:
        batch_id = governance_batch_id or new_governance_batch_id()
        self._validate_target_entries(decision)
        if isinstance(decision, SkipDecision):
            record = self._append_decision(
                decision=decision,
                governance_batch_id=batch_id,
                write_outcome=NoWriteOutcome(),
            )
            return MemoryGovernanceApplyResult(decision_record=record, events=[])

        candidate = self._candidate_for_decision(decision)
        if candidate is None:
            record = self._append_decision(
                decision=_skip_for_invalid(decision),
                governance_batch_id=batch_id,
                write_outcome=NoWriteOutcome(),
            )
            return MemoryGovernanceApplyResult(decision_record=record, events=[])

        if already_exists(candidate, self.graph, graph_id=self.graph_id):
            record = self._append_decision(
                decision=SkipDecision(
                    target_entry_ids=_target_entry_ids(decision),
                    reason="A canonical memory with the same type, scope, and statement already exists.",
                    skip_reason="duplicate_existing_memory",
                ),
                governance_batch_id=batch_id,
                write_outcome=NoWriteOutcome(),
            )
            return MemoryGovernanceApplyResult(decision_record=record, events=[])

        outcome = self.memory_write_service.submit(
            candidate,
            event_context=governance_batch_context(batch_id),
        )
        stored_events = self.event_log.extend(outcome.events)
        self._append_governance_candidate_if_needed(decision, governance_batch_id=batch_id)
        record = self._append_decision(
            decision=decision,
            governance_batch_id=batch_id,
            write_outcome=_write_outcome(outcome, stored_events),
        )
        return MemoryGovernanceApplyResult(decision_record=record, events=stored_events)

    def submit_pending_as_is(
        self,
        *,
        limit: int | None = None,
        governance_batch_id: str | None = None,
    ) -> list[MemoryGovernanceApplyResult]:
        batch_id = governance_batch_id or new_governance_batch_id()
        results: list[MemoryGovernanceApplyResult] = []
        for candidate in self.candidate_pool.list_pending():
            if candidate.source_session_id != self.runtime_session_id:
                continue
            if limit is not None and len(results) >= limit:
                break
            if isinstance(candidate.payload, ValidCandidatePayload):
                decision: GovernanceDecision = SubmitAsIsDecision(
                    target_entry_id=candidate.entry_id,
                    reason="Submit valid pending candidate as-is.",
                )
            else:
                decision = SkipDecision(
                    target_entry_ids=(candidate.entry_id,),
                    reason="Invalid memory tool attempt cannot be submitted as-is.",
                    skip_reason="invalid_attempt",
                )
            results.append(self.apply_decision(decision, governance_batch_id=batch_id))
        return results

    def _candidate_for_decision(self, decision: GovernanceDecision):
        if isinstance(decision, SubmitAsIsDecision):
            pooled = self.candidate_pool.get_candidate(decision.target_entry_id)
            if not isinstance(pooled.payload, ValidCandidatePayload):
                return None
            return pooled.payload.candidate
        if isinstance(decision, CorrectAndSubmitDecision):
            return decision.candidate
        if isinstance(decision, MergeAndSubmitDecision):
            return decision.candidate
        raise TypeError(f"Unsupported governance decision: {decision!r}")

    def _append_governance_candidate_if_needed(
        self,
        decision: GovernanceDecision,
        *,
        governance_batch_id: str,
    ) -> None:
        if isinstance(decision, CorrectAndSubmitDecision):
            self._append_governance_candidate(
                decision.candidate,
                decision.target_entry_id,
                governance_batch_id,
            )
        elif isinstance(decision, MergeAndSubmitDecision):
            self._append_governance_candidate(
                decision.candidate,
                decision.target_entry_ids[0],
                governance_batch_id,
            )

    def _append_governance_candidate(
        self,
        candidate,
        source_entry_id: str,
        governance_batch_id: str,
    ) -> None:
        ctx = governance_batch_context(governance_batch_id)
        self.candidate_pool.append_candidate(
            PooledMemoryCandidate(
                payload=ValidCandidatePayload(candidate=candidate),
                origin=CandidateOrigin.GOVERNANCE,
                source_session_id=self.runtime_session_id,
                source_run_id=ctx.run_id,
                source_turn_id=ctx.turn_id,
                source_reply_id=ctx.reply_id,
                user_quote=f"corrected_from:{source_entry_id}",
            )
        )

    def _append_decision(
        self,
        *,
        decision: GovernanceDecision,
        governance_batch_id: str,
        write_outcome,
    ) -> MemoryGovernanceDecisionRecord:
        return self.candidate_pool.append_decision(
            MemoryGovernanceDecisionRecord(
                governance_batch_id=governance_batch_id,
                decision=decision,
                write_outcome=write_outcome,
            )
        )

    def _validate_target_entries(self, decision: GovernanceDecision) -> None:
        for entry_id in _target_entry_ids(decision):
            self.candidate_pool.get_candidate(entry_id)


def _write_outcome(outcome: MemoryWriteOutcome, events: list[AgentEvent]):
    event_ids = tuple(event.id for event in events)
    result = next((event for event in events if isinstance(event, MemoryWriteResultEvent)), None)
    if result is not None:
        return WriteSucceededOutcome(
            memory_id=result.memory_id,
            memory_type=result.memory_type,
            node_status=result.status,
            confidence_level=result.confidence_level,
            verification_status=result.verification_status,
            gate_reason=result.gate_reason,
            write_event_ids=event_ids,
        )
    failed = next((event for event in events if isinstance(event, MemoryWriteFailedEvent)), None)
    if failed is not None:
        return WriteFailedOutcome(
            error_type=failed.error_type,
            message=failed.message,
            write_event_ids=event_ids,
        )
    raise ValueError(f"MemoryWriteOutcome produced no write result or failure event: {outcome!r}")


def _skip_for_invalid(decision: GovernanceDecision) -> SkipDecision:
    return SkipDecision(
        target_entry_ids=_target_entry_ids(decision),
        reason="Governance decision targets an invalid candidate payload.",
        skip_reason="invalid_attempt",
    )


def _target_entry_ids(decision: GovernanceDecision) -> tuple[str, ...]:
    if isinstance(decision, SkipDecision | MergeAndSubmitDecision):
        return decision.target_entry_ids
    return (decision.target_entry_id,)
