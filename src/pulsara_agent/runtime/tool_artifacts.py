"""Tool result artifact archiving and ownership index."""

from __future__ import annotations

import re
import json
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.capability.descriptor import (
    CapabilityArtifactMode,
    CapabilityDescriptor,
)
from pulsara_agent.event import EventContext
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.message import (
    ToolResultArtifactRef,
    ToolResultPreviewMetadata,
    ToolResultState,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.context_source import ContextArtifactReferenceFact
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.terminal_observation import (
    ArtifactTerminalObservationCoverageFact,
    BoundedPreviewTerminalObservationCoverageFact,
    TerminalProcessObservationReceiptFact,
    TerminalProcessObservationSemanticFact,
)
from pulsara_agent.tools.base import (
    ToolCall,
    ToolExecutionResult,
    ToolResultArtifactCandidate,
)


DEFAULT_TOOL_ARTIFACT_THRESHOLD_BYTES = 8_000
DEFAULT_COMPLETE_PREVIEW_BODY_CHARS = 32_000
DEFAULT_LARGE_PREVIEW_CHARS = 8_000
DEFAULT_HUGE_OUTPUT_CHARS = 200_000
DEFAULT_HUGE_PREVIEW_CHARS = 4_000
DEFAULT_STREAMING_LIVE_HEAD_CAP_CHARS = 2_600
_HEAD_RATIO = 0.65
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_TERMINAL_OBSERVATION_ARTIFACT_CODEC_CONTRACT_FINGERPRINT = context_fingerprint(
    "terminal-observation-artifact-codec-contract:v1",
    {
        "codec": "utf-8",
        "covered_content": "exact-sanitized-output-range",
    },
)


@dataclass(frozen=True, slots=True)
class ToolResultArtifactOptions:
    archive_threshold_bytes: int = DEFAULT_TOOL_ARTIFACT_THRESHOLD_BYTES
    complete_preview_body_chars: int = DEFAULT_COMPLETE_PREVIEW_BODY_CHARS
    large_preview_chars: int = DEFAULT_LARGE_PREVIEW_CHARS
    huge_output_chars: int = DEFAULT_HUGE_OUTPUT_CHARS
    huge_preview_chars: int = DEFAULT_HUGE_PREVIEW_CHARS
    streaming_live_head_cap_chars: int = DEFAULT_STREAMING_LIVE_HEAD_CAP_CHARS

    def __post_init__(self) -> None:
        effective_archive_threshold = self.effective_archive_threshold_bytes
        effective_large_preview = self.effective_large_preview_chars
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

    @property
    def effective_archive_threshold_bytes(self) -> int:
        return self.archive_threshold_bytes

    @property
    def effective_large_preview_chars(self) -> int:
        return self.large_preview_chars


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

    def to_metadata(
        self, *, artifact_id: str | None = None
    ) -> ToolResultPreviewMetadata:
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


@dataclass(frozen=True, slots=True)
class _ArchivedToolResultCandidate:
    tool_result_reference: ToolResultArtifactRef
    context_reference: ContextArtifactReferenceFact
    candidate: ToolResultArtifactCandidate


class ToolResultArtifactIndex(Protocol):
    def put(self, record: ToolResultArtifactRecord) -> None: ...

    def get_for_session(
        self, artifact_id: str, *, session_id: str
    ) -> ToolResultArtifactRecord | None: ...


@dataclass(slots=True)
class InMemoryToolResultArtifactIndex:
    records: dict[str, ToolResultArtifactRecord] = field(default_factory=dict)

    def put(self, record: ToolResultArtifactRecord) -> None:
        existing = self.records.get(record.id)
        if existing is not None and existing != record:
            raise ValueError(
                f"tool result artifact record {record.id!r} already exists with different data"
            )
        self.records[record.id] = record

    def get_for_session(
        self, artifact_id: str, *, session_id: str
    ) -> ToolResultArtifactRecord | None:
        matches = [
            record
            for record in self.records.values()
            if record.artifact_id == artifact_id and record.session_id == session_id
        ]
        if not matches:
            return None
        matches.sort(
            key=lambda record: (record.run_id, record.tool_call_id, record.ordinal)
        )
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

    def get_for_session(
        self, artifact_id: str, *, session_id: str
    ) -> ToolResultArtifactRecord | None:
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
    options: ToolResultArtifactOptions = field(
        default_factory=ToolResultArtifactOptions
    )

    def process_result(
        self,
        result: ToolExecutionResult,
        *,
        event_context: EventContext,
        tool_call: ToolCall,
        descriptor: CapabilityDescriptor | None = None,
    ) -> tuple[ToolExecutionResult, tuple[ToolResultArtifactRef, ...]]:
        artifact_mode = (
            descriptor.artifact_mode
            if descriptor is not None
            else CapabilityArtifactMode.DEFAULT
        )
        if result.tool_name == "artifact_read":
            return result, self._artifact_read_source_refs(result, tool_call)

        # Compatibility for direct service tests/callers that have not yet
        # threaded a descriptor. The normal runtime path supplies the
        # descriptor and expresses this as artifact_mode=NEVER.
        if artifact_mode is CapabilityArtifactMode.NEVER:
            return result, ()

        options = _options_for_tool_call(self.options, tool_call)
        candidates = tuple(result.artifact_candidates)
        processed_output = result.output
        processed_display_payload = result.display_payload
        force_archive = artifact_mode in {
            CapabilityArtifactMode.ALWAYS,
            CapabilityArtifactMode.STRUCTURED_JSON,
        }
        if not candidates and (
            force_archive
            or len(result.output.encode("utf-8"))
            > options.effective_archive_threshold_bytes
        ):
            media_type = (
                "application/json"
                if artifact_mode is CapabilityArtifactMode.STRUCTURED_JSON
                else "text/plain; charset=utf-8"
            )
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

        archived_candidates: list[_ArchivedToolResultCandidate] = []
        for ordinal, candidate in enumerate(candidates):
            size_bytes = _candidate_size_bytes(candidate)
            if (
                not force_archive
                and size_bytes <= options.effective_archive_threshold_bytes
            ):
                continue
            archived_candidates.append(
                self._archive_candidate(
                    candidate,
                    event_context=event_context,
                    tool_call=tool_call,
                    ordinal=ordinal,
                    size_bytes=size_bytes,
                    preview=primary_preview if ordinal == primary_ordinal else None,
                )
            )
        refs = [item.tool_result_reference for item in archived_candidates]

        if refs:
            final_preview = next(
                (ref.preview for ref in refs if ref.preview is not None), None
            )
            preview_for_output = final_preview or (
                primary_preview.to_metadata() if primary_preview is not None else None
            )
            if primary_preview is not None:
                processed_output, processed_display_payload = (
                    _rewrite_result_output_with_preview(
                        result, primary_preview, preview_for_output
                    )
                )
            elif len(processed_output) > options.effective_large_preview_chars:
                fallback_preview = build_adaptive_preview(processed_output, options)
                processed_output = fallback_preview.text

        if (
            processed_output == result.output
            and processed_display_payload == result.display_payload
        ):
            processed = result
        else:
            processed = replace(
                result,
                output=processed_output,
                display_payload=processed_display_payload,
            )
        processed = _attach_exact_terminal_observation_artifact_coverage(
            processed,
            archived_candidates=tuple(archived_candidates),
        )
        return processed, tuple(refs)

    def _artifact_read_source_refs(
        self,
        result: ToolExecutionResult,
        tool_call: ToolCall,
    ) -> tuple[ToolResultArtifactRef, ...]:
        """Attach the source artifact ref for artifact_read without re-archiving.

        artifact_read is intentionally artifact_mode=NEVER: reading an artifact
        should not recursively create another artifact.  But the returned text
        can legitimately be larger than the ledger's inline-output threshold.
        Carrying the original source artifact ref through ToolResultEndEvent
        preserves the evidence anchor and lets persistence use the block-aware
        path instead of treating the read text as an orphaned large output.
        """

        if result.status is not ToolResultState.SUCCESS:
            return ()
        artifact_id = str(tool_call.arguments.get("artifact_id") or "")
        if not artifact_id:
            return ()
        record = self.index.get_for_session(
            artifact_id, session_id=self.runtime_session_id
        )
        if record is None:
            return ()
        return (_artifact_ref_from_record(record),)

    def _archive_candidate(
        self,
        candidate: ToolResultArtifactCandidate,
        *,
        event_context: EventContext,
        tool_call: ToolCall,
        ordinal: int,
        size_bytes: int,
        preview: AdaptivePreview | None = None,
    ) -> _ArchivedToolResultCandidate:
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
        final_preview = (
            preview.to_metadata(artifact_id=write.id) if preview is not None else None
        )
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
        tool_result_reference = ToolResultArtifactRef(
            artifact_id=write.id,
            role=candidate.role,
            media_type=candidate.media_type,
            size_bytes=write.size_bytes,
            stored_complete=candidate.stored_complete,
            loss_reason=candidate.loss_reason,
            preview=final_preview,
        )
        context_reference = build_frozen_fact(
            ContextArtifactReferenceFact,
            schema_version="context_artifact_reference.v1",
            artifact_id=write.id,
            media_type=candidate.media_type,
            content_sha256=write.digest,
            content_bytes=write.size_bytes,
            artifact_contract_fingerprint=(
                _TERMINAL_OBSERVATION_ARTIFACT_CODEC_CONTRACT_FINGERPRINT
            ),
        )
        return _ArchivedToolResultCandidate(
            tool_result_reference=tool_result_reference,
            context_reference=context_reference,
            candidate=candidate,
        )


def _attach_exact_terminal_observation_artifact_coverage(
    result: ToolExecutionResult,
    *,
    archived_candidates: tuple[_ArchivedToolResultCandidate, ...],
) -> ToolExecutionResult:
    receipt = result.terminal_process_observation_receipt
    if receipt is None or not isinstance(
        receipt.observation_semantic.output_coverage,
        BoundedPreviewTerminalObservationCoverageFact,
    ):
        return result
    archived = next(
        (
            item
            for item in archived_candidates
            if item.candidate.role == "combined_output"
            and item.candidate.text is not None
            and item.candidate.stored_complete
        ),
        None,
    )
    if archived is None:
        return result
    semantic = receipt.observation_semantic
    text = archived.candidate.text
    assert text is not None
    expected_chars = (
        semantic.observed_end_cursor.sanitized_char_offset
        - semantic.observed_start_cursor.sanitized_char_offset
    )
    expected_bytes = (
        semantic.observed_end_cursor.sanitized_utf8_byte_offset
        - semantic.observed_start_cursor.sanitized_utf8_byte_offset
    )
    encoded = text.encode("utf-8")
    if len(text) != expected_chars or len(encoded) != expected_bytes:
        return result
    content_sha256 = f"sha256:{sha256(encoded).hexdigest()}"
    if archived.context_reference.content_sha256 != content_sha256:
        raise ValueError("terminal observation artifact content hash mismatch")
    coverage = build_frozen_fact(
        ArtifactTerminalObservationCoverageFact,
        schema_version="artifact_terminal_observation_coverage.v1",
        covered_start_cursor=semantic.observed_start_cursor,
        covered_end_cursor=semantic.observed_end_cursor,
        artifact_reference=archived.context_reference,
        covered_range_content_sha256=content_sha256,
        artifact_codec_contract_fingerprint=(
            _TERMINAL_OBSERVATION_ARTIFACT_CODEC_CONTRACT_FINGERPRINT
        ),
    )
    observation_semantic = build_frozen_fact(
        TerminalProcessObservationSemanticFact,
        schema_version="terminal_process_observation_semantic.v1",
        requested_start_cursor=semantic.requested_start_cursor,
        observed_start_cursor=semantic.observed_start_cursor,
        observed_end_cursor=semantic.observed_end_cursor,
        output_coverage=coverage,
        observed_state=semantic.observed_state,
    )
    updated_receipt = build_frozen_fact(
        TerminalProcessObservationReceiptFact,
        schema_version="terminal_process_observation_receipt.v1",
        observation_semantic=observation_semantic,
        action_kind=receipt.action_kind,
        origin_tool_call_id=receipt.origin_tool_call_id,
        completion_event_reference=receipt.completion_event_reference,
    )
    return replace(result, terminal_process_observation_receipt=updated_receipt)


def _candidate_size_bytes(candidate: ToolResultArtifactCandidate) -> int:
    if candidate.text is not None:
        return len(candidate.text.encode("utf-8"))
    assert candidate.data is not None
    return len(candidate.data)


def _artifact_ref_from_record(
    record: ToolResultArtifactRecord,
) -> ToolResultArtifactRef:
    preview: ToolResultPreviewMetadata | None = None
    raw_preview = record.metadata.get("preview")
    if isinstance(raw_preview, ToolResultPreviewMetadata):
        preview = raw_preview
    elif isinstance(raw_preview, dict):
        try:
            preview = ToolResultPreviewMetadata.model_validate(raw_preview)
        except Exception:
            preview = None
    return ToolResultArtifactRef(
        artifact_id=record.artifact_id,
        role=record.role,
        media_type=record.media_type,
        size_bytes=record.size_bytes,
        stored_complete=record.stored_complete,
        loss_reason=record.loss_reason,
        preview=preview,
    )


def _options_for_tool_call(
    options: ToolResultArtifactOptions, tool_call: ToolCall
) -> ToolResultArtifactOptions:
    if tool_call.name not in {"terminal", "terminal_process", "terminal_monitor"}:
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
    )
    huge_head_cap = build_adaptive_preview(
        "x" * (options.huge_output_chars + 1), streaming_options_seed
    ).visible_head_chars
    return ToolResultArtifactOptions(
        archive_threshold_bytes=options.effective_archive_threshold_bytes,
        complete_preview_body_chars=min(options.complete_preview_body_chars, cap),
        large_preview_chars=min(options.effective_large_preview_chars, cap),
        huge_output_chars=options.huge_output_chars,
        huge_preview_chars=huge_preview,
        streaming_live_head_cap_chars=max(
            1, min(options.streaming_live_head_cap_chars, huge_head_cap)
        ),
    )


def effective_terminal_output_cap(raw: object) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw <= 0:
        return None
    from pulsara_agent.tools.builtins.schemas import (
        DEFAULT_MAX_OUTPUT_CHARS,
        MIN_TERMINAL_OUTPUT_CHARS,
    )

    return max(MIN_TERMINAL_OUTPUT_CHARS, min(raw, DEFAULT_MAX_OUTPUT_CHARS))


def build_adaptive_preview(
    text: str, options: ToolResultArtifactOptions
) -> AdaptivePreview:
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
    text_preview = (
        text[:head_chars] + notice + (text[-tail_chars:] if tail_chars else "")
    )
    return AdaptivePreview(
        text=text_preview,
        policy="head_tail_huge"
        if original_chars > options.huge_output_chars
        else "head_tail",
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


def _primary_preview_candidate_ordinal(
    candidates: tuple[ToolResultArtifactCandidate, ...],
) -> int | None:
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
) -> tuple[str, FrozenJsonObjectFact | None]:
    if result.tool_name not in {"terminal", "terminal_process", "terminal_monitor"}:
        return preview.text, result.display_payload
    if result.display_payload is None:
        raise ValueError(
            "terminal artifact preview requires typed display payload; JSON inference is forbidden"
        )
    payload = thaw_json(result.display_payload)
    payload["output"] = preview.text
    payload["truncated"] = preview.omitted_middle_chars > 0 or bool(
        payload.get("truncated")
    )
    payload["preview_policy"] = preview.policy
    payload["output_preview_chars"] = preview.preview_chars
    payload["output_original_chars"] = preview.original_chars
    payload["output_original_bytes"] = preview.original_bytes
    payload["omitted_middle_chars"] = preview.omitted_middle_chars
    payload["visible_head_chars"] = preview.visible_head_chars
    payload["visible_tail_chars"] = preview.visible_tail_chars
    if metadata is not None:
        payload["preview"] = metadata.model_dump()
    frozen = freeze_json(payload)
    if not isinstance(frozen, FrozenJsonObjectFact):
        raise AssertionError("rewritten terminal display payload must be an object")
    return _json_display_text(payload), frozen


def _json_display_text(payload: dict[str, Any]) -> str:
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
