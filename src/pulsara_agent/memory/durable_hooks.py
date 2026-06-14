"""Durable-memory producer hook.

Bridges the agent loop to the durable-memory write path. Memory candidates are
deposited into a :class:`MemoryProposalSink` from tool-execution threads (by the
``remember_*`` tools); this hook drains them at agent-loop-safe points and
appends them to the durable candidate pool. Canonical ``mem:*`` writes are owned
by memory governance, not by this producer hook.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log import EventLog
from pulsara_agent.memory.candidate_pool import CandidatePool, CandidateOrigin, PooledMemoryCandidate
from pulsara_agent.memory.reflection import (
    MemoryReflectionEngine,
    MemoryReflectionHint,
    cheap_memory_hints,
)
from pulsara_agent.message import Msg, TextBlock, ToolResultBlock
from pulsara_agent.runtime.hooks import NoopMemoryHooks
from pulsara_agent.runtime.proposal_sink import MemoryProposalSink
from pulsara_agent.runtime.state import LoopState


@dataclass(slots=True)
class DurableMemoryHooks(NoopMemoryHooks):
    candidate_pool: CandidatePool
    sink: MemoryProposalSink

    @property
    def memory_proposal_sink(self) -> MemoryProposalSink | None:
        return self.sink

    async def after_model_reply(self, state: LoopState, assistant: Msg) -> list[AgentEvent]:
        self._drain_to_pool(state)
        return []

    async def after_tool_results(
        self, state: LoopState, results: list[ToolResultBlock]
    ) -> list[AgentEvent]:
        self._drain_to_pool(state)
        return []

    async def on_session_end(self, state: LoopState) -> list[AgentEvent]:
        self._drain_to_pool(state)
        return []

    def _drain_to_pool(self, state: LoopState) -> list[PooledMemoryCandidate]:
        proposals = self.sink.drain()
        pooled: list[PooledMemoryCandidate] = []
        for proposal in proposals:
            candidate = proposal.to_pooled(
                source_session_id=state.session_id,
                source_run_id=state.run_id,
                source_turn_id=state.turn_id,
                source_reply_id=state.reply_id,
            )
            if candidate.user_quote is None:
                candidate = candidate.model_copy(update={"user_quote": _latest_user_quote(state)})
            pooled.append(self.candidate_pool.append_candidate(candidate))
        return pooled


@dataclass(slots=True)
class ReflectiveMemoryHooks(DurableMemoryHooks):
    """Single authority for explicit proposals and Flash memory reflection."""

    reflection: MemoryReflectionEngine
    event_store: EventLog
    turns_since_last_reflection: int = 0
    tool_calls_since_last_reflection: int = 0
    token_delta_since_last_reflection: int = 0
    last_reflection_run_id: str | None = None
    _cheap_hints_by_run: dict[str, list[MemoryReflectionHint]] = field(default_factory=dict)
    _last_token_total_by_run: dict[str, int] = field(default_factory=dict)
    _memory_attempts_by_run: set[str] = field(default_factory=set)

    async def on_session_start(self, state: LoopState, user_input: str) -> None:
        self.turns_since_last_reflection += 1
        hints = cheap_memory_hints(user_input)
        if hints:
            self._cheap_hints_by_run.setdefault(state.run_id, []).extend(hints)
        return None

    async def after_model_reply(self, state: LoopState, assistant: Msg) -> list[AgentEvent]:
        self._update_token_delta(state)
        self._remember_attempts(state, self._drain_to_pool(state))
        return []

    async def after_tool_results(
        self, state: LoopState, results: list[ToolResultBlock]
    ) -> list[AgentEvent]:
        drained_candidates = self._drain_to_pool(state)
        self._remember_attempts(state, drained_candidates)
        self.tool_calls_since_last_reflection += len(results)
        self._update_token_delta(state)
        return []

    async def on_session_end(self, state: LoopState) -> list[AgentEvent]:
        drained_candidates = self._drain_to_pool(state)
        self._remember_attempts(state, drained_candidates)
        self._update_token_delta(state)
        try:
            return await self._maybe_reflect(
                state,
                safe_point="on_session_end",
            )
        finally:
            self._cheap_hints_by_run.pop(state.run_id, None)
            self._last_token_total_by_run.pop(state.run_id, None)
            self._memory_attempts_by_run.discard(state.run_id)

    async def _maybe_reflect(
        self,
        state: LoopState,
        *,
        safe_point: str,
    ) -> list[AgentEvent]:
        trigger_reasons = self._trigger_reasons(state, safe_point=safe_point)
        if not trigger_reasons:
            return []
        cheap_hints = list(self._cheap_hints_by_run.get(state.run_id, []))
        reflection_events = await self.reflection.reflect(
            state=state,
            event_store=self.event_store,
            trigger_reasons=trigger_reasons,
            cheap_hints=cheap_hints,
            safe_point=safe_point,
        )
        self._mark_reflected(state)
        return reflection_events

    def _trigger_reasons(
        self,
        state: LoopState,
        *,
        safe_point: str,
    ) -> list[str]:
        reasons: list[str] = []
        has_memory_attempt = state.run_id in self._memory_attempts_by_run
        if safe_point == "on_session_end" and self._cheap_hints_by_run.get(state.run_id) and not has_memory_attempt:
            reasons.append("cheap_memory_hint")
        if self.last_reflection_run_id == state.run_id:
            return []
        return _unique(reasons)

    def _update_token_delta(self, state: LoopState) -> None:
        current = state.token_usage.total_tokens
        previous = self._last_token_total_by_run.get(state.run_id, 0)
        if current > previous:
            self.token_delta_since_last_reflection += current - previous
        self._last_token_total_by_run[state.run_id] = current

    def _mark_reflected(self, state: LoopState) -> None:
        self.last_reflection_run_id = state.run_id
        self.turns_since_last_reflection = 0
        self.tool_calls_since_last_reflection = 0
        self.token_delta_since_last_reflection = 0

    def _remember_attempts(self, state: LoopState, candidates: list[PooledMemoryCandidate]) -> None:
        if any(candidate.origin is CandidateOrigin.MAIN_AGENT_TOOL for candidate in candidates):
            self._memory_attempts_by_run.add(state.run_id)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _latest_user_quote(state: LoopState, max_chars: int = 2_000) -> str | None:
    for message in reversed(state.messages):
        if message.role != "user":
            continue
        text = "\n".join(block.text for block in message.content if isinstance(block, TextBlock)).strip()
        if not text:
            continue
        if len(text) <= max_chars:
            return text
        return text[-max_chars:]
    return None
