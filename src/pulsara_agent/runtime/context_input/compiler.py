"""Pure compiler entrypoint for immutable context-input facts.

This module is the production C3 boundary.  It does not accept or import
``LoopState``/``Msg`` and performs no lifecycle-cache or storage I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from pulsara_agent.llm.estimator import TokenEstimator
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, ToolSpec
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.primitives.context import (
    ContextArtifactTextFact,
    ContextInlineTextFact,
    ContextSourceTimingFact,
    PreparedContextCandidateSet,
    TranscriptCompileInput,
    TranscriptDataPlaceholderFact,
    TranscriptTextBlockFact,
    TranscriptThinkingBlockFact,
    TranscriptToolCallFact,
    TranscriptToolResultRefFact,
    context_fingerprint,
    thaw_json,
)
from pulsara_agent.primitives.tool_result import ToolResultRenderDecisionFact
from pulsara_agent.runtime.context_engine.types import (
    CompiledContext,
    CompiledContextSection,
    CompiledToolSpecUnit,
    ContextBudgetExceeded,
    ContextBudgetReport,
    ContextDiagnostic,
    ContextRenderMode,
    AllocatedContextSection,
)
from pulsara_agent.runtime.context_input.render import (
    PreparedToolResultRenderOutput,
    RenderedToolResultFragment,
)
from pulsara_agent.runtime.context_input.candidate import (
    validate_candidate_against_snapshot,
)
from pulsara_agent.runtime.context_input.snapshot import ContextFactSnapshot


@dataclass(frozen=True, slots=True)
class LoweredTranscriptMessages:
    full_messages: tuple[LLMMessage, ...]
    prior_history_messages: tuple[LLMMessage, ...]
    current_user_messages: tuple[LLMMessage, ...]
    current_run_tail_messages: tuple[LLMMessage, ...]


def compile_context_from_facts(
    *,
    facts: ContextFactSnapshot,
    transcript: TranscriptCompileInput,
    rendered_tool_results: PreparedToolResultRenderOutput,
    section_candidates: PreparedContextCandidateSet,
) -> CompiledContext:
    """Lower one already-prepared immutable aggregate into ``LLMContext``."""

    snapshot = facts.fact
    identity = snapshot.identity
    call = facts.resolved_call
    estimator = call.target.token_estimator
    lowered_transcript = lower_transcript_for_context(
        transcript=transcript,
        rendered_tool_results=rendered_tool_results,
    )
    if transcript.transcript_fingerprint == "":
        raise ValueError("compiler transcript fingerprint is required")
    if section_candidates.policy != snapshot.compile_policy.candidate_collection:
        raise ValueError("compiler candidate policy differs from snapshot")
    for entry in section_candidates.entries:
        validate_candidate_against_snapshot(
            snapshot=snapshot,
            candidate=entry.candidate,
        )

    sections = sorted(
        (
            _candidate_section(entry.candidate, entry.lifecycle.status, estimator)
            for entry in section_candidates.entries
        ),
        key=lambda section: (section.priority, section.id),
    )
    sections.extend(
        _transcript_sections(
            lowered_transcript,
            transcript=transcript,
            snapshot=snapshot,
            estimator=estimator,
        )
    )
    sections = list(
        _apply_timing_overlay(
            snapshot=snapshot,
            sections=tuple(sections),
            estimator=estimator,
        )
    )
    tools, tool_units = _compile_tools(facts)
    target = call.target
    if tools and not target.fact.supports_tools:
        raise _budget_error(
            facts=facts,
            sections=tuple(sections),
            tool_units=tool_units,
            code="model_target_does_not_support_tools",
            message="Resolved model target does not support tool schemas.",
            rendered=rendered_tool_results,
            measurement_stage="section_allocation",
        )
    sections, diagnostics = _apply_section_budget(
        tuple(sections),
        (),
        input_budget_tokens=target.context_budget.input_budget_tokens,
        tools_estimated_tokens=sum(item.estimated_tokens for item in tool_units),
        estimator=estimator,
    )
    system_prompt = _system_prompt(sections)
    messages, scopes = _lower_messages(lowered_transcript, sections=sections)
    llm_context = LLMContext(
        messages=messages,
        tools=tools,
        system_prompt=system_prompt,
        context_id=identity.context_id,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        target_fingerprint=target.fact.target_fingerprint,
        model_call_index=identity.model_call_index,
    )
    estimate = estimator.estimate_context(llm_context)
    if len(scopes) != len(estimate.message_tokens_by_index):
        raise RuntimeError("compiler message budget scopes do not match messages")
    transcript_tokens = sum(
        estimate.message_tokens_by_index[index]
        for index, scope in enumerate(scopes)
        if scope == "transcript"
    )
    baseline_tokens = (
        estimate.system_tokens
        + estimate.tool_tokens
        + estimate.envelope_tokens
        + sum(
            estimate.message_tokens_by_index[index]
            for index, scope in enumerate(scopes)
            if scope == "non_transcript"
        )
    )
    compiled_sections = tuple(_compiled_section(section) for section in sections)
    sections_tokens = sum(
        section.estimated_tokens
        for section in compiled_sections
        if section.included and section.metadata.get("counted_in") is None
    )
    tools_tokens = sum(item.estimated_tokens for item in tool_units if item.included)
    budget = ContextBudgetReport(
        target_fingerprint=target.fact.target_fingerprint,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        measurement_stage="final_payload",
        total_context_tokens=target.limits.total_context_tokens,
        max_input_tokens=target.limits.max_input_tokens,
        max_output_tokens=target.limits.max_output_tokens,
        effective_output_tokens=target.context_budget.effective_output_tokens,
        safety_margin_tokens=target.context_budget.safety_margin_tokens,
        input_budget_tokens=target.context_budget.input_budget_tokens,
        sections_estimated_tokens=sections_tokens,
        tools_estimated_tokens=tools_tokens,
        envelope_estimated_tokens=estimate.envelope_tokens,
        allocation_estimated_tokens=sections_tokens + tools_tokens,
        final_payload_estimated_tokens=estimate.total_input_tokens,
        non_transcript_baseline_tokens=baseline_tokens,
        transcript_estimated_tokens=transcript_tokens,
        estimator=estimator.fact,
    )
    current_user_tokens = sum(
        estimator.estimate_message(message)
        for message in lowered_transcript.current_user_messages
    )
    if current_user_tokens > target.context_budget.input_budget_tokens:
        raise ContextBudgetExceeded(
            "Current user input exceeds the available model input budget; "
            "please split the request into smaller turns.",
            context_id=identity.context_id,
            model_call_index=identity.model_call_index,
            diagnostics=(
                ContextDiagnostic(
                    severity="error",
                    code="current_user_exceeds_model_input_budget",
                    message=(
                        "Current user input exceeds the resolved model input budget."
                    ),
                    section_id="transcript:current_user",
                ),
            ),
            tool_result_render_decisions=(
                rendered_tool_results.tool_result_render_decisions
            ),
            tool_result_budget_report=dict(
                rendered_tool_results.tool_result_budget_report
            ),
            budget_report=budget,
        )
    if estimate.total_input_tokens > target.context_budget.input_budget_tokens:
        raise ContextBudgetExceeded(
            "Final model payload exceeds the resolved model input budget.",
            context_id=identity.context_id,
            model_call_index=identity.model_call_index,
            diagnostics=(
                *diagnostics,
                ContextDiagnostic(
                    severity="error",
                    code="context_budget_still_exceeded",
                    message=(
                        "Final lowered model payload exceeds the resolved input budget."
                    ),
                ),
            ),
            tool_result_render_decisions=(
                rendered_tool_results.tool_result_render_decisions
            ),
            tool_result_budget_report=dict(
                rendered_tool_results.tool_result_budget_report
            ),
            budget_report=budget,
        )
    llm_context = replace(
        llm_context,
        compiler_estimated_input_tokens=estimate.total_input_tokens,
    )
    return CompiledContext(
        context_id=identity.context_id,
        llm_context=llm_context,
        sections=compiled_sections,
        tool_specs=tool_units,
        diagnostics=diagnostics,
        lifecycle_decisions=(
            *(
                {
                    "kind": "candidate_lifecycle",
                    **entry.lifecycle.model_dump(mode="json"),
                }
                for entry in section_candidates.entries
            ),
            *(
                {
                    "kind": "candidate_invalidation",
                    **invalidation.model_dump(mode="json"),
                }
                for invalidation in section_candidates.invalidations
            ),
        ),
        estimated_tokens=estimate.total_input_tokens,
        budget=budget,
        resolved_model_call=call.fact,
        final_token_estimate=estimate,
        message_budget_scopes=scopes,
        tool_result_render_decisions=(
            rendered_tool_results.tool_result_render_decisions
        ),
        tool_result_budget_report=dict(rendered_tool_results.tool_result_budget_report),
        tool_result_render_decision_facts=(rendered_tool_results.canonical_decisions),
        tool_result_render_operational_facts=(rendered_tool_results.operational_facts),
    )


def provider_neutral_payload_fingerprint(context: LLMContext) -> str:
    payload = {
        "system_prompt": context.system_prompt,
        "messages": tuple(
            {
                "role": message.role.value,
                "content": message.content,
                "thinking": message.thinking,
                "tool_calls": tuple(
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                    for call in message.tool_calls
                ),
                "tool_call_id": message.tool_call_id,
                "name": message.name,
                "arguments": message.arguments,
            }
            for message in context.messages
        ),
        "tools": tuple(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in context.tools
        ),
    }
    return context_fingerprint("provider-neutral-llm-context:v1", payload)


def canonical_render_decisions_fingerprint(
    decisions: tuple[ToolResultRenderDecisionFact, ...],
) -> str:
    return context_fingerprint(
        "context-canonical-render-decisions:v1",
        tuple(item.decision_fingerprint for item in decisions),
    )


def lower_transcript_for_context(
    *,
    transcript: TranscriptCompileInput,
    rendered_tool_results: PreparedToolResultRenderOutput,
) -> LoweredTranscriptMessages:
    """The sole normalized-transcript -> provider-message lowering seam."""

    fragments = {
        fragment.unit_id: fragment for fragment in rendered_tool_results.fragments
    }
    if len(fragments) != len(rendered_tool_results.fragments):
        raise ValueError("rendered tool-result fragment IDs are not unique")
    expected_refs = tuple(
        block.tool_result_unit_id
        for message in transcript.messages
        for block in message.blocks
        if isinstance(block, TranscriptToolResultRefFact)
    )
    if tuple(fragments) != expected_refs:
        raise ValueError("rendered fragments do not match transcript result refs")
    pairs_by_call = {pair.tool_call_id: pair for pair in transcript.tool_pairs}

    full: list[LLMMessage] = []
    prior: list[LLMMessage] = []
    current: list[LLMMessage] = []
    tail: list[LLMMessage] = []
    for message_index, message in enumerate(transcript.messages):
        segment = _normalized_segment(message.segment)
        lowered = _lower_transcript_message(
            message=message,
            message_index=message_index,
            segment=segment,
            fragments=fragments,
            pairs_by_call=pairs_by_call,
        )
        full.extend(lowered)
        if segment == "current_user":
            current.extend(lowered)
        elif segment == "current_run_tail":
            tail.extend(lowered)
        else:
            prior.extend(lowered)
    return LoweredTranscriptMessages(
        full_messages=tuple(full),
        prior_history_messages=tuple(prior),
        current_user_messages=tuple(current),
        current_run_tail_messages=tuple(tail),
    )


def _lower_transcript_message(
    *,
    message,
    message_index: int,
    segment: str,
    fragments: dict[str, RenderedToolResultFragment],
    pairs_by_call,
) -> tuple[LLMMessage, ...]:
    lowered: list[LLMMessage] = []
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[LLMToolCall] = []

    def flush() -> None:
        if not text_parts and not thinking_parts and not tool_calls:
            return
        if message.role == "assistant":
            lowered.append(
                LLMMessage.assistant_turn(
                    text="\n".join(text_parts),
                    thinking=tuple(thinking_parts),
                    tool_calls=tuple(tool_calls),
                )
            )
        elif message.role == "system":
            lowered.append(LLMMessage.system("\n".join(text_parts)))
        else:
            lowered.append(LLMMessage.user("\n".join(text_parts)))
        text_parts.clear()
        thinking_parts.clear()
        tool_calls.clear()

    for block_index, block in enumerate(message.blocks):
        if isinstance(block, TranscriptTextBlockFact):
            text_parts.append(block.text)
        elif isinstance(block, TranscriptThinkingBlockFact):
            thinking_parts.append(block.thinking)
        elif isinstance(block, TranscriptDataPlaceholderFact):
            name = f" name={block.name}" if block.name else ""
            text_parts.append(
                f"[data block omitted id={block.block_id}{name} "
                f"media_type={block.media_type} source={block.source_kind}]"
            )
        elif isinstance(block, TranscriptToolCallFact):
            tool_calls.append(
                LLMToolCall(
                    id=block.tool_call_id,
                    name=block.model_tool_name,
                    arguments=block.raw_arguments_json,
                )
            )
        elif isinstance(block, TranscriptToolResultRefFact):
            flush()
            fragment = fragments[block.tool_result_unit_id]
            pair = pairs_by_call.get(block.tool_call_id)
            if pair is None:
                raise ValueError("tool-result ref lacks normalized interaction pair")
            if (
                fragment.tool_call_id != block.tool_call_id
                or fragment.source_message_id != message.message_id
                or fragment.source_message_index != message_index
                or fragment.content_block_index != block_index
                or fragment.segment != segment
                or pair.result_message_id != message.message_id
                or pair.result_block_index != block_index
            ):
                raise ValueError("rendered fragment transcript attribution mismatch")
            lowered.append(
                LLMMessage.tool_result(
                    fragment.text,
                    tool_call_id=fragment.tool_call_id,
                )
            )
    flush()
    return tuple(lowered)


def _normalized_segment(segment: str) -> str:
    if segment in {"current_user", "current_run_tail"}:
        return segment
    return "prior_history"


def _candidate_section(
    candidate, lifecycle_status: str, estimator
) -> AllocatedContextSection:
    payload = candidate.payload
    if isinstance(payload, ContextInlineTextFact):
        text = payload.text
    elif isinstance(payload, ContextArtifactTextFact):
        raise ValueError(
            "artifact-backed candidate must be materialized before pure compile"
        )
    else:  # pragma: no cover - closed Pydantic union
        raise TypeError("unsupported candidate payload")
    stability = {
        "stable": "stable",
        "run": "turn",
        "step": "step",
        "ephemeral": "ephemeral",
    }[candidate.stability]
    return AllocatedContextSection(
        id=candidate.source_instance_id,
        source_id=candidate.source_kind,
        channel=candidate.channel.value,
        priority=candidate.priority,
        stability=stability,
        budget_class="must_keep" if candidate.required else "important",
        text=text,
        estimated_tokens=estimator.estimate_text(text),
        provenance={
            "source_fact_refs": [
                item.model_dump(mode="json") for item in candidate.source_fact_refs
            ],
            "source_artifact_ids": list(candidate.source_artifact_ids),
            "candidate_fingerprint": candidate.candidate_fingerprint,
        },
        metadata={
            "chars": len(text),
            "source_timing": candidate.source_timing.model_dump(mode="json"),
            "lowering_kind": candidate.lowering_kind,
        },
        lifecycle_status=lifecycle_status,
        lifecycle_reason="prepared_candidate_set",
        dependency_fingerprint=candidate.lifecycle_dependency_fingerprint,
    )


def _apply_timing_overlay(
    *,
    snapshot,
    sections: tuple[AllocatedContextSection, ...],
    estimator: TokenEstimator,
) -> tuple[AllocatedContextSection, ...]:
    overlaid: list[AllocatedContextSection] = []
    for section in sections:
        source = ContextSourceTimingFact.model_validate(
            section.metadata.get("source_timing")
        )
        age_seconds = _age_seconds(
            compiled_at_utc=snapshot.timing.compiled_at_utc,
            source=source,
        )
        timing = {
            "compiled_at_utc": snapshot.timing.compiled_at_utc,
            "session_timezone": snapshot.timing.session_timezone,
            "compiled_local_date": snapshot.timing.compiled_local_date,
            "age_seconds": age_seconds,
            "source": source.model_dump(mode="json"),
        }
        metadata = {
            **section.metadata,
            "source_timing": source.model_dump(mode="json"),
            "timing": timing,
        }
        if _section_renders_timing_header(section):
            header = _timing_header_text(timing)
            metadata["timing_header_text"] = header
            metadata["rendered_timing_header_tokens"] = estimator.estimate_text(header)
            metadata["rendered_timing_header_chars"] = len(header)
        overlaid.append(
            replace(
                section,
                metadata=metadata,
                estimated_tokens=_estimate_section_tokens(
                    section,
                    text=section.text,
                    estimator=estimator,
                    metadata=metadata,
                ),
            )
        )
    return tuple(overlaid)


def _transcript_source_timing(
    messages,
    *,
    channel: str,
    snapshot,
) -> ContextSourceTimingFact:
    started_at = next(
        (message.created_at_utc for message in messages if message.created_at_utc),
        None,
    )
    ended_at = next(
        (
            message.finished_at_utc or message.created_at_utc
            for message in reversed(messages)
            if message.finished_at_utc or message.created_at_utc
        ),
        None,
    )
    observed_at = ended_at or started_at
    if channel == "current_user":
        freshness = "current_turn"
        observed_at = observed_at or snapshot.current_user_message.observed_at_utc
    elif channel == "current_run_tail":
        freshness = "current_run_tail"
    else:
        freshness = (
            "compacted_history"
            if any(message.segment == "compaction_summary" for message in messages)
            else "historical_replay"
        )
    payload = {
        "observed_at_utc": observed_at,
        "source_started_at_utc": started_at,
        "source_ended_at_utc": ended_at,
        "source_sequence_start": min(
            (message.source_sequence_start for message in messages),
            default=None,
        ),
        "source_sequence_end": max(
            (message.source_sequence_end for message in messages),
            default=None,
        ),
        "freshness": freshness,
        "clock_source": ("message_created_at" if started_at or ended_at else "mixed"),
    }
    return ContextSourceTimingFact(
        **payload,
        timing_fingerprint=context_fingerprint("context-source-timing:v1", payload),
    )


def _section_renders_timing_header(section: AllocatedContextSection) -> bool:
    return section.id != "system:prompt" and section.metadata.get("lowering_kind") in {
        "system_instruction",
        "leading_user_context",
        "handoff_hint",
    }


def _age_seconds(
    *,
    compiled_at_utc: str,
    source: ContextSourceTimingFact,
) -> float | None:
    source_time = (
        source.source_ended_at_utc
        or source.observed_at_utc
        or source.source_started_at_utc
    )
    if source_time is None:
        return None
    compiled = datetime.fromisoformat(compiled_at_utc.replace("Z", "+00:00"))
    observed = datetime.fromisoformat(source_time.replace("Z", "+00:00"))
    return max(0.0, round((compiled - observed).total_seconds(), 3))


def _timing_header_text(timing: dict[str, object]) -> str:
    source = timing["source"]
    assert isinstance(source, dict)
    parts = [
        f"freshness={source['freshness']}",
        f"compiled_at_utc={timing['compiled_at_utc']}",
    ]
    if timing.get("session_timezone"):
        parts.append(f"session_timezone={timing['session_timezone']}")
    if timing.get("compiled_local_date"):
        parts.append(f"local_date={timing['compiled_local_date']}")
    started = source.get("source_started_at_utc")
    ended = source.get("source_ended_at_utc")
    observed = source.get("observed_at_utc")
    if started and ended:
        parts.append(f"source={started}..{ended}")
    elif observed:
        parts.append(f"source={observed}")
    else:
        parts.append("source=unknown")
    age = timing.get("age_seconds")
    if isinstance(age, int | float):
        parts.append(f"age={_format_duration(float(age))}")
    return "[context timing: " + "; ".join(parts) + "]"


def _format_duration(seconds: float) -> str:
    whole = max(0, int(seconds))
    if whole < 60:
        return f"{whole}s"
    minutes, second = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m{second:02d}s"
    hours, minute = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minute:02d}m"
    days, hour = divmod(hours, 24)
    return f"{days}d{hour:02d}h"


def _component_label(component_id: str) -> str:
    if component_id.startswith("runtime_context"):
        return "Runtime Context"
    if component_id.startswith("memory:projection"):
        return "Recalled Memory and Working Context"
    if component_id.startswith("memory:hook"):
        return "Memory Instructions"
    if component_id.startswith("capability:catalog"):
        return "Available Capabilities"
    if component_id.startswith("capability:active_skill"):
        return "Active Skill"
    if component_id.startswith("subagent:results"):
        return "Subagent Results"
    return component_id


def _render_section_text_with_timing(section: AllocatedContextSection) -> str:
    header = section.metadata.get("timing_header_text")
    if not isinstance(header, str) or not header:
        return section.text
    return f"{header}\n{section.text}" if section.text else header


def _estimate_section_tokens(
    section: AllocatedContextSection,
    *,
    text: str,
    estimator: TokenEstimator,
    metadata: dict | None = None,
) -> int:
    actual_metadata = metadata if metadata is not None else section.metadata
    header = actual_metadata.get("timing_header_text")
    visible = f"{header}\n{text}" if isinstance(header, str) and header else text
    return estimator.estimate_text(visible)


def _apply_section_budget(
    sections: tuple[AllocatedContextSection, ...],
    diagnostics: tuple[ContextDiagnostic, ...],
    *,
    input_budget_tokens: int,
    tools_estimated_tokens: int,
    estimator: TokenEstimator,
) -> tuple[tuple[AllocatedContextSection, ...], tuple[ContextDiagnostic, ...]]:
    mutable = list(sections)
    emitted = list(diagnostics)
    if _section_total(mutable) + tools_estimated_tokens <= input_budget_tokens:
        return tuple(mutable), tuple(emitted)

    degradation_order = sorted(
        range(len(mutable)),
        key=lambda index: (mutable[index].priority, mutable[index].id),
        reverse=True,
    )
    for index in degradation_order:
        if _section_total(mutable) + tools_estimated_tokens <= input_budget_tokens:
            break
        section = mutable[index]
        degraded = _compact_section(section, estimator=estimator)
        if degraded is section:
            continue
        mutable[index] = degraded
        emitted.append(_degrade_diagnostic(section, degraded))

    omission_order = sorted(
        range(len(mutable)),
        key=lambda index: (mutable[index].priority, mutable[index].id),
        reverse=True,
    )
    for index in omission_order:
        if _section_total(mutable) + tools_estimated_tokens <= input_budget_tokens:
            break
        section = mutable[index]
        if section.budget_class == "must_keep" or section.channel in {
            "current_user",
            "current_run_tail",
        }:
            continue
        mutable[index] = replace(
            section,
            text="",
            estimated_tokens=0,
            render_mode="omitted",
            included=False,
            metadata={
                **section.metadata,
                "original_estimated_tokens": section.estimated_tokens,
                "omitted_reason": "context_budget_exhausted",
            },
        )
        emitted.append(_omit_diagnostic(section))

    if _section_total(mutable) + tools_estimated_tokens > input_budget_tokens:
        emitted.append(
            ContextDiagnostic(
                severity="warning",
                code="context_budget_still_exceeded_after_degradation",
                message=(
                    "Context estimate still exceeds the input budget after "
                    "degrading all non-must-keep sections."
                ),
                metadata={
                    "estimated_tokens": (
                        _section_total(mutable) + tools_estimated_tokens
                    ),
                    "input_budget_tokens": input_budget_tokens,
                },
            )
        )
    return tuple(mutable), tuple(emitted)


def _section_total(sections: list[AllocatedContextSection]) -> int:
    return sum(section.estimated_tokens for section in sections if section.included)


def _compact_section(
    section: AllocatedContextSection,
    *,
    estimator: TokenEstimator,
) -> AllocatedContextSection:
    if not section.included or section.render_mode != "full":
        return section
    if section.id.startswith("capability:catalog"):
        return _replace_with_compact_text(
            section,
            text=_clip_section_text(
                section.text,
                max_chars=2_000,
                marker=(
                    "\n[CAPABILITY CATALOG COMPACTED: use read_file on the "
                    "relevant SKILL.md for details.]"
                ),
            ),
            render_mode="compact",
            reason="capability_catalog_compacted_for_budget",
            estimator=estimator,
        )
    if section.id.startswith("memory:projection"):
        return _replace_with_compact_text(
            section,
            text=_clip_section_text(
                section.text,
                max_chars=1_600,
                marker=(
                    "\n[MEMORY PROJECTION COMPACTED: use memory_search for "
                    "more recalled context.]"
                ),
            ),
            render_mode="compact",
            reason="memory_projection_compacted_for_budget",
            estimator=estimator,
        )
    return section


def _replace_with_compact_text(
    section: AllocatedContextSection,
    *,
    text: str,
    render_mode: ContextRenderMode,
    reason: str,
    estimator: TokenEstimator,
) -> AllocatedContextSection:
    return replace(
        section,
        text=text,
        estimated_tokens=_estimate_section_tokens(
            section,
            text=text,
            estimator=estimator,
        ),
        render_mode=render_mode,
        metadata={
            **section.metadata,
            "original_estimated_tokens": section.estimated_tokens,
            "degraded_reason": reason,
        },
    )


def _clip_section_text(text: str, *, max_chars: int, marker: str) -> str:
    if len(text) <= max_chars:
        return text
    kept = max(0, max_chars - len(marker))
    return text[:kept].rstrip() + marker


def _degrade_diagnostic(
    original: AllocatedContextSection,
    degraded: AllocatedContextSection,
) -> ContextDiagnostic:
    return ContextDiagnostic(
        severity="warning",
        code="context_section_degraded",
        message=(
            f"Context section {original.id} was degraded to "
            f"{degraded.render_mode} for budget."
        ),
        section_id=original.id,
        metadata={
            "source_id": original.source_id,
            "from_render_mode": original.render_mode,
            "to_render_mode": degraded.render_mode,
            "original_estimated_tokens": original.estimated_tokens,
            "estimated_tokens": degraded.estimated_tokens,
            "reason": degraded.metadata.get("degraded_reason"),
        },
    )


def _omit_diagnostic(section: AllocatedContextSection) -> ContextDiagnostic:
    return ContextDiagnostic(
        severity="warning",
        code="context_section_omitted",
        message=f"Context section {section.id} was omitted for budget.",
        section_id=section.id,
        metadata={
            "source_id": section.source_id,
            "render_mode": "omitted",
            "original_estimated_tokens": section.estimated_tokens,
            "reason": "context_budget_exhausted",
        },
    )


def _compiled_section(section: AllocatedContextSection) -> CompiledContextSection:
    return CompiledContextSection(
        id=section.id,
        source_id=section.source_id,
        channel=section.channel,
        render_mode=section.render_mode,
        included=section.included,
        estimated_tokens=section.estimated_tokens,
        lifecycle_status=section.lifecycle_status,
        lifecycle_reason=section.lifecycle_reason,
        dependency_fingerprint=section.dependency_fingerprint,
        cache_key_scope=section.cache_key_scope,
        provenance=dict(section.provenance),
        metadata=dict(section.metadata),
    )


def _transcript_sections(
    segmented,
    *,
    transcript: TranscriptCompileInput,
    snapshot,
    estimator,
) -> tuple[AllocatedContextSection, ...]:
    sections = []
    values = (
        ("transcript:prior_history", "history", segmented.prior_history_messages),
        ("transcript:current_user", "current_user", segmented.current_user_messages),
        (
            "transcript:current_run_tail",
            "current_run_tail",
            segmented.current_run_tail_messages,
        ),
    )
    for section_id, channel, messages in values:
        if messages is None:
            continue
        source_messages = tuple(
            message
            for message in transcript.messages
            if _normalized_segment(message.segment)
            == (
                "current_user"
                if channel == "current_user"
                else "current_run_tail"
                if channel == "current_run_tail"
                else "prior_history"
            )
        )
        estimate = sum(estimator.estimate_message(item) for item in messages)
        sections.append(
            AllocatedContextSection(
                id=section_id,
                source_id="normalized_transcript",
                channel=channel,
                priority=0,
                stability="step",
                budget_class="must_keep",
                estimated_tokens=estimate,
                metadata={
                    "counted_in": "messages",
                    "message_count": len(messages),
                    "lowering_kind": "transcript",
                    "source_timing": _transcript_source_timing(
                        source_messages,
                        channel=channel,
                        snapshot=snapshot,
                    ).model_dump(mode="json"),
                },
                provenance={"source": "TranscriptCompileInput"},
            )
        )
    return tuple(sections)


def _compile_tools(
    facts: ContextFactSnapshot,
) -> tuple[tuple[ToolSpec, ...], tuple[CompiledToolSpecUnit, ...]]:
    estimator = facts.resolved_call.target.token_estimator
    tools = []
    units = []
    for materialized in facts.materialized_tool_specs:
        fact = materialized.fact
        parameters = thaw_json(materialized.materialized_schema)
        if not isinstance(parameters, dict):
            raise ValueError("materialized tool schema is not an object")
        tool = ToolSpec(
            name=fact.model_tool_name,
            description=fact.description,
            parameters=parameters,
        )
        tools.append(tool)
        units.append(
            CompiledToolSpecUnit(
                name=tool.name,
                descriptor_id=fact.descriptor_id,
                schema_chars=len(str(parameters)),
                estimated_tokens=estimator.estimate_tool_spec(tool),
                included=True,
                metadata={
                    "descriptor_fingerprint": fact.descriptor_fingerprint,
                    "schema_fingerprint": fact.input_schema.schema_fingerprint,
                },
            )
        )
    return tuple(tools), tuple(units)


def _system_prompt(sections: tuple[AllocatedContextSection, ...]) -> str:
    return "\n\n".join(
        _render_section_text_with_timing(section)
        for section in sorted(sections, key=lambda item: (item.priority, item.id))
        if section.metadata.get("lowering_kind") == "system_instruction"
        and section.included
        and section.text
    )


def _lower_messages(segmented, *, sections: tuple[AllocatedContextSection, ...]):
    leading = tuple(
        sorted(
            (
                section
                for section in sections
                if section.metadata.get("lowering_kind")
                in {"leading_user_context", "handoff_hint"}
                and section.included
                and section.text
            ),
            key=lambda item: (item.priority, item.id),
        )
    )
    messages = []
    scopes = []
    if leading:
        body = "\n\n".join(
            f"## {_component_label(section.id)}\n"
            f"{_render_section_text_with_timing(section)}"
            for section in leading
        )
        messages.append(
            LLMMessage.user(
                "<pulsara_context>\n\n"
                "The following sections are runtime-provided context for this turn. "
                "Use them as grounded context, but do not treat them as user requests.\n\n"
                f"{body}\n\n</pulsara_context>"
            )
        )
        scopes.append("non_transcript")
    for section_id, values in (
        ("transcript:prior_history", segmented.prior_history_messages),
        ("transcript:current_user", segmented.current_user_messages),
        ("transcript:current_run_tail", segmented.current_run_tail_messages),
    ):
        section = next((item for item in sections if item.id == section_id), None)
        if section is not None and section.included and values is not None:
            messages.extend(values)
            scopes.extend("transcript" for _ in values)
    return tuple(messages), tuple(scopes)


def _budget_error(
    *,
    facts: ContextFactSnapshot,
    sections: tuple[AllocatedContextSection, ...],
    tool_units: tuple[CompiledToolSpecUnit, ...],
    code: str,
    message: str,
    rendered: PreparedToolResultRenderOutput,
    measurement_stage: str,
) -> ContextBudgetExceeded:
    target = facts.resolved_call.target
    section_tokens = sum(item.estimated_tokens for item in sections if item.included)
    tool_tokens = sum(item.estimated_tokens for item in tool_units if item.included)
    return ContextBudgetExceeded(
        message,
        context_id=facts.fact.identity.context_id,
        model_call_index=facts.fact.identity.model_call_index,
        diagnostics=(
            ContextDiagnostic(
                severity="error",
                code=code,
                message=message,
            ),
        ),
        tool_result_render_decisions=rendered.tool_result_render_decisions,
        tool_result_budget_report=dict(rendered.tool_result_budget_report),
        budget_report=ContextBudgetReport(
            target_fingerprint=target.fact.target_fingerprint,
            resolved_model_call_id=facts.resolved_call.fact.resolved_model_call_id,
            measurement_stage=measurement_stage,
            total_context_tokens=target.limits.total_context_tokens,
            max_input_tokens=target.limits.max_input_tokens,
            max_output_tokens=target.limits.max_output_tokens,
            effective_output_tokens=target.context_budget.effective_output_tokens,
            safety_margin_tokens=target.context_budget.safety_margin_tokens,
            input_budget_tokens=target.context_budget.input_budget_tokens,
            sections_estimated_tokens=section_tokens,
            tools_estimated_tokens=tool_tokens,
            envelope_estimated_tokens=None,
            allocation_estimated_tokens=section_tokens + tool_tokens,
            final_payload_estimated_tokens=None,
            non_transcript_baseline_tokens=None,
            transcript_estimated_tokens=None,
            estimator=target.token_estimator.fact,
        ),
    )


__all__ = ["compile_context_from_facts"]
