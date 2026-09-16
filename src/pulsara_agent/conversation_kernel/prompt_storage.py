"""Transactional storage join for canonical prompt bodies and image refs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from psycopg import Connection, IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
    PostgresCanonicalBlobStore,
    _blob_id,
)
from pulsara_agent.conversation_kernel.contracts import CanonicalContent, InlineContent
from pulsara_agent.conversation_kernel.prompt_content import (
    CanonicalPromptBody,
    CanonicalPromptImageDescriptor,
    FrozenCanonicalPrompt,
    PROMPT_BODY_CODEC,
    PROMPT_BODY_MEDIA_TYPE,
    PROMPT_IMAGE_BLOB_CODEC,
    canonical_prompt_body_resource_quote,
    decode_canonical_prompt_body,
    hydrate_canonical_prompt_body,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CONTEXT_SNAPSHOT_CODEC,
    CONTEXT_SNAPSHOT_MEDIA_TYPE,
    CompactionSnapshotCarrier,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    compaction_snapshot_canonical_expanded_bytes,
    compaction_snapshot_image_descriptors,
    compaction_snapshot_image_parts,
    parse_compaction_snapshot_carrier,
)
from pulsara_agent.conversation_kernel.repository_errors import (
    ConversationKernelConflict,
)
from pulsara_agent.model_input.contracts import (
    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
)
from pulsara_agent.llm.input import LLMImagePart
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


@dataclass(frozen=True, slots=True)
class CanonicalPromptPublication:
    body: CanonicalContent
    image_blob_ids: tuple[str, ...] = field(repr=False)


class CanonicalImageReferenceUnavailable(LookupError):
    pass


class CanonicalImageReferenceResourceExceeded(ValueError):
    pass


class CanonicalImageReferenceReadPort(Protocol):
    def read_image(
        self,
        *,
        session_id: str,
        workspace_id: str,
        image_ref: str,
        maximum_encoded_bytes: int,
        deadline_monotonic: float,
    ) -> LLMImagePart: ...


class PostgresCanonicalImageReferenceReadPort:
    """Exact, session/workspace-scoped read owner for one known image digest."""

    def __init__(
        self,
        connection_provider: VerifiedPostgresConnectionProviderProtocol,
        *,
        session_id: str,
        workspace_id: str,
    ) -> None:
        if not session_id or not workspace_id:
            raise ValueError("canonical image reference scope is invalid")
        self._provider = connection_provider
        self._session_id = session_id
        self._workspace_id = workspace_id

    def read_image(
        self,
        *,
        session_id: str,
        workspace_id: str,
        image_ref: str,
        maximum_encoded_bytes: int,
        deadline_monotonic: float,
    ) -> LLMImagePart:
        if session_id != self._session_id or workspace_id != self._workspace_id:
            raise RuntimeError("canonical image reference scope does not exact-join")
        if maximum_encoded_bytes < 1:
            raise CanonicalImageReferenceResourceExceeded(
                "canonical image reference exceeds its encoded-byte allowance"
            )
        blob_id = _blob_id(workspace_id, image_ref)
        with self._provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            candidate = connection.execute(
                """
                WITH candidates AS (
                    SELECT 0 AS owner_kind_rank,
                           e.id AS owner_id,
                           r.ref_ordinal,
                           e.content_size
                    FROM pulsara_v3.canonical_image_refs AS r
                    JOIN pulsara_v3.transcript_entries AS e
                      ON e.session_id = r.session_id
                     AND e.workspace_id = r.workspace_id
                     AND e.id = r.transcript_entry_id
                    WHERE r.session_id = %s
                      AND r.workspace_id = %s
                      AND r.blob_id = %s
                      AND e.entry_kind IN (
                          'USER_MESSAGE', 'USER_STEER', 'TOOL_RESULT'
                      )
                    UNION ALL
                    SELECT 1 AS owner_kind_rank,
                           s.id AS owner_id,
                           r.ref_ordinal,
                           s.content_size
                    FROM pulsara_v3.canonical_image_refs AS r
                    JOIN pulsara_v3.context_snapshots AS s
                      ON s.session_id = r.session_id
                     AND s.workspace_id = r.workspace_id
                     AND s.id = r.context_snapshot_id
                    WHERE r.session_id = %s
                      AND r.workspace_id = %s
                      AND r.blob_id = %s
                )
                SELECT owner_kind_rank, owner_id, ref_ordinal, content_size
                FROM candidates
                ORDER BY content_size ASC,
                         owner_kind_rank ASC,
                         owner_id COLLATE "C" ASC,
                         ref_ordinal ASC
                LIMIT 1
                """,
                (
                    session_id,
                    workspace_id,
                    blob_id,
                    session_id,
                    workspace_id,
                    blob_id,
                ),
            ).fetchone()
            if candidate is None:
                raise CanonicalImageReferenceUnavailable(image_ref)
            content_size = int(candidate["content_size"])
            if not 0 <= content_size <= MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES:
                raise ConversationKernelConflict(
                    "canonical image owner body size is out of bounds"
                )
            owner_kind_rank = int(candidate["owner_kind_rank"])
            owner_id = str(candidate["owner_id"])
            ref_ordinal = int(candidate["ref_ordinal"])
            if owner_kind_rank == 0:
                row = connection.execute(
                    """SELECT session_id, workspace_id, inline_content, blob_id,
                                      content_digest, content_size,
                                      content_media_type, content_codec
                         FROM pulsara_v3.transcript_entries
                        WHERE session_id = %s AND workspace_id = %s AND id = %s
                          AND entry_kind IN (
                              'USER_MESSAGE', 'USER_STEER', 'TOOL_RESULT'
                          )""",
                    (session_id, workspace_id, owner_id),
                ).fetchone()
                if row is None:
                    raise ConversationKernelConflict(
                        "canonical image reference owner disappeared"
                    )
                body, decoded, refs = _read_canonical_prompt_metadata(
                    connection,
                    row=row,
                    queue_item_id=None,
                    transcript_entry_id=owner_id,
                    context_snapshot_id=None,
                )
                descriptors = _image_descriptors(decoded)
            elif owner_kind_rank == 1:
                row = connection.execute(
                    """SELECT session_id, workspace_id, inline_content, blob_id,
                                      content_digest, content_size,
                                      content_media_type, content_codec
                         FROM pulsara_v3.context_snapshots
                        WHERE session_id = %s AND workspace_id = %s AND id = %s""",
                    (session_id, workspace_id, owner_id),
                ).fetchone()
                if row is None:
                    raise ConversationKernelConflict(
                        "canonical image reference owner disappeared"
                    )
                body = _read_body(
                    connection,
                    row=row,
                    workspace_id=workspace_id,
                    expected_media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
                    expected_codec=CONTEXT_SNAPSHOT_CODEC,
                )
                compaction_snapshot_canonical_expanded_bytes(body)
                descriptors = compaction_snapshot_image_descriptors(body)
                refs = _read_ref_metadata(
                    connection,
                    session_id=session_id,
                    expected_count=len(descriptors),
                    queue_item_id=None,
                    transcript_entry_id=None,
                    context_snapshot_id=owner_id,
                )
                if not _refs_match_descriptors(
                    refs, descriptors=descriptors, workspace_id=workspace_id
                ):
                    raise ConversationKernelConflict(
                        "canonical snapshot refs do not exact-join"
                    )
            else:  # pragma: no cover - closed SQL union
                raise ConversationKernelConflict(
                    "canonical image reference owner kind is invalid"
                )
            if ref_ordinal < 0 or ref_ordinal >= len(descriptors):
                raise ConversationKernelConflict(
                    "canonical image reference ordinal is invalid"
                )
            descriptor = descriptors[ref_ordinal]
            reference = refs[ref_ordinal]
            if (
                descriptor.digest != image_ref
                or str(reference["blob_id"]) != blob_id
            ):
                raise ConversationKernelConflict(
                    "canonical image reference does not exact-join its owner"
                )
            if (
                descriptor.encoded_bytes > maximum_encoded_bytes
                or len(body) + descriptor.encoded_bytes
                > MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES
            ):
                raise CanonicalImageReferenceResourceExceeded(
                    "canonical image reference exceeds its read allowance"
                )
            payload = PostgresCanonicalBlobStore.read_exact_in_connection(
                connection,
                blob_id=blob_id,
                expected_digest=descriptor.digest,
                expected_size=descriptor.encoded_bytes,
                expected_workspace_id=workspace_id,
                expected_media_type=descriptor.media_type,
                expected_codec=PROMPT_IMAGE_BLOB_CODEC,
            )
            return LLMImagePart(
                media_type=descriptor.media_type,
                immutable_bytes=payload,
                width=descriptor.width,
                height=descriptor.height,
            )


def materialize_canonical_prompt(
    connection: Connection,
    *,
    publisher: CanonicalContentPublisher,
    workspace_id: str,
    prompt: FrozenCanonicalPrompt,
) -> CanonicalPromptPublication:
    """Publish body and every image on one caller-owned transaction."""

    if not isinstance(prompt, FrozenCanonicalPrompt):
        raise TypeError("canonical prompt publication requires a frozen prompt")
    body = publisher.materialize_in_connection(
        connection,
        workspace_id=workspace_id,
        content=prompt.body,
        media_type=PROMPT_BODY_MEDIA_TYPE,
        codec=PROMPT_BODY_CODEC,
    )
    blob_ids = tuple(
        PostgresCanonicalBlobStore.publish_in_connection(
            connection,
            workspace_id=workspace_id,
            content=occurrence.image.immutable_bytes,
            media_type=occurrence.image.media_type,
            codec=PROMPT_IMAGE_BLOB_CODEC,
        ).blob_id
        for occurrence in prompt.image_occurrences
    )
    return CanonicalPromptPublication(body=body, image_blob_ids=blob_ids)


def materialize_compaction_snapshot(
    connection: Connection,
    *,
    publisher: CanonicalContentPublisher,
    workspace_id: str,
    carrier: CompactionSnapshotCarrier,
) -> CanonicalPromptPublication:
    """Publish one typed snapshot body and its image occurrences in transaction."""

    if not isinstance(carrier, CompactionSnapshotCarrier):
        raise TypeError("snapshot publication requires a typed carrier")
    body = publisher.materialize_in_connection(
        connection,
        workspace_id=workspace_id,
        content=carrier.body,
        media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
        codec=CONTEXT_SNAPSHOT_CODEC,
    )
    descriptors = compaction_snapshot_image_descriptors(carrier.body)
    images = compaction_snapshot_image_parts(carrier)
    if len(descriptors) != len(images):
        raise ValueError("snapshot image traversal differs from its body")
    blob_ids: list[str] = []
    for descriptor, image in zip(descriptors, images, strict=True):
        if (
            image.content_digest != descriptor.digest
            or len(image.immutable_bytes) != descriptor.encoded_bytes
            or image.media_type != descriptor.media_type
            or image.width != descriptor.width
            or image.height != descriptor.height
        ):
            raise ValueError("snapshot image content differs from its descriptor")
        blob_ids.append(
            PostgresCanonicalBlobStore.publish_in_connection(
                connection,
                workspace_id=workspace_id,
                content=image.immutable_bytes,
                media_type=image.media_type,
                codec=PROMPT_IMAGE_BLOB_CODEC,
            ).blob_id
        )
    return CanonicalPromptPublication(body=body, image_blob_ids=tuple(blob_ids))


def insert_canonical_prompt_refs(
    connection: Connection,
    *,
    session_id: str,
    workspace_id: str,
    image_blob_ids: tuple[str, ...],
    queue_item_id: str | None = None,
    transcript_entry_id: str | None = None,
    context_snapshot_id: str | None = None,
) -> None:
    if sum(
        value is not None
        for value in (queue_item_id, transcript_entry_id, context_snapshot_id)
    ) != 1:
        raise ValueError("canonical image refs require exactly one owner")
    for ordinal, blob_id in enumerate(image_blob_ids):
        connection.execute(
            """
            INSERT INTO pulsara_v3.canonical_image_refs (
                session_id, workspace_id, queue_item_id,
                transcript_entry_id, context_snapshot_id, ref_ordinal, blob_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                session_id,
                workspace_id,
                queue_item_id,
                transcript_entry_id,
                context_snapshot_id,
                ordinal,
                blob_id,
            ),
        )


def copy_canonical_prompt_refs(
    connection: Connection,
    *,
    session_id: str,
    workspace_id: str,
    target_session_id: str | None = None,
    source_queue_item_id: str | None = None,
    source_transcript_entry_id: str | None = None,
    source_context_snapshot_id: str | None = None,
    target_queue_item_id: str | None = None,
    target_transcript_entry_id: str | None = None,
    target_context_snapshot_id: str | None = None,
) -> tuple[str, ...]:
    source_column, source_id = _owner_column(
        queue_item_id=source_queue_item_id,
        transcript_entry_id=source_transcript_entry_id,
        context_snapshot_id=source_context_snapshot_id,
    )
    rows = connection.execute(
        f"""SELECT ref_ordinal, blob_id
            FROM pulsara_v3.canonical_image_refs
            WHERE session_id = %s AND {source_column} = %s
            ORDER BY ref_ordinal""",
        (session_id, source_id),
    ).fetchall()
    blob_ids = tuple(str(row["blob_id"]) for row in rows)
    if tuple(int(row["ref_ordinal"]) for row in rows) != tuple(range(len(rows))):
        raise ConversationKernelConflict("canonical image ref ordinals are not dense")
    insert_canonical_prompt_refs(
        connection,
        session_id=session_id if target_session_id is None else target_session_id,
        workspace_id=workspace_id,
        image_blob_ids=blob_ids,
        queue_item_id=target_queue_item_id,
        transcript_entry_id=target_transcript_entry_id,
        context_snapshot_id=target_context_snapshot_id,
    )
    return blob_ids


def canonical_prompt_owner_is_exact(
    connection: Connection,
    *,
    row: Mapping[str, object],
    expected: FrozenCanonicalPrompt,
    queue_item_id: str | None = None,
    transcript_entry_id: str | None = None,
    context_snapshot_id: str | None = None,
) -> bool:
    """Confirm body and ref metadata without reading image blob payloads."""

    try:
        body, _, _ = _read_canonical_prompt_metadata(
            connection,
            row=row,
            queue_item_id=queue_item_id,
            transcript_entry_id=transcript_entry_id,
            context_snapshot_id=context_snapshot_id,
        )
        return body == expected.body
    except (KeyError, TypeError, ValueError, ConversationKernelConflict):
        return False


def validate_canonical_prompt_owner_metadata(
    connection: Connection,
    *,
    row: Mapping[str, object],
    queue_item_id: str | None = None,
    transcript_entry_id: str | None = None,
) -> CanonicalPromptBody:
    """Validate one stored prompt body and refs without image payload I/O."""

    _, decoded, _ = _read_canonical_prompt_metadata(
        connection,
        row=row,
        queue_item_id=queue_item_id,
        transcript_entry_id=transcript_entry_id,
        context_snapshot_id=None,
    )
    return decoded


def resolve_canonical_prompt_image_reference(
    connection: Connection,
    *,
    row: Mapping[str, object],
    ref_ordinal: int,
    queue_item_id: str | None = None,
    transcript_entry_id: str | None = None,
) -> Mapping[str, object]:
    """Resolve one exact owner-local image occurrence without its payload."""

    if isinstance(ref_ordinal, bool) or not isinstance(ref_ordinal, int) or ref_ordinal < 0:
        raise ValueError("canonical prompt image ordinal is invalid")
    _, decoded, refs = _read_canonical_prompt_metadata(
        connection,
        row=row,
        queue_item_id=queue_item_id,
        transcript_entry_id=transcript_entry_id,
        context_snapshot_id=None,
    )
    descriptors = _image_descriptors(decoded)
    if ref_ordinal >= len(descriptors):
        raise KeyError(ref_ordinal)
    descriptor = descriptors[ref_ordinal]
    reference = refs[ref_ordinal]
    return {
        "inline_content": None,
        "blob_id": reference["blob_id"],
        "content_digest": descriptor.digest,
        "content_size": descriptor.encoded_bytes,
        "content_media_type": descriptor.media_type,
        "content_codec": PROMPT_IMAGE_BLOB_CODEC,
    }


def hydrate_canonical_prompt_owner(
    connection: Connection,
    *,
    row: Mapping[str, object],
    queue_item_id: str | None = None,
    transcript_entry_id: str | None = None,
    context_snapshot_id: str | None = None,
) -> FrozenCanonicalPrompt:
    """Read one body and exact-hydrate its ordered image occurrences."""

    workspace_id = str(row["workspace_id"])
    body, decoded, refs = _read_canonical_prompt_metadata(
        connection,
        row=row,
        queue_item_id=queue_item_id,
        transcript_entry_id=transcript_entry_id,
        context_snapshot_id=context_snapshot_id,
    )
    descriptors = _image_descriptors(decoded)
    payload_by_blob: dict[str, bytes] = {}
    payloads: list[bytes] = []
    for row_ref, descriptor in zip(refs, descriptors, strict=True):
        blob_id = str(row_ref["blob_id"])
        payload = payload_by_blob.get(blob_id)
        if payload is None:
            payload = PostgresCanonicalBlobStore.read_exact_in_connection(
                connection,
                blob_id=blob_id,
                expected_digest=descriptor.digest,
                expected_size=descriptor.encoded_bytes,
                expected_workspace_id=workspace_id,
                expected_media_type=descriptor.media_type,
                expected_codec=PROMPT_IMAGE_BLOB_CODEC,
            )
            payload_by_blob[blob_id] = payload
        payloads.append(payload)
    try:
        return hydrate_canonical_prompt_body(body=body, image_payloads=tuple(payloads))
    except ValueError as exc:
        raise ConversationKernelConflict(
            "canonical prompt image payload integrity mismatch"
        ) from exc


def canonical_snapshot_owner_is_exact(
    connection: Connection,
    *,
    row: Mapping[str, object],
    expected: CompactionSnapshotCarrier,
    context_snapshot_id: str,
) -> bool:
    """Confirm a snapshot body and ref metadata without image payload reads."""

    try:
        workspace_id = str(row["workspace_id"])
        body = _read_body(
            connection,
            row=row,
            workspace_id=workspace_id,
            expected_media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
            expected_codec=CONTEXT_SNAPSHOT_CODEC,
        )
        if body != expected.body:
            return False
        descriptors = compaction_snapshot_image_descriptors(body)
        refs = _read_ref_metadata(
            connection,
            session_id=str(row["session_id"]),
            expected_count=len(descriptors),
            queue_item_id=None,
            transcript_entry_id=None,
            context_snapshot_id=context_snapshot_id,
        )
        return _refs_match_descriptors(
            refs, descriptors=descriptors, workspace_id=workspace_id
        )
    except (KeyError, TypeError, ValueError, ConversationKernelConflict):
        return False


def hydrate_canonical_snapshot_owner(
    connection: Connection,
    *,
    row: Mapping[str, object],
    context_snapshot_id: str,
) -> CompactionSnapshotCarrier:
    """Read a snapshot body and exact-hydrate its ordered image occurrences."""

    workspace_id = str(row["workspace_id"])
    body = _read_body(
        connection,
        row=row,
        workspace_id=workspace_id,
        expected_media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
        expected_codec=CONTEXT_SNAPSHOT_CODEC,
    )
    compaction_snapshot_canonical_expanded_bytes(body)
    descriptors = compaction_snapshot_image_descriptors(body)
    refs = _read_ref_metadata(
        connection,
        session_id=str(row["session_id"]),
        expected_count=len(descriptors),
        queue_item_id=None,
        transcript_entry_id=None,
        context_snapshot_id=context_snapshot_id,
    )
    if not _refs_match_descriptors(
        refs, descriptors=descriptors, workspace_id=workspace_id
    ):
        raise ConversationKernelConflict("canonical snapshot refs do not exact-join")
    payload_by_blob: dict[str, bytes] = {}
    payloads: list[bytes] = []
    for row_ref, descriptor in zip(refs, descriptors, strict=True):
        blob_id = str(row_ref["blob_id"])
        payload = payload_by_blob.get(blob_id)
        if payload is None:
            payload = PostgresCanonicalBlobStore.read_exact_in_connection(
                connection,
                blob_id=blob_id,
                expected_digest=descriptor.digest,
                expected_size=descriptor.encoded_bytes,
                expected_workspace_id=workspace_id,
                expected_media_type=descriptor.media_type,
                expected_codec=PROMPT_IMAGE_BLOB_CODEC,
            )
            payload_by_blob[blob_id] = payload
        payloads.append(payload)
    try:
        return parse_compaction_snapshot_carrier(
            body,
            image_payloads=tuple(payloads),
        )
    except ValueError as exc:
        raise ConversationKernelConflict(
            "canonical snapshot image payload integrity mismatch"
        ) from exc


def _read_body(
    connection: Connection,
    *,
    row: Mapping[str, object],
    workspace_id: str,
    expected_media_type: str,
    expected_codec: str,
) -> bytes:
    if (
        str(row["content_media_type"]) != expected_media_type
        or str(row["content_codec"]) != expected_codec
    ):
        raise ConversationKernelConflict("canonical prompt body descriptor is invalid")
    expected_digest = str(row["content_digest"])
    expected_size = int(row["content_size"])
    if not 0 <= expected_size <= MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES:
        raise ConversationKernelConflict("canonical body size is out of bounds")
    inline = row["inline_content"]
    blob_id = row["blob_id"]
    if (inline is None) == (blob_id is None):
        raise ConversationKernelConflict("canonical prompt body storage union is invalid")
    if inline is not None:
        content = InlineContent.from_bytes(
            bytes(inline), media_type=expected_media_type, codec=expected_codec
        )
        if content.digest != expected_digest or content.size != expected_size:
            raise ConversationKernelConflict("canonical prompt inline body is corrupt")
        return content.canonical_bytes
    return PostgresCanonicalBlobStore.read_exact_in_connection(
        connection,
        blob_id=str(blob_id),
        expected_digest=expected_digest,
        expected_size=expected_size,
        expected_workspace_id=workspace_id,
        expected_media_type=expected_media_type,
        expected_codec=expected_codec,
    )


def _read_canonical_prompt_metadata(
    connection: Connection,
    *,
    row: Mapping[str, object],
    queue_item_id: str | None,
    transcript_entry_id: str | None,
    context_snapshot_id: str | None,
) -> tuple[bytes, CanonicalPromptBody, tuple[Mapping[str, object], ...]]:
    """Read one prompt body and exact refs before optional image hydration."""

    workspace_id = str(row["workspace_id"])
    body = _read_body(
        connection,
        row=row,
        workspace_id=workspace_id,
        expected_media_type=PROMPT_BODY_MEDIA_TYPE,
        expected_codec=PROMPT_BODY_CODEC,
    )
    decoded = decode_canonical_prompt_body(body)
    # This descriptor-derived quote is complete before any image payload read.
    canonical_prompt_body_resource_quote(decoded)
    descriptors = _image_descriptors(decoded)
    refs = _read_ref_metadata(
        connection,
        session_id=str(row["session_id"]),
        expected_count=len(descriptors),
        queue_item_id=queue_item_id,
        transcript_entry_id=transcript_entry_id,
        context_snapshot_id=context_snapshot_id,
    )
    if not _refs_match_descriptors(
        refs, descriptors=descriptors, workspace_id=workspace_id
    ):
        raise ConversationKernelConflict("canonical prompt image refs do not exact-join")
    return body, decoded, refs


def _owner_column(
    *,
    queue_item_id: str | None,
    transcript_entry_id: str | None,
    context_snapshot_id: str | None,
) -> tuple[str, str]:
    values = (
        ("queue_item_id", queue_item_id),
        ("transcript_entry_id", transcript_entry_id),
        ("context_snapshot_id", context_snapshot_id),
    )
    selected = tuple((column, value) for column, value in values if value is not None)
    if len(selected) != 1 or not selected[0][1]:
        raise ValueError("canonical prompt lookup requires exactly one owner")
    return selected[0][0], str(selected[0][1])


def _read_ref_metadata(
    connection: Connection,
    *,
    session_id: str,
    expected_count: int,
    queue_item_id: str | None,
    transcript_entry_id: str | None,
    context_snapshot_id: str | None,
) -> tuple[Mapping[str, object], ...]:
    column, owner_id = _owner_column(
        queue_item_id=queue_item_id,
        transcript_entry_id=transcript_entry_id,
        context_snapshot_id=context_snapshot_id,
    )
    return tuple(
        connection.execute(
            f"""SELECT r.ref_ordinal, r.blob_id, r.workspace_id,
                       b.logical_digest, b.logical_size, b.media_type, b.codec
                FROM pulsara_v3.canonical_image_refs AS r
                LEFT JOIN pulsara_v3.blobs AS b
                  ON b.id = r.blob_id AND b.workspace_id = r.workspace_id
                WHERE r.session_id = %s AND r.{column} = %s
                ORDER BY r.ref_ordinal
                LIMIT %s""",
            (session_id, owner_id, expected_count + 1),
        ).fetchall()
    )


def _image_descriptors(
    body: CanonicalPromptBody,
) -> tuple[CanonicalPromptImageDescriptor, ...]:
    return tuple(
        part for part in body.parts if isinstance(part, CanonicalPromptImageDescriptor)
    )


def _refs_match_descriptors(
    refs: tuple[Mapping[str, object], ...],
    *,
    descriptors: tuple[CanonicalPromptImageDescriptor, ...],
    workspace_id: str,
) -> bool:
    if len(refs) != len(descriptors):
        return False
    for ordinal, (row, descriptor) in enumerate(zip(refs, descriptors, strict=True)):
        if (
            row["ref_ordinal"] is None
            or row["logical_size"] is None
            or int(row["ref_ordinal"]) != ordinal
            or str(row["workspace_id"]) != workspace_id
            or str(row["blob_id"]) != _blob_id(workspace_id, descriptor.digest)
            or row["logical_digest"] is None
            or str(row["logical_digest"]) != descriptor.digest
            or int(row["logical_size"]) != descriptor.encoded_bytes
            or str(row["media_type"]) != descriptor.media_type
            or str(row["codec"]) != PROMPT_IMAGE_BLOB_CODEC
        ):
            return False
    return True


__all__ = [
    "CanonicalImageReferenceReadPort",
    "CanonicalImageReferenceResourceExceeded",
    "CanonicalImageReferenceUnavailable",
    "CanonicalPromptPublication",
    "PostgresCanonicalImageReferenceReadPort",
    "canonical_prompt_owner_is_exact",
    "copy_canonical_prompt_refs",
    "hydrate_canonical_prompt_owner",
    "hydrate_canonical_snapshot_owner",
    "insert_canonical_prompt_refs",
    "materialize_canonical_prompt",
    "materialize_compaction_snapshot",
    "resolve_canonical_prompt_image_reference",
    "canonical_snapshot_owner_is_exact",
    "validate_canonical_prompt_owner_metadata",
]
