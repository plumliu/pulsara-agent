"""Tool result artifact archiving and ownership index."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.event import EventContext
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.message import ToolResultArtifactRef
from pulsara_agent.runtime.terminal.output import finalize_output
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult, ToolResultArtifactCandidate


DEFAULT_TOOL_ARTIFACT_THRESHOLD_CHARS = 8_000
DEFAULT_TOOL_ARTIFACT_PREVIEW_CHARS = 8_000
DEFAULT_TOOL_RESULT_CONTEXT_CHARS = 8_000
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


@dataclass(frozen=True, slots=True)
class ToolResultArtifactOptions:
    archive_threshold_chars: int = DEFAULT_TOOL_ARTIFACT_THRESHOLD_CHARS
    inline_preview_chars: int = DEFAULT_TOOL_ARTIFACT_PREVIEW_CHARS
    tool_result_context_chars: int = DEFAULT_TOOL_RESULT_CONTEXT_CHARS

    def __post_init__(self) -> None:
        if self.archive_threshold_chars < 1:
            raise ValueError("archive_threshold_chars must be >= 1")
        if self.inline_preview_chars < 1:
            raise ValueError("inline_preview_chars must be >= 1")
        if self.tool_result_context_chars < 1:
            raise ValueError("tool_result_context_chars must be >= 1")
        if self.archive_threshold_chars > self.tool_result_context_chars:
            raise ValueError("archive_threshold_chars must be <= tool_result_context_chars")


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
    ) -> tuple[ToolExecutionResult, tuple[ToolResultArtifactRef, ...]]:
        if result.tool_name == "artifact_read":
            return result, ()

        candidates = tuple(result.artifact_candidates)
        processed_output = result.output
        if not candidates and len(result.output.encode("utf-8")) > self.options.archive_threshold_chars:
            candidates = (
                ToolResultArtifactCandidate(
                    role="output",
                    media_type="text/plain; charset=utf-8",
                    text=result.output,
                    metadata={"fallback": True},
                ),
            )

        refs: list[ToolResultArtifactRef] = []
        for ordinal, candidate in enumerate(candidates):
            size_bytes = _candidate_size_bytes(candidate)
            if size_bytes <= self.options.archive_threshold_chars:
                continue
            refs.append(
                self._archive_candidate(
                    candidate,
                    event_context=event_context,
                    tool_call=tool_call,
                    ordinal=ordinal,
                    size_bytes=size_bytes,
                )
            )

        if refs and len(processed_output) > self.options.inline_preview_chars:
            processed_output = finalize_output(
                processed_output,
                max_chars=self.options.inline_preview_chars,
            ).text

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
            metadata=metadata,
        )
        self.index.put(record)
        return ToolResultArtifactRef(
            artifact_id=write.id,
            role=candidate.role,
            media_type=candidate.media_type,
            size_bytes=write.size_bytes,
            stored_complete=candidate.stored_complete,
            loss_reason=candidate.loss_reason,
        )


def _candidate_size_bytes(candidate: ToolResultArtifactCandidate) -> int:
    if candidate.text is not None:
        return len(candidate.text.encode("utf-8"))
    assert candidate.data is not None
    return len(candidate.data)


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
