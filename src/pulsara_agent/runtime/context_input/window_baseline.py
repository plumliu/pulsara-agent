"""Durable normalized transcript baseline carried by a compacted window."""

from __future__ import annotations

from pydantic import model_validator

from pulsara_agent.primitives._context_base import FrozenContextFact, context_fingerprint
from pulsara_agent.primitives.context import (
    TranscriptCompileInput,
    TranscriptMessageFact,
    ToolInteractionPairFact,
    thaw_json,
)
from pulsara_agent.primitives.tool_result import ToolResultRenderUnit


class WindowCompactionTranscriptBaselineFact(FrozenContextFact):
    schema_version: str = "window-compaction-transcript-baseline.v1"
    compaction_id: str
    run_id: str
    source_window_id: str
    source_through_sequence: int
    current_user_anchor: str
    retained_messages: tuple[TranscriptMessageFact, ...]
    retained_tool_pairs: tuple[ToolInteractionPairFact, ...]
    retained_tool_result_units: tuple[ToolResultRenderUnit, ...]
    baseline_fingerprint: str

    @model_validator(mode="after")
    def _baseline(self) -> "WindowCompactionTranscriptBaselineFact":
        payload = self.model_dump(
            mode="python",
            exclude={"baseline_fingerprint"},
        )
        if self.baseline_fingerprint != context_fingerprint(
            "window-compaction-transcript-baseline:v1", payload
        ):
            raise ValueError("window compaction transcript baseline fingerprint mismatch")
        message_ids = {item.message_id for item in self.retained_messages}
        if self.current_user_anchor not in message_ids:
            raise ValueError("window baseline lacks current user anchor")
        pair_ids = {item.tool_call_id for item in self.retained_tool_pairs}
        unit_ids = {item.tool_call_id for item in self.retained_tool_result_units}
        if pair_ids != unit_ids:
            raise ValueError("window baseline pair/unit identity mismatch")
        return self


def build_window_compaction_transcript_baseline(
    *,
    compaction_id: str,
    run_id: str,
    source_window_id: str,
    transcript: TranscriptCompileInput,
    units: tuple[ToolResultRenderUnit, ...],
    retained_message_ids: tuple[str, ...],
) -> WindowCompactionTranscriptBaselineFact:
    retained = set(retained_message_ids)
    messages = tuple(
        message for message in transcript.messages if message.message_id in retained
    )
    pairs = tuple(
        pair
        for pair in transcript.tool_pairs
        if pair.call_message_id in retained and pair.result_message_id in retained
    )
    pair_ids = {pair.tool_call_id for pair in pairs}
    retained_units = tuple(unit for unit in units if unit.tool_call_id in pair_ids)
    payload = {
        "schema_version": "window-compaction-transcript-baseline.v1",
        "compaction_id": compaction_id,
        "run_id": run_id,
        "source_window_id": source_window_id,
        "source_through_sequence": transcript.through_sequence,
        "current_user_anchor": transcript.current_user_anchor,
        "retained_messages": messages,
        "retained_tool_pairs": pairs,
        "retained_tool_result_units": retained_units,
    }
    return WindowCompactionTranscriptBaselineFact(
        **payload,
        baseline_fingerprint=context_fingerprint(
            "window-compaction-transcript-baseline:v1", payload
        ),
    )


def parse_window_compaction_transcript_baseline(
    value: object,
) -> WindowCompactionTranscriptBaselineFact:
    return WindowCompactionTranscriptBaselineFact.model_validate(thaw_json(value))


__all__ = [
    "WindowCompactionTranscriptBaselineFact",
    "build_window_compaction_transcript_baseline",
    "parse_window_compaction_transcript_baseline",
]
