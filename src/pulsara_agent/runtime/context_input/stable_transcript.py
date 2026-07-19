"""Lower verified transcript-projection entries into compiler input facts.

This is the AP5 boundary between the lossless incremental transcript reducer
and the context compiler.  It deliberately has no EventLog, raw stream-event,
or artifact-store dependency.  Every content artifact and terminal projection
must already have been read-confirmed by the owning preparation service.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256

from pulsara_agent.event import (
    ContextCompactionCompletedEvent,
    ContextWindowCompactionCompletedEvent,
)
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.context import (
    CompactedWindowReferenceFact,
    ToolInteractionPairFact,
    TranscriptBlockFact,
    TranscriptCompileInput,
    TranscriptDataPlaceholderFact,
    TranscriptMessageFact,
    TranscriptProjectionWindowFact,
    TranscriptTextBlockFact,
    TranscriptThinkingBlockFact,
    TranscriptToolCallFact,
    TranscriptToolResultRefFact,
    WindowCompactionSourceDocumentFact,
)
from pulsara_agent.primitives.terminal_projection import (
    CanonicalToolResultDataBlockSemanticFact,
    CanonicalToolResultTextBlockSemanticFact,
    ModelDataBlockSemanticFact,
    ModelProviderErrorSemanticFact,
    ModelTerminalProjectionPayloadFact,
    ModelTextBlockSemanticFact,
    ModelThinkingBlockSemanticFact,
    ModelToolCallBlockSemanticFact,
    TerminalArtifactContentReferenceFact,
    TerminalContentFact,
    TerminalInlineContentFact,
    TerminalProjectionDocumentFact,
    ToolTerminalProjectionPayloadFact,
)
from pulsara_agent.primitives.tool_result import (
    ToolResultContentFact,
    ToolResultDataContentFact,
    ToolResultRenderUnit,
    ToolResultTextContentFact,
)
from pulsara_agent.primitives.transcript_projection import (
    InlineNormalizedMessageContentFact,
    NormalizedMessageContentArtifactFact,
    NormalizedMessageContentArtifactReferenceFact,
    TerminalProjectionMessageContentRefFact,
    TranscriptInlineBlockFact,
    TranscriptMessageLeafEntryFact,
    TranscriptProjectionLeafEntryFact,
    TranscriptProviderDataPlaceholderSemanticFact,
    TranscriptProviderTextBlockSemanticFact,
    TranscriptProviderThinkingBlockSemanticFact,
    TranscriptProviderToolCallBlockSemanticFact,
    TranscriptProviderToolResultRefSemanticFact,
    TranscriptToolPairLeafEntryFact,
    TranscriptToolResultLeafEntryFact,
)
from pulsara_agent.runtime.authority_materialization.evidence_cursor import (
    TranscriptProjectionDocumentResolver,
)
from pulsara_agent.runtime.context_input.transcript import (
    NormalizedContextTranscript,
    ToolResultPairingError,
    TranscriptNormalizationError,
)
from pulsara_agent.runtime.context_input.window_baseline import (
    parse_window_compaction_transcript_baseline,
)


def project_stable_context_transcript(
    *,
    runtime_session_id: str,
    through_sequence: int,
    current_user_anchor: str,
    projection_window: TranscriptProjectionWindowFact,
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...],
    documents: TranscriptProjectionDocumentResolver,
    hydrated_message_contents: tuple[NormalizedMessageContentArtifactFact, ...] = (),
    terminal_content_text_by_artifact_id: Mapping[str, str] | None = None,
    compaction_summary_text: str | None = None,
    compaction_terminal_event: (
        ContextCompactionCompletedEvent
        | ContextWindowCompactionCompletedEvent
        | None
    ) = None,
    window_compaction_source_document: WindowCompactionSourceDocumentFact | None = None,
) -> NormalizedContextTranscript:
    """Lower one verified stable projection without decoding raw semantic events."""

    if through_sequence < 1:
        raise TranscriptNormalizationError("stable transcript high-water is empty")
    if projection_window.protected_run_through_sequence != through_sequence:
        raise TranscriptNormalizationError("stable transcript/window high-water mismatch")
    _validate_entry_order(stable_entries)
    message_documents = {
        item.fact_fingerprint: item for item in hydrated_message_contents
    }
    terminal_texts = terminal_content_text_by_artifact_id or {}
    selected_entries = _select_entries(
        stable_entries,
        projection_window=projection_window,
    )
    window_baseline = None
    if projection_window.window_kind == "window_compaction":
        if window_compaction_source_document is None:
            raise TranscriptNormalizationError("window compaction source is missing")
        window_baseline = parse_window_compaction_transcript_baseline(
            window_compaction_source_document.retained_transcript_baseline
        )
        if window_baseline.current_user_anchor != current_user_anchor:
            raise TranscriptNormalizationError(
                "window baseline current-user anchor mismatch"
            )
    current_user_candidates = tuple(
        entry
        for entry in selected_entries
        if isinstance(entry, TranscriptMessageLeafEntryFact)
        and entry.attribution.message_id == current_user_anchor
    )
    baseline_current_user_candidates = (
        tuple(
            message
            for message in window_baseline.retained_messages
            if message.message_id == current_user_anchor
        )
        if window_baseline is not None
        else ()
    )
    if (
        len(current_user_candidates)
        + len(baseline_current_user_candidates)
        != 1
    ):
        raise TranscriptNormalizationError(
            "stable transcript requires one exact current-user anchor"
    )
    if current_user_candidates:
        current_run_id = current_user_candidates[0].attribution.run_id
    else:
        baseline_current_user = baseline_current_user_candidates[0]
        current_run_id = baseline_current_user.run_id
    if current_run_id is None:
        raise TranscriptNormalizationError(
            "stable transcript current-user anchor lacks run attribution"
        )
    messages: list[TranscriptMessageFact] = []
    message_entries: dict[str, TranscriptMessageLeafEntryFact] = {}
    result_entries: dict[int, TranscriptToolResultLeafEntryFact] = {}
    pair_entries: dict[str, TranscriptToolPairLeafEntryFact] = {}
    pair_entries_by_interaction: dict[
        tuple[str, str], TranscriptToolPairLeafEntryFact
    ] = {}
    units_by_interaction: dict[tuple[str, str], ToolResultRenderUnit] = {}
    message_entries_by_ordinal: dict[int, TranscriptMessageLeafEntryFact] = {}

    for entry in selected_entries:
        if isinstance(entry, TranscriptMessageLeafEntryFact):
            message = _message_from_entry(
                entry,
                current_user_anchor=current_user_anchor,
                current_run_id=current_run_id,
                documents=documents,
                hydrated_message_contents=message_documents,
                terminal_content_texts=terminal_texts,
            )
            if message.message_id in message_entries:
                raise TranscriptNormalizationError("duplicate stable message identity")
            message_entries[message.message_id] = entry
            message_entries_by_ordinal[entry.ordinal.value] = entry
            messages.append(message)
        elif isinstance(entry, TranscriptToolResultLeafEntryFact):
            ordinal = entry.ordinal.value
            if ordinal in result_entries:
                raise ToolResultPairingError("duplicate stable tool-result projection")
            result_entries[ordinal] = entry
        else:
            if entry.pair_id in pair_entries:
                raise ToolResultPairingError("duplicate stable tool-pair projection")
            pair_entries[entry.pair_id] = entry

    paired_result_ordinals = {
        entry.semantic_identity.result_block_position
        for entry in pair_entries.values()
    }
    if set(result_entries) != paired_result_ordinals:
        raise ToolResultPairingError("stable result/pair projection cardinality mismatch")

    messages_by_id = {message.message_id: message for message in messages}
    for pair_entry in sorted(
        pair_entries.values(), key=lambda item: item.ordinal.value
    ):
        semantic = pair_entry.semantic_identity
        assistant_entry = message_entries_by_ordinal.get(semantic.call_block_position)
        if (
            assistant_entry is None
            or assistant_entry.semantic_identity.semantic_fingerprint
            != semantic.assistant_message_semantic_fingerprint
        ):
            raise ToolResultPairingError("stable tool pair has no assistant message")
        assistant = messages_by_id.get(assistant_entry.attribution.message_id)
        if assistant is None:
            raise ToolResultPairingError("stable tool pair assistant was not normalized")
        call_id = semantic.assistant_tool_call_id
        matching_calls = tuple(
            (index, block)
            for index, block in enumerate(assistant.blocks)
            if isinstance(block, TranscriptToolCallFact)
            and block.tool_call_id == call_id
        )
        if len(matching_calls) != 1:
            raise ToolResultPairingError("stable assistant tool-call join is ambiguous")
        call_block_index, call_block = matching_calls[0]
        result_entry = result_entries[semantic.result_block_position]
        document = documents.resolve(result_entry.projection_reference)
        if not isinstance(document.payload, ToolTerminalProjectionPayloadFact):
            raise ToolResultPairingError("stable tool-result document kind drifted")
        tool_semantic = document.semantic_identity
        if tool_semantic.projection_kind != "tool_result":
            raise ToolResultPairingError("stable result does not reference tool semantics")
        if (
            call_block.model_tool_name != semantic.tool_name
            or result_entry.semantic_identity.tool_name != semantic.tool_name
        ):
            raise ToolResultPairingError("stable tool name attribution drifted")
        result_message_id = (
            f"tool-result-message:{call_id}:"
            f"{result_entry.source_event_refs[-1].event_id}"
        )
        unit_id = (
            f"tool-result-unit:{call_id}:"
            f"{result_entry.source_event_refs[-1].event_id}"
        )
        result_message = _tool_result_message(
            entry=result_entry,
            unit_id=unit_id,
            message_id=result_message_id,
            segment=assistant.segment,
        )
        messages.append(result_message)
        unit = _tool_result_unit(
            pair_entry=pair_entry,
            result_entry=result_entry,
            document=document,
            assistant=assistant,
            result_message=result_message,
            call_block_index=call_block_index,
            terminal_content_texts=terminal_texts,
        )
        interaction_key = (assistant.message_id, call_id)
        if interaction_key in units_by_interaction:
            raise ToolResultPairingError("duplicate stable tool interaction")
        units_by_interaction[interaction_key] = unit
        pair_entries_by_interaction[interaction_key] = pair_entry

    messages.sort(key=_message_sort_key)
    compacted_window = _compaction_window(
        projection_window=projection_window,
        compaction_summary_text=compaction_summary_text,
        compaction_terminal_event=compaction_terminal_event,
        source_document=window_compaction_source_document,
        runtime_session_id=runtime_session_id,
    )
    baseline_pairs: tuple[ToolInteractionPairFact, ...] = ()
    baseline_units: tuple[ToolResultRenderUnit, ...] = ()
    if projection_window.window_kind == "window_compaction":
        assert window_baseline is not None
        messages = [*window_baseline.retained_messages, *messages]
        baseline_pairs = window_baseline.retained_tool_pairs
        baseline_units = window_baseline.retained_tool_result_units
    if compacted_window is not None:
        messages.insert(0, compacted_window[0])

    positions = {
        (message.message_id, block_index): position
        for position, (message, block_index) in enumerate(
            (message, block_index)
            for message in messages
            for block_index, _block in enumerate(message.blocks)
        )
    }
    pairs = list(baseline_pairs)
    units: list[ToolResultRenderUnit] = []
    baseline_pair_by_call = {
        (pair.call_message_id, pair.tool_call_id): pair for pair in baseline_pairs
    }
    for unit in baseline_units:
        pair = baseline_pair_by_call.get(
            (unit.call_message_id, unit.tool_call_id)
        )
        if pair is None:
            raise TranscriptNormalizationError(
                "window baseline result unit lacks its durable pair"
            )
        try:
            call_position = positions[
                (pair.call_message_id, pair.call_block_index)
            ]
            result_position = positions[
                (pair.result_message_id, pair.result_block_index)
            ]
        except KeyError as exc:
            raise TranscriptNormalizationError(
                "window baseline pair escaped its retained messages"
            ) from exc
        payload = unit.model_dump(mode="python", exclude={"unit_fingerprint"})
        payload.update(
            call_position=call_position,
            result_position=result_position,
        )
        units.append(
            ToolResultRenderUnit(
                **payload,
                unit_fingerprint=context_fingerprint(
                    "tool-result-render-unit:v1", payload
                ),
            )
        )
    for (call_message_id, call_id), unit in units_by_interaction.items():
        assistant = next(
            message
            for message in messages
            if message.message_id == call_message_id
        )
        call_index = next(
            index
            for index, block in enumerate(assistant.blocks)
            if isinstance(block, TranscriptToolCallFact)
            and block.tool_call_id == call_id
        )
        call_position = positions[(assistant.message_id, call_index)]
        result_position = positions[(unit.result_message_id, 0)]
        pair_entry = pair_entries_by_interaction[(call_message_id, call_id)]
        result_entry = result_entries[
            pair_entry.semantic_identity.result_block_position
        ]
        pair_payload = {
            "tool_call_id": call_id,
            "model_tool_name": unit.model_tool_name,
            "call_message_id": assistant.message_id,
            "call_block_index": call_index,
            "result_message_id": unit.result_message_id,
            "result_block_index": 0,
            "call_sequence": assistant.source_sequence_start,
            "result_sequence": result_entry.source_event_refs[0].sequence,
            "pairing_status": "completed",
        }
        pairs.append(
            ToolInteractionPairFact(
                **pair_payload,
                pair_fingerprint=context_fingerprint(
                    "tool-interaction-pair:v1", pair_payload
                ),
            )
        )
        payload = unit.model_dump(mode="python", exclude={"unit_fingerprint"})
        payload.update(
            call_position=call_position,
            result_position=result_position,
        )
        units.append(
            ToolResultRenderUnit(
                **payload,
                unit_fingerprint=context_fingerprint(
                    "tool-result-render-unit:v1", payload
                ),
            )
        )
    pairs.sort(
        key=lambda item: (
            positions[(item.call_message_id, item.call_block_index)],
            item.tool_call_id,
        )
    )
    units.sort(key=lambda item: (item.result_position, item.unit_id))

    payload = {
        "schema_version": "transcript-input:v1",
        "runtime_session_id": runtime_session_id,
        "through_sequence": through_sequence,
        "current_user_anchor": current_user_anchor,
        "projection_window": projection_window,
        "messages": tuple(messages),
        "tool_pairs": tuple(pairs),
        "compacted_windows": (
            (compacted_window[1],) if compacted_window is not None else ()
        ),
        "stripped_unfinished_call_ids": (),
        "omitted_non_model_block_ids": (),
    }
    transcript = TranscriptCompileInput(
        **payload,
        transcript_fingerprint=context_fingerprint(
            "transcript-compile-input:v1", payload
        ),
    )
    return NormalizedContextTranscript(
        transcript=transcript,
        tool_result_units=tuple(units),
    )


def required_terminal_content_artifacts(
    *,
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...],
    projection_window: TranscriptProjectionWindowFact,
    documents: TranscriptProjectionDocumentResolver,
) -> tuple[TerminalArtifactContentReferenceFact, ...]:
    """Return the exact content artifacts needed by the selected projection."""

    references: dict[str, TerminalArtifactContentReferenceFact] = {}
    for entry in _select_entries(
        stable_entries,
        projection_window=projection_window,
    ):
        reference = None
        if isinstance(entry, TranscriptMessageLeafEntryFact) and isinstance(
            entry.content,
            TerminalProjectionMessageContentRefFact,
        ):
            reference = entry.content.projection_reference
        elif isinstance(entry, TranscriptToolResultLeafEntryFact):
            reference = entry.projection_reference
        if reference is None:
            continue
        document = documents.resolve(reference)
        if isinstance(document.payload, ModelTerminalProjectionPayloadFact):
            content_values = tuple(item.content for item in document.payload.items)
        else:
            content_values = tuple(
                item.content
                for item in document.payload.canonical_result_block.content_blocks
            )
        for content in content_values:
            if not isinstance(content, TerminalArtifactContentReferenceFact):
                continue
            existing = references.get(content.artifact_id)
            if existing is not None and existing != content:
                raise TranscriptNormalizationError(
                    "terminal content artifact identity conflict"
                )
            references[content.artifact_id] = content
    return tuple(references[key] for key in sorted(references))


def _select_entries(
    entries: tuple[TranscriptProjectionLeafEntryFact, ...],
    *,
    projection_window: TranscriptProjectionWindowFact,
) -> tuple[TranscriptProjectionLeafEntryFact, ...]:
    if projection_window.window_kind == "window_compaction":
        through = projection_window.compacted_through_sequence
        if through is None:
            raise TranscriptNormalizationError("window compaction high-water is missing")
        return tuple(
            entry
            for entry in entries
            if max(ref.sequence for ref in entry.source_event_refs) > through
        )
    retained_from = projection_window.retained_history_from_sequence
    retained_through = projection_window.retained_history_through_sequence

    def visible(sequence: int) -> bool:
        retained = (
            retained_from is not None
            and retained_through is not None
            and retained_from <= sequence <= retained_through
        )
        protected = (
            projection_window.protected_run_start_sequence
            <= sequence
            <= projection_window.protected_run_through_sequence
        )
        return retained or protected

    return tuple(
        entry
        for entry in entries
        if any(visible(ref.sequence) for ref in entry.source_event_refs)
    )


def _message_from_entry(
    entry: TranscriptMessageLeafEntryFact,
    *,
    current_user_anchor: str,
    current_run_id: str,
    documents: TranscriptProjectionDocumentResolver,
    hydrated_message_contents: Mapping[str, NormalizedMessageContentArtifactFact],
    terminal_content_texts: Mapping[str, str],
) -> TranscriptMessageFact:
    content = entry.content
    if isinstance(content, InlineNormalizedMessageContentFact):
        blocks = _inline_blocks(content.blocks, source_refs=entry.source_event_refs)
    elif isinstance(content, NormalizedMessageContentArtifactReferenceFact):
        try:
            document = hydrated_message_contents[content.document_fact_fingerprint]
        except KeyError as exc:
            raise TranscriptNormalizationError(
                "normalized message content artifact was not hydrated"
            ) from exc
        if document.provider_semantic_identity != content.provider_semantic_identity:
            raise TranscriptNormalizationError("message content semantic join drifted")
        blocks = _inline_blocks(document.blocks, source_refs=entry.source_event_refs)
    elif isinstance(content, TerminalProjectionMessageContentRefFact):
        document = documents.resolve(content.projection_reference)
        blocks = _model_projection_blocks(
            document,
            selected_orders=content.selected_projection_orders,
            source_refs=entry.source_event_refs,
            terminal_content_texts=terminal_content_texts,
        )
    else:  # pragma: no cover - discriminated union is closed
        raise TranscriptNormalizationError("unknown stable message content carrier")
    attribution = entry.attribution
    segment = (
        attribution.segment
        if attribution.segment in {"recovery_note", "terminal_lifecycle_note"}
        else "current_user"
        if attribution.message_id == current_user_anchor
        else "current_run_tail"
        if attribution.run_id == current_run_id
        else "prior_history"
    )
    payload = {
        "message_id": attribution.message_id,
        "role": entry.semantic_identity.message_provider_semantic_identity.role,
        "name": entry.semantic_identity.message_provider_semantic_identity.name,
        "run_id": attribution.run_id,
        "turn_id": attribution.turn_id,
        "reply_id": attribution.reply_id,
        "created_at_utc": attribution.created_at_utc,
        "finished_at_utc": attribution.finished_at_utc,
        "segment": segment,
        "blocks": blocks,
        "source_sequence_start": min(ref.sequence for ref in entry.source_event_refs),
        "source_sequence_end": max(ref.sequence for ref in entry.source_event_refs),
    }
    return TranscriptMessageFact(
        **payload,
        message_fingerprint=context_fingerprint("transcript-message:v1", payload),
    )


def _inline_blocks(
    blocks: tuple[TranscriptInlineBlockFact, ...],
    *,
    source_refs,
) -> tuple[TranscriptBlockFact, ...]:
    projected: list[TranscriptBlockFact] = []
    for block in blocks:
        semantic = block.provider_semantic_identity
        block_id = block.attribution.block_id
        if isinstance(semantic, TranscriptProviderTextBlockSemanticFact):
            projected.append(
                TranscriptTextBlockFact(
                    block_id=block_id,
                    text=semantic.text,
                    content_fingerprint=context_fingerprint(
                        "transcript-text:v1", semantic.text
                    ),
                    source_events=source_refs,
                )
            )
        elif isinstance(semantic, TranscriptProviderThinkingBlockSemanticFact):
            projected.append(
                TranscriptThinkingBlockFact(
                    block_id=block_id,
                    thinking=semantic.thinking,
                    lowering_policy=semantic.lowering_policy,
                    content_fingerprint=context_fingerprint(
                        "transcript-thinking:v1", semantic.thinking
                    ),
                    source_events=source_refs,
                )
            )
        elif isinstance(semantic, TranscriptProviderDataPlaceholderSemanticFact):
            projected.append(
                TranscriptDataPlaceholderFact(
                    block_id=block_id,
                    name=semantic.name,
                    media_type=semantic.media_type,
                    source_kind=semantic.source_kind,
                    artifact_ids=(),
                    source_events=source_refs,
                )
            )
        elif isinstance(semantic, TranscriptProviderToolCallBlockSemanticFact):
            projected.append(
                TranscriptToolCallFact(
                    tool_call_id=semantic.tool_call_id,
                    model_tool_name=semantic.model_tool_name,
                    raw_arguments_json=semantic.raw_arguments_json,
                    arguments_status=semantic.arguments_status,
                    parsed_arguments=semantic.parsed_arguments,
                    parse_error_code=semantic.parse_error_code,
                    state=semantic.state,
                    source_events=source_refs,
                )
            )
        elif isinstance(semantic, TranscriptProviderToolResultRefSemanticFact):
            projected.append(
                TranscriptToolResultRefFact(
                    tool_call_id=semantic.tool_call_id,
                    tool_result_unit_id=semantic.tool_result_unit_semantic_fingerprint,
                    source_events=source_refs,
                )
            )
        else:  # pragma: no cover - discriminated union is closed
            raise TranscriptNormalizationError("unknown stable provider block")
    return tuple(projected)


def _model_projection_blocks(
    document: TerminalProjectionDocumentFact,
    *,
    selected_orders: tuple[int, ...],
    source_refs,
    terminal_content_texts: Mapping[str, str],
) -> tuple[TranscriptBlockFact, ...]:
    if not isinstance(document.payload, ModelTerminalProjectionPayloadFact):
        raise TranscriptNormalizationError("message references non-model projection")
    selected = set(selected_orders)
    blocks: list[TranscriptBlockFact] = []
    for item in document.payload.items:
        semantic = item.semantic_identity
        if semantic.projection_order not in selected:
            continue
        if isinstance(semantic, ModelProviderErrorSemanticFact):
            raise TranscriptNormalizationError("provider error cannot enter transcript")
        if getattr(semantic, "completion_status", "completed") != "completed":
            raise TranscriptNormalizationError("interrupted model block entered transcript")
        if isinstance(semantic, ModelTextBlockSemanticFact):
            text = _terminal_content_text(item.content, terminal_content_texts)
            blocks.append(
                TranscriptTextBlockFact(
                    block_id=semantic.block_id,
                    text=text,
                    content_fingerprint=context_fingerprint("transcript-text:v1", text),
                    source_events=source_refs,
                )
            )
        elif isinstance(semantic, ModelThinkingBlockSemanticFact):
            text = _terminal_content_text(item.content, terminal_content_texts)
            blocks.append(
                TranscriptThinkingBlockFact(
                    block_id=semantic.block_id,
                    thinking=text,
                    content_fingerprint=context_fingerprint(
                        "transcript-thinking:v1", text
                    ),
                    source_events=source_refs,
                )
            )
        elif isinstance(semantic, ModelDataBlockSemanticFact):
            artifact_ids = (
                (item.content.artifact_id,)
                if isinstance(item.content, TerminalArtifactContentReferenceFact)
                else ()
            )
            blocks.append(
                TranscriptDataPlaceholderFact(
                    block_id=semantic.block_id,
                    name=None,
                    media_type=semantic.media_type,
                    source_kind="terminal_projection",
                    artifact_ids=artifact_ids,
                    source_events=source_refs,
                )
            )
        elif isinstance(semantic, ModelToolCallBlockSemanticFact):
            blocks.append(
                TranscriptToolCallFact(
                    tool_call_id=semantic.tool_call_id,
                    model_tool_name=semantic.tool_name,
                    raw_arguments_json=semantic.raw_arguments_json,
                    arguments_status=semantic.arguments_status,
                    parsed_arguments=semantic.parsed_arguments,
                    parse_error_code=semantic.parse_error_code,
                    state="finished",
                    source_events=source_refs,
                )
            )
    if tuple(
        item.semantic_identity.projection_order
        for item in document.payload.items
        if item.semantic_identity.projection_order in selected
    ) != selected_orders:
        raise TranscriptNormalizationError("selected model projection order drifted")
    return tuple(blocks)


def _tool_result_message(
    *,
    entry: TranscriptToolResultLeafEntryFact,
    unit_id: str,
    message_id: str,
    segment: str,
) -> TranscriptMessageFact:
    ref = TranscriptToolResultRefFact(
        tool_call_id=entry.semantic_identity.tool_call_id,
        tool_result_unit_id=unit_id,
        source_events=entry.source_event_refs,
    )
    payload = {
        "message_id": message_id,
        "role": "assistant",
        "name": entry.semantic_identity.tool_name,
        "run_id": None,
        "turn_id": None,
        "reply_id": None,
        "created_at_utc": None,
        "finished_at_utc": None,
        "segment": segment,
        "blocks": (ref,),
        "source_sequence_start": min(ref.sequence for ref in entry.source_event_refs),
        "source_sequence_end": max(ref.sequence for ref in entry.source_event_refs),
    }
    return TranscriptMessageFact(
        **payload,
        message_fingerprint=context_fingerprint("transcript-message:v1", payload),
    )


def _tool_result_unit(
    *,
    pair_entry: TranscriptToolPairLeafEntryFact,
    result_entry: TranscriptToolResultLeafEntryFact,
    document: TerminalProjectionDocumentFact,
    assistant: TranscriptMessageFact,
    result_message: TranscriptMessageFact,
    call_block_index: int,
    terminal_content_texts: Mapping[str, str],
) -> ToolResultRenderUnit:
    if not isinstance(document.payload, ToolTerminalProjectionPayloadFact):
        raise ToolResultPairingError("tool result references non-tool document")
    semantic = document.semantic_identity
    if semantic.projection_kind != "tool_result":
        raise ToolResultPairingError("tool result semantic kind drifted")
    canonical = document.payload.canonical_result_block
    text_blocks: list[ToolResultTextContentFact] = []
    data_blocks: list[ToolResultDataContentFact] = []
    source_refs = tuple(
        sorted(
            {
                ref.event_id: ref
                for ref in (*assistant.blocks[call_block_index].source_events, *result_entry.source_event_refs)
            }.values(),
            key=lambda item: item.sequence,
        )
    )
    for block in canonical.content_blocks:
        block_semantic = block.semantic_identity
        if isinstance(block_semantic, CanonicalToolResultTextBlockSemanticFact):
            text = _terminal_content_text(block.content, terminal_content_texts)
            text_blocks.append(
                ToolResultTextContentFact(
                    block_id=block_semantic.block_id,
                    text=text,
                    chars=len(text),
                    content_fingerprint=context_fingerprint(
                        "tool-result-text:v1", text
                    ),
                    source_events=source_refs,
                )
            )
        elif isinstance(block_semantic, CanonicalToolResultDataBlockSemanticFact):
            artifact_ids = tuple(item.artifact_id for item in canonical.artifact_refs)
            if isinstance(block.content, TerminalArtifactContentReferenceFact):
                artifact_ids = (*artifact_ids, block.content.artifact_id)
            data_blocks.append(
                ToolResultDataContentFact(
                    block_id=block_semantic.block_id,
                    name=block_semantic.name,
                    media_type=block_semantic.media_type,
                    source_kind=block_semantic.source_kind,
                    artifact_ids=tuple(dict.fromkeys(artifact_ids)),
                    source_events=source_refs,
                )
            )
    content_payload = {
        "text_blocks": tuple(text_blocks),
        "data_blocks": tuple(data_blocks),
    }
    content = ToolResultContentFact(
        **content_payload,
        content_fingerprint=context_fingerprint(
            "tool-result-content:v1", content_payload
        ),
    )
    semantics = semantic.execution_semantics
    unit_id = result_message.blocks[0].tool_result_unit_id
    payload = {
        "schema_version": "tool-result-unit:v1",
        "unit_id": unit_id,
        "tool_call_id": result_entry.semantic_identity.tool_call_id,
        "model_tool_name": result_entry.semantic_identity.tool_name,
        "descriptor_attribution": semantics.render_profile.descriptor_attribution,
        "render_contract_fingerprint": semantics.render_profile.render_contract_fingerprint,
        "render_variant_fingerprint": semantics.render_profile.selected_variant.variant_fingerprint,
        "call_message_id": assistant.message_id,
        "result_message_id": result_message.message_id,
        "call_position": pair_entry.semantic_identity.call_block_position,
        "result_position": pair_entry.semantic_identity.result_block_position,
        "result_state": semantics.result_state,
        "content": content,
        "artifacts": canonical.artifact_refs,
        "observation_timing": semantic.observation_timing,
        "terminal_payload_timing": semantics.terminal_payload_timing,
        "render_profile": semantics.render_profile,
        "essential_capture_policy": semantics.essential_capture_policy,
        "essential": semantics.essential_result,
        "rollup_semantics": semantics.rollup_semantics,
        "source_sequence_start": source_refs[0].sequence,
        "source_sequence_end": source_refs[-1].sequence,
        "source_event_ids": tuple(ref.event_id for ref in source_refs),
    }
    return ToolResultRenderUnit(
        **payload,
        unit_fingerprint=context_fingerprint("tool-result-render-unit:v1", payload),
    )


def _terminal_content_text(
    content: TerminalContentFact | None,
    terminal_content_texts: Mapping[str, str],
) -> str:
    if isinstance(content, TerminalInlineContentFact):
        return content.text
    if isinstance(content, TerminalArtifactContentReferenceFact):
        try:
            text = terminal_content_texts[content.artifact_id]
        except KeyError as exc:
            raise TranscriptNormalizationError(
                "terminal content artifact was not hydrated"
            ) from exc
        encoded = text.encode("utf-8")
        if (
            len(encoded) != content.artifact_bytes
            or f"sha256:{sha256(encoded).hexdigest()}"
            != content.artifact_sha256
        ):
            raise TranscriptNormalizationError("terminal content artifact drifted")
        return text
    raise TranscriptNormalizationError("terminal projection content is missing")


def _compaction_window(
    *,
    projection_window: TranscriptProjectionWindowFact,
    compaction_summary_text: str | None,
    compaction_terminal_event: (
        ContextCompactionCompletedEvent
        | ContextWindowCompactionCompletedEvent
        | None
    ),
    source_document: WindowCompactionSourceDocumentFact | None,
    runtime_session_id: str,
) -> tuple[TranscriptMessageFact, CompactedWindowReferenceFact] | None:
    terminal_ref = projection_window.compaction_terminal_ref
    if terminal_ref is None:
        if any(
            item is not None
            for item in (
                compaction_summary_text,
                compaction_terminal_event,
                source_document,
            )
        ):
            raise TranscriptNormalizationError("uncompacted projection got compaction facts")
        return None
    if compaction_summary_text is None or compaction_terminal_event is None:
        raise TranscriptNormalizationError("compacted projection facts are incomplete")
    event = compaction_terminal_event
    if event.id != terminal_ref.event_id or event.sequence != terminal_ref.sequence:
        raise TranscriptNormalizationError("compaction terminal identity drifted")
    is_prefix = isinstance(event, ContextCompactionCompletedEvent)
    if is_prefix == (projection_window.window_kind == "window_compaction"):
        raise TranscriptNormalizationError("compaction terminal kind drifted")
    if isinstance(event, ContextWindowCompactionCompletedEvent):
        if source_document is None:
            raise TranscriptNormalizationError("window compaction source is missing")
        if (
            source_document.compaction_id != event.compaction_id
            or source_document.document_fingerprint
            != projection_window.window_compaction_source_document_fingerprint
        ):
            raise TranscriptNormalizationError("window compaction source drifted")
    elif source_document is not None:
        raise TranscriptNormalizationError("prefix compaction got window source")
    message_id = (
        f"compaction-summary:{event.compaction_id}"
        if is_prefix
        else f"window-compaction-summary:{event.compaction_id}"
    )
    block = TranscriptTextBlockFact(
        block_id=f"text:{message_id}",
        text=compaction_summary_text,
        content_fingerprint=context_fingerprint(
            "transcript-text:v1", compaction_summary_text
        ),
        source_events=(terminal_ref,),
    )
    message_payload = {
        "message_id": message_id,
        "role": "system",
        "name": "pulsara_compaction" if is_prefix else "pulsara_window_compaction",
        "run_id": None,
        "turn_id": None,
        "reply_id": None,
        "created_at_utc": event.created_at,
        "finished_at_utc": event.created_at,
        "segment": "compaction_summary",
        "blocks": (block,),
        "source_sequence_start": terminal_ref.sequence,
        "source_sequence_end": terminal_ref.sequence,
    }
    message = TranscriptMessageFact(
        **message_payload,
        message_fingerprint=context_fingerprint(
            "transcript-message:v1", message_payload
        ),
    )
    compacted = CompactedWindowReferenceFact(
        compaction_kind="prefix" if is_prefix else "window",
        compaction_id=event.compaction_id,
        summary_artifact_id=event.summary_artifact_id,
        compacted_through_sequence=(
            event.through_sequence
            if is_prefix
            else projection_window.compacted_through_sequence
        ),
        keep_after_sequence=event.keep_after_sequence if is_prefix else None,
        summary_message_id=message_id,
        source_event=terminal_ref,
        source_started_event=(
            projection_window.window_compaction_started_ref
            if not is_prefix
            else None
        ),
    )
    del runtime_session_id
    return message, compacted


def _message_sort_key(message: TranscriptMessageFact) -> tuple[int, int, str]:
    return (
        message.source_sequence_start,
        message.source_sequence_end,
        message.message_id,
    )


def _validate_entry_order(
    entries: tuple[TranscriptProjectionLeafEntryFact, ...],
) -> None:
    ordinals = tuple(item.ordinal.value for item in entries)
    if ordinals != tuple(sorted(ordinals)) or len(ordinals) != len(set(ordinals)):
        raise TranscriptNormalizationError("stable transcript ordinals are invalid")
    for entry in entries:
        if not entry.source_event_refs:
            raise TranscriptNormalizationError("stable transcript entry lacks authority")


__all__ = [
    "project_stable_context_transcript",
    "required_terminal_content_artifacts",
]
