"""Durable-memory producer hook.

Bridges the agent loop to the durable-memory write path. Memory candidates are
deposited into a :class:`MemoryProposalSink` from tool-execution threads (by the
``remember_*`` tools); this hook drains them at agent-loop-safe points and
appends them to the durable candidate pool. Canonical ``mem:*`` writes are owned
by memory governance, not by this producer hook.
"""

from __future__ import annotations

import hashlib
from dataclasses import KW_ONLY, dataclass, field
from datetime import timedelta

from pulsara_agent.event import AgentEvent
from pulsara_agent.event.candidates import ValidCandidatePayload
from pulsara_agent.event_log import EventLog
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    CandidatePool,
    CandidatePoolProposal,
    PooledMemoryCandidate,
)
from pulsara_agent.memory.recall.projection import ProjectionBuilder
from pulsara_agent.memory.recall.projection_ledger import ProjectionLedger
from pulsara_agent.memory.canonical.query import MemoryQuery
from pulsara_agent.memory.recall.service import MemoryRecallService, RecallQuery, RecallStatus, RecallTrigger
from pulsara_agent.memory.reflection.engine import (
    MemoryReflectionEngine,
    MemoryReflectionHint,
    cheap_memory_hints,
)
from pulsara_agent.memory.scope import CTX_USER, MemoryDomainContext, format_scope_list
from pulsara_agent.memory.working_context import (
    PostgresWorkingContextStore,
    WorkingContextSummary,
    propose_working_context_update,
    working_context_projection,
)
from pulsara_agent.message import Msg, TextBlock, ToolResultBlock
from pulsara_agent.runtime.hooks import NoopMemoryHooks
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.runtime.state import LoopState, LoopStatus
from pulsara_agent.runtime.timeline import build_run_timeline
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    CandidateQuotedEvidenceLocatorFact,
)


@dataclass(slots=True)
class DurableMemoryHooks(NoopMemoryHooks):
    candidate_pool: CandidatePool
    sink: MemoryProposalSink
    _: KW_ONLY
    event_store: EventLog | None = None
    recall: MemoryRecallService | None = None
    memory_query: MemoryQuery | None = None
    projector: ProjectionBuilder = field(default_factory=ProjectionBuilder)
    projection_ledger: ProjectionLedger = field(default_factory=ProjectionLedger)
    graph_id: str | None = None
    read_scopes: frozenset[str] | None = None
    working_context_store: PostgresWorkingContextStore | None = None
    working_context_domain: MemoryDomainContext | None = None
    working_context_ttl: timedelta | None = timedelta(days=14)

    @property
    def memory_proposal_sink(self) -> MemoryProposalSink | None:
        return self.sink

    def baseline_projection(self, state: LoopState, *, token_budget: int) -> dict | None:
        # Recent working context remains operational state.  It is deliberately
        # not projected into provider input until it has its own typed authority.
        return None

    async def project(self, state: LoopState, *, token_budget: int) -> dict | None:
        if self.recall is None:
            return None
        latest_user_text = _latest_user_quote(state)
        if latest_user_text is None or _should_skip_recall(latest_user_text):
            return None
        cache_key = "durable_memory_recall_projection_cache"
        cached = state.scratchpad.get(cache_key)
        if isinstance(cached, dict) and cached.get("query_text") == latest_user_text:
            cached_projection = cached.get("projection")
            return cached_projection if isinstance(cached_projection, dict) else None
        query = RecallQuery(
            text=latest_user_text,
            scopes=_recall_scopes(self.read_scopes),
            limit=5,
            trigger=RecallTrigger.CHEAP_AUTO,
            session_id=state.session_id,
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
        )
        result = await self.recall.recall(query, graph_id=self.graph_id)
        if result.status is not RecallStatus.OK or not result.items:
            state.scratchpad[cache_key] = {"query_text": latest_user_text, "projection": None}
            return None
        self.projection_ledger.record(state, result.items)
        recalled = self.projector.build(result, token_budget=token_budget)
        state.scratchpad[cache_key] = {"query_text": latest_user_text, "projection": recalled}
        return recalled

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
        self._finalize_invalid_to_pool(state)
        self._update_working_context(state)
        return []

    def _drain_to_pool(self, state: LoopState) -> list[PooledMemoryCandidate]:
        proposals = self.sink.drain_valid()
        return self._append_to_pool(state, proposals)

    def _finalize_invalid_to_pool(self, state: LoopState) -> list[PooledMemoryCandidate]:
        proposals = self.sink.finalize_invalid_attempts()
        return self._append_to_pool(state, proposals)

    def _append_to_pool(
        self,
        state: LoopState,
        proposals: list[CandidatePoolProposal],
    ) -> list[PooledMemoryCandidate]:
        pooled: list[PooledMemoryCandidate] = []
        for proposal in proposals:
            if self._is_projection_echo(proposal, state):
                continue
            candidate = proposal.to_pooled(
                source_session_id=state.session_id,
                source_run_id=state.run_id,
                source_turn_id=state.turn_id,
                source_reply_id=state.reply_id,
            )
            if candidate.user_quote is None:
                quote = _latest_user_quote_with_locator(state)
                if quote is not None:
                    text, message_id, start_char, end_char = quote
                    candidate = candidate.model_copy(
                        update={
                            "user_quote": text,
                            "quoted_evidence_locator": build_frozen_fact(
                                CandidateQuotedEvidenceLocatorFact,
                                schema_version="candidate_quoted_evidence_locator.v1",
                                locator_kind="canonical_user_message_span",
                                source_message_id=message_id,
                                source_event_reference=None,
                                source_artifact_reference=None,
                                source_quote_index=None,
                                start_char=start_char,
                                end_char=end_char,
                                quoted_text_sha256=hashlib.sha256(
                                    text.encode("utf-8")
                                ).hexdigest(),
                            ),
                        }
                    )
            pooled.append(self.candidate_pool.append_candidate(candidate))
        return pooled

    def _is_projection_echo(self, proposal: CandidatePoolProposal, state: LoopState) -> bool:
        payload = proposal.payload
        if not isinstance(payload, ValidCandidatePayload):
            return False
        return self.projection_ledger.is_echo(payload.candidate.statement, state)

    def memory_context_prompt(self) -> str | None:
        if not self.read_scopes:
            return None
        scopes = format_scope_list(self.read_scopes)
        return (
            "Durable memory scope rules for this run:\n"
            f"- Visible scopes: {scopes}.\n"
            f"- Writable scopes: {scopes}.\n"
            "- Use ctx:user only for durable user-wide preferences or habits.\n"
            "- Use the exact visible ctx:workspace/<id> scope only for durable facts or decisions about the current project.\n"
            "- Do not create durable memory for one-off task details."
        )

    def _working_context_projection(self, *, token_budget: int) -> dict | None:
        if self.working_context_store is None or self.working_context_domain is None:
            return None
        summary = self.working_context_store.get_latest(
            memory_domain_id=self.working_context_domain.memory_domain_id
        )
        if summary is None:
            return None
        return working_context_projection(summary, token_budget=token_budget)

    def _update_working_context(self, state: LoopState) -> WorkingContextSummary | None:
        if (
            self.working_context_store is None
            or self.working_context_domain is None
            or self.event_store is None
        ):
            return None
        try:
            timeline = build_run_timeline(
                self.event_store.iter(run_id=state.run_id),
                runtime_session_id=state.session_id,
                run_id=state.run_id,
            )
        except ValueError:
            return None
        from pulsara_agent.memory.foundation.run_timeline_query import summarize_run_timeline

        existing = self.working_context_store.get_latest(
            memory_domain_id=self.working_context_domain.memory_domain_id
        )
        update = propose_working_context_update(
            summarize_run_timeline(timeline),
            existing_summary=existing,
        )
        if not update.should_update:
            return None
        return self.working_context_store.upsert(
            domain=self.working_context_domain,
            source_session_id=state.session_id,
            source_run_id=state.run_id,
            summary=update.summary,
            metadata=update.metadata | {"update_reason": update.reason},
            ttl=self.working_context_ttl,
        )


@dataclass(slots=True)
class ReflectiveMemoryHooks(DurableMemoryHooks):
    """Single authority for explicit proposals and Flash memory reflection."""

    reflection: MemoryReflectionEngine
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
        finalized_invalid = self._finalize_invalid_to_pool(state)
        self._remember_attempts(state, [*drained_candidates, *finalized_invalid])
        self._update_token_delta(state)
        try:
            events = await self._maybe_reflect(
                state,
                safe_point="on_session_end",
            )
            self._update_working_context(state)
            return events
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
        if state.status in {LoopStatus.ABORTED, LoopStatus.FAILED}:
            return []
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
    quote = _latest_user_quote_with_locator(state, max_chars=max_chars)
    return quote[0] if quote is not None else None


def _latest_user_quote_with_locator(
    state: LoopState,
    max_chars: int = 2_000,
) -> tuple[str, str, int, int] | None:
    for message in reversed(state.messages):
        if message.role != "user":
            continue
        text = "\n".join(block.text for block in message.content if isinstance(block, TextBlock)).strip()
        if not text:
            continue
        if len(text) <= max_chars:
            return text, message.id, 0, len(text)
        start = len(text) - max_chars
        return text[start:], message.id, start, len(text)
    return None


def _should_skip_recall(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    if len(normalized) < 8:
        return True
    skip_markers = (
        "ignore memory",
        "don't use memory",
        "do not use memory",
        "不要使用记忆",
        "忽略记忆",
    )
    return any(marker in normalized for marker in skip_markers)


def _recall_scopes(read_scopes: frozenset[str] | None) -> tuple[str, ...]:
    if read_scopes is None:
        return (CTX_USER,)
    return tuple(sorted(read_scopes))


def _merge_projections(first: dict | None, second: dict | None) -> dict | None:
    if first is None:
        return second
    if second is None:
        return first
    projection_kinds = _projection_kinds(first, second)
    return {
        "summary": "\n\n".join(
            part
            for part in (
                first.get("summary") if isinstance(first.get("summary"), str) else "",
                second.get("summary") if isinstance(second.get("summary"), str) else "",
            )
            if part
        ),
        "items": [*list(first.get("items") or []), *list(second.get("items") or [])],
        "included_memory_ids": [
            *list(first.get("included_memory_ids") or []),
            *list(second.get("included_memory_ids") or []),
        ],
        "filtered_memory_ids": [
            *list(first.get("filtered_memory_ids") or []),
            *list(second.get("filtered_memory_ids") or []),
        ],
        "conflict_groups": _merge_conflict_groups(first, second),
        "do_not_write_back": True,
        "projection_kind": projection_kinds[0] if len(projection_kinds) == 1 else "mixed",
        "projection_kinds": projection_kinds,
    }


def _projection_kinds(first: dict, second: dict) -> list[str]:
    kinds: list[str] = []
    for projection, fallback in ((first, "working_context"), (second, "recalled_memory")):
        kind = projection.get("projection_kind") or fallback
        if isinstance(kind, str) and kind not in kinds:
            kinds.append(kind)
    return kinds


def _merge_conflict_groups(first: dict, second: dict) -> list[dict]:
    groups: list[dict] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for projection in (first, second):
        for group in projection.get("conflict_groups") or []:
            if not isinstance(group, dict):
                continue
            kind = str(group.get("kind") or "")
            memory_ids = tuple(sorted(str(memory_id) for memory_id in group.get("memory_ids") or []))
            key = (kind, memory_ids)
            if not kind or not memory_ids or key in seen:
                continue
            seen.add(key)
            groups.append({"kind": kind, "memory_ids": list(memory_ids)})
    return groups
