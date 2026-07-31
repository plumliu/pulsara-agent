"""Durable-memory producer hook.

Bridges the agent loop to the durable-memory write path. Memory candidates are
deposited into a :class:`MemoryProposalSink` from tool-execution threads (by the
``remember_*`` tools); this hook drains them at agent-loop-safe points and
appends them to the durable candidate pool. Canonical ``mem:*`` writes are owned
by memory governance, not by this producer hook.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import KW_ONLY, dataclass, field
from datetime import timedelta
from time import monotonic

from pulsara_agent.event import AgentEvent, EventType, RunEndEvent
from pulsara_agent.primitives.memory_candidate import ValidCandidatePayload
from pulsara_agent.event_log import EventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    CandidatePool,
    CandidatePoolProposal,
    PooledMemoryCandidate,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.memory.recall.projection import ProjectionBuilder
from pulsara_agent.memory.recall.projection_ledger import ProjectionLedger
from pulsara_agent.memory.canonical.query import MemoryQuery
from pulsara_agent.memory.recall.service import (
    MemoryRecallService,
    RecallQuery,
    RecallStatus,
    RecallTrigger,
)
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
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.memory.hooks.run_owner import (
    MemoryHookRunOwner,
    MemoryHookRunOwnerRegistry,
)
from pulsara_agent.ports.memory_hooks import MemoryHookRunView, NoopMemoryHooks
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
    timeline_graph: object | None = None
    timeline_archive: ArtifactStore | None = None
    recall: MemoryRecallService | None = None
    memory_query: MemoryQuery | None = None
    projector: ProjectionBuilder = field(default_factory=ProjectionBuilder)
    projection_ledger: ProjectionLedger = field(default_factory=ProjectionLedger)
    graph_id: str | None = None
    read_scopes: frozenset[str] | None = None
    working_context_store: PostgresWorkingContextStore | None = None
    working_context_domain: MemoryDomainContext | None = None
    working_context_ttl: timedelta | None = timedelta(days=14)
    working_context_async_operation_port: (
        Callable[[str, Callable[[], object], float], Awaitable[object]] | None
    ) = None
    working_context_refresh_timeout_seconds: float = 2.0
    run_owner_registry: MemoryHookRunOwnerRegistry = field(
        default_factory=MemoryHookRunOwnerRegistry
    )

    @property
    def memory_proposal_sink(self) -> MemoryProposalSink | None:
        return self.sink

    def baseline_projection(
        self, view: MemoryHookRunView, *, token_budget: int
    ) -> dict | None:
        # Recent working context remains operational state.  It is deliberately
        # not projected into provider input until it has its own typed authority.
        return None

    async def project(self, view: MemoryHookRunView, *, token_budget: int) -> dict | None:
        owner = self._owner(view)
        await self._refresh_working_context_once(view, owner)
        if self.recall is None:
            return None
        latest_user_text = _latest_user_quote(view)
        if latest_user_text is None or _should_skip_recall(latest_user_text):
            return None
        cached = owner.recall_projection_cache
        if cached.query_text == latest_user_text:
            return cached.projection
        query = RecallQuery(
            text=latest_user_text,
            scopes=_recall_scopes(self.read_scopes),
            limit=5,
            trigger=RecallTrigger.CHEAP_AUTO,
            session_id=view.session_id,
            run_id=view.run_id,
            turn_id=view.turn_id,
            reply_id=view.reply_id,
        )
        result = await self.recall.recall(query, graph_id=self.graph_id)
        if result.status is not RecallStatus.OK or not result.items:
            cached.generation += 1
            cached.query_text = latest_user_text
            cached.projection = None
            return None
        self.projection_ledger.record(owner.projection_ledger, result.items)
        recalled = self.projector.build(result, token_budget=token_budget)
        cached.generation += 1
        cached.query_text = latest_user_text
        cached.projection = recalled
        return recalled

    async def after_model_reply(
        self, view: MemoryHookRunView, assistant: Msg
    ) -> list[AgentEvent]:
        self._drain_to_pool(view)
        return []

    async def after_tool_results(
        self, view: MemoryHookRunView, results: list[ToolResultBlock]
    ) -> list[AgentEvent]:
        self._drain_to_pool(view)
        return []

    async def on_session_end(self, view: MemoryHookRunView) -> list[AgentEvent]:
        try:
            self._drain_to_pool(view)
            self._finalize_invalid_to_pool(view)
            self._update_working_context(view)
            return []
        finally:
            self._retire_owner(view)

    def _drain_to_pool(self, view: MemoryHookRunView) -> list[PooledMemoryCandidate]:
        proposals = self.sink.drain_valid()
        return self._append_to_pool(view, proposals)

    def _finalize_invalid_to_pool(
        self, view: MemoryHookRunView
    ) -> list[PooledMemoryCandidate]:
        proposals = self.sink.finalize_invalid_attempts()
        return self._append_to_pool(view, proposals)

    def _append_to_pool(
        self,
        view: MemoryHookRunView,
        proposals: list[CandidatePoolProposal],
    ) -> list[PooledMemoryCandidate]:
        pooled: list[PooledMemoryCandidate] = []
        for proposal in proposals:
            if self._is_projection_echo(proposal, view):
                continue
            candidate = proposal.to_pooled(
                source_session_id=view.session_id,
                source_run_id=view.run_id,
                source_turn_id=view.turn_id,
                source_reply_id=view.reply_id,
            )
            if candidate.user_quote is None:
                quote = _latest_user_quote_with_locator(view)
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

    def _is_projection_echo(
        self, proposal: CandidatePoolProposal, view: MemoryHookRunView
    ) -> bool:
        payload = proposal.payload
        if not isinstance(payload, ValidCandidatePayload):
            return False
        owner = self._owner(view)
        return self.projection_ledger.is_echo(
            payload.candidate.statement, owner.projection_ledger
        )

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

    def _update_working_context(self, view: MemoryHookRunView) -> WorkingContextSummary | None:
        return self._update_working_context_for_run(
            runtime_session_id=view.session_id,
            run_id=view.run_id,
        )

    async def _refresh_working_context_once(
        self,
        view: MemoryHookRunView,
        owner: MemoryHookRunOwner,
    ) -> WorkingContextSummary | None:
        model_step_key = view.model_step_key
        if owner.working_context_refresh_attempted_model_step_key == model_step_key:
            return None
        owner.working_context_refresh_attempted_model_step_key = model_step_key

        def operation() -> WorkingContextSummary | None:
            return self._refresh_working_context_from_durable_timeline(
                runtime_session_id=view.session_id,
                current_run_id=view.run_id,
            )

        port = self.working_context_async_operation_port
        if port is None:
            return operation()
        result = await port(
            "working-context-lazy-refresh",
            operation,
            monotonic() + self.working_context_refresh_timeout_seconds,
        )
        return result if isinstance(result, WorkingContextSummary) else None

    def _owner(self, view: MemoryHookRunView) -> MemoryHookRunOwner:
        return self.run_owner_registry.acquire(
            runtime_session_id=view.runtime_session_id,
            run_id=view.run_id,
        )

    def _retire_owner(self, view: MemoryHookRunView) -> None:
        self.run_owner_registry.retire(
            runtime_session_id=view.runtime_session_id,
            run_id=view.run_id,
        )

    def _refresh_working_context_from_durable_timeline(
        self,
        *,
        runtime_session_id: str,
        current_run_id: str,
    ) -> WorkingContextSummary | None:
        if (
            self.event_store is None
            or self.working_context_store is None
            or self.working_context_domain is None
        ):
            return None
        existing = self.working_context_store.get_latest(
            memory_domain_id=self.working_context_domain.memory_domain_id
        )
        terminal_events = self.event_store.read_raw_events_by_type(
            str(EventType.RUN_END),
            limit=8,
        )
        for raw in terminal_events:
            decoded = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            if not isinstance(decoded, RunEndEvent):
                raise ValueError("RunEnd sparse read decoded another event type")
            if decoded.run_id == current_run_id:
                continue
            if existing is not None and decoded.run_id == existing.source_run_id:
                return None
            refreshed = self._update_working_context_for_run(
                runtime_session_id=runtime_session_id,
                run_id=decoded.run_id,
            )
            if refreshed is not None:
                return refreshed
            # The latest terminal run owns freshness. If its async timeline is
            # not ready yet, retry this bounded read at the next model compile.
            return None
        return None

    def _update_working_context_for_run(
        self,
        *,
        runtime_session_id: str,
        run_id: str,
    ) -> WorkingContextSummary | None:
        if (
            self.working_context_store is None
            or self.working_context_domain is None
            or self.timeline_graph is None
            or self.timeline_archive is None
        ):
            return None
        try:
            from pulsara_agent.memory.foundation.run_timeline_query import (
                summarize_persisted_run_timeline,
            )

            summary = summarize_persisted_run_timeline(
                graph=self.timeline_graph,
                archive=self.timeline_archive,
                run_id=run_id,
                runtime_session_id=runtime_session_id,
                graph_id=self.graph_id,
                max_tail_items=256,
            )
        except (KeyError, ValueError):
            # Projection jobs are asynchronous. A missing head is not authority
            # to reconstruct the timeline from the EventLog in this hook.
            return None

        existing = self.working_context_store.get_latest(
            memory_domain_id=self.working_context_domain.memory_domain_id
        )
        update = propose_working_context_update(
            summary,
            existing_summary=existing,
        )
        if not update.should_update:
            return None
        return self.working_context_store.upsert(
            domain=self.working_context_domain,
            source_session_id=runtime_session_id,
            source_run_id=run_id,
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
    _cheap_hints_by_run: dict[str, list[MemoryReflectionHint]] = field(
        default_factory=dict
    )
    _last_token_total_by_run: dict[str, int] = field(default_factory=dict)
    _memory_attempts_by_run: set[str] = field(default_factory=set)

    async def on_session_start(self, view: MemoryHookRunView, user_input: str) -> None:
        self.turns_since_last_reflection += 1
        hints = cheap_memory_hints(user_input)
        if hints:
            self._cheap_hints_by_run.setdefault(view.run_id, []).extend(hints)
        return None

    async def after_model_reply(
        self, view: MemoryHookRunView, assistant: Msg
    ) -> list[AgentEvent]:
        self._update_token_delta(view)
        self._remember_attempts(view, self._drain_to_pool(view))
        return []

    async def after_tool_results(
        self, view: MemoryHookRunView, results: list[ToolResultBlock]
    ) -> list[AgentEvent]:
        drained_candidates = self._drain_to_pool(view)
        self._remember_attempts(view, drained_candidates)
        self.tool_calls_since_last_reflection += len(results)
        self._update_token_delta(view)
        return []

    async def on_session_end(self, view: MemoryHookRunView) -> list[AgentEvent]:
        drained_candidates = self._drain_to_pool(view)
        finalized_invalid = self._finalize_invalid_to_pool(view)
        self._remember_attempts(view, [*drained_candidates, *finalized_invalid])
        self._update_token_delta(view)
        try:
            events = await self._maybe_reflect(
                view,
                safe_point="on_session_end",
            )
            self._update_working_context(view)
            return events
        finally:
            self._cheap_hints_by_run.pop(view.run_id, None)
            self._last_token_total_by_run.pop(view.run_id, None)
            self._memory_attempts_by_run.discard(view.run_id)
            self._retire_owner(view)

    async def _maybe_reflect(
        self,
        view: MemoryHookRunView,
        *,
        safe_point: str,
    ) -> list[AgentEvent]:
        if view.status in {"aborted", "failed"}:
            return []
        trigger_reasons = self._trigger_reasons(view, safe_point=safe_point)
        if not trigger_reasons:
            return []
        cheap_hints = list(self._cheap_hints_by_run.get(view.run_id, []))
        reflection_events = await self.reflection.reflect(
            view=view,
            event_store=self.event_store,
            trigger_reasons=trigger_reasons,
            cheap_hints=cheap_hints,
            safe_point=safe_point,
        )
        self._mark_reflected(view)
        return reflection_events

    def _trigger_reasons(
        self,
        view: MemoryHookRunView,
        *,
        safe_point: str,
    ) -> list[str]:
        reasons: list[str] = []
        has_memory_attempt = view.run_id in self._memory_attempts_by_run
        if (
            safe_point == "on_session_end"
            and self._cheap_hints_by_run.get(view.run_id)
            and not has_memory_attempt
        ):
            reasons.append("cheap_memory_hint")
        if self.last_reflection_run_id == view.run_id:
            return []
        return _unique(reasons)

    def _update_token_delta(self, view: MemoryHookRunView) -> None:
        current = view.token_usage.total_tokens
        previous = self._last_token_total_by_run.get(view.run_id, 0)
        if current > previous:
            self.token_delta_since_last_reflection += current - previous
        self._last_token_total_by_run[view.run_id] = current

    def _mark_reflected(self, view: MemoryHookRunView) -> None:
        self.last_reflection_run_id = view.run_id
        self.turns_since_last_reflection = 0
        self.tool_calls_since_last_reflection = 0
        self.token_delta_since_last_reflection = 0

    def _remember_attempts(
        self, view: MemoryHookRunView, candidates: list[PooledMemoryCandidate]
    ) -> None:
        if any(
            candidate.origin is CandidateOrigin.MAIN_AGENT_TOOL
            for candidate in candidates
        ):
            self._memory_attempts_by_run.add(view.run_id)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _latest_user_quote(view: MemoryHookRunView, max_chars: int = 2_000) -> str | None:
    quote = _latest_user_quote_with_locator(view, max_chars=max_chars)
    return quote[0] if quote is not None else None


def _latest_user_quote_with_locator(
    view: MemoryHookRunView,
    max_chars: int = 2_000,
) -> tuple[str, str, int, int] | None:
    for message in reversed(view.messages):
        if message.role != "user":
            continue
        text = "\n".join(
            block.text for block in message.content if isinstance(block, TextBlock)
        ).strip()
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
        "projection_kind": projection_kinds[0]
        if len(projection_kinds) == 1
        else "mixed",
        "projection_kinds": projection_kinds,
    }


def _projection_kinds(first: dict, second: dict) -> list[str]:
    kinds: list[str] = []
    for projection, fallback in (
        (first, "working_context"),
        (second, "recalled_memory"),
    ):
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
            memory_ids = tuple(
                sorted(str(memory_id) for memory_id in group.get("memory_ids") or [])
            )
            key = (kind, memory_ids)
            if not kind or not memory_ids or key in seen:
                continue
            seen.add(key)
            groups.append({"kind": kind, "memory_ids": list(memory_ids)})
    return groups
