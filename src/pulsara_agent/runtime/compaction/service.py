"""LLM-backed context compaction service."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from importlib import resources
from typing import Literal
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    CompactionCandidateDiagnosticEvent,
    ContextCompiledEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionMemoryCandidatesProposedEvent,
    ContextCompactionStartedEvent,
    ExceedMaxItersEvent,
    EventContext,
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    PlanQuestionAnsweredEvent,
    PlanQuestionAskedEvent,
    RunErrorEvent,
    RunEndEvent,
    RunStartEvent,
    ToolResultEndEvent,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.direct import DirectModelCallResult, collect_direct_model_call
from pulsara_agent.llm.errors import (
    CompactionSummarizerInputBudgetExceeded,
    CompactionTargetUnreachable,
    ModelContextIdentityMismatch,
    ModelInputBudgetExceeded,
    ModelInputEstimateMismatch,
    ModelTargetBindingMismatch,
    ModelTargetCapabilityMismatch,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.resolution import ResolvedModelTarget
from pulsara_agent.primitives.model_call import (
    CompactionObservedAfterMeasurementFact,
    CompactionTargetEstimateFact,
    ModelCallPurpose,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.message import (
    AssistantMsg,
    DataBlock,
    HintBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMsg,
)
from pulsara_agent.message.assembler import BlockAssembler
from pulsara_agent.runtime.compaction.planner import (
    SUMMARY_ARTIFACT_KIND,
    latest_completed_boundary,
    render_compaction_summary,
    strip_compaction_analysis,
)
from pulsara_agent.runtime.compaction.candidates import (
    CompactionCandidateAppendResult,
    CompactionCandidateDiagnostic,
    CompactionCandidateParseResult,
    CompactionMemoryCandidateSink,
    ContextCompactionMemoryCandidatePolicy,
    parse_compaction_memory_candidates,
)

ContextCompactionTrigger = Literal["manual", "auto"]

_PRODUCTION_PROMPT_PACKAGE = "pulsara_agent.runtime.compaction.prompts"
_PRODUCTION_PROMPT_FILE = "context_compaction_prompt.md"
_COMPACTION_TEXT_CLIP_CHARS = 4_000
_COMPACTION_TOOL_INPUT_CLIP_CHARS = 2_000
_COMPACTION_TOOL_RESULT_CLIP_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class ContextCompactionPolicy:
    enabled: bool = True
    auto_enabled: bool = True
    manual_enabled: bool = True
    auto_trigger_ratio: float = 0.80
    post_compaction_target_ratio: float = 0.55
    min_events_after_last_compact: int = 20
    keep_recent_runs: int = 3
    max_summary_chars: int = 12_000
    max_consecutive_failures: int = 3
    summarizer_options: LLMOptions = field(default_factory=LLMOptions)
    memory_candidates: ContextCompactionMemoryCandidatePolicy = field(
        default_factory=ContextCompactionMemoryCandidatePolicy
    )

    def __post_init__(self) -> None:
        if not (0 < self.post_compaction_target_ratio < self.auto_trigger_ratio < 1):
            raise ValueError(
                "compaction ratios must satisfy 0 < post target < auto trigger < 1"
            )


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    through_sequence: int
    keep_after_sequence: int
    target_estimate: CompactionTargetEstimateFact
    threshold_tokens: int
    post_compaction_target_tokens: int
    retained_transcript_tokens: int
    protected_transcript_tokens: int
    included_run_ids: tuple[str, ...]
    included_artifact_ids: tuple[str, ...]
    compacted_events: tuple[AgentEvent, ...]
    tail_events: tuple[AgentEvent, ...]
    window_number: int
    window_id: str
    previous_summary_artifact_id: str | None = None
    previous_summary_text: str | None = None


@dataclass(frozen=True, slots=True)
class CompactionSummaryReplayTemplate:
    summary_artifact_id: str
    compaction_id: str
    window_id: str
    through_sequence: int
    keep_after_sequence: int


@dataclass(slots=True)
class ContextCompactionService:
    event_log: EventLog
    archive: ArtifactStore
    llm_runtime: LLMRuntime
    runtime_session_id: str
    policy: ContextCompactionPolicy = ContextCompactionPolicy()
    model_role: ModelRole = ModelRole.FLASH
    candidate_sink: CompactionMemoryCandidateSink | None = None
    _consecutive_failures: int = 0

    def should_auto_compact(
        self,
        *,
        target_model_target: ResolvedModelTarget,
        current_user_input_if_not_already_represented: str = "",
        model_visible_messages_before: list[Msg] | tuple[Msg, ...] | None = None,
        protected_model_visible_messages_after: tuple[LLMMessage, ...] = (),
        max_compactable_sequence: int | None = None,
        keep_recent_runs_override: int | None = None,
    ) -> bool:
        if not self.policy.enabled or not self.policy.auto_enabled:
            return False
        if self._consecutive_failures >= self.policy.max_consecutive_failures:
            return False
        events = self.event_log.iter()
        try:
            plan = self._build_plan(
                events,
                compaction_id=f"context_compaction:{uuid4().hex}",
                target_model_target=target_model_target,
                current_user_input_if_not_already_represented=(
                    current_user_input_if_not_already_represented
                ),
                model_visible_messages_before=model_visible_messages_before,
                protected_model_visible_messages_after=(
                    protected_model_visible_messages_after
                ),
                max_compactable_sequence=max_compactable_sequence,
                keep_recent_runs_override=keep_recent_runs_override,
            )
        except CompactionTargetUnreachable:
            return True
        if plan is None:
            return False
        return True

    async def compact_if_needed(
        self,
        *,
        target_model_target: ResolvedModelTarget,
        current_user_input_if_not_already_represented: str = "",
        model_visible_messages_before: list[Msg] | tuple[Msg, ...] | None = None,
        protected_model_visible_messages_after: tuple[LLMMessage, ...] = (),
        reason: str = "context_threshold",
        max_compactable_sequence: int | None = None,
        keep_recent_runs_override: int | None = None,
        event_metadata: dict[str, object] | None = None,
    ) -> bool:
        if not self.should_auto_compact(
            target_model_target=target_model_target,
            current_user_input_if_not_already_represented=(
                current_user_input_if_not_already_represented
            ),
            model_visible_messages_before=model_visible_messages_before,
            protected_model_visible_messages_after=(
                protected_model_visible_messages_after
            ),
            max_compactable_sequence=max_compactable_sequence,
            keep_recent_runs_override=keep_recent_runs_override,
        ):
            return False
        return (
            await self.compact(
                target_model_target=target_model_target,
                trigger="auto",
                reason=reason,
                current_user_input_if_not_already_represented=(
                    current_user_input_if_not_already_represented
                ),
                model_visible_messages_before=model_visible_messages_before,
                protected_model_visible_messages_after=(
                    protected_model_visible_messages_after
                ),
                max_compactable_sequence=max_compactable_sequence,
                keep_recent_runs_override=keep_recent_runs_override,
                event_metadata=event_metadata,
            )
            is not None
        )

    async def compact(
        self,
        *,
        target_model_target: ResolvedModelTarget,
        trigger: ContextCompactionTrigger,
        reason: str,
        current_user_input_if_not_already_represented: str = "",
        model_visible_messages_before: list[Msg] | tuple[Msg, ...] | None = None,
        protected_model_visible_messages_after: tuple[LLMMessage, ...] = (),
        force: bool = False,
        max_compactable_sequence: int | None = None,
        keep_recent_runs_override: int | None = None,
        event_metadata: dict[str, object] | None = None,
    ) -> ContextCompactionCompletedEvent | None:
        if not self.policy.enabled:
            return None
        if trigger == "manual" and not self.policy.manual_enabled:
            return None
        if trigger == "auto" and not self.policy.auto_enabled:
            return None
        if (
            trigger == "auto"
            and self._consecutive_failures >= self.policy.max_consecutive_failures
        ):
            return None

        events = self.event_log.iter()
        compaction_id = f"context_compaction:{uuid4().hex}"
        context = _event_context_for_compaction(events)
        try:
            plan = self._build_plan(
                events,
                compaction_id=compaction_id,
                target_model_target=target_model_target,
                current_user_input_if_not_already_represented=(
                    current_user_input_if_not_already_represented
                ),
                model_visible_messages_before=model_visible_messages_before,
                protected_model_visible_messages_after=(
                    protected_model_visible_messages_after
                ),
                force=force,
                max_compactable_sequence=max_compactable_sequence,
                keep_recent_runs_override=keep_recent_runs_override,
            )
        except Exception as exc:
            self._consecutive_failures += 1
            window_number = _next_window_number(events)
            failed = ContextCompactionFailedEvent(
                **context.event_fields(),
                compaction_id=compaction_id,
                trigger=trigger,
                reason=reason,
                window_number=window_number,
                window_id=f"context_window:{window_number}:{uuid4().hex}",
                target_model_target=target_model_target.fact,
                target_input_budget_tokens=(
                    target_model_target.fact.context_budget.input_budget_tokens
                ),
                threshold_tokens=max(
                    1,
                    math.floor(
                        target_model_target.fact.context_budget.input_budget_tokens
                        * self.policy.auto_trigger_ratio
                    ),
                ),
                post_compaction_target_tokens=max(
                    1,
                    math.floor(
                        target_model_target.fact.context_budget.input_budget_tokens
                        * self.policy.post_compaction_target_ratio
                    ),
                ),
                failure_stage="planning",
                error_type=type(exc).__name__,
                message=str(exc),
                metadata=dict(event_metadata or {}),
            )
            await asyncio.to_thread(self.event_log.append, failed)
            if trigger == "manual":
                raise
            return None
        if plan is None:
            return None
        if (
            not force
            and trigger == "auto"
            and plan.target_estimate.estimated_tokens_before < plan.threshold_tokens
        ):
            return None

        metadata = {
            "estimate_scope": plan.target_estimate.estimate_scope,
            "basis_context_id": plan.target_estimate.basis_context_id,
            **(event_metadata or {}),
        }
        phase = (
            str(metadata.get("phase")) if metadata.get("phase") is not None else None
        )
        failure_stage = "summarizer_resolution"
        summarizer_target = None
        summarizer_call = None
        summarizer_context: LLMContext | None = None
        summarizer_input_estimated_tokens: int | None = None
        call_result: DirectModelCallResult | None = None
        completed_target_estimate = plan.target_estimate
        observed_after_measurement: CompactionObservedAfterMeasurementFact | None = None
        try:
            summarizer_target = self.llm_runtime.resolve_target(
                role=self.model_role,
                requested_options=self.policy.summarizer_options,
            )
            summarizer_call = self.llm_runtime.resolve_call(
                target=summarizer_target,
                purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY,
            )
            failure_stage = "summarizer_input_build"
            summarizer_context, summarizer_input_estimated_tokens = (
                self._build_summarizer_context(
                    plan,
                    call=summarizer_call,
                    trigger=trigger,
                    phase=phase,
                )
            )
            started = ContextCompactionStartedEvent(
                **context.event_fields(),
                compaction_id=compaction_id,
                trigger=trigger,
                reason=reason,
                window_number=plan.window_number,
                window_id=plan.window_id,
                target_model_target=target_model_target.fact,
                target_input_budget_tokens=(
                    target_model_target.fact.context_budget.input_budget_tokens
                ),
                threshold_tokens=plan.threshold_tokens,
                post_compaction_target_tokens=plan.post_compaction_target_tokens,
                target_estimate=plan.target_estimate,
                summarizer_call=summarizer_call.fact,
                summarizer_context_id=summarizer_context.context_id or "",
                summarizer_input_estimated_tokens=summarizer_input_estimated_tokens,
                summarizer_input_budget_tokens=(
                    summarizer_target.fact.context_budget.input_budget_tokens
                ),
                through_sequence=plan.through_sequence,
                keep_after_sequence=plan.keep_after_sequence,
                force=force,
                metadata=metadata,
            )
            failure_stage = "started_append"
            await asyncio.to_thread(self.event_log.append, started)

            failure_stage = "model_stream"
            call_result = await self._summarize(
                call=summarizer_call,
                context=summarizer_context,
                event_context=context,
            )
            if call_result.outcome != "completed":
                raise RuntimeError(
                    call_result.error.message
                    if call_result.error
                    else "compact model error"
                )
            raw_summary = call_result.text
            failure_stage = "summary_validation"
            summary = strip_compaction_analysis(raw_summary)
            if not summary:
                raise RuntimeError("compact model returned an empty summary")
            if len(summary) > self.policy.max_summary_chars:
                raise RuntimeError("compact model summary exceeds max_summary_chars")
            artifact_id = _summary_artifact_id(compaction_id)
            replay_template = CompactionSummaryReplayTemplate(
                summary_artifact_id=artifact_id,
                compaction_id=compaction_id,
                window_id=plan.window_id,
                through_sequence=plan.through_sequence,
                keep_after_sequence=plan.keep_after_sequence,
            )
            summary_tokens_actual = estimate_compaction_summary_replay_tokens(
                replay_template=replay_template,
                summary_text=summary,
                target_model_target=target_model_target,
            )
            transcript_tokens_after = (
                plan.retained_transcript_tokens
                + plan.protected_transcript_tokens
                + summary_tokens_actual
            )
            baseline = plan.target_estimate.non_transcript_baseline_tokens
            estimated_tokens_after = transcript_tokens_after + (baseline or 0)
            predicted = (
                estimated_tokens_after <= plan.post_compaction_target_tokens
                if baseline is not None
                else None
            )
            if summary_tokens_actual > plan.target_estimate.summary_tokens_reserved:
                observed_after_measurement = CompactionObservedAfterMeasurementFact(
                    summary_tokens_actual=summary_tokens_actual,
                    retained_transcript_tokens=plan.retained_transcript_tokens,
                    protected_transcript_tokens=plan.protected_transcript_tokens,
                    transcript_tokens_after=transcript_tokens_after,
                    estimated_tokens_after=estimated_tokens_after,
                    predicted_post_target_reached=predicted,
                    violation_code="summary_tokens_exceed_reservation",
                )
                raise ValueError(
                    "actual summary tokens exceed the planning reservation"
                )
            completed_target_estimate = CompactionTargetEstimateFact(
                estimate_scope=plan.target_estimate.estimate_scope,
                basis_context_id=plan.target_estimate.basis_context_id,
                basis_context_compiled_sequence=(
                    plan.target_estimate.basis_context_compiled_sequence
                ),
                target_fingerprint=plan.target_estimate.target_fingerprint,
                non_transcript_baseline_tokens=baseline,
                transcript_tokens_before=plan.target_estimate.transcript_tokens_before,
                estimated_tokens_before=plan.target_estimate.estimated_tokens_before,
                summary_tokens_reserved=plan.target_estimate.summary_tokens_reserved,
                retained_transcript_tokens=(
                    plan.target_estimate.retained_transcript_tokens
                ),
                protected_transcript_tokens=(
                    plan.target_estimate.protected_transcript_tokens
                ),
                summary_tokens_actual=summary_tokens_actual,
                transcript_tokens_after=transcript_tokens_after,
                estimated_tokens_after=estimated_tokens_after,
                predicted_post_target_reached=predicted,
            )
            if predicted is False:
                raise CompactionTargetUnreachable(
                    "actual compacted context exceeds the resolved post-compaction target"
                )
            failure_stage = "artifact_write"
            await asyncio.to_thread(
                self.archive.put_text,
                artifact_id,
                summary,
                session_id=self.runtime_session_id,
                run_id=context.run_id,
                media_type="text/plain; charset=utf-8",
                metadata={
                    "kind": SUMMARY_ARTIFACT_KIND,
                    "do_not_write_back": True,
                    "compaction_id": compaction_id,
                    "trigger": trigger,
                    "reason": reason,
                    "window_number": plan.window_number,
                    "window_id": plan.window_id,
                    "through_sequence": plan.through_sequence,
                    "keep_after_sequence": plan.keep_after_sequence,
                    "included_run_ids": list(plan.included_run_ids),
                    "included_artifact_ids": list(plan.included_artifact_ids),
                    "target_estimate": completed_target_estimate.model_dump(
                        mode="json"
                    ),
                    **(event_metadata or {}),
                },
            )
            completed = ContextCompactionCompletedEvent(
                **context.event_fields(),
                compaction_id=compaction_id,
                trigger=trigger,
                reason=reason,
                window_number=plan.window_number,
                window_id=plan.window_id,
                summary_artifact_id=artifact_id,
                summary_chars=len(summary),
                target_model_target=target_model_target.fact,
                target_input_budget_tokens=(
                    target_model_target.fact.context_budget.input_budget_tokens
                ),
                threshold_tokens=plan.threshold_tokens,
                post_compaction_target_tokens=plan.post_compaction_target_tokens,
                target_estimate=completed_target_estimate,
                summarizer_call=call_result.resolved_call,
                summarizer_context_id=summarizer_context.context_id or "",
                summarizer_input_estimated_tokens=summarizer_input_estimated_tokens,
                summarizer_input_budget_tokens=(
                    summarizer_target.fact.context_budget.input_budget_tokens
                ),
                summarizer_usage_status=call_result.usage_status,
                summarizer_usage=call_result.usage,
                summarizer_estimated_input_tokens=call_result.estimated_input_tokens,
                summarizer_reported_model_id=call_result.reported_model_id,
                predicted_post_target_reached=predicted,
                through_sequence=plan.through_sequence,
                keep_after_sequence=plan.keep_after_sequence,
                included_run_ids=list(plan.included_run_ids),
                included_artifact_ids=list(plan.included_artifact_ids),
                metadata=metadata,
            )
            failure_stage = "completed_append"
            stored = await asyncio.to_thread(self.event_log.append, completed)
            await self._append_memory_candidate_proposals_if_enabled(
                raw_summary=raw_summary,
                summary=summary,
                completed=stored,
                summary_artifact_id=artifact_id,
                phase=phase,
            )
            self._consecutive_failures = 0
            return stored
        except Exception as exc:
            self._consecutive_failures += 1
            if failure_stage == "model_stream" and isinstance(
                exc,
                (
                    ModelInputBudgetExceeded,
                    ModelInputEstimateMismatch,
                    ModelContextIdentityMismatch,
                    ModelTargetCapabilityMismatch,
                    ModelTargetBindingMismatch,
                ),
            ):
                failure_stage = "model_validation"
            estimate = getattr(exc, "estimate", None)
            failed = ContextCompactionFailedEvent(
                **context.event_fields(),
                compaction_id=compaction_id,
                trigger=trigger,
                reason=reason,
                window_number=plan.window_number,
                window_id=plan.window_id,
                target_model_target=target_model_target.fact,
                target_input_budget_tokens=(
                    target_model_target.fact.context_budget.input_budget_tokens
                ),
                threshold_tokens=plan.threshold_tokens,
                post_compaction_target_tokens=plan.post_compaction_target_tokens,
                failure_stage=failure_stage,
                target_estimate=completed_target_estimate,
                observed_after_measurement=observed_after_measurement,
                summarizer_target=(
                    summarizer_target.fact if summarizer_target is not None else None
                ),
                summarizer_call=(
                    summarizer_call.fact if summarizer_call is not None else None
                ),
                summarizer_context_id=(
                    summarizer_context.context_id
                    if summarizer_context is not None
                    else None
                ),
                summarizer_input_estimated_tokens=summarizer_input_estimated_tokens,
                summarizer_input_budget_tokens=(
                    summarizer_target.fact.context_budget.input_budget_tokens
                    if summarizer_target is not None
                    else None
                ),
                summarizer_usage_status=(
                    call_result.usage_status if call_result is not None else "missing"
                ),
                summarizer_usage=call_result.usage if call_result is not None else None,
                summarizer_estimated_input_tokens=(
                    call_result.estimated_input_tokens
                    if call_result is not None
                    else estimate.total_input_tokens
                    if estimate is not None
                    else None
                ),
                summarizer_reported_model_id=(
                    call_result.reported_model_id if call_result is not None else None
                ),
                through_sequence=plan.through_sequence,
                keep_after_sequence=plan.keep_after_sequence,
                error_type=type(exc).__name__,
                message=str(exc),
                metadata=metadata,
            )
            await asyncio.to_thread(self.event_log.append, failed)
            if trigger == "manual":
                raise
            return None

    def _build_summarizer_context(
        self,
        plan: CompactionPlan,
        *,
        call,
        trigger: ContextCompactionTrigger,
        phase: str | None,
    ) -> tuple[LLMContext, int]:
        prompt = production_compaction_prompt(
            memory_candidates_enabled=_memory_candidate_extraction_enabled(
                self.candidate_sink,
                trigger,
                phase=phase,
                policy=self.policy.memory_candidates,
            )
        )

        def build(input_text: str) -> LLMContext:
            return LLMContext(
                messages=(
                    LLMMessage.system(prompt),
                    LLMMessage.user(input_text),
                ),
                tools=(),
                context_id=f"context:compaction:{plan.window_id}",
                resolved_model_call_id=call.fact.resolved_model_call_id,
                target_fingerprint=call.target.fact.target_fingerprint,
                model_call_index=None,
            )

        context = build(build_compaction_input(plan))
        estimate = call.target.token_estimator.estimate_context(context)
        budget = call.target.fact.context_budget.input_budget_tokens
        if estimate.total_input_tokens > budget:
            context = build(build_metadata_only_compaction_input(plan))
            estimate = call.target.token_estimator.estimate_context(context)
        if estimate.total_input_tokens > budget:
            exc = CompactionSummarizerInputBudgetExceeded(
                f"compaction summarizer input {estimate.total_input_tokens} exceeds budget {budget}"
            )
            exc.estimate = estimate  # type: ignore[attr-defined]
            raise exc
        return context, estimate.total_input_tokens

    async def _summarize(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> DirectModelCallResult:
        return await collect_direct_model_call(
            self.llm_runtime.stream(
                call=call,
                context=context,
                event_context=EventContext(
                    run_id=event_context.run_id,
                    turn_id=event_context.turn_id,
                    reply_id=f"{event_context.reply_id}:compaction-model",
                ),
            ),
            expected_call=call,
        )

    async def _append_memory_candidate_proposals_if_enabled(
        self,
        *,
        raw_summary: str,
        summary: str,
        completed: ContextCompactionCompletedEvent,
        summary_artifact_id: str,
        phase: str | None,
    ) -> None:
        sink = self.candidate_sink
        policy = self.policy.memory_candidates
        if not _memory_candidate_extraction_enabled(
            sink,
            completed.trigger,
            phase=phase,
            policy=policy,
        ):
            return
        assert sink is not None
        parse_result = parse_compaction_memory_candidates(
            raw_summary,
            workspace_scope=sink.workspace_scope,
            workspace_kind=sink.workspace_kind,
            policy=policy,
        )
        if not _extraction_attempted(parse_result):
            return
        try:
            append_result = await asyncio.to_thread(
                sink.append_compaction_candidates,
                completed_event=completed,
                summary_artifact_id=summary_artifact_id,
                summary_text=summary,
                parse_result=parse_result,
                policy=policy,
            )
            await self._append_memory_candidates_proposed_event(
                completed=completed,
                summary_artifact_id=summary_artifact_id,
                parse_result=parse_result,
                append_result=append_result,
                diagnostics=(),
            )
        except Exception as exc:
            diagnostic = CompactionCandidateDiagnostic(
                code="compaction_candidate_append_failed",
                message=type(exc).__name__,
                redacted=True,
            )
            fallback_result = CompactionCandidateAppendResult(
                source_event_id=completed.id,
                source_event_sequence=int(completed.sequence or 0),
                source_artifact_id=summary_artifact_id,
                entry_ids=(),
                diagnostics=(diagnostic,),
            )
            await self._append_memory_candidates_proposed_event(
                completed=completed,
                summary_artifact_id=summary_artifact_id,
                parse_result=parse_result,
                append_result=fallback_result,
                diagnostics=(),
            )

    async def _append_memory_candidates_proposed_event(
        self,
        *,
        completed: ContextCompactionCompletedEvent,
        summary_artifact_id: str,
        parse_result: CompactionCandidateParseResult,
        append_result: CompactionCandidateAppendResult,
        diagnostics: tuple[CompactionCandidateDiagnostic, ...],
    ) -> None:
        all_diagnostics = (
            *parse_result.diagnostics,
            *append_result.diagnostics,
            *_skipped_item_diagnostics(parse_result),
            *_skipped_item_diagnostics(append_result),
            *diagnostics,
        )
        skipped_count = len(parse_result.skipped) + len(append_result.skipped)
        error_count = sum(
            1
            for diagnostic in all_diagnostics
            if not diagnostic.code.startswith("compaction_candidate_skipped:")
            and ("failed" in diagnostic.code or "malformed" in diagnostic.code)
        )
        event = ContextCompactionMemoryCandidatesProposedEvent(
            **EventContext(
                run_id=completed.run_id,
                turn_id=completed.turn_id,
                reply_id=completed.reply_id,
            ).event_fields(),
            compaction_id=completed.compaction_id,
            source_event_id=append_result.source_event_id,
            source_event_sequence=append_result.source_event_sequence,
            summary_artifact_id=summary_artifact_id,
            candidate_entry_ids=list(append_result.entry_ids),
            attempted_count=parse_result.attempted_count,
            proposed_count=len(append_result.entry_ids),
            skipped_count=skipped_count,
            duplicate_count=append_result.duplicate_count,
            error_count=error_count,
            extractor_version=self.policy.memory_candidates.extractor_version,
            diagnostics=[
                _event_diagnostic(diagnostic) for diagnostic in all_diagnostics
            ],
        )
        try:
            await asyncio.to_thread(self.event_log.append, event)
        except Exception:
            return

    def _build_plan(
        self,
        events: list[AgentEvent],
        *,
        compaction_id: str,
        target_model_target: ResolvedModelTarget,
        current_user_input_if_not_already_represented: str = "",
        model_visible_messages_before: list[Msg] | tuple[Msg, ...] | None = None,
        protected_model_visible_messages_after: tuple[LLMMessage, ...] = (),
        force: bool = False,
        max_compactable_sequence: int | None = None,
        keep_recent_runs_override: int | None = None,
    ) -> CompactionPlan | None:
        if not events:
            return None
        latest_boundary = latest_completed_boundary(
            events, archive=self.archive, session_id=self.runtime_session_id
        )
        last_keep_after = (
            latest_boundary.keep_after_sequence if latest_boundary is not None else 0
        )
        candidate_events = [
            event
            for event in events
            if event.sequence is not None
            and event.sequence > last_keep_after
            and (
                max_compactable_sequence is None
                or event.sequence <= max_compactable_sequence
            )
        ]
        if not candidate_events:
            return None
        transcript_messages = (
            list(model_visible_messages_before)
            if model_visible_messages_before is not None
            else model_visible_messages_from_events(candidate_events)
        )
        transcript_tokens_before = _estimate_transcript_messages(
            transcript_messages,
            current_user_input=current_user_input_if_not_already_represented,
            target_model_target=target_model_target,
            previous_summary_text=(
                latest_boundary.summary_text
                if latest_boundary is not None and model_visible_messages_before is None
                else None
            ),
        )
        basis = _latest_matching_context_compiled_event(
            events,
            target_fingerprint=target_model_target.fact.target_fingerprint,
            max_sequence=max_compactable_sequence,
        )
        baseline = (
            basis.budget.non_transcript_baseline_tokens if basis is not None else None
        )
        estimate_scope = (
            "compiled_context_baseline" if baseline is not None else "transcript_only"
        )
        estimated_before = transcript_tokens_before + (baseline or 0)
        threshold_tokens = max(
            1,
            math.floor(
                target_model_target.fact.context_budget.input_budget_tokens
                * self.policy.auto_trigger_ratio
            ),
        )
        post_target = max(
            1,
            math.floor(
                target_model_target.fact.context_budget.input_budget_tokens
                * self.policy.post_compaction_target_ratio
            ),
        )
        if not force and estimated_before < threshold_tokens:
            return None
        if (
            not force
            and len(candidate_events) < self.policy.min_events_after_last_compact
        ):
            return None
        keep_recent_runs = (
            keep_recent_runs_override
            if keep_recent_runs_override is not None
            else self.policy.keep_recent_runs
        )
        keep_after_sequence = _keep_after_sequence_for_recent_runs(
            candidate_events, keep_recent_runs
        )
        if keep_after_sequence <= last_keep_after:
            if keep_recent_runs_override is not None and not force:
                return None
            keep_after_sequence = max(event.sequence or 0 for event in candidate_events)
        compacted = tuple(
            event
            for event in candidate_events
            if (event.sequence or 0) <= keep_after_sequence
        )
        tail = tuple(
            event
            for event in candidate_events
            if (event.sequence or 0) > keep_after_sequence
        )
        if not compacted:
            return None
        through_sequence = max(event.sequence or 0 for event in compacted)
        retained_transcript_tokens = _estimate_transcript_messages(
            model_visible_messages_from_events(tail),
            current_user_input=current_user_input_if_not_already_represented,
            target_model_target=target_model_target,
            previous_summary_text=None,
        )
        protected_transcript_tokens = sum(
            target_model_target.token_estimator.estimate_message(message)
            for message in protected_model_visible_messages_after
        )
        next_window_number = _next_window_number(events)
        window_id = f"context_window:{next_window_number}:{uuid4().hex}"
        replay_template = CompactionSummaryReplayTemplate(
            summary_artifact_id=_summary_artifact_id(compaction_id),
            compaction_id=compaction_id,
            window_id=window_id,
            through_sequence=through_sequence,
            keep_after_sequence=through_sequence,
        )
        summary_tokens_reserved = estimate_compaction_summary_replay_tokens(
            replay_template=replay_template,
            summary_text="x" * self.policy.max_summary_chars,
            target_model_target=target_model_target,
        )
        if (
            baseline is not None
            and baseline
            + summary_tokens_reserved
            + retained_transcript_tokens
            + protected_transcript_tokens
            > post_target
        ):
            raise CompactionTargetUnreachable(
                "mandatory retained context cannot meet the post-compaction target"
            )
        target_estimate = CompactionTargetEstimateFact(
            estimate_scope=estimate_scope,
            basis_context_id=basis.context_id if basis is not None else None,
            basis_context_compiled_sequence=basis.sequence
            if basis is not None
            else None,
            target_fingerprint=target_model_target.fact.target_fingerprint,
            non_transcript_baseline_tokens=baseline,
            transcript_tokens_before=transcript_tokens_before,
            estimated_tokens_before=estimated_before,
            summary_tokens_reserved=summary_tokens_reserved,
            retained_transcript_tokens=retained_transcript_tokens,
            protected_transcript_tokens=protected_transcript_tokens,
            summary_tokens_actual=None,
            transcript_tokens_after=None,
            estimated_tokens_after=None,
            predicted_post_target_reached=None,
        )
        return CompactionPlan(
            through_sequence=through_sequence,
            keep_after_sequence=through_sequence,
            target_estimate=target_estimate,
            threshold_tokens=threshold_tokens,
            post_compaction_target_tokens=post_target,
            retained_transcript_tokens=retained_transcript_tokens,
            protected_transcript_tokens=protected_transcript_tokens,
            included_run_ids=tuple(dict.fromkeys(event.run_id for event in compacted)),
            included_artifact_ids=tuple(_artifact_ids(compacted)),
            compacted_events=compacted,
            tail_events=tail,
            window_number=next_window_number,
            window_id=window_id,
            previous_summary_artifact_id=(
                latest_boundary.event.summary_artifact_id
                if latest_boundary is not None
                else None
            ),
            previous_summary_text=latest_boundary.summary_text
            if latest_boundary is not None
            else None,
        )


def production_compaction_prompt(*, memory_candidates_enabled: bool = True) -> str:
    prompt = (
        resources.files(_PRODUCTION_PROMPT_PACKAGE)
        .joinpath(_PRODUCTION_PROMPT_FILE)
        .read_text(encoding="utf-8")
    )
    if memory_candidates_enabled:
        return prompt
    return _without_memory_candidate_instructions(prompt)


def _without_memory_candidate_instructions(prompt: str) -> str:
    prompt = prompt.replace(
        "- Your entire response must be plain text: an <analysis> block followed by a <summary> block, plus an optional <memory_candidates_json> block only when durable-memory candidate extraction is useful.",
        "- Your entire response must be plain text: an <analysis> block followed by a <summary> block.",
    )
    prompt = prompt.replace(
        "   - You may optionally propose durable-memory candidates in <memory_candidates_json>; those proposals are pending observations only and governance decides whether to persist them.\n",
        "",
    )
    optional_start = prompt.find("\nOptional memory-candidate block:")
    rules_start = prompt.find("\nRules:", optional_start)
    if optional_start != -1 and rules_start != -1:
        prompt = prompt[:optional_start] + prompt[rules_start:]
    return prompt


def _memory_candidate_extraction_enabled(
    sink: CompactionMemoryCandidateSink | None,
    trigger: ContextCompactionTrigger,
    *,
    phase: str | None,
    policy: ContextCompactionMemoryCandidatePolicy,
) -> bool:
    if sink is None:
        return False
    if sink.workspace_kind == "transient":
        return False
    if not sink.workspace_scope:
        return False
    return _should_extract_memory_candidates(trigger, phase=phase, policy=policy)


def _should_extract_memory_candidates(
    trigger: ContextCompactionTrigger,
    *,
    phase: str | None,
    policy: ContextCompactionMemoryCandidatePolicy,
) -> bool:
    if not policy.enabled:
        return False
    if phase == "mid_turn":
        return policy.extract_on_mid_turn
    if trigger == "manual":
        return policy.extract_on_manual
    return policy.extract_on_preflight


def _extraction_attempted(parse_result: CompactionCandidateParseResult) -> bool:
    return bool(
        parse_result.attempted_count
        or parse_result.candidates
        or parse_result.skipped
        or parse_result.diagnostics
    )


def _event_diagnostic(
    diagnostic: CompactionCandidateDiagnostic,
) -> CompactionCandidateDiagnosticEvent:
    return CompactionCandidateDiagnosticEvent(
        code=diagnostic.code,
        field=diagnostic.field,
        message=diagnostic.message,
        redacted=diagnostic.redacted,
    )


def _skipped_item_diagnostics(
    result: CompactionCandidateParseResult | CompactionCandidateAppendResult,
) -> tuple[CompactionCandidateDiagnostic, ...]:
    return tuple(
        CompactionCandidateDiagnostic(
            code=f"compaction_candidate_skipped:{item.code}",
            message=item.reason,
            redacted=item.redacted,
        )
        for item in result.skipped
    )


def build_compaction_input(plan: CompactionPlan) -> str:
    lines = [
        "# Pulsara compaction input",
        "",
        "The following canonical event-derived transcript prefix will be summarized.",
        f"through_sequence: {plan.through_sequence}",
        f"keep_after_sequence: {plan.keep_after_sequence}",
        f"included_run_ids: {', '.join(plan.included_run_ids)}",
        f"included_artifact_ids: {', '.join(plan.included_artifact_ids) or '(none)'}",
        f"estimated_model_visible_tokens_before: {plan.target_estimate.estimated_tokens_before}",
        f"estimate_scope: {plan.target_estimate.estimate_scope}",
        "",
        "## Event-derived messages and observations",
        "",
    ]
    if plan.previous_summary_text:
        lines.extend(
            [
                "## Previous compact summary to carry forward",
                "",
                (
                    "The next summary MUST preserve this previous handoff unless newer compacted events "
                    "explicitly supersede or correct it. Do not drop older context merely because only the "
                    "latest completed boundary is replayed at resume time."
                ),
                f"previous_summary_artifact_id: {plan.previous_summary_artifact_id or '(unknown)'}",
                "",
                plan.previous_summary_text.strip(),
                "",
            ]
        )
    lines.append(build_compaction_observation_text(plan.compacted_events))
    return "\n".join(lines)


def build_metadata_only_compaction_input(plan: CompactionPlan) -> str:
    """Deterministic degraded summarizer input without verbose event payloads."""

    return "\n".join(
        [
            "# Pulsara compaction metadata-only input",
            f"through_sequence: {plan.through_sequence}",
            f"keep_after_sequence: {plan.keep_after_sequence}",
            f"included_run_ids: {', '.join(plan.included_run_ids)}",
            f"included_artifact_ids: {', '.join(plan.included_artifact_ids) or '(none)'}",
            f"compacted_event_count: {len(plan.compacted_events)}",
            f"previous_summary_artifact_id: {plan.previous_summary_artifact_id or '(none)'}",
            "",
            "The detailed event representation exceeded the summarizer input budget. "
            "Summarize only the bounded metadata above and any previous summary below.",
            "",
            plan.previous_summary_text or "",
        ]
    )


def estimate_compaction_summary_replay_tokens(
    *,
    replay_template: CompactionSummaryReplayTemplate,
    summary_text: str,
    target_model_target: ResolvedModelTarget,
) -> int:
    rendered = render_compaction_summary(
        summary_text,
        summary_artifact_id=replay_template.summary_artifact_id,
        compaction_id=replay_template.compaction_id,
        window_id=replay_template.window_id,
        through_sequence=replay_template.through_sequence,
        keep_after_sequence=replay_template.keep_after_sequence,
    )
    return target_model_target.token_estimator.estimate_message(
        LLMMessage.system(rendered)
    )


def _estimate_transcript_messages(
    messages: list[Msg] | tuple[Msg, ...],
    *,
    current_user_input: str,
    target_model_target: ResolvedModelTarget,
    previous_summary_text: str | None,
) -> int:
    estimator = target_model_target.token_estimator
    total = sum(
        estimator.estimate_message(_message_for_target_estimate(message))
        for message in messages
    )
    if current_user_input:
        total += estimator.estimate_message(LLMMessage.user(current_user_input))
    if previous_summary_text:
        total += estimator.estimate_message(LLMMessage.system(previous_summary_text))
    return total


def _message_for_target_estimate(message: Msg) -> LLMMessage:
    text = _message_text_for_estimate(message)
    if message.role == "system":
        return LLMMessage.system(text)
    if message.role == "assistant":
        return LLMMessage.assistant(text)
    if message.role == "tool_result":
        return LLMMessage.tool_result(text)
    return LLMMessage.user(text)


def _latest_matching_context_compiled_event(
    events: list[AgentEvent] | tuple[AgentEvent, ...],
    *,
    target_fingerprint: str,
    max_sequence: int | None,
) -> ContextCompiledEvent | None:
    for event in reversed(events):
        if not isinstance(event, ContextCompiledEvent):
            continue
        if max_sequence is not None and (event.sequence or 0) > max_sequence:
            continue
        if (
            event.status != "compiled"
            or event.budget.measurement_stage != "final_payload"
        ):
            continue
        if event.resolved_call.target.target_fingerprint != target_fingerprint:
            continue
        if event.budget.non_transcript_baseline_tokens is None:
            continue
        return event
    return None


def _keep_after_sequence_for_recent_runs(
    events: list[AgentEvent], keep_recent_runs: int
) -> int:
    run_starts = [
        event
        for event in events
        if isinstance(event, RunStartEvent) and event.sequence is not None
    ]
    if len(run_starts) <= keep_recent_runs:
        return 0
    first_kept_run = run_starts[-keep_recent_runs]
    return (first_kept_run.sequence or 0) - 1


def _next_window_number(events: list[AgentEvent]) -> int:
    completed = [event for event in events if hasattr(event, "window_number")]
    numbers = [
        int(getattr(event, "window_number"))
        for event in completed
        if getattr(event, "window_number", None)
    ]
    return max(numbers, default=0) + 1


def _artifact_ids(events: tuple[AgentEvent, ...]) -> list[str]:
    artifact_ids: list[str] = []
    for event in events:
        if isinstance(event, ToolResultEndEvent):
            artifact_ids.extend(artifact.artifact_id for artifact in event.artifacts)
    return list(dict.fromkeys(artifact_ids))


def model_visible_messages_from_events(
    events: list[AgentEvent] | tuple[AgentEvent, ...],
) -> list[Msg]:
    """Build a lightweight model-visible transcript estimate without Host imports."""

    messages: list[Msg] = []
    assistant_blocks_by_reply: dict[str, list[object]] = {}
    assembler = BlockAssembler()
    for event in events:
        if isinstance(event, RunStartEvent):
            user_input = event.metadata.get("user_input")
            if isinstance(user_input, str):
                messages.append(
                    UserMsg(
                        name="user",
                        content=user_input,
                        id=f"user-message:{event.run_id}",
                        created_at=event.created_at,
                        metadata={"run_id": event.run_id},
                    )
                )
            continue
        for completion in assembler.append(event).completed:
            block = completion.block
            if isinstance(block, ThinkingBlock):
                continue
            assistant_blocks_by_reply.setdefault(completion.reply_id, []).append(block)
        if hasattr(event, "type") and str(event.type) == "REPLY_END":
            blocks = assistant_blocks_by_reply.pop(event.reply_id, [])
            if blocks:
                messages.append(
                    AssistantMsg(
                        name="assistant",
                        content=blocks,
                        id=event.reply_id,
                        created_at=getattr(event, "created_at", None),
                    )
                )
    for reply_id, blocks in assistant_blocks_by_reply.items():
        if blocks:
            messages.append(AssistantMsg(name="assistant", content=blocks, id=reply_id))
    return messages


def build_compaction_observation_text(
    events: list[AgentEvent] | tuple[AgentEvent, ...],
) -> str:
    lines: list[str] = []
    assembler = BlockAssembler()
    for event in events:
        if isinstance(event, RunStartEvent):
            user_input = event.metadata.get("user_input")
            rendered = (
                user_input
                if isinstance(user_input, str)
                else f"[user_input_chars={event.user_input_chars}]"
            )
            lines.append(
                f"[user run_id={event.run_id}] {_clip_text(rendered, _COMPACTION_TEXT_CLIP_CHARS)}"
            )
            continue
        if isinstance(event, RunEndEvent):
            if event.status != "finished" or event.abort_kind or event.error_message:
                lines.append(
                    "[run_end "
                    f"run_id={event.run_id} status={event.status} "
                    f"abort_kind={event.abort_kind or '(none)'} "
                    f"error={_clip_text(event.error_message or '', 500)}]"
                )
            continue
        if isinstance(event, RunErrorEvent):
            lines.append(
                f"[run_error run_id={event.run_id} code={event.code}] {_clip_text(event.message, 1000)}"
            )
            continue
        if isinstance(event, ExceedMaxItersEvent):
            lines.append(
                f"[exceed_max_iters run_id={event.run_id} name={event.name} max_iters={event.max_iters}]"
            )
            continue
        if isinstance(
            event,
            (
                PlanModeEnteredEvent,
                PlanQuestionAskedEvent,
                PlanQuestionAnsweredEvent,
                PlanExitRequestedEvent,
                PlanExitResolvedEvent,
                PlanModeExitedEvent,
            ),
        ):
            lines.append(_event_line(event))
            continue
        for completion in assembler.append(event).completed:
            rendered = _render_completed_block(completion.block)
            if rendered:
                lines.append(rendered)
    return "\n".join(lines)


def _render_completed_block(block: object) -> str:
    if isinstance(block, TextBlock):
        return f"[assistant] {_clip_text(block.text, _COMPACTION_TEXT_CLIP_CHARS)}"
    if isinstance(block, ThinkingBlock):
        return ""
    if isinstance(block, ToolCallBlock):
        return (
            f"[tool_call id={block.id} name={block.name} state={block.state}] "
            f"{_clip_text(block.input, _COMPACTION_TOOL_INPUT_CLIP_CHARS)}"
        )
    if isinstance(block, ToolResultBlock):
        artifact_text = ""
        if block.artifacts:
            refs = ", ".join(
                _artifact_ref_summary(artifact) for artifact in block.artifacts
            )
            artifact_text = f" artifacts=[{refs}]"
        output = "\n".join(_block_text(item) for item in block.output)
        return (
            f"[tool_result id={block.id} name={block.name} state={block.state}{artifact_text}] "
            f"{_clip_text(output, _COMPACTION_TOOL_RESULT_CLIP_CHARS)}"
        )
    if isinstance(block, HintBlock):
        return f"[hint source={block.source or '(unknown)'}] {_clip_text(_block_text(block), 1000)}"
    if isinstance(block, DataBlock):
        return _block_text(block)
    return ""


def _artifact_ref_summary(artifact) -> str:
    base = f"{artifact.artifact_id}({artifact.media_type}, {artifact.size_bytes} bytes)"
    preview = getattr(artifact, "preview", None)
    if preview is None:
        return base
    read_more = getattr(preview, "read_more", {}) or {}
    suggested_offset = read_more.get("suggested_offset_chars")
    return (
        f"{base}, preview_policy={preview.preview_policy}, "
        f"visible_head_chars={preview.visible_head_chars}, visible_tail_chars={preview.visible_tail_chars}, "
        f"suggested_offset_chars={suggested_offset}"
    )


def _block_text(block: object) -> str:
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, ThinkingBlock):
        return block.thinking
    if isinstance(block, HintBlock):
        if isinstance(block.hint, str):
            return block.hint
        return "\n".join(_block_text(item) for item in block.hint)
    if isinstance(block, DataBlock):
        source = block.source
        media_type = getattr(source, "media_type", "application/octet-stream")
        if hasattr(source, "url"):
            return f"[data media_type={media_type} url={source.url}]"
        data = getattr(source, "data", "")
        return f"[data media_type={media_type} chars={len(data)}]"
    return str(block)


def _message_text_for_estimate(message: Msg) -> str:
    parts = [f"[{message.role} name={message.name} id={message.id}]"]
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ThinkingBlock):
            parts.append(block.thinking)
        elif isinstance(block, ToolCallBlock):
            parts.append(
                f"[tool_call id={block.id} name={block.name} state={block.state}] {block.input}"
            )
        elif isinstance(block, ToolResultBlock):
            artifacts = " ".join(artifact.artifact_id for artifact in block.artifacts)
            output = "\n".join(_block_text(item) for item in block.output)
            parts.append(
                f"[tool_result id={block.id} name={block.name} state={block.state} artifacts={artifacts}] {output}"
            )
        elif isinstance(block, HintBlock):
            parts.append(
                f"[hint source={block.source or '(unknown)'}] {_block_text(block)}"
            )
        elif isinstance(block, DataBlock):
            parts.append(_block_text(block))
        else:
            parts.append(str(block))
    return "\n".join(part for part in parts if part)


def _clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n[CLIPPED: kept {limit} of {len(text)} chars]"
    return text[: max(0, limit - len(marker))] + marker


def _events_text_for_estimate(events: list[AgentEvent] | tuple[AgentEvent, ...]) -> str:
    return "\n".join(_event_line(event) for event in events)


def _event_line(event: AgentEvent) -> str:
    payload = event.model_dump(mode="json")
    compact: dict[str, object] = {
        "sequence": event.sequence,
        "type": str(event.type),
        "run_id": event.run_id,
    }
    for key in (
        "user_input_chars",
        "status",
        "stop_reason",
        "abort_kind",
        "error_message",
        "delta",
        "tool_call_name",
        "tool_call_id",
        "state",
        "artifacts",
        "question",
        "answer_text",
        "selected_option",
        "decision",
        "summary",
        "accepted_plan_summary",
        "accepted_plan_artifact_id",
    ):
        if key in payload:
            compact[key] = payload[key]
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("user_input", "kind", "runtime_instruction"):
            if key in metadata:
                compact[f"metadata.{key}"] = metadata[key]
    return repr(compact)


def _event_context_for_compaction(events: list[AgentEvent]) -> EventContext:
    latest = events[-1]
    return EventContext(
        run_id=latest.run_id, turn_id=latest.turn_id, reply_id=latest.reply_id
    )


def _summary_artifact_id(compaction_id: str) -> str:
    return compaction_id.replace(":", "_") + ":summary"
