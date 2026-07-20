"""Pure normalization of a frozen event slice into compiler transcript facts.

The projector deliberately does not read ``EventLog``, ``LoopState``, message
replay caches, or a live capability registry.  Every model-visible block and
tool-result unit is derived from the owned canonical events in one
``ContextEventSlice`` and the already-frozen context snapshot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

from pulsara_agent.event.events import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    ContextCompactionCompletedEvent,
    ContextWindowCompactionCompletedEvent,
    ContextWindowCompactionStartedEvent,
    DataBlockSegmentEvent,
    DataBlockEndEvent,
    DataBlockStartEvent,
    ExternalExecutionResultEvent,
    HintBlockEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RunEndEvent,
    RunStartEvent,
    TerminalProcessCompletedEvent,
    TextBlockSegmentEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockSegmentEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallArgumentsSegmentEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.message.assembler import BlockAssembler, BlockCompletion
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.message.blocks import (
    Base64Source,
    DataBlock,
    HintBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultArtifactRef,
    ToolResultBlock,
)
from pulsara_agent.primitives._context_base import FrozenJsonObjectFact
from pulsara_agent.message.reducer import (
    MessageReplayControlError,
    accepted_main_reply_ids,
)
from pulsara_agent.primitives.context import (
    CompactedWindowReferenceFact,
    ContextEventReferenceFact,
    ContextEventRangeFact,
    ContextFactSnapshotFact,
    ContextRunEntryReferenceFact,
    ContextAuthoritySlicePlan,
    ToolArgumentsParseErrorCode,
    ToolInteractionPairFact,
    TranscriptBlockFact,
    TranscriptCompileInput,
    TranscriptDataPlaceholderFact,
    TranscriptMessageFact,
    TranscriptTextBlockFact,
    TranscriptThinkingBlockFact,
    TranscriptToolCallFact,
    TranscriptToolResultRefFact,
    WindowCompactionSourceDocumentFact,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.run_entry import CurrentUserMessageFact
from pulsara_agent.primitives.tool_result import (
    ContextToolResultArtifactRefFact,
    ContextToolResultPreviewFact,
    ExternalToolResultIngressFact,
    ToolResultContentFact,
    ToolResultDataContentFact,
    ToolResultExecutionSemanticsFact,
    ToolResultRenderUnit,
    ToolResultStateFact,
    ToolResultTextContentFact,
)
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.runtime.context_input.event_slice import (
    ContextEventAuthorityView,
    ContextEventSlice,
    ContextEventSliceError,
    FrozenStoredEvent,
)
from pulsara_agent.runtime.context_input.window_baseline import (
    parse_window_compaction_transcript_baseline,
)
from pulsara_agent.runtime.recovery import (
    RECOVERY_NOTE_ID_PREFIX_BY_STATUS,
    project_recovery_from_events,
    render_recovery_text,
)


class TranscriptNormalizationError(RuntimeError):
    """The durable slice cannot be lowered without guessing or losing facts."""


class ToolResultPairingError(TranscriptNormalizationError):
    """A tool call/result pair is missing, duplicated, or inconsistently attributed."""


@dataclass(frozen=True, slots=True)
class TranscriptProjectionIdentity:
    runtime_session_id: str


@dataclass(frozen=True, slots=True)
class ContextTranscriptProjectionAuthority:
    """Small immutable authority needed by transcript normalization.

    Full context compilation uses ``ContextFactSnapshotFact``. Mid-turn
    projection can freeze the same ledger window without inventing a model-call
    identity, while still consuming exactly the same pure projector.
    """

    identity: TranscriptProjectionIdentity
    run_entry: ContextRunEntryReferenceFact
    current_user_message: CurrentUserMessageFact
    authority_slice_plan: ContextAuthoritySlicePlan
    primary_event_range: ContextEventRangeFact
    named_event_ranges: tuple[ContextEventRangeFact, ...] = ()

    @classmethod
    def from_snapshot(
        cls,
        snapshot: ContextFactSnapshotFact,
    ) -> "ContextTranscriptProjectionAuthority":
        return cls(
            identity=TranscriptProjectionIdentity(
                runtime_session_id=snapshot.identity.runtime_session_id
            ),
            run_entry=snapshot.run_entry,
            current_user_message=snapshot.current_user_message,
            authority_slice_plan=snapshot.authority_slice_plan,
            primary_event_range=snapshot.primary_event_range,
            named_event_ranges=snapshot.named_event_ranges,
        )


@dataclass(frozen=True, slots=True)
class NormalizedContextTranscript:
    transcript: TranscriptCompileInput
    tool_result_units: tuple[ToolResultRenderUnit, ...]

    def __post_init__(self) -> None:
        unit_ids = tuple(unit.unit_id for unit in self.tool_result_units)
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("normalized tool-result unit IDs are not unique")
        unit_by_id = {unit.unit_id: unit for unit in self.tool_result_units}
        result_refs = {
            block.tool_result_unit_id: block
            for message in self.transcript.messages
            for block in message.blocks
            if isinstance(block, TranscriptToolResultRefFact)
        }
        if set(result_refs) != set(unit_by_id):
            raise ValueError(
                "transcript tool-result refs do not match normalized units"
            )
        for pair in self.transcript.tool_pairs:
            matching = tuple(
                unit
                for unit in self.tool_result_units
                if (
                    unit.call_message_id,
                    unit.tool_call_id,
                )
                == (
                    pair.call_message_id,
                    pair.tool_call_id,
                )
            )
            if len(matching) != 1:
                raise ValueError("tool pair does not resolve to one normalized unit")
            unit = matching[0]
            if (
                unit.model_tool_name,
                unit.call_message_id,
                unit.result_message_id,
            ) != (
                pair.model_tool_name,
                pair.call_message_id,
                pair.result_message_id,
            ):
                raise ValueError("tool pair/unit attribution mismatch")


@dataclass(slots=True)
class _CallProjection:
    fact: TranscriptToolCallFact
    message_id: str
    block_index: int
    sequence: int


@dataclass(slots=True)
class _ResultProjection:
    unit_id: str
    tool_call_id: str
    model_tool_name: str
    message_id: str
    block_index: int
    sequence: int
    pairing_status: str
    block: ToolResultBlock
    observation_timing: ToolObservationTimingFact
    semantics: ToolResultExecutionSemanticsFact
    source_refs: tuple[ContextEventReferenceFact, ...]


@dataclass(slots=True)
class _PendingUnit:
    result: _ResultProjection
    call: _CallProjection
    call_position: int
    result_position: int


def project_context_transcript(
    *,
    snapshot: ContextFactSnapshotFact | ContextTranscriptProjectionAuthority,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
    compaction_summary_text: str | None = None,
    window_compaction_source_document: WindowCompactionSourceDocumentFact | None = None,
) -> NormalizedContextTranscript:
    """Project one canonical authority slice into immutable transcript inputs."""

    _validate_snapshot_slice(snapshot=snapshot, event_slice=event_slice)
    decoded = tuple(
        (stored, stored.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY))
        for stored in event_slice.events
    )
    decoded_by_sequence = {
        stored.sequence: (stored, event) for stored, event in decoded
    }
    by_id = {stored.event_id: (stored, event) for stored, event in decoded}
    selected = tuple(
        (stored, event)
        for stored, event in decoded
        if _sequence_is_model_visible(
            stored.sequence, snapshot.authority_slice_plan.transcript_window
        )
    )
    accepted_reply_ids = _accepted_main_reply_ids(selected)
    current_start = _current_run_start(snapshot=snapshot, by_id=by_id)
    _validate_descriptor_attributions(
        snapshot=snapshot,
        by_id=by_id,
        events=(event for _, event in selected),
    )

    assembler = BlockAssembler()
    reply_starts: dict[str, tuple[FrozenStoredEvent, ReplyStartEvent]] = {}
    reply_ends: dict[str, tuple[FrozenStoredEvent, ReplyEndEvent]] = {}
    reply_blocks: dict[
        str, list[tuple[BlockCompletion, tuple[ContextEventReferenceFact, ...]]]
    ] = {}
    normal_results: list[
        tuple[
            ToolResultBlock, ToolResultEndEvent, tuple[ContextEventReferenceFact, ...]
        ]
    ] = []
    external_results: list[
        tuple[ToolResultBlock, ExternalToolResultIngressFact, ContextEventReferenceFact]
    ] = []
    omitted_ids: list[str] = []
    tool_call_starts: dict[str, tuple[FrozenStoredEvent, ToolCallStartEvent]] = {}
    tool_call_ends: dict[str, tuple[FrozenStoredEvent, ToolCallEndEvent]] = {}
    tool_result_starts: dict[str, tuple[FrozenStoredEvent, ToolResultStartEvent]] = {}
    tool_result_ends: dict[str, tuple[FrozenStoredEvent, ToolResultEndEvent]] = {}

    for stored, event in selected:
        if _is_model_reply_stream_event(event) and (
            event.reply_id not in accepted_reply_ids
        ):
            continue
        if isinstance(event, ReplyStartEvent):
            if event.reply_id in reply_starts:
                raise TranscriptNormalizationError("duplicate ReplyStart identity")
            reply_starts[event.reply_id] = (stored, event)
        elif isinstance(event, ReplyEndEvent):
            if event.reply_id in reply_ends:
                raise TranscriptNormalizationError("duplicate ReplyEnd identity")
            reply_ends[event.reply_id] = (stored, event)
        elif isinstance(event, ToolCallStartEvent):
            tool_call_starts.setdefault(event.tool_call_id, (stored, event))
        elif isinstance(event, ToolCallEndEvent):
            tool_call_ends[event.tool_call_id] = (stored, event)
        elif isinstance(event, ToolResultStartEvent):
            if event.tool_call_id in tool_result_starts:
                prior_stored, _prior = tool_result_starts[event.tool_call_id]
                raise ToolResultPairingError(
                    "duplicate tool result start for "
                    f"{event.tool_call_id!r}: "
                    f"{prior_stored.event_id}@{prior_stored.sequence}, "
                    f"{stored.event_id}@{stored.sequence}"
                )
            tool_result_starts[event.tool_call_id] = (stored, event)
        elif isinstance(event, ToolResultEndEvent):
            if event.tool_call_id in tool_result_ends:
                raise ToolResultPairingError("duplicate tool result terminal")
            tool_result_ends[event.tool_call_id] = (stored, event)
        elif isinstance(event, HintBlockEvent):
            omitted_ids.append(event.block_id)

        update = assembler.append(event)
        for completion in update.completed:
            refs = _completion_source_refs(
                event_slice=event_slice,
                completion=completion,
                decoded_by_sequence=decoded_by_sequence,
            )
            if isinstance(completion.block, HintBlock):
                continue
            if isinstance(completion.block, ToolResultBlock):
                if not isinstance(event, ToolResultEndEvent):
                    raise TranscriptNormalizationError(
                        "normal result block did not terminate with ToolResultEndEvent"
                    )
                normal_results.append((completion.block, event, refs))
            else:
                reply_blocks.setdefault(completion.reply_id, []).append(
                    (completion, refs)
                )

        if isinstance(event, ExternalExecutionResultEvent):
            event_ref = stored.to_reference(event_slice.runtime_session_id)
            for ingress in event.external_results:
                block = ToolResultBlock.model_validate(
                    thaw_json(ingress.result_block.canonical_block_payload)
                )
                external_results.append((block, ingress, event_ref))

    historical_unfinished_call_ids = _validate_stream_completeness(
        assembler=assembler,
        current_run_start_sequence=current_start.sequence,
        tool_call_starts=tool_call_starts,
        tool_call_ends=tool_call_ends,
        tool_result_starts=tool_result_starts,
        tool_result_ends=tool_result_ends,
        external_result_ids={
            ingress.result_block.tool_call_id for _, ingress, _ in external_results
        },
        selected=selected,
    )

    messages: list[TranscriptMessageFact] = []
    calls: dict[str, _CallProjection] = {}
    results: dict[str, _ResultProjection] = {}
    stripped_ids: list[str] = list(historical_unfinished_call_ids)

    summary_message, compacted_window = _compaction_summary_message(
        snapshot=snapshot,
        event_slice=event_slice,
        compaction_summary_text=compaction_summary_text,
        window_compaction_source_document=window_compaction_source_document,
    )
    if summary_message is not None:
        messages.append(summary_message)

    terminal_runs = {
        event.run_id: event for _, event in selected if isinstance(event, RunEndEvent)
    }
    for stored, event in selected:
        if not isinstance(event, RunStartEvent):
            continue
        messages.append(
            _user_message(
                event_slice=event_slice,
                stored=stored,
                event=event,
                current_start_sequence=current_start.sequence,
                current_message_id=snapshot.current_user_message.message_id,
            )
        )

    messages.extend(
        _prior_lifecycle_messages(
            snapshot=snapshot,
            event_slice=event_slice,
            decoded=decoded,
        )
    )

    for reply_id, blocks_with_refs in reply_blocks.items():
        start_pair = reply_starts.get(reply_id)
        end_pair = reply_ends.get(reply_id)
        if start_pair is None or end_pair is None:
            unfinished_only = bool(blocks_with_refs) and all(
                isinstance(completion.block, ToolCallBlock)
                and completion.block.id in historical_unfinished_call_ids
                for completion, _refs in blocks_with_refs
            )
            if unfinished_only:
                stripped_ids.extend(
                    completion.block.id
                    for completion, _refs in blocks_with_refs
                    if isinstance(completion.block, ToolCallBlock)
                )
                continue
            raise TranscriptNormalizationError(
                f"reply {reply_id!r} is missing start or end attribution"
            )
        start_stored, start_event = start_pair
        end_stored, end_event = end_pair
        if start_stored.sequence > end_stored.sequence:
            raise TranscriptNormalizationError("reply terminal precedes reply start")
        facts: list[TranscriptBlockFact] = []
        for completion, refs in sorted(
            blocks_with_refs,
            key=lambda item: (
                item[0].start_sequence or item[0].end_sequence or 0,
                item[0].end_sequence or 0,
            ),
        ):
            block = completion.block
            if isinstance(block, ToolCallBlock):
                if block.id in calls:
                    omitted_ids.append(
                        f"duplicate-tool-call:{block.id}:{refs[0].event_id}"
                    )
                    continue
                fact = _tool_call_fact(block=block, refs=refs)
                completed = block.id in tool_result_ends or any(
                    ingress.result_block.tool_call_id == block.id
                    for _, ingress, _ in external_results
                )
                if not completed:
                    terminal = terminal_runs.get(start_event.run_id)
                    if (
                        start_stored.sequence < current_start.sequence
                        and terminal is not None
                        and terminal.status in {"failed", "aborted"}
                    ):
                        stripped_ids.append(block.id)
                        continue
                    raise ToolResultPairingError(
                        f"active/completed tool call {block.id!r} has no result"
                    )
                block_index = len(facts)
                facts.append(fact)
                calls[block.id] = _CallProjection(
                    fact=fact,
                    message_id=reply_id,
                    block_index=block_index,
                    sequence=refs[0].sequence,
                )
            else:
                facts.append(_transcript_block(block=block, refs=refs))
        if not facts:
            continue
        segment = (
            "prior_history"
            if start_stored.sequence < current_start.sequence
            else "current_run_tail"
        )
        messages.append(
            _message_fact(
                message_id=reply_id,
                role="assistant",
                name=start_event.name,
                run_id=start_event.run_id,
                turn_id=start_event.turn_id,
                reply_id=reply_id,
                created_at_utc=start_event.created_at,
                finished_at_utc=end_event.created_at,
                segment=segment,
                blocks=tuple(facts),
                source_sequence_start=start_stored.sequence,
                source_sequence_end=end_stored.sequence,
            )
        )

    for block, end_event, refs in normal_results:
        result = _result_projection(
            block=block,
            observation_timing=end_event.observation_timing,
            semantics=ToolResultExecutionSemanticsFact(
                render_profile=end_event.render_profile,
                result_state=ToolResultStateFact(end_event.state.value),
                essential_capture_policy=end_event.essential_capture_policy,
                essential_result=end_event.essential_result,
                terminal_payload_timing=end_event.terminal_payload_timing,
                rollup_semantics=end_event.rollup_semantics,
            ),
            source_refs=refs,
            result_sequence=refs[-1].sequence,
            pairing_status="completed",
        )
        _append_result_message(
            messages=messages,
            results=results,
            result=result,
            current_run_start_sequence=current_start.sequence,
        )

    for block, ingress, event_ref in external_results:
        _validate_external_ingress_requirement(
            event_slice=event_slice,
            by_id=by_id,
            ingress=ingress,
            block=block,
        )
        result = _result_projection(
            block=block,
            observation_timing=ingress.observation_timing,
            semantics=ingress.execution_semantics,
            source_refs=(event_ref,),
            result_sequence=event_ref.sequence,
            pairing_status="external_completed",
        )
        _append_result_message(
            messages=messages,
            results=results,
            result=result,
            current_run_start_sequence=current_start.sequence,
        )

    messages, calls, results, baseline_pairs, baseline_units = (
        _apply_window_compaction_projection(
        messages=messages,
        calls=calls,
        results=results,
        snapshot=snapshot,
        source_document=window_compaction_source_document,
        )
    )
    messages = _sort_messages(messages, results=results)
    positions = {
        (message.message_id, block_index): position
        for position, (message, block_index) in enumerate(
            (message, block_index)
            for message in messages
            for block_index, _ in enumerate(message.blocks)
        )
    }
    pairs: list[ToolInteractionPairFact] = list(baseline_pairs)
    units: list[ToolResultRenderUnit] = list(baseline_units)
    ordered_results = sorted(
        results.items(),
        key=lambda item: positions[(item[1].message_id, item[1].block_index)],
    )
    for tool_call_id, result in ordered_results:
        call = calls.get(tool_call_id)
        if call is None:
            raise ToolResultPairingError(
                f"tool result {tool_call_id!r} has no assistant tool call"
            )
        if result.model_tool_name != call.fact.model_tool_name:
            raise ToolResultPairingError("tool call/result model name mismatch")
        call_position = positions[(call.message_id, call.block_index)]
        result_position = positions[(result.message_id, result.block_index)]
        if call_position >= result_position:
            raise ToolResultPairingError("tool result does not follow its call")
        pair_payload = {
            "tool_call_id": tool_call_id,
            "model_tool_name": result.model_tool_name,
            "call_message_id": call.message_id,
            "call_block_index": call.block_index,
            "result_message_id": result.message_id,
            "result_block_index": result.block_index,
            "call_sequence": call.sequence,
            "result_sequence": result.sequence,
            "pairing_status": result.pairing_status,
        }
        pairs.append(
            ToolInteractionPairFact(
                **pair_payload,
                pair_fingerprint=context_fingerprint(
                    "tool-interaction-pair:v1", pair_payload
                ),
            )
        )
        units.append(
            _tool_result_unit(
                pending=_PendingUnit(
                    result=result,
                    call=call,
                    call_position=call_position,
                    result_position=result_position,
                )
            )
        )

    pairs.sort(
        key=lambda pair: (
            positions[(pair.call_message_id, pair.call_block_index)],
            pair.tool_call_id,
        )
    )
    units = _reposition_tool_result_units(
        units=units,
        pairs=tuple(pairs),
        positions=positions,
    )

    transcript_payload = {
        "schema_version": "transcript-input:v1",
        "runtime_session_id": event_slice.runtime_session_id,
        "through_sequence": event_slice.through_sequence,
        "current_user_anchor": snapshot.current_user_message.message_id,
        "projection_window": snapshot.authority_slice_plan.transcript_window,
        "messages": tuple(messages),
        "tool_pairs": tuple(pairs),
        "compacted_windows": (compacted_window,) if compacted_window else (),
        "stripped_unfinished_call_ids": tuple(stripped_ids),
        "omitted_non_model_block_ids": tuple(omitted_ids),
    }
    transcript = TranscriptCompileInput(
        **transcript_payload,
        transcript_fingerprint=context_fingerprint(
            "transcript-compile-input:v1", transcript_payload
        ),
    )
    return NormalizedContextTranscript(
        transcript=transcript,
        tool_result_units=tuple(units),
    )


def _accepted_main_reply_ids(
    selected: tuple[tuple[FrozenStoredEvent, AgentEvent], ...],
) -> frozenset[str]:
    """Resolve the durable control disposition before projecting semantics.

    Provider stream completion is not control-plane acceptance.  A completed
    main call becomes model-visible only after its exact ACCEPTED disposition
    has committed; all other terminal outcomes and suppressed calls remain
    audit-only.  Missing or contradictory lifecycle facts are structural input
    corruption rather than a reason to guess from ReplyEnd or downstream data.
    """

    try:
        return accepted_main_reply_ids(tuple(event for _, event in selected))
    except MessageReplayControlError as exc:
        raise TranscriptNormalizationError(str(exc)) from exc


def _is_model_reply_stream_event(event: AgentEvent) -> bool:
    return isinstance(
        event,
        ReplyStartEvent
        | ReplyEndEvent
        | TextBlockStartEvent
        | TextBlockSegmentEvent
        | TextBlockEndEvent
        | ThinkingBlockStartEvent
        | ThinkingBlockSegmentEvent
        | ThinkingBlockEndEvent
        | DataBlockStartEvent
        | DataBlockSegmentEvent
        | DataBlockEndEvent
        | ToolCallStartEvent
        | ToolCallArgumentsSegmentEvent
        | ToolCallEndEvent
        | HintBlockEvent,
    )


def _validate_snapshot_slice(
    *,
    snapshot: ContextFactSnapshotFact | ContextTranscriptProjectionAuthority,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
) -> None:
    if event_slice.runtime_session_id != snapshot.identity.runtime_session_id:
        raise ContextEventSliceError("transcript event-slice owner mismatch")
    if event_slice.to_range_fact() != snapshot.primary_event_range:
        raise ContextEventSliceError("transcript event slice does not match snapshot")
    expected_local = tuple(
        item
        for item in getattr(snapshot, "named_event_ranges", ())
        if item.runtime_session_id == snapshot.identity.runtime_session_id
    )
    actual_local = (
        event_slice.named_range_facts()
        if isinstance(event_slice, ContextEventAuthorityView)
        else ()
    )
    if actual_local != expected_local:
        raise ContextEventSliceError(
            "transcript named authority ranges do not match snapshot"
        )


def _prior_lifecycle_messages(
    *,
    snapshot: ContextFactSnapshotFact,
    event_slice: ContextEventSlice,
    decoded: tuple[tuple[FrozenStoredEvent, AgentEvent], ...],
) -> tuple[TranscriptMessageFact, ...]:
    """Rebuild one-shot lifecycle notes from the run-frozen prior watermark."""

    run_entry = getattr(snapshot, "run_entry", None)
    transcript_basis = getattr(
        getattr(run_entry, "run_entry", None), "transcript", None
    )
    prior_through = getattr(transcript_basis, "source_through_sequence", 0)
    if not isinstance(prior_through, int) or prior_through <= 0:
        return ()
    prior = tuple(
        (stored, event) for stored, event in decoded if stored.sequence <= prior_through
    )
    events = [event for _, event in prior]
    notes: list[TranscriptMessageFact] = []
    recovery = project_recovery_from_events(events)
    last_terminal = next(
        (
            (stored, event)
            for stored, event in reversed(prior)
            if isinstance(event, RunEndEvent) and event.status in {"failed", "aborted"}
        ),
        None,
    )
    if recovery is not None and last_terminal is not None:
        stored, terminal = last_terminal
        prefix = RECOVERY_NOTE_ID_PREFIX_BY_STATUS.get(
            terminal.status,
            "recovery-note",
        )
        notes.append(
            _lifecycle_note_message(
                message_id=f"{prefix}:{terminal.run_id}",
                text=render_recovery_text(recovery, audience="transcript"),
                stored=stored,
                event=terminal,
                event_slice=event_slice,
            )
        )

    last_run_start_sequence = max(
        (
            stored.sequence
            for stored, event in prior
            if isinstance(event, RunStartEvent)
        ),
        default=0,
    )
    completions = tuple(
        (stored, event)
        for stored, event in prior
        if isinstance(event, TerminalProcessCompletedEvent)
        and stored.sequence > last_run_start_sequence
        and event.created_at <= snapshot.current_user_message.observed_at_utc
    )
    if completions:
        selected = completions[:3]
        lines = tuple(_terminal_completion_note_line(event) for _, event in selected)
        remaining = len(completions) - len(selected)
        suffix = (
            f" {remaining} more terminal task(s) completed; use terminal_process "
            "list if still retained."
            if remaining > 0
            else ""
        )
        latest_stored, latest = completions[-1]
        notes.append(
            _lifecycle_note_message(
                message_id=(f"terminal-completion-note:{latest_stored.sequence}"),
                text=(
                    "Pulsara note: terminal background task update. "
                    + " ".join(lines)
                    + suffix
                ),
                stored=latest_stored,
                event=latest,
                event_slice=event_slice,
            )
        )
    return tuple(notes)


def _lifecycle_note_message(
    *,
    message_id: str,
    text: str,
    stored: FrozenStoredEvent,
    event: AgentEvent,
    event_slice: ContextEventSlice,
) -> TranscriptMessageFact:
    ref = stored.to_reference(event_slice.runtime_session_id)
    block = TranscriptTextBlockFact(
        block_id=f"{message_id}:text",
        text=text,
        content_fingerprint=context_fingerprint("transcript-text:v1", text),
        source_events=(ref,),
    )
    return _message_fact(
        message_id=message_id,
        role="runtime_observation",
        name="pulsara",
        run_id=event.run_id,
        turn_id=event.turn_id,
        reply_id=event.reply_id,
        created_at_utc=event.created_at,
        finished_at_utc=event.created_at,
        segment="terminal_lifecycle_note",
        blocks=(block,),
        source_sequence_start=stored.sequence,
        source_sequence_end=stored.sequence,
    )


def _terminal_completion_note_line(event: TerminalProcessCompletedEvent) -> str:
    return (
        f"Process {event.process_id} completed with status {event.status} "
        f"and exit code {event.exit_code}. This note is lifecycle-only, not the "
        "full output; if still retained, inspect retained output with "
        "terminal_process log."
    )


def _sequence_is_model_visible(sequence: int, window) -> bool:
    if window.window_kind == "window_compaction":
        # Exact message retention is applied after complete normalization. This
        # keeps provider tool pairing reconstructible without making sequence
        # ranges a second compaction authority.
        return True
    retained = (
        window.retained_history_from_sequence,
        window.retained_history_through_sequence,
    )
    in_retained = (
        retained[0] is not None
        and retained[1] is not None
        and retained[0] <= sequence <= retained[1]
    )
    return in_retained or (
        window.protected_run_start_sequence
        <= sequence
        <= window.protected_run_through_sequence
    )


def _current_run_start(
    *,
    snapshot: ContextFactSnapshotFact,
    by_id: dict[str, tuple[FrozenStoredEvent, AgentEvent]],
) -> FrozenStoredEvent:
    pair = by_id.get(snapshot.run_entry.run_start.event_id)
    if pair is None or not isinstance(pair[1], RunStartEvent):
        raise TranscriptNormalizationError(
            "snapshot RunStart is absent from authority slice"
        )
    stored, event = pair
    if event.current_user_message != snapshot.current_user_message:
        raise TranscriptNormalizationError(
            "snapshot current user differs from RunStart"
        )
    return stored


def _validate_descriptor_attributions(
    *,
    snapshot: ContextFactSnapshotFact,
    by_id: dict[str, tuple[FrozenStoredEvent, AgentEvent]],
    events: Iterable[AgentEvent],
) -> None:
    profiles = []
    for event in events:
        if isinstance(event, ToolResultEndEvent):
            profiles.append(event.render_profile)
        elif isinstance(event, ExternalExecutionResultEvent):
            profiles.extend(
                item.execution_semantics.render_profile
                for item in event.external_results
            )
    for profile in profiles:
        attribution = profile.descriptor_attribution
        if attribution is None:
            if profile.tool_origin != "unknown":
                raise TranscriptNormalizationError(
                    "known tool result lacks descriptor attribution"
                )
            continue
        if attribution.owner_runtime_session_id != snapshot.identity.runtime_session_id:
            raise TranscriptNormalizationError("descriptor attribution owner mismatch")
        source_pair = by_id.get(attribution.descriptor_source_event_id)
        if source_pair is None or not isinstance(
            source_pair[1], CapabilityExposureResolvedEvent
        ):
            raise TranscriptNormalizationError(
                "descriptor source exposure is absent from authority slice"
            )
        source_stored, exposure_event = source_pair
        exposure = exposure_event.exposure
        if (
            source_stored.sequence,
            source_stored.payload_fingerprint,
            exposure.exposure_id,
            exposure.exposure_fact_fingerprint,
            exposure.semantic.execution_surface.descriptor_set_fingerprint,
        ) != (
            attribution.descriptor_source_sequence,
            attribution.descriptor_source_payload_fingerprint,
            attribution.exposure_id,
            attribution.exposure_fact_fingerprint,
            attribution.descriptor_set_fingerprint,
        ):
            raise TranscriptNormalizationError(
                "descriptor exposure attribution mismatch"
            )
        matching = tuple(
            entry
            for entry in exposure.semantic.execution_surface.entries
            if entry.descriptor_id == attribution.descriptor_id
        )
        if len(matching) != 1 or matching[0].descriptor_fingerprint != (
            attribution.descriptor_fingerprint
        ):
            raise TranscriptNormalizationError(
                "descriptor identity is not in source exposure"
            )


def _completion_source_refs(
    *,
    event_slice: ContextEventSlice,
    completion: BlockCompletion,
    decoded_by_sequence: dict[int, tuple[FrozenStoredEvent, AgentEvent]],
) -> tuple[ContextEventReferenceFact, ...]:
    if completion.start_sequence is None or completion.end_sequence is None:
        raise TranscriptNormalizationError(
            "completed block lacks durable sequence span"
        )
    refs: list[ContextEventReferenceFact] = []
    for sequence in range(completion.start_sequence, completion.end_sequence + 1):
        pair = decoded_by_sequence.get(sequence)
        if pair is None:
            raise TranscriptNormalizationError(
                "completed block source sequence is absent"
            )
        stored, event = pair
        if event.reply_id != completion.reply_id:
            continue
        if _event_matches_block(event, completion):
            refs.append(stored.to_reference(event_slice.runtime_session_id))
    if (
        not refs
        or refs[0].event_id != completion.start_event_id
        or refs[-1].event_id != completion.end_event_id
    ):
        raise TranscriptNormalizationError("completed block source span is incomplete")
    return tuple(refs)


def _event_matches_block(event: AgentEvent, completion: BlockCompletion) -> bool:
    if completion.block_type == "text":
        return (
            isinstance(
                event, TextBlockStartEvent | TextBlockSegmentEvent | TextBlockEndEvent
            )
            and event.block_id == completion.block_id
        )
    if completion.block_type == "thinking":
        return (
            isinstance(
                event,
                ThinkingBlockStartEvent
                | ThinkingBlockSegmentEvent
                | ThinkingBlockEndEvent,
            )
            and event.block_id == completion.block_id
        )
    if completion.block_type == "data":
        return (
            isinstance(
                event, DataBlockStartEvent | DataBlockSegmentEvent | DataBlockEndEvent
            )
            and event.block_id == completion.block_id
        )
    if completion.block_type == "tool_call":
        return (
            isinstance(
                event,
                ToolCallStartEvent
                | ToolCallArgumentsSegmentEvent
                | ToolCallEndEvent,
            )
            and event.tool_call_id == completion.block_id
        )
    if completion.block_type == "tool_result":
        return (
            isinstance(
                event,
                ToolResultStartEvent
                | ToolResultTextDeltaEvent
                | ToolResultDataDeltaEvent
                | ToolResultEndEvent,
            )
            and event.tool_call_id == completion.block_id
        )
    if completion.block_type == "hint":
        return (
            isinstance(event, HintBlockEvent) and event.block_id == completion.block_id
        )
    return False


def _validate_stream_completeness(
    *,
    assembler: BlockAssembler,
    current_run_start_sequence: int,
    tool_call_starts: dict[str, tuple[FrozenStoredEvent, ToolCallStartEvent]],
    tool_call_ends: dict[str, tuple[FrozenStoredEvent, ToolCallEndEvent]],
    tool_result_starts: dict[str, tuple[FrozenStoredEvent, ToolResultStartEvent]],
    tool_result_ends: dict[str, tuple[FrozenStoredEvent, ToolResultEndEvent]],
    external_result_ids: set[str],
    selected: tuple[tuple[FrozenStoredEvent, AgentEvent], ...],
) -> tuple[str, ...]:
    run_ends = {
        event.run_id: event for _, event in selected if isinstance(event, RunEndEvent)
    }
    missing_call_starts = set(tool_call_ends) - set(tool_call_starts)
    if missing_call_starts:
        raise ToolResultPairingError("tool call terminal is missing its start")
    missing_result_starts = set(tool_result_ends) - set(tool_result_starts)
    if missing_result_starts:
        raise ToolResultPairingError("tool result terminal is missing its start")
    missing_result_ends = set(tool_result_starts) - set(tool_result_ends)
    if missing_result_ends:
        raise ToolResultPairingError("tool result start is missing its terminal")
    historical_unfinished: list[str] = []
    for tool_call_id in set(tool_call_starts) - set(tool_call_ends):
        stored, event = tool_call_starts[tool_call_id]
        terminal = run_ends.get(event.run_id)
        if not (
            stored.sequence < current_run_start_sequence
            and terminal is not None
            and terminal.status in {"failed", "aborted"}
        ):
            raise ToolResultPairingError("active tool call is unfinished at compile")
        historical_unfinished.append(tool_call_id)
    # A crashed historical run may have durably closed the provider-native
    # tool-call block without ever producing a result (or even a reply
    # envelope). Recovery terminalizes that run; the incomplete call is then a
    # deterministic strip, not active context corruption.
    completed_result_ids = set(tool_result_ends) | external_result_ids
    for tool_call_id in set(tool_call_ends) - completed_result_ids:
        stored, event = tool_call_starts[tool_call_id]
        terminal = run_ends.get(event.run_id)
        if not (
            stored.sequence < current_run_start_sequence
            and terminal is not None
            and terminal.status in {"failed", "aborted"}
        ):
            raise ToolResultPairingError(
                "active/completed tool call has no result at compile"
            )
        historical_unfinished.append(tool_call_id)
    # Remaining active blocks can only be historical unfinished tool calls that
    # the projector audits and strips. Any other active block is corrupt input.
    allowed_active = len(set(tool_call_starts) - set(tool_call_ends))
    if assembler.active_count() != allowed_active:
        raise TranscriptNormalizationError(
            "event slice contains unfinished content blocks"
        )
    return tuple(sorted(set(historical_unfinished)))


def _compaction_summary_message(
    *,
    snapshot: ContextFactSnapshotFact | ContextTranscriptProjectionAuthority,
    event_slice: ContextEventSlice,
    compaction_summary_text: str | None,
    window_compaction_source_document: WindowCompactionSourceDocumentFact | None,
) -> tuple[TranscriptMessageFact | None, CompactedWindowReferenceFact | None]:
    window = snapshot.authority_slice_plan.transcript_window
    if window.compaction_terminal_ref is None:
        if (
            compaction_summary_text is not None
            or window_compaction_source_document is not None
        ):
            raise TranscriptNormalizationError(
                "uncompacted transcript cannot receive compaction summary text"
            )
        return None, None
    if compaction_summary_text is None:
        raise TranscriptNormalizationError(
            "compacted transcript requires prepared summary artifact text"
        )
    stored = event_slice.event_by_id(window.compaction_terminal_ref.event_id)
    event = stored.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    prefix = isinstance(event, ContextCompactionCompletedEvent)
    same_run = isinstance(event, ContextWindowCompactionCompletedEvent)
    if not prefix and not same_run:
        raise TranscriptNormalizationError("compaction window terminal type mismatch")
    if prefix and window_compaction_source_document is not None:
        raise TranscriptNormalizationError(
            "prefix compaction cannot receive a window source document"
        )
    if same_run:
        _validate_window_compaction_source_document(
            snapshot=snapshot,
            event_slice=event_slice,
            completed=event,
            source_document=window_compaction_source_document,
        )
    summary_message_id = (
        f"compaction-summary:{event.compaction_id}"
        if prefix
        else f"window-compaction-summary:{event.compaction_id}"
    )
    source_ref = stored.to_reference(event_slice.runtime_session_id)
    block = TranscriptTextBlockFact(
        block_id=f"text:{summary_message_id}",
        text=compaction_summary_text,
        content_fingerprint=context_fingerprint(
            "transcript-text:v1", compaction_summary_text
        ),
        source_events=(source_ref,),
    )
    message = _message_fact(
        message_id=summary_message_id,
        role="runtime_observation",
        name="pulsara_compaction" if prefix else "pulsara_window_compaction",
        run_id=None,
        turn_id=None,
        reply_id=None,
        created_at_utc=event.created_at,
        finished_at_utc=event.created_at,
        segment="compaction_summary",
        blocks=(block,),
        source_sequence_start=stored.sequence,
        source_sequence_end=stored.sequence,
    )
    return message, CompactedWindowReferenceFact(
        compaction_kind="prefix" if prefix else "window",
        compaction_id=event.compaction_id,
        summary_artifact_id=event.summary_artifact_id,
        compacted_through_sequence=(
            event.through_sequence
            if prefix
            else snapshot.authority_slice_plan.transcript_window.compacted_through_sequence
        ),
        keep_after_sequence=event.keep_after_sequence if prefix else None,
        summary_message_id=summary_message_id,
        source_event=source_ref,
        source_started_event=(
            snapshot.authority_slice_plan.transcript_window.window_compaction_started_ref
            if same_run
            else None
        ),
    )


def _validate_window_compaction_source_document(
    *,
    snapshot: ContextFactSnapshotFact | ContextTranscriptProjectionAuthority,
    event_slice: ContextEventSlice,
    completed: ContextWindowCompactionCompletedEvent,
    source_document: WindowCompactionSourceDocumentFact | None,
) -> None:
    window = snapshot.authority_slice_plan.transcript_window
    if window.window_kind != "window_compaction":
        raise TranscriptNormalizationError("window terminal used by a prefix projection")
    if source_document is None:
        raise TranscriptNormalizationError(
            "window compaction requires its source document artifact"
        )
    started_ref = window.window_compaction_started_ref
    if started_ref is None:
        raise TranscriptNormalizationError("window compaction lacks Started reference")
    stored = event_slice.event_by_id(started_ref.event_id)
    started = stored.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    if not isinstance(started, ContextWindowCompactionStartedEvent):
        raise TranscriptNormalizationError("window compaction Started type mismatch")
    plan = started.plan
    if (
        completed.started_event_id != started.id
        or completed.plan_fingerprint != plan.plan_fingerprint
        or source_document.compaction_id != plan.compaction_id
        or source_document.run_id != plan.run_id
        or source_document.source_window_id != plan.source_window_id
        or source_document.source_projection_generation
        != plan.source_projection_generation
        or source_document.source_through_sequence != plan.source_through_sequence
        or source_document.document_fingerprint != plan.source_document_fingerprint
        or source_document.summarized_message_ids != plan.summarized_message_ids
        or source_document.retained_message_ids != plan.retained_message_ids
        or source_document.summarized_pair_group_ids
        != plan.summarized_pair_group_ids
        or source_document.retained_pair_group_ids != plan.retained_pair_group_ids
    ):
        raise TranscriptNormalizationError(
            "window compaction source document differs from its durable plan"
        )


def _apply_window_compaction_projection(
    *,
    messages: list[TranscriptMessageFact],
    calls: dict[str, _CallProjection],
    results: dict[str, _ResultProjection],
    snapshot: ContextFactSnapshotFact | ContextTranscriptProjectionAuthority,
    source_document: WindowCompactionSourceDocumentFact | None,
) -> tuple[
    list[TranscriptMessageFact],
    dict[str, _CallProjection],
    dict[str, _ResultProjection],
    tuple[ToolInteractionPairFact, ...],
    tuple[ToolResultRenderUnit, ...],
]:
    window = snapshot.authority_slice_plan.transcript_window
    if window.window_kind != "window_compaction":
        if source_document is not None:
            raise TranscriptNormalizationError(
                "non-window projection received a window source document"
            )
        return messages, calls, results, (), ()
    if source_document is None:
        raise TranscriptNormalizationError("window compaction source document missing")
    through = window.compacted_through_sequence
    if through is None:
        raise TranscriptNormalizationError("window compaction high-water missing")
    baseline = parse_window_compaction_transcript_baseline(
        source_document.retained_transcript_baseline
    )
    if (
        baseline.compaction_id != source_document.compaction_id
        or baseline.run_id != source_document.run_id
        or baseline.source_window_id != source_document.source_window_id
        or baseline.source_through_sequence != source_document.source_through_sequence
        or tuple(item.message_id for item in baseline.retained_messages)
        != source_document.retained_message_ids
    ):
        raise TranscriptNormalizationError(
            "window compaction transcript baseline differs from source document"
        )
    retained = set(window.retained_message_ids)
    summarized = set(window.summarized_message_ids)
    reconstructed_retained = tuple(
        message for message in messages if message.message_id in retained
    )
    if reconstructed_retained and reconstructed_retained != baseline.retained_messages:
        raise TranscriptNormalizationError(
            "window compaction retained message differs from durable baseline"
        )
    kept_messages = [
        message
        for message in messages
        if message.segment == "compaction_summary"
        or message.source_sequence_start > through
    ]
    summary_count = sum(
        message.segment == "compaction_summary" for message in kept_messages
    )
    kept_messages[summary_count:summary_count] = list(baseline.retained_messages)
    if any(message.message_id in summarized for message in kept_messages):
        raise TranscriptNormalizationError("summarized message leaked into new window")
    kept_ids = {
        message.message_id
        for message in kept_messages
        if message.message_id not in retained
    }
    kept_calls = {
        tool_call_id: call
        for tool_call_id, call in calls.items()
        if call.message_id in kept_ids
    }
    kept_results = {
        tool_call_id: result
        for tool_call_id, result in results.items()
        if result.message_id in kept_ids
    }
    if set(kept_calls) != set(kept_results):
        raise ToolResultPairingError(
            "window compaction retained only one side of a tool interaction"
        )
    return (
        kept_messages,
        kept_calls,
        kept_results,
        baseline.retained_tool_pairs,
        baseline.retained_tool_result_units,
    )


def _reposition_tool_result_units(
    *,
    units: list[ToolResultRenderUnit],
    pairs: tuple[ToolInteractionPairFact, ...],
    positions: dict[tuple[str, int], int],
) -> list[ToolResultRenderUnit]:
    pair_by_call = {
        (pair.call_message_id, pair.tool_call_id): pair for pair in pairs
    }
    repositioned: list[ToolResultRenderUnit] = []
    for unit in units:
        pair = pair_by_call.get((unit.call_message_id, unit.tool_call_id))
        if pair is None:
            raise TranscriptNormalizationError(
                "tool result unit lacks its normalized interaction pair"
            )
        payload = unit.model_dump(mode="python", exclude={"unit_fingerprint"})
        payload.update(
            call_position=positions[
                (pair.call_message_id, pair.call_block_index)
            ],
            result_position=positions[
                (pair.result_message_id, pair.result_block_index)
            ],
        )
        repositioned.append(
            ToolResultRenderUnit(
                **payload,
                unit_fingerprint=context_fingerprint(
                    "tool-result-render-unit:v1", payload
                ),
            )
        )
    repositioned.sort(key=lambda item: (item.result_position, item.unit_id))
    return repositioned


def _user_message(
    *,
    event_slice: ContextEventSlice,
    stored: FrozenStoredEvent,
    event: RunStartEvent,
    current_start_sequence: int,
    current_message_id: str,
) -> TranscriptMessageFact:
    is_current = stored.sequence == current_start_sequence
    if is_current and event.current_user_message.message_id != current_message_id:
        raise TranscriptNormalizationError("current user anchor mismatch")
    source_ref = stored.to_reference(event_slice.runtime_session_id)
    block = TranscriptTextBlockFact(
        block_id=f"text:{event.current_user_message.message_id}",
        text=event.current_user_message.text,
        content_fingerprint=context_fingerprint(
            "transcript-text:v1", event.current_user_message.text
        ),
        source_events=(source_ref,),
    )
    return _message_fact(
        message_id=event.current_user_message.message_id,
        role="user",
        name="user",
        run_id=event.run_id,
        turn_id=event.turn_id,
        reply_id=event.reply_id,
        created_at_utc=event.current_user_message.observed_at_utc,
        finished_at_utc=event.current_user_message.observed_at_utc,
        segment="current_user" if is_current else "prior_history",
        blocks=(block,),
        source_sequence_start=stored.sequence,
        source_sequence_end=stored.sequence,
    )


def _transcript_block(
    *, block, refs: tuple[ContextEventReferenceFact, ...]
) -> TranscriptBlockFact:
    if isinstance(block, TextBlock):
        return TranscriptTextBlockFact(
            block_id=block.id,
            text=block.text,
            content_fingerprint=context_fingerprint("transcript-text:v1", block.text),
            source_events=refs,
        )
    if isinstance(block, ThinkingBlock):
        return TranscriptThinkingBlockFact(
            block_id=block.id,
            thinking=block.thinking,
            content_fingerprint=context_fingerprint(
                "transcript-thinking:v1", block.thinking
            ),
            source_events=refs,
        )
    if isinstance(block, DataBlock):
        source = block.source
        return TranscriptDataPlaceholderFact(
            block_id=block.id,
            name=block.name,
            media_type=source.media_type,
            source_kind="base64" if isinstance(source, Base64Source) else "url",
            artifact_ids=(),
            source_events=refs,
        )
    raise TranscriptNormalizationError(
        f"unsupported transcript block type: {type(block).__name__}"
    )


def _tool_call_fact(
    *, block: ToolCallBlock, refs: tuple[ContextEventReferenceFact, ...]
) -> TranscriptToolCallFact:
    raw = block.input
    parsed: FrozenJsonObjectFact | None = None
    status: str
    error: ToolArgumentsParseErrorCode | None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        status = "invalid_json"
        error = ToolArgumentsParseErrorCode.INVALID_JSON_SYNTAX
    else:
        if isinstance(value, dict):
            frozen = freeze_json(value)
            if not isinstance(frozen, FrozenJsonObjectFact):
                raise AssertionError("JSON object did not freeze as object fact")
            parsed = frozen
            status = "valid_object"
            error = None
        else:
            status = "non_object_json"
            error = ToolArgumentsParseErrorCode.JSON_ROOT_NOT_OBJECT
    return TranscriptToolCallFact(
        tool_call_id=block.id,
        model_tool_name=block.name,
        raw_arguments_json=raw,
        arguments_status=status,
        parsed_arguments=parsed,
        parse_error_code=error,
        state="finished",
        source_events=refs,
    )


def _message_fact(**payload) -> TranscriptMessageFact:
    return TranscriptMessageFact(
        **payload,
        message_fingerprint=context_fingerprint("transcript-message:v1", payload),
    )


def _result_projection(
    *,
    block: ToolResultBlock,
    observation_timing: ToolObservationTimingFact,
    semantics: ToolResultExecutionSemanticsFact,
    source_refs: tuple[ContextEventReferenceFact, ...],
    result_sequence: int,
    pairing_status: str,
) -> _ResultProjection:
    if block.state.value != semantics.result_state.value:
        raise ToolResultPairingError("tool result block/semantics state mismatch")
    unit_id = f"tool-result-unit:{block.id}:{source_refs[-1].event_id}"
    return _ResultProjection(
        unit_id=unit_id,
        tool_call_id=block.id,
        model_tool_name=block.name,
        message_id=f"tool-result-message:{block.id}:{source_refs[-1].event_id}",
        block_index=0,
        sequence=result_sequence,
        pairing_status=pairing_status,
        block=block,
        observation_timing=observation_timing,
        semantics=semantics,
        source_refs=source_refs,
    )


def _append_result_message(
    *,
    messages: list[TranscriptMessageFact],
    results: dict[str, _ResultProjection],
    result: _ResultProjection,
    current_run_start_sequence: int,
) -> None:
    if result.tool_call_id in results:
        raise ToolResultPairingError("tool call has more than one result")
    results[result.tool_call_id] = result
    ref_block = TranscriptToolResultRefFact(
        tool_call_id=result.tool_call_id,
        tool_result_unit_id=result.unit_id,
        source_events=result.source_refs,
    )
    messages.append(
        _message_fact(
            message_id=result.message_id,
            role="assistant",
            name=result.model_tool_name,
            run_id=None,
            turn_id=None,
            reply_id=None,
            created_at_utc=None,
            finished_at_utc=None,
            segment=(
                "prior_history"
                if result.sequence < current_run_start_sequence
                else "current_run_tail"
            ),
            blocks=(ref_block,),
            source_sequence_start=result.source_refs[0].sequence,
            source_sequence_end=result.source_refs[-1].sequence,
        )
    )


def _sort_messages(
    messages: list[TranscriptMessageFact],
    *,
    results: dict[str, _ResultProjection],
) -> list[TranscriptMessageFact]:
    summaries = [
        message for message in messages if message.segment == "compaction_summary"
    ]
    regular = [
        message for message in messages if message.segment != "compaction_summary"
    ]
    regular.sort(
        key=lambda item: (
            item.source_sequence_start,
            item.source_sequence_end,
            item.message_id,
        )
    )
    result_message_ids = {result.message_id for result in results.values()}
    result_messages = {
        message.message_id: message
        for message in regular
        if message.message_id in result_message_ids
    }
    ordered: list[TranscriptMessageFact] = []
    for message in regular:
        if message.message_id in result_message_ids:
            continue
        ordered.append(message)
        message_results = [
            (block_index, results[block.tool_call_id])
            for block_index, block in enumerate(message.blocks)
            if isinstance(block, TranscriptToolCallFact)
            and block.tool_call_id in results
        ]
        has_pre_execution_result = any(
            result.semantics.render_profile.selected_variant.execution_phase
            == "pre_execution"
            for _, result in message_results
        )
        message_results.sort(
            key=(
                (lambda item: (item[1].sequence, item[0]))
                if has_pre_execution_result
                else (lambda item: (item[0], item[1].sequence))
            )
        )
        ordered.extend(
            result_messages[result.message_id] for _, result in message_results
        )
    unattached = result_message_ids - {message.message_id for message in ordered}
    if unattached:
        raise ToolResultPairingError(
            "tool result messages could not be ordered after calls"
        )
    return [*summaries, *ordered]


def _tool_result_unit(*, pending: _PendingUnit) -> ToolResultRenderUnit:
    result = pending.result
    semantics = result.semantics
    content = _tool_result_content(result.block, result.source_refs)
    artifacts = tuple(_artifact_fact(item) for item in result.block.artifacts)
    source_refs = tuple(
        sorted(
            {
                ref.event_id: ref
                for ref in (*pending.call.fact.source_events, *result.source_refs)
            }.values(),
            key=lambda item: item.sequence,
        )
    )
    payload = {
        "schema_version": "tool-result-unit:v1",
        "unit_id": result.unit_id,
        "tool_call_id": result.tool_call_id,
        "model_tool_name": result.model_tool_name,
        "descriptor_attribution": semantics.render_profile.descriptor_attribution,
        "render_contract_fingerprint": semantics.render_profile.render_contract_fingerprint,
        "render_variant_fingerprint": semantics.render_profile.selected_variant.variant_fingerprint,
        "call_message_id": pending.call.message_id,
        "result_message_id": result.message_id,
        "call_position": pending.call_position,
        "result_position": pending.result_position,
        "result_state": semantics.result_state,
        "content": content,
        "artifacts": artifacts,
        "observation_timing": result.observation_timing,
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


def _tool_result_content(
    block: ToolResultBlock,
    refs: tuple[ContextEventReferenceFact, ...],
) -> ToolResultContentFact:
    text_blocks: list[ToolResultTextContentFact] = []
    data_blocks: list[ToolResultDataContentFact] = []
    artifact_ids = tuple(item.artifact_id for item in block.artifacts)
    source_identity = refs[-1].event_id
    for output_index, output in enumerate(block.output):
        if isinstance(output, TextBlock):
            text_blocks.append(
                ToolResultTextContentFact(
                    block_id=(
                        f"tool-result-text:{block.id}:{source_identity}:{output_index}"
                    ),
                    text=output.text,
                    chars=len(output.text),
                    content_fingerprint=context_fingerprint(
                        "tool-result-text:v1", output.text
                    ),
                    source_events=refs,
                )
            )
        elif isinstance(output, DataBlock):
            source = output.source
            data_blocks.append(
                ToolResultDataContentFact(
                    block_id=(
                        f"tool-result-data:{block.id}:{source_identity}:{output_index}"
                    ),
                    name=output.name,
                    media_type=source.media_type,
                    source_kind=(
                        "base64" if isinstance(source, Base64Source) else "url"
                    ),
                    artifact_ids=artifact_ids,
                    source_events=refs,
                )
            )
    payload = {
        "text_blocks": tuple(text_blocks),
        "data_blocks": tuple(data_blocks),
    }
    return ToolResultContentFact(
        **payload,
        content_fingerprint=context_fingerprint("tool-result-content:v1", payload),
    )


def _artifact_fact(artifact: ToolResultArtifactRef) -> ContextToolResultArtifactRefFact:
    preview = None
    if artifact.preview is not None:
        frozen_read_more = freeze_json(artifact.preview.read_more)
        if not isinstance(frozen_read_more, FrozenJsonObjectFact):
            raise TypeError("artifact read_more must freeze as an object")
        preview = ContextToolResultPreviewFact(
            preview_policy=artifact.preview.preview_policy,
            preview_chars=artifact.preview.preview_chars,
            original_chars=artifact.preview.original_chars,
            original_bytes=artifact.preview.original_bytes,
            omitted_middle_chars=artifact.preview.omitted_middle_chars,
            visible_head_chars=artifact.preview.visible_head_chars,
            visible_tail_chars=artifact.preview.visible_tail_chars,
            read_more=frozen_read_more,
        )
    payload = {
        "artifact_id": artifact.artifact_id,
        "role": artifact.role,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "stored_complete": artifact.stored_complete,
        "loss_reason": artifact.loss_reason,
        "preview": preview,
    }
    return ContextToolResultArtifactRefFact(
        **payload,
        ref_fingerprint=context_fingerprint(
            "context-tool-result-artifact-ref:v1", payload
        ),
    )


def _validate_external_ingress_requirement(
    *,
    event_slice: ContextEventSlice,
    by_id: dict[str, tuple[FrozenStoredEvent, AgentEvent]],
    ingress: ExternalToolResultIngressFact,
    block: ToolResultBlock,
) -> None:
    ref = ingress.requirement_ref
    pair = by_id.get(ref.require_event_id)
    if pair is None:
        raise ToolResultPairingError(
            "external requirement event is outside authority slice"
        )
    stored, event = pair
    from pulsara_agent.event.events import RequireExternalExecutionEvent

    if not isinstance(event, RequireExternalExecutionEvent):
        raise ToolResultPairingError("external requirement reference has wrong type")
    if (
        stored.sequence,
        stored.payload_fingerprint,
        ref.owner_runtime_session_id,
    ) != (
        ref.require_event_sequence,
        ref.require_event_payload_fingerprint,
        event_slice.runtime_session_id,
    ):
        raise ToolResultPairingError("external requirement reference mismatch")
    requirements = tuple(
        item
        for item in event.external_tool_calls
        if item.tool_call_id == ref.tool_call_id
    )
    if len(requirements) != 1:
        raise ToolResultPairingError("external result has no unique requirement")
    requirement = requirements[0]
    if (
        requirement.requirement_fingerprint,
        requirement.model_tool_name,
        block.id,
        block.name,
    ) != (
        ref.requirement_fingerprint,
        block.name,
        requirement.tool_call_id,
        requirement.model_tool_name,
    ):
        raise ToolResultPairingError("external result/requirement identity mismatch")
    profile = ingress.execution_semantics.render_profile
    if (
        profile.descriptor_attribution != requirement.descriptor_attribution
        or profile.render_contract_fingerprint
        != requirement.result_render_contract.contract_fingerprint
        or profile.selected_variant
        not in requirement.result_render_contract.allowed_variants
    ):
        raise ToolResultPairingError(
            "external result semantics differ from requirement"
        )


__all__ = [
    "NormalizedContextTranscript",
    "ToolResultPairingError",
    "TranscriptNormalizationError",
    "project_context_transcript",
]
