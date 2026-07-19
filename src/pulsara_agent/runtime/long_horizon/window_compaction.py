"""Pairing-safe source preparation for same-run context-window compaction."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    ContextSourceTimingFact,
    TranscriptCompileInput,
    TranscriptDataPlaceholderFact,
    TranscriptTextBlockFact,
    TranscriptThinkingBlockFact,
    TranscriptToolCallFact,
    TranscriptToolResultRefFact,
    WindowCompactionPairGroupFact,
    WindowCompactionSourceDocumentFact,
    WindowCompactionSourceEntryFact,
    WindowCompactionSummaryFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.long_horizon import (
    ContextWindowCompactionPlanFact,
    ContextWindowFact,
    ContextWindowOpenReason,
    ContextWindowProjectionState,
    ContextWindowTranscriptBasisFact,
    PreparedObservationRollupUnit,
    ToolObservationProtectionFact,
    RolloutReservationFact,
)
from pulsara_agent.primitives.model_call import ResolvedModelCallFact
from pulsara_agent.primitives.tool_result import ToolResultRenderUnit
from pulsara_agent.runtime.context_input.render import PreparedToolResultRenderOutput
from pulsara_agent.runtime.context_input.window_baseline import (
    build_window_compaction_transcript_baseline,
)
from pulsara_agent.runtime.long_horizon.run_contract import (
    empty_projection_state_fingerprint,
)


@dataclass(frozen=True, slots=True)
class PreparedWindowCompactionSourceDocument:
    fact: WindowCompactionSourceDocumentFact
    canonical_json: str
    protected_unit_ids: tuple[str, ...]
    summarized_unit_ids: tuple[str, ...]
    retained_unit_ids: tuple[str, ...]


def window_compaction_identity(
    *,
    run_id: str,
    source_window: ContextWindowFact,
    source_projection: ContextWindowProjectionState,
    source_through_sequence: int,
    attempt_index: int,
) -> tuple[str, str]:
    payload = {
        "run_id": run_id,
        "source_window_id": source_window.window_id,
        "source_window_generation": source_window.generation,
        "source_projection_generation": source_projection.projection_generation,
        "source_through_sequence": source_through_sequence,
        "attempt_index": attempt_index,
    }
    digest = context_fingerprint(
        "context-window-compaction-identity:v1", payload
    ).removeprefix("sha256:")
    return f"window_compaction:{digest}", digest


def parse_window_compaction_summary(
    raw_text: str,
    *,
    source: WindowCompactionSourceDocumentFact,
) -> WindowCompactionSummaryFact:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            raise ValueError("window compaction summary has an unclosed code fence")
        text = "\n".join(lines[1:-1]).strip()
        if text.startswith("json\n"):
            text = text[5:].lstrip()
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("window compaction summary is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("window compaction summary must be a JSON object")
    expected_fields = {
        "observed_facts",
        "model_inferences",
        "unresolved_questions",
        "critical_constraints",
        "artifact_locators",
        "cited_source_entry_ids",
    }
    if set(parsed) != expected_fields:
        raise ValueError("window compaction summary fields do not match the contract")
    payload: dict[str, tuple[str, ...]] = {}
    for field_name in sorted(expected_fields):
        value = parsed[field_name]
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError(
                f"window compaction summary {field_name} must be non-empty strings"
            )
        payload[field_name] = tuple(item.strip() for item in value)
    source_entry_ids = set(source.summarized_entry_ids)
    citations = set(payload["cited_source_entry_ids"])
    if not citations or not citations <= source_entry_ids:
        raise ValueError("window compaction summary cites an unknown source entry")
    source_artifact_ids = {
        artifact_id
        for entry in source.entries
        if entry.source_entry_id in source_entry_ids
        for artifact_id in entry.source_artifact_refs
    }
    if not set(payload["artifact_locators"]) <= source_artifact_ids:
        raise ValueError("window compaction summary invents an artifact locator")
    fingerprint_payload = {
        "schema_version": "window-compaction-summary.v1",
        **payload,
    }
    return WindowCompactionSummaryFact(
        **payload,
        summary_fingerprint=context_fingerprint(
            "window-compaction-summary:v1", fingerprint_payload
        ),
    )


def build_compacted_context_window(
    *,
    plan: ContextWindowCompactionPlanFact,
    source_window: ContextWindowFact,
    summary_artifact_id: str,
    summary: WindowCompactionSummaryFact,
) -> ContextWindowFact:
    if (
        plan.source_window_id != source_window.window_id
        or plan.source_window_generation != source_window.generation
    ):
        raise ValueError("target window source identity mismatch")
    summarized_groups_fingerprint = context_fingerprint(
        "window-compaction-summarized-pair-groups:v1",
        plan.summarized_pair_group_ids,
    )
    retained_groups_fingerprint = context_fingerprint(
        "window-compaction-retained-pair-groups:v1",
        plan.retained_pair_group_ids,
    )
    basis_payload = {
        "basis_kind": "window_compaction",
        "run_start_event_id": source_window.transcript_basis.run_start_event_id,
        "source_compaction_started_event_id": plan.stable_started_event_id,
        "source_compaction_plan_fingerprint": plan.plan_fingerprint,
        "source_through_sequence_at_compaction": plan.source_through_sequence,
        "summarized_pair_groups_fingerprint": summarized_groups_fingerprint,
        "retained_pair_groups_fingerprint": retained_groups_fingerprint,
    }
    basis = ContextWindowTranscriptBasisFact(
        **basis_payload,
        basis_fingerprint=context_fingerprint(
            "context-window-transcript-basis:v1", basis_payload
        ),
    )
    window_payload = {
        "contract_version": "context-window:v1",
        "window_id": plan.target_window_id,
        "run_id": plan.run_id,
        "generation": plan.target_window_generation,
        "previous_window_id": source_window.window_id,
        "open_reason": ContextWindowOpenReason.LLM_COMPACTION,
        "transcript_basis": basis,
        "source_through_sequence_at_open": plan.source_through_sequence,
        "resolved_model_target_fingerprint": (
            source_window.resolved_model_target_fingerprint
        ),
        "input_budget_tokens": source_window.input_budget_tokens,
        "token_estimator_fingerprint": source_window.token_estimator_fingerprint,
        "window_policy_fingerprint": source_window.window_policy_fingerprint,
        "initial_projection_generation": 0,
        "initial_projection_unit_count": 0,
        "initial_projection_state_fingerprint": empty_projection_state_fingerprint(),
        "stable_close_event_id": plan.stable_target_window_close_event_id,
        "source_compaction_id": plan.compaction_id,
        "source_summary_artifact_id": summary_artifact_id,
        "source_summary_fingerprint": summary.summary_fingerprint,
    }
    semantic_payload = {
        key: value
        for key, value in window_payload.items()
        if key not in {"window_id", "stable_close_event_id"}
    }
    semantic_fingerprint = context_fingerprint(
        "context-window-semantic:v1", semantic_payload
    )
    fact_payload = {
        **window_payload,
        "window_semantic_fingerprint": semantic_fingerprint,
    }
    return ContextWindowFact(
        **fact_payload,
        window_fact_fingerprint=context_fingerprint(
            "context-window-fact:v1", fact_payload
        ),
    )


def build_window_compaction_plan(
    *,
    compaction_id: str,
    compaction_attempt_index: int,
    run_id: str,
    source_window: ContextWindowFact,
    source_projection: ContextWindowProjectionState,
    source: PreparedWindowCompactionSourceDocument,
    source_context_fingerprint: str,
    summarizer_call: ResolvedModelCallFact,
    rollout_reservation: RolloutReservationFact,
    summarizer_input_manifest_artifact_id: str,
    summarizer_input_manifest_fingerprint: str,
    source_document_artifact_id: str,
    estimated_tokens_before: int,
    fixed_new_window_tokens: int,
    protected_tail_tokens: int,
    summarizer_input_estimated_tokens: int,
    post_compaction_target_tokens: int,
) -> ContextWindowCompactionPlanFact:
    if source.fact.compaction_id != compaction_id or source.fact.run_id != run_id:
        raise ValueError("window compaction source/plan identity mismatch")
    if source.fact.source_window_id != source_window.window_id:
        raise ValueError("window compaction source window drift")
    if (
        source.fact.source_projection_generation
        != source_projection.projection_generation
    ):
        raise ValueError("window compaction source projection drift")
    digest = context_fingerprint(
        "context-window-compaction-stable-ids:v1",
        {
            "compaction_id": compaction_id,
            "attempt_index": compaction_attempt_index,
            "source_window_id": source_window.window_id,
            "source_projection_generation": source_projection.projection_generation,
            "source_through_sequence": source.fact.source_through_sequence,
        },
    ).removeprefix("sha256:")
    target_window_id = f"context_window:{digest}"
    payload = {
        "compaction_id": compaction_id,
        "compaction_attempt_index": compaction_attempt_index,
        "run_id": run_id,
        "source_window_id": source_window.window_id,
        "source_window_generation": source_window.generation,
        "source_projection_generation": source_projection.projection_generation,
        "source_projection_state_fingerprint": (
            source_projection.state_semantic_fingerprint
        ),
        "source_through_sequence": source.fact.source_through_sequence,
        "target_window_id": target_window_id,
        "target_window_generation": source_window.generation + 1,
        "source_context_fingerprint": source_context_fingerprint,
        "summarizer_call": summarizer_call,
        "rollout_reservation": rollout_reservation,
        "summarizer_input_manifest_artifact_id": (
            summarizer_input_manifest_artifact_id
        ),
        "summarizer_input_manifest_fingerprint": (
            summarizer_input_manifest_fingerprint
        ),
        "source_document_artifact_id": source_document_artifact_id,
        "source_document_fingerprint": source.fact.document_fingerprint,
        "protected_unit_ids": source.protected_unit_ids,
        "summarized_unit_ids": source.summarized_unit_ids,
        "retained_tail_unit_ids": source.retained_unit_ids,
        "summarized_message_ids": source.fact.summarized_message_ids,
        "retained_message_ids": source.fact.retained_message_ids,
        "summarized_pair_group_ids": source.fact.summarized_pair_group_ids,
        "retained_pair_group_ids": source.fact.retained_pair_group_ids,
        "estimated_tokens_before": estimated_tokens_before,
        "fixed_new_window_tokens": fixed_new_window_tokens,
        "protected_tail_tokens": protected_tail_tokens,
        "summarizer_input_estimated_tokens": summarizer_input_estimated_tokens,
        "summary_output_budget_tokens": (
            summarizer_call.target.context_budget.effective_output_tokens
        ),
        "post_compaction_target_tokens": post_compaction_target_tokens,
        "stable_started_event_id": f"window-compaction-started:{digest}",
        "stable_completed_event_id": f"window-compaction-completed:{digest}",
        "stable_failed_event_id": f"window-compaction-failed:{digest}",
        "stable_source_window_close_event_id": source_window.stable_close_event_id,
        "stable_target_window_open_event_id": f"window-open:{digest}",
        "stable_target_window_close_event_id": f"window-close:{digest}",
    }
    return ContextWindowCompactionPlanFact(
        **payload,
        plan_fingerprint=context_fingerprint(
            "context-window-compaction-plan:v1",
            {"schema_version": "context-window-compaction-plan.v1", **payload},
        ),
    )


def prepare_window_compaction_source_document(
    *,
    compaction_id: str,
    run_id: str,
    window: ContextWindowFact,
    projection_state: ContextWindowProjectionState,
    transcript: TranscriptCompileInput,
    units: tuple[ToolResultRenderUnit, ...],
    rendered: PreparedToolResultRenderOutput,
    prepared_rollups: tuple[PreparedObservationRollupUnit, ...],
    protection_facts: tuple[ToolObservationProtectionFact, ...],
    source_through_sequence: int,
) -> PreparedWindowCompactionSourceDocument:
    """Build one immutable source document without splitting provider tool groups."""

    if window.run_id != run_id:
        raise ValueError("window compaction source run/window mismatch")
    if projection_state.window_id != window.window_id:
        raise ValueError("window compaction source projection/window mismatch")
    if source_through_sequence != transcript.through_sequence:
        raise ValueError("window compaction source high-water differs from transcript")
    if projection_state.through_sequence > source_through_sequence:
        raise ValueError("window compaction projection exceeds source high-water")
    unit_by_id = {unit.unit_id: unit for unit in units}
    fragment_by_id = {fragment.unit_id: fragment for fragment in rendered.fragments}
    protection_by_id = {fact.unit_id: fact for fact in protection_facts}
    if (
        len(unit_by_id) != len(units)
        or set(fragment_by_id) != set(unit_by_id)
        or set(protection_by_id) != set(unit_by_id)
    ):
        raise ValueError("window compaction unit/render/protection identity mismatch")

    entries: list[WindowCompactionSourceEntryFact] = []
    entry_ids_by_message: dict[str, list[str]] = {}
    result_entry_id_by_unit: dict[str, str] = {}
    tool_call_entry_id_by_call: dict[tuple[str, str], str] = {}
    message_by_id = {message.message_id: message for message in transcript.messages}

    for message in transcript.messages:
        message_entry_ids = entry_ids_by_message.setdefault(message.message_id, [])
        timing = _message_timing(message)
        for block_index, block in enumerate(message.blocks):
            if isinstance(block, TranscriptToolResultRefFact):
                unit = unit_by_id.get(block.tool_result_unit_id)
                fragment = fragment_by_id.get(block.tool_result_unit_id)
                if unit is None or fragment is None or unit.tool_call_id != block.tool_call_id:
                    raise ValueError("window compaction result ref is not normalized")
                entry = _source_entry(
                    source_entry_id=_source_entry_id(
                        window.window_id, message.message_id, block_index
                    ),
                    source_kind="tool_result_projection",
                    source_event_refs=block.source_events,
                    source_artifact_refs=tuple(
                        sorted({artifact.artifact_id for artifact in unit.artifacts})
                    ),
                    source_message_id=message.message_id,
                    source_block_index=block_index,
                    model_visible_text=fragment.text,
                    timing=timing,
                )
                result_entry_id_by_unit[unit.unit_id] = entry.source_entry_id
            elif isinstance(block, TranscriptToolCallFact):
                entry = _source_entry(
                    source_entry_id=_source_entry_id(
                        window.window_id, message.message_id, block_index
                    ),
                    source_kind="tool_call",
                    source_event_refs=block.source_events,
                    source_artifact_refs=(),
                    source_message_id=message.message_id,
                    source_block_index=block_index,
                    model_visible_text=(
                        f"tool_call {block.model_tool_name} "
                        f"arguments={block.raw_arguments_json} state={block.state}"
                    ),
                    timing=timing,
                )
                tool_call_entry_id_by_call[
                    (message.message_id, block.tool_call_id)
                ] = entry.source_entry_id
            elif isinstance(block, TranscriptTextBlockFact):
                entry = _source_entry(
                    source_entry_id=_source_entry_id(
                        window.window_id, message.message_id, block_index
                    ),
                    source_kind=(
                        "user_message" if message.role == "user" else "assistant_text"
                    ),
                    source_event_refs=block.source_events,
                    source_artifact_refs=(),
                    source_message_id=message.message_id,
                    source_block_index=block_index,
                    model_visible_text=block.text,
                    timing=timing,
                )
            elif isinstance(block, TranscriptThinkingBlockFact):
                entry = _source_entry(
                    source_entry_id=_source_entry_id(
                        window.window_id, message.message_id, block_index
                    ),
                    source_kind="assistant_text",
                    source_event_refs=block.source_events,
                    source_artifact_refs=(),
                    source_message_id=message.message_id,
                    source_block_index=block_index,
                    model_visible_text=block.thinking,
                    timing=timing,
                )
            elif isinstance(block, TranscriptDataPlaceholderFact):
                entry = _source_entry(
                    source_entry_id=_source_entry_id(
                        window.window_id, message.message_id, block_index
                    ),
                    source_kind=(
                        "user_message" if message.role == "user" else "assistant_text"
                    ),
                    source_event_refs=block.source_events,
                    source_artifact_refs=tuple(sorted(set(block.artifact_ids))),
                    source_message_id=message.message_id,
                    source_block_index=block_index,
                    model_visible_text=(
                        f"data_placeholder media_type={block.media_type} "
                        f"artifacts={','.join(block.artifact_ids)}"
                    ),
                    timing=timing,
                )
            else:  # pragma: no cover - closed union guard
                raise TypeError("unsupported window compaction transcript block")
            entries.append(entry)
            message_entry_ids.append(entry.source_entry_id)

    pair_groups: list[WindowCompactionPairGroupFact] = []
    pairs_by_message: dict[str, list] = {}
    for pair in transcript.tool_pairs:
        pairs_by_message.setdefault(pair.call_message_id, []).append(pair)
    for assistant_message_id, group_pairs in sorted(
        pairs_by_message.items(),
        key=lambda item: min(pair.call_sequence for pair in item[1]),
    ):
        ordered_pairs = tuple(sorted(group_pairs, key=lambda pair: pair.call_block_index))
        assistant = message_by_id.get(assistant_message_id)
        if assistant is None or assistant.role != "assistant":
            raise ValueError("window compaction pair group lacks assistant message")
        tool_call_ids = tuple(pair.tool_call_id for pair in ordered_pairs)
        result_unit_ids: list[str] = []
        source_entry_ids = list(entry_ids_by_message[assistant_message_id])
        protection_classes: set[str] = set()
        source_through = assistant.source_sequence_end
        for pair in ordered_pairs:
            unit = next(
                (
                    item
                    for item in units
                    if item.tool_call_id == pair.tool_call_id
                    and item.result_message_id == pair.result_message_id
                ),
                None,
            )
            if unit is None:
                raise ValueError("window compaction pair group lacks result unit")
            result_unit_ids.append(unit.unit_id)
            protection_classes.update(protection_by_id[unit.unit_id].classes)
            call_entry_id = tool_call_entry_id_by_call.get(
                (pair.call_message_id, pair.tool_call_id)
            )
            result_entry_id = result_entry_id_by_unit.get(unit.unit_id)
            if call_entry_id is None or result_entry_id is None:
                raise ValueError("window compaction pair group lacks source entries")
            if call_entry_id not in source_entry_ids:
                raise ValueError("tool call entry escaped its assistant message")
            source_entry_ids.extend(
                entry_ids_by_message.get(pair.result_message_id, ())
            )
            source_through = max(source_through, pair.result_sequence)
        group_payload = {
            "group_id": _pair_group_id(window.window_id, assistant_message_id),
            "assistant_message_id": assistant_message_id,
            "tool_call_ids": tool_call_ids,
            "result_unit_ids": tuple(result_unit_ids),
            "source_sequence_from": min(pair.call_sequence for pair in ordered_pairs),
            "source_sequence_through": source_through,
            "protection_classes": tuple(sorted(protection_classes)),
            "source_entry_ids": tuple(dict.fromkeys(source_entry_ids)),
        }
        pair_groups.append(
            WindowCompactionPairGroupFact(
                **group_payload,
                group_fingerprint=context_fingerprint(
                    "window-compaction-pair-group:v1", group_payload
                ),
            )
        )

    first_retained_group = next(
        (
            index
            for index, group in enumerate(pair_groups)
            if group.protection_classes
        ),
        len(pair_groups),
    )
    summarized_groups = tuple(pair_groups[:first_retained_group])
    retained_groups = tuple(pair_groups[first_retained_group:])
    grouped_entry_ids = {
        entry_id for group in pair_groups for entry_id in group.source_entry_ids
    }
    summarized_entry_ids = {
        entry_id for group in summarized_groups for entry_id in group.source_entry_ids
    }
    retained_entry_ids = {
        entry_id for group in retained_groups for entry_id in group.source_entry_ids
    }
    for message in transcript.messages:
        for entry_id in entry_ids_by_message[message.message_id]:
            if entry_id in grouped_entry_ids:
                continue
            if message.segment in {"current_user", "current_run_tail"}:
                retained_entry_ids.add(entry_id)
            else:
                summarized_entry_ids.add(entry_id)

    for rollup in prepared_rollups:
        member_units = tuple(unit_by_id[item] for item in rollup.ordered_member_unit_ids)
        refs = tuple(
            sorted(
                {
                    ref.event_id: ref
                    for unit in member_units
                    for ref in _unit_source_refs(unit, transcript)
                }.values(),
                key=lambda ref: ref.sequence,
            )
        )
        entry = _source_entry(
            source_entry_id=f"window-rollup-source:{rollup.rollup.rollup_id}",
            source_kind="observation_rollup",
            source_event_refs=refs,
            source_artifact_refs=(rollup.artifact_id,),
            source_message_id=None,
            source_block_index=None,
            model_visible_text=rollup.compile_unit.inline_text,
            timing=None,
        )
        entries.append(entry)
        if all(unit.unit_id in _group_unit_ids(summarized_groups) for unit in member_units):
            summarized_entry_ids.add(entry.source_entry_id)
        else:
            retained_entry_ids.add(entry.source_entry_id)

    ordered_entry_ids = tuple(entry.source_entry_id for entry in entries)
    summarized_message_ids: list[str] = []
    retained_message_ids: list[str] = []
    for message in transcript.messages:
        message_entry_ids = set(entry_ids_by_message[message.message_id])
        if not message_entry_ids:
            raise ValueError("window compaction message has no source entries")
        if message_entry_ids <= summarized_entry_ids:
            summarized_message_ids.append(message.message_id)
        elif message_entry_ids <= retained_entry_ids:
            retained_message_ids.append(message.message_id)
        else:
            raise ValueError("window compaction split one transcript message")
    if transcript.current_user_anchor not in retained_message_ids:
        raise ValueError("window compaction must retain the current user message")
    retained_transcript_baseline = freeze_json(
        build_window_compaction_transcript_baseline(
            compaction_id=compaction_id,
            run_id=run_id,
            source_window_id=window.window_id,
            transcript=transcript,
            units=units,
            retained_message_ids=tuple(retained_message_ids),
        ).model_dump(mode="json")
    )
    document_payload = {
        "compaction_id": compaction_id,
        "run_id": run_id,
        "source_window_id": window.window_id,
        "source_projection_generation": projection_state.projection_generation,
        "source_through_sequence": source_through_sequence,
        "entries": tuple(entries),
        "pair_groups": tuple(pair_groups),
        "summarized_entry_ids": tuple(
            entry_id for entry_id in ordered_entry_ids if entry_id in summarized_entry_ids
        ),
        "retained_entry_ids": tuple(
            entry_id for entry_id in ordered_entry_ids if entry_id in retained_entry_ids
        ),
        "summarized_message_ids": tuple(summarized_message_ids),
        "retained_message_ids": tuple(retained_message_ids),
        "summarized_pair_group_ids": tuple(
            group.group_id for group in summarized_groups
        ),
        "retained_pair_group_ids": tuple(group.group_id for group in retained_groups),
        "retained_transcript_baseline": retained_transcript_baseline,
    }
    fingerprint_payload = {
        **document_payload,
        # Match the model validator's JSON representation. Frozen JSON normally
        # canonicalizes to its thawed value, while model_dump preserves the typed
        # carrier shape used by this enclosing fact's fingerprint contract.
        "retained_transcript_baseline": retained_transcript_baseline.model_dump(
            mode="json"
        ),
    }
    fact = WindowCompactionSourceDocumentFact(
        **document_payload,
        document_fingerprint=context_fingerprint(
            "window-compaction-source-document:v1",
            {
                "schema_version": "window-compaction-source-document.v1",
                **fingerprint_payload,
            },
        ),
    )
    serialized = canonical_json_bytes(fact.model_dump(mode="json")).decode("utf-8")
    summarized_units = _group_unit_ids(summarized_groups)
    retained_units = _group_unit_ids(retained_groups)
    protected_units = tuple(
        unit.unit_id
        for unit in units
        if protection_by_id[unit.unit_id].classes
    )
    return PreparedWindowCompactionSourceDocument(
        fact=fact,
        canonical_json=serialized,
        protected_unit_ids=protected_units,
        summarized_unit_ids=tuple(
            unit.unit_id for unit in units if unit.unit_id in summarized_units
        ),
        retained_unit_ids=tuple(
            unit.unit_id for unit in units if unit.unit_id in retained_units
        ),
    )


def _source_entry(
    *,
    source_entry_id: str,
    source_kind: str,
    source_event_refs: tuple[ContextEventReferenceFact, ...],
    source_artifact_refs: tuple[str, ...],
    source_message_id: str | None,
    source_block_index: int | None,
    model_visible_text: str,
    timing: ContextSourceTimingFact | None,
) -> WindowCompactionSourceEntryFact:
    payload = {
        "source_entry_id": source_entry_id,
        "source_kind": source_kind,
        "source_event_refs": source_event_refs,
        "source_artifact_refs": source_artifact_refs,
        "source_message_id": source_message_id,
        "source_block_index": source_block_index,
        "model_visible_text": model_visible_text,
        "timing": timing,
    }
    return WindowCompactionSourceEntryFact(
        **payload,
        semantic_fingerprint=context_fingerprint(
            "window-compaction-source-entry:v1", payload
        ),
    )


def _message_timing(message) -> ContextSourceTimingFact:
    observed = message.finished_at_utc or message.created_at_utc
    payload = {
        "observed_at_utc": observed,
        "source_started_at_utc": message.created_at_utc,
        "source_ended_at_utc": message.finished_at_utc,
        "source_sequence_start": message.source_sequence_start,
        "source_sequence_end": message.source_sequence_end,
        "freshness": (
            "current_turn"
            if message.segment == "current_user"
            else "current_run_tail"
            if message.segment == "current_run_tail"
            else "compacted_history"
            if message.segment == "compaction_summary"
            else "historical_replay"
        ),
        "clock_source": "message_created_at",
    }
    return ContextSourceTimingFact(
        **payload,
        timing_fingerprint=context_fingerprint("context-source-timing:v1", payload),
    )


def _source_entry_id(window_id: str, message_id: str, block_index: int) -> str:
    digest = context_fingerprint(
        "window-compaction-source-entry-id:v1",
        {"window_id": window_id, "message_id": message_id, "block_index": block_index},
    ).removeprefix("sha256:")
    return f"window_compaction_source:{digest}"


def _pair_group_id(window_id: str, assistant_message_id: str) -> str:
    digest = context_fingerprint(
        "window-compaction-pair-group-id:v1",
        {"window_id": window_id, "assistant_message_id": assistant_message_id},
    ).removeprefix("sha256:")
    return f"window_compaction_pair_group:{digest}"


def _group_unit_ids(groups: tuple[WindowCompactionPairGroupFact, ...]) -> set[str]:
    return {unit_id for group in groups for unit_id in group.result_unit_ids}


def _unit_source_refs(
    unit: ToolResultRenderUnit,
    transcript: TranscriptCompileInput,
) -> tuple[ContextEventReferenceFact, ...]:
    for message in transcript.messages:
        if message.message_id != unit.result_message_id:
            continue
        for block in message.blocks:
            if (
                isinstance(block, TranscriptToolResultRefFact)
                and block.tool_result_unit_id == unit.unit_id
            ):
                return block.source_events
    raise ValueError("window compaction rollup member lacks source refs")


__all__ = [
    "PreparedWindowCompactionSourceDocument",
    "build_compacted_context_window",
    "build_window_compaction_plan",
    "parse_window_compaction_summary",
    "prepare_window_compaction_source_document",
    "window_compaction_identity",
]
