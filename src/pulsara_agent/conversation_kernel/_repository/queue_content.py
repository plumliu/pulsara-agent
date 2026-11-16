"""Queue-owned content boundaries and process-local input carriers.

Only content_parts stores readable content. These helpers neither copy queue
content on consumption nor retain a consumed-entry mapping for materials.
"""

from __future__ import annotations

from hashlib import sha256

from pulsara_agent.conversation_kernel.content_parts import (
    ContentPartDraft,
    read_content_parts,
    write_content_parts,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.prompt_content import (
    PROMPT_BODY_MEDIA_TYPE,
    PROMPT_BODY_CODEC,
)
from pulsara_agent.conversation_kernel.prompt_storage import (
    content_parts_to_prompt,
    prompt_to_content_drafts,
    publish_prompt_images,
)
from pulsara_agent.ports.terminal_observation import (
    CellOutputContent,
    CELL_OUTPUT_MEDIA_TYPE,
    CELL_OUTPUT_CODEC,
)


def input_body_storage(value):
    """A disposable exact-value carrier, never a second durable body."""
    return InlineContent(
        canonical_bytes=value.body,
        digest="sha256:" + sha256(value.body).hexdigest(),
        size=len(value.body),
        media_type=CELL_OUTPUT_MEDIA_TYPE
        if isinstance(value, CellOutputContent)
        else PROMPT_BODY_MEDIA_TYPE,
        codec=CELL_OUTPUT_CODEC
        if isinstance(value, CellOutputContent)
        else PROMPT_BODY_CODEC,
    )


def publish_queue_input(
    connection,
    *,
    publisher,
    session_id,
    workspace_id,
    queue_item_id,
    value,
    start_ordinal=None,
):
    if isinstance(value, CellOutputContent):
        from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore
        from pulsara_agent.conversation_kernel.prompt_content import (
            PROMPT_IMAGE_BLOB_CODEC,
        )

        for image in value.images:
            PostgresCanonicalBlobStore.publish_in_connection(
                connection,
                workspace_id=None,
                content=image.immutable_bytes,
                media_type=image.media_type,
                codec=PROMPT_IMAGE_BLOB_CODEC,
                validated_image=image,
            )
        drafts = tuple(
            ContentPartDraft(part["type"], part["value"])
            for part in value.storage_parts
        )
    else:
        publish_prompt_images(connection, prompt=value)
        drafts = prompt_to_content_drafts(value)
    return write_content_parts(
        connection,
        session_id=session_id,
        workspace_id=workspace_id,
        queue_item_id=queue_item_id,
        parts=drafts,
        publisher=publisher,
        start_ordinal=start_ordinal,
    )


def read_input_owner(
    connection,
    *,
    row,
    queue_item_id=None,
    transcript_entry_id=None,
    ordinal_start=None,
    ordinal_stop=None,
):
    if queue_item_id is None and transcript_entry_id is None:
        if "queue_sequence" in row:
            queue_item_id = str(row["id"])
        else:
            transcript_entry_id = str(row["id"])
    if queue_item_id is not None and row.get("status") == "CONSUMED":
        transcript_entry_id, queue_item_id = str(row["consumed_entry_id"]), None
        ordinal_start = ordinal_stop = None
    if (
        queue_item_id is not None
        and row.get("input_origin") == "AGENT_INSTRUCTION"
        and ordinal_start is None
    ):
        instruction = row["work_inputs"][0]
        ordinal_start = instruction["content_start"]
        ordinal_stop = ordinal_start + instruction["content_count"]
    parts = read_content_parts(
        connection,
        session_id=str(row["session_id"]),
        queue_item_id=queue_item_id,
        entry_id=transcript_entry_id,
    )
    if ordinal_start is not None:
        parts = tuple(
            x
            for x in parts
            if x.ordinal >= ordinal_start
            and (ordinal_stop is None or x.ordinal < ordinal_stop)
        )
    if row["input_origin"] == "CELL_OUTPUT":
        source = row
        if "source_tool_attempt_id" not in source:
            source = connection.execute(
                "SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND consumed_entry_id=%s AND input_origin='CELL_OUTPUT'",
                (str(row["session_id"]), transcript_entry_id),
            ).fetchone()
            if source is None:
                raise ValueError("Cell output entry lacks its accepted queue source")
            source = {
                **source,
                **{
                    name: row[name]
                    for name in (
                        "source_cell_id",
                        "source_record_sequence",
                        "cell_output_kind",
                    )
                },
            }
        images = tuple(_read_cell_images(connection, parts).values())
        return CellOutputContent(
            cell_id=str(source["source_cell_id"]),
            source_turn_id=str(source["source_turn_id"]),
            source_exec_attempt_id=str(source["source_tool_attempt_id"]),
            record_sequence=int(source["source_record_sequence"]),
            record={
                "kind": source["cell_output_kind"],
                "content": [{"type": part.type, "value": part.value} for part in parts],
            },
            images=images,
        )
    return content_parts_to_prompt(connection, parts)


def _read_cell_images(connection, parts):
    from pulsara_agent.conversation_kernel.prompt_storage import (
        CanonicalImageReferenceUnavailable,
        read_image_reference_payload_in_connection,
        read_image_reference_metadata_in_connection,
    )
    from pulsara_agent.conversation_kernel.repository_errors import (
        ConversationKernelConflict,
    )
    from pulsara_agent.conversation_kernel.native_tool_delivery import _image_error
    from pulsara_agent.ports.terminal_observation import CellOutputImageReadFailure

    loaded = {}
    for part in parts:
        if part.type != "image":
            continue
        image_ref = part.value["image_ref"]
        if image_ref in loaded:
            continue
        # Owner/metadata failures are not a payload observation and must escape.
        descriptor = read_image_reference_metadata_in_connection(
            connection, image_ref=image_ref
        )
        try:
            image = read_image_reference_payload_in_connection(
                connection, descriptor=descriptor
            )
        except (
            TimeoutError,
            CanonicalImageReferenceUnavailable,
            ValueError,
            ConversationKernelConflict,
        ) as error:
            reason = _image_error(error)
            if str(error):
                reason += "\n" + str(error)
            image = CellOutputImageReadFailure(
                descriptor.image_ref,
                descriptor.digest,
                descriptor.encoded_bytes,
                descriptor.media_type,
                descriptor.width,
                descriptor.height,
                reason,
            )
        loaded[image_ref] = image
    return loaded


def read_cell_entry_provider_parts(connection, *, session_id, entry_id):
    """Hydrate one self-contained accepted Cell entry, including imported history."""
    from pulsara_agent.llm.input import LLMTextPart
    from pulsara_agent.ports.terminal_observation import cell_output_provider_text

    parts = read_content_parts(connection, session_id=session_id, entry_id=entry_id)
    loaded = _read_cell_images(connection, parts)
    result = []
    for part in parts:
        if part.type == "text":
            result.append(LLMTextPart(cell_output_provider_text(part.value)))
        elif part.type == "image":
            result.append(loaded[part.value["image_ref"]])
        else:
            raise ValueError("Cell output requires text and image parts")
    return tuple(result)


def input_owner_metadata(
    connection, *, row, queue_item_id=None, transcript_entry_id=None
):
    """Quote a pending owner before materializing any image payload."""
    if row["input_origin"] == "CELL_OUTPUT":
        from pulsara_agent.ports.terminal_observation import (
            cell_output_provider_text,
            cell_output_canonical_body,
        )
        from pulsara_agent.llm.input import LLMTextPart
        from pulsara_agent.conversation_kernel.prompt_content import (
            CanonicalPromptImageDescriptor,
        )

        parts = read_content_parts(
            connection,
            session_id=str(row["session_id"]),
            queue_item_id=queue_item_id,
            entry_id=transcript_entry_id,
        )
        metadata = []
        for part in parts:
            if part.type == "text":
                metadata.append(LLMTextPart(cell_output_provider_text(part.value)))
            elif part.type == "image":
                ref = part.value["image_ref"]
                image = connection.execute(
                    "SELECT logical_digest,logical_size,media_type,width,height FROM pulsara_v3.blobs WHERE id=%s",
                    (ref,),
                ).fetchone()
                if image is None:
                    raise ValueError("Cell output image asset is unavailable")
                metadata.append(
                    CanonicalPromptImageDescriptor(
                        ref,
                        image["logical_digest"],
                        int(image["logical_size"]),
                        image["media_type"],
                        int(image["width"]),
                        int(image["height"]),
                    )
                )
            else:
                raise ValueError("Cell output requires text and image parts")
        body, expanded = cell_output_canonical_body(metadata)
        content = InlineContent(
            canonical_bytes=body,
            digest="sha256:" + sha256(body).hexdigest(),
            size=len(body),
            media_type=CELL_OUTPUT_MEDIA_TYPE,
            codec=CELL_OUTPUT_CODEC,
        )
        return content, expanded
    from pulsara_agent.conversation_kernel.prompt_storage import (
        validate_canonical_prompt_owner_metadata,
    )
    from pulsara_agent.conversation_kernel.prompt_content import (
        canonical_prompt_body_value,
        canonical_prompt_body_resource_quote,
    )
    from pulsara_agent.primitives.context import canonical_json_bytes

    body = validate_canonical_prompt_owner_metadata(
        connection,
        row=row,
        queue_item_id=queue_item_id,
        transcript_entry_id=transcript_entry_id,
    )
    encoded = canonical_json_bytes(canonical_prompt_body_value(body))
    content = InlineContent(
        canonical_bytes=encoded,
        digest="sha256:" + sha256(encoded).hexdigest(),
        size=len(encoded),
        media_type=PROMPT_BODY_MEDIA_TYPE,
        codec=PROMPT_BODY_CODEC,
    )
    return content, canonical_prompt_body_resource_quote(body).canonical_expanded_bytes
