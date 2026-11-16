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
from pulsara_agent.ports.terminal_observation import (
    cell_output_provider_text,
    CellOutputSource,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("console output\nnext line\n", "console output\nnext line\n"),
        ('{"value":42}', '{"value":42}'),
        ("汉🙂\r\n", "汉🙂\r\n"),
        ("\ud800", "\\ud800"),
        ("汉\udfff🙂\r\n", "汉\\udfff🙂\r\n"),
    ),
)
def test_cell_projection_keeps_exact_common_text_and_source(text, expected):
    item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        random_id("entry"),
        1,
        random_id("turn"),
        (LLMTextPart(cell_output_provider_text(text)),),
        input_origin=CanonicalInputOriginKind.CELL_OUTPUT,
        cell_output_source=CellOutputSource(random_id("cell"), 1, "log"),
    )
    lowered = lower_canonical_item(item)
    assert lowered.source is item
    assert lowered.message.role == "user"
    assert lowered.message.content[0] == LLMTextPart(
        f"Cell {item.cell_output_source.cell_id} 输出（log，第 1 条）：\n"
    )
    assert lowered.message.content[1:] == (LLMTextPart(expected),)
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
        ConversationKernelRepository,
        build_prepared_tool_result_acceptance,
    )
    from pulsara_agent.conversation_kernel.steer import PreparedCellOutputIngress
    from pulsara_agent.ports.terminal_observation import CellOutputContent
    from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
    from pulsara_agent.storage.postgres_connection_provider import (
        PostgresConnectionLane,
    )
    from tests.support.postgres import verified_postgres_provider
    from tests.support.tool_outputs import _install_tool_call
    from tests.test_conversation_fork import child_lease, final, fork, turn

    repository = ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )
    workspace = random_id("workspace")
    source_code = 'console.log("a surrogate");'
    lease, source_turn, request, call, attempt = _install_tool_call(
        repository,
        workspace,
        tool_name="exec_cell",
        arguments={"code": source_code},
    )
    repository.freeze_code_mode_source(
        lease.guard,
        attempt_id=attempt,
        content=InlineContent.from_bytes(
            source_code.encode(), media_type="text/javascript"
        ),
        deadline_monotonic=monotonic() + 30,
    )
    raw = "汉🙂\r\n\ud800\n末尾\udfff"
    expected = "汉🙂\r\n\\ud800\n末尾\\udfff"
    value = CellOutputContent(
        random_id("cell"),
        source_turn,
        attempt,
        1,
        {"kind": "log", "content": [{"type": "text", "value": raw}]},
    )
    ingress = PreparedCellOutputIngress(
        lease.guard.session_id, workspace, value, datetime.now(timezone.utc), "test"
    )
    repository.enqueue_cell_output(
        lease.guard, candidate=ingress, deadline_monotonic=monotonic() + 30
    )
    with repository._writer_transaction(
        lease.guard, deadline_monotonic=monotonic() + 30
    ) as connection:
        before = read_content_parts(connection, queue_item_id=ingress.queue_item_id)
        assert before[0].value == raw
        repository._consume_plan_terminal_inputs(
            connection, lease.guard, turn_id=source_turn, workspace_id=workspace
        )
        source_entry = connection.execute(
            "SELECT consumed_entry_id FROM pulsara_v3.prompt_queue_items WHERE id=%s",
            (ingress.queue_item_id,),
        ).fetchone()["consumed_entry_id"]
        assert read_content_parts(connection, entry_id=source_entry) == before
        stored_bytes = bytes(
            connection.execute(
                "SELECT inline_value FROM pulsara_v3.content_parts WHERE entry_id=%s",
                (source_entry,),
            ).fetchone()["inline_value"]
        )
        assert stored_bytes == raw.encode("utf-8", errors="surrogatepass")
    assert (
        repository.confirm_cell_output_ingress(
            candidate=ingress, deadline_monotonic=monotonic() + 30
        ).kind.value
        == "FULL_COMPATIBLE"
    )
    repository.accept_tool_result(
        lease.guard,
        candidate=build_prepared_tool_result_acceptance(
            guard=lease.guard,
            workspace_id=workspace,
            result_id=random_id("result"),
            result_entry_id=random_id("entry"),
            turn_id=source_turn,
            assistant_entry_id=request,
            tool_call_id=call,
            attempt_id=attempt,
            result_state="SUCCESS",
            public_fields={},
            content_parts=(),
            observed_at=datetime.now(timezone.utc),
            observation_duration_microseconds=None,
            observation_origin_kind=ToolObservationOrigin.BUILTIN,
            trusted_tool_reported_duration_microseconds=None,
            actor_id="test",
        ),
        deadline_monotonic=monotonic() + 30,
    )
    reader = CanonicalProviderInputReader(repository.connection_provider)
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=source_turn, deadline_monotonic=monotonic() + 30
    )
    source_snapshot = reader.read_frozen_snapshot(
        cut, deadline_monotonic=monotonic() + 30
    )
    cell = next(
        item
        for item in source_snapshot.items
        if item.item_kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION
    )
    assert cell.content == (LLMTextPart(expected),)
    assert cell.cell_output_source == value.source
    assert lower_canonical_item(cell).message.content[1:] == cell.content
    quote = reader.read_compaction_headroom_preflight(
        cut, deadline_monotonic=monotonic() + 30
    )
    assert (
        quote.selected_canonical_expanded_bytes
        >= source_snapshot.canonical_expanded_bytes
    )
    anchor = final(repository, lease.guard, source_turn)
    child = fork(repository, lease.guard.session_id, anchor)
    assert child.created, child.public_code
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        copied = connection.execute(
            "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND origin_entry_id=%s",
            (child.child_session_id, source_entry),
        ).fetchone()["id"]
        assert read_content_parts(connection, entry_id=copied)[0].value == raw
    assert (
        repository.delete_session(
            session_id=lease.guard.session_id,
            memory_domain_id="u_local",
            required_lifecycle=None,
            closed_writer=lease.guard,
            deadline_monotonic=monotonic() + 30,
        )
        == "DELETED"
    )
    guard = child_lease(repository, child.child_session_id).guard
    _, child_cut, _ = turn(repository, guard, "continue", finish=False)
    child_snapshot = reader.read_frozen_snapshot(
        child_cut, deadline_monotonic=monotonic() + 30
    )
    copied_cell = next(
        item
        for item in child_snapshot.items
        if item.item_kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION
    )
    assert copied_cell.content == (LLMTextPart(expected),)
    assert copied_cell.input_origin is CanonicalInputOriginKind.CELL_OUTPUT
    assert copied_cell.cell_output_source == value.source
    assert (
        lower_canonical_item(copied_cell).message.content[0]
        == lower_canonical_item(cell).message.content[0]
    )
    child_quote = reader.read_compaction_headroom_preflight(
        child_cut, deadline_monotonic=monotonic() + 30
    )
    assert (
        child_quote.selected_canonical_expanded_bytes
        >= child_snapshot.canonical_expanded_bytes
    )
    child_anchor = final(repository, guard, child_cut.turn_id)
    nested = fork(repository, child.child_session_id, child_anchor)
    assert nested.created, nested.public_code
    assert (
        repository.delete_session(
            session_id=child.child_session_id,
            memory_domain_id="u_local",
            required_lifecycle=None,
            closed_writer=guard,
            deadline_monotonic=monotonic() + 30,
        )
        == "DELETED"
    )
    nested_guard = child_lease(repository, nested.child_session_id).guard
    _, nested_cut, _ = turn(repository, nested_guard, "continue nested", finish=False)
    nested_snapshot = reader.read_frozen_snapshot(
        nested_cut, deadline_monotonic=monotonic() + 30
    )
    nested_cell = next(
        item
        for item in nested_snapshot.items
        if item.item_kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION
    )
    assert nested_cell.content == (LLMTextPart(expected),)
    assert nested_cell.input_origin is CanonicalInputOriginKind.CELL_OUTPUT
    assert nested_cell.cell_output_source == value.source
    assert (
        lower_canonical_item(nested_cell).message.content[0]
        == lower_canonical_item(cell).message.content[0]
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert (
            read_content_parts(connection, entry_id=nested_cell.source_entry_id)[
                0
            ].value
            == raw
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND input_origin='CELL_OUTPUT'",
                (nested.child_session_id,),
            ).fetchone()["count"]
            == 0
        )
    nested_quote = reader.read_compaction_headroom_preflight(
        nested_cut, deadline_monotonic=monotonic() + 30
    )
    assert (
        nested_quote.selected_canonical_expanded_bytes
        >= nested_snapshot.canonical_expanded_bytes
    )


@pytest.mark.postgres
@pytest.mark.parametrize(
    "raw", ["", "汉🙂\r\n\ud800\n末尾\udfff", "x" * 70000 + "\ud800\r\n"]
)
def test_cell_protocol_projects_exact_js_text_for_queue_entry_and_chunk_reads(
    stage2_migrated_postgres_database,
    raw,
):
    import asyncio
    from datetime import datetime, timezone
    import json
    from time import monotonic

    from pulsara_agent.conversation_kernel.content_parts import read_content_parts
    from pulsara_agent.conversation_kernel.contracts import InlineContent
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelRepository,
    )
    from pulsara_agent.conversation_kernel.steer import PreparedCellOutputIngress
    from pulsara_agent.ports.terminal_observation import CellOutputContent
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
    from pulsara_agent.terminal_protocol.generated_v3 import (
        terminal_kernel_v3_pb2 as wire,
    )
    from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
    from tests.support.postgres import verified_postgres_provider
    from tests.support.tool_outputs import _install_tool_call

    repository = ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )
    workspace = random_id("workspace")
    code = 'console.log("output");'
    lease, turn, request, call, attempt = _install_tool_call(
        repository, workspace, tool_name="exec_cell", arguments={"code": code}
    )
    repository.freeze_code_mode_source(
        lease.guard,
        attempt_id=attempt,
        content=InlineContent.from_bytes(code.encode(), media_type="text/javascript"),
        deadline_monotonic=monotonic() + 30,
    )
    value = CellOutputContent(
        random_id("cell"),
        turn,
        attempt,
        1,
        {"kind": "log", "content": [{"type": "text", "value": raw}]},
    )
    ingress = PreparedCellOutputIngress(
        lease.guard.session_id, workspace, value, datetime.now(timezone.utc), "test"
    )
    repository.enqueue_cell_output(
        lease.guard, candidate=ingress, deadline_monotonic=monotonic() + 30
    )
    protocol = CanonicalProtocolReader(repository.connection_provider)

    def snapshot():
        return protocol.snapshot(
            session_id=lease.guard.session_id,
            maximum_entries=100,
            maximum_control_items=100,
            deadline_monotonic=monotonic() + 30,
        )

    def read_body(reference, **target):
        data = bytearray()
        while len(data) < reference.size:
            frame = asyncio.run(
                TerminalKernelProtocolServer.read_canonical_content(
                    connection_provider=repository.connection_provider,
                    protocol_reader=protocol,
                    session_id=lease.guard.session_id,
                    request=wire.ReadContentRequest(
                        request_id=random_id("request"),
                        offset_bytes=len(data),
                        limit_bytes=4096,
                        **target,
                    ),
                )
            )
            assert frame.HasField("content"), frame
            assert frame.content.digest == reference.digest
            assert frame.content.media_type == "application/vnd.pulsara.prompt+json"
            assert frame.content.codec == "utf-8"
            data.extend(frame.content.content)
        assert len(data) == reference.size
        if reference.kind == wire.INLINE:
            assert bytes(data) == reference.inline_content
        assert json.loads(data.decode("utf-8")) == {
            "parts": [{"type": "text", "text": raw}]
        }
        return bytes(data)

    queued = next(
        item
        for item in snapshot().control.prompt_queue
        if item.queue_item_id == ingress.queue_item_id
    )
    assert (
        queued.input_origin == "CELL_OUTPUT"
        and queued.source_tool_attempt_id == attempt
    )
    before_body = read_body(queued.content, queue_item_id=ingress.queue_item_id)
    with repository._writer_transaction(
        lease.guard, deadline_monotonic=monotonic() + 30
    ) as connection:
        original = read_content_parts(connection, queue_item_id=ingress.queue_item_id)
        assert original[0].value == raw
        repository._consume_plan_terminal_inputs(
            connection, lease.guard, turn_id=turn, workspace_id=workspace
        )
        entry_id = connection.execute(
            "SELECT consumed_entry_id FROM pulsara_v3.prompt_queue_items WHERE id=%s",
            (ingress.queue_item_id,),
        ).fetchone()["consumed_entry_id"]
        assert read_content_parts(connection, entry_id=entry_id) == original
    entry = next(item for item in snapshot().entries if item.entry_id == entry_id)
    assert (
        entry.input_origin == "CELL_OUTPUT"
        and entry.input_source.source_tool_attempt_id == attempt
    )
    assert read_body(entry.content, entry_id=entry_id) == before_body


@pytest.mark.postgres
def test_cell_image_new_turn_and_nested_forks_keep_content_and_source_without_parent_queues(
    stage2_migrated_postgres_database,
    monkeypatch,
):
    from datetime import datetime, timezone
    from io import BytesIO
    from time import monotonic
    from PIL import Image
    import psycopg
    from psycopg.rows import dict_row
    from pulsara_agent.conversation_kernel.content_parts import read_content_parts
    from pulsara_agent.conversation_kernel.contracts import (
        InlineContent,
        PromptDeliveryMode,
    )
    from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelRepository,
        build_prepared_tool_result_acceptance,
    )
    from pulsara_agent.conversation_kernel.steer import PreparedCellOutputIngress
    from pulsara_agent.llm.input import LLMImagePart
    from pulsara_agent.ports.terminal_observation import CellOutputContent
    from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
    from pulsara_agent.storage.postgres_connection_provider import (
        PostgresConnectionLane,
    )
    from tests.support.images import bound_test_image
    from tests.support.postgres import verified_postgres_provider
    from tests.support.tool_outputs import _install_tool_call
    from tests.test_conversation_fork import child_lease, final, fork, turn

    repository = ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )
    workspace = random_id("workspace")
    guard_lease, source_turn, request, call, attempt = _install_tool_call(
        repository,
        workspace,
        tool_name="exec_cell",
        arguments={"code": 'console.log("images");'},
    )
    guard = guard_lease.guard
    repository.freeze_code_mode_source(
        guard,
        attempt_id=attempt,
        content=InlineContent.from_bytes(
            b'console.log("images");', media_type="text/javascript"
        ),
        deadline_monotonic=monotonic() + 30,
    )
    cell_id = random_id("cell")
    repository.accept_tool_result(
        guard,
        candidate=build_prepared_tool_result_acceptance(
            guard=guard,
            workspace_id=workspace,
            result_id=random_id("result"),
            result_entry_id=random_id("entry"),
            turn_id=source_turn,
            assistant_entry_id=request,
            tool_call_id=call,
            attempt_id=attempt,
            result_state="SUCCESS",
            public_fields={"target": {"kind": "CELL", "cell_id": cell_id}},
            content_parts=(),
            observed_at=datetime.now(timezone.utc),
            observation_duration_microseconds=None,
            observation_origin_kind=ToolObservationOrigin.BUILTIN,
            trusted_tool_reported_duration_microseconds=None,
            actor_id="test",
        ),
        deadline_monotonic=monotonic() + 30,
    )
    final(repository, guard, source_turn)
    encoded = BytesIO()
    Image.new("RGB", (7, 5), "navy").save(encoded, format="PNG")
    image = bound_test_image("image/png", encoded.getvalue(), 7, 5)
    encoded_second = BytesIO()
    Image.new("RGB", (7, 5), "olive").save(encoded_second, format="PNG")
    second_image = bound_test_image("image/png", encoded_second.getvalue(), 7, 5)
    image_part = {
        "type": "image",
        "value": {"kind": "IMAGE_REF", "image_ref": image.image_ref},
    }
    value = CellOutputContent(
        cell_id,
        source_turn,
        attempt,
        1,
        {
            "kind": "log",
            "content": [
                {"type": "text", "value": "先"},
                image_part,
                {"type": "text", "value": "后"},
                {
                    "type": "image",
                    "value": {"kind": "IMAGE_REF", "image_ref": second_image.image_ref},
                },
                image_part,
            ],
        },
        (image, second_image),
    )
    ingress = PreparedCellOutputIngress(
        guard.session_id, workspace, value, datetime.now(timezone.utc), "test"
    )
    accepted = repository.enqueue_cell_output(
        guard, candidate=ingress, deadline_monotonic=monotonic() + 30
    )
    assert accepted.delivery_mode is PromptDeliveryMode.NEW_TURN
    prepared = repository.prepare_prompt_head_consumption(
        session_id=guard.session_id,
        occurred_at=datetime.now(timezone.utc),
        actor_id="test",
        deadline_monotonic=monotonic() + 30,
    )
    started = repository.consume_prepared_prompt_head(
        guard, candidate=prepared, deadline_monotonic=monotonic() + 30
    )
    initial = started.accepted.started_input_group.ordered_items
    assert len(initial) == 1
    assert initial[0].cell_output_source == value.source
    assert initial[0].content == value.content.parts
    reader = CanonicalProviderInputReader(repository.connection_provider)
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader

    protocol = CanonicalProtocolReader(repository.connection_provider)
    source_session_id = guard.session_id

    def check(current_guard, current_turn):
        cut = repository.prepare_provider_input_cut(
            current_guard, turn_id=current_turn, deadline_monotonic=monotonic() + 30
        )
        snapshot = reader.read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
        cell = next(
            item
            for item in snapshot.items
            if item.item_kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION
        )
        assert cell.cell_output_source == value.source
        assert cell.content == value.content.parts
        projected = lower_canonical_item(cell).message.content
        assert projected[0] == LLMTextPart(f"Cell {cell_id} 输出（log，第 1 条）：\n")
        assert [
            part.immutable_bytes for part in projected if isinstance(part, LLMImagePart)
        ] == [
            image.immutable_bytes,
            second_image.immutable_bytes,
            image.immutable_bytes,
        ]
        assert [
            part.text
            for part in projected
            if isinstance(part, LLMTextPart) and part.text in {"先", "后"}
        ] == ["先", "后"]
        quote = reader.read_compaction_headroom_preflight(
            cut, deadline_monotonic=monotonic() + 30
        )
        assert (
            quote.selected_canonical_expanded_bytes >= snapshot.canonical_expanded_bytes
        )
        with repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            parts = read_content_parts(connection, entry_id=cell.source_entry_id)
            assert [part.type for part in parts] == [
                "text",
                "image",
                "text",
                "image",
                "image",
            ]
            assert [
                part.value["image_ref"] for part in parts if part.type == "image"
            ] == [image.image_ref, second_image.image_ref, image.image_ref]
        wire_snapshot = protocol.snapshot(
            session_id=current_guard.session_id,
            maximum_entries=100,
            maximum_control_items=100,
            deadline_monotonic=monotonic() + 30,
        )
        wire_cell = next(
            entry
            for entry in wire_snapshot.entries
            if entry.entry_id == cell.source_entry_id
        )
        assert wire_cell.input_origin == "CELL_OUTPUT"
        assert wire_cell.input_source.source_cell_id == cell_id
        assert wire_cell.input_source.source_record_sequence == 1
        assert wire_cell.input_source.cell_output_kind == "log"
        if current_guard.session_id != source_session_id:
            assert not wire_cell.input_source.queue_item_id
            assert not wire_cell.input_source.source_tool_attempt_id
            assert not wire_cell.input_source.source_turn_id
        return cell

    current_turn = prepared.exact_turn_id
    healthy_cell = check(guard, current_turn)
    from pulsara_agent.conversation_kernel._repository.queue_content import (
        input_owner_metadata,
        read_input_owner,
    )
    from pulsara_agent.conversation_kernel.prompt_storage import (
        read_image_reference_in_connection,
    )
    from pulsara_agent.model_input.contracts import provider_input_item_fingerprint
    from pulsara_agent.conversation_kernel.compaction.contracts import (
        provider_input_item_canonical_expanded_bytes,
    )

    cut = repository.prepare_provider_input_cut(
        guard, turn_id=current_turn, deadline_monotonic=monotonic() + 30
    )
    healthy_snapshot = reader.read_frozen_snapshot(
        cut, deadline_monotonic=monotonic() + 30
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        queue = connection.execute(
            "SELECT * FROM pulsara_v3.prompt_queue_items WHERE id=%s",
            (ingress.queue_item_id,),
        ).fetchone()
        original_parts = read_content_parts(
            connection, entry_id=healthy_cell.source_entry_id
        )
    # Deliberately damage only this isolated test database's accepted asset
    # payload. Metadata and canonical entry/part/source rows remain untouched.
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            "UPDATE pulsara_v3.blobs SET body=%s WHERE id=%s",
            (b"!" * len(image.immutable_bytes), image.image_ref),
        )
    try:
        damaged_snapshot = reader.read_frozen_snapshot(
            cut, deadline_monotonic=monotonic() + 30
        )
        damaged_cell = next(
            item
            for item in damaged_snapshot.items
            if item.source_entry_id == healthy_cell.source_entry_id
        )
        assert damaged_cell.cell_output_source == value.source
        assert provider_input_item_fingerprint(
            damaged_cell
        ) == provider_input_item_fingerprint(healthy_cell)
        assert (
            damaged_snapshot.canonical_expanded_bytes
            == healthy_snapshot.canonical_expanded_bytes
        )
        assert (
            provider_input_item_canonical_expanded_bytes(damaged_cell)
            == value.resource_quote.canonical_expanded_bytes
        )
        projected = lower_canonical_item(damaged_cell).message.content
        texts = [part.text for part in projected if isinstance(part, LLMTextPart)]
        assert texts[0] == f"Cell {cell_id} 输出（log，第 1 条）：\n"
        assert texts.count("先") == texts.count("后") == 1
        assert sum("blob content integrity mismatch" in text for text in texts) == 2
        assert sum(image.image_ref in text for text in texts) == 2
        assert [
            part.immutable_bytes for part in projected if isinstance(part, LLMImagePart)
        ] == [second_image.immutable_bytes]
        with repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            degraded = read_input_owner(connection, row=queue)
            assert degraded.storage_parts == value.storage_parts
            assert degraded.body == value.body
            assert degraded.resource_quote == value.resource_quote
            assert degraded.source == value.source
            assert (
                read_content_parts(connection, entry_id=healthy_cell.source_entry_id)
                == original_parts
            )
            _, quoted = input_owner_metadata(
                connection,
                row={"session_id": guard.session_id, "input_origin": "CELL_OUTPUT"},
                transcript_entry_id=healthy_cell.source_entry_id,
            )
            assert quoted == value.resource_quote.canonical_expanded_bytes
    finally:
        with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
            connection.execute(
                "UPDATE pulsara_v3.blobs SET body=%s WHERE id=%s",
                (image.immutable_bytes, image.image_ref),
            )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.ARTIFACT,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert (
            read_image_reference_in_connection(connection, image_ref=image.image_ref)
            == image
        )
    recovered_cell = check(guard, current_turn)
    assert provider_input_item_fingerprint(
        recovered_cell
    ) == provider_input_item_fingerprint(damaged_cell)
    assert lower_canonical_item(damaged_cell).message.content == projected
    import pulsara_agent.conversation_kernel.prompt_storage as asset_owner
    from pulsara_agent.conversation_kernel.repository_errors import (
        ConversationKernelConflict,
    )

    original_payload_reader = asset_owner.read_image_reference_payload_in_connection
    calls = []

    def timeout_one_asset(connection, *, descriptor):
        calls.append(descriptor.image_ref)
        if descriptor.image_ref == image.image_ref:
            raise TimeoutError("PNG payload read deadline elapsed")
        return original_payload_reader(connection, descriptor=descriptor)

    with monkeypatch.context() as fault:
        fault.setattr(
            asset_owner, "read_image_reference_payload_in_connection", timeout_one_asset
        )
        with repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            timed_out = read_input_owner(connection, row=queue)
        assert calls == [image.image_ref, second_image.image_ref]
        assert (
            timed_out.body == value.body
            and timed_out.storage_parts == value.storage_parts
        )
        assert timed_out.resource_quote == value.resource_quote
        failed_parts = timed_out.content.parts
        assert failed_parts[1] is failed_parts[-1]
        assert "PNG payload read deadline elapsed" in failed_parts[1].reason
        assert failed_parts[3] == second_image

    for failure_owner in (
        "read_image_reference_metadata_in_connection",
        "read_image_reference_payload_in_connection",
    ):
        failure = (
            ConversationKernelConflict("asset metadata owner cannot confirm identity")
            if "metadata" in failure_owner
            else psycopg.OperationalError("asset database owner unavailable")
        )

        def reject_owner(*args, **kwargs):
            raise failure

        with monkeypatch.context() as fault:
            fault.setattr(asset_owner, failure_owner, reject_owner)
            with repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR,
                row_factory=dict_row,
                deadline_monotonic=monotonic() + 30,
            ) as connection:
                with pytest.raises(type(failure)) as caught:
                    read_input_owner(connection, row=queue)
                assert caught.value is failure
    for _ in range(2):
        anchor = final(repository, guard, current_turn)
        child = fork(repository, guard.session_id, anchor)
        assert child.created, child.public_code
        assert (
            repository.delete_session(
                session_id=guard.session_id,
                memory_domain_id="u_local",
                required_lifecycle=None,
                closed_writer=guard,
                deadline_monotonic=monotonic() + 30,
            )
            == "DELETED"
        )
        guard = child_lease(repository, child.child_session_id).guard
        child_turn, _, _ = turn(
            repository, guard, "continue from independent Cell history", finish=False
        )
        cell = check(guard, child_turn)
        with repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND input_origin='CELL_OUTPUT'",
                    (guard.session_id,),
                ).fetchone()["count"]
                == 0
            )
            source = connection.execute(
                "SELECT source_cell_id,source_record_sequence,cell_output_kind FROM pulsara_v3.transcript_entries WHERE id=%s",
                (cell.source_entry_id,),
            ).fetchone()
            assert tuple(source.values()) == (cell_id, 1, "log")
        current_turn = child_turn
