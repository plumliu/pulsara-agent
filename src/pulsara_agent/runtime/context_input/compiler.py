"""Pure compiler entrypoint for immutable context-input facts.

This module is the production C3 boundary.  It does not accept or import
``LoopState``/``Msg`` and performs no lifecycle-cache or storage I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from pulsara_agent.llm.estimator import TokenEstimator
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, ToolSpec
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.context import (
    ContextSectionCandidate,
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
from pulsara_agent.primitives.context_source import ContextSourceId
from pulsara_agent.primitives.long_horizon import PreparedObservationRollupUnit
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.tool_result import ToolResultRenderDecisionFact
from pulsara_agent.runtime.context_engine.types import (
    CompiledContext,
    CompiledContextSection,
    CompiledProviderSourceFragment,
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
from pulsara_agent.runtime.context_input.sources.render import (
    render_context_source_candidate,
)
from pulsara_agent.runtime.context_input.snapshot import ContextFactSnapshot
from pulsara_agent.runtime.context_input.snapshot import ContextFactSnapshotDraft
from pulsara_agent.runtime.context_input.provider_projection import (
    materialize_transcript_provider_projection,
    prepare_transcript_provider_projection,
)
from pulsara_agent.primitives.transcript_projection import (
    ModelVisibleNamedFactSemanticIdentityFact,
    ModelVisibleNamedFactSemanticSelectionFact,
    TranscriptProviderProjectionFact,
    TranscriptProjectionLeafEntryFact,
)
from pulsara_agent.runtime.provider_input.causal import (
    LoweredProviderTranscriptMessage,
    PreparedOrderedProviderTranscriptProjection,
    build_default_resolved_causal_physical_policy,
    build_ordered_provider_transcript_projection,
)
from pulsara_agent.primitives.provider_input import (
    ProviderOrderedTranscriptProjectionFact,
    ResolvedProviderInputCausalAndPhysicalPolicyFact,
)


@dataclass(frozen=True, slots=True)
class LoweredTranscriptMessages:
    full_messages: tuple[LLMMessage, ...]
    full_source_event_refs: tuple[tuple[ContextEventReferenceFact, ...], ...]
    full_sources: tuple[LoweredProviderTranscriptMessage, ...]
    prior_history_messages: tuple[LLMMessage, ...]
    prior_history_source_event_refs: tuple[tuple[ContextEventReferenceFact, ...], ...]
    current_user_messages: tuple[LLMMessage, ...]
    current_user_source_event_refs: tuple[tuple[ContextEventReferenceFact, ...], ...]
    current_run_tail_messages: tuple[LLMMessage, ...]
    current_run_tail_source_event_refs: tuple[
        tuple[ContextEventReferenceFact, ...], ...
    ]


def compile_context_from_facts(
    *,
    facts: ContextFactSnapshot | ContextFactSnapshotDraft,
    transcript: TranscriptCompileInput,
    rendered_tool_results: PreparedToolResultRenderOutput,
    prepared_rollups: tuple[PreparedObservationRollupUnit, ...],
    section_candidates: PreparedContextCandidateSet,
    transcript_provider_projection: TranscriptProviderProjectionFact | None = None,
    ordered_transcript_projection: ProviderOrderedTranscriptProjectionFact
    | None = None,
    transcript_stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...] = (),
    provider_causal_physical_policy: (
        ResolvedProviderInputCausalAndPhysicalPolicyFact | None
    ) = None,
    context_source_hydrated_contents: tuple[tuple[str, str], ...] = (),
) -> CompiledContext:
    """Lower one already-prepared immutable aggregate into ``LLMContext``."""

    snapshot = facts.fact
    identity = snapshot.identity
    call = facts.resolved_call
    estimator = call.target.token_estimator
    lowered_transcript = lower_transcript_for_context(
        transcript=transcript,
        rendered_tool_results=rendered_tool_results,
        prepared_rollups=prepared_rollups,
    )
    carrier = call.target.fact.runtime_observation_carrier
    if prepared_rollups and carrier is None:
        raise ValueError(
            "prepared observation rollups require a resolved runtime carrier"
        )
    if carrier is not None and any(
        item.compile_unit.carrier_contract_fingerprint != carrier.contract_fingerprint
        for item in prepared_rollups
    ):
        raise ValueError("prepared rollup carrier differs from resolved model target")
    if transcript.transcript_fingerprint == "":
        raise ValueError("compiler transcript fingerprint is required")
    if section_candidates.policy != snapshot.compile_policy.candidate_collection:
        raise ValueError("compiler candidate policy differs from snapshot")
    hydrated_source_contents = dict(context_source_hydrated_contents)
    for entry in section_candidates.entries:
        validate_candidate_against_snapshot(
            snapshot=snapshot,
            candidate=entry.candidate,
            hydrated_contents=hydrated_source_contents,
        )

    candidate_sections = sorted(
        (
            _candidate_section(
                entry.candidate,
                entry.lifecycle.status,
                estimator,
                hydrated_contents=hydrated_source_contents,
            )
            for entry in section_candidates.entries
        ),
        key=lambda section: (section.priority, section.id),
    )
    transcript_sections = _transcript_sections(
        lowered_transcript,
        transcript=transcript,
        snapshot=snapshot,
        estimator=estimator,
    )
    prepared_provider_projection = (
        prepare_transcript_provider_projection(
            snapshot=snapshot,
            transcript=transcript,
            prior_history_messages=lowered_transcript.prior_history_messages,
            current_user_messages=lowered_transcript.current_user_messages,
            current_run_tail_messages=lowered_transcript.current_run_tail_messages,
            chronological_messages=lowered_transcript.full_messages,
            sections=transcript_sections,
            estimator=estimator,
        )
        if transcript_provider_projection is None
        else materialize_transcript_provider_projection(
            snapshot=snapshot,
            projection_fact=transcript_provider_projection,
            transcript=transcript,
            prior_history_messages=lowered_transcript.prior_history_messages,
            current_user_messages=lowered_transcript.current_user_messages,
            current_run_tail_messages=lowered_transcript.current_run_tail_messages,
            chronological_messages=lowered_transcript.full_messages,
            sections=transcript_sections,
            estimator=estimator,
        )
    )
    causal_policy = (
        provider_causal_physical_policy
        or build_default_resolved_causal_physical_policy()
    )
    prepared_ordered_projection: PreparedOrderedProviderTranscriptProjection | None = (
        None
    )
    if transcript_stable_entries:
        prepared_ordered_projection = build_ordered_provider_transcript_projection(
            runtime_session_id=transcript.runtime_session_id,
            context_id=snapshot.identity.context_id,
            transcript=transcript,
            lowered_messages=lowered_transcript.full_sources,
            stable_entries=transcript_stable_entries,
            rendering_contract_fingerprint=(
                prepared_provider_projection.projection_fact.rendering_contract.contract_fingerprint
            ),
            policy=causal_policy,
        )
        if (
            ordered_transcript_projection is not None
            and ordered_transcript_projection
            != prepared_ordered_projection.projection
        ):
            raise ValueError("frozen ordered provider transcript projection drifted")
    elif ordered_transcript_projection is not None:
        raise ValueError(
            "ordered provider projection replay requires stable transcript entries"
        )
    sections = list(candidate_sections)
    sections.extend(prepared_provider_projection.rendered_transcript_sections)
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
    provider_source_fragments = _compiled_provider_source_fragments(
        sections=sections,
        candidates=section_candidates,
        estimator=estimator,
    )
    system_prompt = "\n\n".join(
        fragment.message.content[0]
        for fragment in provider_source_fragments
        if fragment.provider_lane == "system_prompt"
    )
    chronological_transcript_messages = (
        prepared_ordered_projection.lowered_messages
        if prepared_ordered_projection is not None
        else prepared_provider_projection.lowered_provider_messages
    )
    messages, scopes = _lower_compiled_provider_messages(
        transcript_messages=chronological_transcript_messages,
        source_fragments=provider_source_fragments,
    )
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
    named_fact_semantic_selection = _model_visible_named_fact_semantic_selection(
        sections=sections,
        candidates=section_candidates,
    )
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
        prepared_transcript_provider_projection=prepared_provider_projection,
        prepared_ordered_transcript_projection=prepared_ordered_projection,
        provider_causal_physical_policy=causal_policy,
        model_visible_named_fact_semantic_selection=(named_fact_semantic_selection),
        tool_result_render_decisions=(
            rendered_tool_results.tool_result_render_decisions
        ),
        tool_result_budget_report=dict(rendered_tool_results.tool_result_budget_report),
        tool_result_render_decision_facts=(rendered_tool_results.canonical_decisions),
        tool_result_render_operational_facts=(rendered_tool_results.operational_facts),
        provider_source_fragments=provider_source_fragments,
        transcript_source_event_refs_by_message=(
            lowered_transcript.full_source_event_refs
        ),
    )


def _model_visible_named_fact_semantic_selection(
    *,
    sections: tuple[AllocatedContextSection, ...],
    candidates: PreparedContextCandidateSet,
) -> ModelVisibleNamedFactSemanticSelectionFact:
    candidates_by_id = {
        entry.candidate.source_instance_id: entry.candidate
        for entry in candidates.entries
    }
    if len(candidates_by_id) != len(candidates.entries):
        raise ValueError("named context candidate source IDs are not unique")
    channel_order = {
        "system": 0,
        "leading_user": 1,
        "handoff_hint": 2,
        "tool_context": 3,
        "current_user": 4,
        "current_run_tail": 5,
        "history": 6,
    }
    selected = sorted(
        (
            section
            for section in sections
            if section.included
            and section.metadata.get("lowering_kind") != "transcript"
        ),
        key=lambda item: (
            channel_order.get(item.channel, 99),
            item.priority,
            item.id,
        ),
    )
    identities: list[ModelVisibleNamedFactSemanticIdentityFact] = []
    for section in selected:
        try:
            candidate = candidates_by_id[section.id]
        except KeyError as exc:
            raise ValueError(
                "included named context section has no prepared candidate"
            ) from exc
        rendered_text = _render_section_text_with_timing(section)
        payload_fingerprint = context_fingerprint(
            "model-visible-named-fact-payload:v1",
            {
                "candidate_semantic_fingerprint": candidate.semantic_fingerprint,
                "rendered_text": rendered_text,
                "render_mode": section.render_mode,
                "channel": section.channel,
                "lowering_kind": section.metadata.get("lowering_kind"),
            },
        )
        lowering_contract_fingerprint = context_fingerprint(
            "model-visible-named-fact-lowering-contract:v1",
            {
                "channel": section.channel,
                "lowering_kind": section.metadata.get("lowering_kind"),
                "envelope_contract": "pulsara-named-context-envelope:v1",
            },
        )
        identities.append(
            build_frozen_fact(
                ModelVisibleNamedFactSemanticIdentityFact,
                schema_version="model_visible_named_fact_semantic_identity.v1",
                source_kind=candidate.source_kind,
                semantic_key=candidate.source_instance_id,
                semantic_payload_fingerprint=payload_fingerprint,
                lowering_contract_fingerprint=lowering_contract_fingerprint,
            )
        )
    return build_frozen_fact(
        ModelVisibleNamedFactSemanticSelectionFact,
        schema_version="model_visible_named_fact_semantic_selection.v1",
        selection_contract_fingerprint=context_fingerprint(
            "model-visible-named-fact-selection-contract:v1",
            "provider-order+included-only+exact-rendered-body",
        ),
        selected_items=tuple(identities),
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
    prepared_rollups: tuple[PreparedObservationRollupUnit, ...],
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
    pairs_by_call = {
        (pair.result_message_id, pair.tool_call_id): pair
        for pair in transcript.tool_pairs
    }
    messages_by_id = {message.message_id: message for message in transcript.messages}
    anchored_rollups: dict[str, list[PreparedObservationRollupUnit]] = {}
    seen_rollup_ids: set[str] = set()
    for prepared in prepared_rollups:
        rollup = prepared.rollup
        if rollup.rollup_id in seen_rollup_ids:
            raise ValueError("prepared rollup IDs are not unique")
        seen_rollup_ids.add(rollup.rollup_id)
        anchor = prepared.compile_unit.placement_anchor
        message = messages_by_id.get(anchor.insert_after_transcript_message_id)
        if message is None:
            raise ValueError("prepared rollup anchor message is absent")
        if message.source_sequence_end != anchor.insert_after_source_sequence:
            raise ValueError("prepared rollup anchor sequence differs from transcript")
        member_ids = tuple(member.unit_id for member in rollup.member_facts)
        if member_ids != prepared.ordered_member_unit_ids or any(
            member_id not in fragments for member_id in member_ids
        ):
            raise ValueError("prepared rollup members differ from rendered results")
        anchored_rollups.setdefault(message.message_id, []).append(prepared)

    full: list[LLMMessage] = []
    full_refs: list[tuple[ContextEventReferenceFact, ...]] = []
    full_sources: list[LoweredProviderTranscriptMessage] = []
    prior: list[LLMMessage] = []
    prior_refs: list[tuple[ContextEventReferenceFact, ...]] = []
    current: list[LLMMessage] = []
    current_refs: list[tuple[ContextEventReferenceFact, ...]] = []
    tail: list[LLMMessage] = []
    tail_refs: list[tuple[ContextEventReferenceFact, ...]] = []
    source_refs_by_event_id = {
        ref.event_id: ref
        for source_message in transcript.messages
        for block in source_message.blocks
        for ref in getattr(block, "source_events", ())
    }
    for message_index, message in enumerate(transcript.messages):
        segment = _normalized_segment(message.segment)
        lowered_message_parts = _lower_transcript_message(
            message=message,
            message_index=message_index,
            segment=segment,
            fragments=fragments,
            pairs_by_call=pairs_by_call,
        )
        message_refs = _ordered_transcript_source_refs(
            ref
            for block in message.blocks
            for ref in getattr(block, "source_events", ())
        )
        ordered_rollups = tuple(
            sorted(
                anchored_rollups.get(message.message_id, ()),
                key=lambda item: (
                    item.rollup.member_facts[-1].result_sequence,
                    item.rollup.rollup_id,
                ),
            )
        )
        rollup_messages = tuple(
            LLMMessage.runtime_observation(item.compile_unit.inline_text)
            for item in ordered_rollups
        )
        rollup_refs = tuple(
            _ordered_transcript_source_refs(
                _rollup_member_source_ref(
                    event_id=member.result_event_id,
                    source_refs_by_event_id=source_refs_by_event_id,
                )
                for member in item.rollup.member_facts
            )
            for item in ordered_rollups
        )
        lowered = (*lowered_message_parts, *rollup_messages)
        lowered_refs = (
            *(message_refs for _ in lowered_message_parts),
            *rollup_refs,
        )
        if len(lowered) != len(lowered_refs):
            raise ValueError("lowered transcript source attribution drifted")
        full.extend(lowered)
        full_refs.extend(lowered_refs)
        full_sources.extend(
            LoweredProviderTranscriptMessage(
                message=provider_message,
                source_message=message,
                source_event_refs=source_refs,
                lowered_part_index=index,
                source_kind=(
                    "canonical_message"
                    if index < len(lowered_message_parts)
                    else "rollup_observation"
                ),
            )
            for index, (provider_message, source_refs) in enumerate(
                zip(lowered, lowered_refs, strict=True)
            )
        )
        if segment == "current_user":
            current.extend(lowered)
            current_refs.extend(lowered_refs)
        elif segment == "current_run_tail":
            tail.extend(lowered)
            tail_refs.extend(lowered_refs)
        else:
            prior.extend(lowered)
            prior_refs.extend(lowered_refs)
    return LoweredTranscriptMessages(
        full_messages=tuple(full),
        full_source_event_refs=tuple(full_refs),
        full_sources=tuple(full_sources),
        prior_history_messages=tuple(prior),
        prior_history_source_event_refs=tuple(prior_refs),
        current_user_messages=tuple(current),
        current_user_source_event_refs=tuple(current_refs),
        current_run_tail_messages=tuple(tail),
        current_run_tail_source_event_refs=tuple(tail_refs),
    )


def _ordered_transcript_source_refs(
    refs,
) -> tuple[ContextEventReferenceFact, ...]:
    by_identity = {
        (ref.runtime_session_id, ref.sequence, ref.event_id): ref for ref in refs
    }
    return tuple(by_identity[key] for key in sorted(by_identity))


def _rollup_member_source_ref(
    *,
    event_id: str,
    source_refs_by_event_id: dict[str, ContextEventReferenceFact],
) -> ContextEventReferenceFact:
    try:
        return source_refs_by_event_id[event_id]
    except KeyError as exc:
        raise ValueError("rollup member lacks exact transcript source ref") from exc


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
            pair = pairs_by_call.get((message.message_id, block.tool_call_id))
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
    candidate,
    lifecycle_status: str,
    estimator,
    *,
    hydrated_contents: dict[str, str],
) -> AllocatedContextSection:
    text = render_context_source_candidate(
        candidate,
        hydrated_contents=hydrated_contents,
    )
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
            "source_absolute_timing": (
                candidate.attribution.source_absolute_timing.model_dump(mode="json")
                if candidate.attribution.source_absolute_timing is not None
                else None
            ),
            "lowering_kind": candidate.lowering_kind,
            "source_revision_fingerprint": (
                candidate.attribution.semantic.source_revision.revision_fingerprint
            ),
            "source_contract_fingerprint": (
                candidate.attribution.source_contract_fingerprint
            ),
        },
        lifecycle_status=lifecycle_status,
        lifecycle_reason="prepared_candidate_set",
        dependency_fingerprint=candidate.lifecycle_dependency_fingerprint,
    )


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
    facts: ContextFactSnapshot | ContextFactSnapshotDraft,
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


def _compiled_provider_source_fragments(
    *,
    sections: tuple[AllocatedContextSection, ...],
    candidates: PreparedContextCandidateSet,
    estimator: TokenEstimator,
) -> tuple[CompiledProviderSourceFragment, ...]:
    candidates_by_id = {
        entry.candidate.source_instance_id: entry.candidate
        for entry in candidates.entries
    }
    fragments = []
    for section in sorted(sections, key=lambda item: (item.priority, item.id)):
        if (
            section.metadata.get("lowering_kind") == "transcript"
            or not section.included
            or not section.text
        ):
            continue
        candidate = candidates_by_id.get(section.id)
        if candidate is None:
            raise ValueError("compiled provider section lacks ContextSource owner")
        rendered = _render_section_text_with_timing(section)
        provider_lane, message = _lower_context_source_provider_message(
            candidate,
            rendered,
        )
        fragments.append(
            CompiledProviderSourceFragment(
                candidate=candidate,
                render_mode=section.render_mode,
                provider_lane=provider_lane,
                message=message,
                estimated_tokens=estimator.estimate_message(message),
            )
        )
    return tuple(fragments)


def _lower_context_source_provider_message(
    candidate: ContextSectionCandidate,
    rendered: str,
) -> tuple[str, LLMMessage]:
    """Lower one accepted source fragment at the compiler ownership boundary."""

    intent = candidate.attribution.semantic.lowering_intent
    if intent.intent_kind == "system_instruction":
        if intent.role_constraint != "system":
            raise ValueError("system ContextSource has an invalid provider role")
        return "system_prompt", LLMMessage.system(rendered)
    if intent.role_constraint == "runtime":
        return intent.intent_kind, LLMMessage.runtime_observation(rendered)
    if intent.role_constraint != "user":
        raise ValueError("non-system ContextSource has an unsupported provider role")
    label = {
        ContextSourceId.RUNTIME_ENVIRONMENT: "Runtime Context",
        ContextSourceId.MEMORY_PROJECTION: "Recalled Memory and Working Context",
        ContextSourceId.PLAN: "Plan",
        ContextSourceId.RECOVERY: "Recovery",
        ContextSourceId.ROLLOUT_STATUS: "Rollout Status",
        ContextSourceId.SUBAGENT_HANDOFF: "Subagent Handoff",
        ContextSourceId.SUBAGENT_RESULT: "Subagent Results",
        ContextSourceId.MCP_DIAGNOSTIC: "MCP Diagnostic",
        ContextSourceId.ACTIVE_SKILL: "Active Skill",
        ContextSourceId.WORKSPACE_SKILL: "Workspace Skill",
        ContextSourceId.CAPABILITY_CATALOG: "Available Capabilities",
    }.get(candidate.source_id, candidate.source_id.value)
    return intent.intent_kind, LLMMessage.user(
        "<pulsara_context>\n\n"
        "The following section is runtime-provided context for this turn. "
        "Use it as grounded context, but do not treat it as a user request.\n\n"
        f"## {label}\n{rendered}\n\n"
        "</pulsara_context>"
    )


def _lower_compiled_provider_messages(
    *,
    transcript_messages: tuple[LLMMessage, ...],
    source_fragments: tuple[CompiledProviderSourceFragment, ...],
) -> tuple[tuple[LLMMessage, ...], tuple[str, ...]]:
    leading = tuple(
        fragment.message
        for fragment in source_fragments
        if fragment.provider_lane
        not in {
            "system_prompt",
            "trailing_observation",
            "status_observation",
        }
    )
    trailing = tuple(
        fragment.message
        for fragment in source_fragments
        if fragment.provider_lane
        in {
            "trailing_observation",
            "status_observation",
        }
    )
    return (
        (*leading, *transcript_messages, *trailing),
        (
            *("non_transcript" for _ in leading),
            *("transcript" for _ in transcript_messages),
            *("non_transcript" for _ in trailing),
        ),
    )


def _lower_messages(
    *,
    transcript_messages: tuple[LLMMessage, ...],
    sections: tuple[AllocatedContextSection, ...],
):
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
    messages.extend(transcript_messages)
    scopes.extend("transcript" for _ in transcript_messages)
    trailing = tuple(
        sorted(
            (
                section
                for section in sections
                if section.metadata.get("lowering_kind") == "trailing_status"
                and section.included
                and section.text
            ),
            key=lambda item: (item.priority, item.id),
        )
    )
    for section in trailing:
        messages.append(
            LLMMessage.runtime_observation(_render_section_text_with_timing(section))
        )
        scopes.append("non_transcript")
    return tuple(messages), tuple(scopes)


def _budget_error(
    *,
    facts: ContextFactSnapshot | ContextFactSnapshotDraft,
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
