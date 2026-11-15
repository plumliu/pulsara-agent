"""The ordered content rows shared by entries and queued inputs.

The caller owns the transaction and locks its business owner before allocating
ordinals. Blob publication and verification stay with the existing blob/image
owners; this module only adapts typed content to their byte storage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Literal

from psycopg.types.json import Jsonb

from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
    PostgresCanonicalBlobStore,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
from pulsara_agent.identity import random_id, require_id
from pulsara_agent.ports.tool_execution import (
    encode_tool_public_json,
    freeze_tool_json_object,
)


PublicPartType = Literal["text", "image", "target", "object"]
ContentPartType = Literal["text", "image", "target", "object", "annotation", "tool_call", "data"]
_PUBLIC_TYPES = frozenset({"text", "image", "target", "object"})
_CONTENT_TYPES = _PUBLIC_TYPES | {"annotation", "tool_call", "data"}


def _object(value: object, *, target: bool = False) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("content value must be a JSON object")
    frozen = freeze_tool_json_object(value)
    if target and (not isinstance(frozen.get("kind"), str) or not frozen["kind"]):
        raise ValueError("target content requires a Target kind")
    # Target-specific business validation belongs to the producer's existing
    # Target contract. Storing a Target neither resolves it nor grants access.
    return frozen


def _image_ref(value: object) -> str:
    if not isinstance(value, Mapping) or set(value) != {"kind", "image_ref"} or value["kind"] != "IMAGE_REF":
        raise ValueError("image content requires an IMAGE_REF Target")
    return require_id(value["image_ref"], "image")


@dataclass(frozen=True, slots=True)
class ToolCallValue:
    tool_call_id: str
    tool_name: str
    tool_arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        # Provider tool-call IDs are external identities, not Pulsara IDs.
        if not isinstance(self.tool_call_id, str) or not self.tool_call_id:
            raise ValueError("tool_call_id must be non-empty")
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("tool_name must be non-empty")
        object.__setattr__(self, "tool_arguments", _object(self.tool_arguments))


def _validate_value(kind: str, value: object, data_media_type: str | None = None, data_codec: str | None = None) -> object:
    if kind not in _CONTENT_TYPES:
        raise ValueError("unsupported content part type")
    if kind != "data" and (data_media_type is not None or data_codec is not None):
        raise ValueError("only data content carries a media type or codec")
    if kind == "text":
        if not isinstance(value, str):
            raise TypeError("text content value must be a string")
        value.encode("utf-8", errors="surrogatepass")
    elif kind == "image":
        _image_ref(value)
        value = _object(value)
    elif kind in {"object", "annotation", "target"}:
        value = _object(value, target=kind == "target")
    elif kind == "tool_call":
        if not isinstance(value, ToolCallValue):
            raise TypeError("tool_call content requires ToolCallValue")
    elif kind == "data":
        if not isinstance(value, bytes):
            raise TypeError("data content value must be bytes")
        if not isinstance(data_media_type, str) or not data_media_type:
            raise ValueError("data content requires its actual media type")
        if not isinstance(data_codec, str) or not data_codec:
            raise ValueError("data content requires its actual codec")
    return value


@dataclass(frozen=True, slots=True)
class ResultPart:
    type: PublicPartType
    value: object

    def __post_init__(self) -> None:
        if self.type not in _PUBLIC_TYPES:
            raise ValueError("result content requires a public content type")
        object.__setattr__(self, "value", _validate_value(self.type, self.value))


@dataclass(frozen=True, slots=True)
class ContentPartDraft:
    type: ContentPartType
    value: object
    id: str | None = None
    data_media_type: str | None = None
    data_codec: str | None = None

    def __post_init__(self) -> None:
        if self.id is not None:
            require_id(self.id, "block")
        object.__setattr__(self, "value", _validate_value(self.type, self.value, self.data_media_type, self.data_codec))


@dataclass(frozen=True, slots=True)
class ContentPart:
    id: str
    ordinal: int
    type: ContentPartType
    value: object
    data_media_type: str | None = None
    data_codec: str | None = None

    def __post_init__(self) -> None:
        require_id(self.id, "block")
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("content ordinal must be a non-negative integer")
        object.__setattr__(self, "value", _validate_value(self.type, self.value, self.data_media_type, self.data_codec))


def _owner(entry_id: str | None, queue_item_id: str | None) -> tuple[str, str]:
    if (entry_id is None) == (queue_item_id is None):
        raise ValueError("content requires exactly one entry or queue owner")
    if entry_id is not None:
        return "entry_id", require_id(entry_id, "entry")
    return "queue_item_id", require_id(queue_item_id, "queue")


def next_content_ordinal(connection, *, entry_id: str | None = None,
                         queue_item_id: str | None = None) -> int:
    """Allocate after the last occurrence under the caller's owner lock."""
    column, owner_id = _owner(entry_id, queue_item_id)
    row = connection.execute(
        f"SELECT COALESCE(MAX(ordinal) + 1, 0) AS next_ordinal FROM pulsara_v3.content_parts WHERE {column} = %s",
        (owner_id,),
    ).fetchone()
    return int(row["next_ordinal"])


def write_content_parts(connection, *, session_id: str, workspace_id: str,
                        parts: Sequence[ContentPartDraft | ResultPart],
                        publisher: CanonicalContentPublisher | None = None,
                        entry_id: str | None = None, queue_item_id: str | None = None,
                        start_ordinal: int | None = None) -> tuple[ContentPart, ...]:
    """Publish all occurrences inside the existing acceptance transaction.

    Exceptions propagate to that transaction; a partially accepted result must
    never be committed as complete by the caller.
    """
    _owner(entry_id, queue_item_id)
    require_id(session_id, "session")
    require_id(workspace_id, "workspace")
    ordinal = (next_content_ordinal(connection, entry_id=entry_id, queue_item_id=queue_item_id)
               if start_ordinal is None else start_ordinal)
    if type(ordinal) is not int or ordinal < 0:
        raise ValueError("content ordinal must be a non-negative integer")
    result = []
    for draft in parts:
        if not isinstance(draft, (ContentPartDraft, ResultPart)):
            raise TypeError("content publication requires typed parts")
        part_id = draft.id if isinstance(draft, ContentPartDraft) and draft.id is not None else random_id("block")
        media_type = draft.data_media_type if isinstance(draft, ContentPartDraft) else None
        data_codec = draft.data_codec if isinstance(draft, ContentPartDraft) else None
        inline_value = blob_id = tool_call_id = tool_name = tool_arguments = None
        stored_media_type = stored_codec = None
        if draft.type == "tool_call":
            if entry_id is None:
                raise ValueError("tool calls require an assistant entry owner")
            tool_call_id, tool_name, tool_arguments = (
                draft.value.tool_call_id, draft.value.tool_name, Jsonb(dict(draft.value.tool_arguments)))
        elif draft.type == "image":
            from pulsara_agent.conversation_kernel.prompt_storage import read_image_reference_in_connection

            blob_id = _image_ref(draft.value)
            read_image_reference_in_connection(connection, image_ref=blob_id)
        else:
            if draft.type == "text":
                content = draft.value.encode("utf-8", errors="surrogatepass")
                content_media_type, codec = "text/plain", "utf-8"
            elif draft.type == "data":
                content, content_media_type, codec = draft.value, media_type, data_codec
            else:
                content, content_media_type, codec = encode_tool_public_json(draft.value), "application/json", "utf-8"
            if publisher is None:
                from pulsara_agent.conversation_kernel.blob import materialize_content_in_connection

                materialized = materialize_content_in_connection(
                    connection, workspace_id=workspace_id, content=content,
                    media_type=content_media_type, codec=codec)
            else:
                materialized = publisher.materialize_in_connection(
                    connection, workspace_id=workspace_id, content=content,
                    media_type=content_media_type, codec=codec)
            if isinstance(materialized, InlineContent):
                inline_value = materialized.canonical_bytes
                stored_media_type = media_type if draft.type == "data" else None
                stored_codec = data_codec if draft.type == "data" else None
            else:
                blob_id = materialized.blob_id
        connection.execute(
            """INSERT INTO pulsara_v3.content_parts
               (id, session_id, workspace_id, entry_id, queue_item_id, ordinal, type,
                inline_value, blob_id, tool_call_id, tool_name, tool_arguments, data_media_type, data_codec)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (part_id, session_id, workspace_id, entry_id, queue_item_id, ordinal, draft.type,
             inline_value, blob_id, tool_call_id, tool_name, tool_arguments, stored_media_type, stored_codec),
        )
        result.append(ContentPart(part_id, ordinal, draft.type, draft.value, media_type, data_codec))
        ordinal += 1
    return tuple(result)


def decode_content_part(connection, row: Mapping[str, object]) -> ContentPart:
    """Decode inline and external payloads through one typed boundary."""
    try:
        return _decode_content_part(connection, row)
    except (ValueError, TypeError) as error:
        raise ConversationKernelConflict("stored content part payload is invalid") from error


def _decode_content_part(connection, row: Mapping[str, object]) -> ContentPart:
    kind = row["type"]
    media_type = data_codec = None
    if kind == "tool_call":
        value = ToolCallValue(row["tool_call_id"], row["tool_name"], row["tool_arguments"])
    elif kind == "image":
        image_ref = require_id(row["blob_id"], "image")
        value = {"kind": "IMAGE_REF", "image_ref": image_ref}
    else:
        if (row["inline_value"] is None) == (row["blob_id"] is None):
            raise ConversationKernelConflict("content part payload is not exclusive")
        if row["inline_value"] is not None:
            content = bytes(row["inline_value"])
            media_type = row["data_media_type"] if kind == "data" else None
            data_codec = row["data_codec"] if kind == "data" else None
        else:
            blob = connection.execute(
                """SELECT logical_digest, logical_size, media_type, codec
                   FROM pulsara_v3.blobs WHERE id = %s""", (row["blob_id"],)
            ).fetchone()
            if blob is None:
                raise ConversationKernelConflict("content part blob is missing")
            expected_media_type = ("text/plain" if kind == "text" else "application/json") if kind != "data" else str(blob["media_type"])
            content = PostgresCanonicalBlobStore.read_exact_in_connection(
                connection, blob_id=str(row["blob_id"]),
                expected_digest=str(blob["logical_digest"]), expected_size=int(blob["logical_size"]),
                expected_workspace_id=str(row["workspace_id"]),
                expected_media_type=expected_media_type,
                expected_codec=str(blob["codec"]) if kind == "data" else "utf-8")
            media_type = str(blob["media_type"]) if kind == "data" else None
            data_codec = str(blob["codec"]) if kind == "data" else None
        value = (
            content
            if kind == "data"
            else content.decode("utf-8", errors="surrogatepass" if kind == "text" else "strict")
        )
        if kind in {"target", "object", "annotation"}:
            value = json.loads(value)
    return ContentPart(str(row["id"]), row["ordinal"], kind, value, media_type, data_codec)


def read_content_parts(connection, *, entry_id: str | None = None,
                       queue_item_id: str | None = None, session_id: str | None = None,
                       ordinal_start: int | None = None,
                       ordinal_stop: int | None = None) -> tuple[ContentPart, ...]:
    """Read the complete ordered owner or an explicit queue material range."""
    column, owner_id = _owner(entry_id, queue_item_id)
    clauses, values = [f"{column} = %s"], [owner_id]
    if session_id is not None:
        clauses.append("session_id = %s")
        values.append(require_id(session_id, "session"))
    _ordinal_range(clauses, values, ordinal_start, ordinal_stop)
    rows = connection.execute(
        f"SELECT * FROM pulsara_v3.content_parts WHERE {' AND '.join(clauses)} ORDER BY ordinal",
        tuple(values),
    ).fetchall()
    return tuple(decode_content_part(connection, row) for row in rows)


def _ordinal_range(clauses, values, start, stop) -> None:
    for value in (start, stop):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("content range must use non-negative ordinals")
    if start is not None and stop is not None and stop < start:
        raise ValueError("content range end precedes start")
    if start is not None:
        clauses.append("ordinal >= %s")
        values.append(start)
    if stop is not None:
        clauses.append("ordinal < %s")
        values.append(stop)


def move_queue_content_parts(connection, *, queue_item_id: str,
                             entry_id: str | None = None,
                             replacement_queue_item_id: str | None = None,
                             ordinal_start: int | None = None,
                             ordinal_stop: int | None = None) -> int:
    """Transfer queued occurrences without rewriting their identity or payload."""
    require_id(queue_item_id, "queue")
    _owner(entry_id, replacement_queue_item_id)
    if replacement_queue_item_id == queue_item_id:
        raise ValueError("replacement queue must differ from its source")
    clauses, values = ["queue_item_id = %s"], [queue_item_id]
    _ordinal_range(clauses, values, ordinal_start, ordinal_stop)
    cursor = connection.execute(
        f"""UPDATE pulsara_v3.content_parts SET entry_id = %s, queue_item_id = %s
            WHERE {' AND '.join(clauses)}""",
        (entry_id, replacement_queue_item_id, *values),
    )
    return cursor.rowcount


def public_result_parts(parts: Sequence[ContentPart]) -> tuple[ResultPart, ...]:
    return tuple(ResultPart(part.type, part.value) for part in parts if part.type in _PUBLIC_TYPES)


__all__ = [
    "ContentPart", "ContentPartDraft", "ContentPartType", "PublicPartType", "ResultPart",
    "ToolCallValue", "decode_content_part", "move_queue_content_parts", "next_content_ordinal",
    "public_result_parts", "read_content_parts", "write_content_parts",
]
