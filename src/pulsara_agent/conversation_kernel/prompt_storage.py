"""Prompt content joins and direct reads of immutable image assets."""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence

from psycopg import Connection, IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.identity import require_id
from pulsara_agent.primitives.context import canonical_json_bytes
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
    PROMPT_IMAGE_BLOB_CODEC,
    canonical_prompt_body_value,
    freeze_canonical_prompt,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CONTEXT_SNAPSHOT_CODEC,
    CONTEXT_SNAPSHOT_MEDIA_TYPE,
    CompactionSnapshotCarrier,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    parse_compaction_snapshot_carrier,
)
from pulsara_agent.conversation_kernel.repository_errors import (
    ConversationKernelConflict,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMTextPart,
    PromptAnnotationPart,
    annotation_from_value,
    annotation_value,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


class CanonicalImageReferenceUnavailable(LookupError):
    pass


class CanonicalImageReferenceResourceExceeded(ValueError):
    pass


class CanonicalImageReferenceReadPort(Protocol):
    def read_image(
        self,
        *,
        image_ref: str,
        maximum_encoded_bytes: int | None = None,
        deadline_monotonic: float,
    ) -> LLMImagePart: ...


class PostgresCanonicalImageReferenceReadPort:
    """Read known image identities without caller or source qualification."""

    def __init__(
        self, connection_provider: VerifiedPostgresConnectionProviderProtocol
    ) -> None:
        self._provider = connection_provider

    def read_image(
        self,
        *,
        image_ref: str,
        maximum_encoded_bytes: int | None = None,
        deadline_monotonic: float,
    ) -> LLMImagePart:
        with self._provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            return read_image_reference_in_connection(
                connection,
                image_ref=image_ref,
                maximum_encoded_bytes=maximum_encoded_bytes,
            )


def read_image_reference_metadata_in_connection(
    connection: Connection,
    *,
    image_ref: str,
) -> CanonicalPromptImageDescriptor:
    """Validate an immutable global asset descriptor without loading payload."""
    require_id(image_ref, "image")
    row = connection.execute(
        """SELECT workspace_id, logical_digest, logical_size, media_type, codec, width, height
           FROM pulsara_v3.blobs WHERE id = %s""",
        (image_ref,),
    ).fetchone()
    if row is None:
        raise CanonicalImageReferenceUnavailable(image_ref)
    if row["workspace_id"] is not None or row["codec"] != PROMPT_IMAGE_BLOB_CODEC:
        raise ConversationKernelConflict("image asset properties are invalid")
    digest, media_type = str(row["logical_digest"]), str(row["media_type"])
    if image_ref != _blob_id(
        None, digest, media_type, PROMPT_IMAGE_BLOB_CODEC, image=True
    ):
        raise ConversationKernelConflict(
            "image asset identity does not match its content"
        )
    try:
        return CanonicalPromptImageDescriptor(
            image_ref,
            digest,
            int(row["logical_size"]),
            media_type,
            row["width"],
            row["height"],
        )
    except (ValueError, TypeError) as exc:
        raise ConversationKernelConflict("image asset metadata is invalid") from exc


def read_image_reference_in_connection(
    connection: Connection,
    *,
    image_ref: str,
    maximum_encoded_bytes: int | None = None,
) -> LLMImagePart:
    """Read an existing asset; a known identity is sufficient read authority.

    The optional allowance is a caller's actual I/O boundary, never a stored
    asset or complete-result size limit. Source message and queue state have
    no role in this join.
    """
    require_id(image_ref, "image")
    if maximum_encoded_bytes is not None and (
        isinstance(maximum_encoded_bytes, bool)
        or not isinstance(maximum_encoded_bytes, int)
        or maximum_encoded_bytes < 1
    ):
        raise CanonicalImageReferenceResourceExceeded("image read allowance is invalid")
    descriptor = read_image_reference_metadata_in_connection(
        connection, image_ref=image_ref
    )
    if (
        maximum_encoded_bytes is not None
        and descriptor.encoded_bytes > maximum_encoded_bytes
    ):
        raise CanonicalImageReferenceResourceExceeded(
            "image exceeds its read allowance"
        )
    return read_image_reference_payload_in_connection(connection, descriptor=descriptor)


def read_image_reference_payload_in_connection(
    connection: Connection, *, descriptor: CanonicalPromptImageDescriptor
) -> LLMImagePart:
    """Read and verify payload using metadata confirmed by the asset owner."""
    if not isinstance(descriptor, CanonicalPromptImageDescriptor):
        raise TypeError("image payload read requires its validated descriptor")
    image_ref, size, digest, media_type = (
        descriptor.image_ref,
        descriptor.encoded_bytes,
        descriptor.digest,
        descriptor.media_type,
    )
    payload = PostgresCanonicalBlobStore.read_exact_in_connection(
        connection,
        blob_id=image_ref,
        expected_digest=digest,
        expected_size=size,
        expected_media_type=media_type,
        expected_codec=PROMPT_IMAGE_BLOB_CODEC,
    )
    # Pillow owns image decoding, including format and pixel validation. Reuse
    # the validation owner so corrupt bytes cannot become provider attachments.
    from pulsara_agent.conversation_kernel.image_validation import (
        _validated_image_facts,
    )

    try:
        actual_mime, width, height = _validated_image_facts(payload, media_type)
        if (width, height) != (descriptor.width, descriptor.height):
            raise ValueError("stored image dimensions differ from decoded bytes")
        return LLMImagePart(
            image_ref=image_ref,
            media_type=actual_mime,
            immutable_bytes=payload,
            width=width,
            height=height,
        )
    except Exception as exc:
        raise ConversationKernelConflict(
            "image asset decoding or dimensions are invalid"
        ) from exc


def publish_prompt_images(
    connection: Connection, *, prompt: FrozenCanonicalPrompt
) -> tuple[str, ...]:
    """Publish every image on the caller's content acceptance transaction."""
    if not isinstance(prompt, FrozenCanonicalPrompt):
        raise TypeError("prompt publication requires a frozen prompt")
    return tuple(
        PostgresCanonicalBlobStore.publish_in_connection(
            connection,
            workspace_id=None,
            content=item.image.immutable_bytes,
            media_type=item.image.media_type,
            codec=PROMPT_IMAGE_BLOB_CODEC,
            validated_image=item.image,
        ).blob_id
        for item in prompt.image_occurrences
    )


def prompt_to_content_drafts(prompt: FrozenCanonicalPrompt) -> tuple:
    """Translate one frozen prompt to ordered content, with no body carrier."""
    from pulsara_agent.conversation_kernel.content_parts import ContentPartDraft

    if not isinstance(prompt, FrozenCanonicalPrompt):
        raise TypeError("prompt content requires a frozen prompt")
    return tuple(
        ContentPartDraft("text", part.text)
        if isinstance(part, LLMTextPart)
        else ContentPartDraft("annotation", annotation_value(part))
        if isinstance(part, PromptAnnotationPart)
        else ContentPartDraft(
            "image", {"kind": "IMAGE_REF", "image_ref": part.image_ref}
        )
        for part in prompt.content.parts
    )


def content_parts_to_prompt(
    connection: Connection, parts: Sequence
) -> FrozenCanonicalPrompt:
    """Hydrate prompt parts from their sole relational content authority."""
    hydrated = []
    for part in parts:
        if part.type == "text":
            hydrated.append(LLMTextPart(part.value))
        elif part.type == "annotation":
            hydrated.append(annotation_from_value(part.value))
        elif part.type == "image":
            hydrated.append(
                read_image_reference_in_connection(
                    connection, image_ref=part.value["image_ref"]
                )
            )
        elif part.type in {"object", "target"}:
            from pulsara_agent.ports.tool_execution import encode_tool_public_json

            hydrated.append(
                LLMTextPart(encode_tool_public_json(part.value).decode("utf-8"))
            )
        else:
            raise ConversationKernelConflict(
                "content type cannot be projected as prompt input"
            )
    return freeze_canonical_prompt(FrozenPromptContent(tuple(hydrated)))


def _owner_parts(
    connection: Connection,
    *,
    row: Mapping[str, object],
    queue_item_id=None,
    transcript_entry_id=None,
    ordinal_start=None,
    ordinal_stop=None,
) -> tuple:
    from pulsara_agent.conversation_kernel.content_parts import read_content_parts

    if (queue_item_id is None) == (transcript_entry_id is None):
        raise ValueError("prompt lookup requires exactly one owner")
    return read_content_parts(
        connection,
        entry_id=transcript_entry_id,
        queue_item_id=queue_item_id,
        session_id=str(row["session_id"]),
        ordinal_start=ordinal_start,
        ordinal_stop=ordinal_stop,
    )


def hydrate_canonical_prompt_owner(
    connection: Connection,
    *,
    row: Mapping[str, object],
    queue_item_id=None,
    transcript_entry_id=None,
    ordinal_start=None,
    ordinal_stop=None,
) -> FrozenCanonicalPrompt:
    return content_parts_to_prompt(
        connection,
        _owner_parts(
            connection,
            row=row,
            queue_item_id=queue_item_id,
            transcript_entry_id=transcript_entry_id,
            ordinal_start=ordinal_start,
            ordinal_stop=ordinal_stop,
        ),
    )


def canonical_prompt_owner_is_exact(
    connection: Connection,
    *,
    row: Mapping[str, object],
    expected: FrozenCanonicalPrompt,
    queue_item_id=None,
    transcript_entry_id=None,
) -> bool:
    try:
        metadata = validate_canonical_prompt_owner_metadata(
            connection,
            row=row,
            queue_item_id=queue_item_id,
            transcript_entry_id=transcript_entry_id,
        )
        return (
            canonical_json_bytes(canonical_prompt_body_value(metadata)) == expected.body
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        ConversationKernelConflict,
        CanonicalImageReferenceUnavailable,
    ):
        return False


def validate_canonical_prompt_owner_metadata(
    connection: Connection,
    *,
    row: Mapping[str, object],
    queue_item_id=None,
    transcript_entry_id=None,
    ordinal_start=None,
    ordinal_stop=None,
) -> CanonicalPromptBody:
    """Quote ordered content and image properties without image payload I/O."""
    parts = []
    for part in _owner_parts(
        connection,
        row=row,
        queue_item_id=queue_item_id,
        transcript_entry_id=transcript_entry_id,
        ordinal_start=ordinal_start,
        ordinal_stop=ordinal_stop,
    ):
        if part.type == "image":
            parts.append(
                read_image_reference_metadata_in_connection(
                    connection, image_ref=part.value["image_ref"]
                )
            )
        elif part.type == "text":
            parts.append(LLMTextPart(part.value))
        elif part.type == "annotation":
            parts.append(annotation_from_value(part.value))
        elif part.type in {"object", "target"}:
            from pulsara_agent.ports.tool_execution import encode_tool_public_json

            parts.append(
                LLMTextPart(encode_tool_public_json(part.value).decode("utf-8"))
            )
        else:
            raise ConversationKernelConflict(
                "content type cannot be projected as prompt input"
            )
    return CanonicalPromptBody(tuple(parts))


def resolve_canonical_prompt_image_reference(
    connection: Connection,
    *,
    row: Mapping[str, object],
    ref_ordinal: int,
    queue_item_id=None,
    transcript_entry_id=None,
) -> Mapping[str, object]:
    if (
        isinstance(ref_ordinal, bool)
        or not isinstance(ref_ordinal, int)
        or ref_ordinal < 0
    ):
        raise ValueError("prompt image ordinal is invalid")
    parts = tuple(
        p
        for p in _owner_parts(
            connection,
            row=row,
            queue_item_id=queue_item_id,
            transcript_entry_id=transcript_entry_id,
        )
        if p.type == "image"
    )
    if ref_ordinal >= len(parts):
        raise KeyError(ref_ordinal)
    image = read_image_reference_in_connection(
        connection, image_ref=parts[ref_ordinal].value["image_ref"]
    )
    return {
        "inline_content": None,
        "blob_id": image.image_ref,
        "content_digest": image.content_digest,
        "content_size": len(image.immutable_bytes),
        "content_media_type": image.media_type,
        "content_codec": PROMPT_IMAGE_BLOB_CODEC,
        "image_ref": image.image_ref,
        "width": image.width,
        "height": image.height,
    }


def materialize_compaction_snapshot(
    connection: Connection,
    *,
    publisher: CanonicalContentPublisher,
    workspace_id: str,
    carrier: CompactionSnapshotCarrier,
) -> CanonicalContent:
    if not isinstance(carrier, CompactionSnapshotCarrier):
        raise TypeError("snapshot publication requires a typed carrier")
    return publisher.materialize_in_connection(
        connection,
        workspace_id=workspace_id,
        content=carrier.body,
        media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
        codec=CONTEXT_SNAPSHOT_CODEC,
    )


def canonical_snapshot_owner_is_exact(
    connection: Connection,
    *,
    row: Mapping[str, object],
    expected: CompactionSnapshotCarrier,
    context_snapshot_id: str,
) -> bool:
    try:
        return (
            hydrate_canonical_snapshot_owner(
                connection,
                row=row,
                context_snapshot_id=context_snapshot_id,
            )
            == expected
        )
    except (KeyError, TypeError, ValueError, ConversationKernelConflict):
        return False


def hydrate_canonical_snapshot_owner(
    connection: Connection, *, row: Mapping[str, object], context_snapshot_id: str
) -> CompactionSnapshotCarrier:
    require_id(context_snapshot_id, "snapshot")
    if str(row["id"]) != context_snapshot_id:
        raise ConversationKernelConflict("snapshot owner does not exact-join")
    body = _read_body(
        connection,
        row=row,
        workspace_id=str(row["workspace_id"]),
        expected_media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
        expected_codec=CONTEXT_SNAPSHOT_CODEC,
    )
    try:
        return parse_compaction_snapshot_carrier(body)
    except (TypeError, ValueError) as exc:
        raise ConversationKernelConflict("snapshot carrier is invalid") from exc


def _read_body(
    connection: Connection,
    *,
    row: Mapping[str, object],
    workspace_id: str,
    expected_media_type: str,
    expected_codec: str,
) -> bytes:
    """Read independently owned blob/inline bodies such as context snapshots."""
    if (
        row["content_media_type"] != expected_media_type
        or row["content_codec"] != expected_codec
    ):
        raise ConversationKernelConflict("body descriptor is invalid")
    expected_digest, expected_size = (
        str(row["content_digest"]),
        int(row["content_size"]),
    )
    if expected_size < 0:
        raise ConversationKernelConflict("body size is invalid")
    inline, blob_id = row["inline_content"], row["blob_id"]
    if (inline is None) == (blob_id is None):
        raise ConversationKernelConflict("body storage union is invalid")
    if inline is not None:
        content = InlineContent.from_bytes(
            bytes(inline), media_type=expected_media_type, codec=expected_codec
        )
        if content.digest != expected_digest or content.size != expected_size:
            raise ConversationKernelConflict("inline body is corrupt")
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
