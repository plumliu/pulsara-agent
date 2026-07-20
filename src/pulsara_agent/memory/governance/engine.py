"""Typed, claim-owned memory-governance execution pipeline."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from pulsara_agent.event import (
    EventContext,
    MemoryCandidateEvidenceRejectedEvent,
    MemoryGovernanceBatchBlockedEvent,
    MemoryGovernanceBatchCompletedEvent,
    MemoryGovernanceBatchFailedEvent,
    MemoryGovernanceBatchPreparedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    utc_now,
)
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.commit import RuntimeSessionModelStreamEventCommitPort
from pulsara_agent.llm.direct import (
    DirectModelCallResult,
    collect_direct_model_call_handle,
)
from pulsara_agent.llm.lifecycle import prepare_model_lifecycle_start_bundle
from pulsara_agent.llm.materialize import (
    MAX_MODEL_CALL_MATERIALIZATION_EVENTS,
    MAX_MODEL_CALL_MATERIALIZATION_PAYLOAD_BYTES,
    materialize_committed_model_call_result_from_terminal_projection,
)
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.terminal_projection import (
    hydrate_terminal_projection,
)
from pulsara_agent.memory.candidates.pool import (
    GovernanceDecision,
    PooledMemoryCandidate,
    decision_target_entry_ids,
    governance_batch_context,
    new_governance_batch_id,
)
from pulsara_agent.memory.candidates.projection_outbox import (
    MemoryCandidateProjectionCommitPort,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.memory.governance.batch_input import (
    MemoryGovernanceBatchPreparationCommitPort,
    PreparedGovernanceBatchInput,
    build_governance_batch_input,
    build_governance_model_input_attribution,
    freeze_relatedness_batch,
    governance_candidate_replacement_evidence_refs,
    hydrate_governance_batch_input,
    llm_context_from_model_input,
    persist_governance_batch_input,
)
from pulsara_agent.memory.governance.claims import (
    ClaimedGovernanceBatch,
    MemoryGovernanceCandidateClaimRepository,
)
from pulsara_agent.memory.governance.evidence import (
    GovernanceEvidencePreparation,
    GovernanceSourceEvidenceBuilder,
)
from pulsara_agent.memory.governance.executor import (
    GovernanceDecisionExecutionIdentity,
    MemoryGovernanceApplyResult,
    MemoryGovernanceExecutor,
    governance_decision_identity,
)
from pulsara_agent.memory.governance.preparation import (
    GovernanceBatchPreparationRecord,
    GovernanceBatchPreparationRepository,
    GovernanceBatchPreparationStatus,
)
from pulsara_agent.memory.governance.recovery import (
    GovernanceBatchExecutionOwnerRegistry,
    MemoryGovernanceBatchRecoveryService,
    RecoverableGovernanceBatch,
    governance_batch_terminal_event_ids,
)
from pulsara_agent.memory.governance.relatedness import (
    GovernanceRelatednessService,
    RelatednessAvailability,
    RelatednessBatchResult,
    RelatednessExecutionContext,
)
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.governance_evidence import (
    GovernanceBatchInputSnapshotFact,
    GovernanceCandidateClaimStatus,
    GovernanceEvidenceBuildStatus,
    GovernanceModelInputAttributionFact,
    ImmutableGovernanceCandidateSnapshotFact,
    MemoryGovernanceCandidateClaimFact,
)
from pulsara_agent.primitives.model_call import (
    ModelCallDiagnosticFact,
    ModelCallPurpose,
    ModelTokenUsageFact,
    ResolvedModelCallFact,
)
from pulsara_agent.primitives.runtime_observation import (
    RuntimeClockObservationPayloadFact,
    RuntimeObservationWireSemanticFact,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


class MemoryGovernanceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = ""
    decisions: list[GovernanceDecision] = Field(default_factory=list, max_length=32)


@dataclass(frozen=True, slots=True)
class MemoryGovernanceOptions:
    model_role: ModelRole = ModelRole.FLASH
    llm_options: LLMOptions = field(default_factory=LLMOptions)
    limit: int = 20


@dataclass(frozen=True, slots=True)
class MemoryGovernanceRunResult:
    governance_batch_id: str
    decisions: list[GovernanceDecision]
    applied: list[MemoryGovernanceApplyResult]
    relatedness_diagnostics: dict[str, object] = field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    resolved_model_call: ResolvedModelCallFact | None = None
    usage_status: Literal["reported", "missing"] | None = None
    usage: ModelTokenUsageFact | None = None
    estimated_input_tokens: int | None = None
    reported_model_id: str | None = None


@dataclass(frozen=True, slots=True)
class _GovernanceModelResult:
    text: str
    resolved_call: ResolvedModelCallFact
    estimated_input_tokens: int
    outcome: str
    error: ModelCallDiagnosticFact | None
    usage_status: Literal["reported", "missing"]
    usage: ModelTokenUsageFact | None
    reported_model_id: str | None

    @classmethod
    def from_direct(cls, result: DirectModelCallResult) -> "_GovernanceModelResult":
        return cls(
            text=result.text,
            resolved_call=result.resolved_call,
            estimated_input_tokens=result.estimated_input_tokens,
            outcome=result.outcome,
            error=result.error,
            usage_status=result.usage_status,
            usage=result.usage,
            reported_model_id=result.reported_model_id,
        )


@dataclass(slots=True)
class MemoryGovernanceEngine:
    """Only production owner for claim, evidence, model input and decision apply."""

    llm_runtime: LLMRuntime
    executor: MemoryGovernanceExecutor
    runtime_session: "RuntimeSession"
    archive: ArtifactStore
    claim_repository: MemoryGovernanceCandidateClaimRepository
    preparation_repository: GovernanceBatchPreparationRepository
    evidence_builder: GovernanceSourceEvidenceBuilder
    preparation_commit_port: MemoryGovernanceBatchPreparationCommitPort
    candidate_projection_commit_port: MemoryCandidateProjectionCommitPort | None = None
    options: MemoryGovernanceOptions = field(default_factory=MemoryGovernanceOptions)
    relatedness_service: GovernanceRelatednessService | None = None
    execution_owners: GovernanceBatchExecutionOwnerRegistry[
        MemoryGovernanceRunResult
    ] = field(default_factory=GovernanceBatchExecutionOwnerRegistry)
    _retry_required: bool = field(default=False, init=False, repr=False)

    async def run_pending(
        self,
        *,
        trigger_reason: str,
        governance_batch_id: str | None = None,
        limit: int | None = None,
    ) -> MemoryGovernanceRunResult:
        open_batches = await self._discover_open_batches()
        batch_id = governance_batch_id or (
            open_batches[0].governance_batch_id
            if open_batches
            else new_governance_batch_id()
        )
        recoverable = next(
            (item for item in open_batches if item.governance_batch_id == batch_id),
            None,
        )
        return await self.execution_owners.run(
            batch_id,
            lambda: self._run_owned(
                trigger_reason=trigger_reason,
                governance_batch_id=batch_id,
                limit=limit or self.options.limit,
                recoverable=recoverable,
            ),
        )

    @property
    def retry_required(self) -> bool:
        return self._retry_required

    async def stop_admission_and_drain(self, *, deadline_monotonic: float) -> None:
        await self.execution_owners.stop_admission_and_drain(
            deadline_monotonic=deadline_monotonic
        )

    async def _run_owned(
        self,
        *,
        trigger_reason: str,
        governance_batch_id: str,
        limit: int,
        recoverable: RecoverableGovernanceBatch | None,
    ) -> MemoryGovernanceRunResult:
        self._retry_required = False
        try:
            await self.executor.flush_pending_event_outbox_async()
            if self.candidate_projection_commit_port is not None:
                await self.candidate_projection_commit_port.flush_pending()
        except Exception as exc:
            self._retry_required = True
            return self._error_result(
                governance_batch_id,
                exc,
                "durable producer outbox could not be drained",
            )

        if recoverable is not None and recoverable.preparation is not None:
            try:
                prepared = await self._hydrate_preparation(recoverable)
            except Exception as exc:
                self.runtime_session.latch_memory_governance_reconciliation_required()
                return self._error_result(
                    governance_batch_id,
                    exc,
                    "governance batch input artifact is untrusted",
                )
            if recoverable.claim_status is GovernanceCandidateClaimStatus.PREPARING:
                (
                    prepared_event,
                    attribution,
                    preparation_record,
                ) = await self.preparation_commit_port.commit_prepared_bundle(
                    prepared=prepared,
                    claims=recoverable.claims,
                    preparation_record=recoverable.preparation,
                )
                claims = await self._prepared_claims(
                    governance_batch_id=governance_batch_id,
                    expected_candidate_entry_ids=(prepared_event.candidate_entry_ids),
                )
            else:
                if recoverable.prepared_event is None:
                    raise RuntimeError("prepared governance recovery lacks its event")
                prepared_event = recoverable.prepared_event
                attribution = build_governance_model_input_attribution(
                    prepared_event=prepared_event,
                    prepared=prepared,
                    runtime_session_id=self.runtime_session.runtime_session_id,
                )
                claims = recoverable.claims
                preparation_record = recoverable.preparation
                if (
                    preparation_record.status
                    is not GovernanceBatchPreparationStatus.PREPARED
                    or preparation_record.prepared_event_id != prepared_event.id
                ):
                    raise RuntimeError(
                        "prepared governance recovery locator is inconsistent"
                    )
            return await self._execute_prepared(
                prepared=prepared,
                prepared_event=prepared_event,
                attribution=attribution,
                claims=claims,
                preparation_record=preparation_record,
                relatedness_diagnostics={"mode": "durable_recovery"},
            )

        claimed = await self._claim_batch(
            governance_batch_id=governance_batch_id,
            limit=limit,
        )
        if not claimed.candidates:
            return MemoryGovernanceRunResult(
                governance_batch_id=governance_batch_id,
                decisions=[],
                applied=[],
            )
        authority = self.runtime_session.transcript_projection_state_store.capture_governance_authority_snapshot()
        preparations = await self._prepare_evidence(claimed, authority)
        if any(
            item.result.status is GovernanceEvidenceBuildStatus.AUTHORITY_UNTRUSTED
            for item in preparations
        ):
            self.runtime_session.latch_memory_governance_reconciliation_required()
            return MemoryGovernanceRunResult(
                governance_batch_id=governance_batch_id,
                decisions=[],
                applied=[],
                error_type="GovernanceAuthorityUntrusted",
                error_message="canonical governance evidence could not be trusted",
            )

        surviving_candidates: list[PooledMemoryCandidate] = []
        surviving_claims: list[MemoryGovernanceCandidateClaimFact] = []
        full_snapshots: list[ImmutableGovernanceCandidateSnapshotFact] = []
        has_not_ready = False
        for candidate, claim, evidence in zip(
            claimed.candidates,
            claimed.claims,
            preparations,
            strict=True,
        ):
            if (
                evidence.result.status
                is GovernanceEvidenceBuildStatus.CANDIDATE_SOURCE_INVALID
            ):
                await self._commit_rejection(
                    governance_batch_id=governance_batch_id,
                    claim=claim,
                    evidence=evidence,
                )
                continue
            surviving_candidates.append(candidate)
            surviving_claims.append(claim)
            if evidence.result.status is GovernanceEvidenceBuildStatus.NOT_READY:
                has_not_ready = True
            elif evidence.result.status is GovernanceEvidenceBuildStatus.FULL:
                if evidence.candidate_snapshot is None:
                    raise AssertionError("full evidence lost its snapshot")
                full_snapshots.append(evidence.candidate_snapshot)
            else:
                raise RuntimeError(
                    f"illegal governance evidence status: {evidence.result.status}"
                )
        if not surviving_claims:
            return MemoryGovernanceRunResult(
                governance_batch_id=governance_batch_id,
                decisions=[],
                applied=[],
            )
        if has_not_ready:
            self._retry_required = True
            return MemoryGovernanceRunResult(
                governance_batch_id=governance_batch_id,
                decisions=[],
                applied=[],
                error_type="GovernanceEvidenceNotReady",
                error_message="source run has not reached its evidence closure",
            )
        if len(full_snapshots) != len(surviving_claims):
            raise RuntimeError("governance evidence classification lost a candidate")

        relatedness, diagnostics = await self._collect_relatedness(
            tuple(surviving_candidates)
        )
        projection_contract_fingerprint = context_fingerprint(
            "governance-relatedness-prompt-projection-contract:v1",
            "bounded-canonical-memory-statement+typed-relationship-codes",
        )
        relatedness_snapshots = await self._run_context_io(
            "governance-relatedness-freeze",
            lambda: freeze_relatedness_batch(
                batch=relatedness,
                candidates=tuple(full_snapshots),
                graph_id=self.executor.graph_id,
                archive=self.archive,
                runtime_session_id=self.runtime_session.runtime_session_id,
                provider_contract_fingerprint=projection_contract_fingerprint,
            ),
        )
        target = self.llm_runtime.resolve_target(
            role=self.options.model_role,
            requested_options=self.options.llm_options,
        )
        call = self.llm_runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.MEMORY_GOVERNANCE,
        )
        prepared = build_governance_batch_input(
            runtime_session_id=self.runtime_session.runtime_session_id,
            governance_batch_id=governance_batch_id,
            source_ledger_through_sequence=authority.ledger_through_sequence,
            transcript_authority_snapshot_fingerprint=authority.snapshot_fingerprint,
            claims=tuple(surviving_claims),
            candidate_snapshots=tuple(full_snapshots),
            relatedness_snapshots=relatedness_snapshots,
            allowed_scopes=self.executor.allowed_write_scopes,
            prompt_projection_contract_fingerprint=(
                self.evidence_builder.prompt_contract.contract_fingerprint
            ),
            max_candidates_per_batch=(
                self.evidence_builder.prompt_contract.max_candidates_per_batch
            ),
            max_batch_projection_utf8_bytes=(
                self.evidence_builder.prompt_contract.max_batch_projection_utf8_bytes
            ),
            call=call,
            trigger_reason=trigger_reason,
            clock_observed_at_utc=utc_now(),
        )
        await persist_governance_batch_input(
            prepared=prepared,
            runtime_session=self.runtime_session,
            archive=self.archive,
        )
        record = GovernanceBatchPreparationRecord.build(
            runtime_session_id=self.runtime_session.runtime_session_id,
            reference=prepared.reference,
            claims=tuple(surviving_claims),
            source_ledger_through_sequence=(
                prepared.snapshot.source_ledger_through_sequence
            ),
            resolved_model_call_id=(
                prepared.snapshot.model_input.resolved_call.resolved_model_call_id
            ),
        )
        await self._run_context_io(
            "governance-preparation-stage",
            lambda: self.preparation_repository.stage(record),
        )
        (
            prepared_event,
            attribution,
            preparation_record,
        ) = await self.preparation_commit_port.commit_prepared_bundle(
            prepared=prepared,
            claims=tuple(surviving_claims),
            preparation_record=record,
        )
        prepared_claims = await self._prepared_claims(
            governance_batch_id=governance_batch_id,
            expected_candidate_entry_ids=prepared_event.candidate_entry_ids,
        )
        return await self._execute_prepared(
            prepared=prepared,
            prepared_event=prepared_event,
            attribution=attribution,
            claims=prepared_claims,
            preparation_record=preparation_record,
            relatedness_diagnostics=diagnostics,
        )

    async def _execute_prepared(
        self,
        *,
        prepared: PreparedGovernanceBatchInput,
        prepared_event: MemoryGovernanceBatchPreparedEvent,
        attribution: GovernanceModelInputAttributionFact,
        claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
        preparation_record: GovernanceBatchPreparationRecord,
        relatedness_diagnostics: dict[str, object],
    ) -> MemoryGovernanceRunResult:
        batch_id = prepared.snapshot.governance_batch_id
        if any(
            claim.status is not GovernanceCandidateClaimStatus.PREPARED
            or claim.prepared_event_id != prepared_event.id
            for claim in claims
        ):
            raise RuntimeError("governance execution lacks prepared claim ownership")
        try:
            call = self._rebind_frozen_call(prepared.snapshot)
        except Exception as exc:
            await self._commit_terminal(
                prepared=prepared,
                prepared_event=prepared_event,
                claims=claims,
                preparation_record=preparation_record,
                terminal_kind="blocked",
                terminal_reason="historical_binding_missing",
                governance_model_call_id=None,
                decision_ids=(),
                diagnostics=(type(exc).__name__,),
            )
            return self._error_result(
                batch_id,
                exc,
                "historical governance model binding is unavailable",
            )

        model_result = await self._call_or_materialize(
            prepared=prepared,
            attribution=attribution,
            call=call,
        )
        if model_result is None:
            self._retry_required = True
            return MemoryGovernanceRunResult(
                governance_batch_id=batch_id,
                decisions=[],
                applied=[],
                relatedness_diagnostics=relatedness_diagnostics,
                error_type="GovernanceModelStreamNotTerminal",
                error_message="durable governance model stream is still open",
                resolved_model_call=call.fact,
            )
        if model_result.outcome != "completed":
            await self._commit_terminal(
                prepared=prepared,
                prepared_event=prepared_event,
                claims=claims,
                preparation_record=preparation_record,
                terminal_kind="failed",
                terminal_reason="model_failed",
                governance_model_call_id=call.fact.resolved_model_call_id,
                decision_ids=(),
                diagnostics=(
                    model_result.error.code
                    if model_result.error is not None
                    else model_result.outcome,
                ),
            )
            return MemoryGovernanceRunResult(
                governance_batch_id=batch_id,
                decisions=[],
                applied=[],
                relatedness_diagnostics=relatedness_diagnostics,
                error_type="GovernanceModelFailed",
                error_message=(
                    model_result.error.message
                    if model_result.error is not None
                    else model_result.outcome
                ),
                resolved_model_call=model_result.resolved_call,
                usage_status=model_result.usage_status,
                usage=model_result.usage,
                estimated_input_tokens=model_result.estimated_input_tokens,
                reported_model_id=model_result.reported_model_id,
            )
        try:
            output = _parse_governance_output(model_result.text)
            _validate_decision_coverage(
                output.decisions,
                expected_candidate_ids=tuple(
                    claim.candidate_entry_id for claim in claims
                ),
            )
        except Exception as exc:
            await self._commit_terminal(
                prepared=prepared,
                prepared_event=prepared_event,
                claims=claims,
                preparation_record=preparation_record,
                terminal_kind="failed",
                terminal_reason="output_invalid",
                governance_model_call_id=call.fact.resolved_model_call_id,
                decision_ids=(),
                diagnostics=(type(exc).__name__,),
            )
            return self._error_result(
                batch_id,
                exc,
                "governance model output is invalid",
                model_result=model_result,
                relatedness_diagnostics=relatedness_diagnostics,
            )

        execution_context = _execution_context_from_snapshot(prepared.snapshot)
        applied: list[MemoryGovernanceApplyResult] = []
        existing_decisions = await self._run_context_io(
            "governance-existing-decision-read",
            self.executor.candidate_pool.list_decisions,
        )
        existing_by_index = {
            (record.batch_input_fingerprint, record.decision_index): record
            for record in existing_decisions
        }
        try:
            for index, decision in enumerate(output.decisions):
                existing = existing_by_index.get(
                    (prepared.snapshot.batch_input_fingerprint, index)
                )
                if existing is not None:
                    identity = GovernanceDecisionExecutionIdentity(
                        batch_input_fingerprint=(
                            prepared.snapshot.batch_input_fingerprint
                        ),
                        batch_input_reference_fingerprint=(
                            prepared.reference.reference_fingerprint
                        ),
                        governance_model_call_id=call.fact.resolved_model_call_id,
                        decision_index=index,
                        allowed_candidate_entry_ids=frozenset(
                            claim.candidate_entry_id for claim in claims
                        ),
                        allowed_scopes=frozenset(prepared.snapshot.allowed_scopes),
                    )
                    expected_id, requested_fingerprint = governance_decision_identity(
                        decision=decision,
                        execution_identity=identity,
                    )
                    if (
                        existing.governance_batch_id != batch_id
                        or existing.decision_id != expected_id
                        or existing.requested_decision_payload_fingerprint
                        != requested_fingerprint
                        or existing.batch_input_reference_fingerprint
                        != prepared.reference.reference_fingerprint
                        or existing.governance_model_call_id
                        != call.fact.resolved_model_call_id
                    ):
                        raise ValueError(
                            "durable governance decision attribution drifted"
                        )
                    applied.append(
                        MemoryGovernanceApplyResult(
                            decision_record=existing,
                            events=[],
                            diagnostics=("recovered_existing_decision",),
                        )
                    )
                    continue
                applied.append(
                    await self.executor.apply_decision_async(
                        decision,
                        governance_batch_id=batch_id,
                        relatedness_context=execution_context,
                        execution_identity=GovernanceDecisionExecutionIdentity(
                            batch_input_fingerprint=(
                                prepared.snapshot.batch_input_fingerprint
                            ),
                            batch_input_reference_fingerprint=(
                                prepared.reference.reference_fingerprint
                            ),
                            governance_model_call_id=(call.fact.resolved_model_call_id),
                            decision_index=index,
                            allowed_candidate_entry_ids=frozenset(
                                claim.candidate_entry_id for claim in claims
                            ),
                            allowed_scopes=frozenset(prepared.snapshot.allowed_scopes),
                        ),
                    )
                )
        except Exception as exc:
            # A decision UOW may have committed before a later decision fails.
            # Keep the frozen Prepared owner so recovery can validate and adopt
            # existing deterministic decision IDs, then apply only the suffix.
            self._retry_required = True
            return self._error_result(
                batch_id,
                exc,
                "governance decision application is pending recovery",
                decisions=output.decisions,
                applied=applied,
                model_result=model_result,
                relatedness_diagnostics=relatedness_diagnostics,
            )
        await self._commit_terminal(
            prepared=prepared,
            prepared_event=prepared_event,
            claims=claims,
            preparation_record=preparation_record,
            terminal_kind="completed",
            terminal_reason="decisions_applied" if applied else "no_decisions",
            governance_model_call_id=call.fact.resolved_model_call_id,
            decision_ids=tuple(item.decision_record.decision_id for item in applied),
            diagnostics=(),
        )
        return MemoryGovernanceRunResult(
            governance_batch_id=batch_id,
            decisions=output.decisions,
            applied=applied,
            relatedness_diagnostics=relatedness_diagnostics,
            resolved_model_call=model_result.resolved_call,
            usage_status=model_result.usage_status,
            usage=model_result.usage,
            estimated_input_tokens=model_result.estimated_input_tokens,
            reported_model_id=model_result.reported_model_id,
        )

    async def _call_or_materialize(
        self,
        *,
        prepared: PreparedGovernanceBatchInput,
        attribution: GovernanceModelInputAttributionFact,
        call: ResolvedModelCall,
    ) -> _GovernanceModelResult | None:
        raw_events = await self._run_context_io(
            "governance-model-call-recovery-read",
            lambda: self.executor.event_log.read_raw_model_call_events(
                call.fact.resolved_model_call_id,
                max_events=MAX_MODEL_CALL_MATERIALIZATION_EVENTS,
                max_payload_bytes=MAX_MODEL_CALL_MATERIALIZATION_PAYLOAD_BYTES,
            ),
        )
        events = tuple(
            envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            for envelope in raw_events
        )
        starts = tuple(
            event for event in events if isinstance(event, ModelCallStartEvent)
        )
        ends = tuple(event for event in events if isinstance(event, ModelCallEndEvent))
        if not starts:
            return _GovernanceModelResult.from_direct(
                await self._call_flash(
                    prepared=prepared,
                    attribution=attribution,
                    call=call,
                )
            )
        if len(starts) != 1:
            raise RuntimeError("governance model call has ambiguous Start events")
        if not ends:
            return None
        if len(ends) != 1:
            raise RuntimeError("governance model call has ambiguous terminal events")
        document = await hydrate_terminal_projection(
            self.runtime_session,
            ends[0].terminal_projection.projection_reference,
        )
        committed = materialize_committed_model_call_result_from_terminal_projection(
            events,
            resolved_model_call_id=call.fact.resolved_model_call_id,
            runtime_session_id=self.runtime_session.runtime_session_id,
            document=document,
        )
        error = None
        if committed.terminal_outcome == "provider_error":
            provider_error = committed.provider_errors[0]
            error = ModelCallDiagnosticFact(
                code=provider_error.code.value,
                message=provider_error.message,
            )
        return _GovernanceModelResult(
            text=committed.combined_text,
            resolved_call=call.fact,
            estimated_input_tokens=ends[0].estimated_input_tokens,
            outcome=committed.terminal_outcome,
            error=error,
            usage_status=committed.usage_status,
            usage=committed.usage,
            reported_model_id=committed.reported_model_id,
        )

    async def _call_flash(
        self,
        *,
        prepared: PreparedGovernanceBatchInput,
        attribution: GovernanceModelInputAttributionFact,
        call: ResolvedModelCall,
    ) -> DirectModelCallResult:
        context = prepared.llm_context
        batch_id = prepared.snapshot.governance_batch_id
        event_context = EventContext(
            run_id=f"run:governance/{batch_id}",
            turn_id=f"turn:governance/{batch_id}",
            reply_id=f"reply:governance/{batch_id}",
        )
        provider_input = await self.runtime_session.provider_input_generation_coordinator.prepare_one_shot_call(
            call=call,
            context=context,
            event_context=event_context,
            operation_kind="governance_model_call",
            operation_id=batch_id,
            clock_observed_at_utc=_governance_clock_observed_at_utc(context),
        )
        try:
            context = provider_input.carrier.to_llm_context(context)
            start_bundle = prepare_model_lifecycle_start_bundle(
                call=call,
                context=context,
                event_context=event_context,
                runtime_session=self.runtime_session,
                lifecycle_kind="direct_internal_call",
                governance_input_attribution=attribution,
                provider_input_start_bundle=provider_input,
            )
            handle = self.llm_runtime.start_stream(
                call=call,
                context=context,
                event_context=event_context,
                start_bundle=start_bundle,
                commit_port=RuntimeSessionModelStreamEventCommitPort(
                    runtime_session=self.runtime_session,
                    state=None,
                ),
                execution_registry=(
                    self.runtime_session.model_stream_execution_registry
                ),
            )
        except BaseException:
            await self.runtime_session.provider_input_generation_coordinator.abandon_uncommitted_preparation(
                provider_input.prepared_candidate.preparation_ownership.preparation_id,
                reason="one_shot_failed_before_start",
            )
            raise
        return await collect_direct_model_call_handle(
            handle,
            expected_call=call,
            runtime_session_id=self.runtime_session.runtime_session_id,
        )

    async def _commit_rejection(
        self,
        *,
        governance_batch_id: str,
        claim: MemoryGovernanceCandidateClaimFact,
        evidence: GovernanceEvidencePreparation,
    ) -> None:
        if evidence.rejection is None:
            raise ValueError("candidate rejection lacks its typed record")
        event = MemoryCandidateEvidenceRejectedEvent(
            id=(
                f"memory_candidate:{claim.candidate_entry_id}:"
                f"evidence_rejected:{claim.claim_generation}"
            ),
            **governance_batch_context(governance_batch_id).event_fields(),
            governance_batch_id=governance_batch_id,
            rejection=evidence.rejection,
        )
        companion = self.claim_repository.transition_companion(
            runtime_session_id=self.runtime_session.runtime_session_id,
            expected_claims=(claim,),
            target_status=GovernanceCandidateClaimStatus.TERMINAL,
            terminal_record_id=event.id,
        )
        await self.runtime_session.write_events(
            (event,), transaction_companion=companion
        )

    async def _commit_terminal(
        self,
        *,
        prepared: PreparedGovernanceBatchInput,
        prepared_event: MemoryGovernanceBatchPreparedEvent,
        claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
        preparation_record: GovernanceBatchPreparationRecord,
        terminal_kind: Literal["completed", "failed", "blocked"],
        terminal_reason: str,
        governance_model_call_id: str | None,
        decision_ids: tuple[str, ...],
        diagnostics: tuple[str, ...],
    ) -> None:
        batch_id = prepared.snapshot.governance_batch_id
        event_id = governance_batch_terminal_event_ids(batch_id)[
            {"completed": 0, "failed": 1, "blocked": 2}[terminal_kind]
        ]
        payload = {
            "governance_batch_id": batch_id,
            "prepared_event_id": prepared_event.id,
            "batch_input_fingerprint": prepared.snapshot.batch_input_fingerprint,
            "governance_model_call_id": governance_model_call_id,
            "decision_ids": decision_ids,
            "terminal_reason": terminal_reason,
            "diagnostics": tuple(item[:256] for item in diagnostics[:8]),
        }
        fingerprint = context_fingerprint(
            f"memory-governance-batch-{terminal_kind}-event:v1", payload
        )
        common = {
            "id": event_id,
            **governance_batch_context(batch_id).event_fields(),
            **payload,
            "terminal_event_fingerprint": fingerprint,
        }
        if terminal_kind == "completed":
            if governance_model_call_id is None:
                raise ValueError("completed governance batch requires model call")
            event = MemoryGovernanceBatchCompletedEvent(**common)
        elif terminal_kind == "failed":
            event = MemoryGovernanceBatchFailedEvent(**common)
        else:
            event = MemoryGovernanceBatchBlockedEvent(**common)
        claim_companion = self.claim_repository.transition_companion(
            runtime_session_id=self.runtime_session.runtime_session_id,
            expected_claims=claims,
            target_status=GovernanceCandidateClaimStatus.TERMINAL,
            terminal_record_id=event.id,
        )
        companion = self.preparation_repository.transition_companion(
            expected_record=preparation_record,
            claim_companion=claim_companion,
            target_status=GovernanceBatchPreparationStatus.TERMINAL,
            terminal_event_id=event.id,
        )
        await self.runtime_session.write_events(
            (event,), transaction_companion=companion
        )

    async def _discover_open_batches(self) -> tuple[RecoverableGovernanceBatch, ...]:
        service = MemoryGovernanceBatchRecoveryService(
            runtime_session_id=self.runtime_session.runtime_session_id,
            event_log=self.executor.event_log,
            claim_repository=self.claim_repository,
            preparation_repository=self.preparation_repository,
        )
        return await self._run_context_io(
            "governance-open-batch-discovery",
            service.discover_open_batches,
        )

    async def _hydrate_preparation(
        self,
        recoverable: RecoverableGovernanceBatch,
    ) -> PreparedGovernanceBatchInput:
        if recoverable.preparation is None:
            raise ValueError("governance recovery lacks an artifact locator")
        reference = recoverable.preparation.batch_input_reference
        snapshot = await self._run_context_io(
            "governance-batch-input-hydrate",
            lambda: hydrate_governance_batch_input(
                reference=reference,
                archive=self.archive,
                runtime_session_id=self.runtime_session.runtime_session_id,
            ),
        )
        _validate_recovery_claims(snapshot, recoverable.claims)
        return PreparedGovernanceBatchInput(
            snapshot=snapshot,
            reference=reference,
            canonical_text="",
            llm_context=llm_context_from_model_input(snapshot.model_input),
        )

    async def _claim_batch(
        self,
        *,
        governance_batch_id: str,
        limit: int,
    ) -> ClaimedGovernanceBatch:
        return await self._run_context_io(
            "governance-candidate-claim",
            lambda: self.claim_repository.claim_pending_batch(
                runtime_session_id=self.runtime_session.runtime_session_id,
                governance_batch_id=governance_batch_id,
                limit=limit,
            ),
        )

    async def _prepared_claims(
        self,
        *,
        governance_batch_id: str,
        expected_candidate_entry_ids: tuple[str, ...],
    ) -> tuple[MemoryGovernanceCandidateClaimFact, ...]:
        claims = await self._run_context_io(
            "governance-prepared-claims-read",
            lambda: self.claim_repository.claims_for_batch(
                runtime_session_id=self.runtime_session.runtime_session_id,
                governance_batch_id=governance_batch_id,
            ),
        )
        by_candidate_id = {claim.candidate_entry_id: claim for claim in claims}
        if len(by_candidate_id) != len(claims):
            raise RuntimeError(
                "governance claim repository returned duplicate candidates"
            )
        try:
            prepared = tuple(
                by_candidate_id[candidate_entry_id]
                for candidate_entry_id in expected_candidate_entry_ids
            )
        except KeyError as exc:
            raise RuntimeError(
                "governance Prepared event references a missing claim"
            ) from exc
        if not prepared or any(
            claim.status is not GovernanceCandidateClaimStatus.PREPARED
            for claim in prepared
        ):
            raise RuntimeError("governance Prepared claim transition is incomplete")
        expected = set(expected_candidate_entry_ids)
        if any(
            claim.candidate_entry_id not in expected
            and claim.status is not GovernanceCandidateClaimStatus.TERMINAL
            for claim in claims
        ):
            raise RuntimeError(
                "governance batch retained an unrelated non-terminal claim"
            )
        return prepared

    async def _prepare_evidence(self, claimed, authority):
        return await self._run_context_io(
            "governance-source-evidence",
            lambda: tuple(
                self.evidence_builder.prepare(
                    candidate=candidate,
                    authority=authority,
                )
                for candidate in claimed.candidates
            ),
        )

    async def _collect_relatedness(
        self,
        candidates: tuple[PooledMemoryCandidate, ...],
    ) -> tuple[RelatednessBatchResult, dict[str, object]]:
        if self.relatedness_service is None:
            result = RelatednessBatchResult.unavailable(
                candidates,
                warning="relatedness_unavailable",
            )
        else:
            try:
                result = await self.relatedness_service.collect_batch(
                    candidates,
                    graph_id=self.executor.graph_id,
                )
            except Exception as exc:
                result = RelatednessBatchResult.unavailable(
                    candidates,
                    warning=f"relatedness_service_failed:{type(exc).__name__}",
                )
        return result, dict(result.diagnostics)

    def _rebind_frozen_call(
        self,
        snapshot: GovernanceBatchInputSnapshotFact,
    ) -> ResolvedModelCall:
        fact = snapshot.model_input.resolved_call
        target = self.llm_runtime.rebind_target(fact.target)
        if target.fact != fact.target:
            raise ValueError("historical governance target rebind drifted")
        return ResolvedModelCall(target=target, fact=fact)

    async def _run_context_io(self, operation_name, operation):
        return await self.runtime_session.context_input_io_service.execute(
            operation_name=operation_name,
            operation=operation,
            deadline_monotonic=monotonic() + 30.0,
        )

    def _error_result(
        self,
        governance_batch_id: str,
        error: BaseException,
        message: str,
        *,
        decisions: list[GovernanceDecision] | None = None,
        applied: list[MemoryGovernanceApplyResult] | None = None,
        model_result: _GovernanceModelResult | None = None,
        relatedness_diagnostics: dict[str, object] | None = None,
    ) -> MemoryGovernanceRunResult:
        return MemoryGovernanceRunResult(
            governance_batch_id=governance_batch_id,
            decisions=decisions or [],
            applied=applied or [],
            relatedness_diagnostics=relatedness_diagnostics or {},
            error_type=type(error).__name__,
            error_message=message,
            resolved_model_call=(
                model_result.resolved_call if model_result is not None else None
            ),
            usage_status=(model_result.usage_status if model_result else None),
            usage=(model_result.usage if model_result else None),
            estimated_input_tokens=(
                model_result.estimated_input_tokens if model_result else None
            ),
            reported_model_id=(
                model_result.reported_model_id if model_result else None
            ),
        )


def _parse_governance_output(text: str) -> MemoryGovernanceOutput:
    return MemoryGovernanceOutput.model_validate_json(_json_object_text(text))


def _json_object_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Memory governance response did not contain a JSON object")
    return stripped[start : end + 1]


def _governance_clock_observed_at_utc(context: LLMContext) -> str:
    if not context.messages:
        raise ValueError("governance prepared input lacks its runtime clock")
    semantic = context.messages[0].provider_user_carrier_semantic
    if not isinstance(semantic, RuntimeObservationWireSemanticFact) or not isinstance(
        semantic.payload, RuntimeClockObservationPayloadFact
    ):
        raise ValueError("governance prepared input lacks its exact clock authority")
    return semantic.payload.observed_at_utc


def _validate_decision_coverage(
    decisions: list[GovernanceDecision],
    *,
    expected_candidate_ids: tuple[str, ...],
) -> None:
    flattened = tuple(
        entry_id
        for decision in decisions
        for entry_id in decision_target_entry_ids(decision)
    )
    if len(flattened) != len(set(flattened)):
        raise ValueError("governance decisions target one candidate more than once")
    if frozenset(flattened) != frozenset(expected_candidate_ids):
        raise ValueError("governance decisions do not cover the prepared candidate set")


def _validate_recovery_claims(
    snapshot: GovernanceBatchInputSnapshotFact,
    current_claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
) -> None:
    original = snapshot.ordered_preparing_claims
    if len(original) != len(current_claims):
        raise ValueError("governance recovery claim count drifted")
    for before, current in zip(original, current_claims, strict=True):
        if (
            before.candidate_entry_id != current.candidate_entry_id
            or before.candidate_row_fingerprint != current.candidate_row_fingerprint
            or before.governance_batch_id != current.governance_batch_id
            or before.claim_generation != current.claim_generation
            or before.previous_claim_fingerprint != current.previous_claim_fingerprint
            or current.status
            not in {
                GovernanceCandidateClaimStatus.PREPARING,
                GovernanceCandidateClaimStatus.PREPARED,
            }
        ):
            raise ValueError("governance recovery claim identity drifted")


def _execution_context_from_snapshot(
    snapshot: GovernanceBatchInputSnapshotFact,
) -> RelatednessExecutionContext:
    allowlists: dict[str, frozenset[str]] = {}
    availability: dict[str, RelatednessAvailability] = {}
    node_revisions: dict[str, Mapping[str, int]] = {}
    verified_refs: dict[str, frozenset[str]] = {}
    candidate_by_id = {
        item.candidate_attribution.entry_id: item
        for item in snapshot.ordered_candidate_snapshots
    }
    for related in snapshot.ordered_relatedness_snapshots:
        allowlists[related.candidate_entry_id] = frozenset(
            item.canonical_memory.memory_id for item in related.ordered_candidates
        )
        availability[related.candidate_entry_id] = RelatednessAvailability(
            related.availability
        )
        node_revisions[related.candidate_entry_id] = MappingProxyType(
            {
                item.canonical_memory.memory_id: item.memory_node_revision
                for item in related.ordered_candidates
            }
        )
        candidate = candidate_by_id[related.candidate_entry_id]
        verified_refs[related.candidate_entry_id] = frozenset(
            governance_candidate_replacement_evidence_refs(candidate)
        )
    return RelatednessExecutionContext(
        governance_batch_id=snapshot.governance_batch_id,
        allowlists=MappingProxyType(allowlists),
        availability=MappingProxyType(availability),
        node_revisions=MappingProxyType(node_revisions),
        verified_evidence_refs=MappingProxyType(verified_refs),
    )


__all__ = [
    "MemoryGovernanceEngine",
    "MemoryGovernanceOptions",
    "MemoryGovernanceOutput",
    "MemoryGovernanceRunResult",
]
