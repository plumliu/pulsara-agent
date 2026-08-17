"""Round 1 tool-output preview, publication, and scoped read owners."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from time import monotonic
from typing import Mapping, Protocol

from psycopg import InterfaceError, IsolationLevel, OperationalError
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.blob import (
    MAXIMUM_BLOB_BYTES,
    PostgresCanonicalBlobStore,
)
from pulsara_agent.conversation_kernel.contracts import BlobContent, InlineContent
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
from pulsara_agent.ports.artifact import (
    ArtifactContentError,
    ToolArtifactInfoView,
    ToolArtifactRecordView,
    ToolArtifactTextSliceView,
    ToolOutputArtifactDisposition,
    ToolOutputArtifactUnavailabilityReason,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import (
    ToolOutputArtifactCandidate,
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
    ToolOutputSourceFormatHint,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)


ARTIFACT_ARCHIVE_THRESHOLD_BYTES = 8_000
HEAD_TAIL_PREVIEW_CHARS = 8_000
ARTIFACT_READ_DEFAULT_CHARS = 20_000
ARTIFACT_READ_HARD_CHARS = 32_000
CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES = 65_536
PRIMARY_TOOL_OUTPUT_MEDIA_TYPE = "text/plain"
PRIMARY_TOOL_OUTPUT_CODEC = "utf-8"
_HEAD_RATIO = 0.65
CANONICAL_TOOL_RESULT_PREVIEW_CONTRACT = (
    "pulsara.canonical-tool-result-preview.v2-utf8-logical-bound"
)


class ToolOutputArtifactPublisher(Protocol):
    def publish(
        self,
        *,
        workspace_id: str,
        content: bytes,
        media_type: str,
        codec: str,
        deadline_monotonic: float,
    ) -> BlobContent: ...


class KnownArtifactPublicationFailure(RuntimeError):
    """A publication failed without an ambiguous commit acknowledgement."""


@dataclass(frozen=True, slots=True)
class PreparedToolOutputProjection:
    canonical_preview: InlineContent
    artifact_disposition: ToolOutputArtifactDisposition
    artifact_id: str | None
    artifact_blob: BlobContent | None
    source_coverage: ToolOutputSourceCoverage
    display_kind: ToolResultDisplayKind
    source_coverage_reason: ToolOutputSourceCoverageReason | None
    artifact_unavailability_reason: ToolOutputArtifactUnavailabilityReason | None
    candidate_utf8_bytes: int
    candidate_chars: int
    visible_head_chars: int
    visible_tail_chars: int
    omitted_middle_chars: int

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_preview, InlineContent):
            raise TypeError("tool result preview must be inline")
        if self.canonical_preview.size > CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES:
            raise ValueError("tool result preview exceeds its hard bound")
        available = self.artifact_disposition in {
            ToolOutputArtifactDisposition.AVAILABLE,
            ToolOutputArtifactDisposition.INCOMPLETE,
        }
        if available != (
            self.artifact_id is not None and self.artifact_blob is not None
        ):
            raise ValueError("tool artifact edge does not match its disposition")
        unavailable = (
            self.artifact_disposition is ToolOutputArtifactDisposition.UNAVAILABLE
        )
        if unavailable != (self.artifact_unavailability_reason is not None):
            raise ValueError("tool artifact unavailable reason is inconsistent")
        if (self.source_coverage is ToolOutputSourceCoverage.COMPLETE) != (
            self.source_coverage_reason is None
        ):
            raise ValueError("tool artifact source coverage is inconsistent")


class ToolOutputArtifactProcessor:
    """Build the sole inline preview and optional immutable primary blob."""

    def __init__(
        self,
        connection_provider: VerifiedPostgresConnectionProviderProtocol,
        *,
        publisher: ToolOutputArtifactPublisher | None = None,
    ) -> None:
        self._publisher = publisher or PostgresCanonicalBlobStore(connection_provider)

    def prepare(
        self,
        *,
        workspace_id: str,
        result_entry_id: str,
        public_output: str,
        candidate: ToolOutputArtifactCandidate | None,
        artifact_source_read: bool,
        deadline_monotonic: float,
    ) -> PreparedToolOutputProjection:
        if not workspace_id or not result_entry_id:
            raise ValueError("tool output artifact identity is incomplete")
        public_output.encode("utf-8")
        if artifact_source_read and candidate is not None:
            raise ValueError("artifact_read cannot recursively publish an artifact")
        primary = candidate or ToolOutputArtifactCandidate(
            role="OUTPUT",
            text=public_output,
            source_coverage=ToolOutputSourceCoverage.COMPLETE,
            original_utf8_bytes=len(public_output.encode("utf-8")),
        )
        body = primary.text.encode("utf-8")
        artifact_id: str | None = None
        blob: BlobContent | None = None
        unavailable_reason: ToolOutputArtifactUnavailabilityReason | None = None
        needs_artifact = not artifact_source_read and (
            primary.source_coverage is ToolOutputSourceCoverage.RETAINED_SNAPSHOT
            or len(body) > ARTIFACT_ARCHIVE_THRESHOLD_BYTES
        )
        if not needs_artifact:
            disposition = ToolOutputArtifactDisposition.NOT_REQUIRED
        elif len(body) > MAXIMUM_BLOB_BYTES:
            disposition = ToolOutputArtifactDisposition.UNAVAILABLE
            unavailable_reason = (
                ToolOutputArtifactUnavailabilityReason.ARTIFACT_CONTENT_TOO_LARGE
            )
        else:
            artifact_id = _artifact_id(result_entry_id)
            blob, unavailable_reason = self._publish_exact(
                workspace_id=workspace_id,
                body=body,
                deadline_monotonic=deadline_monotonic,
            )
            if blob is None:
                artifact_id = None
                disposition = ToolOutputArtifactDisposition.UNAVAILABLE
            elif primary.source_coverage is ToolOutputSourceCoverage.COMPLETE:
                disposition = ToolOutputArtifactDisposition.AVAILABLE
            else:
                disposition = ToolOutputArtifactDisposition.INCOMPLETE

        display = _build_final_preview(
            public_output=public_output,
            candidate=primary,
            artifact_source_read=artifact_source_read,
            artifact_disposition=disposition,
            artifact_id=artifact_id,
            artifact_unavailability_reason=unavailable_reason,
        )
        return PreparedToolOutputProjection(
            canonical_preview=InlineContent.from_bytes(
                display.encoded,
                media_type="text/plain",
                codec="utf-8",
            ),
            artifact_disposition=disposition,
            artifact_id=artifact_id,
            artifact_blob=blob,
            source_coverage=primary.source_coverage,
            display_kind=display.kind,
            source_coverage_reason=primary.source_coverage_reason,
            artifact_unavailability_reason=unavailable_reason,
            candidate_utf8_bytes=len(body),
            candidate_chars=len(primary.text),
            visible_head_chars=display.visible_head_chars,
            visible_tail_chars=display.visible_tail_chars,
            omitted_middle_chars=display.omitted_middle_chars,
        )

    def _publish_exact(
        self,
        *,
        workspace_id: str,
        body: bytes,
        deadline_monotonic: float,
    ) -> tuple[BlobContent | None, ToolOutputArtifactUnavailabilityReason | None]:
        try:
            return (
                self._publisher.publish(
                    workspace_id=workspace_id,
                    content=body,
                    media_type=PRIMARY_TOOL_OUTPUT_MEDIA_TYPE,
                    codec=PRIMARY_TOOL_OUTPUT_CODEC,
                    deadline_monotonic=deadline_monotonic,
                ),
                None,
            )
        except ConversationKernelConflict:
            raise
        except KnownArtifactPublicationFailure:
            return (
                None,
                ToolOutputArtifactUnavailabilityReason.BLOB_PUBLICATION_FAILED,
            )
        except ValueError:
            return (
                None,
                ToolOutputArtifactUnavailabilityReason.BLOB_PUBLICATION_FAILED,
            )
        except (TimeoutError, ConnectionError, InterfaceError, OperationalError):
            # The immutable content-addressed write may have committed without
            # its acknowledgement.  Reissuing this exact candidate is the
            # confirmation operation; it never re-runs the physical tool.
            if monotonic() >= deadline_monotonic:
                return (
                    None,
                    ToolOutputArtifactUnavailabilityReason.BLOB_PUBLICATION_UNCONFIRMED,
                )
            try:
                return (
                    self._publisher.publish(
                        workspace_id=workspace_id,
                        content=body,
                        media_type=PRIMARY_TOOL_OUTPUT_MEDIA_TYPE,
                        codec=PRIMARY_TOOL_OUTPUT_CODEC,
                        deadline_monotonic=deadline_monotonic,
                    ),
                    None,
                )
            except ConversationKernelConflict:
                raise
            except (TimeoutError, ConnectionError, InterfaceError, OperationalError):
                return (
                    None,
                    ToolOutputArtifactUnavailabilityReason.BLOB_PUBLICATION_UNCONFIRMED,
                )
            except Exception:
                return (
                    None,
                    ToolOutputArtifactUnavailabilityReason.BLOB_PUBLICATION_FAILED,
                )
        except Exception:
            return (
                None,
                ToolOutputArtifactUnavailabilityReason.BLOB_PUBLICATION_FAILED,
            )


@dataclass(frozen=True, slots=True)
class _RenderedPreview:
    encoded: bytes
    kind: ToolResultDisplayKind
    visible_head_chars: int
    visible_tail_chars: int
    omitted_middle_chars: int


def _build_final_preview(
    *,
    public_output: str,
    candidate: ToolOutputArtifactCandidate,
    artifact_source_read: bool,
    artifact_disposition: ToolOutputArtifactDisposition,
    artifact_id: str | None,
    artifact_unavailability_reason: ToolOutputArtifactUnavailabilityReason | None,
) -> _RenderedPreview:
    envelope = _preview_envelope(public_output, candidate)
    candidate_utf8_bytes = len(candidate.text.encode("utf-8"))
    prefer_complete = (
        candidate_utf8_bytes
        <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
    )
    if prefer_complete:
        complete_body = candidate.text + _complete_footer(
            candidate=candidate,
            artifact_source_read=artifact_source_read,
            disposition=artifact_disposition,
            artifact_id=artifact_id,
            unavailable_reason=artifact_unavailability_reason,
        )
        encoded = _render_envelope(envelope, complete_body)
        if len(encoded) <= CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES:
            return _RenderedPreview(
                encoded,
                ToolResultDisplayKind.COMPLETE,
                len(candidate.text),
                0,
                0,
            )
        if artifact_source_read:
            raise ValueError(
                "artifact_read response exceeds its non-recursive inline bound"
            )

    if artifact_source_read:
        raise ValueError(
            "artifact_read response exceeds the provider logical FULL bound"
        )

    maximum_visible = min(HEAD_TAIL_PREVIEW_CHARS, len(candidate.text))
    low = 0
    high = maximum_visible
    winner: tuple[bytes, int, int, int] | None = None
    while low <= high:
        visible = (low + high) // 2
        body, head, tail, omitted = _head_tail_body(
            candidate=candidate,
            visible_chars=visible,
            disposition=artifact_disposition,
            artifact_id=artifact_id,
            unavailable_reason=artifact_unavailability_reason,
        )
        encoded = _render_envelope(envelope, body)
        if (
            len(body) <= HEAD_TAIL_PREVIEW_CHARS
            and len(encoded) <= CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES
        ):
            winner = encoded, head, tail, omitted
            low = visible + 1
        else:
            high = visible - 1
    if winner is None:
        raise ValueError("tool result envelope alone exceeds the inline hard bound")
    encoded, head, tail, omitted = winner
    return _RenderedPreview(
        encoded,
        ToolResultDisplayKind.HEAD_TAIL,
        head,
        tail,
        omitted,
    )


def _preview_envelope(
    public_output: str, candidate: ToolOutputArtifactCandidate
) -> dict[str, object] | None:
    if candidate.source_format_hint is not ToolOutputSourceFormatHint.JSON:
        return None
    try:
        value = json.loads(public_output)
    except json.JSONDecodeError as exc:
        raise ValueError("tool JSON preview envelope is invalid") from exc
    if not isinstance(value, dict) or "output" not in value:
        raise ValueError("tool JSON preview envelope lacks its output field")
    return {str(key): nested for key, nested in value.items()}


def _render_envelope(envelope: dict[str, object] | None, body: str) -> bytes:
    if envelope is None:
        return body.encode("utf-8")
    rendered = dict(envelope)
    rendered["output"] = body
    return json.dumps(
        rendered,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _complete_footer(
    *,
    candidate: ToolOutputArtifactCandidate,
    artifact_source_read: bool,
    disposition: ToolOutputArtifactDisposition,
    artifact_id: str | None,
    unavailable_reason: ToolOutputArtifactUnavailabilityReason | None,
) -> str:
    if artifact_source_read:
        return ""
    parts: list[str] = []
    if artifact_id is not None:
        parts.append(
            "\n\n[TOOL OUTPUT ARTIFACT: full retained output is available as "
            f"artifact_id={artifact_id}. If an exact reread is necessary for "
            "the current task, read the retained artifact with artifact_read("
            f'{{"artifact_id":"{artifact_id}","offset_chars":0,'
            '"max_chars":20000}); otherwise continue from the complete visible '
            "result without opening the artifact.]"
        )
    elif disposition is ToolOutputArtifactDisposition.UNAVAILABLE:
        reason = (
            "" if unavailable_reason is None else f" reason={unavailable_reason.value}."
        )
        parts.append(
            "\n\n[ARTIFACT UNAVAILABLE: the complete candidate output is shown "
            "inline, but no separate readable artifact was retained. Do not "
            f"retry the tool automatically.{reason}]"
        )
    parts.append(_coverage_warning(candidate))
    return "".join(parts)


def _head_tail_body(
    *,
    candidate: ToolOutputArtifactCandidate,
    visible_chars: int,
    disposition: ToolOutputArtifactDisposition,
    artifact_id: str | None,
    unavailable_reason: ToolOutputArtifactUnavailabilityReason | None,
) -> tuple[str, int, int, int]:
    visible_chars = max(0, min(visible_chars, len(candidate.text)))
    head = int(visible_chars * _HEAD_RATIO)
    tail = visible_chars - head
    omitted = max(0, len(candidate.text) - head - tail)
    marker = _omission_marker(
        omitted=omitted,
        head=head,
        disposition=disposition,
        artifact_id=artifact_id,
        unavailable_reason=unavailable_reason,
    )
    body = (
        candidate.text[:head]
        + marker
        + (candidate.text[-tail:] if tail else "")
        + _coverage_warning(candidate)
    )
    return body, head, tail, omitted


def _omission_marker(
    *,
    omitted: int,
    head: int,
    disposition: ToolOutputArtifactDisposition,
    artifact_id: str | None,
    unavailable_reason: ToolOutputArtifactUnavailabilityReason | None,
) -> str:
    if artifact_id is not None:
        return (
            f"\n\n[OUTPUT TRUNCATED / PREVIEW: omitted {omitted} chars from the middle.\n"
            f"Full retained output: artifact_id={artifact_id}\n"
            "If the omitted content is necessary for the current task, read the "
            f'retained artifact with artifact_read({{"artifact_id":"{artifact_id}",'
            f'"offset_chars":{head},"max_chars":20000}}) '
            "to inspect content after the visible head; otherwise continue from "
            "the visible result without opening the artifact.]\n\n"
        )
    reason = (
        "" if unavailable_reason is None else f" reason={unavailable_reason.value}."
    )
    return (
        f"\n\n[OUTPUT TRUNCATED / PREVIEW: omitted {omitted} chars from the middle.\n"
        "OUTPUT RETENTION UNAVAILABLE: the tool outcome is known and accepted, "
        "but omitted output could not be retained. Do not retry the tool "
        f"automatically.{reason}]\n\n"
    )


def _coverage_warning(candidate: ToolOutputArtifactCandidate) -> str:
    if candidate.source_coverage is ToolOutputSourceCoverage.COMPLETE:
        return ""
    return (
        "\n\n[SOURCE COVERAGE: retained snapshot only; earlier output is unavailable.\n"
        "artifact_read offsets are relative to the retained artifact body, not "
        "the original process stream.]"
    )


def _artifact_id(result_entry_id: str) -> str:
    digest = sha256(f"{result_entry_id}\0OUTPUT".encode("utf-8")).hexdigest()
    return f"artifact:tool-result:{digest}"


class PostgresToolArtifactReadPort:
    """Exact session/workspace-scoped canonical artifact query owner."""

    def __init__(
        self,
        connection_provider: VerifiedPostgresConnectionProviderProtocol,
        *,
        session_id: str,
        workspace_id: str,
        operation_timeout_seconds: float = 12.0,
    ) -> None:
        if not session_id or not workspace_id or operation_timeout_seconds <= 0:
            raise ValueError("artifact read scope is invalid")
        self._provider = connection_provider
        self._session_id = session_id
        self._workspace_id = workspace_id
        self._timeout = operation_timeout_seconds

    def lookup(self, artifact_id: str) -> ToolArtifactRecordView | None:
        row = self._fetch(artifact_id, include_body=False)
        return None if row is None else self._record(row)

    def info(self, artifact_id: str) -> ToolArtifactInfoView:
        row = self._fetch(artifact_id, include_body=True)
        if row is None:
            raise KeyError(artifact_id)
        record = self._record(row)
        self._verified_text(row, record)
        return ToolArtifactInfoView(record)

    def read_text(
        self, artifact_id: str, *, offset_chars: int, max_chars: int
    ) -> ToolArtifactTextSliceView:
        if offset_chars < 0 or not 1 <= max_chars <= ARTIFACT_READ_HARD_CHARS:
            raise ValueError("artifact text range is outside the closed bound")
        row = self._fetch(artifact_id, include_body=True)
        if row is None:
            raise KeyError(artifact_id)
        record = self._record(row)
        text = self._verified_text(row, record)
        total = len(text)
        value = text[offset_chars : offset_chars + max_chars]
        returned = len(value)
        next_offset = offset_chars + returned
        has_more = next_offset < total
        return ToolArtifactTextSliceView(
            info=ToolArtifactInfoView(record),
            text=value,
            offset_chars=offset_chars,
            returned_chars=returned,
            total_chars=total,
            has_more=has_more,
            next_offset_chars=next_offset if has_more else None,
        )

    @staticmethod
    def _verified_text(
        row: Mapping[str, object], record: ToolArtifactRecordView
    ) -> str:
        content = bytes(row["body"])
        if len(content) != record.size_bytes or (
            "sha256:" + sha256(content).hexdigest() != record.digest
        ):
            raise ArtifactContentError("artifact_content_integrity_failed")
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactContentError("artifact_content_codec_failed") from exc

    def _fetch(
        self, artifact_id: str, *, include_body: bool
    ) -> Mapping[str, object] | None:
        if not artifact_id:
            return None
        body_column = ", b.body" if include_body else ""
        query = f"""
            SELECT r.output_artifact_id, r.output_artifact_disposition,
                   r.output_source_coverage, r.output_display_kind,
                   r.output_source_coverage_reason,
                   r.output_artifact_unavailability_reason,
                   r.model_visible_memory_fact_ids, r.accepted_at,
                   b.id AS blob_id, b.logical_digest, b.logical_size,
                   b.media_type, b.codec{body_column}
            FROM pulsara_v3.tool_results AS r
            JOIN pulsara_v3.sessions AS s
              ON s.id = r.session_id AND s.workspace_id = r.workspace_id
            LEFT JOIN pulsara_v3.blobs AS b
              ON b.id = r.output_artifact_blob_id
             AND b.workspace_id = r.workspace_id
            WHERE r.session_id = %s AND r.workspace_id = %s
              AND r.output_artifact_id = %s
              AND r.output_artifact_disposition IN ('AVAILABLE', 'INCOMPLETE')
        """
        with self._provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + self._timeout,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            rows = connection.execute(
                query,
                (self._session_id, self._workspace_id, artifact_id),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ConversationKernelConflict("artifact handle is not unique")
        row = rows[0]
        if any(
            row[name] is None
            for name in (
                "blob_id",
                "logical_digest",
                "logical_size",
                "media_type",
                "codec",
            )
        ):
            raise ArtifactContentError("artifact_content_missing")
        if (
            row["media_type"] != PRIMARY_TOOL_OUTPUT_MEDIA_TYPE
            or row["codec"] != "utf-8"
        ):
            raise ArtifactContentError("artifact_content_descriptor_failed")
        return row

    @staticmethod
    def _record(row: Mapping[str, object]) -> ToolArtifactRecordView:
        return ToolArtifactRecordView(
            artifact_id=str(row["output_artifact_id"]),
            role="OUTPUT",
            media_type=str(row["media_type"]),
            size_bytes=int(row["logical_size"]),
            artifact_disposition=ToolOutputArtifactDisposition(
                str(row["output_artifact_disposition"])
            ),
            source_coverage=ToolOutputSourceCoverage(
                str(row["output_source_coverage"])
            ),
            display_kind=ToolResultDisplayKind(str(row["output_display_kind"])),
            source_coverage_reason=(
                None
                if row["output_source_coverage_reason"] is None
                else ToolOutputSourceCoverageReason(
                    str(row["output_source_coverage_reason"])
                )
            ),
            artifact_unavailability_reason=(
                None
                if row["output_artifact_unavailability_reason"] is None
                else ToolOutputArtifactUnavailabilityReason(
                    str(row["output_artifact_unavailability_reason"])
                )
            ),
            blob_id=str(row["blob_id"]),
            digest=str(row["logical_digest"]),
            codec=str(row["codec"]),
            accepted_at_utc=row["accepted_at"].isoformat(),  # type: ignore[union-attr]
            model_visible_memory_fact_ids=tuple(
                str(value) for value in row["model_visible_memory_fact_ids"]
            ),
        )


__all__ = [
    "ARTIFACT_ARCHIVE_THRESHOLD_BYTES",
    "ARTIFACT_READ_DEFAULT_CHARS",
    "ARTIFACT_READ_HARD_CHARS",
    "CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES",
    "CANONICAL_TOOL_RESULT_PREVIEW_CONTRACT",
    "HEAD_TAIL_PREVIEW_CHARS",
    "KnownArtifactPublicationFailure",
    "PostgresToolArtifactReadPort",
    "PreparedToolOutputProjection",
    "PRIMARY_TOOL_OUTPUT_CODEC",
    "PRIMARY_TOOL_OUTPUT_MEDIA_TYPE",
    "ToolOutputArtifactProcessor",
    "ToolOutputArtifactPublisher",
]
