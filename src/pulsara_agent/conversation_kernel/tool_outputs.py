"""Store an accepted result's body without making a second display decision."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Mapping

from psycopg import InterfaceError, IsolationLevel, OperationalError
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.blob import CanonicalContentPublisher, MAXIMUM_BLOB_BYTES
from pulsara_agent.conversation_kernel.contracts import CanonicalContent, InlineContent
from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
from pulsara_agent.identity import require_id
from pulsara_agent.ports.terminal_observation import CELL_OUTPUT_MEDIA_TYPE, CellOutputContent
from pulsara_agent.ports.tool_execution import (
    ToolOutputSourceCoverage, ToolOutputSourceCoverageReason, ToolOutputSourceFormatHint,
)
from pulsara_agent.ports.tool_output import (
    ToolOutputBodyCandidate, ToolOutputBodyView, ToolOutputContentError,
    ToolOutputUnavailableReason, parse_tool_output_target,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane, VerifiedPostgresConnectionProviderProtocol,
)

CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES = 65_536
CANONICAL_TOOL_RESULT_PREVIEW_CONTRACT = "pulsara.canonical-tool-result-preview.v3-owned-body"


class KnownOutputPublicationFailure(RuntimeError):
    """The immutable body was not published; the business outcome remains real."""


@dataclass(frozen=True, slots=True)
class PreparedToolOutputBody:
    output_body: CanonicalContent | None
    output_unavailability_reason: ToolOutputUnavailableReason | None
    source_coverage: ToolOutputSourceCoverage
    source_coverage_reason: ToolOutputSourceCoverageReason | None

    def __post_init__(self) -> None:
        if (self.output_body is None) != (self.output_unavailability_reason is not None):
            raise ValueError("body availability must be represented exactly once")
        if (self.source_coverage is ToolOutputSourceCoverage.COMPLETE) != (self.source_coverage_reason is None):
            raise ValueError("tool result source coverage is inconsistent")


@dataclass(frozen=True, slots=True)
class PreparedToolOutputProjection:
    canonical_preview: InlineContent
    output_body: CanonicalContent | None
    output_unavailability_reason: ToolOutputUnavailableReason | None
    source_coverage: ToolOutputSourceCoverage
    source_coverage_reason: ToolOutputSourceCoverageReason | None
    output_omitted: bool

    def __post_init__(self) -> None:
        PreparedToolOutputBody(self.output_body, self.output_unavailability_reason,
                               self.source_coverage, self.source_coverage_reason)
        if self.canonical_preview.size > CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES:
            raise ValueError("tool result preview exceeds its physical inline bound")
        if type(self.output_omitted) is not bool:
            raise TypeError("output_omitted must be a boolean")


class ToolOutputProcessor:
    def __init__(self, connection_provider: VerifiedPostgresConnectionProviderProtocol,
                 *, publisher: CanonicalContentPublisher | None = None) -> None:
        self._publisher = publisher or CanonicalContentPublisher(connection_provider)

    def materialize_body(self, *, session_id: str, workspace_id: str, result_entry_id: str,
                public_output: str, output_body_candidate: ToolOutputBodyCandidate | None,
                public_format: ToolOutputSourceFormatHint,
                deadline_monotonic: float) -> PreparedToolOutputBody:
        require_id(session_id, "session")
        require_id(workspace_id, "workspace")
        require_id(result_entry_id, "entry")
        primary = output_body_candidate or ToolOutputBodyCandidate(
            text=public_output, source_format=public_format,
            source_coverage=ToolOutputSourceCoverage.COMPLETE,
        )
        body = primary.text.encode("utf-8")
        content: CanonicalContent | None = None
        reason: ToolOutputUnavailableReason | None = None
        if len(body) > MAXIMUM_BLOB_BYTES:
            reason = ToolOutputUnavailableReason.BODY_TOO_LARGE
        else:
            media_type = ("application/json" if primary.source_format is ToolOutputSourceFormatHint.JSON
                          else "text/plain")
            arguments = dict(session_id=session_id, content=body, media_type=media_type,
                             codec="utf-8", deadline_monotonic=deadline_monotonic)
            try:
                content = self._publisher.materialize(**arguments)
            except ConversationKernelConflict:
                raise
            except (KnownOutputPublicationFailure, ValueError):
                reason = ToolOutputUnavailableReason.BODY_PUBLICATION_FAILED
            except (TimeoutError, ConnectionError, InterfaceError, OperationalError):
                # Confirm only this immutable body, never rerun the business tool.
                if monotonic() >= deadline_monotonic:
                    reason = ToolOutputUnavailableReason.BODY_PUBLICATION_UNCONFIRMED
                else:
                    try:
                        content = self._publisher.materialize(**arguments)
                    except ConversationKernelConflict:
                        raise
                    except (TimeoutError, ConnectionError, InterfaceError, OperationalError):
                        reason = ToolOutputUnavailableReason.BODY_PUBLICATION_UNCONFIRMED
                    except (KnownOutputPublicationFailure, ValueError):
                        reason = ToolOutputUnavailableReason.BODY_PUBLICATION_FAILED
        return PreparedToolOutputBody(content, reason, primary.source_coverage, primary.source_coverage_reason)

    @staticmethod
    def prepare(*, body: PreparedToolOutputBody, native_output: str,
                output_omitted: bool) -> PreparedToolOutputProjection:
        """Combine already decided P with the accepted body facts; no I/O or slicing."""
        preview = InlineContent.from_bytes(native_output.encode("utf-8"), media_type="text/plain", codec="utf-8")
        return PreparedToolOutputProjection(preview, body.output_body, body.output_unavailability_reason,
            body.source_coverage, body.source_coverage_reason, output_omitted)


def read_tool_output_content(connection, row: Mapping[str, object]) -> bytes:
    """Read and verify C; the caller owns authorization and any historical cut."""
    unavailable = row.get("output_unavailability_reason")
    if unavailable is not None:
        raise ToolOutputContentError(str(unavailable))
    required = ("output_body_digest", "output_body_size", "output_body_media_type", "output_body_codec")
    if any(row.get(name) is None for name in required):
        raise ToolOutputContentError("output_body_descriptor_missing")
    inline, blob_id = row.get("output_body_inline_content"), row.get("output_body_blob_id")
    if (inline is None) == (blob_id is None):
        raise ToolOutputContentError("output_body_storage_missing")
    if inline is not None:
        content = bytes(inline)
    else:
        try:
            require_id(blob_id, "blob")
        except (TypeError, ValueError) as error:
            raise ToolOutputContentError("output_body_descriptor_failed") from error
        blob = connection.execute(
            "SELECT body,logical_digest,logical_size,media_type,codec FROM pulsara_v3.blobs "
            "WHERE id=%s AND workspace_id=%s", (blob_id, row["workspace_id"]),
        ).fetchone()
        if blob is None:
            raise ToolOutputContentError("output_body_content_missing")
        if (blob["logical_digest"] != row["output_body_digest"]
                or blob["logical_size"] != row["output_body_size"]
                or blob["media_type"] != row["output_body_media_type"]
                or blob["codec"] != row["output_body_codec"]):
            raise ToolOutputContentError("output_body_descriptor_failed")
        content = bytes(blob["body"])
    if (len(content) != row["output_body_size"]
            or "sha256:" + sha256(content).hexdigest() != row["output_body_digest"]):
        raise ToolOutputContentError("output_body_integrity_failed")
    if (row["output_body_codec"] != "utf-8"
            or row["output_body_media_type"] not in {"text/plain", "application/json", CELL_OUTPUT_MEDIA_TYPE}):
        raise ToolOutputContentError("output_body_codec_failed")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ToolOutputContentError("output_body_codec_failed") from error
    return content


class PostgresToolOutputReadPort:
    def __init__(self, connection_provider: VerifiedPostgresConnectionProviderProtocol,
                 *, session_id: str, workspace_id: str, operation_timeout_seconds: float = 12.0) -> None:
        require_id(session_id, "session")
        require_id(workspace_id, "workspace")
        if operation_timeout_seconds <= 0:
            raise ValueError("output source timeout must be positive")
        self._provider, self._session_id, self._workspace_id = connection_provider, session_id, workspace_id
        self._timeout = operation_timeout_seconds

    def read_body(self, target: Mapping[str, object]) -> ToolOutputBodyView:
        selected = parse_tool_output_target(target, session_id=self._session_id)
        with self._provider.connection(lane=PostgresConnectionLane.ARTIFACT,
                row_factory=dict_row, deadline_monotonic=monotonic() + self._timeout,
                isolation_level=IsolationLevel.REPEATABLE_READ) as connection:
            row = connection.execute(
                """SELECT e.*, r.output_body_inline_content,r.output_body_blob_id,
                          r.output_body_digest,r.output_body_size,r.output_body_media_type,
                          r.output_body_codec,r.output_unavailability_reason,
                          r.output_source_coverage,r.output_source_coverage_reason,
                          r.model_visible_memory_fact_ids,r.result_facts
                   FROM pulsara_v3.transcript_entries e
                   LEFT JOIN pulsara_v3.tool_results r
                     ON r.session_id=e.session_id AND r.result_entry_id=e.id
                    AND r.workspace_id=e.workspace_id
                   WHERE e.session_id=%s AND e.workspace_id=%s AND e.id=%s
                     AND (e.entry_kind='TOOL_RESULT' OR
                          (e.entry_kind='TERMINAL_OBSERVATION' AND e.input_origin='CELL_OUTPUT'))""",
                (self._session_id, self._workspace_id, selected["entry_id"]),
            ).fetchone()
            if row is None:
                raise KeyError(selected["entry_id"])
            if row["entry_kind"] == "TERMINAL_OBSERVATION":
                if row["content_media_type"] != CELL_OUTPUT_MEDIA_TYPE:
                    raise ToolOutputContentError("cell_output_descriptor_failed")
                body_row = {"output_body_" + key: row[value] for key, value in (
                    ("inline_content", "inline_content"), ("blob_id", "blob_id"),
                    ("digest", "content_digest"), ("size", "content_size"),
                    ("media_type", "content_media_type"), ("codec", "content_codec"))}
                body_row["workspace_id"] = row["workspace_id"]
                content = read_tool_output_content(connection, body_row)
                try:
                    CellOutputContent.from_bytes(content)
                except (TypeError, ValueError) as error:
                    raise ToolOutputContentError("cell_output_content_invalid") from error
                return ToolOutputBodyView(selected, content, CELL_OUTPUT_MEDIA_TYPE, "utf-8",
                                          ToolOutputSourceCoverage.COMPLETE)
            content = read_tool_output_content(connection, row)
            return ToolOutputBodyView(selected, content, str(row["output_body_media_type"]),
                str(row["output_body_codec"]), ToolOutputSourceCoverage(row["output_source_coverage"]),
                None if row["output_source_coverage_reason"] is None else
                ToolOutputSourceCoverageReason(row["output_source_coverage_reason"]),
                tuple(row["model_visible_memory_fact_ids"] or ()), row["result_facts"])


__all__ = ["CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES", "CANONICAL_TOOL_RESULT_PREVIEW_CONTRACT",
           "KnownOutputPublicationFailure", "PreparedToolOutputBody", "PreparedToolOutputProjection", "ToolOutputProcessor",
           "PostgresToolOutputReadPort", "read_tool_output_content"]
