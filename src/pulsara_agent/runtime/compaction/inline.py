"""Mid-turn context compaction for active agent runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from pulsara_agent.event import (
    AgentEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionMemoryCandidatesProposedEvent,
    ContextCompactionStartedEvent,
    CustomEvent,
    RunStartEvent,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.message import Msg
from pulsara_agent.runtime.compaction.planner import latest_completed_boundary
from pulsara_agent.runtime.compaction.service import ContextCompactionService
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.state import LoopState, LoopStatus, LoopTransition
from pulsara_agent.runtime.transcript import rebuild_prior_messages_before_sequence


@dataclass(frozen=True, slots=True)
class MidTurnCompactionResult:
    compacted: bool
    events: tuple[AgentEvent, ...] = ()
    rewritten_messages: tuple[Msg, ...] | None = None
    skipped_reason: str | None = None


class RuntimeContextCompactorProtocol(Protocol):
    async def maybe_compact_before_followup(
        self,
        *,
        state: LoopState,
        model_visible_messages: list[Msg],
    ) -> MidTurnCompactionResult: ...


@dataclass(frozen=True, slots=True)
class NoopRuntimeContextCompactor:
    async def maybe_compact_before_followup(
        self,
        *,
        state: LoopState,
        model_visible_messages: list[Msg],
    ) -> MidTurnCompactionResult:
        del state, model_visible_messages
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
    ) -> MidTurnCompactionResult:
        guard_reason = self._safe_point_guard(state)
        if guard_reason is not None:
            return MidTurnCompactionResult(compacted=False, skipped_reason=guard_reason)

        events = await asyncio.to_thread(self.event_log.iter)
        current_run_start = _current_run_start(events, state.run_id)
        if current_run_start is None or current_run_start.sequence is None:
            diagnostic = await self._emit_skip_diagnostic(
                state,
                reason="current_run_start_missing",
                current_run_start_sequence=None,
                max_compactable_sequence=None,
            )
            return MidTurnCompactionResult(
                compacted=False,
                events=(diagnostic,),
                skipped_reason="current_run_start_missing",
            )

        current_run_start_sequence = current_run_start.sequence
        max_compactable_sequence = current_run_start_sequence - 1
        latest_boundary = latest_completed_boundary(
            events,
            archive=self.archive,
            session_id=self.runtime_session.runtime_session_id,
        )
        if latest_boundary is not None and max_compactable_sequence <= latest_boundary.keep_after_sequence:
            diagnostic = await self._emit_skip_diagnostic(
                state,
                reason="no_compactable_prefix_before_current_run",
                current_run_start_sequence=current_run_start_sequence,
                max_compactable_sequence=max_compactable_sequence,
            )
            return MidTurnCompactionResult(
                compacted=False,
                events=(diagnostic,),
                skipped_reason="no_compactable_prefix_before_current_run",
            )

        tail = _current_run_tail_from_state(state)
        if not tail:
            diagnostic = await self._emit_skip_diagnostic(
                state,
                reason="current_run_tail_missing",
                current_run_start_sequence=current_run_start_sequence,
                max_compactable_sequence=max_compactable_sequence,
            )
            return MidTurnCompactionResult(
                compacted=False,
                events=(diagnostic,),
                skipped_reason="current_run_tail_missing",
            )

        before_sequence = await asyncio.to_thread(self.event_log.next_sequence)
        try:
            compacted = await self.service.compact_if_needed(
                model_visible_messages=model_visible_messages,
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
            )
        finally:
            compaction_events = await asyncio.to_thread(self._compaction_events_after, before_sequence - 1)
            self.runtime_session.publish_stored_events(compaction_events, state=state)

        if not compacted:
            return MidTurnCompactionResult(compacted=False, events=tuple(compaction_events), skipped_reason="not_needed")

        completed = _latest_completed(compaction_events)
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
        rewritten = tuple(
            message.model_copy(deep=True)
            for message in [*prefix, *tail]
        )
        state.compacted = True
        state.scratchpad["mid_turn_compaction"] = {
            "compaction_id": completed.compaction_id,
            "phase": "mid_turn",
            "safe_point": "before_followup_model_call",
            "current_run_id": state.run_id,
            "current_run_start_sequence": current_run_start_sequence,
            "tail_message_count": len(tail),
        }
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
        missing_results = [call.id for call in state.pending_tool_calls if call.id not in completed_tool_ids]
        if missing_results:
            return "pending_tool_results"
        return None

    async def _emit_skip_diagnostic(
        self,
        state: LoopState,
        *,
        reason: str,
        current_run_start_sequence: int | None,
        max_compactable_sequence: int | None,
    ) -> AgentEvent:
        return await self.runtime_session.emit(
            CustomEvent(
                run_id=state.run_id,
                turn_id=state.turn_id,
                reply_id=state.reply_id,
                name="mid_turn_compaction_skipped",
                value={
                    "reason": reason,
                    "current_run_id": state.run_id,
                    "current_run_start_sequence": current_run_start_sequence,
                    "max_compactable_sequence": max_compactable_sequence,
                },
            ),
            state=state,
        )

    def _compaction_events_after(self, after_sequence: int) -> list[AgentEvent]:
        return [
            event
            for event in self.event_log.iter(after_sequence=after_sequence)
            if isinstance(
                event,
                (
                    ContextCompactionStartedEvent,
                    ContextCompactionCompletedEvent,
                    ContextCompactionMemoryCandidatesProposedEvent,
                    ContextCompactionFailedEvent,
                ),
            )
        ]


def _current_run_start(events: list[AgentEvent], run_id: str) -> RunStartEvent | None:
    for event in events:
        if isinstance(event, RunStartEvent) and event.run_id == run_id:
            return event
    return None


def _current_run_tail_from_state(state: LoopState) -> list[Msg]:
    tail: list[Msg] = []
    in_current_run = False
    user_message_id = f"user-message:{state.run_id}"
    for message in state.messages:
        metadata_run_id = message.metadata.get("run_id") if isinstance(message.metadata, dict) else None
        if metadata_run_id == state.run_id or message.id == user_message_id:
            in_current_run = True
        if in_current_run:
            tail.append(message.model_copy(deep=True))
    return tail


def _latest_completed(events: list[AgentEvent]) -> ContextCompactionCompletedEvent | None:
    for event in reversed(events):
        if isinstance(event, ContextCompactionCompletedEvent):
            return event
    return None
