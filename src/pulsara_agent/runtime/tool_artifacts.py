"""Tool result artifact archiving and ownership index."""

from __future__ import annotations

import re
import json
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Any, Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from time import monotonic

from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
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
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolExecutionResult,
    ToolResultArtifactCandidate,
)
from pulsara_agent.ports.artifact import (
    AdaptivePreview,
    ToolArtifactMode,
    ToolResultArtifactOptions,
    ToolResultArtifactProcessingPolicy,
    build_adaptive_preview,
    effective_terminal_output_cap,
    build_tool_artifact_info_view,
    build_tool_artifact_record_view,
    build_tool_artifact_text_slice_view,
    build_tool_result_artifact_processing_policy,
)


_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_TERMINAL_OBSERVATION_ARTIFACT_CODEC_CONTRACT_FINGERPRINT = context_fingerprint(
    "terminal-observation-artifact-codec-contract:v1",
    {
        "codec": "utf-8",
        "covered_content": "exact-sanitized-output-range",
    },
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
class PostgresToolResultArtifactIndex:
    connection_provider: VerifiedPostgresConnectionProviderProtocol

    def put(self, record: ToolResultArtifactRecord) -> None:
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30.0,
        ) as connection:
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
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30.0,
        ) as connection:
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


@dataclass(frozen=True, slots=True)
class RuntimeToolArtifactReadPort:
    """Session-scoped artifact reader that exposes only immutable views."""

    archive: ArtifactStore
    index: ToolResultArtifactIndex
    runtime_session_id: str

    def lookup(self, artifact_id: str):
        record = self.index.get_for_session(
            artifact_id, session_id=self.runtime_session_id
        )
        if record is None:
            return None
        try:
            info = self.archive.get_info(
                artifact_id, session_id=self.runtime_session_id
            )
        except KeyError:
            return None
        return build_tool_artifact_record_view(
            artifact_id=artifact_id,
            role=record.role,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            stored_complete=record.stored_complete,
            loss_reason=record.loss_reason,
            content_digest=info.digest,
        )

    def info(self, artifact_id: str):
        record_view = self.lookup(artifact_id)
        if record_view is None:
            raise KeyError(artifact_id)
        info = self.archive.get_info(artifact_id, session_id=self.runtime_session_id)
        return build_tool_artifact_info_view(
            record=record_view,
            stored_at_utc=info.stored_at,
            created_at_utc=info.created_at,
        )

    def read_text(self, artifact_id: str, *, offset_chars: int, max_chars: int):
        info_view = self.info(artifact_id)
        text_slice = self.archive.read_text(
            artifact_id,
            session_id=self.runtime_session_id,
            offset_chars=offset_chars,
            max_chars=max_chars,
        )
        return build_tool_artifact_text_slice_view(
            info=info_view,
            text=text_slice.text,
            offset_chars=text_slice.offset_chars,
            returned_chars=text_slice.returned_chars,
            total_chars=text_slice.total_chars,
            has_more=text_slice.has_more,
        )


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
        policy: ToolResultArtifactProcessingPolicy,
    ) -> tuple[ToolExecutionResult, tuple[ToolResultArtifactRef, ...]]:
        artifact_mode = policy.artifact_mode
        if policy.source_reference_policy == "reuse_input_artifact":
            return result, self._artifact_read_source_refs(result, tool_call)
        if artifact_mode is ToolArtifactMode.NEVER:
            return result, ()

        options = ToolResultArtifactOptions(
            archive_threshold_bytes=policy.archive_threshold_bytes,
            complete_preview_body_chars=policy.complete_preview_body_chars,
            large_preview_chars=policy.large_preview_chars,
            huge_output_chars=policy.huge_output_chars,
            huge_preview_chars=policy.huge_preview_chars,
            streaming_live_head_cap_chars=policy.streaming_live_head_cap_chars,
        )
        candidates = tuple(result.artifact_candidates)
        processed_output = result.output
        processed_display_payload = result.display_payload
        force_archive = artifact_mode in {
            ToolArtifactMode.ALWAYS,
            ToolArtifactMode.STRUCTURED_JSON,
        }
        if not candidates and (
            force_archive
            or len(result.output.encode("utf-8"))
            > options.effective_archive_threshold_bytes
        ):
            media_type = policy.fallback_media_type
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
        """Attach the source artifact ref without recursively archiving a read."""

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


def artifact_processing_policy_for_descriptor(
    *,
    descriptor,
    tool_call: ToolCall,
    options: ToolResultArtifactOptions,
) -> ToolResultArtifactProcessingPolicy:
    """Freeze descriptor and resolved call bounds before artifact processing."""

    if descriptor is None:
        raise ValueError("artifact processing requires a frozen capability descriptor")
    resolved = _options_for_tool_call(options, tool_call)
    return build_tool_result_artifact_processing_policy(
        descriptor_id=descriptor.id,
        descriptor_fingerprint=descriptor.fingerprint(),
        artifact_mode=descriptor.artifact_mode,
        source_reference_policy=(
            "reuse_input_artifact" if descriptor.name == "artifact_read" else "none"
        ),
        archive_threshold_bytes=resolved.effective_archive_threshold_bytes,
        complete_preview_body_chars=resolved.complete_preview_body_chars,
        large_preview_chars=resolved.effective_large_preview_chars,
        huge_output_chars=resolved.huge_output_chars,
        huge_preview_chars=resolved.huge_preview_chars,
        streaming_live_head_cap_chars=resolved.streaming_live_head_cap_chars,
        max_inline_chars=descriptor.max_inline_chars,
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
