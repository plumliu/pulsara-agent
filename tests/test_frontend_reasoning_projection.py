from __future__ import annotations

from hashlib import sha256

import pytest

from pulsara_agent.llm.provider_replay import (
    build_prepared_durable_provider_assistant_replay,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact, canonical_json_bytes, freeze_json,
)
from pulsara_agent.terminal_protocol.canonical_v3 import (
    CanonicalProtocolReader, MAXIMUM_TOOL_ARGUMENT_PREVIEW_BYTES,
)
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from tests.support.model_config import build_test_provider_replay_target


class _Result:
    def __init__(self, *, one: object | None = None, many: tuple[object, ...] = ()):
        self._one = one
        self._many = many

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class _Connection:
    def __init__(self, entry: dict[str, object], replay: dict[str, object]):
        self.entry = entry
        self.replay = replay

    def execute(self, sql: str, _parameters: object) -> _Result:
        if "SELECT root_session_id FROM pulsara_v3.sessions" in sql:
            return _Result(one={"root_session_id": None})
        if "AS root_final" in sql:
            return _Result(one={"root_final": False})
        if "executed_status" in sql:
            return _Result(one={**self.entry, "root_session_id": None, "executed_status": "RUNNING", "executed_final": None})
        if "FROM pulsara_v3.assistant_message_blocks" in sql:
            return _Result(many=())
        if "FROM pulsara_v3.assistant_visualizations" in sql:
            return _Result(many=())
        if "FROM pulsara_v3.provider_assistant_replay_fragments" in sql:
            return _Result(one=self.replay)
        if "FROM pulsara_v3.transcript_entries" in sql:
            return _Result(one=self.entry)
        raise AssertionError(sql)


def _fixture(reasoning: str) -> tuple[dict[str, object], dict[str, object]]:
    frozen = freeze_json(
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_content": reasoning,
        }
    )
    assert isinstance(frozen, FrozenJsonObjectFact)
    target = build_test_provider_replay_target(
        model_id="reasoning-model",
        transport_binding_id="test-chat",
    )
    replay = build_prepared_durable_provider_assistant_replay(
        session_id='session:336wrfwkamrr3up6kjtsgvgcte',
        workspace_id='workspace:wdl6b3mtrkghwnvjo4an3ekbau',
        assistant_entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
        target=target,
        public_projection_fingerprint="sha256:" + "2" * 64,
        ordered_items=(frozen,),
    )
    answer = b"answer"
    entry = {
        "id": 'entry:5qidalqvkujk5ykzzzfpwie2qy',
        "entry_owner_kind": "EXECUTED_TURN",
        "session_id": 'session:336wrfwkamrr3up6kjtsgvgcte',
        "turn_id": 'turn:klmmazz67vfgsfsvg6ceqgdsfu',
        "entry_sequence": 1,
        "entry_kind": "ASSISTANT_MESSAGE",
        "scheduled_input": None,
        "input_origin": None,
        "conversation_scope_kind": "ROOT",
        "scope_subagent_task_id": None,
        "source_subagent_task_id": None,
        "context_binding_revision_id": None,
        "provider_input_through_sequence": 0,
        "inline_content": answer,
        "blob_id": None,
        "content_digest": "sha256:" + sha256(answer).hexdigest(),
        "content_size": len(answer),
        "content_media_type": "text/plain",
        "content_codec": "utf-8",
        "accepted_at": "2026-08-30T00:00:00Z",
    }
    replay_row = {
        "assistant_entry_kind": "ASSISTANT_MESSAGE",
        "codec_kind": replay.codec_kind.value,
        "payload_bytes": replay.payload_bytes,
        "payload_digest": replay.payload_digest,
        "payload_size": replay.payload_size,
        "item_count": replay.item_count,
    }
    return entry, replay_row


def test_canonical_read_projects_provider_reasoning_without_new_durable_blocks() -> (
    None
):
    entry, replay = _fixture("provider-visible reasoning")
    connection = _Connection(entry, replay)
    reader = CanonicalProtocolReader(None)  # type: ignore[arg-type]

    projected = reader._entry(connection, entry)

    assert len(projected.reasoning_blocks) == 1
    block = projected.reasoning_blocks[0]
    from pulsara_agent.desktop_app.renderer_bridge import protobuf_json
    assert block.HasField("ordinal") and block.ordinal == 0
    assert protobuf_json(projected)["reasoning_blocks"][0]["ordinal"] == 0
    assert block.presentation_kind == wire.REASONING_PRESENTATION_FULL
    assert block.content.kind == wire.INLINE
    assert block.content.inline_content == b"provider-visible reasoning"
    assert not projected.blocks


def test_large_provider_reasoning_uses_the_existing_chunked_content_read() -> None:
    reasoning = "思" * 30_000
    entry, replay = _fixture(reasoning)
    connection = _Connection(entry, replay)
    reader = CanonicalProtocolReader(None)  # type: ignore[arg-type]

    projected = reader._entry(connection, entry)
    block = projected.reasoning_blocks[0]
    resolved = reader._resolve_reasoning_content(
        connection,
        session_id='session:336wrfwkamrr3up6kjtsgvgcte',
        entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
        block_id=block.block_id,
        ordinal=block.ordinal,
    )

    assert block.content.kind == wire.CANONICAL_BLOB
    assert not block.content.inline_content
    assert resolved is not None
    assert resolved["inline_content"] == reasoning.encode("utf-8")
    assert resolved["blob_id"] is None


@pytest.mark.parametrize(
    ("character", "split_bytes"),
    (("é", 1), ("中", 1), ("中", 2), ("🙂", 1), ("🙂", 2), ("🙂", 3)),
)
def test_long_tool_argument_preview_keeps_a_valid_utf8_boundary(
    character: str, split_bytes: int,
) -> None:
    prefix = b'{"content":"'
    padding = "x" * (MAXIMUM_TOOL_ARGUMENT_PREVIEW_BYTES - len(prefix) - split_bytes)
    arguments = {"content": padding + character + "tail"}
    encoded = canonical_json_bytes(arguments)
    entry, replay = _fixture("reasoning")

    class WithTool(_Connection):
        def execute(self, sql: str, parameters: object) -> _Result:
            if "FROM pulsara_v3.assistant_message_blocks" in sql:
                return _Result(many=({
                    "id": 'block:wsp762qk2gdn6mufgvr73pkg3a', "block_ordinal": 0,
                    "block_kind": "TOOL_CALL", "tool_call_id": "call:write",
                    "tool_name": "write_file", "tool_arguments": arguments,
                },))
            return super().execute(sql, parameters)

    reader = CanonicalProtocolReader(None)  # type: ignore[arg-type]
    projected = reader._entry(WithTool(entry, replay), entry)
    block = projected.blocks[0]
    assert block.tool_arguments_preview.decode("utf-8") == prefix.decode() + padding
    assert block.tool_arguments_truncated
    assert block.tool_arguments_size == len(encoded)
    assert block.tool_arguments_digest == "sha256:" + sha256(encoded).hexdigest()


def test_reasoning_selector_recomputes_before_replay_and_rejects_collisions(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from pulsara_agent.identity import derive_id
    from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
    import pulsara_agent.terminal_protocol.canonical_v3 as canonical

    entry, replay = _fixture("reasoning")
    connection = _Connection(entry, replay)
    reader = CanonicalProtocolReader(None)

    @contextmanager
    def connected(_deadline):
        yield connection

    monkeypatch.setattr(reader, "_connection", connected)
    monkeypatch.setattr(reader, "_session", lambda *_: {"workspace_id": "workspace"})
    wanted = derive_id("reasoning-block", "provider-reasoning", str(entry["id"]), "0")
    other = derive_id("reasoning-block", "provider-reasoning", str(entry["id"]), "1")
    replay["payload_bytes"] = b"corrupt"
    def denied(*_):
        raise KeyError("unauthorized session")
    monkeypatch.setattr(reader, "_session", denied)
    with pytest.raises(KeyError, match="unauthorized session"):
        reader.resolve_content_reference(session_id=str(entry["session_id"]), entry_id=str(entry["id"]),
            block_id=wanted, reasoning_ordinal=0, deadline_monotonic=1)
    monkeypatch.setattr(reader, "_session", lambda *_: {"workspace_id": "workspace"})
    with pytest.raises(KeyError):
        reader.resolve_content_reference(session_id=str(entry["session_id"]), entry_id=str(entry["id"]),
            block_id=other, reasoning_ordinal=0, deadline_monotonic=1)
    with pytest.raises(RuntimeError, match="replay body is corrupt"):
        reader.resolve_content_reference(session_id=str(entry["session_id"]), entry_id=str(entry["id"]),
            block_id=wanted, reasoning_ordinal=0, deadline_monotonic=1)

    monkeypatch.setattr(canonical, "derive_id", lambda *args: wanted)
    monkeypatch.setattr(reader, "_reasoning_blocks", lambda *_: (
        SimpleNamespace(text="first", presentation_kind=canonical.ReasoningPresentationKind.FULL),
        SimpleNamespace(text="second", presentation_kind=canonical.ReasoningPresentationKind.FULL),
    ))
    with pytest.raises(ConversationKernelConflict, match="identity collision"):
        reader.resolve_content_reference(session_id=str(entry["session_id"]), entry_id=str(entry["id"]),
            block_id=wanted, reasoning_ordinal=0, deadline_monotonic=1)
    with pytest.raises(ConversationKernelConflict, match="identity collision"):
        reader._entry(connection, entry)


def test_gateway_reasoning_selector_requires_presence_and_matching_kind():
    import asyncio
    from pulsara_agent.identity import random_id
    from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer

    class Reader:
        calls = []
        def resolve_content_reference(self, **kwargs):
            self.calls.append(kwargs)
            raise KeyError('missing')

    reader = Reader()
    entry, block, reasoning = random_id('entry'), random_id('block'), random_id('reasoning-block')
    async def read(**kwargs):
        return await TerminalKernelProtocolServer.read_canonical_content(
            connection_provider=None, protocol_reader=reader, session_id=random_id('session'),
            request=wire.ReadContentRequest(request_id='req', entry_id=entry, limit_bytes=10, **kwargs))
    for selector in ({'block_id': block, 'reasoning_ordinal': 0}, {'block_id': reasoning},
                     {'reasoning_ordinal': 0}, {'block_id': reasoning, 'reasoning_ordinal': 0, 'image_ref_ordinal': 0}):
        reply = asyncio.run(read(**selector))
        assert reply.error.stable_code == 'CONTENT_TARGET_INVALID'
    assert not reader.calls
    reply = asyncio.run(read(block_id=reasoning, reasoning_ordinal=0))
    assert reply.error.stable_code == 'CONTENT_REFERENCE_MISSING'
    assert reader.calls[-1]['reasoning_ordinal'] == 0


def test_cold_http_content_parser_preserves_reasoning_ordinal_presence():
    import asyncio
    from types import SimpleNamespace
    from pulsara_agent.desktop_app.http_server import LocalHttpServer
    from pulsara_agent.identity import random_id

    seen = []
    async def history_content(session_id, request, *, tool_output):
        assert tool_output is False
        seen.append(request)
        return wire.CanonicalContentChunk(request_id='req')
    target = {'entry_id': random_id('entry'), 'block_id': random_id('reasoning-block'), 'reasoning_ordinal': 0}
    asyncio.run(LocalHttpServer._cold_content(SimpleNamespace(sessions=SimpleNamespace(history_content=history_content)),
                                             random_id('session'), target))
    assert seen[0].HasField('reasoning_ordinal') and seen[0].reasoning_ordinal == 0
