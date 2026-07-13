"""Typed event-ledger helpers for context-input contract tests."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.event import RunStartEvent
from pulsara_agent.event_log.protocol import EventLog
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1
from pulsara_agent.primitives.context import ContextRunEntryReferenceFact
from pulsara_agent.runtime.context_input import (
    ContextEventSlice,
    LoweredTranscriptMessages,
    finalize_context_authority_slice_plan,
    lower_transcript_for_context,
    prepare_tool_result_render_input,
    project_context_transcript,
    render_prepared_tool_result_units,
    resolve_context_compile_policy,
)
from pulsara_agent.runtime.context_input.render import PreparedToolResultRenderOutput
from pulsara_agent.runtime.context_input.transcript import (
    ContextTranscriptProjectionAuthority,
    NormalizedContextTranscript,
    TranscriptProjectionIdentity,
)
from pulsara_agent.runtime.state import LoopBudget


@dataclass(frozen=True, slots=True)
class RenderedEventTranscript:
    event_slice: ContextEventSlice
    normalized: NormalizedContextTranscript
    rendered: PreparedToolResultRenderOutput
    lowered: LoweredTranscriptMessages


def render_event_log_transcript(
    event_log: EventLog,
    *,
    run_start_event_id: str,
    runtime_session_id: str = "runtime:test",
    budget: LoopBudget | None = None,
) -> RenderedEventTranscript:
    read = event_log.read_range_snapshot(minimum_sequence=1)
    full = ContextEventSlice.from_read_snapshot(
        runtime_session_id=runtime_session_id,
        minimum_sequence=1,
        snapshot=read,
    )
    start_stored = full.event_by_id(run_start_event_id)
    start = start_stored.decode_owned()
    if not isinstance(start, RunStartEvent):
        raise TypeError("typed context fixture requires a RunStart event")
    entry = start.new_run_boundary or start.subagent_run_entry
    if entry is None:
        raise ValueError("typed context fixture RunStart lacks a run entry")
    start_ref = start_stored.to_reference(runtime_session_id)
    run_entry = ContextRunEntryReferenceFact(
        run_entry_kind="host" if start.new_run_boundary is not None else "subagent",
        run_start=start_ref,
        stable_terminal_event_id=start.terminal_run_end_event_id,
        run_entry=entry,
    )
    plan = finalize_context_authority_slice_plan(
        event_slice=full,
        required_local_event_refs=(start_ref,),
        run_start_ref=start_ref,
        latest_compaction_terminal_ref=None,
    )
    authority_slice = full.subslice(from_sequence=plan.authority_from_sequence)
    authority = ContextTranscriptProjectionAuthority(
        identity=TranscriptProjectionIdentity(runtime_session_id=runtime_session_id),
        run_entry=run_entry,
        current_user_message=start.current_user_message,
        authority_slice_plan=plan,
        primary_event_range=authority_slice.to_range_fact(),
    )
    normalized = project_context_transcript(
        snapshot=authority,
        event_slice=authority_slice,
    )
    policy = resolve_context_compile_policy(budget or LoopBudget())
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=policy.tool_result_basis,
    )
    rendered = render_prepared_tool_result_units(
        prepared=prepared,
        transcript=normalized.transcript,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )
    lowered = lower_transcript_for_context(
        transcript=normalized.transcript,
        rendered_tool_results=rendered,
    )
    return RenderedEventTranscript(
        event_slice=authority_slice,
        normalized=normalized,
        rendered=rendered,
        lowered=lowered,
    )


__all__ = ["RenderedEventTranscript", "render_event_log_transcript"]
