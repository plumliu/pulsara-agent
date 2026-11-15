"""Accepted DATA keeps its codec through provider, protocol and fork readers."""

import asyncio
from datetime import datetime, timezone
import json
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.blob import CanonicalContentPublisher
from pulsara_agent.conversation_kernel.content_parts import read_content_parts
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.repository import AssistantDataBlock, AssistantTextBlock
from pulsara_agent.llm.input import LLMTextPart
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from tests.test_conversation_fork import (
    child_lease, fork, identity, new_session, repo as repo, rows, turn,
)


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("codec,unit", [
    ("utf-8", "中文\r\n".encode()),
    ("binary", b"ASCII bytes"),
    ("opaque", b"ASCII bytes"),
    ("binary", b"\x00\xff\r\n"),
])
def test_data_readers_and_fork_keep_bytes_media_and_codec(repo, external, codec, unit):
    guard = new_session(repo).guard
    _, cut, _ = turn(repo, guard, "question", finish=False)
    workspace = repo.read_session_workspace_id(guard, deadline_monotonic=monotonic()+30)
    raw = unit * (70000 // len(unit) + 1) if external else unit
    publisher = CanonicalContentPublisher(repo.connection_provider)
    data = publisher.materialize(
        session_id=guard.session_id, content=raw, media_type="application/test", codec=codec,
        deadline_monotonic=monotonic()+30,
    )
    entry, block = identity("entry"), identity("block")
    repo.commit_assistant_message(
        guard, cut=cut, entry_id=entry,
        blocks=(AssistantDataBlock(block, data), AssistantTextBlock(identity("block"), InlineContent.from_bytes(b"answer"))),
        complete_turn=True, occurred_at=datetime.now(timezone.utc), actor_id="model:test",
        deadline_monotonic=monotonic()+30,
    )
    before = rows(repo, "SELECT * FROM pulsara_v3.content_parts WHERE entry_id=%s ORDER BY ordinal", (entry,))
    assert before[0]["data_codec"] == (None if external else codec)
    assert before[0]["data_media_type"] == (None if external else "application/test")
    child = fork(repo, guard.session_id, entry)
    assert child.created, child.public_code
    assert rows(repo, "SELECT * FROM pulsara_v3.content_parts WHERE entry_id=%s ORDER BY ordinal", (entry,)) == before
    copied = rows(repo, "SELECT * FROM pulsara_v3.content_parts WHERE session_id=%s AND type='data'", (child.child_session_id,))[0]
    assert copied["id"] != block
    assert copied["blob_id"] == before[0]["blob_id"]
    assert repo.delete_session(session_id=guard.session_id, memory_domain_id="u_local", required_lifecycle=None,
                               closed_writer=guard, deadline_monotonic=monotonic()+30) == "DELETED"
    child_guard = child_lease(repo, child.child_session_id).guard
    _, child_cut, _ = turn(repo, child_guard, "continue", finish=False)
    with repo._writer_transaction(child_guard, deadline_monotonic=monotonic()+30) as connection:
        part = read_content_parts(connection, entry_id=copied["entry_id"])[0]
        assert (part.value, part.data_media_type, part.data_codec) == (raw, "application/test", codec)
    read = CanonicalProviderInputReader(repo.connection_provider).read_frozen_snapshot(child_cut, deadline_monotonic=monotonic()+30)
    quote = CanonicalProviderInputReader(repo.connection_provider).read_compaction_headroom_preflight(
        child_cut, deadline_monotonic=monotonic()+30)
    assert quote.selected_canonical_expanded_bytes >= read.canonical_expanded_bytes
    assistant = next(item for item in read.items if item.source_entry_id == copied["entry_id"])
    projected = assistant.content[0]
    assert isinstance(projected, LLMTextPart)
    if codec == "utf-8":
        assert projected.text == raw.decode() + "answer"
    else:
        assert projected.text.endswith("answer")
        assert json.loads(projected.text[:-len("answer")]) == {"kind": "binary_content", "codec": codec, "size": len(raw)}
    protocol = CanonicalProtocolReader(repo.connection_provider)
    snapshot = protocol.snapshot(session_id=child.child_session_id, maximum_entries=20, maximum_control_items=20,
                                 deadline_monotonic=monotonic()+30)
    displayed = next(item for item in snapshot.entries if item.entry_id == copied["entry_id"])
    data_block = next(item for item in displayed.blocks if item.block_id == copied["id"])
    assert data_block.content.codec == codec
    assert data_block.content.media_type == "application/test"
    chunks = bytearray()
    while len(chunks) < len(raw):
        frame = asyncio.run(TerminalKernelProtocolServer.read_canonical_content(
            connection_provider=repo.connection_provider, protocol_reader=protocol, session_id=child.child_session_id,
            request=wire.ReadContentRequest(request_id=identity("request"), entry_id=copied["entry_id"],
                block_id=copied["id"], offset_bytes=len(chunks), limit_bytes=16384),
        ))
        assert frame.HasField("content"), frame
        assert frame.content.codec == codec
        chunks.extend(frame.content.content)
    assert bytes(chunks) == raw
    assert workspace == copied["workspace_id"]
