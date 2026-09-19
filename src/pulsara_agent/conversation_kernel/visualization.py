"""One-turn HTML subscription values and bounded publication reads.

The file is only a mutable authoring source.  The assistant publication owns
the first durable HTML bytes and the user-visible occurrence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import stat

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.blob import (
    MAXIMUM_BLOB_BYTES,
    PostgresCanonicalBlobStore,
)
from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)
from pulsara_agent.tools.builtins.filesystem import IMAGE_REFERENCE_PATTERN


VISUALIZATION_MEDIA_TYPE = "text/html"
VISUALIZATION_CODEC = "utf-8"


class VisualizationSourceKind(StrEnum):
    PATH = "path"
    VISUALIZATION_REF = "visualization_ref"


@dataclass(frozen=True, slots=True)
class VisualizationSource:
    kind: VisualizationSourceKind
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("visualization source is empty")
        if self.kind is VisualizationSourceKind.VISUALIZATION_REF and (
            IMAGE_REFERENCE_PATTERN.fullmatch(self.value) is None
        ):
            raise ValueError("visualization reference is invalid")

    def provider_value(self) -> dict[str, str]:
        return {self.kind.value: self.value}


@dataclass(frozen=True, slots=True)
class VisualizationSubscription:
    turn_id: str
    source_result_entry_id: str
    source: VisualizationSource


class VisualizationOccurrenceState(StrEnum):
    READY = "READY"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class FrozenVisualizationOccurrence:
    source_result_entry_id: str
    state: VisualizationOccurrenceState
    html: bytes | None = None
    failure_code: str | None = None
    failure_detail: str | None = None

    def __post_init__(self) -> None:
        if self.state is VisualizationOccurrenceState.READY:
            if not self.html or self.failure_code or self.failure_detail:
                raise ValueError("READY visualization has invalid content")
        elif self.html is not None or not self.failure_code or not self.failure_detail:
            raise ValueError("FAILED visualization has invalid content")


def parse_visualization_source(arguments: dict[str, object]) -> tuple[VisualizationSource, bool]:
    if set(arguments) - {"path", "visualization_ref", "review"}:
        raise ValueError("visualization_render has unsupported arguments")
    path = arguments.get("path")
    reference = arguments.get("visualization_ref")
    review = arguments.get("review", False)
    if not isinstance(review, bool):
        raise ValueError("visualization_render review must be a boolean")
    if (isinstance(path, str) and bool(path)) == (
        isinstance(reference, str) and bool(reference)
    ):
        raise ValueError("visualization_render requires exactly one source")
    if path is not None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("visualization_render path is invalid")
        return VisualizationSource(VisualizationSourceKind.PATH, path), review
    if not isinstance(reference, str):
        raise ValueError("visualization_render reference is invalid")
    return VisualizationSource(VisualizationSourceKind.VISUALIZATION_REF, reference), review


def read_visualization_file(path: Path) -> bytes | None:
    """Return None only for a reliable not-found; every other failure is typed."""
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        facts = os.fstat(descriptor)
        if not stat.S_ISREG(facts.st_mode):
            raise ValueError("HTML_NOT_REGULAR")
        if facts.st_size > MAXIMUM_BLOB_BYTES:
            raise ValueError("HTML_TOO_LARGE")
        remaining = MAXIMUM_BLOB_BYTES + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) > MAXIMUM_BLOB_BYTES:
            raise ValueError("HTML_TOO_LARGE")
        if not body:
            raise ValueError("HTML_EMPTY")
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("HTML_NOT_UTF8") from exc
        return body
    except FileNotFoundError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class PostgresCanonicalVisualizationReadPort:
    """Resolve a digest only through a READY owner in the current session."""

    def __init__(
        self, provider: VerifiedPostgresConnectionProviderProtocol,
        *, session_id: str, workspace_id: str,
    ) -> None:
        self._provider = provider
        self._session_id = session_id
        self._workspace_id = workspace_id

    def read_ref(self, reference: str, *, deadline_monotonic: float) -> bytes:
        if IMAGE_REFERENCE_PATTERN.fullmatch(reference) is None:
            raise ValueError("visualization reference is invalid")
        with self._provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            row = connection.execute(
                """SELECT v.blob_id, b.logical_size
                   FROM pulsara_v3.assistant_visualizations AS v
                   JOIN pulsara_v3.blobs AS b
                     ON b.id = v.blob_id AND b.workspace_id = v.workspace_id
                   WHERE v.session_id = %s AND v.workspace_id = %s
                     AND v.state = 'READY' AND b.logical_digest = %s
                     AND b.media_type = %s AND b.codec = %s
                   ORDER BY v.assistant_entry_id, v.ordinal LIMIT 1""",
                (
                    self._session_id, self._workspace_id, reference,
                    VISUALIZATION_MEDIA_TYPE, VISUALIZATION_CODEC,
                ),
            ).fetchone()
            if row is None:
                raise KeyError(reference)
            return PostgresCanonicalBlobStore.read_exact_in_connection(
                connection,
                blob_id=str(row["blob_id"]),
                expected_digest=reference,
                expected_size=int(row["logical_size"]),
                expected_workspace_id=self._workspace_id,
                expected_media_type=VISUALIZATION_MEDIA_TYPE,
                expected_codec=VISUALIZATION_CODEC,
            )


def materialize_visualization_subscription(
    subscription: VisualizationSubscription,
    reader: PostgresCanonicalVisualizationReadPort,
    *, deadline_monotonic: float,
) -> FrozenVisualizationOccurrence | None:
    try:
        if subscription.source.kind is VisualizationSourceKind.PATH:
            body = read_visualization_file(Path(subscription.source.value))
            if body is None:
                return None
        else:
            body = reader.read_ref(
                subscription.source.value, deadline_monotonic=deadline_monotonic
            )
        if not body:
            raise ValueError("HTML_EMPTY")
        if len(body) > MAXIMUM_BLOB_BYTES:
            raise ValueError("HTML_TOO_LARGE")
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("HTML_NOT_UTF8") from exc
        return FrozenVisualizationOccurrence(
            subscription.source_result_entry_id,
            VisualizationOccurrenceState.READY,
            html=body,
        )
    except (PermissionError, IsADirectoryError):
        code = "HTML_READ_DENIED"
    except KeyError:
        code = "VISUALIZATION_REF_UNAVAILABLE"
    except ConversationKernelConflict:
        code = "VISUALIZATION_REF_CORRUPT"
    except ValueError as exc:
        code = str(exc)
    except OSError:
        code = "HTML_READ_FAILED"
    return FrozenVisualizationOccurrence(
        subscription.source_result_entry_id,
        VisualizationOccurrenceState.FAILED,
        failure_code=code,
        failure_detail=f"Visualization could not be displayed ({code}).",
    )


__all__ = [
    "FrozenVisualizationOccurrence",
    "VisualizationOccurrenceState",
    "VisualizationSource",
    "VisualizationSourceKind",
    "VisualizationSubscription",
    "VISUALIZATION_CODEC",
    "VISUALIZATION_MEDIA_TYPE",
    "PostgresCanonicalVisualizationReadPort",
    "materialize_visualization_subscription",
    "parse_visualization_source",
    "read_visualization_file",
]
