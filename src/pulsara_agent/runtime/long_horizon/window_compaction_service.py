"""Service-owned same-run context-window compaction lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from pulsara_agent.event import (
    AgentEvent,
    ContextWindowClosedEvent,
    ContextWindowCompactionCompletedEvent,
    ContextWindowCompactionFailedEvent,
    ContextWindowCompactionStartedEvent,
    ContextWindowOpenedEvent,
    EventContext,
)
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.llm.commit import RuntimeSessionModelStreamEventCommitPort
from pulsara_agent.llm.direct import (
    DirectModelCallCollectionError,
    collect_direct_model_call_handle,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.lifecycle import (
    PreparedModelRolloutReservation,
    prepare_model_lifecycle_start_bundle,
    prepare_model_rollout_reservation,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.primitives.context import (
    TranscriptCompileInput,
    WindowCompactionSourceDocumentFact,
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.long_horizon import (
    ContextWindowCloseReason,
    ContextWindowFact,
    ContextWindowProjectionState,
    PreparedObservationRollupUnit,
    RunLongHorizonContractFact,
    ToolObservationProtectionFact,
    calculate_model_call_reservation,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.tool_result import ToolResultRenderUnit
from pulsara_agent.runtime.context_input.render import PreparedToolResultRenderOutput
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)
from pulsara_agent.runtime.long_horizon.window_compaction import (
    PreparedWindowCompactionSourceDocument,
    build_compacted_context_window,
    build_window_compaction_plan,
    parse_window_compaction_summary,
    prepare_window_compaction_source_document,
    window_compaction_identity,
)
from pulsara_agent.runtime.long_horizon.accounting import resolve_run_rollout_binding
from pulsara_agent.runtime.long_horizon.coordinator import (
    build_rollout_phase_transition_event,
    plan_root_model_admission,
)
from pulsara_agent.runtime.session import (
    EventPublicationAfterCommitError,
    EventReconciliationRequired,
    EventWriteResult,
)

if TYPE_CHECKING:
    from pulsara_agent.llm.runtime import LLMRuntime
    from pulsara_agent.runtime.session import RuntimeSession
    from pulsara_agent.runtime.state import LoopState


WINDOW_COMPACTION_PROMPT_VERSION = "window-compaction-summary-prompt.v1"
WINDOW_COMPACTION_PROMPT = """You compress one Pulsara context window.
Return exactly one JSON object with these array-of-string fields:
observed_facts, model_inferences, unresolved_questions, critical_constraints,
artifact_locators, cited_source_entry_ids.
Only cite source_entry_id values present in the source document. Artifact
locators must also occur in that document. Distinguish observations from model
inferences, retain user constraints and unresolved work, and never claim that
an unobserved operation succeeded. Do not summarize the retained tail.
"""


class WindowCompactionError(RuntimeError):
    pass


class PendingWindowCompactionError(WindowCompactionError):
    pass


class WindowCompactionBatchNotCommitted(WindowCompactionError):
    """A stable compaction batch is confirmed absent from the ledger."""


class WindowCompactionSourceStale(WindowCompactionError):
    """The frozen safe-point authority slice is no longer current."""


@dataclass(frozen=True, slots=True)
class WindowCompactionRequest:
    event_context: EventContext
    state: LoopState | None
    run_contract: RunLongHorizonContractFact
    source_window: ContextWindowFact
    source_projection: ContextWindowProjectionState
    transcript: TranscriptCompileInput
    tool_result_units: tuple[ToolResultRenderUnit, ...]
    rendered_tool_results: PreparedToolResultRenderOutput
    prepared_rollups: tuple[PreparedObservationRollupUnit, ...]
    protection_facts: tuple[ToolObservationProtectionFact, ...]
    source_through_sequence: int
    source_context_fingerprint: str
    estimated_tokens_before: int
    non_transcript_baseline_tokens: int
    transcript_tokens_before: int
    force: bool = False
    pending_interaction: bool = False
    tool_call_in_flight: bool = False


@dataclass(frozen=True, slots=True)
class WindowCompactionOutcome:
    status: Literal[
        "compacted",
        "failed",
        "blocked",
        "disabled",
        "phase_transitioned",
        "exhausted",
        "source_stale",
    ]
    compaction_id: str | None
    attempt_index: int | None
    reason_code: str | None
    target_window: ContextWindowFact | None = None
    terminal_event_id: str | None = None
    publication_errors: tuple[object, ...] = ()


@dataclass(slots=True)
class _WindowCompactionOwner:
    run_id: str
    compaction_id: str
    attempt_index: int
    task: asyncio.Task[WindowCompactionOutcome]


class ContextWindowCompactionService:
    """Own L4 attempts independently from caller cancellation."""

    def __init__(
        self,
        *,
        runtime_session: RuntimeSession,
        llm_runtime: LLMRuntime,
        max_automatic_failures_per_window: int = 2,
    ) -> None:
        if max_automatic_failures_per_window < 1:
            raise ValueError("window compaction failure limit must be positive")
        self.runtime_session = runtime_session
        self.llm_runtime = llm_runtime
        self.max_automatic_failures_per_window = max_automatic_failures_per_window
        self._lock = asyncio.Lock()
        self._owners: dict[str, _WindowCompactionOwner] = {}
        self._recovery_done = False

    @property
    def pending_count(self) -> int:
        return len(self._owners)

    async def compact(
        self, request: WindowCompactionRequest
    ) -> WindowCompactionOutcome:
        if request.pending_interaction or request.tool_call_in_flight:
            return WindowCompactionOutcome(
                status="blocked",
                compaction_id=None,
                attempt_index=None,
                reason_code=(
                    "window_compaction_pending_interaction"
                    if request.pending_interaction
                    else "window_compaction_tool_call_in_flight"
                ),
            )
        failures = self.runtime_session.long_horizon_state_store.window_compaction_failure_count(
            request.source_window.window_id
        )
        if not request.force and failures >= self.max_automatic_failures_per_window:
            return WindowCompactionOutcome(
                status="disabled",
                compaction_id=None,
                attempt_index=None,
                reason_code="window_compaction_failure_circuit_open",
            )
        attempt_index = self.runtime_session.long_horizon_state_store.next_window_compaction_attempt_index(
            request.source_window.window_id
        )
        compaction_id, _digest = window_compaction_identity(
            run_id=request.event_context.run_id,
            source_window=request.source_window,
            source_projection=request.source_projection,
            source_through_sequence=request.source_through_sequence,
            attempt_index=attempt_index,
        )
        task: asyncio.Task[WindowCompactionOutcome]
        async with self._lock:
            existing = self._owners.get(request.event_context.run_id)
            if existing is not None:
                if not existing.task.done():
                    task = existing.task
                else:
                    self._owners.pop(request.event_context.run_id, None)
                    existing = None
            if existing is None:
                task = asyncio.create_task(
                    self._execute(
                        request,
                        compaction_id=compaction_id,
                        attempt_index=attempt_index,
                    ),
                    name=f"context-window-compaction:{compaction_id}",
                )
                owner = _WindowCompactionOwner(
                    run_id=request.event_context.run_id,
                    compaction_id=compaction_id,
                    attempt_index=attempt_index,
                    task=task,
                )
                self._owners[request.event_context.run_id] = owner
                task.add_done_callback(
                    lambda done, owned=owner: self._owner_done(owned, done)
                )
        return await asyncio.shield(task)

    async def recover_interrupted(
        self,
        *,
        state: LoopState | None = None,
    ) -> tuple[ContextWindowCompactionFailedEvent, ...]:
        if self._recovery_done:
            return ()
        store = self.runtime_session.long_horizon_state_store
        started = store.pending_window_compactions()
        completed = store.completed_window_compactions()
        required_ids = tuple(
            dict.fromkeys(
                (
                    *(
                        event_id
                        for item in completed
                        for event_id in (
                            item.source_window_close_event_id,
                            item.target_window_open_event_id,
                        )
                    ),
                    *(
                        f"rollout_reservation_settled:{item.plan.rollout_reservation.reservation_id}"
                        for item in started
                    ),
                )
            )
        )
        deadline = time.monotonic() + 30.0
        raw_required = await self.runtime_session.context_input_io_service.execute(
            operation_name="window-compaction-recovery-refs",
            operation=lambda: self.runtime_session.event_log.read_raw_events_by_id(
                required_ids,
                deadline_monotonic=deadline,
            ),
            deadline_monotonic=deadline,
        )
        found_ids = {item.event_id for item in raw_required}
        for terminal in completed:
            required = {
                terminal.source_window_close_event_id,
                terminal.target_window_open_event_id,
            }
            if not required <= found_ids:
                self.runtime_session.latch_event_commit_outcome_unknown()
                raise EventReconciliationRequired(
                    "window compaction success batch is incomplete"
                )
        recovered: list[ContextWindowCompactionFailedEvent] = []
        for item in started:
            settlement_id = item.plan.rollout_reservation.reservation_id
            stable_settlement_id = f"rollout_reservation_settled:{settlement_id}"
            if stable_settlement_id not in found_ids:
                self.runtime_session.latch_event_commit_outcome_unknown()
                raise EventReconciliationRequired(
                    "window compaction recovery requires model settlement first"
                )
            failed = ContextWindowCompactionFailedEvent(
                id=item.plan.stable_failed_event_id,
                run_id=item.run_id,
                turn_id=item.turn_id,
                reply_id=item.reply_id,
                created_at=item.created_at,
                compaction_id=item.plan.compaction_id,
                compaction_attempt_index=item.plan.compaction_attempt_index,
                source_window_id=item.plan.source_window_id,
                source_window_generation=item.plan.source_window_generation,
                started_event_id=item.id,
                plan_fingerprint=item.plan.plan_fingerprint,
                failure_stage="recovery",
                reason_code="context_window_compaction_recovered_interrupted",
                summarizer_call=item.plan.summarizer_call,
                rollout_settlement_event_id=stable_settlement_id,
                observed_summary_tokens=None,
                observed_post_compaction_tokens=None,
                retryable=True,
            )
            write = await self._commit_stable_batch((failed,), state=state)
            stored = next(
                event
                for event in write.committed_events
                if isinstance(event, ContextWindowCompactionFailedEvent)
            )
            recovered.append(stored)
        self._recovery_done = True
        return tuple(recovered)

    async def drain_pending(self, *, deadline_monotonic: float) -> None:
        while True:
            tasks = tuple(owner.task for owner in self._owners.values())
            if not tasks:
                return
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise PendingWindowCompactionError(
                    "window compaction drain deadline exceeded"
                )
            done, pending = await asyncio.wait(
                tuple(asyncio.shield(task) for task in tasks),
                timeout=remaining,
            )
            if pending:
                raise PendingWindowCompactionError(
                    "window compaction drain deadline exceeded"
                )
            for completed in done:
                try:
                    completed.result()
                except BaseException:
                    pass
            await asyncio.sleep(0)

    def close_if_idle(self) -> None:
        if self._owners:
            raise PendingWindowCompactionError(
                "cannot close window compaction service with pending attempts"
            )

    def _owner_done(
        self,
        owner: _WindowCompactionOwner,
        completed: asyncio.Task[WindowCompactionOutcome],
    ) -> None:
        if not completed.cancelled():
            completed.exception()

        async def remove_exact() -> None:
            async with self._lock:
                if self._owners.get(owner.run_id) is owner:
                    self._owners.pop(owner.run_id, None)

        try:
            asyncio.get_running_loop().create_task(remove_exact())
        except RuntimeError:
            pass

    async def _execute(
        self,
        request: WindowCompactionRequest,
        *,
        compaction_id: str,
        attempt_index: int,
    ) -> WindowCompactionOutcome:
        self.runtime_session.require_mutation_allowed()
        source: PreparedWindowCompactionSourceDocument | None = None
        call = None
        plan = None
        started_committed = False
        observed_summary_tokens: int | None = None
        observed_post_tokens: int | None = None
        failure_stage: Literal[
            "planning",
            "summarizer_resolution",
            "input_manifest",
            "model_validation",
            "model_stream",
            "summary_validation",
            "summary_artifact",
            "terminal_batch",
            "recovery",
        ] = "planning"
        settlement_event_id: str | None = None
        try:
            _validate_request(request)
            source = prepare_window_compaction_source_document(
                compaction_id=compaction_id,
                run_id=request.event_context.run_id,
                window=request.source_window,
                projection_state=request.source_projection,
                transcript=request.transcript,
                units=request.tool_result_units,
                rendered=request.rendered_tool_results,
                prepared_rollups=request.prepared_rollups,
                protection_facts=request.protection_facts,
                source_through_sequence=request.source_through_sequence,
            )
            await _validate_source_refs(
                runtime_session=self.runtime_session,
                source=source.fact,
            )
            failure_stage = "summarizer_resolution"
            target = self.llm_runtime.rebind_target(
                request.run_contract.window_compaction_summarizer_target
            )
            call = self.llm_runtime.resolve_call(
                target=target,
                purpose=ModelCallPurpose.CONTEXT_WINDOW_COMPACTION_SUMMARY,
            )
            binding = resolve_run_rollout_binding(
                self.runtime_session,
                run_id=request.event_context.run_id,
            )
            if binding.child_state is None:
                quote = calculate_model_call_reservation(
                    target=call.target.fact,
                    resolved_model_call_id=call.fact.resolved_model_call_id,
                    policy=binding.account.policy,
                )
                admission = plan_root_model_admission(
                    account=binding.account,
                    state=binding.parent_state,
                    quote=quote,
                    purpose=call.fact.purpose,
                )
                if admission.action == "transition":
                    transition = build_rollout_phase_transition_event(
                        event_context=request.event_context,
                        account=binding.account,
                        state=binding.parent_state,
                        plan=admission,
                    )
                    write = await self._commit_stable_batch(
                        (transition,),
                        state=request.state,
                    )
                    stored = next(
                        event
                        for event in write.committed_events
                        if event.id == transition.id
                    )
                    return WindowCompactionOutcome(
                        status="phase_transitioned",
                        compaction_id=compaction_id,
                        attempt_index=attempt_index,
                        reason_code=transition.reason_code.value,
                        terminal_event_id=stored.id,
                        publication_errors=tuple(write.publication_errors),
                    )
                if admission.action == "blocked":
                    return WindowCompactionOutcome(
                        status="blocked",
                        compaction_id=compaction_id,
                        attempt_index=attempt_index,
                        reason_code=("window_compaction_rollout_reservation_pending"),
                    )
                if admission.action == "terminal":
                    return WindowCompactionOutcome(
                        status="exhausted",
                        compaction_id=compaction_id,
                        attempt_index=attempt_index,
                        reason_code="window_compaction_rollout_exhausted",
                    )
            model_context = _build_summarizer_context(call=call, source=source.fact)
            estimate = call.target.token_estimator.estimate_context(model_context)
            model_context = replace(
                model_context,
                compiler_estimated_input_tokens=estimate.total_input_tokens,
            )
            if (
                estimate.total_input_tokens
                > call.target.context_budget.input_budget_tokens
            ):
                raise WindowCompactionError(
                    "window compaction source exceeds summarizer input budget"
                )
            model_event_context = EventContext(
                run_id=request.event_context.run_id,
                turn_id=request.event_context.turn_id,
                reply_id=(
                    f"{request.event_context.reply_id}:window-compaction:{attempt_index}"
                ),
            )
            prepared_reservation = prepare_model_rollout_reservation(
                call=call,
                event_context=model_event_context,
                runtime_session=self.runtime_session,
            )
            reservation = _require_window_reservation(prepared_reservation)
            source_artifact_id = _artifact_id("window-compaction-source", compaction_id)
            manifest_artifact_id = _artifact_id(
                "window-compaction-input", compaction_id
            )
            manifest_text, manifest_fingerprint = _summarizer_manifest(
                call_id=call.fact.resolved_model_call_id,
                context=model_context,
                source=source.fact,
                estimated_input_tokens=estimate.total_input_tokens,
            )
            summarized_visible_tokens = sum(
                call.target.token_estimator.estimate_text(entry.model_visible_text)
                for entry in source.fact.entries
                if entry.source_entry_id in set(source.fact.summarized_entry_ids)
            )
            retained_tail_tokens = max(
                0,
                request.transcript_tokens_before - summarized_visible_tokens,
            )
            post_target = (
                request.source_window.input_budget_tokens
                * request.run_contract.window_policy.window_compaction_post_target_ratio_ppm
                // 1_000_000
            )
            plan = build_window_compaction_plan(
                compaction_id=compaction_id,
                compaction_attempt_index=attempt_index,
                run_id=request.event_context.run_id,
                source_window=request.source_window,
                source_projection=request.source_projection,
                source=source,
                source_context_fingerprint=request.source_context_fingerprint,
                summarizer_call=call.fact,
                rollout_reservation=reservation,
                summarizer_input_manifest_artifact_id=manifest_artifact_id,
                summarizer_input_manifest_fingerprint=manifest_fingerprint,
                source_document_artifact_id=source_artifact_id,
                estimated_tokens_before=request.estimated_tokens_before,
                fixed_new_window_tokens=request.non_transcript_baseline_tokens,
                protected_tail_tokens=retained_tail_tokens,
                summarizer_input_estimated_tokens=estimate.total_input_tokens,
                post_compaction_target_tokens=post_target,
            )
            failure_stage = "input_manifest"
            await self._put_artifact(
                artifact_id=source_artifact_id,
                text=source.canonical_json,
                run_id=request.event_context.run_id,
                kind="context_window_compaction_source_document",
                semantic_fingerprint=source.fact.document_fingerprint,
            )
            await self._put_artifact(
                artifact_id=manifest_artifact_id,
                text=manifest_text,
                run_id=request.event_context.run_id,
                kind="context_window_compaction_input_manifest",
                semantic_fingerprint=manifest_fingerprint,
            )
            failure_stage = "model_validation"
            started = ContextWindowCompactionStartedEvent(
                id=plan.stable_started_event_id,
                **model_event_context.event_fields(),
                plan=plan,
            )
            provider_input = await self.runtime_session.provider_input_generation_coordinator.prepare_one_shot_call(
                call=call,
                context=model_context,
                event_context=model_event_context,
                operation_kind="window_summarizer",
                operation_id=compaction_id,
                attempt_index=attempt_index,
            )
            try:
                model_context = provider_input.carrier.to_llm_context(model_context)
                bundle = prepare_model_lifecycle_start_bundle(
                    call=call,
                    context=model_context,
                    event_context=model_event_context,
                    runtime_session=self.runtime_session,
                    lifecycle_kind="window_compaction_summary",
                    window_compaction_started_event_id=started.id,
                    extra_companion_candidates=(started,),
                    prepared_rollout_reservation=prepared_reservation,
                    provider_input_start_bundle=provider_input,
                )
                settlement_event_id = bundle.recovery_plan.stable_settlement_event_id
                if settlement_event_id is None:
                    raise RuntimeError(
                        "window compaction lifecycle lacks settlement identity"
                    )
                failure_stage = "model_stream"
                handle = self.llm_runtime.start_stream(
                    call=call,
                    context=model_context,
                    event_context=model_event_context,
                    start_bundle=bundle,
                    commit_port=RuntimeSessionModelStreamEventCommitPort(
                        runtime_session=self.runtime_session,
                        state=request.state,
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
            result = await collect_direct_model_call_handle(
                handle,
                expected_call=call,
                runtime_session_id=self.runtime_session.runtime_session_id,
            )
            started_committed = (
                self.runtime_session.event_log.get_by_id(plan.stable_started_event_id)
                is not None
            )
            if result.outcome != "completed":
                raise WindowCompactionError(
                    result.error.message
                    if result.error is not None
                    else "window compaction summarizer failed"
                )
            failure_stage = "summary_validation"
            summary = parse_window_compaction_summary(result.text, source=source.fact)
            summary_text = canonical_json_bytes(summary.model_dump(mode="json")).decode(
                "utf-8"
            )
            observed_summary_tokens = call.target.token_estimator.estimate_text(
                summary_text
            )
            if observed_summary_tokens > plan.summary_output_budget_tokens:
                raise WindowCompactionError(
                    "window compaction summary exceeds its resolved output budget"
                )
            summary_message_tokens = call.target.token_estimator.estimate_message(
                LLMMessage.system(summary_text)
            )
            observed_post_tokens = (
                plan.fixed_new_window_tokens
                + plan.protected_tail_tokens
                + summary_message_tokens
            )
            if observed_post_tokens > plan.post_compaction_target_tokens:
                raise WindowCompactionError(
                    "window compaction summary does not reach the post target"
                )
            summary_artifact_id = _artifact_id(
                "window-compaction-summary", compaction_id
            )
            failure_stage = "summary_artifact"
            await self._put_artifact(
                artifact_id=summary_artifact_id,
                text=summary_text,
                run_id=request.event_context.run_id,
                kind="context_window_compaction_summary",
                semantic_fingerprint=summary.summary_fingerprint,
            )
            target_window = build_compacted_context_window(
                plan=plan,
                source_window=request.source_window,
                summary_artifact_id=summary_artifact_id,
                summary=summary,
            )
            completed = ContextWindowCompactionCompletedEvent(
                id=plan.stable_completed_event_id,
                **request.event_context.event_fields(),
                compaction_id=compaction_id,
                started_event_id=plan.stable_started_event_id,
                plan_fingerprint=plan.plan_fingerprint,
                summary_artifact_id=summary_artifact_id,
                summary_content_sha256=(
                    "sha256:" + hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
                ),
                summary_fact_fingerprint=summary.summary_fingerprint,
                summary_estimated_tokens=observed_summary_tokens,
                actual_post_compaction_estimated_tokens=observed_post_tokens,
                post_compaction_target_tokens=plan.post_compaction_target_tokens,
                target_reached=True,
                summarizer_call=result.resolved_call,
                summarizer_usage=result.usage,
                usage_status=result.usage_status,
                rollout_settlement_event_id=settlement_event_id,
                source_window_close_event_id=(plan.stable_source_window_close_event_id),
                target_window_open_event_id=plan.stable_target_window_open_event_id,
            )
            closed = ContextWindowClosedEvent(
                id=plan.stable_source_window_close_event_id,
                **request.event_context.event_fields(),
                window_id=request.source_window.window_id,
                window_generation=request.source_window.generation,
                close_reason=ContextWindowCloseReason.LLM_COMPACTION,
                final_projection_generation=(
                    request.source_projection.projection_generation
                ),
                final_projection_state_fingerprint=(
                    request.source_projection.state_semantic_fingerprint
                ),
                source_through_sequence=plan.source_through_sequence,
                next_window_id=target_window.window_id,
                compaction_terminal_event_id=completed.id,
            )
            opened = ContextWindowOpenedEvent(
                id=plan.stable_target_window_open_event_id,
                **request.event_context.event_fields(),
                window=target_window,
                opening_batch_id=compaction_id,
            )
            failure_stage = "terminal_batch"
            write = await self._commit_terminal_until_confirmed(
                (completed, closed, opened),
                state=request.state,
            )
            return WindowCompactionOutcome(
                status="compacted",
                compaction_id=compaction_id,
                attempt_index=attempt_index,
                reason_code=None,
                target_window=target_window,
                terminal_event_id=completed.id,
                publication_errors=tuple(write.publication_errors),
            )
        except WindowCompactionSourceStale:
            return WindowCompactionOutcome(
                status="source_stale",
                compaction_id=compaction_id,
                attempt_index=attempt_index,
                reason_code="window_compaction_safe_point_revision_required",
            )
        except BaseException as exc:
            if self.runtime_session.reconciliation_required:
                raise
            if plan is not None:
                started_committed = started_committed or (
                    self.runtime_session.event_log.get_by_id(
                        plan.stable_started_event_id
                    )
                    is not None
                )
            if not started_committed and failure_stage == "model_stream":
                failure_stage = "model_validation"
            if started_committed and settlement_event_id is not None:
                if (
                    self.runtime_session.event_log.get_by_id(settlement_event_id)
                    is None
                ):
                    raise EventReconciliationRequired(
                        "window compaction Started lacks model terminal settlement"
                    ) from exc
            failed = ContextWindowCompactionFailedEvent(
                id=(
                    plan.stable_failed_event_id
                    if plan is not None
                    else _preplan_failed_event_id(compaction_id)
                ),
                **request.event_context.event_fields(),
                compaction_id=compaction_id,
                compaction_attempt_index=attempt_index,
                source_window_id=request.source_window.window_id,
                source_window_generation=request.source_window.generation,
                started_event_id=(
                    plan.stable_started_event_id if started_committed and plan else None
                ),
                plan_fingerprint=plan.plan_fingerprint if plan is not None else None,
                failure_stage=failure_stage,
                reason_code=_failure_reason_code(failure_stage, exc),
                summarizer_call=call.fact if call is not None else None,
                rollout_settlement_event_id=(
                    settlement_event_id if started_committed else None
                ),
                observed_summary_tokens=observed_summary_tokens,
                observed_post_compaction_tokens=observed_post_tokens,
                retryable=not isinstance(exc, EventReconciliationRequired),
            )
            if started_committed:
                await self._commit_terminal_until_confirmed(
                    (failed,),
                    state=request.state,
                )
            else:
                await self._commit_stable_batch((failed,), state=request.state)
            return WindowCompactionOutcome(
                status="failed",
                compaction_id=compaction_id,
                attempt_index=attempt_index,
                reason_code=failed.reason_code,
                terminal_event_id=failed.id,
            )

    async def _put_artifact(
        self,
        *,
        artifact_id: str,
        text: str,
        run_id: str,
        kind: str,
        semantic_fingerprint: str,
    ) -> None:
        deadline = time.monotonic() + 30.0
        await self.runtime_session.context_input_io_service.execute(
            operation_name=kind,
            deadline_monotonic=deadline,
            operation=lambda: (
                self.runtime_session.archive.put_text_if_absent_or_confirm_identical(
                    artifact_id,
                    text,
                    session_id=self.runtime_session.runtime_session_id,
                    run_id=run_id,
                    media_type="application/json; charset=utf-8",
                    semantic_metadata={
                        "kind": kind,
                        "semantic_fingerprint": semantic_fingerprint,
                        "do_not_write_back": True,
                    },
                    deadline_monotonic=deadline,
                )
            ),
        )

    async def _commit_stable_batch(
        self,
        candidates: tuple[AgentEvent, ...],
        *,
        state: LoopState | None,
    ) -> EventWriteResult:
        try:
            return await self.runtime_session.write_events(
                candidates,
                state=state,
            )
        except EventPublicationAfterCommitError as exc:
            return exc.result
        except BaseException as original:
            outcome = self.runtime_session.resolved_event_write_outcome(original)
            if outcome.status == "unknown":
                raise
            if outcome.status == "none":
                raise WindowCompactionBatchNotCommitted(
                    "stable window compaction batch was not committed"
                ) from original
            assert outcome.result is not None
            return outcome.result

    async def _commit_terminal_until_confirmed(
        self,
        candidates: tuple[AgentEvent, ...],
        *,
        state: LoopState | None,
    ) -> EventWriteResult:
        """Hold the Started-to-terminal obligation across confirmed NONE writes."""

        while True:
            try:
                return await self._commit_stable_batch(candidates, state=state)
            except WindowCompactionBatchNotCommitted:
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if task is not None:
                        task.uncancel()
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()


def _validate_request(request: WindowCompactionRequest) -> None:
    if request.event_context.run_id != request.source_window.run_id:
        raise ValueError("window compaction request run mismatch")
    if request.source_projection.window_id != request.source_window.window_id:
        raise ValueError("window compaction request projection mismatch")
    if request.source_through_sequence != request.transcript.through_sequence:
        raise ValueError("window compaction request high-water mismatch")
    if (
        request.run_contract.window_policy.policy_fingerprint
        != request.source_window.window_policy_fingerprint
    ):
        raise ValueError("window compaction request policy drift")


async def _validate_source_refs(
    *,
    runtime_session: RuntimeSession,
    source: WindowCompactionSourceDocumentFact,
) -> None:
    ids = tuple(
        dict.fromkeys(
            ref.event_id
            for entry in source.entries
            for ref in entry.source_event_refs
            if ref.runtime_session_id == runtime_session.runtime_session_id
        )
    )
    deadline = time.monotonic() + 30.0
    snapshot = await runtime_session.context_input_io_service.execute(
        operation_name="window-compaction-source-refs",
        operation=lambda: runtime_session.event_log.read_raw_events_by_id_snapshot(
            ids,
            deadline_monotonic=deadline,
        ),
        deadline_monotonic=deadline,
    )
    if source.source_through_sequence != snapshot.through_sequence:
        raise WindowCompactionSourceStale(
            "window compaction source is not the current ledger high-water"
        )
    by_id = {
        event.event_id: event.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        for event in snapshot.events
    }
    for entry in source.entries:
        for ref in entry.source_event_refs:
            if ref.runtime_session_id != runtime_session.runtime_session_id:
                continue
            stored = by_id.get(ref.event_id)
            canonical_ref = (
                event_reference_from_stored(
                    stored,
                    runtime_session_id=runtime_session.runtime_session_id,
                )
                if stored is not None
                else None
            )
            if canonical_ref != ref or ref.sequence > source.source_through_sequence:
                raise ValueError(
                    "window compaction source entry references a non-canonical event"
                )


def _build_summarizer_context(
    *, call, source: WindowCompactionSourceDocumentFact
) -> LLMContext:
    summarized_ids = set(source.summarized_entry_ids)
    payload = {
        "schema_version": "window-compaction-summarizer-input.v1",
        "compaction_id": source.compaction_id,
        "source_document_fingerprint": source.document_fingerprint,
        "entries": tuple(
            {
                "source_entry_id": entry.source_entry_id,
                "source_kind": entry.source_kind,
                "source_event_refs": tuple(
                    ref.model_dump(mode="json") for ref in entry.source_event_refs
                ),
                "source_artifact_refs": entry.source_artifact_refs,
                "model_visible_text": entry.model_visible_text,
                "timing": (
                    entry.timing.model_dump(mode="json")
                    if entry.timing is not None
                    else None
                ),
                "semantic_fingerprint": entry.semantic_fingerprint,
            }
            for entry in source.entries
            if entry.source_entry_id in summarized_ids
        ),
        "allowed_artifact_locators": tuple(
            sorted(
                {
                    artifact_id
                    for entry in source.entries
                    if entry.source_entry_id in summarized_ids
                    for artifact_id in entry.source_artifact_refs
                }
            )
        ),
        "retained_tail_entry_ids": source.retained_entry_ids,
    }
    return LLMContext(
        messages=(LLMMessage.user(canonical_json_bytes(payload).decode("utf-8")),),
        system_prompt=WINDOW_COMPACTION_PROMPT,
        context_id=f"context:window-compaction:{source.compaction_id}",
        resolved_model_call_id=call.fact.resolved_model_call_id,
        target_fingerprint=call.target.fact.target_fingerprint,
        model_call_index=None,
    )


def _summarizer_manifest(
    *,
    call_id: str,
    context: LLMContext,
    source: WindowCompactionSourceDocumentFact,
    estimated_input_tokens: int,
) -> tuple[str, str]:
    payload = {
        "schema_version": "window-compaction-input-manifest.v1",
        "compaction_id": source.compaction_id,
        "resolved_model_call_id": call_id,
        "source_document_fingerprint": source.document_fingerprint,
        "summarized_entry_ids": source.summarized_entry_ids,
        "retained_entry_ids": source.retained_entry_ids,
        "prompt_version": WINDOW_COMPACTION_PROMPT_VERSION,
        "prompt_fingerprint": context_fingerprint(
            "window-compaction-prompt:v1", WINDOW_COMPACTION_PROMPT
        ),
        "estimated_input_tokens": estimated_input_tokens,
    }
    text = canonical_json_bytes(payload).decode("utf-8")
    return text, context_fingerprint("window-compaction-input-manifest:v1", payload)


def _require_window_reservation(
    prepared: PreparedModelRolloutReservation,
):
    reservation = prepared.reservation
    if reservation is None or prepared.accounting_mode == "not_rollout_accounted":
        raise WindowCompactionError(
            "window compaction requires an active rollout reservation"
        )
    return reservation


def _artifact_id(kind: str, compaction_id: str) -> str:
    digest = context_fingerprint(f"{kind}-artifact-id:v1", compaction_id).removeprefix(
        "sha256:"
    )
    return f"artifact:{kind}:{digest}"


def _preplan_failed_event_id(compaction_id: str) -> str:
    return "window-compaction-failed:" + context_fingerprint(
        "window-compaction-preplan-failed:v1", compaction_id
    ).removeprefix("sha256:")


def _failure_reason_code(stage: str, error: BaseException) -> str:
    if isinstance(error, asyncio.CancelledError):
        return "context_window_compaction_cancelled"
    if isinstance(error, DirectModelCallCollectionError):
        return "context_window_compaction_model_stream_failed"
    return f"context_window_compaction_{stage}_failed"


__all__ = [
    "ContextWindowCompactionService",
    "PendingWindowCompactionError",
    "WindowCompactionError",
    "WindowCompactionOutcome",
    "WindowCompactionRequest",
]
