"""Tool result artifact archiving and ownership index."""

from __future__ import annotations

import re
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.capability.descriptor import CapabilityArtifactMode, CapabilityDescriptor
from pulsara_agent.event import EventContext
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.message import ToolResultArtifactRef, ToolResultPreviewMetadata
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult, ToolResultArtifactCandidate


DEFAULT_TOOL_ARTIFACT_THRESHOLD_BYTES = 8_000
DEFAULT_COMPLETE_PREVIEW_BODY_CHARS = 32_000
DEFAULT_LARGE_PREVIEW_CHARS = 8_000
DEFAULT_HUGE_OUTPUT_CHARS = 200_000
DEFAULT_HUGE_PREVIEW_CHARS = 4_000
DEFAULT_STREAMING_LIVE_HEAD_CAP_CHARS = 2_600
DEFAULT_TOOL_RESULT_MESSAGE_CONTEXT_CHARS = 36_000
_HEAD_RATIO = 0.65
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


@dataclass(frozen=True, slots=True)
class ToolResultArtifactOptions:
    archive_threshold_bytes: int = DEFAULT_TOOL_ARTIFACT_THRESHOLD_BYTES
    complete_preview_body_chars: int = DEFAULT_COMPLETE_PREVIEW_BODY_CHARS
    large_preview_chars: int = DEFAULT_LARGE_PREVIEW_CHARS
    huge_output_chars: int = DEFAULT_HUGE_OUTPUT_CHARS
    huge_preview_chars: int = DEFAULT_HUGE_PREVIEW_CHARS
    streaming_live_head_cap_chars: int = DEFAULT_STREAMING_LIVE_HEAD_CAP_CHARS
    tool_result_message_context_chars: int = DEFAULT_TOOL_RESULT_MESSAGE_CONTEXT_CHARS

    def __post_init__(self) -> None:
        effective_archive_threshold = self.effective_archive_threshold_bytes
        effective_large_preview = self.effective_large_preview_chars
        effective_message_context = self.effective_tool_result_message_context_chars
        if effective_archive_threshold < 1:
            raise ValueError("archive_threshold_bytes must be >= 1")
        if self.complete_preview_body_chars < 1:
            raise ValueError("complete_preview_body_chars must be >= 1")
        if effective_large_preview < 1:
            raise ValueError("large_preview_chars must be >= 1")
        if self.huge_output_chars < 1:
            raise ValueError("huge_output_chars must be >= 1")
        if self.huge_preview_chars < 1:
            raise ValueError("huge_preview_chars must be >= 1")
        if self.streaming_live_head_cap_chars < 1:
            raise ValueError("streaming_live_head_cap_chars must be >= 1")
        if effective_message_context < 1:
            raise ValueError("tool_result_message_context_chars must be >= 1")
        if effective_archive_threshold > effective_message_context:
            raise ValueError("archive_threshold_bytes must be <= tool_result_message_context_chars")

    @property
    def effective_archive_threshold_bytes(self) -> int:
        return self.archive_threshold_bytes

    @property
    def effective_large_preview_chars(self) -> int:
        return self.large_preview_chars

    @property
    def effective_tool_result_message_context_chars(self) -> int:
        return self.tool_result_message_context_chars


@dataclass(frozen=True, slots=True)
class AdaptivePreview:
    text: str
    policy: str
    original_chars: int
    original_bytes: int
    preview_chars: int
    visible_head_chars: int
    visible_tail_chars: int
    omitted_middle_chars: int

    def to_metadata(self, *, artifact_id: str | None = None) -> ToolResultPreviewMetadata:
        read_more: dict[str, object] = {
            "tool": "artifact_read",
            "suggested_offset_chars": self.visible_head_chars,
            "suggested_max_chars": 20_000,
        }
        if artifact_id is not None:
            read_more["artifact_id"] = artifact_id
        return ToolResultPreviewMetadata(
            preview_policy=self.policy,  # type: ignore[arg-type]
            preview_chars=self.preview_chars,
            original_chars=self.original_chars,
            original_bytes=self.original_bytes,
            omitted_middle_chars=self.omitted_middle_chars,
            visible_head_chars=self.visible_head_chars,
            visible_tail_chars=self.visible_tail_chars,
            read_more=read_more,
        )


@dataclass(frozen=True, slots=True)
class ToolResultArtifactRecord:
    id: str
    session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    tool_call_id: str
    tool_name: str
    artifact_id: str
    role: str
    ordinal: int
    media_type: str
    size_bytes: int
    stored_complete: bool = True
    loss_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolResultArtifactIndex(Protocol):
    def put(self, record: ToolResultArtifactRecord) -> None: ...

    def get_for_session(self, artifact_id: str, *, session_id: str) -> ToolResultArtifactRecord | None: ...


@dataclass(slots=True)
class InMemoryToolResultArtifactIndex:
    records: dict[str, ToolResultArtifactRecord] = field(default_factory=dict)

    def put(self, record: ToolResultArtifactRecord) -> None:
        existing = self.records.get(record.id)
        if existing is not None and existing != record:
            raise ValueError(f"tool result artifact record {record.id!r} already exists with different data")
        self.records[record.id] = record

    def get_for_session(self, artifact_id: str, *, session_id: str) -> ToolResultArtifactRecord | None:
        matches = [
            record
            for record in self.records.values()
            if record.artifact_id == artifact_id and record.session_id == session_id
        ]
        if not matches:
            return None
        matches.sort(key=lambda record: (record.run_id, record.tool_call_id, record.ordinal))
        return matches[0]


@dataclass(slots=True)
class PostgresToolResultArtifactIndex:
    dsn: str

    def put(self, record: ToolResultArtifactRecord) -> None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into tool_result_artifacts (
                        id,
                        session_id,
                        run_id,
                        turn_id,
                        reply_id,
                        tool_call_id,
                        tool_name,
                        artifact_id,
                        role,
                        ordinal,
                        media_type,
                        size_bytes,
                        stored_complete,
                        loss_reason,
                        metadata
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (run_id, tool_call_id, role, ordinal) do update set
                        artifact_id = excluded.artifact_id,
                        media_type = excluded.media_type,
                        size_bytes = excluded.size_bytes,
                        stored_complete = excluded.stored_complete,
                        loss_reason = excluded.loss_reason,
                        metadata = excluded.metadata
                    """,
                    (
                        record.id,
                        record.session_id,
                        record.run_id,
                        record.turn_id,
                        record.reply_id,
                        record.tool_call_id,
                        record.tool_name,
                        record.artifact_id,
                        record.role,
                        record.ordinal,
                        record.media_type,
                        record.size_bytes,
                        record.stored_complete,
                        record.loss_reason,
                        Jsonb(record.metadata),
                    ),
                )

    def get_for_session(self, artifact_id: str, *, session_id: str) -> ToolResultArtifactRecord | None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select
                        id,
                        session_id,
                        run_id,
                        turn_id,
                        reply_id,
                        tool_call_id,
                        tool_name,
                        artifact_id,
                        role,
                        ordinal,
                        media_type,
                        size_bytes,
                        stored_complete,
                        loss_reason,
                        metadata
                    from tool_result_artifacts
                    where artifact_id = %s and session_id = %s
                    order by run_id, tool_call_id, ordinal
                    limit 1
                    """,
                    (artifact_id, session_id),
                )
                row = cursor.fetchone()
        return _record_from_row(row) if row is not None else None


@dataclass(slots=True)
class ToolResultArtifactService:
    archive: ArtifactStore
    index: ToolResultArtifactIndex
    runtime_session_id: str
    options: ToolResultArtifactOptions = field(default_factory=ToolResultArtifactOptions)

    def process_result(
        self,
        result: ToolExecutionResult,
        *,
        event_context: EventContext,
        tool_call: ToolCall,
        descriptor: CapabilityDescriptor | None = None,
    ) -> tuple[ToolExecutionResult, tuple[ToolResultArtifactRef, ...]]:
        artifact_mode = descriptor.artifact_mode if descriptor is not None else CapabilityArtifactMode.DEFAULT
        # Compatibility for direct service tests/callers that have not yet
        # threaded a descriptor. The normal runtime path supplies the
        # descriptor and expresses this as artifact_mode=NEVER.
        if artifact_mode is CapabilityArtifactMode.NEVER or (
            descriptor is None and result.tool_name == "artifact_read"
        ):
            return result, ()

        options = _options_for_tool_call(self.options, tool_call)
        candidates = tuple(result.artifact_candidates)
        processed_output = result.output
        force_archive = artifact_mode in {CapabilityArtifactMode.ALWAYS, CapabilityArtifactMode.STRUCTURED_JSON}
        if not candidates and (
            force_archive or len(result.output.encode("utf-8")) > options.effective_archive_threshold_bytes
        ):
            media_type = "application/json" if artifact_mode is CapabilityArtifactMode.STRUCTURED_JSON else "text/plain; charset=utf-8"
            candidates = (
                ToolResultArtifactCandidate(
                    role="output",
                    media_type=media_type,
                    text=result.output,
                    metadata={"fallback": True},
                ),
            )

        primary_ordinal = _primary_preview_candidate_ordinal(candidates)
        primary_preview: AdaptivePreview | None = None
        if primary_ordinal is not None:
            candidate = candidates[primary_ordinal]
            if candidate.text is not None:
                primary_preview = build_adaptive_preview(candidate.text, options)
        elif candidates:
            primary_preview = build_adaptive_preview(result.output, options)

        refs: list[ToolResultArtifactRef] = []
        for ordinal, candidate in enumerate(candidates):
            size_bytes = _candidate_size_bytes(candidate)
            if not force_archive and size_bytes <= options.effective_archive_threshold_bytes:
                continue
            refs.append(
                self._archive_candidate(
                    candidate,
                    event_context=event_context,
                    tool_call=tool_call,
                    ordinal=ordinal,
                    size_bytes=size_bytes,
                    preview=primary_preview if ordinal == primary_ordinal else None,
                )
            )

        if refs:
            final_preview = next((ref.preview for ref in refs if ref.preview is not None), None)
            preview_for_output = final_preview or (primary_preview.to_metadata() if primary_preview is not None else None)
            if primary_preview is not None:
                processed_output = _rewrite_result_output_with_preview(result, primary_preview, preview_for_output)
            elif len(processed_output) > options.effective_large_preview_chars:
                fallback_preview = build_adaptive_preview(processed_output, options)
                processed_output = fallback_preview.text

        if processed_output == result.output:
            processed = result
        else:
            processed = ToolExecutionResult(
                call_id=result.call_id,
                tool_name=result.tool_name,
                status=result.status,
                output=processed_output,
                metadata=result.metadata,
                artifact_candidates=result.artifact_candidates,
            )
        return processed, tuple(refs)

    def _archive_candidate(
        self,
        candidate: ToolResultArtifactCandidate,
        *,
        event_context: EventContext,
        tool_call: ToolCall,
        ordinal: int,
        size_bytes: int,
        preview: AdaptivePreview | None = None,
    ) -> ToolResultArtifactRef:
        role = _sanitize_part(candidate.role or "output")
        artifact_id = _artifact_id(event_context.run_id, tool_call.id, role, ordinal)
        metadata = {
            "tool_name": tool_call.name,
            "tool_call_id": tool_call.id,
            "role": candidate.role,
            "ordinal": ordinal,
            "redacted": candidate.redacted,
            **candidate.metadata,
        }
        if candidate.text is not None:
            write = self.archive.put_text(
                artifact_id,
                candidate.text,
                session_id=self.runtime_session_id,
                run_id=event_context.run_id,
                media_type=candidate.media_type,
                metadata=metadata,
            )
        else:
            assert candidate.data is not None
            write = self.archive.put_bytes(
                artifact_id,
                candidate.data,
                session_id=self.runtime_session_id,
                run_id=event_context.run_id,
                media_type=candidate.media_type,
                metadata=metadata,
            )
        final_preview = preview.to_metadata(artifact_id=write.id) if preview is not None else None
        record_metadata = dict(metadata)
        if final_preview is not None:
            record_metadata["preview"] = final_preview.model_dump()
        record = ToolResultArtifactRecord(
            id=f"tool-result-artifact:{_sanitize_part(event_context.run_id)}:{_sanitize_part(tool_call.id)}:{role}:{ordinal}",
            session_id=self.runtime_session_id,
            run_id=event_context.run_id,
            turn_id=event_context.turn_id,
            reply_id=event_context.reply_id,
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            artifact_id=write.id,
            role=candidate.role,
            ordinal=ordinal,
            media_type=candidate.media_type,
            size_bytes=write.size_bytes,
            stored_complete=candidate.stored_complete,
            loss_reason=candidate.loss_reason,
            metadata=record_metadata,
        )
        self.index.put(record)
        return ToolResultArtifactRef(
            artifact_id=write.id,
            role=candidate.role,
            media_type=candidate.media_type,
            size_bytes=write.size_bytes,
            stored_complete=candidate.stored_complete,
            loss_reason=candidate.loss_reason,
            preview=final_preview,
        )


def _candidate_size_bytes(candidate: ToolResultArtifactCandidate) -> int:
    if candidate.text is not None:
        return len(candidate.text.encode("utf-8"))
    assert candidate.data is not None
    return len(candidate.data)


def _options_for_tool_call(options: ToolResultArtifactOptions, tool_call: ToolCall) -> ToolResultArtifactOptions:
    if tool_call.name not in {"terminal", "terminal_process"}:
        return options
    cap = effective_terminal_output_cap(tool_call.arguments.get("max_output_chars"))
    if cap is None:
        return options
    huge_preview = min(options.huge_preview_chars, cap)
    streaming_options_seed = ToolResultArtifactOptions(
        archive_threshold_bytes=options.effective_archive_threshold_bytes,
        complete_preview_body_chars=min(options.complete_preview_body_chars, cap),
        large_preview_chars=min(options.effective_large_preview_chars, cap),
        huge_output_chars=options.huge_output_chars,
        huge_preview_chars=huge_preview,
        streaming_live_head_cap_chars=1,
        tool_result_message_context_chars=options.effective_tool_result_message_context_chars,
    )
    huge_head_cap = build_adaptive_preview("x" * (options.huge_output_chars + 1), streaming_options_seed).visible_head_chars
    return ToolResultArtifactOptions(
        archive_threshold_bytes=options.effective_archive_threshold_bytes,
        complete_preview_body_chars=min(options.complete_preview_body_chars, cap),
        large_preview_chars=min(options.effective_large_preview_chars, cap),
        huge_output_chars=options.huge_output_chars,
        huge_preview_chars=huge_preview,
        streaming_live_head_cap_chars=max(1, min(options.streaming_live_head_cap_chars, huge_head_cap)),
        tool_result_message_context_chars=options.effective_tool_result_message_context_chars,
    )


def effective_terminal_output_cap(raw: object) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw <= 0:
        return None
    from pulsara_agent.tools.builtins.schemas import DEFAULT_MAX_OUTPUT_CHARS, MIN_TERMINAL_OUTPUT_CHARS

    return max(MIN_TERMINAL_OUTPUT_CHARS, min(raw, DEFAULT_MAX_OUTPUT_CHARS))


def build_adaptive_preview(text: str, options: ToolResultArtifactOptions) -> AdaptivePreview:
    original_chars = len(text)
    original_bytes = len(text.encode("utf-8"))
    if original_chars <= options.complete_preview_body_chars:
        return AdaptivePreview(
            text=text,
            policy="full",
            original_chars=original_chars,
            original_bytes=original_bytes,
            preview_chars=original_chars,
            visible_head_chars=original_chars,
            visible_tail_chars=0,
            omitted_middle_chars=0,
        )

    budget = (
        options.huge_preview_chars
        if original_chars > options.huge_output_chars
        else options.effective_large_preview_chars
    )
    budget = max(1, min(budget, original_chars))
    preliminary_head = max(1, int(budget * _HEAD_RATIO))
    preliminary_tail = max(0, budget - preliminary_head)
    preliminary_omitted = max(0, original_chars - preliminary_head - preliminary_tail)
    notice = _preview_truncation_notice(preliminary_omitted, preliminary_head)
    content_budget = max(1, budget - len(notice))
    head_chars = max(1, int(content_budget * _HEAD_RATIO))
    tail_chars = max(0, content_budget - head_chars)
    if head_chars + tail_chars >= original_chars:
        return AdaptivePreview(
            text=text,
            policy="full",
            original_chars=original_chars,
            original_bytes=original_bytes,
            preview_chars=original_chars,
            visible_head_chars=original_chars,
            visible_tail_chars=0,
            omitted_middle_chars=0,
        )
    omitted = max(0, original_chars - head_chars - tail_chars)
    notice = _preview_truncation_notice(omitted, head_chars)
    text_preview = text[:head_chars] + notice + (text[-tail_chars:] if tail_chars else "")
    return AdaptivePreview(
        text=text_preview,
        policy="head_tail_huge" if original_chars > options.huge_output_chars else "head_tail",
        original_chars=original_chars,
        original_bytes=original_bytes,
        preview_chars=len(text_preview),
        visible_head_chars=head_chars,
        visible_tail_chars=tail_chars,
        omitted_middle_chars=omitted,
    )


def _preview_truncation_notice(omitted: int, suggested_offset_chars: int) -> str:
    return (
        f"\n\n[OUTPUT TRUNCATED / PREVIEW: omitted {omitted} chars from the middle. "
        f"Full retained output is available via artifact_read. Prefer reading from offset_chars={suggested_offset_chars} "
        "if you need content after the visible head.]\n\n"
    )


def _primary_preview_candidate_ordinal(candidates: tuple[ToolResultArtifactCandidate, ...]) -> int | None:
    preferred_roles = {"combined_output", "output"}
    for idx, candidate in enumerate(candidates):
        if candidate.text is not None and candidate.role in preferred_roles:
            return idx
    for idx, candidate in enumerate(candidates):
        if candidate.text is not None:
            return idx
    return None


def _rewrite_result_output_with_preview(
    result: ToolExecutionResult,
    preview: AdaptivePreview,
    metadata: ToolResultPreviewMetadata | None,
) -> str:
    if result.tool_name not in {"terminal", "terminal_process"}:
        return preview.text
    try:
        payload = json.loads(result.output)
    except json.JSONDecodeError:
        return preview.text
    if not isinstance(payload, dict):
        return preview.text
    payload["output"] = preview.text
    payload["truncated"] = preview.omitted_middle_chars > 0 or bool(payload.get("truncated"))
    payload["preview_policy"] = preview.policy
    payload["output_preview_chars"] = preview.preview_chars
    payload["output_original_chars"] = preview.original_chars
    payload["output_original_bytes"] = preview.original_bytes
    payload["omitted_middle_chars"] = preview.omitted_middle_chars
    payload["visible_head_chars"] = preview.visible_head_chars
    payload["visible_tail_chars"] = preview.visible_tail_chars
    if metadata is not None:
        payload["preview"] = metadata.model_dump()
    return json.dumps(payload, ensure_ascii=False)


def _artifact_id(run_id: str, tool_call_id: str, role: str, ordinal: int) -> str:
    return f"artifact:tool-result:{_sanitize_part(run_id)}:{_sanitize_part(tool_call_id)}:{role}:{ordinal}"


def _sanitize_part(value: str) -> str:
    sanitized = _SAFE_ID_RE.sub("_", value.strip())
    return sanitized or "unknown"


def _record_from_row(row: dict[str, Any]) -> ToolResultArtifactRecord:
    return ToolResultArtifactRecord(
        id=row["id"],
        session_id=row["session_id"],
        run_id=row["run_id"],
        turn_id=row["turn_id"],
        reply_id=row["reply_id"],
        tool_call_id=row["tool_call_id"],
        tool_name=row["tool_name"],
        artifact_id=row["artifact_id"],
        role=row["role"],
        ordinal=row["ordinal"],
        media_type=row["media_type"],
        size_bytes=row["size_bytes"],
        stored_complete=row["stored_complete"],
        loss_reason=row["loss_reason"],
        metadata=dict(row["metadata"] or {}),
    )
