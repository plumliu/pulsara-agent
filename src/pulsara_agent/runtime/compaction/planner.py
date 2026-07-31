"""Compaction boundary selection and transcript rehydration helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from pulsara_agent.event import AgentEvent, ContextCompactionCompletedEvent
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.message import Msg, SystemMsg, TextBlock


SUMMARY_ARTIFACT_KIND = "context_compaction_summary"
SUMMARY_MESSAGE_KIND = "context_compaction_summary"

_ANALYSIS_RE = re.compile(r"<analysis>[\s\S]*?</analysis>", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"<summary>([\s\S]*?)</summary>", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CompactionBoundary:
    event: ContextCompactionCompletedEvent
    summary_text: str

    @property
    def keep_after_sequence(self) -> int:
        return self.event.keep_after_sequence


def strip_compaction_analysis(raw_text: str) -> str:
    """Strip compact-model drafting analysis and return the official summary body."""

    without_analysis = _ANALYSIS_RE.sub("", raw_text).strip()
    summary_match = _SUMMARY_RE.search(without_analysis)
    if summary_match is not None:
        return summary_match.group(1).strip()
    if re.search(r"<analysis\b", raw_text, re.IGNORECASE):
        return ""
    if re.search(r"<summary\b", raw_text, re.IGNORECASE):
        return ""
    return without_analysis.strip()


def latest_completed_boundary(
    events: Iterable[AgentEvent],
    *,
    archive: ArtifactStore | None,
    session_id: str | None,
) -> CompactionBoundary | None:
    """Return the latest usable completed compaction boundary.

    A boundary is usable only if its summary artifact still exists. This keeps
    transcript rehydration fail-open to the canonical event log when artifact
    state is damaged or an attempt only wrote a started/failed event.
    """

    if archive is None:
        return None
    for event in reversed(list(events)):
        if not isinstance(event, ContextCompactionCompletedEvent):
            continue
        try:
            summary = archive.get_text(event.summary_artifact_id, session_id=session_id)
        except Exception:
            continue
        return CompactionBoundary(event=event, summary_text=summary)
    return None


def build_compaction_summary_message(boundary: CompactionBoundary) -> Msg:
    event = boundary.event
    content = render_compaction_summary(
        boundary.summary_text,
        summary_artifact_id=event.summary_artifact_id,
        compaction_id=event.compaction_id,
        window_id=event.window_id,
        through_sequence=event.through_sequence,
        keep_after_sequence=event.keep_after_sequence,
    )
    return SystemMsg(
        name="pulsara",
        content=content,
        id=f"context-compaction-summary:{event.compaction_id}",
        created_at=event.created_at,
        metadata={
            "kind": SUMMARY_MESSAGE_KIND,
            "do_not_write_back": True,
            "artifact_id": event.summary_artifact_id,
            "compaction_id": event.compaction_id,
            "window_id": event.window_id,
            "through_sequence": event.through_sequence,
            "keep_after_sequence": event.keep_after_sequence,
        },
    )


def render_compaction_summary(
    summary_text: str,
    *,
    summary_artifact_id: str,
    compaction_id: str,
    window_id: str,
    through_sequence: int,
    keep_after_sequence: int,
) -> str:
    return "\n".join(
        [
            '<context-compaction-summary source="pulsara" do_not_write_back="true"',
            f'  artifact_id="{summary_artifact_id}"',
            f'  compaction_id="{compaction_id}"',
            f'  window_id="{window_id}"',
            f'  through_sequence="{through_sequence}"',
            f'  keep_after_sequence="{keep_after_sequence}">',
            "",
            "This session is being continued from a compacted Pulsara runtime context.",
            "",
            (
                "Another model summarized the earlier portion of this same runtime session so you can "
                "continue without replaying every old event. The full canonical event log and artifacts "
                "still exist outside this summary; this summary is only a continuity handoff, not a "
                "replacement source of truth."
            ),
            "",
            "Use the summary below to continue the current task, but preserve these boundaries:",
            "",
            "- User messages are user facts only when identified as user messages.",
            "- Tool outputs are observations from tools, not user preferences.",
            "- Memory recall and working context are projections, not fresh user statements.",
            "- Recovery/abort notes are runtime diagnostics, not durable semantic memory.",
            "- Long artifacts are referenced by id/path; do not assume omitted content.",
            "",
            "Compaction summary:",
            summary_text.strip(),
            "",
            "</context-compaction-summary>",
            "",
            "Continue from the compacted context as if the conversation had not been interrupted. "
            "Do not greet the user again because of compaction, do not recap this summary unless "
            "asked, and prefer recent messages that follow this summary if they conflict with it.",
        ]
    )


def message_text(message: Msg) -> str:
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        else:
            parts.append(f"[{getattr(block, 'type', 'non_text_block')}:{getattr(block, 'id', '')}]")
    return "\n".join(part for part in parts if part)
