"""Session-bound execution driver for post-compaction memory extraction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from hashlib import sha256
from importlib import resources
from time import monotonic
from typing import Awaitable, Callable

from pulsara_agent.event import (
    ContextCompactionMemoryExtractionRequestedEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ModelCallTerminalProjectionCommittedEvent,
    RunStartEvent,
)
from pulsara_agent.event_log.protocol import EventLog, RawStoredEventEnvelope
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.llm.derived_model_job import (
    LLMContext,
    LLMMessage,
    LLMRuntime,
    ModelLifecycleStartCommitBundle,
    PreparedModelRolloutReservation,
    ResolvedModelCall,
    RuntimeSessionModelStreamEventCommitPort,
    collect_direct_model_call_handle,
    hydrate_terminal_projection,
    prepare_model_lifecycle_start_bundle,
    stable_event_identity,
    validate_model_terminal_projection_document,
)
from pulsara_agent.memory.compaction.driver_support import (
    ArtifactStore,
    CompactionMemoryExtractionOutputError,
    CompactionMemoryExtractionRepositoryPort,
    CompactionMemoryExtractionSettlementPort,
    ExactHumanEvidenceSource,
    PARSER_CONTRACT_FINGERPRINT,
    SelectedCompactionMemoryExtractionInput,
    build_extraction_completed_event,
    build_preference_candidate_attributions,
    build_result_candidate,
    build_settlement_write_attempt,
    parse_compaction_memory_extraction_output,
    restore_selected_compaction_memory_extraction_input,
    select_compaction_memory_extraction_input,
)
from pulsara_agent.ports.model_lifecycle import (
    BackgroundModelCallAdmissionLease,
    CompactionMemoryExtractionDriverRegistry,
    ModelLifecycleTransactionCompanionFactory,
)
from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.compaction import (
    BackgroundDerivedWorkBudgetReservationFact,
    BackgroundDerivedWorkBudgetSettlementFact,
    CompactionMemoryInputBudgetFailureFact,
    CompactionMemoryExtractionModelInputAttributionFact,
    CompactionMemoryExtractionInputDocumentFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    GovernanceEvidenceArtifactReferenceFact,
    GovernanceStoredEventReferenceFact,
)
from pulsara_agent.primitives.long_horizon import (
    calculate_model_call_reservation,
    default_rollout_budget_policy,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.terminal_projection import (
    ModelTerminalProjectionPayloadFact,
    ModelTextBlockSemanticFact,
    TerminalArtifactContentReferenceFact,
    TerminalInlineContentFact,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    build_bounded_runtime_failure_diagnostic,
)
from pulsara_agent.projection_jobs.compaction_memory import (
    CompactionMemoryBackgroundBudgetExhaustedAttributionFact,
    CompactionMemoryBackgroundBudgetExhaustedResultSemanticFact,
    CompactionMemoryExtractionOccurrenceAttributionFact,
    CompactionMemoryInputBudgetUnsatisfiableAttributionFact,
    CompactionMemoryInputBudgetUnsatisfiableResultSemanticFact,
    CompactionMemoryModelResultAttributionFact,
    CompactionMemoryNoEligibleEvidenceAttributionFact,
    CompactionMemoryNoEligibleEvidenceResultSemanticFact,
    CompactionMemoryValidCandidatesResultSemanticFact,
    CompactionMemoryValidEmptyResultSemanticFact,
)
from pulsara_agent.projection_jobs.compaction_memory_policy import (
    compaction_memory_delivery_policy_from_request,
    compaction_memory_retry_delay_seconds,
)
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionCommitConfirmation,
    DurableProjectionKind,
    LeasedDurableProjectionJob,
)
from pulsara_agent.runtime.projection_jobs.compaction_budget import (
    resolve_extraction_input_budget,
)
from pulsara_agent.blocking_executor import (
    auxiliary_io_executor,
    projection_maintenance_executor,
)
from pulsara_agent.runtime.provider_input.planner import (
    PreparedProviderInputStartBundle,
)
from pulsara_agent.runtime.session import RuntimeSession


_PROMPT_PACKAGE = "pulsara_agent.memory.compaction.prompts"
_PROMPT_FILE = "memory_extraction_prompt.md"


def production_memory_extraction_prompt() -> str:
    return (
        resources.files(_PROMPT_PACKAGE)
        .joinpath(_PROMPT_FILE)
        .read_text(encoding="utf-8")
    )


_SYSTEM_PROMPT = production_memory_extraction_prompt()
_PROMPT_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-memory-extraction-prompt-contract:v1",
    _SYSTEM_PROMPT,
)
_INPUT_ARTIFACT_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-memory-extraction-input-artifact-contract:v1",
    {
        "schema": "compaction_memory_extraction_input_document.v1",
        "codec": "canonical-json-utf8",
        "content_addressed": True,
    },
)
_EXTRACTION_SEMANTIC_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-memory-extraction-semantic-contract:v1",
    {
        "output": "preference-only",
        "evidence": "verified-complete-sanitized-human-message",
        "parser": PARSER_CONTRACT_FINGERPRINT,
    },
)


SafePointAcquirer = Callable[
    [str, float], Awaitable[BackgroundModelCallAdmissionLease | None]
]


async def _run_projection(operation, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        projection_maintenance_executor(),
        partial(operation, *args, **kwargs),
    )


async def _run_auxiliary(operation, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        auxiliary_io_executor(),
        partial(operation, *args, **kwargs),
    )


async def _run_auxiliary_to_physical_exit(operation, /, *args, **kwargs):
    """Keep ownership until a deadline-bound blocking operation really exits."""

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        auxiliary_io_executor(),
        partial(operation, *args, **kwargs),
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(future)
        except Exception:
            pass
        raise


@dataclass(frozen=True, slots=True)
class _PreparedExtractionDispatch:
    request: ContextCompactionMemoryExtractionRequestedEvent
    request_reference: GovernanceStoredEventReferenceFact
    selected: SelectedCompactionMemoryExtractionInput
    input_reference: GovernanceEvidenceArtifactReferenceFact
    dispatch_ordinal: int
    operation_id: str
    call: ResolvedModelCall
    reservation: BackgroundDerivedWorkBudgetReservationFact
    context: LLMContext
    event_context: EventContext
    provider_input: PreparedProviderInputStartBundle
    start_bundle: ModelLifecycleStartCommitBundle


def _runtime_fact(cls, field_name: str, domain: str, **payload):
    payload[field_name] = context_fingerprint(domain, payload)
    return cls(**payload)


def _stored_reference(
    envelope: RawStoredEventEnvelope,
) -> GovernanceStoredEventReferenceFact:
    event = envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    return build_frozen_fact(
        GovernanceStoredEventReferenceFact,
        schema_version="governance_stored_event_reference.v1",
        stable_identity=stable_event_identity(
            event,
            runtime_session_id=envelope.runtime_session_id,
        ),
        sequence=envelope.sequence,
        stored_envelope_fingerprint=envelope.envelope_fingerprint,
    )


def _stable_call_id(job_id: str, dispatch_ordinal: int) -> str:
    value = context_fingerprint(
        "compaction-memory-extraction-model-operation:v1",
        (job_id, dispatch_ordinal),
    )
    return f"model_call:{value.removeprefix('sha256:')[:32]}"


def _reservation_id(job_id: str, dispatch_ordinal: int) -> str:
    return "background-budget-reservation:" + context_fingerprint(
        "compaction-memory-background-reservation-id:v1",
        (job_id, dispatch_ordinal),
    ).removeprefix("sha256:")


def _retry_delay_seconds(
    lease: LeasedDurableProjectionJob,
    *,
    dispatch_attempt_ordinal: int,
) -> float:
    return compaction_memory_retry_delay_seconds(
        lease.delivery_policy,
        dispatch_attempt_ordinal=dispatch_attempt_ordinal,
    )


@dataclass(slots=True)
class CompactionMemoryExtractionSessionDriver:
    runtime_session: RuntimeSession
    llm_runtime: LLMRuntime
    event_log: EventLog
    archive: ArtifactStore
    repository: CompactionMemoryExtractionRepositoryPort
    settlement_port: CompactionMemoryExtractionSettlementPort
    model_lifecycle_companion_factory: ModelLifecycleTransactionCompanionFactory
    driver_registry: CompactionMemoryExtractionDriverRegistry
    safe_point_acquirer: SafePointAcquirer
    driver_generation: int
    binding_fingerprint: str
    _accepting: bool = field(default=True, init=False)
    _owned_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    @property
    def runtime_session_id(self) -> str:
        return self.runtime_session.runtime_session_id

    async def acquire_model_safe_point(
        self,
        *,
        operation_id: str,
        deadline_monotonic: float,
    ) -> BackgroundModelCallAdmissionLease | None:
        return await self.safe_point_acquirer(
            operation_id,
            deadline_monotonic,
        )

    async def execute_leased_job(
        self,
        job: object,
        *,
        deadline_monotonic: float,
    ) -> None:
        if not isinstance(job, LeasedDurableProjectionJob):
            raise TypeError("extraction driver requires a leased projection job")
        if not self._accepting:
            await _run_projection(
                self.repository.release_session_model_lease_without_attempt,
                job,
                reason="driver_busy",
                deadline_monotonic=min(deadline_monotonic, monotonic() + 10.0),
            )
            return
        task = asyncio.create_task(
            self._execute_owned(job, deadline_monotonic=deadline_monotonic),
            name=f"pulsara-compaction-memory-extraction:{job.job.job_id}",
        )
        self._owned_tasks.add(task)
        task.add_done_callback(self._owned_tasks.discard)
        await asyncio.shield(task)

    async def settle_result_candidate(
        self,
        result_candidate: object,
        *,
        settlement_generation: int,
        deadline_monotonic: float,
    ) -> None:
        from pulsara_agent.projection_jobs.compaction_memory import (
            CompactionMemoryExtractionResultCandidateFact,
        )

        if not isinstance(
            result_candidate, CompactionMemoryExtractionResultCandidateFact
        ):
            raise TypeError("extraction driver received an invalid result candidate")
        attempt = build_settlement_write_attempt(
            result_candidate=result_candidate,
            settlement_generation=settlement_generation,
            deadline_monotonic=deadline_monotonic,
        )
        await self.settlement_port.commit_result(
            result_candidate=result_candidate,
            write_attempt=attempt,
        )

    def stop_admission(self) -> None:
        self._accepting = False

    async def close(self, *, deadline_monotonic: float) -> None:
        self.stop_admission()
        while self.driver_registry.active_borrow_count(self.runtime_session_id):
            if monotonic() >= deadline_monotonic:
                raise TimeoutError("extraction driver borrows did not drain")
            await asyncio.sleep(0.01)
        pending = tuple(self._owned_tasks)
        if pending:
            _done, active = await asyncio.wait(
                pending,
                timeout=max(0.0, deadline_monotonic - monotonic()),
            )
            if active:
                raise TimeoutError("extraction model operations did not drain")
        await self.settlement_port.drain(deadline_monotonic=deadline_monotonic)
        await _run_projection(
            self.repository.supersede_unstarted_compaction_memory_jobs,
            runtime_session_id=self.runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        await self._drain_close_settlements(deadline_monotonic=deadline_monotonic)

    async def _drain_close_settlements(self, *, deadline_monotonic: float) -> None:
        while monotonic() < deadline_monotonic:
            remaining = deadline_monotonic - monotonic()
            claims = await _run_projection(
                self.repository.claim_compaction_memory_settlements,
                runtime_session_ids=(self.runtime_session_id,),
                limit=4,
                bypass_retry_not_before=True,
                reclaim_active_writing=True,
                settlement_attempt_seconds=max(0.1, min(20.0, remaining)),
                deadline_monotonic=deadline_monotonic,
            )
            if not claims:
                await self.settlement_port.drain(
                    deadline_monotonic=deadline_monotonic
                )
                return
            for claim in claims:
                await self.settle_result_candidate(
                    claim.result_candidate,
                    settlement_generation=claim.state.settlement_generation,
                    deadline_monotonic=deadline_monotonic,
                )
            await asyncio.sleep(0.01)
        raise TimeoutError("extraction settlement close maintenance did not drain")

    async def _execute_owned(
        self,
        lease: LeasedDurableProjectionJob,
        *,
        deadline_monotonic: float,
    ) -> None:
        if await self._recover_existing_attempt(
            lease,
            deadline_monotonic=deadline_monotonic,
        ):
            return
        prepared = await self._prepare_dispatch(
            lease,
            deadline_monotonic=deadline_monotonic,
        )
        if prepared is None:
            return
        try:
            safe_point = await self.acquire_model_safe_point(
                operation_id=prepared.operation_id,
                deadline_monotonic=deadline_monotonic,
            )
        except BaseException:
            await self.runtime_session.provider_input_generation_coordinator.abandon_uncommitted_preparation(
                prepared.provider_input.prepared_candidate.preparation_ownership.preparation_id,
                reason="compaction_memory_extraction_safe_point_failed",
            )
            raise
        if safe_point is None:
            try:
                await self.runtime_session.provider_input_generation_coordinator.abandon_uncommitted_preparation(
                    prepared.provider_input.prepared_candidate.preparation_ownership.preparation_id,
                    reason="compaction_memory_extraction_safe_point_unavailable",
                )
            finally:
                await _run_projection(
                    self.repository.release_session_model_lease_without_attempt,
                    lease,
                    reason="safe_point_stale",
                    deadline_monotonic=min(
                        deadline_monotonic, monotonic() + 10.0
                    ),
                )
            return
        try:
            await self._dispatch_prepared(
                lease,
                prepared=prepared,
                safe_point=safe_point,
                deadline_monotonic=deadline_monotonic,
            )
        finally:
            safe_point.release()

    async def _recover_existing_attempt(
        self,
        lease: LeasedDurableProjectionJob,
        *,
        deadline_monotonic: float,
    ) -> bool:
        existing_candidate = await _run_projection(
            self.repository.read_compaction_memory_result_candidate,
            lease.job.job_id,
            deadline_monotonic=min(deadline_monotonic, monotonic() + 10.0),
        )
        if existing_candidate is not None:
            return True
        dispatch_ordinal = lease.dispatch_attempt_count
        if dispatch_ordinal == 0:
            return False
        resolved_model_call_id = _stable_call_id(lease.job.job_id, dispatch_ordinal)
        start_id = f"model_call_start:{resolved_model_call_id}"
        end_id = f"model_call_end:{resolved_model_call_id}"
        start, end = await _run_projection(
            lambda: (
                self.event_log.get_by_id(start_id),
                self.event_log.get_by_id(end_id),
            )
        )
        if not isinstance(start, ModelCallStartEvent):
            raise ValueError(
                "durable extraction dispatch ordinal lacks its ModelCallStart"
            )
        attribution = start.compaction_memory_extraction_input_attribution
        if (
            start.resolved_call.resolved_model_call_id != resolved_model_call_id
            or start.resolved_call.purpose
            is not ModelCallPurpose.COMPACTION_MEMORY_EXTRACTION
            or attribution is None
            or attribution.extraction_job_id != lease.job.job_id
            or attribution.dispatch_attempt_ordinal != dispatch_ordinal
        ):
            raise ValueError("recovered extraction ModelCallStart authority drifted")
        if end is None:
            await self._defer_recovered_attempt(
                lease,
                reason="compaction memory extraction ModelCallStart awaits recovery",
                delay_seconds=1.0,
                deadline_monotonic=deadline_monotonic,
            )
            return True
        if not isinstance(end, ModelCallEndEvent):
            raise ValueError("recovered extraction terminal event type drifted")
        if end.resolved_model_call_id != resolved_model_call_id:
            raise ValueError("recovered extraction ModelCallEnd identity drifted")
        if end.outcome != "completed":
            await self._defer_recovered_attempt(
                lease,
                reason=f"compaction memory extraction ended with {end.outcome}",
                delay_seconds=_retry_delay_seconds(
                    lease,
                    dispatch_attempt_ordinal=dispatch_ordinal,
                ),
                deadline_monotonic=deadline_monotonic,
            )
            return True
        request, request_reference = await _run_projection(self._request, lease)
        selected = await self._restore_input_document(
            attribution.input_artifact_reference,
            deadline_monotonic=deadline_monotonic,
        )
        if (
            selected.document.attribution.request_event_reference != request_reference
            or selected.document.attribution.durable_job_id != lease.job.job_id
            or selected.document.semantic.input_semantic_fingerprint
            != attribution.input_semantic_fingerprint
            or selected.document.document_fingerprint
            != attribution.input_document_fingerprint
        ):
            raise ValueError("recovered extraction input authority drifted")
        terminal_text = await self._hydrate_completed_terminal_text(
            start=start,
            end=end,
            deadline_monotonic=deadline_monotonic,
        )
        reservation, settlement = await _run_projection(
            self.repository.read_background_budget_terminal_authority,
            reservation_id=attribution.background_budget_reservation.reservation_id,
            extraction_job_id=lease.job.job_id,
            resolved_model_call_id=resolved_model_call_id,
            dispatch_attempt_ordinal=dispatch_ordinal,
            deadline_monotonic=min(deadline_monotonic, monotonic() + 10.0),
        )
        if reservation != attribution.background_budget_reservation:
            raise ValueError("recovered extraction budget reservation drifted")
        try:
            await self._install_completed_model_result(
                lease=lease,
                request=request,
                request_reference=request_reference,
                selected=selected,
                input_reference=attribution.input_artifact_reference,
                model_start=start,
                model_end=end,
                model_text=terminal_text,
                dispatch_ordinal=dispatch_ordinal,
                reservation=reservation,
                settlement=settlement,
            )
        except CompactionMemoryExtractionOutputError:
            await self._defer_recovered_attempt(
                lease,
                reason="compaction memory extraction output contract failed",
                delay_seconds=_retry_delay_seconds(
                    lease,
                    dispatch_attempt_ordinal=dispatch_ordinal,
                ),
                deadline_monotonic=deadline_monotonic,
            )
        return True

    async def _defer_recovered_attempt(
        self,
        lease: LeasedDurableProjectionJob,
        *,
        reason: str,
        delay_seconds: float,
        deadline_monotonic: float,
    ) -> None:
        diagnostic = build_bounded_runtime_failure_diagnostic(
            error=RuntimeError(reason),
            redaction_profile_id="durable_projection_job_error.v1",
        )
        confirmation = await _run_projection(
            self.repository.defer_recovered_session_model_attempt,
            lease,
            failure=diagnostic,
            delay_seconds=delay_seconds,
            deadline_monotonic=min(deadline_monotonic, monotonic() + 10.0),
        )
        if confirmation is not DurableProjectionCommitConfirmation.FULL:
            raise RuntimeError(
                f"recovered extraction attempt deferral was {confirmation.value}"
            )

    async def _prepare_dispatch(
        self,
        lease: LeasedDurableProjectionJob,
        *,
        deadline_monotonic: float,
    ) -> _PreparedExtractionDispatch | None:
        request, request_reference = await _run_projection(self._request, lease)
        target = self.llm_runtime.rebind_target(request.extraction_policy.model_target)
        input_budget = resolve_extraction_input_budget(
            target=target.fact,
            static_prompt_tokens=target.token_estimator.estimate_text(_SYSTEM_PROMPT),
        )
        selected = await _run_projection(
            select_compaction_memory_extraction_input,
            runtime_session_id=self.runtime_session_id,
            compaction_id=request.extension_link.compaction_id,
            extension_link=request.extension_link,
            request_event_reference=request_reference,
            durable_job_id=lease.job.job_id,
            durable_job_source_reference=lease.job.source_event_reference,
            manifest_reference=request.human_evidence_manifest_reference,
            archive=self.archive,
            exact_source_resolver=self._resolve_human_source,
            resolved_budget=input_budget.budget,
            token_estimator=target.token_estimator.estimate_text,
            prompt_contract_fingerprint=_PROMPT_CONTRACT_FINGERPRINT,
            extraction_contract_fingerprint=(
                request.extraction_contract.contract_fingerprint
            ),
            deadline_monotonic=deadline_monotonic,
        )
        if input_budget.failure is not None:
            semantic = build_frozen_fact(
                CompactionMemoryInputBudgetUnsatisfiableResultSemanticFact,
                schema_version=(
                    "compaction_memory_input_budget_unsatisfiable_result_semantic.v1"
                ),
                outcome_kind="input_budget_unsatisfiable",
                failure_kind=input_budget.failure.failure_kind,
                evidence_set_semantic_fingerprint=(
                    selected.document.semantic.evidence_set.evidence_set_semantic_fingerprint
                ),
                budget_selection_contract_fingerprint=(
                    input_budget.budget.budget_selection_contract_fingerprint
                ),
                extraction_semantic_contract_fingerprint=(
                    _EXTRACTION_SEMANTIC_CONTRACT_FINGERPRINT
                ),
            )
            attribution = build_frozen_fact(
                CompactionMemoryInputBudgetUnsatisfiableAttributionFact,
                schema_version=(
                    "compaction_memory_input_budget_unsatisfiable_attribution.v1"
                ),
                outcome_kind="input_budget_unsatisfiable",
                resolved_input_budget=input_budget.budget,
                budget_failure=input_budget.failure,
            )
            await self._install_result(
                lease=lease,
                request=request,
                request_reference=request_reference,
                selected=selected,
                result_semantic=semantic,
                outcome_attribution=attribution,
                created_at_utc=request.created_at,
            )
            return
        if not selected.ordered_nodes and selected.source_eligible_leaf_count:
            budget_failure = build_frozen_fact(
                CompactionMemoryInputBudgetFailureFact,
                schema_version="compaction_memory_input_budget_failure.v1",
                failure_kind="no_complete_evidence_message_fits",
                resolved_budget_attribution_fingerprint=(
                    input_budget.budget.attribution_fingerprint
                ),
            )
            semantic = build_frozen_fact(
                CompactionMemoryInputBudgetUnsatisfiableResultSemanticFact,
                schema_version=(
                    "compaction_memory_input_budget_unsatisfiable_result_semantic.v1"
                ),
                outcome_kind="input_budget_unsatisfiable",
                failure_kind=budget_failure.failure_kind,
                evidence_set_semantic_fingerprint=(
                    selected.document.semantic.evidence_set.evidence_set_semantic_fingerprint
                ),
                budget_selection_contract_fingerprint=(
                    input_budget.budget.budget_selection_contract_fingerprint
                ),
                extraction_semantic_contract_fingerprint=(
                    _EXTRACTION_SEMANTIC_CONTRACT_FINGERPRINT
                ),
            )
            attribution = build_frozen_fact(
                CompactionMemoryInputBudgetUnsatisfiableAttributionFact,
                schema_version=(
                    "compaction_memory_input_budget_unsatisfiable_attribution.v1"
                ),
                outcome_kind="input_budget_unsatisfiable",
                resolved_input_budget=input_budget.budget,
                budget_failure=budget_failure,
            )
            await self._install_result(
                lease=lease,
                request=request,
                request_reference=request_reference,
                selected=selected,
                result_semantic=semantic,
                outcome_attribution=attribution,
                created_at_utc=request.created_at,
            )
            return
        if not selected.ordered_nodes:
            semantic = build_frozen_fact(
                CompactionMemoryNoEligibleEvidenceResultSemanticFact,
                schema_version="compaction_memory_no_eligible_result_semantic.v1",
                outcome_kind="no_eligible_evidence",
                evidence_set_semantic_fingerprint=(
                    selected.document.semantic.evidence_set.evidence_set_semantic_fingerprint
                ),
                extraction_semantic_contract_fingerprint=(
                    _EXTRACTION_SEMANTIC_CONTRACT_FINGERPRINT
                ),
            )
            attribution = build_frozen_fact(
                CompactionMemoryNoEligibleEvidenceAttributionFact,
                schema_version="compaction_memory_no_eligible_attribution.v1",
                outcome_kind="no_eligible_evidence",
            )
            await self._install_result(
                lease=lease,
                request=request,
                request_reference=request_reference,
                selected=selected,
                result_semantic=semantic,
                outcome_attribution=attribution,
                created_at_utc=request.created_at,
            )
            return

        input_reference = await self._persist_input(
            selected,
            deadline_monotonic=deadline_monotonic,
        )
        dispatch_ordinal = lease.dispatch_attempt_count + 1
        operation_id = context_fingerprint(
            "compaction-memory-extraction-model-operation:v1",
            (lease.job.job_id, dispatch_ordinal),
        )
        call = self.llm_runtime.resolve_call(
            target=target,
            purpose=ModelCallPurpose.COMPACTION_MEMORY_EXTRACTION,
            resolved_model_call_id=_stable_call_id(lease.job.job_id, dispatch_ordinal),
        )
        quote = calculate_model_call_reservation(
            target=target.fact,
            resolved_model_call_id=call.fact.resolved_model_call_id,
            policy=default_rollout_budget_policy(),
        )
        reserve = await _run_projection(
            self.repository.prepare_background_budget_reservation,
            runtime_session_id=self.runtime_session_id,
            reservation_id=_reservation_id(lease.job.job_id, dispatch_ordinal),
            extraction_job_id=lease.job.job_id,
            operation_id=operation_id,
            dispatch_attempt_ordinal=dispatch_ordinal,
            quote=quote,
            deadline_monotonic=min(deadline_monotonic, monotonic() + 10.0),
        )
        if reserve.failure is not None:
            semantic = build_frozen_fact(
                CompactionMemoryBackgroundBudgetExhaustedResultSemanticFact,
                schema_version=(
                    "compaction_memory_background_budget_exhausted_result_semantic.v1"
                ),
                outcome_kind="background_budget_exhausted",
                input_semantic_fingerprint=(
                    selected.document.semantic.input_semantic_fingerprint
                ),
                evidence_set_semantic_fingerprint=(
                    selected.document.semantic.evidence_set.evidence_set_semantic_fingerprint
                ),
                exhaustion_kind=reserve.failure.failure_kind,
                extraction_semantic_contract_fingerprint=(
                    _EXTRACTION_SEMANTIC_CONTRACT_FINGERPRINT
                ),
            )
            attribution = build_frozen_fact(
                CompactionMemoryBackgroundBudgetExhaustedAttributionFact,
                schema_version=(
                    "compaction_memory_background_budget_exhausted_attribution.v1"
                ),
                outcome_kind="background_budget_exhausted",
                input_artifact_reference=input_reference,
                input_semantic_fingerprint=(
                    selected.document.semantic.input_semantic_fingerprint
                ),
                resolved_input_budget=input_budget.budget,
                rejected_reservation_quote=quote,
                budget_admission_failure=reserve.failure,
            )
            await self._install_result(
                lease=lease,
                request=request,
                request_reference=request_reference,
                selected=selected,
                result_semantic=semantic,
                outcome_attribution=attribution,
                created_at_utc=request.created_at,
            )
            return
        reservation = reserve.reservation
        assert reservation is not None
        model_input_attribution = build_frozen_fact(
            CompactionMemoryExtractionModelInputAttributionFact,
            schema_version=("compaction_memory_extraction_model_input_attribution.v1"),
            extraction_job_id=lease.job.job_id,
            dispatch_attempt_ordinal=dispatch_ordinal,
            request_event_reference=request_reference,
            input_artifact_reference=input_reference,
            input_semantic_fingerprint=(
                selected.document.semantic.input_semantic_fingerprint
            ),
            input_document_fingerprint=selected.document.document_fingerprint,
            resolved_input_budget_attribution_fingerprint=(
                input_budget.budget.attribution_fingerprint
            ),
            background_budget_reservation=reservation,
            extraction_contract_fingerprint=(
                request.extraction_contract.contract_fingerprint
            ),
        )
        context = LLMContext(
            system_prompt=_SYSTEM_PROMPT,
            messages=(
                LLMMessage.runtime_request(
                    selected.canonical_input_utf8,
                    request_kind="compaction_memory_extraction_request",
                    business_occurrence_semantic_fingerprint=(
                        selected.document.document_fingerprint
                    ),
                ),
            ),
            tools=(),
            context_id=f"context:compaction-memory:{operation_id}",
            resolved_model_call_id=call.fact.resolved_model_call_id,
            target_fingerprint=target.fact.target_fingerprint,
            model_call_index=None,
        )
        event_context = EventContext(
            run_id=request.run_id,
            turn_id=request.turn_id,
            reply_id=f"{request.reply_id}:compaction-memory-extraction",
        )
        provider_input = await self.runtime_session.provider_input_generation_coordinator.prepare_one_shot_call(
            call=call,
            context=context,
            event_context=event_context,
            operation_kind="compaction_memory_extraction_model_call",
            operation_id=operation_id,
            attempt_index=dispatch_ordinal,
            deadline_monotonic=deadline_monotonic,
        )
        try:
            context = provider_input.carrier.to_llm_context(context)
            start_bundle = prepare_model_lifecycle_start_bundle(
                call=call,
                context=context,
                event_context=event_context,
                runtime_session=self.runtime_session,
                lifecycle_kind="direct_internal_call",
                prepared_rollout_reservation=PreparedModelRolloutReservation(
                    reservation=None,
                    accounting_mode="not_rollout_accounted",
                    expected_account_state_fingerprint=None,
                ),
                compaction_memory_extraction_input_attribution=(
                    model_input_attribution
                ),
                provider_input_start_bundle=provider_input,
            )
        except BaseException:
            await self.runtime_session.provider_input_generation_coordinator.abandon_uncommitted_preparation(
                provider_input.prepared_candidate.preparation_ownership.preparation_id,
                reason="compaction_memory_extraction_start_bundle_failed",
            )
            raise
        return _PreparedExtractionDispatch(
            request=request,
            request_reference=request_reference,
            selected=selected,
            input_reference=input_reference,
            dispatch_ordinal=dispatch_ordinal,
            operation_id=operation_id,
            call=call,
            reservation=reservation,
            context=context,
            event_context=event_context,
            provider_input=provider_input,
            start_bundle=start_bundle,
        )

    async def _dispatch_prepared(
        self,
        lease: LeasedDurableProjectionJob,
        *,
        prepared: _PreparedExtractionDispatch,
        safe_point: BackgroundModelCallAdmissionLease,
        deadline_monotonic: float,
    ) -> None:
        request = prepared.request
        request_reference = prepared.request_reference
        selected = prepared.selected
        input_reference = prepared.input_reference
        dispatch_ordinal = prepared.dispatch_ordinal
        call = prepared.call
        reservation = prepared.reservation
        context = prepared.context
        event_context = prepared.event_context
        provider_input = prepared.provider_input
        start_bundle = prepared.start_bundle
        start_companion, terminal_companion = self.model_lifecycle_companion_factory(
            lease=lease,
            reservation=reservation,
            admission_lease=safe_point,
            model_call_start_event_id=(
                start_bundle.recovery_plan.model_call_start_event_id
            ),
            model_call_end_event_id=(
                start_bundle.recovery_plan.stable_model_call_end_event_id
            ),
        )
        try:
            safe_point.begin_model_start()
            handle = self.llm_runtime.start_stream(
                call=call,
                context=context,
                event_context=event_context,
                start_bundle=start_bundle,
                commit_port=RuntimeSessionModelStreamEventCommitPort(
                    runtime_session=self.runtime_session,
                    start_transaction_companion=start_companion,
                    terminal_transaction_companion=terminal_companion,
                ),
                execution_registry=self.runtime_session.model_stream_execution_registry,
            )
        except BaseException:
            await self.runtime_session.provider_input_generation_coordinator.abandon_uncommitted_preparation(
                provider_input.prepared_candidate.preparation_ownership.preparation_id,
                reason="compaction_memory_extraction_failed_before_start",
            )
            raise
        direct_result = await self._collect_after_start(
            handle=handle,
            call=call,
            safe_point=safe_point,
        )
        model_end = await _run_projection(
            self.event_log.get_by_id,
            direct_result.model_call_end_event_identity.event_id,
        )
        if not isinstance(model_end, ModelCallEndEvent):
            raise ValueError("extraction model End is unavailable")
        if direct_result.outcome != "completed":
            await self._defer_failed_model_attempt(
                lease,
                dispatch_attempt_ordinal=dispatch_ordinal,
                failure_message="compaction memory extraction provider failed",
                transition_name="provider",
                deadline_monotonic=deadline_monotonic,
            )
            return
        if terminal_companion.settlement is None:
            raise ValueError("extraction terminal lacks budget settlement")
        model_start = await _run_projection(
            self.event_log.get_by_id,
            start_bundle.recovery_plan.model_call_start_event_id,
        )
        if not isinstance(model_start, ModelCallStartEvent):
            raise ValueError("extraction model Start is unavailable after FULL")
        try:
            await self._install_completed_model_result(
                lease=lease,
                request=request,
                request_reference=request_reference,
                selected=selected,
                input_reference=input_reference,
                model_start=model_start,
                model_end=model_end,
                model_text=direct_result.text,
                dispatch_ordinal=dispatch_ordinal,
                reservation=reservation,
                settlement=terminal_companion.settlement,
            )
        except CompactionMemoryExtractionOutputError:
            await self._defer_failed_model_attempt(
                lease,
                dispatch_attempt_ordinal=dispatch_ordinal,
                failure_message=(
                    "compaction memory extraction output contract failed"
                ),
                transition_name="output",
                deadline_monotonic=deadline_monotonic,
            )

    async def _defer_failed_model_attempt(
        self,
        lease: LeasedDurableProjectionJob,
        *,
        dispatch_attempt_ordinal: int,
        failure_message: str,
        transition_name: str,
        deadline_monotonic: float,
    ) -> None:
        failure = build_bounded_runtime_failure_diagnostic(
            error=RuntimeError(failure_message),
            redaction_profile_id="durable_projection_job_error.v1",
        )
        confirmation = await _run_projection(
            self.repository.defer_session_model_job_after_attempt,
            lease,
            failure=failure,
            delay_seconds=_retry_delay_seconds(
                lease,
                dispatch_attempt_ordinal=dispatch_attempt_ordinal,
            ),
            deadline_monotonic=min(deadline_monotonic, monotonic() + 10.0),
        )
        if confirmation is not DurableProjectionCommitConfirmation.FULL:
            raise RuntimeError(
                f"extraction {transition_name} retry transition was "
                f"{confirmation.value}"
            )

    async def _install_completed_model_result(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        request: ContextCompactionMemoryExtractionRequestedEvent,
        request_reference: GovernanceStoredEventReferenceFact,
        selected: SelectedCompactionMemoryExtractionInput,
        input_reference: GovernanceEvidenceArtifactReferenceFact,
        model_start: ModelCallStartEvent,
        model_end: ModelCallEndEvent,
        model_text: str,
        dispatch_ordinal: int,
        reservation: BackgroundDerivedWorkBudgetReservationFact,
        settlement: BackgroundDerivedWorkBudgetSettlementFact,
    ) -> None:
        parsed = parse_compaction_memory_extraction_output(
            model_text,
            allowed_evidence_node_ids=tuple(
                node.evidence_node_id for node in selected.ordered_nodes
            ),
        )
        candidate_attributions = build_preference_candidate_attributions(
            parsed=parsed,
            nodes=selected.ordered_nodes,
            scope=request.resolved_scope,
            job_id=lease.job.job_id,
            request_event_id=request.id,
            extraction_contract_fingerprint=(
                request.extraction_contract.contract_fingerprint
            ),
            created_at_utc=model_end.created_at,
        )
        outcome_kind = "valid_candidates" if candidate_attributions else "valid_empty"
        common = {
            "outcome_kind": outcome_kind,
            "input_semantic_fingerprint": (
                selected.document.semantic.input_semantic_fingerprint
            ),
            "evidence_set_semantic_fingerprint": (
                selected.document.semantic.evidence_set.evidence_set_semantic_fingerprint
            ),
            "terminal_projection_semantic_fingerprint": (
                model_end.terminal_projection.projection_reference.semantic_join.semantic_fingerprint
            ),
            "parser_contract_fingerprint": PARSER_CONTRACT_FINGERPRINT,
            "extraction_semantic_contract_fingerprint": (
                _EXTRACTION_SEMANTIC_CONTRACT_FINGERPRINT
            ),
        }
        if candidate_attributions:
            result_semantic = build_frozen_fact(
                CompactionMemoryValidCandidatesResultSemanticFact,
                schema_version="compaction_memory_valid_candidates_result_semantic.v1",
                **common,
                ordered_candidate_semantic_fingerprints=tuple(
                    sorted(
                        item.candidate_payload.candidate_semantic_fingerprint
                        for item in candidate_attributions
                    )
                ),
            )
        else:
            result_semantic = build_frozen_fact(
                CompactionMemoryValidEmptyResultSemanticFact,
                schema_version="compaction_memory_valid_empty_result_semantic.v1",
                **common,
            )
        model_start_reference, model_end_reference = await _run_projection(
            lambda: (
                self._event_reference(model_start.id),
                self._event_reference(model_end.id),
            )
        )
        outcome_attribution = build_frozen_fact(
            CompactionMemoryModelResultAttributionFact,
            schema_version="compaction_memory_model_result_attribution.v1",
            outcome_kind=outcome_kind,
            input_artifact_reference=input_reference,
            input_semantic_fingerprint=(
                selected.document.semantic.input_semantic_fingerprint
            ),
            resolved_input_budget=(
                selected.document.attribution.resolved_input_budget_attribution
            ),
            model_call_start_event_reference=model_start_reference,
            model_call_end_event_reference=model_end_reference,
            model_terminal_projection_reference=(
                model_end.terminal_projection.projection_reference
            ),
            parsed_output_semantic_fingerprint=parsed.semantic_fingerprint,
            dispatch_attempt_ordinal=dispatch_ordinal,
            background_budget_reservation=reservation,
            background_budget_settlement=settlement,
        )
        await self._install_result(
            lease=lease,
            request=request,
            request_reference=request_reference,
            selected=selected,
            result_semantic=result_semantic,
            outcome_attribution=outcome_attribution,
            created_at_utc=model_end.created_at,
            candidate_attributions=candidate_attributions,
        )

    async def _restore_input_document(
        self,
        reference: GovernanceEvidenceArtifactReferenceFact,
        *,
        deadline_monotonic: float,
    ) -> SelectedCompactionMemoryExtractionInput:
        text = await _run_auxiliary(
            self.archive.get_text,
            reference.artifact_id,
            session_id=self.runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        encoded = text.encode("utf-8")
        if (
            len(encoded) != reference.content_bytes
            or sha256(encoded).hexdigest() != reference.content_sha256
            or reference.artifact_kind != "compaction_memory_extraction_input"
            or reference.media_type != "application/json"
            or reference.artifact_contract_fingerprint
            != _INPUT_ARTIFACT_CONTRACT_FINGERPRINT
        ):
            raise ValueError("extraction input artifact authority drifted")
        document = CompactionMemoryExtractionInputDocumentFact.model_validate_json(text)
        return restore_selected_compaction_memory_extraction_input(document)

    async def _hydrate_completed_terminal_text(
        self,
        *,
        start: ModelCallStartEvent,
        end: ModelCallEndEvent,
        deadline_monotonic: float,
    ) -> str:
        committed_id = (
            end.terminal_projection.projection_committed_event_identity.event_id
        )
        committed = await _run_projection(self.event_log.get_by_id, committed_id)
        if not isinstance(committed, ModelCallTerminalProjectionCommittedEvent):
            raise ValueError("extraction terminal projection event is unavailable")
        document = await hydrate_terminal_projection(
            self.runtime_session,
            end.terminal_projection.projection_reference,
            deadline_monotonic=deadline_monotonic,
        )
        validate_model_terminal_projection_document(
            runtime_session_id=self.runtime_session_id,
            start=start,
            committed=committed,
            end=end,
            document=document,
        )
        if not isinstance(document.payload, ModelTerminalProjectionPayloadFact):
            raise ValueError("extraction terminal projection payload kind drifted")
        parts: list[str] = []
        for item in document.payload.items:
            if not isinstance(item.semantic_identity, ModelTextBlockSemanticFact):
                continue
            content = item.content
            if isinstance(content, TerminalInlineContentFact):
                parts.append(content.text)
                continue
            if not isinstance(content, TerminalArtifactContentReferenceFact):
                raise ValueError("extraction text block content is unavailable")
            text = await _run_auxiliary(
                self.archive.get_text,
                content.artifact_id,
                session_id=self.runtime_session_id,
                deadline_monotonic=deadline_monotonic,
            )
            encoded = text.encode("utf-8")
            if (
                len(encoded) != content.artifact_bytes
                or f"sha256:{sha256(encoded).hexdigest()}" != content.artifact_sha256
            ):
                raise ValueError("extraction terminal content artifact drifted")
            parts.append(text)
        return "".join(parts)

    async def _collect_after_start(self, *, handle, call, safe_point):
        subscription = handle.subscribe()
        collect_task = asyncio.create_task(
            collect_direct_model_call_handle(
                handle,
                expected_call=call,
                runtime_session_id=self.runtime_session_id,
            )
        )
        try:
            async with subscription:
                async for event in subscription:
                    if isinstance(event, ModelCallStartEvent):
                        safe_point.confirm_model_start_full()
                        break
            return await collect_task
        except BaseException:
            if not collect_task.done():
                collect_task.cancel()
            raise

    async def _install_result(
        self,
        *,
        lease: LeasedDurableProjectionJob,
        request: ContextCompactionMemoryExtractionRequestedEvent,
        request_reference: GovernanceStoredEventReferenceFact,
        selected: SelectedCompactionMemoryExtractionInput,
        result_semantic,
        outcome_attribution,
        created_at_utc: str,
        candidate_attributions=(),
    ) -> None:
        occurrence = build_frozen_fact(
            CompactionMemoryExtractionOccurrenceAttributionFact,
            schema_version=("compaction_memory_extraction_occurrence_attribution.v1"),
            compaction_id=request.extension_link.compaction_id,
            extension_link=request.extension_link,
            request_event_reference=request_reference,
            durable_job_id=lease.job.job_id,
            durable_job_source_reference=lease.job.source_event_reference,
            human_evidence_manifest_reference=(
                request.human_evidence_manifest_reference
            ),
            outcome_attribution=outcome_attribution,
        )
        event = build_extraction_completed_event(
            runtime_session_id=self.runtime_session_id,
            event_context=EventContext(
                run_id=request.run_id,
                turn_id=request.turn_id,
                reply_id=request.reply_id,
            ),
            created_at_utc=created_at_utc,
            lease=lease,
            result_semantic=result_semantic,
            occurrence_attribution=occurrence,
            candidate_attributions=tuple(candidate_attributions),
        )
        head = await _run_projection(
            self.repository.read_head,
            DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION,
            lease.job.target_key,
            deadline_monotonic=monotonic() + 10.0,
        )
        candidate = build_result_candidate(
            runtime_session_id=self.runtime_session_id,
            lease=lease,
            event=event,
            intended_target_head_revision=1 if head is None else head.head_revision + 1,
            expected_target_head_fingerprint=(
                head.head_fingerprint if head is not None else None
            ),
            permanent_automatic_omission_count=(selected.permanent_omission_count),
            permanent_automatic_omission_semantic_accumulator=(
                selected.permanent_omission_semantic_accumulator
            ),
            permanent_automatic_omission_attribution_accumulator=(
                selected.permanent_omission_attribution_accumulator
            ),
        )
        installation_guard = await _run_projection(
            self.repository.prepare_compaction_memory_result_installation_guard,
            lease=lease,
            result_candidate=candidate,
            deadline_monotonic=monotonic() + 10.0,
        )
        confirmation = await _run_projection(
            self.repository.install_compaction_memory_result_candidate,
            lease=lease,
            result_candidate=candidate,
            installation_guard=installation_guard,
            deadline_monotonic=monotonic() + 10.0,
        )
        if confirmation is not DurableProjectionCommitConfirmation.FULL:
            raise RuntimeError(
                f"extraction RESULT_READY installation was {confirmation.value}"
            )

    async def _persist_input(
        self,
        selected: SelectedCompactionMemoryExtractionInput,
        *,
        deadline_monotonic: float,
    ) -> GovernanceEvidenceArtifactReferenceFact:
        content = canonical_json_bytes(
            selected.document.model_dump(mode="json")
        ).decode("utf-8")
        encoded = content.encode("utf-8")
        digest = sha256(encoded).hexdigest()
        artifact_id = f"compaction-memory-extraction-input:{digest}"
        await _run_auxiliary_to_physical_exit(
            self.archive.put_text_if_absent_or_confirm_identical,
            artifact_id,
            content,
            session_id=self.runtime_session_id,
            run_id=None,
            media_type="application/json",
            semantic_metadata={
                "input_document_fingerprint": (selected.document.document_fingerprint)
            },
            deadline_monotonic=deadline_monotonic,
        )
        return build_frozen_fact(
            GovernanceEvidenceArtifactReferenceFact,
            schema_version="governance_evidence_artifact_reference.v1",
            artifact_kind="compaction_memory_extraction_input",
            artifact_id=artifact_id,
            media_type="application/json",
            content_sha256=digest,
            content_bytes=len(encoded),
            artifact_contract_id="pulsara.compaction-memory-extraction.input",
            artifact_contract_version="1",
            artifact_contract_fingerprint=(_INPUT_ARTIFACT_CONTRACT_FINGERPRINT),
        )

    def _request(
        self,
        lease: LeasedDurableProjectionJob,
    ) -> tuple[
        ContextCompactionMemoryExtractionRequestedEvent,
        GovernanceStoredEventReferenceFact,
    ]:
        source = lease.job.source_event_reference
        raw = self.event_log.read_raw_events_by_id((source.event_id,))
        if len(raw) != 1:
            raise ValueError("extraction Request source is unavailable")
        envelope = raw[0]
        if (
            envelope.runtime_session_id != self.runtime_session_id
            or envelope.sequence != source.sequence
            or envelope.payload_fingerprint != source.payload_fingerprint
        ):
            raise ValueError("extraction Request source authority drifted")
        event = envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if not isinstance(event, ContextCompactionMemoryExtractionRequestedEvent):
            raise TypeError("extraction job source is not a Request event")
        delivery_policy = compaction_memory_delivery_policy_from_request(
            event.extraction_policy
        )
        if delivery_policy != lease.delivery_policy:
            raise ValueError("extraction Request/job delivery policy drifted")
        return event, _stored_reference(envelope)

    def _event_reference(self, event_id: str) -> GovernanceStoredEventReferenceFact:
        raw = self.event_log.read_raw_events_by_id((event_id,))
        if len(raw) != 1:
            raise ValueError(f"event authority is unavailable: {event_id}")
        return _stored_reference(raw[0])

    def _resolve_human_source(self, reference) -> ExactHumanEvidenceSource:
        raw = self.event_log.read_raw_events_by_id((reference.event_id,))
        if len(raw) != 1:
            raise ValueError("human evidence source event is unavailable")
        envelope = raw[0]
        if (
            envelope.sequence != reference.sequence
            or envelope.payload_fingerprint != reference.payload_fingerprint
        ):
            raise ValueError("human evidence source reference drifted")
        event = envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if not isinstance(event, RunStartEvent):
            raise TypeError("human evidence source is not a RunStart")
        return ExactHumanEvidenceSource(
            event=event,
            stored_reference=_stored_reference(envelope),
        )


def build_driver_binding_fingerprint(
    *,
    runtime_session_id: str,
    driver_generation: int,
) -> str:
    return context_fingerprint(
        "compaction-memory-extraction-session-driver-binding:v1",
        {
            "runtime_session_id": runtime_session_id,
            "driver_generation": driver_generation,
            "prompt_contract": _PROMPT_CONTRACT_FINGERPRINT,
            "semantic_contract": _EXTRACTION_SEMANTIC_CONTRACT_FINGERPRINT,
            "parser_contract": PARSER_CONTRACT_FINGERPRINT,
        },
    )


__all__ = [
    "CompactionMemoryExtractionSessionDriver",
    "build_driver_binding_fingerprint",
    "production_memory_extraction_prompt",
]
