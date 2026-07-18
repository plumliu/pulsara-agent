"""Governance executor for memory candidate-pool decisions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from time import monotonic
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
from pulsara_agent.memory.governance.event_outbox import (
    GovernanceEventDispatchTicket,
    GovernanceEventOutboxDispatcher,
)
from pulsara_agent.memory.canonical.mutation_outbox import (
    CanonicalMutationSurface,
    governed_memory_mutation_payload,
)
from pulsara_agent.memory.canonical.write_service import MemoryWriteOutcome, MemoryWriteService
from pulsara_agent.memory.scope import CTX_USER
from pulsara_agent.ontology import memory
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    GovernanceDerivedWriteAttributionFact,
)


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


@dataclass(frozen=True, slots=True)
class GovernanceDecisionExecutionIdentity:
    batch_input_fingerprint: str
    batch_input_reference_fingerprint: str
    governance_model_call_id: str
    decision_index: int
    allowed_candidate_entry_ids: frozenset[str]
    allowed_scopes: frozenset[str]

    def __post_init__(self) -> None:
        if (
            not self.batch_input_fingerprint
            or not self.batch_input_reference_fingerprint
            or not self.governance_model_call_id
            or not 0 <= self.decision_index <= 31
            or not self.allowed_candidate_entry_ids
            or not self.allowed_scopes
        ):
            raise ValueError("invalid governance decision execution identity")


@dataclass(slots=True)
class MemoryGovernanceExecutor:
    candidate_pool: CandidatePool
    memory_write_service: MemoryWriteService
    event_log: EventLog
    event_commit_port: Callable[[Sequence[AgentEvent]], Sequence[AgentEvent]]
    graph: GraphStore
    runtime_session_id: str
    memory_write_uow_factory: Callable[[], GovernanceWriteUnitOfWork]
    graph_id: str | None = None
    allowed_write_scopes: frozenset[str] = frozenset({CTX_USER})
    async_surfaces: tuple[str, ...] = (
        CanonicalMutationSurface.SEARCH_INDEX.value,
        CanonicalMutationSurface.OXIGRAPH.value,
    )
    event_outbox_dispatcher: GovernanceEventOutboxDispatcher | None = None
    async_operation_port: Callable[
        [str, Callable[[], Any], float], Awaitable[Any]
    ] | None = None
    _event_dispatch_retry_required: bool = field(
        default=False,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.memory_write_uow_factory is None:
            raise ValueError("memory_write_uow_factory is required; no storage fallback is allowed")
        if self.event_commit_port is None:
            raise ValueError(
                "event_commit_port is required; governance cannot append EventLog directly"
            )

    def apply_decision(
        self,
        decision: GovernanceDecision,
        *,
        governance_batch_id: str | None = None,
        relatedness_context: RelatednessExecutionContext | None = None,
        execution_identity: GovernanceDecisionExecutionIdentity,
    ) -> MemoryGovernanceApplyResult:
        batch_id = governance_batch_id or new_governance_batch_id()
        target_entries = self._validate_target_entries(decision)
        target_ids = frozenset(item.entry_id for item in target_entries)
        if not target_ids.issubset(execution_identity.allowed_candidate_entry_ids):
            raise ValueError("governance decision targets candidates outside its batch")
        if not execution_identity.allowed_scopes.issubset(
            self.allowed_write_scopes
        ):
            raise ValueError("governance decision input scopes exceed runtime policy")
        return self._apply_decision_with_uow(
            decision,
            governance_batch_id=batch_id,
            relatedness_context=relatedness_context,
            target_entries=target_entries,
            execution_identity=execution_identity,
        )

    async def apply_decision_async(
        self,
        decision: GovernanceDecision,
        *,
        governance_batch_id: str | None = None,
        relatedness_context: RelatednessExecutionContext | None = None,
        execution_identity: GovernanceDecisionExecutionIdentity,
    ) -> MemoryGovernanceApplyResult:
        operation = partial(
            self.apply_decision,
            decision,
            governance_batch_id=governance_batch_id,
            relatedness_context=relatedness_context,
            execution_identity=execution_identity,
        )
        return await self._run_auxiliary_operation(
            "memory-governance-apply",
            operation,
        )

    async def flush_pending_event_outbox_async(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[AgentEvent, ...]:
        return await self._run_auxiliary_operation(
            "memory-governance-event-outbox-dispatch",
            self.flush_pending_event_outbox,
            deadline_monotonic=deadline_monotonic,
        )

    def flush_pending_event_outbox(self) -> tuple[AgentEvent, ...]:
        dispatcher = self.event_outbox_dispatcher
        if dispatcher is None:
            self._event_dispatch_retry_required = False
            return ()
        try:
            committed = dispatcher.dispatch_pending()
        except BaseException:
            self._event_dispatch_retry_required = True
            raise
        self._event_dispatch_retry_required = dispatcher.has_pending()
        return committed

    @property
    def event_dispatch_retry_required(self) -> bool:
        return self._event_dispatch_retry_required

    def _apply_decision_with_uow(
        self,
        decision: GovernanceDecision,
        *,
        governance_batch_id: str,
        relatedness_context: RelatednessExecutionContext | None,
        target_entries: tuple[PooledMemoryCandidate, ...],
        execution_identity: GovernanceDecisionExecutionIdentity,
    ) -> MemoryGovernanceApplyResult:
        if isinstance(decision, SkipDecision):
            record = _decision_record(
                decision=decision,
                requested_decision=decision,
                governance_batch_id=governance_batch_id,
                write_outcome=NoWriteOutcome(),
                execution_identity=execution_identity,
            )
            with self.memory_write_uow_factory() as uow:
                uow.decisions.append_decision(record)
            return MemoryGovernanceApplyResult(decision_record=record, events=[])

        candidate = self._candidate_for_decision(decision)
        if candidate is None:
            record = _decision_record(
                decision=_skip_for_invalid(decision),
                requested_decision=decision,
                governance_batch_id=governance_batch_id,
                write_outcome=NoWriteOutcome(),
                execution_identity=execution_identity,
            )
            with self.memory_write_uow_factory() as uow:
                uow.decisions.append_decision(record)
            return MemoryGovernanceApplyResult(decision_record=record, events=[])
        if (
            candidate.scope not in self.allowed_write_scopes
            or candidate.scope not in execution_identity.allowed_scopes
        ):
            record = _decision_record(
                decision=_skip_out_of_scope(decision, candidate.scope),
                requested_decision=decision,
                governance_batch_id=governance_batch_id,
                write_outcome=NoWriteOutcome(),
                execution_identity=execution_identity,
            )
            with self.memory_write_uow_factory() as uow:
                uow.decisions.append_decision(record)
            return MemoryGovernanceApplyResult(decision_record=record, events=[])
        compaction_lifecycle_block = self._compaction_lifecycle_block_reason(
            decision,
            target_entries=target_entries,
        )
        if compaction_lifecycle_block is not None:
            record = _decision_record(
                decision=SkipDecision(
                    target_entry_ids=_target_entry_ids(decision),
                    reason="Compaction-origin candidates cannot perform replacement lifecycle decisions in V1.",
                    skip_reason=compaction_lifecycle_block,
                ),
                requested_decision=decision,
                governance_batch_id=governance_batch_id,
                write_outcome=NoWriteOutcome(),
                execution_identity=execution_identity,
            )
            with self.memory_write_uow_factory() as uow:
                uow.decisions.append_decision(record)
            return MemoryGovernanceApplyResult(
                decision_record=record,
                events=[],
                diagnostics=(compaction_lifecycle_block,),
            )

        dispatch_ticket: GovernanceEventDispatchTicket | None = None
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
                    requested_decision=decision,
                    governance_batch_id=governance_batch_id,
                    write_outcome=NoWriteOutcome(),
                    execution_identity=execution_identity,
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
                ) or self._replacement_evidence_block_reason(
                    decision,
                    target_entry=_single_target_entry(decision.target_entry_id, target_entries),
                    relatedness_context=relatedness_context,
                )
                if supersede_blocked_reason is None:
                    valid_old_ids, supersede_blocked_reason = self._validate_supersede_targets(
                        decision,
                        uow,
                        relatedness_context=relatedness_context,
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
                        self._validate_contradiction_targets(
                            decision,
                            uow,
                            relatedness_context=relatedness_context,
                        )
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

            record = _decision_record(
                decision=effective_decision,
                requested_decision=decision,
                governance_batch_id=governance_batch_id,
                write_outcome=_write_outcome(
                    outcome,
                    outcome.events,
                    superseded_memory_ids=recorded_superseded_ids,
                    contradicted_memory_ids=recorded_contradicted_ids,
                ),
                execution_identity=execution_identity,
            )
            is_supersede_origin = isinstance(decision, SupersedeAndSubmitDecision)
            is_contradiction_origin = isinstance(decision, ContradictAndSubmitDecision)
            if not (is_supersede_origin or is_contradiction_origin):
                governance_candidate = self._governance_candidate_for_decision(
                    effective_decision,
                    record=record,
                )
                if governance_candidate is not None:
                    uow.decisions.append_candidate(governance_candidate)
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
                payload=(
                    mutation_payload.model_dump(mode="json")
                    if mutation_payload is not None
                    else None
                ),
            )
            event_candidates = tuple(
                outcome.events + supersede_events + contradiction_events
            )
            dispatch_ticket = uow.runtime_events.append_batch(
                event_candidates,
                governance_batch_id=governance_batch_id,
                decision_id=record.decision_id,
            )

        assert dispatch_ticket is not None
        try:
            stored_events = list(self._dispatch_event_ticket(dispatch_ticket))
        except BaseException:
            self._event_dispatch_retry_required = True
            raise
        self._event_dispatch_retry_required = bool(
            self.event_outbox_dispatcher
            and self.event_outbox_dispatcher.has_pending()
        )
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

    def _dispatch_event_ticket(
        self,
        ticket: GovernanceEventDispatchTicket,
    ) -> Sequence[AgentEvent]:
        dispatcher = self.event_outbox_dispatcher
        if dispatcher is not None:
            return dispatcher.dispatch_ticket(ticket)
        return self.event_commit_port(ticket.events)

    async def _run_auxiliary_operation(
        self,
        name: str,
        operation,
        *,
        deadline_monotonic: float | None = None,
    ):
        deadline = deadline_monotonic or monotonic() + 30.0
        if self.async_operation_port is not None:
            return await self.async_operation_port(name, operation, deadline)
        loop = asyncio.get_running_loop()
        from pulsara_agent.runtime.blocking_executor import auxiliary_io_executor

        return await loop.run_in_executor(auxiliary_io_executor(), operation)

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
        record: MemoryGovernanceDecisionRecord,
    ) -> PooledMemoryCandidate | None:
        if isinstance(decision, CorrectAndSubmitDecision):
            return self._governance_candidate(
                decision.candidate,
                (decision.target_entry_id,),
                record,
            )
        if isinstance(decision, MergeAndSubmitDecision):
            return self._governance_candidate(
                decision.candidate,
                decision.target_entry_ids,
                record,
            )
        return None

    def _governance_candidate(
        self,
        candidate,
        source_entry_ids: tuple[str, ...],
        record: MemoryGovernanceDecisionRecord,
    ) -> PooledMemoryCandidate:
        ctx = governance_batch_context(record.governance_batch_id)
        attribution = build_frozen_fact(
            GovernanceDerivedWriteAttributionFact,
            schema_version="governance_derived_write_attribution.v1",
            parent_candidate_entry_ids=source_entry_ids,
            governance_batch_id=record.governance_batch_id,
            batch_input_fingerprint=record.batch_input_fingerprint,
            decision_id=record.decision_id,
            decision_payload_fingerprint=record.decision_payload_fingerprint,
        )
        entry_digest = context_fingerprint(
            "governance-derived-candidate-entry-id:v1",
            attribution.attribution_fingerprint,
        ).removeprefix("sha256:")
        return PooledMemoryCandidate(
            entry_id=f"pool:governance:{entry_digest}",
            payload=ValidCandidatePayload(candidate=candidate),
            origin=CandidateOrigin.GOVERNANCE,
            source_session_id=self.runtime_session_id,
            source_run_id=ctx.run_id,
            source_turn_id=ctx.turn_id,
            source_reply_id=ctx.reply_id,
            user_quote=f"derived_from:{','.join(source_entry_ids)}",
            metadata={
                "governance_derived_write_attribution": attribution.model_dump(
                    mode="json"
                )
            },
        )

    def _validate_target_entries(self, decision: GovernanceDecision) -> tuple[PooledMemoryCandidate, ...]:
        targets: list[PooledMemoryCandidate] = []
        for entry_id in _target_entry_ids(decision):
            target = self.candidate_pool.get_candidate(entry_id)
            if target.source_session_id != self.runtime_session_id:
                raise ValueError(f"governance decision targets candidate from another runtime: {entry_id}")
            targets.append(target)
        return tuple(targets)

    def _compaction_lifecycle_block_reason(
        self,
        decision: GovernanceDecision,
        *,
        target_entries: tuple[PooledMemoryCandidate, ...],
    ) -> str | None:
        if not isinstance(decision, (SupersedeAndSubmitDecision, ContradictAndSubmitDecision)):
            return None
        if any(target.origin is CandidateOrigin.COMPACTION for target in target_entries):
            return "compaction_origin_replacement_evidence_unsupported"
        return None

    def _validate_supersede_targets(
        self,
        decision: SupersedeAndSubmitDecision,
        uow: GovernanceWriteUnitOfWork,
        *,
        relatedness_context: RelatednessExecutionContext | None,
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
            locked = uow.lock_canonical_memory(old_id)
            if locked is None:
                return (), f"supersede_target_missing:{old_id}"
            old_doc, actual_revision = locked
            revision_error = _relatedness_revision_block_reason(
                entry_id=decision.target_entry_id,
                memory_id=old_id,
                actual_revision=actual_revision,
                relatedness_context=relatedness_context,
            )
            if revision_error is not None:
                return (), revision_error
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
        *,
        target_entry: PooledMemoryCandidate,
        relatedness_context: RelatednessExecutionContext | None,
    ) -> str | None:
        if not decision.replacement_evidence_refs:
            return "missing_replacement_evidence"
        allowed: set[str] = set()
        if relatedness_context is not None:
            allowed.update(
                relatedness_context.verified_evidence_refs.get(
                    target_entry.entry_id, frozenset()
                )
            )
        for ref in decision.replacement_evidence_refs:
            if ref not in allowed:
                return f"replacement_evidence_not_in_source_context:{ref}"
        return None

    def _validate_contradiction_targets(
        self,
        decision: ContradictAndSubmitDecision,
        uow: GovernanceWriteUnitOfWork,
        *,
        relatedness_context: RelatednessExecutionContext | None,
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
            locked = uow.lock_canonical_memory(old_id)
            if locked is None:
                return (), f"contradiction_target_missing:{old_id}"
            old_doc, actual_revision = locked
            revision_error = _relatedness_revision_block_reason(
                entry_id=decision.target_entry_id,
                memory_id=old_id,
                actual_revision=actual_revision,
                relatedness_context=relatedness_context,
            )
            if revision_error is not None:
                return (), revision_error
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


def _relatedness_revision_block_reason(
    *,
    entry_id: str,
    memory_id: str,
    actual_revision: int,
    relatedness_context: RelatednessExecutionContext | None,
) -> str | None:
    if relatedness_context is None:
        return "relatedness_context_missing"
    expected_revision = relatedness_context.node_revisions.get(entry_id, {}).get(
        memory_id
    )
    if expected_revision is None:
        return f"relatedness_revision_missing:{memory_id}"
    if expected_revision != actual_revision:
        return (
            f"relatedness_target_revision_drift:{memory_id}:"
            f"expected={expected_revision}:actual={actual_revision}"
        )
    return None


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
    requested_decision: GovernanceDecision,
    write_outcome,
    execution_identity: GovernanceDecisionExecutionIdentity,
) -> MemoryGovernanceDecisionRecord:
    decision_id, requested_decision_payload_fingerprint = governance_decision_identity(
        decision=requested_decision,
        execution_identity=execution_identity,
    )
    decision_payload_fingerprint = context_fingerprint(
        "memory-governance-effective-decision-payload:v1",
        decision.model_dump(mode="json"),
    )
    return MemoryGovernanceDecisionRecord(
        decision_id=decision_id,
        governance_batch_id=governance_batch_id,
        batch_input_fingerprint=execution_identity.batch_input_fingerprint,
        batch_input_reference_fingerprint=(
            execution_identity.batch_input_reference_fingerprint
        ),
        governance_model_call_id=execution_identity.governance_model_call_id,
        decision_index=execution_identity.decision_index,
        requested_decision_payload_fingerprint=(
            requested_decision_payload_fingerprint
        ),
        decision_payload_fingerprint=decision_payload_fingerprint,
        decision=decision,
        write_outcome=write_outcome,
    )


def governance_decision_identity(
    *,
    decision: GovernanceDecision,
    execution_identity: GovernanceDecisionExecutionIdentity,
) -> tuple[str, str]:
    decision_payload_fingerprint = context_fingerprint(
        "memory-governance-decision-payload:v1",
        decision.model_dump(mode="json"),
    )
    decision_id = "memory_governance_decision:" + context_fingerprint(
        "memory-governance-decision-id:v1",
        (
            execution_identity.batch_input_fingerprint,
            execution_identity.decision_index,
            decision_payload_fingerprint,
        ),
    ).removeprefix("sha256:")
    return decision_id, decision_payload_fingerprint


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


def _single_target_entry(
    entry_id: str,
    target_entries: tuple[PooledMemoryCandidate, ...],
) -> PooledMemoryCandidate:
    for target in target_entries:
        if target.entry_id == entry_id:
            return target
    raise KeyError(entry_id)


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
            "target_revision_drift",
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
