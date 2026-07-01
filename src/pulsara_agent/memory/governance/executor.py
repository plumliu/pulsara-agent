"""Governance executor for memory candidate-pool decisions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pulsara_agent.event import AgentEvent, MemoryWriteFailedEvent, MemoryWriteResultEvent
from pulsara_agent.event.candidates import ValidCandidatePayload
from pulsara_agent.event_log import EventLog
from pulsara_agent.graph import GraphStore
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    CandidatePool,
    ContradictAndSubmitDecision,
    CorrectAndSubmitDecision,
    GovernanceDecision,
    MemoryGovernanceDecisionRecord,
    MergeAndSubmitDecision,
    NoWriteOutcome,
    PooledMemoryCandidate,
    SkipDecision,
    SubmitAsIsDecision,
    SupersedeAndSubmitDecision,
    WriteFailedOutcome,
    WriteSucceededOutcome,
    governance_batch_context,
    new_governance_batch_id,
)
from pulsara_agent.memory.governance.dedupe import already_exists
from pulsara_agent.memory.governance.relatedness import (
    RelatednessAvailability,
    RelatednessExecutionContext,
)
from pulsara_agent.memory.canonical.unit_of_work import GovernanceWriteUnitOfWork
from pulsara_agent.memory.canonical.mutation_outbox import (
    CanonicalMutationSurface,
    governed_memory_mutation_payload,
)
from pulsara_agent.memory.canonical.write_service import MemoryWriteOutcome, MemoryWriteService
from pulsara_agent.memory.scope import CTX_USER
from pulsara_agent.ontology import memory


_SUPERSEDABLE_TYPES: frozenset[str] = frozenset({"Preference"})
_MAX_SUPERSEDED_PER_DECISION = 1
_SUPERSEDE_DOWNGRADE_SENTINEL = "supersede_downgraded_to_coexist"
_CONTRADICTABLE_TYPES: frozenset[str] = frozenset({"Preference"})
_MAX_CONTRADICTED_PER_DECISION = 1
_CONTRADICTION_DOWNGRADE_SENTINEL = "contradiction_downgraded_to_coexist"


@dataclass(frozen=True, slots=True)
class MemoryGovernanceApplyResult:
    decision_record: MemoryGovernanceDecisionRecord
    events: list[AgentEvent]
    diagnostics: tuple[str, ...] = ()


@dataclass(slots=True)
class MemoryGovernanceExecutor:
    candidate_pool: CandidatePool
    memory_write_service: MemoryWriteService
    event_log: EventLog
    graph: GraphStore
    runtime_session_id: str
    memory_write_uow_factory: Callable[[], GovernanceWriteUnitOfWork]
    graph_id: str | None = None
    allowed_write_scopes: frozenset[str] = frozenset({CTX_USER})
    async_surfaces: tuple[str, ...] = (
        CanonicalMutationSurface.SEARCH_INDEX.value,
        CanonicalMutationSurface.OXIGRAPH.value,
    )

    def __post_init__(self) -> None:
        if self.memory_write_uow_factory is None:
            raise ValueError("memory_write_uow_factory is required; no storage fallback is allowed")

    def apply_decision(
        self,
        decision: GovernanceDecision,
        *,
        governance_batch_id: str | None = None,
        relatedness_context: RelatednessExecutionContext | None = None,
    ) -> MemoryGovernanceApplyResult:
        batch_id = governance_batch_id or new_governance_batch_id()
        self._validate_target_entries(decision)
        return self._apply_decision_with_uow(
            decision,
            governance_batch_id=batch_id,
            relatedness_context=relatedness_context,
        )

    def _apply_decision_with_uow(
        self,
        decision: GovernanceDecision,
        *,
        governance_batch_id: str,
        relatedness_context: RelatednessExecutionContext | None,
    ) -> MemoryGovernanceApplyResult:
        if isinstance(decision, SkipDecision):
            record = _decision_record(
                decision=decision,
                governance_batch_id=governance_batch_id,
                write_outcome=NoWriteOutcome(),
            )
            with self.memory_write_uow_factory() as uow:
                uow.decisions.append_decision(record)
            return MemoryGovernanceApplyResult(decision_record=record, events=[])

        candidate = self._candidate_for_decision(decision)
        if candidate is None:
            record = _decision_record(
                decision=_skip_for_invalid(decision),
                governance_batch_id=governance_batch_id,
                write_outcome=NoWriteOutcome(),
            )
            with self.memory_write_uow_factory() as uow:
                uow.decisions.append_decision(record)
            return MemoryGovernanceApplyResult(decision_record=record, events=[])
        if candidate.scope not in self.allowed_write_scopes:
            record = _decision_record(
                decision=_skip_out_of_scope(decision, candidate.scope),
                governance_batch_id=governance_batch_id,
                write_outcome=NoWriteOutcome(),
            )
            with self.memory_write_uow_factory() as uow:
                uow.decisions.append_decision(record)
            return MemoryGovernanceApplyResult(decision_record=record, events=[])

        with self.memory_write_uow_factory() as uow:
            if already_exists(candidate, uow.graph, graph_id=uow.resolved_graph_id):
                # Dedupe wins before supersede: exact duplicates are skipped and
                # never retire an old active memory.
                record = _decision_record(
                    decision=SkipDecision(
                        target_entry_ids=_target_entry_ids(decision),
                        reason="A canonical memory with the same type, scope, and statement already exists.",
                        skip_reason="duplicate_existing_memory",
                    ),
                    governance_batch_id=governance_batch_id,
                    write_outcome=NoWriteOutcome(),
                )
                uow.decisions.append_decision(record)
                return MemoryGovernanceApplyResult(decision_record=record, events=[])

            valid_old_ids: tuple[str, ...] = ()
            supersede_blocked_reason: str | None = None
            if isinstance(decision, SupersedeAndSubmitDecision):
                supersede_blocked_reason = self._relatedness_block_reason(
                    decision.target_entry_id,
                    decision.superseded_memory_ids,
                    governance_batch_id=governance_batch_id,
                    relatedness_context=relatedness_context,
                ) or self._replacement_evidence_block_reason(decision)
                if supersede_blocked_reason is None:
                    valid_old_ids, supersede_blocked_reason = self._validate_supersede_targets(
                        decision, uow
                    )
            valid_contradicted_ids: tuple[str, ...] = ()
            contradiction_blocked_reason: str | None = None
            if isinstance(decision, ContradictAndSubmitDecision):
                contradiction_blocked_reason = self._relatedness_block_reason(
                    decision.target_entry_id,
                    decision.contradicted_memory_ids,
                    governance_batch_id=governance_batch_id,
                    relatedness_context=relatedness_context,
                )
                if contradiction_blocked_reason is None:
                    valid_contradicted_ids, contradiction_blocked_reason = (
                        self._validate_contradiction_targets(decision, uow)
                    )

            context = governance_batch_context(governance_batch_id)
            outcome = uow.memory_write_service.submit(candidate, event_context=context)
            uow.ensure_event_context_rows(context)

            new_active_id = _active_memory_id(outcome)
            supersede_events: list[AgentEvent] = []
            did_supersede = bool(
                isinstance(decision, SupersedeAndSubmitDecision)
                and supersede_blocked_reason is None
                and valid_old_ids
                and new_active_id is not None
            )
            if did_supersede:
                for old_id in valid_old_ids:
                    supersede_events.extend(
                        uow.lifecycle.supersede(
                            old_id=old_id,
                            new_id=new_active_id,
                            governance_batch_id=governance_batch_id,
                            graph_id=uow.resolved_graph_id,
                        )
                    )
            contradiction_events: list[AgentEvent] = []
            did_contradict = bool(
                isinstance(decision, ContradictAndSubmitDecision)
                and contradiction_blocked_reason is None
                and valid_contradicted_ids
                and new_active_id is not None
            )
            if did_contradict:
                for old_id in valid_contradicted_ids:
                    contradiction_events.extend(
                        uow.lifecycle.link_contradiction(
                            left_id=old_id,
                            right_id=new_active_id,
                            governance_batch_id=governance_batch_id,
                            graph_id=uow.resolved_graph_id,
                        )
                    )

            if isinstance(decision, SupersedeAndSubmitDecision) and not did_supersede:
                effective_decision = _downgrade_to_coexist(
                    decision,
                    _lifecycle_downgrade_reason(
                        supersede_blocked_reason or "write_not_active"
                    ),
                )
            elif isinstance(decision, ContradictAndSubmitDecision) and not did_contradict:
                effective_decision = _downgrade_contradiction_to_coexist(
                    decision,
                    _lifecycle_downgrade_reason(
                        contradiction_blocked_reason or "write_not_active"
                    ),
                )
            else:
                effective_decision = decision
            recorded_superseded_ids = valid_old_ids if did_supersede else ()
            recorded_contradicted_ids = valid_contradicted_ids if did_contradict else ()

            is_supersede_origin = isinstance(decision, SupersedeAndSubmitDecision)
            is_contradiction_origin = isinstance(decision, ContradictAndSubmitDecision)
            if not (is_supersede_origin or is_contradiction_origin):
                governance_candidate = self._governance_candidate_for_decision(
                    effective_decision,
                    governance_batch_id=governance_batch_id,
                )
                if governance_candidate is not None:
                    uow.decisions.append_candidate(governance_candidate)
            record = _decision_record(
                decision=effective_decision,
                governance_batch_id=governance_batch_id,
                write_outcome=_write_outcome(
                    outcome,
                    outcome.events,
                    superseded_memory_ids=recorded_superseded_ids,
                    contradicted_memory_ids=recorded_contradicted_ids,
                ),
            )
            uow.decisions.append_decision(record)
            mutation_payload = governed_memory_mutation_payload(
                record=record,
                graph=uow.graph,
                graph_id=uow.resolved_graph_id,
                async_surfaces=self.async_surfaces,
            )
            uow.outbox.append_decision(
                record,
                graph_id=uow.resolved_graph_id,
                payload=mutation_payload.model_dump(mode="json") if mutation_payload is not None else None,
            )

        stored_events = self.event_log.extend(outcome.events + supersede_events + contradiction_events)
        diagnostics: list[str] = []
        blocked_reason = supersede_blocked_reason or contradiction_blocked_reason
        if blocked_reason is not None:
            diagnostics.append(blocked_reason)
            if _is_target_drift(blocked_reason):
                diagnostics.append("target_drift_requires_regovernance")
        return MemoryGovernanceApplyResult(
            decision_record=record,
            events=stored_events,
            diagnostics=tuple(diagnostics),
        )

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
        if isinstance(decision, SupersedeAndSubmitDecision):
            return decision.candidate
        if isinstance(decision, ContradictAndSubmitDecision):
            return decision.candidate
        raise TypeError(f"Unsupported governance decision: {decision!r}")

    def _governance_candidate_for_decision(
        self,
        decision: GovernanceDecision,
        *,
        governance_batch_id: str,
    ) -> PooledMemoryCandidate | None:
        if isinstance(decision, CorrectAndSubmitDecision):
            return self._governance_candidate(
                decision.candidate,
                decision.target_entry_id,
                governance_batch_id,
            )
        if isinstance(decision, MergeAndSubmitDecision):
            return self._governance_candidate(
                decision.candidate,
                decision.target_entry_ids[0],
                governance_batch_id,
            )
        return None

    def _governance_candidate(
        self,
        candidate,
        source_entry_id: str,
        governance_batch_id: str,
    ) -> PooledMemoryCandidate:
        ctx = governance_batch_context(governance_batch_id)
        return PooledMemoryCandidate(
            payload=ValidCandidatePayload(candidate=candidate),
            origin=CandidateOrigin.GOVERNANCE,
            source_session_id=self.runtime_session_id,
            source_run_id=ctx.run_id,
            source_turn_id=ctx.turn_id,
            source_reply_id=ctx.reply_id,
            user_quote=f"corrected_from:{source_entry_id}",
        )

    def _validate_target_entries(self, decision: GovernanceDecision) -> tuple[PooledMemoryCandidate, ...]:
        targets: list[PooledMemoryCandidate] = []
        for entry_id in _target_entry_ids(decision):
            target = self.candidate_pool.get_candidate(entry_id)
            if target.source_session_id != self.runtime_session_id:
                raise ValueError(f"governance decision targets candidate from another runtime: {entry_id}")
            targets.append(target)
        return tuple(targets)

    def _validate_supersede_targets(
        self,
        decision: SupersedeAndSubmitDecision,
        uow: GovernanceWriteUnitOfWork,
    ) -> tuple[tuple[str, ...], str | None]:
        candidate = decision.candidate
        if candidate.kind not in _SUPERSEDABLE_TYPES:
            return (), f"type_not_supersedable:{candidate.kind}"
        if not decision.superseded_memory_ids:
            return (), "missing_supersede_target"
        if len(decision.superseded_memory_ids) > _MAX_SUPERSEDED_PER_DECISION:
            return (), "too_many_supersede_targets"

        valid: list[str] = []
        for old_id in decision.superseded_memory_ids:
            try:
                old_doc = uow.graph.get_jsonld(old_id, graph_id=uow.resolved_graph_id)
            except KeyError:
                return (), f"supersede_target_missing:{old_id}"
            old_status = str(old_doc.get(memory.STATUS.name, ""))
            old_scope = str(old_doc.get(memory.SCOPE.name, ""))
            old_types = _jsonld_type_names(old_doc)
            if old_status != memory.NodeStatus.ACTIVE.value:
                return (), f"supersede_target_not_active:{old_id}:{old_status}"
            if old_scope != candidate.scope:
                return (), f"supersede_target_scope_mismatch:{old_id}"
            if not (old_types & _SUPERSEDABLE_TYPES):
                return (), f"supersede_target_type_not_supersedable:{old_id}:{sorted(old_types)}"
            valid.append(old_id)
        return tuple(valid), None

    def _relatedness_block_reason(
        self,
        entry_id: str,
        memory_ids: tuple[str, ...],
        *,
        governance_batch_id: str,
        relatedness_context: RelatednessExecutionContext | None,
    ) -> str | None:
        if relatedness_context is None:
            return "relatedness_context_missing"
        if relatedness_context.governance_batch_id != governance_batch_id:
            return "relatedness_context_batch_mismatch"
        availability = relatedness_context.availability.get(entry_id)
        if availability is not RelatednessAvailability.FULL:
            return f"relatedness_evidence_{availability.value if availability else 'unavailable'}"
        allowlist = relatedness_context.allowlists.get(entry_id, frozenset())
        for memory_id in memory_ids:
            if memory_id not in allowlist:
                return f"relatedness_target_not_surfaced:{memory_id}"
        return None

    def _replacement_evidence_block_reason(
        self,
        decision: SupersedeAndSubmitDecision,
    ) -> str | None:
        if not decision.replacement_evidence_refs:
            return "missing_replacement_evidence"
        pooled = self.candidate_pool.get_candidate(decision.target_entry_id)
        allowed: set[str] = set()
        if isinstance(pooled.payload, ValidCandidatePayload):
            allowed.update(pooled.payload.candidate.evidence_ids)
        allowed.update(event.id for event in self.event_log.iter(run_id=pooled.source_run_id))
        if pooled.user_quote:
            allowed.add("candidate_user_quote")
        for ref in decision.replacement_evidence_refs:
            if ref not in allowed:
                return f"replacement_evidence_not_in_source_context:{ref}"
        return None

    def _validate_contradiction_targets(
        self,
        decision: ContradictAndSubmitDecision,
        uow: GovernanceWriteUnitOfWork,
    ) -> tuple[tuple[str, ...], str | None]:
        candidate = decision.candidate
        if candidate.kind not in _CONTRADICTABLE_TYPES:
            return (), f"type_not_contradictable:{candidate.kind}"
        if not decision.contradicted_memory_ids:
            return (), "missing_contradiction_target"
        if len(decision.contradicted_memory_ids) > _MAX_CONTRADICTED_PER_DECISION:
            return (), "too_many_contradiction_targets"

        valid: list[str] = []
        for old_id in decision.contradicted_memory_ids:
            try:
                old_doc = uow.graph.get_jsonld(old_id, graph_id=uow.resolved_graph_id)
            except KeyError:
                return (), f"contradiction_target_missing:{old_id}"
            old_status = str(old_doc.get(memory.STATUS.name, ""))
            old_scope = str(old_doc.get(memory.SCOPE.name, ""))
            old_types = _jsonld_type_names(old_doc)
            if old_status != memory.NodeStatus.ACTIVE.value:
                return (), f"contradiction_target_not_active:{old_id}:{old_status}"
            if old_scope != candidate.scope:
                return (), f"contradiction_target_scope_mismatch:{old_id}"
            if not (old_types & _CONTRADICTABLE_TYPES):
                return (), f"contradiction_target_type_not_contradictable:{old_id}:{sorted(old_types)}"
            valid.append(old_id)
        return tuple(valid), None


def _write_outcome(
    outcome: MemoryWriteOutcome,
    events: list[AgentEvent],
    *,
    superseded_memory_ids: tuple[str, ...] = (),
    contradicted_memory_ids: tuple[str, ...] = (),
):
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
            superseded_memory_ids=superseded_memory_ids,
            contradicted_memory_ids=contradicted_memory_ids,
        )
    failed = next((event for event in events if isinstance(event, MemoryWriteFailedEvent)), None)
    if failed is not None:
        return WriteFailedOutcome(
            error_type=failed.error_type,
            message=failed.message,
            write_event_ids=event_ids,
        )
    raise ValueError(f"MemoryWriteOutcome produced no write result or failure event: {outcome!r}")


def _decision_record(
    *,
    governance_batch_id: str,
    decision: GovernanceDecision,
    write_outcome,
) -> MemoryGovernanceDecisionRecord:
    return MemoryGovernanceDecisionRecord(
        governance_batch_id=governance_batch_id,
        decision=decision,
        write_outcome=write_outcome,
    )


def _skip_for_invalid(decision: GovernanceDecision) -> SkipDecision:
    return SkipDecision(
        target_entry_ids=_target_entry_ids(decision),
        reason="Governance decision targets an invalid candidate payload.",
        skip_reason="invalid_attempt",
    )


def _skip_out_of_scope(decision: GovernanceDecision, scope: str) -> SkipDecision:
    return SkipDecision(
        target_entry_ids=_target_entry_ids(decision),
        reason=f"Memory candidate scope is not writable in this runtime: {scope}",
        skip_reason="scope_not_allowed",
    )


def _target_entry_ids(decision: GovernanceDecision) -> tuple[str, ...]:
    if isinstance(decision, SkipDecision | MergeAndSubmitDecision):
        return decision.target_entry_ids
    return (decision.target_entry_id,)


def _downgrade_to_coexist(decision: SupersedeAndSubmitDecision, reason: str) -> CorrectAndSubmitDecision:
    return CorrectAndSubmitDecision(
        target_entry_id=decision.target_entry_id,
        candidate=decision.candidate,
        reason=f"{_SUPERSEDE_DOWNGRADE_SENTINEL}: {reason}; original: {decision.reason}",
    )


def _downgrade_contradiction_to_coexist(
    decision: ContradictAndSubmitDecision,
    reason: str,
) -> CorrectAndSubmitDecision:
    return CorrectAndSubmitDecision(
        target_entry_id=decision.target_entry_id,
        candidate=decision.candidate,
        reason=f"{_CONTRADICTION_DOWNGRADE_SENTINEL}: {reason}; original: {decision.reason}",
    )


def _is_target_drift(reason: str) -> bool:
    return any(
        marker in reason
        for marker in (
            "target_missing",
            "target_not_active",
            "target_scope_mismatch",
            "target_type_not_",
        )
    )


def _lifecycle_downgrade_reason(reason: str) -> str:
    if _is_target_drift(reason):
        return f"target_drift_requires_regovernance:{reason}"
    return reason


def _active_memory_id(outcome: MemoryWriteOutcome) -> str | None:
    result = next((event for event in outcome.events if isinstance(event, MemoryWriteResultEvent)), None)
    if result is None:
        return None
    if result.status != memory.NodeStatus.ACTIVE:
        return None
    return result.memory_id


def _jsonld_type_names(document: Mapping[str, Any]) -> set[str]:
    raw = document.get("@type", ())
    values = raw if isinstance(raw, (list, tuple)) else (raw,)
    names: set[str] = set()
    for value in values:
        if not value:
            continue
        text = str(value)
        if "#" in text:
            text = text.rsplit("#", 1)[-1]
        elif "/" in text:
            text = text.rsplit("/", 1)[-1]
        names.add(text)
    return names
