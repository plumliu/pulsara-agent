"""Mid-turn context compaction for active agent runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from pulsara_agent.event import (
    AgentEvent,
    ContextCompactionCompletedEvent,
    EventType,
    MidTurnContextCompactionSkippedEvent,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.message import Msg
from pulsara_agent.runtime.compaction.planner import latest_completed_boundary
from pulsara_agent.runtime.compaction.service import (
    ContextCompactionAttemptResult,
    ContextCompactionPublicationFailedAfterCommit,
    ContextCompactionService,
)
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.state import LoopState, LoopStatus, LoopTransition
from pulsara_agent.runtime.transcript import rebuild_prior_messages_before_sequence
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.runtime_event_vocabulary import (
    MidTurnCompactionSkipFact,
    RuntimeEventOperationDeadlineBudget,
    build_runtime_event_deadline_budget,
    stable_runtime_event_id,
)
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)


@dataclass(frozen=True, slots=True)
class MidTurnCompactionResult:
    compacted: bool
    events: tuple[AgentEvent, ...] = ()
    rewritten_messages: tuple[Msg, ...] | None = None
    skipped_reason: str | None = None
    publication_failure: ContextCompactionAttemptResult | None = None
    mandatory_audit_deadline_budget: (
        RuntimeEventOperationDeadlineBudget | None
    ) = None
    mandatory_audit_publication_failed: bool = False


class RuntimeContextCompactorProtocol(Protocol):
    async def maybe_compact_before_followup(
        self,
        *,
        state: LoopState,
        model_visible_messages: list[Msg],
        protected_model_visible_messages_after: tuple[LLMMessage, ...],
    ) -> MidTurnCompactionResult: ...


@dataclass(frozen=True, slots=True)
class NoopRuntimeContextCompactor:
    async def maybe_compact_before_followup(
        self,
        *,
        state: LoopState,
        model_visible_messages: list[Msg],
        protected_model_visible_messages_after: tuple[LLMMessage, ...],
    ) -> MidTurnCompactionResult:
        del state, model_visible_messages, protected_model_visible_messages_after
        return MidTurnCompactionResult(compacted=False, skipped_reason="disabled")


@dataclass(slots=True)
class RuntimeContextCompactor:
    event_log: EventLog
    archive: ArtifactStore
    runtime_session: RuntimeSession
    service: ContextCompactionService

    async def maybe_compact_before_followup(
        self,
        *,
        state: LoopState,
        model_visible_messages: list[Msg],
        protected_model_visible_messages_after: tuple[LLMMessage, ...],
    ) -> MidTurnCompactionResult:
        guard_reason = self._safe_point_guard(state)
        if guard_reason is not None:
            return MidTurnCompactionResult(compacted=False, skipped_reason=guard_reason)

        working_set = state.run_working_set
        current_run_start = (
            self.event_log.get_by_id(working_set.run_start_event_id)
            if working_set is not None
            else None
        )
        if current_run_start is None or current_run_start.sequence is None:
            diagnostic, deadline_budget, publication_failed = (
                await self._emit_skip_diagnostic(
                    state,
                    reason="current_run_start_missing",
                    current_run_start=None,
                )
            )
            return MidTurnCompactionResult(
                compacted=False,
                events=(diagnostic,),
                skipped_reason="current_run_start_missing",
                mandatory_audit_deadline_budget=deadline_budget,
                mandatory_audit_publication_failed=publication_failed,
            )

        current_run_start_sequence = current_run_start.sequence
        max_compactable_sequence = current_run_start_sequence - 1
        boundary_rows = await asyncio.to_thread(
            self.event_log.read_raw_events_by_type,
            str(EventType.CONTEXT_COMPACTION_COMPLETED),
            limit=8,
            through_sequence=max_compactable_sequence,
            deadline_monotonic=asyncio.get_running_loop().time() + 10.0,
        )
        boundary_events = tuple(
            raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY) for raw in boundary_rows
        )
        latest_boundary = latest_completed_boundary(
            boundary_events,
            archive=self.archive,
            session_id=self.runtime_session.runtime_session_id,
        )
        if (
            latest_boundary is not None
            and max_compactable_sequence <= latest_boundary.keep_after_sequence
        ):
            diagnostic, deadline_budget, publication_failed = (
                await self._emit_skip_diagnostic(
                    state,
                    reason="no_compactable_prefix_before_current_run",
                    current_run_start=current_run_start,
                )
            )
            return MidTurnCompactionResult(
                compacted=False,
                events=(diagnostic,),
                skipped_reason="no_compactable_prefix_before_current_run",
                mandatory_audit_deadline_budget=deadline_budget,
                mandatory_audit_publication_failed=publication_failed,
            )

        tail = _current_run_tail_from_state(state)
        if not tail:
            diagnostic, deadline_budget, publication_failed = (
                await self._emit_skip_diagnostic(
                    state,
                    reason="current_run_tail_missing",
                    current_run_start=current_run_start,
                )
            )
            return MidTurnCompactionResult(
                compacted=False,
                events=(diagnostic,),
                skipped_reason="current_run_tail_missing",
                mandatory_audit_deadline_budget=deadline_budget,
                mandatory_audit_publication_failed=publication_failed,
            )

        if not protected_model_visible_messages_after:
            diagnostic, deadline_budget, publication_failed = (
                await self._emit_skip_diagnostic(
                    state,
                    reason="current_run_rendered_tail_missing",
                    current_run_start=current_run_start,
                )
            )
            return MidTurnCompactionResult(
                compacted=False,
                events=(diagnostic,),
                skipped_reason="current_run_rendered_tail_missing",
                mandatory_audit_deadline_budget=deadline_budget,
                mandatory_audit_publication_failed=publication_failed,
            )

        if state.run_model_target is None:
            raise RuntimeError(
                "mid-turn compaction requires the frozen run model target"
            )
        try:
            attempt = await self.service.compact_if_needed(
                target_model_target=state.run_model_target,
                model_visible_messages_before=model_visible_messages,
                protected_model_visible_messages_after=(
                    protected_model_visible_messages_after
                ),
                current_user_input_if_not_already_represented="",
                reason="mid_turn_context_threshold",
                max_compactable_sequence=max_compactable_sequence,
                keep_recent_runs_override=1,
                event_metadata={
                    "phase": "mid_turn",
                    "safe_point": "before_followup_model_call",
                    "current_run_id": state.run_id,
                    "current_run_start_sequence": current_run_start_sequence,
                    "max_compactable_sequence": max_compactable_sequence,
                    "tail_message_count": len(tail),
                    "model_visible_message_count": len(model_visible_messages),
                },
                runtime_state=state,
            )
        except ContextCompactionPublicationFailedAfterCommit as error:
            return MidTurnCompactionResult(
                compacted=False,
                events=error.result.core_committed_events,
                skipped_reason="publication_failed_after_commit",
                publication_failure=error.result,
            )
        compaction_events = attempt.core_committed_events

        if not attempt:
            return MidTurnCompactionResult(
                compacted=False,
                events=tuple(compaction_events),
                skipped_reason="not_needed",
            )

        completed = (
            attempt.terminal_event
            if isinstance(attempt.terminal_event, ContextCompactionCompletedEvent)
            else None
        )
        if completed is None:
            return MidTurnCompactionResult(
                compacted=False,
                events=tuple(compaction_events),
                skipped_reason="completed_event_missing",
            )
        prefix = await asyncio.to_thread(
            rebuild_prior_messages_before_sequence,
            self.event_log,
            archive=self.archive,
            session_id=self.runtime_session.runtime_session_id,
            before_sequence=current_run_start_sequence,
        )
        rewritten = tuple(message.model_copy(deep=True) for message in [*prefix, *tail])
        state.compacted = True
        return MidTurnCompactionResult(
            compacted=True,
            events=tuple(compaction_events),
            rewritten_messages=rewritten,
        )

    def _safe_point_guard(self, state: LoopState) -> str | None:
        if state.status is not LoopStatus.RUNNING:
            return "state_not_running"
        if state.last_transition is not LoopTransition.CONTINUE_AFTER_TOOL:
            return "not_after_tool"
        if state.pending_interaction_kind is not None:
            return "pending_interaction"
        if state.stop_request is not None:
            return "stop_requested"
        completed_tool_ids = {result.id for result in state.tool_results}
        missing_results = [
            call.id
            for call in state.pending_tool_calls
            if call.id not in completed_tool_ids
        ]
        if missing_results:
            return "pending_tool_results"
        return None

    async def _emit_skip_diagnostic(
        self,
        state: LoopState,
        *,
        reason: str,
        current_run_start: AgentEvent | None,
    ) -> tuple[
        AgentEvent,
        RuntimeEventOperationDeadlineBudget,
        bool,
    ]:
        start_ref = (
            event_reference_from_stored(
                current_run_start,
                runtime_session_id=self.runtime_session.runtime_session_id,
            )
            if current_run_start is not None
            else None
        )
        skip = build_frozen_fact(
            MidTurnCompactionSkipFact,
            schema_version="mid_turn_context_compaction_skip.v1",
            reason=reason,
            current_run_start_event_reference=start_ref,
            safe_point="before_followup_model_call",
        )
        candidate = MidTurnContextCompactionSkippedEvent(
            id=stable_runtime_event_id(
                "mid-turn-context-compaction-skipped-event:v1",
                state.run_id,
                skip.skip_semantic_fingerprint,
            ),
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
            skip=skip,
        )
        deadline_budget = build_runtime_event_deadline_budget(
            admitted_at_monotonic=monotonic(),
            total_timeout_seconds=30.0,
            terminal_reserve_seconds=10.0,
        )
        receipt = await self.runtime_session.mandatory_runtime_audit_owner.commit(
            candidate,
            deadline_budget=deadline_budget,
            state=state,
        )
        if receipt.status != "full":
            raise RuntimeError("mid-turn compaction skip requires reconciliation")
        stored = self.event_log.get_by_id(candidate.id)
        if not isinstance(stored, MidTurnContextCompactionSkippedEvent):
            raise RuntimeError("mid-turn compaction skip cannot be rebound")
        return (
            stored,
            deadline_budget,
            receipt.publication_summary not in {"completed", "enqueued"},
        )

def _current_run_tail_from_state(state: LoopState) -> list[Msg]:
    tail: list[Msg] = []
    in_current_run = False
    user_message_id = f"user-message:{state.run_id}"
    for message in state.messages:
        metadata_run_id = (
            message.metadata.get("run_id")
            if isinstance(message.metadata, dict)
            else None
        )
        if metadata_run_id == state.run_id or message.id == user_message_id:
            in_current_run = True
        if in_current_run:
            tail.append(message.model_copy(deep=True))
    return tail
