"""Tool-result aware transcript rendering for the context compiler.

This module owns the model-visible rendering policy for historical tool
results.  Tool execution and artifact archiving decide what is true; this
renderer decides what shape of that existing truth enters a compiled model
context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pulsara_agent.llm.input import LLMMessage, LLMToolCall
from pulsara_agent.message import (
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultArtifactRef,
    ToolResultBlock,
    URLSource,
)
from pulsara_agent.runtime.context_engine.types import (
    ContextBudgetExceeded,
    ContextDiagnostic,
)
from pulsara_agent.runtime.state import LoopBudget


@dataclass(frozen=True, slots=True)
class SegmentedLLMMessages:
    full_messages: tuple[LLMMessage, ...]
    prior_history_messages: tuple[LLMMessage, ...] | None
    current_user_messages: tuple[LLMMessage, ...] | None
    current_run_tail_messages: tuple[LLMMessage, ...] | None
    tool_result_render_decisions: tuple[dict[str, object], ...]
    tool_result_budget_report: dict[str, object]


def render_segmented_llm_messages(
    messages: list[Msg],
    budget: LoopBudget,
    anchor: str | None,
) -> SegmentedLLMMessages:
    """Render raw Msg history into budgeted model-visible LLM messages.

    ``anchor`` is the current user message id.  When present, transcript is
    split into prior history, current user, and current-run tail so fresh tool
    results cannot be starved by large old outputs.
    """

    llm_messages: list[LLMMessage] = []
    prior_messages: list[LLMMessage] = []
    current_user_messages: list[LLMMessage] = []
    tail_messages: list[LLMMessage] = []
    anchor_index: int | None = None
    if anchor is not None:
        matches = [
            index
            for index, message in enumerate(messages)
            if message.id == anchor and message.role == "user"
        ]
        if len(matches) == 1:
            anchor_index = matches[0]
    source_assistant_by_index = _source_assistant_message_ids(messages)
    latest_ids = _latest_tail_tool_result_ids(
        messages,
        anchor_index=anchor_index,
        source_assistant_by_index=source_assistant_by_index,
    )
    latest_reserved_ids = _latest_reserved_tool_result_ids(
        messages,
        latest_ids=latest_ids,
        latest_reserved_chars=budget.latest_tool_result_reserved_chars,
    )
    tool_budget = _ToolResultRenderAllocator.from_loop_budget(
        budget,
        latest_tool_result_ids=latest_ids,
        latest_reserved_tool_result_ids=latest_reserved_ids,
    )
    for index, message in enumerate(messages):
        segment = _message_segment(index, anchor_index)
        converted = _message_to_llm_messages(
            message,
            tool_budget,
            segment=segment,
            source_message_id=message.id,
            source_assistant_message_id=source_assistant_by_index.get(index),
        )
        llm_messages.extend(converted)
        if anchor_index is None:
            continue
        if index < anchor_index:
            prior_messages.extend(converted)
        elif index == anchor_index:
            current_user_messages.extend(converted)
        else:
            tail_messages.extend(converted)
    return SegmentedLLMMessages(
        full_messages=tuple(llm_messages),
        prior_history_messages=tuple(prior_messages) if anchor_index is not None else None,
        current_user_messages=tuple(current_user_messages) if anchor_index is not None else None,
        current_run_tail_messages=tuple(tail_messages) if anchor_index is not None else None,
        tool_result_render_decisions=tuple(tool_budget.decisions),
        tool_result_budget_report=tool_budget.report(),
    )


def raise_if_tool_result_budget_unsatisfied(
    *,
    context_id: str,
    model_call_index: int,
    segmented_messages: SegmentedLLMMessages,
) -> None:
    report = segmented_messages.tool_result_budget_report
    diagnostics = [
        diagnostic
        for diagnostic in report.get("diagnostics", [])
        if isinstance(diagnostic, dict)
    ]
    fail_codes = {
        "tool_result_total_budget_unsatisfied",
        "tool_result_body_budget_unsatisfied",
        "essential_envelope_budget_unsatisfied",
        "max_tool_results_per_context_exceeded",
    }
    failures = [
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.get("code") in fail_codes
    ]
    if not failures:
        return
    context_diagnostics = tuple(
        ContextDiagnostic(
            severity="error",
            code=str(diagnostic.get("code") or "tool_result_budget_unsatisfied"),
            message="Tool result render budget hard cap was exceeded before model call.",
            section_id="transcript:tool_results",
            metadata=dict(diagnostic),
        )
        for diagnostic in failures
    )
    codes = ", ".join(str(diagnostic.get("code")) for diagnostic in failures)
    raise ContextBudgetExceeded(
        f"Tool result render budget hard cap exceeded: {codes}",
        context_id=context_id,
        model_call_index=model_call_index,
        diagnostics=context_diagnostics,
        tool_result_render_decisions=segmented_messages.tool_result_render_decisions,
        tool_result_budget_report=report,
    )


def _message_segment(index: int, anchor_index: int | None) -> str:
    if anchor_index is None:
        return "legacy_history"
    if index < anchor_index:
        return "prior_history"
    if index == anchor_index:
        return "current_user"
    return "current_run_tail"


def _message_to_llm_messages(
    message: Msg,
    tool_budget: "_ToolResultRenderAllocator",
    *,
    segment: str,
    source_message_id: str,
    source_assistant_message_id: str | None,
) -> list[LLMMessage]:
    if message.role == "user":
        parts = _textual_parts(
            message,
            tool_budget,
            segment=segment,
            source_message_id=source_message_id,
            source_assistant_message_id=source_assistant_message_id,
        )
        return [LLMMessage.user("\n".join(parts))] if parts else []
    if message.role == "assistant":
        return _assistant_messages(
            message,
            tool_budget,
            segment=segment,
            source_message_id=source_message_id,
            source_assistant_message_id=source_assistant_message_id,
        )
    if message.role == "system":
        parts = _textual_parts(
            message,
            tool_budget,
            segment=segment,
            source_message_id=source_message_id,
            source_assistant_message_id=source_assistant_message_id,
        )
        return [LLMMessage.system("\n".join(parts))] if parts else []
    if message.role == "tool_result":
        return _tool_result_messages(
            message,
            tool_budget,
            segment=segment,
            source_message_id=source_message_id,
            source_assistant_message_id=source_assistant_message_id,
        )
    return []


def _assistant_messages(
    message: Msg,
    tool_budget: "_ToolResultRenderAllocator",
    *,
    segment: str,
    source_message_id: str,
    source_assistant_message_id: str | None,
) -> list[LLMMessage]:
    messages: list[LLMMessage] = []
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[LLMToolCall] = []

    def flush_assistant_turn() -> None:
        nonlocal text_parts, thinking_parts, tool_calls
        if not text_parts and not thinking_parts and not tool_calls:
            return
        messages.append(
            LLMMessage.assistant_turn(
                text="\n".join(text_parts),
                thinking=tuple(thinking_parts),
                tool_calls=tuple(tool_calls),
            )
        )
        text_parts = []
        thinking_parts = []
        tool_calls = []

    for block in message.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ThinkingBlock):
            thinking_parts.append(block.thinking)
        elif isinstance(block, DataBlock):
            text_parts.append(_data_placeholder(block))
        elif isinstance(block, ToolCallBlock):
            tool_calls.append(
                LLMToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input or "{}",
                )
            )
        elif isinstance(block, ToolResultBlock):
            flush_assistant_turn()
            messages.extend(
                _tool_result_messages(
                    Msg(role="tool_result", name=block.name, content=[block], id=source_message_id),
                    tool_budget,
                    segment=segment,
                    source_message_id=source_message_id,
                    source_assistant_message_id=source_assistant_message_id or source_message_id,
                )
            )
    flush_assistant_turn()
    return messages


def _tool_result_messages(
    message: Msg,
    tool_budget: "_ToolResultRenderAllocator",
    *,
    segment: str,
    source_message_id: str,
    source_assistant_message_id: str | None,
) -> list[LLMMessage]:
    messages: list[LLMMessage] = []
    for block in message.content:
        if not isinstance(block, ToolResultBlock):
            continue
        body = _render_tool_result_body(
            block,
            tool_budget,
            segment=segment,
            source_message_id=source_message_id,
            source_assistant_message_id=source_assistant_message_id,
        )
        messages.append(
            LLMMessage.tool_result(
                f"[tool_result:{block.name}:{block.state.value}]\n{body}",
                tool_call_id=block.id,
            )
        )
    return messages


def _textual_parts(
    message: Msg,
    tool_budget: "_ToolResultRenderAllocator",
    *,
    segment: str,
    source_message_id: str,
    source_assistant_message_id: str | None,
) -> list[str]:
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            body = _render_tool_result_body(
                block,
                tool_budget,
                segment=segment,
                source_message_id=source_message_id,
                source_assistant_message_id=source_assistant_message_id,
            )
            parts.append(f"[tool_result:{block.name}:{block.state.value}]\n{body}")
        elif isinstance(block, DataBlock):
            parts.append(_data_placeholder(block))
        else:
            continue
    return [part for part in parts if part]


def _tool_result_text(block: ToolResultBlock) -> str:
    parts: list[str] = []
    for output in block.output:
        if isinstance(output, TextBlock):
            parts.append(output.text)
        elif isinstance(output, DataBlock):
            parts.append(_data_placeholder(output))
    return "\n".join(parts)


class _ToolResultRenderAllocator:
    def __init__(
        self,
        *,
        caps: dict[str, int],
        segment_remaining: dict[str, int],
        latest_tool_result_ids: set[str],
        latest_reserved_tool_result_ids: set[str],
        latest_reserved_chars: int,
        per_tool_cap_chars: int,
        per_message_cap_chars: int,
        per_envelope_cap_chars: int,
    ) -> None:
        self.caps = caps
        self.segment_remaining = segment_remaining
        self.latest_tool_result_ids = latest_tool_result_ids
        self.latest_reserved_tool_result_ids = latest_reserved_tool_result_ids
        self.latest_reserved_chars = latest_reserved_chars
        self.latest_reserved_remaining = latest_reserved_chars * len(latest_reserved_tool_result_ids)
        self.per_tool_cap_chars = per_tool_cap_chars
        self.per_message_cap_chars = per_message_cap_chars
        self.per_envelope_cap_chars = per_envelope_cap_chars
        self.body_remaining = caps["tool_result_body_context_chars"]
        self.total_remaining = caps["tool_result_total_context_chars"]
        self.batch_remaining: dict[str, int] = {}
        self.envelope_remaining = caps["tool_result_envelope_context_chars"]
        self.decisions: list[dict[str, object]] = []
        self._render_order = 0

    @classmethod
    def from_loop_budget(
        cls,
        budget: LoopBudget,
        *,
        latest_tool_result_ids: set[str],
        latest_reserved_tool_result_ids: set[str],
    ) -> "_ToolResultRenderAllocator":
        total = max(0, budget.tool_result_context_chars)
        body_total = budget.tool_result_body_context_chars
        configured_envelope_total = max(0, budget.tool_result_envelope_context_chars)
        if body_total is None:
            envelope_total = min(configured_envelope_total, max(0, total // 3))
            body_total = max(0, total - envelope_total)
        else:
            body_total = max(0, min(body_total, total))
            envelope_total = min(configured_envelope_total, max(0, total - body_total))
        prior = budget.prior_tool_result_context_chars
        current = budget.current_tail_tool_result_context_chars
        legacy = budget.legacy_tool_result_context_chars
        if prior is None or current is None:
            derived_prior = min(12_000, body_total // 3)
            derived_current = max(0, body_total - derived_prior)
            prior = derived_prior if prior is None else prior
            current = derived_current if current is None else current
        legacy = body_total if legacy is None else legacy
        latest_reserved = max(0, budget.latest_tool_result_reserved_chars)
        per_envelope = max(256, budget.tool_result_per_envelope_cap_chars)
        reserved_total = latest_reserved * len(latest_reserved_tool_result_ids)
        normal_current = max(0, current - reserved_total)
        per_tool = budget.tool_result_per_tool_cap_chars
        if per_tool is None:
            per_tool = max(latest_reserved, min(12_000, max(current, legacy, prior)))
        per_message = budget.tool_result_per_message_cap_chars
        if per_message is None:
            per_message = max(latest_reserved, min(20_000, max(current, legacy, prior)))
        caps = {
            "tool_result_total_context_chars": total,
            "tool_result_body_context_chars": max(0, body_total),
            "tool_result_envelope_context_chars": envelope_total,
            "prior_tool_result_context_chars": prior,
            "current_tail_tool_result_context_chars": current,
            "legacy_tool_result_context_chars": legacy,
            "tool_result_per_tool_cap_chars": per_tool,
            "tool_result_per_message_cap_chars": per_message,
            "tool_result_per_envelope_cap_chars": per_envelope,
            "latest_tool_result_reserved_chars": latest_reserved,
            "latest_reserved_total_chars": reserved_total,
            "current_tail_normal_context_chars": normal_current,
            "max_tool_results_per_context": max(0, budget.max_tool_results_per_context),
            "minimum_essential_envelope_chars": max(1, budget.minimum_essential_envelope_chars),
        }
        return cls(
            caps=caps,
            segment_remaining={
                "prior_history": max(0, prior),
                "current_run_tail": normal_current,
                "legacy_history": max(0, legacy),
                "current_user": max(0, current),
            },
            latest_tool_result_ids=set(latest_tool_result_ids),
            latest_reserved_tool_result_ids=set(latest_reserved_tool_result_ids),
            latest_reserved_chars=latest_reserved,
            per_tool_cap_chars=max(0, per_tool),
            per_message_cap_chars=max(0, per_message),
            per_envelope_cap_chars=per_envelope,
        )

    def render(
        self,
        block: ToolResultBlock,
        *,
        segment: str,
        source_message_id: str,
        source_assistant_message_id: str | None,
    ) -> str:
        self._render_order += 1
        text = _tool_result_text(block)
        original_chars = _tool_result_original_chars(block, text)
        body_candidate_chars, body_candidate_source = _tool_result_body_candidate(block, text)
        latest_candidate = block.id in self.latest_tool_result_ids
        latest_short = (
            latest_candidate
            and body_candidate_chars is not None
            and body_candidate_chars <= self.latest_reserved_chars
            and body_candidate_source != "non_short_truncated_preview"
        )
        budget_key = segment if segment in self.segment_remaining else "legacy_history"
        budget_before = self.segment_remaining.get(budget_key, 0)
        tool_batch_id = source_assistant_message_id or block.id
        batch_before = self.batch_remaining.setdefault(tool_batch_id, self.per_message_cap_chars)
        batch_allows_full_reserved = (
            body_candidate_chars is None
            or batch_before >= min(self.latest_reserved_chars, body_candidate_chars)
        )
        use_reserved = (
            latest_short
            and block.id in self.latest_reserved_tool_result_ids
            and self.latest_reserved_remaining > 0
            and batch_before > 0
            and batch_allows_full_reserved
        )
        body_allowed = self.latest_reserved_chars if use_reserved else budget_before
        body_allowed = min(body_allowed, batch_before)
        body_allowed = min(body_allowed, self.per_tool_cap_chars) if self.per_tool_cap_chars > 0 else body_allowed
        body_allowed = min(body_allowed, self.body_remaining, self.total_remaining)
        envelope_allowed = min(self.envelope_remaining, self.per_envelope_cap_chars, self.total_remaining)
        total_allowed = self.total_remaining

        rendered, visible_body_chars, body_policy, envelope_policy, primary_artifact_id, artifact_ids, reason = (
            self._render_with_allowance(
                block,
                text,
                body_allowed=body_allowed,
                envelope_allowed=envelope_allowed,
                total_allowed=total_allowed,
            )
        )
        reserved_applied = use_reserved and _latest_reserved_was_satisfied(
            body_candidate_chars=body_candidate_chars,
            visible_body_chars=visible_body_chars,
            body_policy=body_policy,
        )

        if reserved_applied:
            self.latest_reserved_remaining = max(0, self.latest_reserved_remaining - visible_body_chars)
        else:
            self.segment_remaining[budget_key] = max(0, budget_before - visible_body_chars)
        self.batch_remaining[tool_batch_id] = max(0, batch_before - visible_body_chars)
        rendered_envelope_chars = max(0, len(rendered) - visible_body_chars)
        self.body_remaining = max(0, self.body_remaining - visible_body_chars)
        self.envelope_remaining = max(0, self.envelope_remaining - rendered_envelope_chars)
        self.total_remaining = max(0, self.total_remaining - len(rendered))
        remaining_after = self.latest_reserved_remaining if reserved_applied else self.segment_remaining[budget_key]
        latest_reason = self._latest_reserved_reason(
            latest_candidate=latest_candidate,
            latest_short=latest_short,
            use_reserved=reserved_applied,
            body_candidate_chars=body_candidate_chars,
            body_candidate_source=body_candidate_source,
        )
        self.decisions.append(
            {
                "tool_call_id": block.id,
                "source_message_id": source_message_id,
                "source_assistant_message_id": source_assistant_message_id,
                "tool_batch_id": tool_batch_id,
                "tool_name": block.name,
                "segment": segment,
                "render_order": self._render_order,
                "state": block.state.value,
                "original_chars": original_chars,
                "body_candidate_chars": body_candidate_chars,
                "body_candidate_source": body_candidate_source,
                "latest_reserved_candidate": latest_candidate,
                "latest_reserved_applied": reserved_applied,
                "latest_reserved_reason": latest_reason,
                "visible_body_chars": visible_body_chars,
                "rendered_envelope_chars": rendered_envelope_chars,
                "rendered_total_chars": len(rendered),
                "body_budget_remaining": remaining_after,
                "batch_body_budget_remaining": self.batch_remaining[tool_batch_id],
                "envelope_budget_remaining": self.envelope_remaining,
                "primary_artifact_id": primary_artifact_id,
                "artifact_ids": artifact_ids,
                "artifact_ref_count": len(block.artifacts),
                "body_policy": body_policy,
                "envelope_policy": envelope_policy,
                "reason": reason,
                "clipped_envelope_fields": [],
                "read_more": _decision_read_more(primary_artifact_id, block),
                "diagnostics": _tool_result_decision_diagnostics(block, primary_artifact_id),
            }
        )
        return rendered

    def _render_with_allowance(
        self,
        block: ToolResultBlock,
        text: str,
        *,
        body_allowed: int,
        envelope_allowed: int,
        total_allowed: int,
    ) -> tuple[str, int, str, str, str | None, list[str], str]:
        artifact_ids = [artifact.artifact_id for artifact in block.artifacts]
        primary_artifact = _primary_text_artifact(block)
        primary_artifact_id = primary_artifact.artifact_id if primary_artifact is not None else None
        if not block.artifacts:
            parsed = _parse_tool_result_json(text)
            if body_allowed <= 0:
                essential = _terminal_essential_envelope(
                    block,
                    parsed=parsed,
                    artifact_refs=(),
                    per_envelope_cap_chars=self.per_envelope_cap_chars,
                )
                if essential is not None:
                    return (
                        essential,
                        0,
                        "omitted_non_artifact",
                        "essential_envelope",
                        None,
                        artifact_ids,
                        "budget_exhausted",
                    )
            if len(text) > body_allowed and not _has_room_for_clipped_preview(text, body_allowed):
                if parsed:
                    essential = _terminal_essential_envelope(
                        block,
                        parsed=parsed,
                        artifact_refs=(),
                        per_envelope_cap_chars=self.per_envelope_cap_chars,
                    )
                    if essential is not None:
                        return (
                            essential,
                            0,
                            "omitted_non_artifact",
                            "essential_envelope",
                            None,
                            artifact_ids,
                            "parseable_payload_over_body_budget",
                        )
                return (
                    _non_artifact_omitted_placeholder(),
                    0,
                    "omitted_non_artifact",
                    "essential_envelope",
                    None,
                    artifact_ids,
                    "budget_exhausted",
                )
            clipped, _ = _clip_with_remaining(text, body_allowed)
            if clipped == text:
                return clipped, len(clipped), "full_visible", "full_envelope", None, artifact_ids, "within_budget"
            if parsed:
                essential = _terminal_essential_envelope(
                    block,
                    parsed=parsed,
                    artifact_refs=(),
                    per_envelope_cap_chars=self.per_envelope_cap_chars,
                )
                if essential is not None:
                    return (
                        essential,
                        0,
                        "omitted_non_artifact",
                        "essential_envelope",
                        None,
                        artifact_ids,
                        "parseable_payload_over_body_budget",
                    )
            if body_allowed <= 0:
                return clipped, 0, "omitted_non_artifact", "essential_envelope", None, artifact_ids, "budget_exhausted"
            return clipped, len(clipped), "clipped_preview", "full_envelope", None, artifact_ids, "per_segment_budget"

        if body_allowed <= 0 or total_allowed <= 0 or (
            len(text) > body_allowed
            and not _has_room_for_clipped_preview(text, body_allowed)
        ):
            compact = _compact_artifact_envelope(block, per_envelope_cap_chars=self.per_envelope_cap_chars)
            return compact, 0, "artifact_preview", "essential_envelope", primary_artifact_id, artifact_ids, "budget_exhausted"

        artifact_payloads = _artifact_refs_for_model(block, primary_artifact=primary_artifact)
        envelope_overhead = len(
            json.dumps(
                {
                    "output_preview": "",
                    "output_truncated": False,
                    "artifacts": artifact_payloads,
                },
                ensure_ascii=False,
            )
        )
        body_budget = min(body_allowed, max(0, total_allowed - envelope_overhead))
        clipped, _ = _clip_with_remaining(text, body_budget)
        output_truncated = len(clipped) < len(text) or any(
            len(clipped.encode("utf-8")) < artifact.size_bytes for artifact in block.artifacts
        )
        envelope = {
            "output_preview": clipped,
            "output_truncated": output_truncated,
            "artifacts": artifact_payloads,
        }
        if block.artifacts and primary_artifact is None:
            envelope["primary_artifact_id"] = None
            envelope["artifact_ids"] = artifact_ids
            envelope["artifact_ref_count"] = len(block.artifacts)
            envelope["diagnostics"] = [{"code": "tool_result_primary_text_artifact_missing"}]
        rendered = json.dumps(envelope, ensure_ascii=False)
        rendered_envelope_chars = max(0, len(rendered) - len(clipped))
        if rendered_envelope_chars <= envelope_allowed and len(rendered) <= total_allowed:
            body_policy = "full_visible" if not output_truncated else "artifact_preview"
            return rendered, len(clipped), body_policy, "full_envelope", primary_artifact_id, artifact_ids, "within_budget"
        compact = _compact_artifact_envelope(block, per_envelope_cap_chars=self.per_envelope_cap_chars)
        return compact, 0, "artifact_preview", "essential_envelope", primary_artifact_id, artifact_ids, "envelope_over_budget"

    def _latest_reserved_reason(
        self,
        *,
        latest_candidate: bool,
        latest_short: bool,
        use_reserved: bool,
        body_candidate_chars: int | None,
        body_candidate_source: str | None,
    ) -> str:
        if not latest_candidate:
            return "not_latest_batch"
        if body_candidate_source == "non_short_truncated_preview":
            return "non_short_truncated_preview"
        if body_candidate_chars is None:
            return "body_candidate_unknown"
        if body_candidate_chars > self.latest_reserved_chars:
            return "body_candidate_exceeds_reserved"
        if not latest_short:
            return "not_short_result"
        if use_reserved:
            return "short_result_visible"
        return "latest_reserved_budget_unsatisfied"

    def report(self) -> dict[str, object]:
        used_by_scope: dict[str, dict[str, int]] = {}
        for segment, remaining in self.segment_remaining.items():
            cap_key = {
                "prior_history": "prior_tool_result_context_chars",
                "current_run_tail": "current_tail_normal_context_chars",
                "legacy_history": "legacy_tool_result_context_chars",
                "current_user": "current_tail_tool_result_context_chars",
            }.get(segment)
            cap = int(self.caps.get(cap_key or "", 0))
            used_by_scope[segment] = {"body": max(0, cap - remaining), "remaining": remaining}
        latest_reserved_total = self.caps["latest_reserved_total_chars"]
        used_by_scope["latest_reserved"] = {
            "body": max(0, latest_reserved_total - self.latest_reserved_remaining),
            "remaining": self.latest_reserved_remaining,
        }
        used_by_scope["envelope"] = {
            "envelope": max(0, self.caps["tool_result_envelope_context_chars"] - self.envelope_remaining),
            "remaining": self.envelope_remaining,
        }
        diagnostics: list[dict[str, object]] = []
        rendered_total = sum(_int_decision(decision.get("rendered_total_chars")) for decision in self.decisions)
        rendered_body = sum(_int_decision(decision.get("visible_body_chars")) for decision in self.decisions)
        rendered_envelope = sum(_int_decision(decision.get("rendered_envelope_chars")) for decision in self.decisions)
        if any(
            decision.get("latest_reserved_reason") == "latest_reserved_budget_unsatisfied"
            for decision in self.decisions
        ):
            diagnostics.append({"code": "latest_reserved_budget_unsatisfied"})
        if any(_decision_has_diagnostic(decision, "tool_result_primary_text_artifact_missing") for decision in self.decisions):
            diagnostics.append({"code": "tool_result_primary_text_artifact_missing"})
        if rendered_total > self.caps["tool_result_total_context_chars"]:
            diagnostics.append(
                {
                    "code": "tool_result_total_budget_unsatisfied",
                    "rendered_total_chars": rendered_total,
                    "cap": self.caps["tool_result_total_context_chars"],
                }
            )
        if rendered_body > self.caps["tool_result_body_context_chars"]:
            diagnostics.append(
                {
                    "code": "tool_result_body_budget_unsatisfied",
                    "rendered_body_chars": rendered_body,
                    "cap": self.caps["tool_result_body_context_chars"],
                }
            )
        if rendered_envelope > self.caps["tool_result_envelope_context_chars"]:
            diagnostics.append(
                {
                    "code": "essential_envelope_budget_unsatisfied",
                    "rendered_envelope_chars": rendered_envelope,
                    "cap": self.caps["tool_result_envelope_context_chars"],
                }
            )
        if len(self.decisions) > self.caps["max_tool_results_per_context"]:
            diagnostics.append(
                {
                    "code": "max_tool_results_per_context_exceeded",
                    "tool_result_count": len(self.decisions),
                    "cap": self.caps["max_tool_results_per_context"],
                }
            )
        return {
            "caps": dict(self.caps),
            "used": {
                "total": rendered_total,
                "body": rendered_body,
                "envelope": rendered_envelope,
            },
            "remaining": {
                "total": self.total_remaining,
                "body": self.body_remaining,
                "envelope": self.envelope_remaining,
            },
            "used_by_scope": used_by_scope,
            "used_by_batch": {
                batch_id: {
                    "body": max(0, self.per_message_cap_chars - remaining),
                    "remaining": remaining,
                }
                for batch_id, remaining in self.batch_remaining.items()
            },
            "diagnostics": diagnostics,
        }


def _render_tool_result_body(
    block: ToolResultBlock,
    budget: _ToolResultRenderAllocator,
    *,
    segment: str,
    source_message_id: str,
    source_assistant_message_id: str | None,
) -> str:
    return budget.render(
        block,
        segment=segment,
        source_message_id=source_message_id,
        source_assistant_message_id=source_assistant_message_id,
    )


def _int_decision(value: object) -> int:
    return value if isinstance(value, int) else 0


def _latest_reserved_was_satisfied(
    *,
    body_candidate_chars: int | None,
    visible_body_chars: int,
    body_policy: str,
) -> bool:
    return (
        body_policy == "full_visible"
        and body_candidate_chars is not None
        and visible_body_chars >= body_candidate_chars
    )


def _decision_has_diagnostic(decision: dict[str, object], code: str) -> bool:
    diagnostics = decision.get("diagnostics")
    if not isinstance(diagnostics, list):
        return False
    return any(
        isinstance(diagnostic, dict) and diagnostic.get("code") == code
        for diagnostic in diagnostics
    )


def _source_assistant_message_ids(messages: list[Msg]) -> dict[int, str | None]:
    by_index: dict[int, str | None] = {}
    last_tool_call_message_id: str | None = None
    for index, message in enumerate(messages):
        has_tool_call = any(isinstance(block, ToolCallBlock) for block in message.content)
        has_tool_result = any(isinstance(block, ToolResultBlock) for block in message.content)
        if message.role == "assistant" and has_tool_call:
            last_tool_call_message_id = message.id
        if has_tool_result:
            by_index[index] = last_tool_call_message_id
        if message.role == "user":
            last_tool_call_message_id = None
    return by_index


def _latest_tail_tool_result_ids(
    messages: list[Msg],
    *,
    anchor_index: int | None,
    source_assistant_by_index: dict[int, str | None],
) -> set[str]:
    if anchor_index is None:
        return set()
    tail_indices = [
        index
        for index in range(anchor_index + 1, len(messages))
        if any(isinstance(block, ToolResultBlock) for block in messages[index].content)
    ]
    if not tail_indices:
        return set()
    latest_index = tail_indices[-1]
    latest_source_assistant = source_assistant_by_index.get(latest_index)
    selected_indices = [
        index
        for index in tail_indices
        if latest_source_assistant is not None
        and source_assistant_by_index.get(index) == latest_source_assistant
    ]
    if not selected_indices:
        selected_indices = [latest_index]
    ids: set[str] = set()
    for index in selected_indices:
        ids.update(
            block.id
            for block in messages[index].content
            if isinstance(block, ToolResultBlock)
        )
    return ids


def _latest_reserved_tool_result_ids(
    messages: list[Msg],
    *,
    latest_ids: set[str],
    latest_reserved_chars: int,
) -> set[str]:
    reserved: set[str] = set()
    for message in messages:
        for block in message.content:
            if not isinstance(block, ToolResultBlock) or block.id not in latest_ids:
                continue
            body_candidate_chars, body_candidate_source = _tool_result_body_candidate(
                block,
                _tool_result_text(block),
            )
            if (
                body_candidate_chars is not None
                and body_candidate_chars <= latest_reserved_chars
                and body_candidate_source != "non_short_truncated_preview"
            ):
                reserved.add(block.id)
    return reserved


def _tool_result_original_chars(block: ToolResultBlock, text: str) -> int | None:
    artifact_originals = [
        artifact.preview.original_chars
        for artifact in block.artifacts
        if artifact.preview is not None
    ]
    if artifact_originals:
        return max(artifact_originals)
    parsed = _parse_tool_result_json(text)
    for key in ("output_original_chars", "original_chars", "output_chars", "chars"):
        value = parsed.get(key)
        if isinstance(value, int):
            return value
    return len(text)


def _tool_result_body_candidate(block: ToolResultBlock, text: str) -> tuple[int | None, str | None]:
    parsed = _parse_tool_result_json(text)
    output = parsed.get("output")
    if isinstance(output, str):
        preview_policy = parsed.get("preview_policy")
        original_chars = parsed.get("output_original_chars")
        omitted_middle_chars = parsed.get("omitted_middle_chars")
        if (
            preview_policy not in (None, "full")
            or (isinstance(omitted_middle_chars, int) and omitted_middle_chars > 0)
            or (isinstance(original_chars, int) and original_chars > len(output))
            or parsed.get("truncated") is True
        ):
            return (
                original_chars if isinstance(original_chars, int) else len(output),
                "non_short_truncated_preview",
            )
        return len(output), "terminal_output_field"
    for artifact in block.artifacts:
        if not _is_text_artifact(artifact):
            continue
        preview = artifact.preview
        if preview is None:
            continue
        if preview.preview_policy == "full" or preview.original_chars == preview.preview_chars:
            return preview.preview_chars, "artifact_preview_full"
        return preview.original_chars, "non_short_truncated_preview"
    return len(text), "render_source_text_fallback"


def _parse_tool_result_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return {}
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _primary_text_artifact(block: ToolResultBlock) -> ToolResultArtifactRef | None:
    text_artifacts = [
        artifact
        for artifact in block.artifacts
        if _is_text_artifact(artifact)
    ]
    with_preview = next((artifact for artifact in text_artifacts if artifact.preview is not None), None)
    if with_preview is not None:
        return with_preview
    if text_artifacts:
        return text_artifacts[0]
    return None


def _is_text_artifact(artifact: ToolResultArtifactRef) -> bool:
    if artifact.role in {"diagnostics", "metadata"}:
        return False
    media_type = artifact.media_type.split(";", 1)[0].strip().lower()
    return media_type.startswith("text/") or media_type in {
        "application/json",
        "application/x-ndjson",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
    } or media_type.endswith("+json") or media_type.endswith("+xml")


def _artifact_refs_for_model(
    block: ToolResultBlock,
    *,
    primary_artifact: ToolResultArtifactRef | None,
) -> list[dict[str, object]]:
    return [
        _artifact_ref_payload(
            artifact,
            include_read_more=(
                primary_artifact is not None
                and artifact.artifact_id == primary_artifact.artifact_id
            ),
        )
        for artifact in block.artifacts
    ]


def _artifact_ref_payload(
    artifact: ToolResultArtifactRef,
    *,
    include_read_more: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_id": artifact.artifact_id,
        "role": artifact.role,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "stored_complete": artifact.stored_complete,
    }
    if artifact.loss_reason is not None:
        payload["loss_reason"] = artifact.loss_reason
    if artifact.preview is not None:
        preview = artifact.preview.model_dump()
        if include_read_more:
            preview["read_more"] = _compact_read_more_payload(artifact)
        else:
            preview.pop("read_more", None)
        payload["preview"] = preview
    if include_read_more:
        payload["read_more"] = _compact_read_more_payload(artifact)
    return payload


def _tool_result_decision_diagnostics(
    block: ToolResultBlock,
    primary_artifact_id: str | None,
) -> list[dict[str, object]]:
    if block.artifacts and primary_artifact_id is None:
        return [{"code": "tool_result_primary_text_artifact_missing"}]
    return []


def _decision_read_more(artifact_id: str | None, block: ToolResultBlock) -> dict[str, object] | None:
    if artifact_id is None:
        return None
    artifact = next((candidate for candidate in block.artifacts if candidate.artifact_id == artifact_id), None)
    if artifact is None:
        return {"tool": "artifact_read", "artifact_id": artifact_id}
    return _compact_read_more_payload(artifact)


def _compact_artifact_envelope(block: ToolResultBlock, *, per_envelope_cap_chars: int = 1_200) -> str:
    artifact = _primary_text_artifact(block)
    refs = [_compact_artifact_ref_payload(artifact)] if artifact is not None else []
    omitted = max(0, len(block.artifacts) - len(refs))
    parsed = _parse_tool_result_json(_tool_result_text(block))
    essential = _terminal_essential_envelope(
        block,
        parsed=parsed,
        artifact_refs=tuple(refs),
        per_envelope_cap_chars=per_envelope_cap_chars,
    )
    if essential is not None:
        payload = json.loads(essential)
    else:
        payload = {
            "output_preview": (
                _artifact_backed_omitted_placeholder()
                if refs
                else _non_artifact_omitted_placeholder()
            ),
            "output_truncated": True,
            "artifacts": refs,
        }
    if block.artifacts and artifact is None:
        payload["primary_artifact_id"] = None
        payload["artifact_ids"] = [candidate.artifact_id for candidate in block.artifacts]
        payload["artifact_ref_count"] = len(block.artifacts)
        payload["diagnostics"] = [
            *(
                payload["diagnostics"]
                if isinstance(payload.get("diagnostics"), list)
                else []
            ),
            {"code": "tool_result_primary_text_artifact_missing"},
        ]
    payload["artifact_refs_omitted"] = omitted
    return json.dumps(payload, ensure_ascii=False)


def _terminal_essential_envelope(
    block: ToolResultBlock,
    *,
    parsed: dict[str, Any],
    artifact_refs: tuple[dict[str, object], ...],
    per_envelope_cap_chars: int,
) -> str | None:
    if not _is_terminal_like_payload(block, parsed):
        return None
    return _essential_tool_result_envelope(
        block,
        parsed=parsed,
        artifact_refs=artifact_refs,
        per_envelope_cap_chars=per_envelope_cap_chars,
    )


def _is_terminal_like_payload(block: ToolResultBlock, parsed: dict[str, Any]) -> bool:
    if block.name in {"terminal", "terminal_process"}:
        return True
    return (
        "terminal_process_action" in parsed
        or "terminal_session_id" in parsed
        or "backend_type" in parsed
    )


def _essential_tool_result_envelope(
    block: ToolResultBlock,
    *,
    parsed: dict[str, Any],
    artifact_refs: tuple[dict[str, object], ...],
    per_envelope_cap_chars: int,
) -> str | None:
    if not parsed:
        return None
    essential_keys = (
        "status",
        "exit_code",
        "cwd",
        "timed_out",
        "truncated",
        "error",
        "process_id",
        "terminal_session_id",
        "yielded_to_background",
        "backend_type",
        "io_mode",
        "terminal_process_action",
        "duration_seconds",
        "stdin_closed",
        "policy_code",
        "live_process_count",
        "finished_process_count",
    )
    payload: dict[str, object] = {
        "output_preview": (
            _artifact_backed_omitted_placeholder()
            if artifact_refs
            else _non_artifact_omitted_placeholder()
        ),
        "output_truncated": True,
        "tool_result_body_omitted": True,
        "tool_result_body_omitted_reason": "tool_result_render_budget_exhausted",
    }
    for key in essential_keys:
        value = parsed.get(key)
        if value is not None:
            payload[key] = _clip_envelope_value(value, max_string_chars=240)
    if parsed.get("terminal_process_action") == "list" and isinstance(parsed.get("processes"), list):
        processes, omitted = _summarize_terminal_processes(parsed["processes"])
        payload["processes_summary"] = processes
        payload["processes_summary_truncated"] = omitted > 0
        payload["omitted_process_count"] = omitted
    if artifact_refs:
        payload["artifacts"] = list(artifact_refs)
    rendered = json.dumps(payload, ensure_ascii=False)
    if len(rendered) <= per_envelope_cap_chars:
        return rendered
    clipped_payload = dict(payload)
    for key in ("error", "cwd", "command"):
        if isinstance(clipped_payload.get(key), str):
            clipped_payload[key] = _clip_string(str(clipped_payload[key]), 96)
    if "processes_summary" in clipped_payload and isinstance(clipped_payload["processes_summary"], list):
        clipped_payload["processes_summary"] = clipped_payload["processes_summary"][:3]
        clipped_payload["processes_summary_truncated"] = True
    rendered = json.dumps(clipped_payload, ensure_ascii=False)
    if len(rendered) <= per_envelope_cap_chars:
        return rendered
    minimal: dict[str, object] = {
        "output_preview": payload["output_preview"],
        "output_truncated": True,
        "tool_result_body_omitted": True,
        "tool_result_body_omitted_reason": "tool_result_render_budget_exhausted",
        "status": payload.get("status", block.state.value),
    }
    for key in ("exit_code", "process_id", "terminal_process_action", "error"):
        if key in payload:
            minimal[key] = _clip_envelope_value(payload[key], max_string_chars=72)
    if artifact_refs:
        minimal["artifacts"] = list(artifact_refs[:1])
    return json.dumps(minimal, ensure_ascii=False)


def _summarize_terminal_processes(processes: list[Any], *, max_processes: int = 8) -> tuple[list[dict[str, object]], int]:
    dicts = [process for process in processes if isinstance(process, dict)]

    def sort_key(process: dict[str, Any]) -> tuple[int, float, str]:
        status = str(process.get("status") or "")
        actionable = status in {"running", "pending", "blocked"} or bool(process.get("yielded_to_background"))
        timestamp = _process_sort_timestamp(process)
        return (0 if actionable else 1, -timestamp, str(process.get("process_id") or ""))

    summaries: list[dict[str, object]] = []
    for process in sorted(dicts, key=sort_key)[:max_processes]:
        summary: dict[str, object] = {}
        for key in ("process_id", "status", "cwd", "exit_code", "terminal_session_id", "backend_type"):
            value = process.get(key)
            if value is not None:
                summary[key] = _clip_envelope_value(value, max_string_chars=160)
        summaries.append(summary)
    return summaries, max(0, len(dicts) - len(summaries))


def _process_sort_timestamp(process: dict[str, Any]) -> float:
    for key in (
        "ended_at_monotonic",
        "finished_at_monotonic",
        "finished_at",
        "started_at_monotonic",
        "started_at",
    ):
        value = process.get(key)
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            parsed = _parse_timestamp(value)
            if parsed is not None:
                return parsed
    return 0.0


def _parse_timestamp(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _clip_envelope_value(value: object, *, max_string_chars: int) -> object:
    if isinstance(value, str):
        return _clip_string(value, max_string_chars)
    return value


def _clip_string(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    marker = f"...[clipped {len(value) - max_chars} chars]"
    kept = max(0, max_chars - len(marker))
    return value[:kept] + marker


def _artifact_backed_omitted_placeholder() -> str:
    return (
        "[TOOL RESULT BODY OMITTED: tool-result render budget exhausted; "
        "full output is available via artifact_read]"
    )


def _non_artifact_omitted_placeholder() -> str:
    return (
        "[TOOL RESULT BODY OMITTED: tool-result render budget exhausted; "
        "no artifact was retained for this result]"
    )


def _compact_artifact_ref_payload(artifact: ToolResultArtifactRef) -> dict[str, object]:
    payload = _artifact_ref_payload(artifact, include_read_more=True)
    if artifact.preview is not None:
        read_more = _compact_read_more_payload(artifact)
        payload["preview"] = {
            "preview_policy": artifact.preview.preview_policy,
            "visible_head_chars": artifact.preview.visible_head_chars,
            "read_more": read_more,
        }
        payload["read_more"] = read_more
    return payload


def _compact_read_more_payload(artifact: ToolResultArtifactRef) -> dict[str, object]:
    read_more: dict[str, object] = {"tool": "artifact_read", "artifact_id": artifact.artifact_id}
    if artifact.preview is None:
        return read_more
    for key in ("suggested_offset_chars", "suggested_max_chars"):
        value = artifact.preview.read_more.get(key)
        if isinstance(value, int):
            read_more[key] = value
    return read_more


def _clip_with_remaining(text: str, remaining: int) -> tuple[str, int]:
    if remaining <= 0:
        return _non_artifact_omitted_placeholder(), 0
    if len(text) <= remaining:
        return text, remaining - len(text)
    marker = _truncation_marker(text_len=len(text), remaining=remaining)
    kept = max(0, remaining - len(marker))
    return text[:kept] + marker, 0


def _has_room_for_clipped_preview(text: str, remaining: int) -> bool:
    if remaining <= 0 or len(text) <= remaining:
        return True
    return remaining > len(_truncation_marker(text_len=len(text), remaining=remaining))


def _truncation_marker(*, text_len: int, remaining: int) -> str:
    return (
        f"\n[TOOL RESULT BODY TRUNCATED: kept {remaining} of {text_len} chars "
        "due to tool-result render budget]"
    )


def _data_placeholder(block: DataBlock) -> str:
    media_type = "unknown"
    source_kind = "data"
    if isinstance(block.source, Base64Source):
        media_type = block.source.media_type
        source_kind = "base64"
    elif isinstance(block.source, URLSource):
        media_type = block.source.media_type
        source_kind = "url"
    name = f" name={block.name}" if block.name else ""
    return f"[data block omitted id={block.id}{name} media_type={media_type} source={source_kind}]"
