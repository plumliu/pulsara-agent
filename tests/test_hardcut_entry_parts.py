"""Entry acceptance preserves common content and protocol-owned replay."""
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import replace
from io import BytesIO
from time import monotonic

from PIL import Image
from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.content_parts import read_content_parts, ToolCallValue
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.prompt_content import bind_prompt_images, freeze_canonical_prompt
from pulsara_agent.conversation_kernel.query import CanonicalConversationQuery
from pulsara_agent.conversation_kernel.repository import (
    AssistantDataBlock, AssistantTextBlock, AssistantToolCallBlock,
    ConversationKernelConflict, ConversationKernelRepository, build_prepared_root_turn_intent,
)
from pulsara_agent.conversation_kernel.visualization import (
    FrozenVisualizationOccurrence, VisualizationOccurrenceState,
)
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart
from pulsara_agent.llm.provider_replay import build_prepared_durable_provider_assistant_replay
from pulsara_agent.llm.request import provider_assistant_public_projection_fingerprint
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.model_config import (
    acquire_bound_test_writer, build_test_provider_replay_target,
    test_model_resolution_snapshot as model_resolution_snapshot,
)
from tests.support.postgres import verified_postgres_provider

pytestmark = pytest.mark.postgres


@pytest.fixture
def repository(stage2_migrated_postgres_database):
    return ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )


@contextmanager
def read(repository):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        yield connection


def open_turn(repository, text="run\n"):
    lease = acquire_bound_test_writer(
        repository, session_id=random_id("session"), workspace_id=random_id("workspace"),
        writer_owner_id=random_id("host"), lease_seconds=300,
        deadline_monotonic=monotonic() + 30,
    )
    intent = build_prepared_root_turn_intent(
        session_id=lease.guard.session_id, command_id=random_id("command"),
        turn_id=random_id("turn"), entry_id=random_id("entry"),
        context_binding_revision_id=random_id("binding"),
        permission_snapshot_id=random_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        canonical_prompt=freeze_canonical_prompt(FrozenPromptContent.text(text)),
        occurred_at=datetime.now(timezone.utc), actor_kind="human", actor_id="test",
    )
    accepted = repository.accept_root_turn_intent(
        lease.guard, intent=intent, model_resolution_snapshot=model_resolution_snapshot(),
        deadline_monotonic=monotonic() + 30,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic() + 30,
    )
    return lease, intent, accepted, cut


def test_ordered_assistant_parts_keep_call_association_and_exact_confirmation(repository):
    lease, intent, accepted, cut = open_turn(repository, text="原文\r\n\n末尾\n")
    entry_id = random_id("entry")
    call_id = "provider-call-external"
    blocks = (
        AssistantTextBlock(random_id("block"), InlineContent.from_bytes("前\n\n".encode())),
        AssistantToolCallBlock(random_id("block"), call_id, "todo", freeze_json({"x": None, "n": [1, True]})),
        AssistantDataBlock(random_id("block"), InlineContent.from_bytes(b"\x00\xff\r\n", media_type="application/octet-stream", codec="binary")),
        AssistantTextBlock(random_id("block"), InlineContent.from_bytes("后\r\n".encode())),
    )
    arguments = dict(cut=cut, entry_id=entry_id, blocks=blocks, complete_turn=False,
                     occurred_at=datetime.now(timezone.utc), actor_id="model:test")
    winner = repository.commit_assistant_message(
        lease.guard, **arguments, deadline_monotonic=monotonic() + 30,
    )
    assert repository.confirm_assistant_message_winner(
        lease.guard, **arguments, deadline_monotonic=monotonic() + 30,
    ) == winner
    changed_codec = (*blocks[:2], replace(blocks[2], data=replace(blocks[2].data, codec="opaque")), blocks[3])
    with pytest.raises(ConversationKernelConflict, match="content differs"):
        repository.confirm_assistant_message_winner(
            lease.guard, **{**arguments, "blocks": changed_codec}, deadline_monotonic=monotonic()+30,
        )
    with read(repository) as connection:
        prompt = read_content_parts(connection, entry_id=intent.entry_id)
        parts = read_content_parts(connection, entry_id=entry_id)
        permission = connection.execute(
            "SELECT permission_snapshot_fingerprint FROM pulsara_v3.turns WHERE id=%s",
            (intent.turn_id,),
        ).fetchone()["permission_snapshot_fingerprint"]
    assert prompt[0].value == "原文\r\n\n末尾\n"
    assert [part.id for part in parts] == [block.block_id for block in blocks]
    assert [part.ordinal for part in parts] == [0, 1, 2, 3]
    assert [part.type for part in parts] == ["text", "tool_call", "data", "text"]
    assert parts[0].value == "前\n\n" and parts[3].value == "后\r\n"
    assert parts[2].value == b"\x00\xff\r\n" and parts[2].data_media_type == "application/octet-stream"
    assert parts[2].data_codec == "binary"
    assert isinstance(parts[1].value, ToolCallValue)
    assert dict(parts[1].value.tool_arguments) == {"x": None, "n": (1, True)}
    attempt = repository.accept_tool_attempt(
        lease.guard, attempt_id=random_id("attempt"), assistant_entry_id=entry_id,
        tool_call_id=call_id, authorization_kind="policy", authorization_reference="allow",
        actor_kind="runtime", actor_id="test", remote_idempotency_key=None,
        retry_of_attempt_id=None, permission_snapshot_fingerprint=permission,
        occurred_at=datetime.now(timezone.utc), deadline_monotonic=monotonic() + 30,
    )
    assert attempt is not None
    with pytest.raises(ConversationKernelConflict):
        repository.confirm_assistant_message_winner(
            lease.guard, **{**arguments, "blocks": (blocks[3], *blocks[1:3], blocks[0])},
            deadline_monotonic=monotonic() + 30,
        )
    restored = repository.rehydrate_session(
        session_id=lease.guard.session_id, deadline_monotonic=monotonic() + 30,
    )
    assert [row["id"] for row in restored] == [intent.entry_id, entry_id]
    assert restored[1]["content_parts"] == parts
    assert accepted.accepted.entry_id == intent.entry_id
    page = CanonicalConversationQuery(repository.connection_provider).page_entries(
        session_id=lease.guard.session_id, deadline_monotonic=monotonic() + 30,
    )
    assert [item["id"] for item in page.entries] == [intent.entry_id, entry_id]
    assert [part["type"] for part in page.entries[1]["content_parts"]] == ["text", "tool_call", "data", "text"]
    assert page.entries[0]["content_parts"][0]["value"] == "原文\r\n\n末尾\n"
    assert page.to_dict()["entries"][1]["content_parts"][1]["value"]["tool_call_id"] == call_id


def test_failed_part_publication_rolls_back_entire_assistant_entry(repository):
    lease, intent, _, cut = open_turn(repository)
    block_id = random_id("block")
    entry_id = random_id("entry")
    with pytest.raises(ConversationKernelConflict, match="IDENTITY_CONFLICT") as failure:
        repository.commit_assistant_message(
            lease.guard, cut=cut, entry_id=entry_id,
            blocks=(AssistantTextBlock(block_id, InlineContent.from_bytes(b"first")),
                    AssistantTextBlock(block_id, InlineContent.from_bytes(b"second"))),
            occurred_at=datetime.now(timezone.utc), actor_id="model:test",
            deadline_monotonic=monotonic() + 30,
        )
    assert failure.value.__cause__.diag.constraint_name == "content_parts_pkey"
    with read(repository) as connection:
        assert connection.execute("SELECT 1 FROM pulsara_v3.transcript_entries WHERE id=%s", (entry_id,)).fetchone() is None
        assert read_content_parts(connection, entry_id=entry_id) == ()
        assert connection.execute("SELECT latest_entry_sequence FROM pulsara_v3.sessions WHERE id=%s", (lease.guard.session_id,)).fetchone()["latest_entry_sequence"] == 1
    assert repository.confirm_assistant_message_winner(
        lease.guard, cut=cut, entry_id=entry_id,
        blocks=(AssistantTextBlock(block_id, InlineContent.from_bytes(b"first")),),
        complete_turn=False, occurred_at=datetime.now(timezone.utc), actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    ) is None


def test_provider_replay_and_image_visualization_keep_independent_truth(repository):
    lease, intent, _, cut = open_turn(repository)
    with read(repository) as connection:
        workspace_id = connection.execute("SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s", (lease.guard.session_id,)).fetchone()["workspace_id"]
    entry_id = random_id("entry")
    output = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(output, "PNG")
    image = bind_prompt_images(FrozenPromptContent((LLMImagePart("image/png", output.getvalue(), 7, 5),))).parts[0]
    replay = build_prepared_durable_provider_assistant_replay(
        session_id=lease.guard.session_id, workspace_id=workspace_id,
        assistant_entry_id=entry_id,
        target=build_test_provider_replay_target(wire_api="openai_chat_completions", transport_binding_id="test"),
        public_projection_fingerprint=provider_assistant_public_projection_fingerprint(text="answer", tool_calls=()),
        ordered_items=(freeze_json({"role": "assistant", "content": "answer", "reasoning_content": "精确思考\r\n"}),),
    )
    arguments = dict(
        cut=cut, entry_id=entry_id,
        blocks=(AssistantTextBlock(random_id("block"), InlineContent.from_bytes(b"answer")),),
        visualizations=(FrozenVisualizationOccurrence("image.png", VisualizationOccurrenceState.READY, image=image),),
        provider_replay=replay, complete_turn=True,
        occurred_at=datetime.now(timezone.utc), actor_id="model:test",
    )
    winner = repository.commit_assistant_message(lease.guard, **arguments, deadline_monotonic=monotonic() + 30)
    assert repository.confirm_assistant_message_winner(lease.guard, **arguments, deadline_monotonic=monotonic() + 30) == winner
    with read(repository) as connection:
        payload = connection.execute("SELECT payload_bytes FROM pulsara_v3.provider_assistant_replay_fragments WHERE assistant_entry_id=%s", (entry_id,)).fetchone()["payload_bytes"]
        media = connection.execute("SELECT b.width,b.height,b.workspace_id FROM pulsara_v3.assistant_visualizations v JOIN pulsara_v3.blobs b ON b.id=v.blob_id WHERE v.assistant_entry_id=%s", (entry_id,)).fetchone()
        parts = read_content_parts(connection, entry_id=entry_id)
    assert bytes(payload) == replay.payload_bytes
    assert media == {"width": 7, "height": 5, "workspace_id": None}
    assert [part.value for part in parts] == ["answer"]


def test_scheduled_manual_confirmation_and_inspector_follow_consumed_content(repository):
    from pulsara_agent.conversation_kernel.contracts import PromptDeliveryMode
    from pulsara_agent.conversation_kernel.steer import (
        build_prompt_ingress_command, QueuedRootTurnAdmissionConfirmationKind,
    )
    from pulsara_agent.scheduling.contracts import ScheduledAdmission, ScheduledTriggerSource

    lease = acquire_bound_test_writer(
        repository, session_id=random_id("session"), workspace_id=random_id("workspace"),
        writer_owner_id=random_id("host"), lease_seconds=300,
        deadline_monotonic=monotonic() + 30,
    )
    resolution = model_resolution_snapshot()
    task = repository.create_scheduled_task(
        status="PAUSED", session_id=lease.guard.session_id, memory_domain_id="u_local",
        guard=lease.guard,
        values={"name": "report", "prompt": "任务\r\n\n原文\n", "schedule": {
            "kind": "interval", "anchor_at_utc": "2020-01-01T00:00:00+00:00", "seconds": 900,
        }, "timezone": "Asia/Shanghai", "permission_mode": DEFAULT_PERMISSION_MODE.value},
        model_resolution_snapshot=resolution, deadline_monotonic=monotonic() + 30,
    )
    command_id = random_id("command")
    manual = ScheduledAdmission(
        task["id"], lease.guard.session_id, task["revision"], datetime.now(timezone.utc).replace(microsecond=0),
        task["prompt"], DEFAULT_PERMISSION_MODE, trigger_source=ScheduledTriggerSource.RUN_ONCE,
        client_command_id=command_id,
    )
    permission = repository.prepare_root_permission_snapshot(
        lease.guard, snapshot_id=random_id("permission"), requested_mode=DEFAULT_PERMISSION_MODE,
        deadline_monotonic=monotonic() + 30,
    )
    candidate = build_prompt_ingress_command(
        session_id=lease.guard.session_id, command_id=command_id, queue_item_id=random_id("queue"),
        client_submission_id=random_id("command"), delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None, permission_snapshot_id=permission.snapshot_id,
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        canonical_prompt=freeze_canonical_prompt(FrozenPromptContent.text(task["prompt"])), scheduled=manual,
    )
    repository.enqueue_prompt(
        lease.guard, candidate=candidate, model_resolution_snapshot=resolution,
        occurred_at=datetime.now(timezone.utc), actor_id="test", deadline_monotonic=monotonic() + 30,
        _expected_permission_snapshot=permission,
    )
    with read(repository) as connection:
        original = read_content_parts(connection, queue_item_id=candidate.queue_item_id)
    query = CanonicalConversationQuery(repository.connection_provider)
    pending = query.inspect(session_id=lease.guard.session_id, deadline_monotonic=monotonic() + 30)
    assert pending.prompt_queue[0]["content_parts"][0]["value"] == task["prompt"]
    started = repository.prepare_prompt_head_consumption(
        session_id=lease.guard.session_id, occurred_at=datetime.now(timezone.utc),
        actor_id="test", deadline_monotonic=monotonic() + 30,
    )
    consumed = repository.consume_prepared_prompt_head(
        lease.guard, candidate=started, deadline_monotonic=monotonic() + 30,
    )
    assert consumed.kind is QueuedRootTurnAdmissionConfirmationKind.FULL
    confirmed = repository.confirm_scheduled_action(
        request=manual.action_value(), memory_domain_id="u_local", deadline_monotonic=monotonic() + 30,
    )
    assert confirmed["id"] == candidate.queue_item_id and confirmed["status"] == "CONSUMED"
    with read(repository) as connection:
        assert read_content_parts(connection, queue_item_id=candidate.queue_item_id) == ()
        actual = read_content_parts(connection, entry_id=confirmed["consumed_entry_id"])
    assert actual == original
    inspector = query.inspect(session_id=lease.guard.session_id, deadline_monotonic=monotonic() + 30)
    assert inspector.prompt_queue[0]["content_parts"] == pending.prompt_queue[0]["content_parts"]


def test_fork_keeps_canonical_workspace_and_existing_memory_row(repository, tmp_path):
    from tests.support.model_config import start_test_root_turn, test_model_binding, test_model_runtime
    from pulsara_agent.workspace_identity import HostWorkspaceInput, resolve_workspace

    workspace = resolve_workspace(HostWorkspaceInput("project", tmp_path))
    lease = acquire_bound_test_writer(
        repository, session_id=random_id("session"), workspace_id=workspace.workspace_key,
        workspace_root=workspace.workspace_root.as_posix(), workspace_label=workspace.display_label,
        writer_owner_id=random_id("host"), lease_seconds=300,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id, anchor_id = random_id("turn"), random_id("entry")
    start_test_root_turn(
        repository, lease.guard, command_id=random_id("command"), turn_id=turn_id,
        entry_id=random_id("entry"), context_binding_revision_id=random_id("binding"),
        permission_snapshot_id=random_id("permission"), requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text("Use the existing project memory.\n"),
        occurred_at=datetime.now(timezone.utc), deadline_monotonic=monotonic() + 30,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30,
    )
    repository.commit_assistant_message(
        lease.guard, cut=cut, entry_id=anchor_id,
        blocks=(AssistantTextBlock(random_id("block"), InlineContent.from_bytes(b"Project context retained.\n")),),
        complete_turn=True, occurred_at=datetime.now(timezone.utc), actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    memory_id = random_id("memory")
    with read(repository) as connection:
        # A current canonical memory row is sufficient to exercise fork storage;
        # public memory CRUD and recall contracts belong to the E work package.
        connection.execute(
            """INSERT INTO pulsara_v3.memory_facts
               (id,memory_domain_id,context_id,fact_kind,title,statement)
               VALUES (%s,'u_local',%s,'FACT','Shared project checklist',%s)""",
            (memory_id, workspace.workspace_key, "Release checklist\r\nremains shared.\n"),
        )
        memory_before = connection.execute(
            "SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id='u_local' ORDER BY id",
        ).fetchall()
        workspace_before = connection.execute(
            "SELECT * FROM pulsara_v3.workspaces WHERE memory_domain_id='u_local' AND id=%s",
            (workspace.workspace_key,),
        ).fetchall()
    child_id = random_id("session")
    created = repository.fork_conversation(
        source_session_id=lease.guard.session_id, anchor_entry_id=anchor_id,
        child_session_id=child_id, memory_domain_id="u_local", deadline_monotonic=monotonic() + 30,
    )
    assert created.created, created.public_code
    with read(repository) as connection:
        sessions = connection.execute(
            "SELECT id,workspace_id,memory_domain_id FROM pulsara_v3.sessions WHERE id=ANY(%s) ORDER BY id",
            ([lease.guard.session_id, child_id],),
        ).fetchall()
        assert {row["id"] for row in sessions} == {lease.guard.session_id, child_id}
        assert all(row["workspace_id"] == workspace.workspace_key and row["memory_domain_id"] == "u_local" for row in sessions)
        assert connection.execute(
            "SELECT * FROM pulsara_v3.workspaces WHERE memory_domain_id='u_local' AND id=%s",
            (workspace.workspace_key,),
        ).fetchall() == workspace_before
        assert connection.execute(
            "SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id='u_local' ORDER BY id",
        ).fetchall() == memory_before
    assert repository.delete_session(
        session_id=lease.guard.session_id, memory_domain_id="u_local", closed_writer=lease.guard,
        required_lifecycle=None, deadline_monotonic=monotonic() + 30,
    ) == "DELETED"
    with read(repository) as connection:
        assert connection.execute(
            "SELECT * FROM pulsara_v3.memory_facts WHERE memory_domain_id='u_local' ORDER BY id",
        ).fetchall() == memory_before
        assert connection.execute(
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s", (child_id,),
        ).fetchone()["workspace_id"] == workspace.workspace_key
