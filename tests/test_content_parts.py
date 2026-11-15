"""Observable boundaries of the shared ordered-content adapter."""

from hashlib import sha256
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.content_parts import (
    ContentPart,
    ContentPartDraft,
    ResultPart,
    ToolCallValue,
    decode_content_part,
    move_queue_content_parts,
    next_content_ordinal,
    public_result_parts,
    read_content_parts,
    write_content_parts,
)
from pulsara_agent.conversation_kernel.contracts import BlobContent, InlineContent
from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
from pulsara_agent.identity import random_id


class Connection:
    def __init__(self, *, rows=()):
        self.calls = []
        self.rows = iter(rows)

    def execute(self, statement, parameters):
        self.calls.append((statement, parameters))
        if statement.lstrip().startswith("SELECT"):
            row = next(self.rows)
            return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: row)
        return SimpleNamespace(rowcount=2)


class Publisher:
    def __init__(self, *, external=False):
        self.calls = []
        self.external = external

    def materialize_in_connection(self, connection, **kwargs):
        self.calls.append(kwargs)
        content = kwargs["content"]
        if self.external:
            return BlobContent(random_id("blob"), "sha256:" + sha256(content).hexdigest(),
                               len(content), kwargs["media_type"], kwargs["codec"])
        return InlineContent.from_bytes(content, media_type=kwargs["media_type"], codec=kwargs["codec"])


def row(kind, value, **updates):
    result = dict(id=random_id("block"), ordinal=7, type=kind,
                  workspace_id=random_id("workspace"),
                  inline_value=value, blob_id=None, data_media_type=None, data_codec=None,
                  tool_call_id=None, tool_name=None, tool_arguments=None)
    result.update(updates)
    return result


def test_public_json_freezes_the_accepted_value_without_losing_null_or_number_types():
    business = {"null": None, "values": [1, 1.25, True], "nested": {"文字": "原文"}}
    part = ResultPart("object", business)
    business["values"].append("later")
    assert part.value["values"] == (1, 1.25, True)
    assert part.value["null"] is None
    with pytest.raises(TypeError):
        part.value["nested"]["文字"] = "changed"


@pytest.mark.parametrize(("kind", "value"), [
    ("text", b"bytes"), ("object", [1, 2]), ("image", {"kind": "IMAGE_REF", "image_ref": "missing"}),
    ("target", {"path": "."}), ("annotation", {"quote": "internal"}),
    ("object", {"nan": float("nan")}), ("object", {1: "key"}),
])
def test_public_parts_reject_invalid_shapes(kind, value):
    with pytest.raises((TypeError, ValueError)):
        ResultPart(kind, value)


def test_ordered_publication_retains_occurrences_and_explicit_tool_call_identity():
    block_id, entry_id = random_id("block"), random_id("entry")
    call = ToolCallValue("provider-call", "read_file", {"target": {"kind": "PATH", "path": "."}})
    parts = [ContentPartDraft("text", "before\n"), ContentPartDraft("tool_call", call, id=block_id),
             ContentPartDraft("text", "after"), ContentPartDraft("data", b"\x00\xff", data_media_type="application/test", data_codec="binary")]
    connection, publisher = Connection(), Publisher()
    accepted = write_content_parts(connection, session_id=random_id("session"), workspace_id=random_id("workspace"),
                                   entry_id=entry_id, parts=parts, publisher=publisher, start_ordinal=3)
    assert [part.ordinal for part in accepted] == [3, 4, 5, 6]
    assert accepted[1].id == block_id
    assert [part.value for part in accepted] == ["before\n", call, "after", b"\x00\xff"]
    assert [part.value for part in public_result_parts(accepted)] == ["before\n", "after"]
    assert len(connection.calls) == 4
    stored_call = connection.calls[1][1]
    assert stored_call[7:11] == (None, None, "provider-call", "read_file")
    assert connection.calls[3][1][-2:] == ("application/test", "binary")


def test_publication_preserves_a_single_complete_large_item():
    text = "x" * ((16 << 20) + 1)
    connection, publisher = Connection(), Publisher(external=True)
    accepted = write_content_parts(connection, session_id=random_id("session"), workspace_id=random_id("workspace"),
                                   entry_id=random_id("entry"), parts=[ResultPart("text", text)],
                                   publisher=publisher, start_ordinal=0)
    assert len(accepted) == 1 and accepted[0].value == text
    assert publisher.calls[0]["content"] == text.encode()
    assert connection.calls[0][1][7] is None
    assert connection.calls[0][1][8].startswith("blob:")


def test_inline_and_blob_decode_agree_and_check_external_byte_integrity():
    content = b'{"null":null,"array":[1,2.25],"nested":{"value":"text"}}'
    inline = decode_content_part(Connection(), row("object", content))
    workspace_id = random_id("workspace")
    blob_id = random_id("blob")
    blob = dict(workspace_id=workspace_id, logical_digest="sha256:" + sha256(content).hexdigest(),
                logical_size=len(content), media_type="application/json", codec="utf-8", body=content)
    external_row = row("object", None, blob_id=blob_id, workspace_id=workspace_id)
    external = decode_content_part(Connection(rows=[blob, blob]), external_row)
    assert external.value == inline.value
    tampered = dict(blob, body=content.replace(b"text", b"fake"))
    with pytest.raises(ConversationKernelConflict, match="integrity"):
        decode_content_part(Connection(rows=[blob, tampered]), external_row)
    foreign = dict(blob, workspace_id=random_id("workspace"))
    with pytest.raises(ConversationKernelConflict, match="integrity"):
        decode_content_part(Connection(rows=[blob, foreign]), external_row)


def test_data_is_bytes_and_retains_its_actual_media_type():
    value = b"\x00\xffnot-text"
    part = decode_content_part(Connection(), row("data", value, data_media_type="application/octet-stream", data_codec="binary"))
    assert part.value == value
    assert part.data_media_type == "application/octet-stream"
    with pytest.raises(ValueError, match="media type"):
        ContentPart(random_id("block"), 0, "data", value)


def test_image_content_reads_the_reference_without_eagerly_loading_asset_bytes():
    image_ref = random_id("image")
    connection = Connection()
    part = decode_content_part(connection, row("image", None, blob_id=image_ref))
    assert part.value == {"kind": "IMAGE_REF", "image_ref": image_ref}
    assert connection.calls == []


@pytest.mark.parametrize(("kind", "payload"), [("text", b"\xff"), ("object", b"{invalid"), ("object", b"[]")])
def test_invalid_stored_payload_raises_a_typed_integrity_failure(kind, payload):
    with pytest.raises(ConversationKernelConflict, match="payload is invalid"):
        decode_content_part(Connection(), row(kind, payload))


def test_read_preserves_gaps_and_tool_order_with_explicit_material_range():
    rows = [row("text", b"first", ordinal=5), row("tool_call", None, ordinal=9,
             tool_call_id="external", tool_name="test", tool_arguments={}), row("text", b"last", ordinal=12)]
    connection = Connection(rows=[rows])
    parts = read_content_parts(connection, queue_item_id=random_id("queue"), ordinal_start=5, ordinal_stop=13)
    assert [part.ordinal for part in parts] == [5, 9, 12]
    statement, parameters = connection.calls[0]
    assert "ORDER BY ordinal" in statement
    assert "LIMIT" not in statement
    assert parameters[1:] == (5, 13)


def test_queue_transfer_changes_only_its_owner_in_half_open_range():
    source, target = random_id("queue"), random_id("entry")
    connection = Connection()
    assert move_queue_content_parts(connection, queue_item_id=source, entry_id=target,
                                    ordinal_start=2, ordinal_stop=4) == 2
    statement, parameters = connection.calls[0]
    assert statement.split("SET", 1)[1].split("WHERE", 1)[0].strip() == "entry_id = %s, queue_item_id = %s"
    assert parameters == (target, None, source, 2, 4)
    replacement = random_id("queue")
    move_queue_content_parts(connection, queue_item_id=source, replacement_queue_item_id=replacement)
    assert connection.calls[-1][1] == (None, replacement, source)


def test_owner_and_ordinal_admission_does_not_allow_ambiguous_or_reverse_moves():
    with pytest.raises(ValueError, match="exactly one"):
        read_content_parts(Connection(), entry_id=random_id("entry"), queue_item_id=random_id("queue"))
    with pytest.raises(ValueError, match="precedes"):
        move_queue_content_parts(Connection(), queue_item_id=random_id("queue"),
                                 entry_id=random_id("entry"), ordinal_start=4, ordinal_stop=2)
    with pytest.raises(ValueError, match="assistant entry"):
        write_content_parts(Connection(), session_id=random_id("session"), workspace_id=random_id("workspace"),
                            queue_item_id=random_id("queue"), start_ordinal=0, publisher=Publisher(),
                            parts=[ContentPartDraft("tool_call", ToolCallValue("provider-id", "test", {}))])
    assert next_content_ordinal(Connection(rows=[{"next_ordinal": 100001}]), entry_id=random_id("entry")) == 100001
