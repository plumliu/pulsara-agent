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
        drafts = (ContentPartDraft("text", value.storage_text),)
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
        if len(parts) != 1 or parts[0].type != "text":
            raise ValueError("Cell output owner must contain its complete text payload")
        source = row
        if "source_cell_id" not in source:
            source = connection.execute(
                "SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND consumed_entry_id=%s AND input_origin='CELL_OUTPUT'",
                (str(row["session_id"]), transcript_entry_id),
            ).fetchone()
            if source is None:
                raise ValueError("Cell output entry lacks its accepted queue source")
        return CellOutputContent.from_storage(
            cell_id=str(source["source_cell_id"]),
            source_turn_id=str(source["source_turn_id"]),
            source_exec_attempt_id=str(source["source_tool_attempt_id"]),
            record_sequence=int(source["source_record_sequence"]),
            kind=str(source["cell_output_kind"]),
            text=parts[0].value,
        )
    return content_parts_to_prompt(connection, parts)


def input_owner_metadata(
    connection, *, row, queue_item_id=None, transcript_entry_id=None
):
    """Quote a pending owner before materializing any image payload."""
    if row["input_origin"] == "CELL_OUTPUT":
        value = read_input_owner(
            connection,
            row=row,
            queue_item_id=queue_item_id,
            transcript_entry_id=transcript_entry_id,
        )
        return input_body_storage(value), value.resource_quote.canonical_expanded_bytes
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
