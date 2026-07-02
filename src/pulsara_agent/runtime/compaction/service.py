"""LLM-backed context compaction service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib import resources
from typing import Literal
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionStartedEvent,
    EventContext,
    ModelCallEndEvent,
    RunErrorEvent,
    RunStartEvent,
    TextBlockDeltaEvent,
    ToolResultEndEvent,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.message import Msg
from pulsara_agent.runtime.compaction.planner import (
    SUMMARY_ARTIFACT_KIND,
    latest_completed_boundary,
    message_text,
    strip_compaction_analysis,
)

ContextCompactionTrigger = Literal["manual", "auto"]

_PRODUCTION_PROMPT_PACKAGE = "pulsara_agent.runtime.compaction.prompts"
_PRODUCTION_PROMPT_FILE = "context_compaction_prompt.md"


@dataclass(frozen=True, slots=True)
class ContextCompactionPolicy:
    enabled: bool = True
    auto_enabled: bool = True
    manual_enabled: bool = True
    context_window_tokens: int = 256_000
    auto_threshold_tokens: int = 200_000
    min_events_after_last_compact: int = 20
    keep_recent_runs: int = 3
    max_summary_chars: int = 12_000
    max_consecutive_failures: int = 3
    # Ordinary natural-language text is still estimated at chars/4 by default.
    # Event-log/request-shaped text is denser (lots of quotes, braces, ids and
    # punctuation), so it needs a more conservative ratio.
    chars_per_token: float = 4.0
    event_chars_per_token: float = 2.0
    estimate_safety_margin: float = 1.25
    summary_max_output_tokens: int = 8_192


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    through_sequence: int
    keep_after_sequence: int
    estimated_tokens_before: int
    estimated_tokens_after_replay_tail: int
    included_run_ids: tuple[str, ...]
    included_artifact_ids: tuple[str, ...]
    compacted_events: tuple[AgentEvent, ...]
    tail_events: tuple[AgentEvent, ...]
    window_number: int
    window_id: str
    previous_summary_artifact_id: str | None = None
    previous_summary_text: str | None = None


@dataclass(slots=True)
class ContextCompactionService:
    event_log: EventLog
    archive: ArtifactStore
    llm_runtime: LLMRuntime
    runtime_session_id: str
    policy: ContextCompactionPolicy = ContextCompactionPolicy()
    model_role: ModelRole = ModelRole.FLASH
    _consecutive_failures: int = 0

    def should_auto_compact(self, *, current_user_input: str = "") -> bool:
        if not self.policy.enabled or not self.policy.auto_enabled:
            return False
        if self._consecutive_failures >= self.policy.max_consecutive_failures:
            return False
        events = self.event_log.iter()
        plan = self._build_plan(events, current_user_input=current_user_input)
        if plan is None:
            return False
        return plan.estimated_tokens_before >= self.policy.auto_threshold_tokens

    async def compact_if_needed(
        self,
        *,
        current_user_input: str = "",
        reason: str = "context_threshold",
    ) -> bool:
        if not self.should_auto_compact(current_user_input=current_user_input):
            return False
        return await self.compact(trigger="auto", reason=reason, current_user_input=current_user_input) is not None

    async def compact(
        self,
        *,
        trigger: ContextCompactionTrigger,
        reason: str,
        current_user_input: str = "",
        force: bool = False,
    ) -> ContextCompactionCompletedEvent | None:
        if not self.policy.enabled:
            return None
        if trigger == "manual" and not self.policy.manual_enabled:
            return None
        if trigger == "auto" and not self.policy.auto_enabled:
            return None
        if trigger == "auto" and self._consecutive_failures >= self.policy.max_consecutive_failures:
            return None

        events = self.event_log.iter()
        plan = self._build_plan(events, current_user_input=current_user_input, force=force)
        if plan is None:
            return None
        if not force and trigger == "auto" and plan.estimated_tokens_before < self.policy.auto_threshold_tokens:
            return None

        compaction_id = f"context_compaction:{uuid4().hex}"
        context = _event_context_for_compaction(events)
        started = ContextCompactionStartedEvent(
            **context.event_fields(),
            compaction_id=compaction_id,
            trigger=trigger,
            reason=reason,
            window_number=plan.window_number,
            window_id=plan.window_id,
            estimated_tokens_before=plan.estimated_tokens_before,
            threshold_tokens=self.policy.auto_threshold_tokens,
            context_window_tokens=self.policy.context_window_tokens,
            through_sequence=plan.through_sequence,
            keep_after_sequence=plan.keep_after_sequence,
            force=force,
        )
        await asyncio.to_thread(self.event_log.append, started)

        try:
            raw_summary = await self._summarize(plan)
            summary = strip_compaction_analysis(raw_summary)
            if not summary:
                raise RuntimeError("compact model returned an empty summary")
            if len(summary) > self.policy.max_summary_chars:
                marker = f"\n[COMPACTION SUMMARY TRUNCATED: kept {self.policy.max_summary_chars} of {len(summary)} chars]"
                summary = summary[: max(0, self.policy.max_summary_chars - len(marker))] + marker
            artifact_id = _summary_artifact_id(compaction_id)
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
                },
            )
            estimated_after = estimate_post_compaction_tokens(
                summary,
                plan.tail_events,
                policy=self.policy,
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
                estimated_tokens_before=plan.estimated_tokens_before,
                estimated_tokens_after=estimated_after,
                threshold_tokens=self.policy.auto_threshold_tokens,
                context_window_tokens=self.policy.context_window_tokens,
                through_sequence=plan.through_sequence,
                keep_after_sequence=plan.keep_after_sequence,
                included_run_ids=list(plan.included_run_ids),
                included_artifact_ids=list(plan.included_artifact_ids),
            )
            stored = await asyncio.to_thread(self.event_log.append, completed)
            self._consecutive_failures = 0
            return stored
        except Exception as exc:
            self._consecutive_failures += 1
            failed = ContextCompactionFailedEvent(
                **context.event_fields(),
                compaction_id=compaction_id,
                trigger=trigger,
                reason=reason,
                window_number=plan.window_number,
                window_id=plan.window_id,
                estimated_tokens_before=plan.estimated_tokens_before,
                threshold_tokens=self.policy.auto_threshold_tokens,
                context_window_tokens=self.policy.context_window_tokens,
                through_sequence=plan.through_sequence,
                keep_after_sequence=plan.keep_after_sequence,
                error_type=type(exc).__name__,
                message=str(exc),
            )
            await asyncio.to_thread(self.event_log.append, failed)
            if trigger == "manual":
                raise
            return None

    async def _summarize(self, plan: CompactionPlan) -> str:
        prompt = production_compaction_prompt()
        input_text = build_compaction_input(plan)
        context = LLMContext(
            messages=(
                LLMMessage.system(prompt),
                LLMMessage.user(input_text),
            ),
            tools=(),
        )
        event_context = EventContext(
            run_id=f"compaction_model:{uuid4().hex}",
            turn_id=f"compaction_model_turn:{uuid4().hex}",
            reply_id=f"compaction_model_reply:{uuid4().hex}",
        )
        parts: list[str] = []
        async for event in self.llm_runtime.stream(
            role=self.model_role,
            context=context,
            event_context=event_context,
            options=LLMOptions(max_output_tokens=self.policy.summary_max_output_tokens),
        ):
            if isinstance(event, TextBlockDeltaEvent):
                parts.append(event.delta)
            elif isinstance(event, RunErrorEvent):
                raise RuntimeError(f"compact model error ({event.code}): {event.message}")
            elif isinstance(event, ModelCallEndEvent):
                continue
        return "".join(parts)

    def _build_plan(
        self,
        events: list[AgentEvent],
        *,
        current_user_input: str = "",
        force: bool = False,
    ) -> CompactionPlan | None:
        if not events:
            return None
        latest_boundary = latest_completed_boundary(events, archive=self.archive, session_id=self.runtime_session_id)
        last_keep_after = latest_boundary.keep_after_sequence if latest_boundary is not None else 0
        candidate_events = [
            event for event in events if event.sequence is not None and event.sequence > last_keep_after
        ]
        if not candidate_events:
            return None
        estimated_before = estimate_compaction_window_tokens(
            candidate_events,
            current_user_input=current_user_input,
            policy=self.policy,
            previous_summary_text=latest_boundary.summary_text if latest_boundary is not None else None,
        )
        if not force and len(candidate_events) < self.policy.min_events_after_last_compact:
            return None
        keep_after_sequence = _keep_after_sequence_for_recent_runs(candidate_events, self.policy.keep_recent_runs)
        if keep_after_sequence <= last_keep_after:
            if not force and estimated_before < self.policy.auto_threshold_tokens:
                return None
            keep_after_sequence = max(event.sequence or 0 for event in candidate_events)
        compacted = tuple(event for event in candidate_events if (event.sequence or 0) <= keep_after_sequence)
        tail = tuple(event for event in candidate_events if (event.sequence or 0) > keep_after_sequence)
        if not compacted:
            return None
        through_sequence = max(event.sequence or 0 for event in compacted)
        estimated_tail = estimate_compaction_window_tokens(
            tail,
            current_user_input=current_user_input,
            policy=self.policy,
            previous_summary_text=latest_boundary.summary_text if latest_boundary is not None else None,
        )
        next_window_number = _next_window_number(events)
        return CompactionPlan(
            through_sequence=through_sequence,
            keep_after_sequence=through_sequence,
            estimated_tokens_before=estimated_before,
            estimated_tokens_after_replay_tail=estimated_tail,
            included_run_ids=tuple(dict.fromkeys(event.run_id for event in compacted)),
            included_artifact_ids=tuple(_artifact_ids(compacted)),
            compacted_events=compacted,
            tail_events=tail,
            window_number=next_window_number,
            window_id=f"context_window:{next_window_number}:{uuid4().hex}",
            previous_summary_artifact_id=(
                latest_boundary.event.summary_artifact_id if latest_boundary is not None else None
            ),
            previous_summary_text=latest_boundary.summary_text if latest_boundary is not None else None,
        )


def production_compaction_prompt() -> str:
    return resources.files(_PRODUCTION_PROMPT_PACKAGE).joinpath(_PRODUCTION_PROMPT_FILE).read_text(encoding="utf-8")


def build_compaction_input(plan: CompactionPlan) -> str:
    lines = [
        "# Pulsara compaction input",
        "",
        "The following canonical event-derived transcript prefix will be summarized.",
        f"through_sequence: {plan.through_sequence}",
        f"keep_after_sequence: {plan.keep_after_sequence}",
        f"included_run_ids: {', '.join(plan.included_run_ids)}",
        f"included_artifact_ids: {', '.join(plan.included_artifact_ids) or '(none)'}",
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
    for event in plan.compacted_events:
        lines.append(_event_line(event))
    return "\n".join(lines)


def estimate_context_tokens(text_or_messages: str | list[Msg], *, chars_per_token: float = 4.0) -> int:
    if isinstance(text_or_messages, str):
        text = text_or_messages
    else:
        text = "\n".join(message_text(message) for message in text_or_messages)
    if not text:
        return 0
    return max(1, int(len(text) / max(chars_per_token, 0.1)))


def estimate_compaction_window_tokens(
    events: list[AgentEvent] | tuple[AgentEvent, ...],
    *,
    current_user_input: str = "",
    policy: ContextCompactionPolicy = ContextCompactionPolicy(),
    previous_summary_text: str | None = None,
) -> int:
    """Conservatively estimate model-visible compaction context size.

    Event-log lines are JSON-ish and token dense, so treating them like plain
    prose (chars/4) underestimates right where compaction needs a safety fuse.
    Current user input is ordinary text for estimation purposes and is never
    included in the compact summary input itself.
    """

    event_tokens = estimate_context_tokens(
        _events_text_for_estimate(events),
        chars_per_token=policy.event_chars_per_token,
    )
    user_tokens = estimate_context_tokens(
        current_user_input,
        chars_per_token=policy.chars_per_token,
    )
    previous_summary_tokens = estimate_context_tokens(
        render_summary_for_estimate(previous_summary_text),
        chars_per_token=policy.chars_per_token,
    ) if previous_summary_text else 0
    raw = previous_summary_tokens + event_tokens + user_tokens
    if raw <= 0:
        return 0
    return max(1, int(raw * max(policy.estimate_safety_margin, 1.0)))


def estimate_post_compaction_tokens(
    summary: str,
    tail_events: list[AgentEvent] | tuple[AgentEvent, ...],
    *,
    policy: ContextCompactionPolicy = ContextCompactionPolicy(),
) -> int:
    summary_tokens = estimate_context_tokens(
        render_summary_for_estimate(summary),
        chars_per_token=policy.chars_per_token,
    )
    tail_tokens = estimate_context_tokens(
        _events_text_for_estimate(tail_events),
        chars_per_token=policy.event_chars_per_token,
    )
    raw = summary_tokens + tail_tokens
    if raw <= 0:
        return 0
    return max(1, int(raw * max(policy.estimate_safety_margin, 1.0)))


def render_summary_for_estimate(summary: str) -> str:
    # Keep this estimate conservative without requiring a real event object.
    return summary + "\n<context-compaction-summary do_not_write_back=true>"


def _keep_after_sequence_for_recent_runs(events: list[AgentEvent], keep_recent_runs: int) -> int:
    run_starts = [event for event in events if isinstance(event, RunStartEvent) and event.sequence is not None]
    if len(run_starts) <= keep_recent_runs:
        return 0
    first_kept_run = run_starts[-keep_recent_runs]
    return (first_kept_run.sequence or 0) - 1


def _next_window_number(events: list[AgentEvent]) -> int:
    completed = [event for event in events if hasattr(event, "window_number")]
    numbers = [int(getattr(event, "window_number")) for event in completed if getattr(event, "window_number", None)]
    return max(numbers, default=0) + 1


def _artifact_ids(events: tuple[AgentEvent, ...]) -> list[str]:
    artifact_ids: list[str] = []
    for event in events:
        if isinstance(event, ToolResultEndEvent):
            artifact_ids.extend(artifact.id for artifact in event.artifacts)
    return list(dict.fromkeys(artifact_ids))


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
    return EventContext(run_id=latest.run_id, turn_id=latest.turn_id, reply_id=latest.reply_id)


def _summary_artifact_id(compaction_id: str) -> str:
    return compaction_id.replace(":", "_") + ":summary"
