"""A Cell's accepted common text does not require its ingress source envelope."""

import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import LLMTextPart
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
)
from pulsara_agent.model_input.lowering import lower_canonical_item
from pulsara_agent.ports.terminal_observation import cell_output_provider_text


@pytest.mark.parametrize(("text", "expected"), (
    ("console output\nnext line\n", "console output\nnext line\n"),
    ('{"value":42}', '{"value":42}'),
    ("汉🙂\r\n", "汉🙂\r\n"),
    ("\ud800", "\\ud800"),
    ("汉\udfff🙂\r\n", "汉\\udfff🙂\r\n"),
))
def test_cell_projection_keeps_exact_common_text_and_source(text, expected):
    item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        random_id("entry"), 1, random_id("turn"), (LLMTextPart(cell_output_provider_text(text)),),
        input_origin=CanonicalInputOriginKind.CELL_OUTPUT,
    )
    lowered = lower_canonical_item(item)
    assert lowered.source is item
    assert lowered.fixed_message.role == "user"
    assert lowered.fixed_message.content == (LLMTextPart(expected),)
    assert item.input_origin is CanonicalInputOriginKind.CELL_OUTPUT


@pytest.mark.postgres
def test_cell_surrogate_storage_reader_compaction_and_fork_are_lossless(
    stage2_migrated_postgres_database,
):
    from datetime import datetime, timezone
    from time import monotonic

    from psycopg.rows import dict_row

    from pulsara_agent.conversation_kernel.content_parts import read_content_parts
    from pulsara_agent.conversation_kernel.contracts import InlineContent
    from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelRepository, build_prepared_tool_result_acceptance,
    )
    from pulsara_agent.conversation_kernel.steer import PreparedCellOutputIngress
    from pulsara_agent.ports.terminal_observation import CellOutputContent
    from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    from tests.support.postgres import verified_postgres_provider
    from tests.support.tool_outputs import _install_tool_call
    from tests.test_conversation_fork import child_lease, final, fork, turn

    repository = ConversationKernelRepository(verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn))
    workspace = random_id("workspace")
    source_code = 'console.log("a surrogate");'
    lease, source_turn, request, call, attempt = _install_tool_call(
        repository, workspace, tool_name="exec_cell", arguments={"code": source_code},
    )
    repository.freeze_code_mode_source(
        lease.guard, attempt_id=attempt,
        content=InlineContent.from_bytes(source_code.encode(), media_type="text/javascript"),
        deadline_monotonic=monotonic()+30,
    )
    raw = "汉🙂\r\n\ud800\n末尾\udfff"
    expected = "汉🙂\r\n\\ud800\n末尾\\udfff"
    value = CellOutputContent(random_id("cell"), source_turn, attempt, 1, {"kind": "log", "text": raw})
    ingress = PreparedCellOutputIngress(lease.guard.session_id, workspace, value, datetime.now(timezone.utc), "test")
    repository.enqueue_cell_output(lease.guard, candidate=ingress, deadline_monotonic=monotonic()+30)
    with repository._writer_transaction(lease.guard, deadline_monotonic=monotonic()+30) as connection:
        before = read_content_parts(connection, queue_item_id=ingress.queue_item_id)
        assert before[0].value == raw
        repository._consume_plan_terminal_inputs(connection, lease.guard, turn_id=source_turn, workspace_id=workspace)
        source_entry = connection.execute("SELECT consumed_entry_id FROM pulsara_v3.prompt_queue_items WHERE id=%s", (ingress.queue_item_id,)).fetchone()["consumed_entry_id"]
        assert read_content_parts(connection, entry_id=source_entry) == before
        stored_bytes = bytes(connection.execute("SELECT inline_value FROM pulsara_v3.content_parts WHERE entry_id=%s", (source_entry,)).fetchone()["inline_value"])
        assert stored_bytes == raw.encode("utf-8", errors="surrogatepass")
    assert repository.confirm_cell_output_ingress(candidate=ingress, deadline_monotonic=monotonic()+30).kind.value == "FULL_COMPATIBLE"
    repository.accept_tool_result(
        lease.guard, candidate=build_prepared_tool_result_acceptance(
            guard=lease.guard, workspace_id=workspace, result_id=random_id("result"),
            result_entry_id=random_id("entry"), turn_id=source_turn, assistant_entry_id=request,
            tool_call_id=call, attempt_id=attempt, result_state="SUCCESS", public_fields={}, content_parts=(),
            observed_at=datetime.now(timezone.utc), observation_duration_microseconds=None,
            observation_origin_kind=ToolObservationOrigin.BUILTIN,
            trusted_tool_reported_duration_microseconds=None, actor_id="test",
        ), deadline_monotonic=monotonic()+30,
    )
    reader = CanonicalProviderInputReader(repository.connection_provider)
    cut = repository.prepare_provider_input_cut(lease.guard, turn_id=source_turn, deadline_monotonic=monotonic()+30)
    source_snapshot = reader.read_frozen_snapshot(cut, deadline_monotonic=monotonic()+30)
    cell = next(item for item in source_snapshot.items if item.item_kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION)
    assert cell.content == (LLMTextPart(expected),)
    assert lower_canonical_item(cell).fixed_message.content == cell.content
    quote = reader.read_compaction_headroom_preflight(cut, deadline_monotonic=monotonic()+30)
    assert quote.selected_canonical_expanded_bytes >= source_snapshot.canonical_expanded_bytes
    anchor = final(repository, lease.guard, source_turn)
    child = fork(repository, lease.guard.session_id, anchor)
    assert child.created, child.public_code
    with repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row, deadline_monotonic=monotonic()+30) as connection:
        copied = connection.execute("SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND origin_entry_id=%s", (child.child_session_id, source_entry)).fetchone()["id"]
        assert read_content_parts(connection, entry_id=copied)[0].value == raw
    assert repository.delete_session(session_id=lease.guard.session_id, memory_domain_id="u_local", required_lifecycle=None, closed_writer=lease.guard, deadline_monotonic=monotonic()+30) == "DELETED"
    guard = child_lease(repository, child.child_session_id).guard
    _, child_cut, _ = turn(repository, guard, "continue", finish=False)
    child_snapshot = reader.read_frozen_snapshot(child_cut, deadline_monotonic=monotonic()+30)
    copied_cell = next(item for item in child_snapshot.items if item.item_kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION)
    assert copied_cell.content == (LLMTextPart(expected),)
    assert copied_cell.input_origin is CanonicalInputOriginKind.CELL_OUTPUT


@pytest.mark.postgres
@pytest.mark.parametrize("raw", ["", "汉🙂\r\n\ud800\n末尾\udfff", "x" * 70000 + "\ud800\r\n"])
def test_cell_protocol_projects_exact_js_text_for_queue_entry_and_chunk_reads(
    stage2_migrated_postgres_database, raw,
):
    import asyncio
    from datetime import datetime, timezone
    import json
    from time import monotonic

    from pulsara_agent.conversation_kernel.content_parts import read_content_parts
    from pulsara_agent.conversation_kernel.contracts import InlineContent
    from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
    from pulsara_agent.conversation_kernel.steer import PreparedCellOutputIngress
    from pulsara_agent.ports.terminal_observation import CellOutputContent
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
    from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
    from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
    from tests.support.postgres import verified_postgres_provider
    from tests.support.tool_outputs import _install_tool_call

    repository = ConversationKernelRepository(verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn))
    workspace = random_id("workspace")
    code = 'console.log("output");'
    lease, turn, request, call, attempt = _install_tool_call(repository, workspace, tool_name="exec_cell", arguments={"code": code})
    repository.freeze_code_mode_source(lease.guard, attempt_id=attempt,
        content=InlineContent.from_bytes(code.encode(), media_type="text/javascript"), deadline_monotonic=monotonic()+30)
    value = CellOutputContent(random_id("cell"), turn, attempt, 1, {"kind": "log", "text": raw})
    ingress = PreparedCellOutputIngress(lease.guard.session_id, workspace, value, datetime.now(timezone.utc), "test")
    repository.enqueue_cell_output(lease.guard, candidate=ingress, deadline_monotonic=monotonic()+30)
    protocol = CanonicalProtocolReader(repository.connection_provider)

    def snapshot():
        return protocol.snapshot(session_id=lease.guard.session_id, maximum_entries=100,
                                 maximum_control_items=100, deadline_monotonic=monotonic()+30)

    def read_body(reference, **target):
        data = bytearray()
        while len(data) < reference.size:
            frame = asyncio.run(TerminalKernelProtocolServer.read_canonical_content(
                connection_provider=repository.connection_provider, protocol_reader=protocol,
                session_id=lease.guard.session_id, request=wire.ReadContentRequest(
                    request_id=random_id("request"), offset_bytes=len(data), limit_bytes=4096, **target),
            ))
            assert frame.HasField("content"), frame
            assert frame.content.digest == reference.digest
            assert frame.content.media_type == "application/vnd.pulsara.prompt+json"
            assert frame.content.codec == "utf-8"
            data.extend(frame.content.content)
        assert len(data) == reference.size
        if reference.kind == wire.INLINE:
            assert bytes(data) == reference.inline_content
        assert json.loads(data.decode("utf-8")) == {"parts": [{"type": "text", "text": raw}]}
        return bytes(data)

    queued = next(item for item in snapshot().control.prompt_queue if item.queue_item_id == ingress.queue_item_id)
    assert queued.input_origin == "CELL_OUTPUT" and queued.source_tool_attempt_id == attempt
    before_body = read_body(queued.content, queue_item_id=ingress.queue_item_id)
    with repository._writer_transaction(lease.guard, deadline_monotonic=monotonic()+30) as connection:
        original = read_content_parts(connection, queue_item_id=ingress.queue_item_id)
        assert original[0].value == raw
        repository._consume_plan_terminal_inputs(connection, lease.guard, turn_id=turn, workspace_id=workspace)
        entry_id = connection.execute("SELECT consumed_entry_id FROM pulsara_v3.prompt_queue_items WHERE id=%s", (ingress.queue_item_id,)).fetchone()["consumed_entry_id"]
        assert read_content_parts(connection, entry_id=entry_id) == original
    entry = next(item for item in snapshot().entries if item.entry_id == entry_id)
    assert entry.input_origin == "CELL_OUTPUT" and entry.input_source.source_tool_attempt_id == attempt
    assert read_body(entry.content, entry_id=entry_id) == before_body
