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
    ExceedMaxItersEvent,
    EventContext,
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    PlanQuestionAnsweredEvent,
    PlanQuestionAskedEvent,
    ModelCallEndEvent,
    RunEndEvent,
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
    strip_compaction_analysis,
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
    estimated_compaction_input_tokens_before: int
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

    def should_auto_compact(
        self,
        *,
        current_user_input: str = "",
        model_visible_messages: list[Msg] | tuple[Msg, ...] | None = None,
    ) -> bool:
        if not self.policy.enabled or not self.policy.auto_enabled:
            return False
        if self._consecutive_failures >= self.policy.max_consecutive_failures:
            return False
        events = self.event_log.iter()
        plan = self._build_plan(
            events,
            current_user_input=current_user_input,
            model_visible_messages=model_visible_messages,
        )
        if plan is None:
            return False
        return plan.estimated_tokens_before >= self.policy.auto_threshold_tokens

    async def compact_if_needed(
        self,
        *,
        current_user_input: str = "",
        model_visible_messages: list[Msg] | tuple[Msg, ...] | None = None,
        reason: str = "context_threshold",
    ) -> bool:
        if not self.should_auto_compact(
            current_user_input=current_user_input,
            model_visible_messages=model_visible_messages,
        ):
            return False
        return await self.compact(
            trigger="auto",
            reason=reason,
            current_user_input=current_user_input,
            model_visible_messages=model_visible_messages,
        ) is not None

    async def compact(
        self,
        *,
        trigger: ContextCompactionTrigger,
        reason: str,
        current_user_input: str = "",
        model_visible_messages: list[Msg] | tuple[Msg, ...] | None = None,
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
        plan = self._build_plan(
            events,
            current_user_input=current_user_input,
            model_visible_messages=model_visible_messages,
            force=force,
        )
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
            metadata={
                "estimate_source": "model_visible_context",
                "estimated_compaction_input_tokens_before": plan.estimated_compaction_input_tokens_before,
            },
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
                    "estimated_model_visible_tokens_before": plan.estimated_tokens_before,
                    "estimated_compaction_input_tokens_before": plan.estimated_compaction_input_tokens_before,
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
                metadata={
                    "estimate_source": "model_visible_context",
                    "estimated_compaction_input_tokens_before": plan.estimated_compaction_input_tokens_before,
                },
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
                metadata={
                    "estimate_source": "model_visible_context",
                    "estimated_compaction_input_tokens_before": plan.estimated_compaction_input_tokens_before,
                },
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
        model_visible_messages: list[Msg] | tuple[Msg, ...] | None = None,
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
        estimated_compaction_input_before = estimate_compaction_window_tokens(
            candidate_events,
            policy=self.policy,
            previous_summary_text=latest_boundary.summary_text if latest_boundary is not None else None,
        )
        estimated_before = estimate_model_visible_tokens(
            model_visible_messages if model_visible_messages is not None else model_visible_messages_from_events(candidate_events),
            current_user_input=current_user_input,
            policy=self.policy,
            previous_summary_text=(
                latest_boundary.summary_text if latest_boundary is not None and model_visible_messages is None else None
            ),
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
            policy=self.policy,
            previous_summary_text=latest_boundary.summary_text if latest_boundary is not None else None,
        )
        next_window_number = _next_window_number(events)
        return CompactionPlan(
            through_sequence=through_sequence,
            keep_after_sequence=through_sequence,
            estimated_tokens_before=estimated_before,
            estimated_compaction_input_tokens_before=estimated_compaction_input_before,
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
        f"estimated_model_visible_tokens_before: {plan.estimated_tokens_before}",
        f"estimated_compaction_input_tokens_before: {plan.estimated_compaction_input_tokens_before}",
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


def estimate_context_tokens(text_or_messages: str | list[Msg], *, chars_per_token: float = 4.0) -> int:
    if isinstance(text_or_messages, str):
        text = text_or_messages
    else:
        text = "\n".join(_message_text_for_estimate(message) for message in text_or_messages)
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
    """Conservatively estimate compact-model input size.

    The compact model receives a coalesced event-derived transcript, not raw
    streaming deltas. Current user input is never included in that summary
    input; it belongs only to the model-visible auto-trigger estimate.
    """

    compact_input_tokens = estimate_context_tokens(
        build_compaction_observation_text(events),
        chars_per_token=policy.event_chars_per_token,
    )
    previous_summary_tokens = estimate_context_tokens(
        render_summary_for_estimate(previous_summary_text),
        chars_per_token=policy.chars_per_token,
    ) if previous_summary_text else 0
    raw = previous_summary_tokens + compact_input_tokens
    if raw <= 0:
        return 0
    return max(1, int(raw * max(policy.estimate_safety_margin, 1.0)))


def estimate_model_visible_tokens(
    messages: list[Msg] | tuple[Msg, ...],
    *,
    current_user_input: str = "",
    policy: ContextCompactionPolicy = ContextCompactionPolicy(),
    previous_summary_text: str | None = None,
) -> int:
    message_tokens = estimate_context_tokens(list(messages), chars_per_token=policy.chars_per_token)
    current_user_tokens = estimate_context_tokens(current_user_input, chars_per_token=policy.chars_per_token)
    previous_summary_tokens = estimate_context_tokens(
        render_summary_for_estimate(previous_summary_text),
        chars_per_token=policy.chars_per_token,
    ) if previous_summary_text else 0
    raw = previous_summary_tokens + message_tokens + current_user_tokens
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
            artifact_ids.extend(artifact.artifact_id for artifact in event.artifacts)
    return list(dict.fromkeys(artifact_ids))


def model_visible_messages_from_events(events: list[AgentEvent] | tuple[AgentEvent, ...]) -> list[Msg]:
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


def build_compaction_observation_text(events: list[AgentEvent] | tuple[AgentEvent, ...]) -> str:
    lines: list[str] = []
    assembler = BlockAssembler()
    for event in events:
        if isinstance(event, RunStartEvent):
            user_input = event.metadata.get("user_input")
            rendered = user_input if isinstance(user_input, str) else f"[user_input_chars={event.user_input_chars}]"
            lines.append(f"[user run_id={event.run_id}] {_clip_text(rendered, _COMPACTION_TEXT_CLIP_CHARS)}")
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
            lines.append(f"[run_error run_id={event.run_id} code={event.code}] {_clip_text(event.message, 1000)}")
            continue
        if isinstance(event, ExceedMaxItersEvent):
            lines.append(f"[exceed_max_iters run_id={event.run_id} name={event.name} max_iters={event.max_iters}]")
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
                f"{artifact.artifact_id}({artifact.media_type}, {artifact.size_bytes} bytes)"
                for artifact in block.artifacts
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
            parts.append(f"[tool_call id={block.id} name={block.name} state={block.state}] {block.input}")
        elif isinstance(block, ToolResultBlock):
            artifacts = " ".join(artifact.artifact_id for artifact in block.artifacts)
            output = "\n".join(_block_text(item) for item in block.output)
            parts.append(f"[tool_result id={block.id} name={block.name} state={block.state} artifacts={artifacts}] {output}")
        elif isinstance(block, HintBlock):
            parts.append(f"[hint source={block.source or '(unknown)'}] {_block_text(block)}")
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
    return EventContext(run_id=latest.run_id, turn_id=latest.turn_id, reply_id=latest.reply_id)


def _summary_artifact_id(compaction_id: str) -> str:
    return compaction_id.replace(":", "_") + ":summary"
