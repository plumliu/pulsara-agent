from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Barrier
from time import monotonic, sleep
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.conversation_kernel.contracts import (
    InlineContent,
    JobSafetyClass,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.activation import (
    require_stage2_runtime_privilege_boundary,
)
from pulsara_agent.conversation_kernel.blob import (
    MAXIMUM_BLOB_BYTES,
    MAXIMUM_CONTENT_CHUNK_BYTES,
    PostgresCanonicalBlobStore,
)
from pulsara_agent.conversation_kernel.memory import PostgresMemoryQuery
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenMemoryProposal,
    MemoryKindHint,
    MemoryProducerKind,
    prepare_memory_candidate,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    JobAttemptTerminalized,
    StaleHostWriter,
    ToolRemoteIdentityConfirmationKind,
    build_prepared_tool_remote_identity_publication,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.steer import (
    PromptIngressConfirmationKind,
    QueuedRootTurnAdmissionConfirmationKind,
    SteerConsumptionConfirmationKind,
    SteerResourceRejectionConfirmationKind,
    build_prompt_ingress_command,
    build_steer_canonical_base_fence,
    build_steer_consumption_candidate,
    build_steer_resource_rejection,
)
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.model_input.continuity import (
    FULL_HISTORY_CONTEXT_BASE_IDENTITY,
    NoNewTriggerAnchor,
    ProcessLocalCanonicalFrontier,
    ProviderInputContinuityScope,
)
from pulsara_agent.model_input.contracts import (
    ModelInputScopeKind,
    PreparedProviderInputCut,
)
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.memory.scope import CTX_USER, MemoryScopeKind
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.postgres import verified_postgres_provider


pytestmark = pytest.mark.postgres


def test_lightweight_todo_queued_root_admission_has_exact_confirmation(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = f"session:{uuid4().hex}"
    workspace_id = f"workspace:{uuid4().hex}"
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=f"host:{uuid4().hex}",
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    queue_item_id = f"queue:{uuid4().hex}"
    command_id = f"command:{uuid4().hex}"
    repository.enqueue_prompt(
        lease.guard,
        command_id=command_id,
        queue_item_id=queue_item_id,
        client_submission_id=f"submission:{uuid4().hex}",
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        permission_snapshot_id=f"permission:{uuid4().hex}",
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        content=InlineContent.from_bytes(b"queued TODO run"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="test",
        deadline_monotonic=monotonic() + 30,
    )
    candidate = repository.prepare_prompt_head_consumption(
        session_id=session_id,
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert candidate is not None and candidate.queue_item_id == queue_item_id
    before = repository.confirm_prepared_prompt_head_consumption(
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    )
    assert before.kind is QueuedRootTurnAdmissionConfirmationKind.NONE
    consumed = repository.consume_prepared_prompt_head(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    )
    assert consumed is not None
    assert consumed.kind is QueuedRootTurnAdmissionConfirmationKind.FULL
    confirmed = repository.confirm_prepared_prompt_head_consumption(
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    )
    assert confirmed == consumed
    assert confirmed.accepted is not None
    assert confirmed.accepted.turn_id == candidate.exact_turn_id


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _repository(stage2_migrated_postgres_database) -> ConversationKernelRepository:
    return ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )


class _FailingSteerRejectionRepository(ConversationKernelRepository):
    fail_event_append = False

    def _append_events(self, *args, **kwargs):
        if self.fail_event_append:
            raise RuntimeError("injected event append failure")
        return super()._append_events(*args, **kwargs)


def _start_root_turn(repository: ConversationKernelRepository, *args, **kwargs):
    """Retained Stage 2 fixture expressed through the Round 4 admission API."""

    kwargs.setdefault("permission_snapshot_id", _name("permission-snapshot"))
    kwargs.setdefault("requested_permission_mode", DEFAULT_PERMISSION_MODE)
    return repository.start_root_turn(*args, **kwargs)


def _enqueue_prompt(repository: ConversationKernelRepository, *args, **kwargs):
    if kwargs["delivery_mode"] is PromptDeliveryMode.NEW_TURN:
        kwargs.setdefault("permission_snapshot_id", _name("permission-snapshot"))
        kwargs.setdefault("requested_permission_mode", DEFAULT_PERMISSION_MODE)
    else:
        kwargs.setdefault("permission_snapshot_id", None)
        kwargs.setdefault("requested_permission_mode", None)
    return repository.enqueue_prompt(*args, **kwargs)


def _assistant_permission_fingerprint(
    repository: ConversationKernelRepository,
    guard,
    assistant_entry_id: str,
    deadline: float,
) -> str:
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        row = connection.execute(
            """
            SELECT t.permission_snapshot_fingerprint
            FROM pulsara_v3.transcript_entries AS e
            JOIN pulsara_v3.turns AS t
              ON t.session_id = e.session_id AND t.id = e.turn_id
            WHERE e.session_id = %s AND e.id = %s
            """,
            (guard.session_id, assistant_entry_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _accept_tool_attempt(repository, guard, *args, **kwargs):
    kwargs.setdefault(
        "permission_snapshot_fingerprint",
        _assistant_permission_fingerprint(
            repository,
            guard,
            kwargs["assistant_entry_id"],
            kwargs["deadline_monotonic"],
        ),
    )
    return repository.accept_tool_attempt(guard, *args, **kwargs)


def _accept_tool_interaction_decision(repository, guard, *args, **kwargs):
    kwargs.setdefault(
        "permission_snapshot_fingerprint",
        _assistant_permission_fingerprint(
            repository,
            guard,
            kwargs["assistant_entry_id"],
            kwargs["deadline_monotonic"],
        ),
    )
    return repository.accept_tool_interaction_decision(guard, *args, **kwargs)


def test_stage2_orphan_blob_gc_deletes_only_unreferenced_content_after_grace(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    store = PostgresCanonicalBlobStore(provider)
    orphan = store.publish(
        workspace_id=workspace_id,
        content=b"orphan" * 20_000,
        media_type="application/octet-stream",
        codec="binary",
        deadline_monotonic=monotonic() + 30,
    )
    referenced = store.publish(
        workspace_id=workspace_id,
        content=b"referenced" * 20_000,
        media_type="text/plain",
        codec="utf-8",
        deadline_monotonic=monotonic() + 30,
    )
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=_name("turn"),
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=referenced,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        # Test-only clock aging: production never mutates immutable blob
        # metadata.  The admin fixture temporarily disables the generic
        # runtime-write trigger in the same transaction and restores it
        # before commit.
        connection.execute("ALTER TABLE pulsara_v3.blobs DISABLE TRIGGER USER")
        connection.execute(
            """
            UPDATE pulsara_v3.blobs
            SET created_at = clock_timestamp() - interval '25 hours'
            WHERE id = ANY(%s)
            """,
            ([orphan.blob_id, referenced.blob_id],),
        )
        connection.execute("ALTER TABLE pulsara_v3.blobs ENABLE TRIGGER USER")
    deleted = store.delete_orphans(
        grace_seconds=24 * 60 * 60,
        maximum_items=128,
        deadline_monotonic=monotonic() + 30,
    )
    assert deleted == (orphan.blob_id,)
    with pytest.raises(KeyError):
        store.read_exact(
            blob_id=orphan.blob_id,
            expected_digest=orphan.digest,
            expected_size=orphan.size,
            deadline_monotonic=monotonic() + 30,
        )
    assert (
        store.read_exact(
            blob_id=referenced.blob_id,
            expected_digest=referenced.digest,
            expected_size=referenced.size,
            deadline_monotonic=monotonic() + 30,
        )
        == b"referenced" * 20_000
    )


def test_stage2_blob_chunk_exactly_joins_complete_canonical_descriptor(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    store = PostgresCanonicalBlobStore(provider)
    content = b"descriptor-bound-content"
    reference = store.publish(
        workspace_id=_name("workspace"),
        content=content,
        media_type="text/plain",
        codec="utf-8",
        deadline_monotonic=monotonic() + 30,
    )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            "UPDATE pulsara_v3.blobs SET media_type = 'application/octet-stream' "
            "WHERE id = %s",
            (reference.blob_id,),
        )
        connection.commit()
    with pytest.raises(ConversationKernelConflict):
        store.read_chunk(
            blob_id=reference.blob_id,
            expected_digest=reference.digest,
            expected_size=reference.size,
            expected_media_type=reference.media_type,
            expected_codec=reference.codec,
            offset=0,
            maximum_bytes=1024,
            deadline_monotonic=monotonic() + 30,
        )


def test_stage2_maximum_blob_is_read_as_exact_bounded_storage_ranges(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    store = PostgresCanonicalBlobStore(provider)
    content = b"0123456789abcdef" * (MAXIMUM_BLOB_BYTES // 16)
    reference = store.publish(
        workspace_id=_name("workspace"),
        content=content,
        media_type="text/plain",
        codec="utf-8",
        deadline_monotonic=monotonic() + 30,
    )
    offsets = (0, MAXIMUM_CONTENT_CHUNK_BYTES, len(content) - 97, len(content))
    for offset in offsets:
        chunk = store.read_chunk(
            blob_id=reference.blob_id,
            expected_digest=reference.digest,
            expected_size=reference.size,
            expected_media_type=reference.media_type,
            expected_codec=reference.codec,
            offset=offset,
            maximum_bytes=MAXIMUM_CONTENT_CHUNK_BYTES,
            deadline_monotonic=monotonic() + 30,
        )
        assert chunk.content == content[offset : offset + MAXIMUM_CONTENT_CHUNK_BYTES]
        assert chunk.has_more is (offset + len(chunk.content) < len(content))


def test_stage2_schema_and_descriptor_oracles_are_exact(
    stage2_migrated_postgres_database,
) -> None:
    assert len(CONVERSATION_KERNEL_RELATIONS) == 26
    assert len(set(CONVERSATION_KERNEL_RELATIONS)) == 26
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 31
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 13
    assert len(APPEND_GUARDS) == 2

    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        observed = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT c.relname
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'pulsara_v3' AND c.relkind = 'r'
                ORDER BY c.relname
                """
            ).fetchall()
        )
        assert observed == tuple(sorted(CONVERSATION_KERNEL_RELATIONS))
        assert connection.execute(
            "SELECT to_regclass('public.sessions'), to_regclass('pulsara_v3.sessions')"
        ).fetchone() == (None, "pulsara_v3.sessions")


def test_stage2_snapshot_and_history_page_are_bounded_by_final_wire_bytes(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = repository.acquire_host_writer(
        session_id=_name("session"),
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    payload = b"x" * (64 * 1024)
    for _ in range(6):
        turn_id = _name("turn")
        _start_root_turn(
            repository,
            lease.guard,
            command_id=_name("command"),
            turn_id=turn_id,
            entry_id=_name("entry"),
            context_binding_revision_id=_name("revision"),
            content=InlineContent.from_bytes(payload),
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=monotonic() + 30,
        )
        repository.interrupt_turn(
            lease.guard,
            turn_id=turn_id,
            reason="TEST_BOUNDARY",
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 30,
        )
    reader = CanonicalProtocolReader(repository.connection_provider)
    snapshot = reader.snapshot(
        session_id=lease.guard.session_id,
        maximum_entries=256,
        maximum_control_items=128,
        maximum_serialized_bytes=140_000,
        deadline_monotonic=monotonic() + 30,
    )
    assert 0 < len(snapshot.entries) < 6
    assert len(snapshot.SerializeToString(deterministic=True)) <= 140_000
    assert snapshot.HasField("older_history_cursor")
    entries, cursor, has_more = reader.history_page(
        session_id=lease.guard.session_id,
        cut_sequence=snapshot.older_history_cursor.cut_sequence,
        before_entry_sequence=snapshot.older_history_cursor.entry_sequence,
        maximum_entries=256,
        maximum_serialized_bytes=140_000,
        deadline_monotonic=monotonic() + 30,
    )
    assert entries
    assert (
        sum(len(item.SerializeToString(deterministic=True)) + 8 for item in entries)
        + 512
        <= 140_000
    )
    assert has_more == (cursor is not None)


def test_stage2_text_turn_is_canonical_and_sequences_rollback_without_gaps(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"hello"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=deadline
    )
    duplicate_block_id = _name("block")
    with pytest.raises(Exception):
        repository.commit_assistant_message(
            lease.guard,
            cut=cut,
            entry_id=_name("entry"),
            parent_content=InlineContent.from_bytes(b"bad"),
            blocks=(
                AssistantTextBlock(
                    duplicate_block_id, InlineContent.from_bytes(b"one")
                ),
                AssistantTextBlock(
                    duplicate_block_id, InlineContent.from_bytes(b"two")
                ),
            ),
            occurred_at=datetime.now(timezone.utc),
            actor_id="model:test",
            deadline_monotonic=deadline,
        )
    accepted = repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=_name("entry"),
        parent_content=InlineContent.from_bytes(b"ok"),
        blocks=(AssistantTextBlock(_name("block"), InlineContent.from_bytes(b"ok")),),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=deadline,
    )
    assert accepted.entry_sequence == 2
    assert accepted.event_sequence == 2


def test_stage2_stale_writer_cannot_mutate_after_takeover(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    first = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    second = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    assert second.guard.writer_generation == first.guard.writer_generation + 1
    with pytest.raises(StaleHostWriter):
        _start_root_turn(
            repository,
            first.guard,
            command_id=_name("command"),
            turn_id=_name("turn"),
            entry_id=_name("entry"),
            context_binding_revision_id=_name("revision"),
            content=InlineContent.from_bytes(b"stale"),
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=deadline,
        )


def test_stage2_host_takeover_rejects_pending_exact_turn_steer(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    first = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        first.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"initial"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    queue_item_id = _name("queue")
    _enqueue_prompt(
        repository,
        first.guard,
        command_id=_name("command"),
        queue_item_id=queue_item_id,
        client_submission_id=_name("client"),
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=turn_id,
        content=InlineContent.from_bytes(b"new direction"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )

    second = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    assert second.guard.writer_generation == first.guard.writer_generation + 1
    with verified_postgres_provider(
        stage2_migrated_postgres_database.runtime_dsn
    ).connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT status, terminal_reason
            FROM pulsara_v3.prompt_queue_items
            WHERE session_id = %s AND id = %s
            """,
            (session_id, queue_item_id),
        ).fetchone() == ("REJECTED", "TARGET_TURN_INTERRUPTED")
        assert connection.execute(
            """
            SELECT event_type, subject_queue_item_id
            FROM pulsara_v3.agent_events
            WHERE session_id = %s AND subject_queue_item_id = %s
            ORDER BY event_sequence
            """,
            (session_id, queue_item_id),
        ).fetchall() == [
            ("PromptQueued", queue_item_id),
            ("PromptRejected", queue_item_id),
        ]


def test_stage2_tool_message_precedes_attempt_and_job_claim_mints_second_guard(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"use tool"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=deadline
    )
    request_entry_id = _name("entry")
    call_id = _name("call")
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=request_entry_id,
        parent_content=InlineContent.from_bytes(b"tool request"),
        blocks=(
            AssistantToolCallBlock(
                block_id=_name("block"),
                tool_call_id=call_id,
                tool_name="terminal",
                arguments=freeze_json({"command": "true"}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=deadline,
    )
    attempt = _accept_tool_attempt(
        repository,
        lease.guard,
        attempt_id=_name("attempt"),
        assistant_entry_id=request_entry_id,
        tool_call_id=call_id,
        authorization_kind="policy",
        authorization_reference="trusted",
        actor_kind="runtime",
        actor_id="tool-executor",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert attempt.tool_call_id == call_id
    remote_identity = _name("process")
    remote_identity_candidate = build_prepared_tool_remote_identity_publication(
        session_id=session_id,
        attempt_id=attempt.attempt_id,
        remote_identity=remote_identity,
        occurred_at=datetime.now(timezone.utc),
        actor_id="tool-executor",
    )
    repository.publish_tool_remote_identity(
        lease.guard,
        candidate=remote_identity_candidate,
        deadline_monotonic=deadline,
    )
    # The set-once publication is idempotent for the compatible winner and
    # owns the previously missing committed occurrence.
    repository.publish_tool_remote_identity(
        lease.guard,
        candidate=remote_identity_candidate,
        deadline_monotonic=deadline,
    )
    assert (
        repository.confirm_tool_remote_identity(
            lease.guard,
            candidate=remote_identity_candidate,
            deadline_monotonic=deadline,
        )
        is ToolRemoteIdentityConfirmationKind.FULL
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT remote_identity FROM pulsara_v3.tool_execution_attempts
            WHERE session_id = %s AND id = %s
            """,
            (session_id, attempt.attempt_id),
        ).fetchone() == (remote_identity,)
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.agent_events
            WHERE session_id = %s
              AND event_type = 'ToolRemoteIdentityPublished'
              AND subject_tool_attempt_id = %s
            """,
            (session_id, attempt.attempt_id),
        ).fetchone() == (1,)
        occurrence_payload = connection.execute(
            """
            SELECT payload FROM pulsara_v3.agent_events
            WHERE session_id = %s
              AND event_type = 'ToolRemoteIdentityPublished'
              AND subject_tool_attempt_id = %s
            """,
            (session_id, attempt.attempt_id),
        ).fetchone()[0]
        assert remote_identity not in repr(occurrence_payload)
        assert occurrence_payload == {
            "remote_identity_utf8_bytes": len(remote_identity.encode("utf-8")),
            "remote_identity_digest": "sha256:"
            + sha256(remote_identity.encode("utf-8")).hexdigest(),
        }

    job_id = _name("job")
    repository.enqueue_job(
        lease.guard,
        job_id=job_id,
        handler_type="BACKGROUND_COMPACTION",
        intent_schema_version="memory_index_refresh.v1",
        intent_payload={"workspace_id": "x", "generation": 1},
        automatic_intent_key=_name("intent"),
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=45_000,
        provider_input_token_limit_per_attempt=32_000,
        provider_output_token_limit_per_attempt=2_048,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    claimed = repository.claim_due_job(
        handler_type="BACKGROUND_COMPACTION",
        claim_owner_id=_name("worker"),
        lease_seconds=15,
        deadline_monotonic=deadline,
    )
    assert claimed is not None
    assert claimed.guard.job_id == job_id
    assert claimed.guard.claim_generation == 1


@pytest.mark.parametrize("decision", ["ALLOW", "DENY"])
def test_stage2_human_tool_decision_atomically_installs_exact_effect_boundary(
    stage2_migrated_postgres_database,
    decision: str,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"use tool"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=deadline
    )
    assistant_entry_id = _name("entry")
    tool_call_id = _name("call")
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b"tool request"),
        blocks=(
            AssistantToolCallBlock(
                block_id=_name("block"),
                tool_call_id=tool_call_id,
                tool_name="terminal",
                arguments=freeze_json({"command": "true"}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=deadline,
    )
    command_id = _name("interaction-command")
    decision_id = _name("interaction-decision")
    attempt_id = _name("attempt") if decision == "ALLOW" else None
    result_id = _name("result") if decision == "DENY" else None
    result_entry_id = _name("entry") if decision == "DENY" else None
    accepted = _accept_tool_interaction_decision(
        repository,
        lease.guard,
        command_id=command_id,
        decision_id=decision_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        decision=decision,
        attempt_id=attempt_id,
        result_id=result_id,
        result_entry_id=result_entry_id,
        denial_content=(
            InlineContent.from_bytes(b"tool execution denied by user")
            if decision == "DENY"
            else None
        ),
        redacted_subject="tool:terminal",
        actor_id="attachment:test",
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert accepted.decision == decision
    # Lost response is resolved by exact command-winner confirmation, not by
    # creating a second decision or physical effect.
    confirmed = _accept_tool_interaction_decision(
        repository,
        lease.guard,
        command_id=command_id,
        decision_id=decision_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        decision=decision,
        attempt_id=attempt_id,
        result_id=result_id,
        result_entry_id=result_entry_id,
        denial_content=(
            InlineContent.from_bytes(b"tool execution denied by user")
            if decision == "DENY"
            else None
        ),
        redacted_subject="tool:terminal",
        actor_id="attachment:test",
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert confirmed == accepted
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM pulsara_v3.interaction_decisions
               WHERE session_id = %s AND id = %s),
              (SELECT count(*) FROM pulsara_v3.session_commands
               WHERE session_id = %s AND command_id = %s),
              (SELECT count(*) FROM pulsara_v3.tool_execution_attempts
               WHERE session_id = %s AND assistant_entry_id = %s
                 AND tool_call_id = %s),
              (SELECT count(*) FROM pulsara_v3.tool_results
               WHERE session_id = %s AND tool_call_entry_id = %s
                 AND tool_call_id = %s)
            """,
            (
                session_id,
                decision_id,
                session_id,
                command_id,
                session_id,
                assistant_entry_id,
                tool_call_id,
                session_id,
                assistant_entry_id,
                tool_call_id,
            ),
        ).fetchone()
        assert counts == ((1, 1, 1, 0) if decision == "ALLOW" else (1, 1, 0, 1))
        event_types = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT event_type FROM pulsara_v3.agent_events
                WHERE session_id = %s
                  AND event_type IN (
                    'InteractionDecisionAccepted', 'ToolAttemptAccepted',
                    'ToolResultAccepted'
                  )
                ORDER BY event_sequence
                """,
                (session_id,),
            ).fetchall()
        )
        assert event_types == (
            ("InteractionDecisionAccepted", "ToolAttemptAccepted")
            if decision == "ALLOW"
            else ("InteractionDecisionAccepted", "ToolResultAccepted")
        )


def test_stage2_unqualified_product_sql_cannot_resolve_a_product_relation(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    session_id = _name("session")
    repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    with psycopg.connect(stage2_migrated_postgres_database.runtime_dsn) as connection:
        assert connection.execute("SHOW search_path").fetchone() == ('"$user", public',)
        with pytest.raises(psycopg.errors.UndefinedTable):
            connection.execute(
                "SELECT count(*) FROM sessions WHERE id = %s",
                (session_id,),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.sessions WHERE id = %s",
            (session_id,),
        ).fetchone() == (1,)
    require_stage2_runtime_privilege_boundary(
        repository.connection_provider,
        deadline_monotonic=monotonic() + 30,
    )


def test_stage2_job_attempt_retry_and_terminal_event_are_finite(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    job_id = _name("job")
    repository.enqueue_job(
        lease.guard,
        job_id=job_id,
        handler_type="BACKGROUND_COMPACTION",
        intent_schema_version="memory_governance.v1",
        intent_payload={"candidate_id": _name("candidate")},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=45_000,
        provider_input_token_limit_per_attempt=32_000,
        provider_output_token_limit_per_attempt=2_048,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    first = repository.claim_due_job(
        handler_type="BACKGROUND_COMPACTION",
        claim_owner_id=_name("worker"),
        lease_seconds=15,
        deadline_monotonic=deadline,
    )
    assert first is not None
    repository.mark_job_provider_call_started(
        first.guard,
        input_tokens=100,
        requested_output_tokens=50,
        deadline_monotonic=deadline,
    )
    with pytest.raises(Exception):
        repository.mark_job_provider_call_started(
            first.guard,
            input_tokens=100,
            requested_output_tokens=50,
            deadline_monotonic=deadline,
        )
    settlement = repository.settle_job_attempt(
        first.guard,
        terminal_status="FAILED",
        result_payload=None,
        error_code="PROVIDER_UNAVAILABLE",
        retryable=True,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert settlement.aggregate_status == "PENDING"
    assert settlement.retry_scheduled
    # The retry due is durable and deterministic.  A worker cannot consume the
    # next attempt early merely because it polls aggressively.
    assert (
        repository.prepare_job_claim_candidate(
            handler_type="BACKGROUND_COMPACTION",
            deadline_monotonic=deadline,
        )
        is None
    )
    assert (
        repository.claim_due_job(
            handler_type="BACKGROUND_COMPACTION",
            claim_owner_id=_name("worker"),
            lease_seconds=15,
            deadline_monotonic=deadline,
        )
        is None
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.agent_events
            WHERE session_id = %s AND event_type = 'JobTerminalAccepted'
            """,
            (session_id,),
        ).fetchone() == (0,)
        connection.execute(
            "UPDATE pulsara_v3.durable_jobs SET next_eligible_at = clock_timestamp() - interval '1 second' WHERE id = %s",
            (job_id,),
        )
    terminal = None
    for ordinal in (2, 3):
        attempt = repository.claim_due_job(
            handler_type="BACKGROUND_COMPACTION",
            claim_owner_id=_name("worker"),
            lease_seconds=15,
            deadline_monotonic=deadline,
        )
        assert attempt is not None and attempt.attempt_ordinal == ordinal
        terminal = repository.settle_job_attempt(
            attempt.guard,
            terminal_status="FAILED",
            result_payload=None,
            error_code="PROVIDER_UNAVAILABLE",
            retryable=True,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=deadline,
        )
        if ordinal == 2:
            assert terminal.aggregate_status == "PENDING"
            with repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR,
                deadline_monotonic=deadline,
            ) as connection:
                connection.execute(
                    "UPDATE pulsara_v3.durable_jobs SET next_eligible_at = clock_timestamp() - interval '1 second' WHERE id = %s",
                    (job_id,),
                )
    assert terminal is not None
    assert terminal.aggregate_status == "FAILED"
    assert not terminal.retry_scheduled
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.agent_events
            WHERE session_id = %s AND event_type = 'JobTerminalAccepted'
            """,
            (session_id,),
        ).fetchone() == (1,)


def test_stage2_job_claim_ack_unknown_and_host_takeover_keep_one_attempt_owner(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    first_host = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    job_id = _name("job")
    repository.enqueue_job(
        first_host.guard,
        job_id=job_id,
        handler_type="BACKGROUND_COMPACTION",
        intent_schema_version="memory_governance.v1",
        intent_payload={"candidate_id": _name("candidate")},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=45_000,
        provider_input_token_limit_per_attempt=32_000,
        provider_output_token_limit_per_attempt=2_048,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert (
        repository.prepare_job_claim_candidate(
            handler_type="BACKGROUND_COMPACTION", deadline_monotonic=deadline
        )
        == job_id
    )
    worker_id = _name("worker")
    accepted = repository.claim_due_job(
        handler_type="BACKGROUND_COMPACTION",
        claim_owner_id=worker_id,
        lease_seconds=15,
        expected_job_id=job_id,
        deadline_monotonic=deadline,
    )
    assert accepted is not None

    # Simulate a lost commit ACK: the same worker exact-confirms the installed
    # guard; a different worker cannot acquire or confirm a second owner.
    confirmed = repository.confirm_active_job_claim(
        job_id=job_id,
        handler_type="BACKGROUND_COMPACTION",
        claim_owner_id=worker_id,
        deadline_monotonic=deadline,
    )
    assert confirmed is not None
    assert confirmed.guard == accepted.guard
    assert (
        repository.confirm_active_job_claim(
            job_id=job_id,
            handler_type="BACKGROUND_COMPACTION",
            claim_owner_id=_name("other-worker"),
            deadline_monotonic=deadline,
        )
        is None
    )

    second_host = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    assert second_host.guard.writer_generation == first_host.guard.writer_generation + 1
    settlement = repository.settle_job_attempt(
        confirmed.guard,
        terminal_status="SUCCEEDED",
        result_payload={"ok": True},
        error_code=None,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert settlement.aggregate_status == "SUCCEEDED"
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.durable_job_attempts WHERE job_id = %s",
            (job_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.agent_events
            WHERE session_id = %s AND event_type = 'JobAttemptAccepted'
              AND subject_job_attempt_id = %s
            """,
            (session_id, accepted.guard.attempt_id),
        ).fetchone() == (1,)


def test_stage2_job_cancel_is_set_once_and_exact_claim_owner_terminalizes_it(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    job_id = _name("job")
    repository.enqueue_job(
        lease.guard,
        job_id=job_id,
        handler_type="BACKGROUND_COMPACTION",
        intent_schema_version="memory_index_refresh.v1",
        intent_payload={"workspace_id": "workspace:test", "generation": 1},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=45_000,
        provider_input_token_limit_per_attempt=32_000,
        provider_output_token_limit_per_attempt=2_048,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert (
        repository.request_job_cancel(
            lease.guard,
            job_id=job_id,
            actor_id="human:test",
            reason="USER_CANCELLED",
            deadline_monotonic=deadline,
        )
        == "CANCEL_REQUESTED"
    )
    assert (
        repository.request_job_cancel(
            lease.guard,
            job_id=job_id,
            actor_id="human:test",
            reason="USER_CANCELLED",
            deadline_monotonic=deadline,
        )
        == "CANCEL_REQUESTED"
    )
    with pytest.raises(ConversationKernelConflict):
        repository.request_job_cancel(
            lease.guard,
            job_id=job_id,
            actor_id="human:other",
            reason="OTHER_REASON",
            deadline_monotonic=deadline,
        )
    attempt = repository.claim_due_job(
        handler_type="BACKGROUND_COMPACTION",
        claim_owner_id=_name("worker"),
        lease_seconds=15,
        expected_job_id=job_id,
        deadline_monotonic=deadline,
    )
    assert attempt is not None and attempt.cancel_requested
    settlement = repository.settle_job_attempt(
        attempt.guard,
        terminal_status="CANCELLED",
        result_payload=None,
        error_code="USER_CANCELLED",
        retryable=False,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert settlement.aggregate_status == "CANCELLED"
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            "SELECT status, terminal_reason FROM pulsara_v3.durable_jobs WHERE id = %s",
            (job_id,),
        ).fetchone() == ("CANCELLED", "USER_CANCELLED")
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.agent_events
            WHERE session_id = %s AND event_type = 'JobAttemptAccepted'
              AND subject_job_attempt_id = %s
            """,
            (session_id, attempt.guard.attempt_id),
        ).fetchone() == (1,)


def test_stage2_job_claim_and_host_cancel_share_session_first_lock_order(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    job_id = _name("job")
    repository.enqueue_job(
        lease.guard,
        job_id=job_id,
        handler_type="BACKGROUND_COMPACTION",
        intent_schema_version="memory_index_refresh.v1",
        intent_payload={"workspace_id": "workspace:test", "generation": 1},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=45_000,
        provider_input_token_limit_per_attempt=32_000,
        provider_output_token_limit_per_attempt=2_048,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    start = Barrier(2)

    def claim():
        start.wait()
        return repository.claim_due_job(
            handler_type="BACKGROUND_COMPACTION",
            claim_owner_id=_name("worker"),
            lease_seconds=15,
            expected_job_id=job_id,
            deadline_monotonic=monotonic() + 10,
        )

    def cancel():
        start.wait()
        return repository.request_job_cancel(
            lease.guard,
            job_id=job_id,
            actor_id="human:test",
            reason="USER_CANCELLED",
            deadline_monotonic=monotonic() + 10,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(claim)
        cancel_future = executor.submit(cancel)
        attempt = claim_future.result(timeout=12)
        assert cancel_future.result(timeout=12) == "CANCEL_REQUESTED"
    assert attempt is not None
    settlement = repository.settle_job_attempt(
        attempt.guard,
        terminal_status="CANCELLED",
        result_payload=None,
        error_code="USER_CANCELLED",
        retryable=False,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert settlement.aggregate_status == "CANCELLED"
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT count(*), count(DISTINCT event_sequence)
            FROM pulsara_v3.agent_events WHERE session_id = %s
            """,
            (session_id,),
        ).fetchone() == (3, 3)


def test_stage2_expired_job_reaper_rebinds_normal_claim_append_guard(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    job_id = _name("job")
    repository.enqueue_job(
        lease.guard,
        job_id=job_id,
        handler_type="BACKGROUND_COMPACTION",
        intent_schema_version="memory_index_refresh.v1",
        intent_payload={"workspace_id": "workspace:test", "generation": 1},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=45_000,
        provider_input_token_limit_per_attempt=32_000,
        provider_output_token_limit_per_attempt=2_048,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    for ordinal in (1, 2, 3):
        claimed = repository.claim_due_job(
            handler_type="BACKGROUND_COMPACTION",
            claim_owner_id=_name("worker"),
            lease_seconds=0.01,
            expected_job_id=job_id,
            deadline_monotonic=deadline,
        )
        assert claimed is not None and claimed.attempt_ordinal == ordinal
        sleep(0.03)
        assert (
            repository.claim_due_job(
                handler_type="BACKGROUND_COMPACTION",
                claim_owner_id=_name("reaper"),
                lease_seconds=15,
                expected_job_id=job_id,
                deadline_monotonic=deadline,
            )
            is None
        )
        if ordinal < 3:
            with repository.connection_provider.connection(
                lane=PostgresConnectionLane.BACKGROUND_WORK,
                deadline_monotonic=deadline,
            ) as connection:
                connection.execute(
                    """
                    UPDATE pulsara_v3.durable_jobs
                    SET next_eligible_at = clock_timestamp() - interval '1 second'
                    WHERE id = %s
                    """,
                    (job_id,),
                )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            "SELECT status, terminal_reason FROM pulsara_v3.durable_jobs WHERE id = %s",
            (job_id,),
        ).fetchone() == ("FAILED", "RETRY_EXHAUSTED")
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.agent_events
            WHERE session_id = %s AND event_type = 'JobTerminalAccepted'
              AND subject_job_id = %s
            """,
            (session_id, job_id),
        ).fetchone() == (1,)


def test_stage2_provider_request_bound_terminalizes_without_retry(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    job_id = _name("job")
    repository.enqueue_job(
        lease.guard,
        job_id=job_id,
        handler_type="BACKGROUND_COMPACTION",
        intent_schema_version="memory_governance.v1",
        intent_payload={"candidate_id": _name("candidate")},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=45_000,
        provider_input_token_limit_per_attempt=32_000,
        provider_output_token_limit_per_attempt=2_048,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    attempt = repository.claim_due_job(
        handler_type="BACKGROUND_COMPACTION",
        claim_owner_id=_name("worker"),
        lease_seconds=15,
        deadline_monotonic=deadline,
    )
    assert attempt is not None
    with pytest.raises(JobAttemptTerminalized):
        repository.mark_job_provider_call_started(
            attempt.guard,
            input_tokens=32_001,
            requested_output_tokens=2_048,
            deadline_monotonic=deadline,
        )
    assert (
        repository.prepare_job_claim_candidate(
            handler_type="BACKGROUND_COMPACTION", deadline_monotonic=deadline
        )
        is None
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            "SELECT status, terminal_reason FROM pulsara_v3.durable_jobs WHERE id = %s",
            (job_id,),
        ).fetchone() == ("FAILED", "PROVIDER_REQUEST_LIMIT_EXCEEDED")
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.agent_events
            WHERE session_id = %s AND event_type = 'JobTerminalAccepted'
              AND subject_job_id = %s
            """,
            (session_id, job_id),
        ).fetchone() == (1,)


def test_stage2_memory_refresh_exhaustion_is_stable_and_query_is_unavailable(
    stage2_migrated_postgres_database,
) -> None:
    """Round 8 successor: there is no durable refresh debt or retry job."""

    repository = _repository(stage2_migrated_postgres_database)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT to_regclass('pulsara_v3.memory_index_state')"
        ).fetchone() == (None,)
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.durable_jobs
            WHERE handler_type IN ('MEMORY_INDEX_REFRESH', 'MEMORY_GOVERNANCE',
                                   'POST_COMPACTION_MEMORY_EXTRACTION')
            """
        ).fetchone() == (0,)


def test_stage2_memory_governance_is_async_and_postgres_only(
    stage2_migrated_postgres_database,
) -> None:
    """Round 8 successor: governance owns rows but no durable execution."""

    repository = _repository(stage2_migrated_postgres_database)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        relations = {
            row[0]
            for row in connection.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema='pulsara_v3' AND table_name LIKE 'memory_%'
                """
            ).fetchall()
        }
    assert relations == {
        "memory_candidates",
        "memory_candidate_tool_result_refs",
        "memory_candidate_basis_refs",
        "memory_facts",
        "memory_relations",
        "memory_embeddings",
    }


def test_stage2_memory_lifecycle_change_has_one_canonical_occurrence(
    stage2_migrated_postgres_database,
) -> None:
    """Round 8 successor: memory lifecycle is relational, not an occurrence."""

    del stage2_migrated_postgres_database
    assert {item.event_type.value for item in COMMITTED_EVENT_DESCRIPTORS}.isdisjoint(
        {"MemoryFactAccepted", "MemoryFactLifecycleChanged", "MemoryRelationAccepted"}
    )
    assert all("memory" not in slot.lower() for slot in SUBJECT_SLOTS)


def test_stage2_postgres_memory_preserves_bounded_direct_and_two_hop_paths(
    stage2_migrated_postgres_database,
) -> None:
    """Round 8 successor: only exact rows and direct relations remain public."""

    del stage2_migrated_postgres_database
    assert "max_hops" not in PostgresMemoryQuery.search.__code__.co_varnames
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src/pulsara_agent/conversation_kernel/memory/recall.py"
    ).read_text(encoding="utf-8")
    assert "WITH RECURSIVE" not in source
    assert "max_hops" not in source


def test_stage2_prompt_queue_has_stable_fifo_and_frozen_terminal_steer_target(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    root_turn = _name("turn")
    initial = _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=root_turn,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"first"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    first_item = _name("queue")
    second_item = _name("queue")
    third_item = _name("queue")
    assert (
        _enqueue_prompt(
            repository,
            lease.guard,
            command_id=_name("command"),
            queue_item_id=first_item,
            client_submission_id=_name("client"),
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=root_turn,
            content=InlineContent.from_bytes(b"steer"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=deadline,
        )
        == 1
    )
    assert (
        _enqueue_prompt(
            repository,
            lease.guard,
            command_id=_name("command"),
            queue_item_id=second_item,
            client_submission_id=_name("client"),
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=root_turn,
            content=InlineContent.from_bytes(b"second steer"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=deadline,
        )
        == 2
    )
    assert (
        _enqueue_prompt(
            repository,
            lease.guard,
            command_id=_name("command"),
            queue_item_id=third_item,
            client_submission_id=_name("client"),
            delivery_mode=PromptDeliveryMode.NEW_TURN,
            target_turn_id=None,
            content=InlineContent.from_bytes(b"next"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=deadline,
        )
        == 3
    )
    assert initial.turn_id == root_turn
    assert (
        repository.prepare_prompt_head_consumption(
            session_id=session_id,
            occurred_at=datetime.now(timezone.utc),
            actor_id="runtime",
            deadline_monotonic=deadline,
        )
        is None
    )
    assert repository.interrupt_turn(
        lease.guard,
        turn_id=root_turn,
        reason="TEST_TARGET_TERMINAL",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=deadline,
    )
    # One coalesced wake rejects the entire consecutive stale-steer prefix,
    # then consumes the next global NEW_TURN head in the same call.
    candidate = repository.prepare_prompt_head_consumption(
        session_id=session_id,
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime",
        deadline_monotonic=deadline,
    )
    assert candidate is not None
    consumed = repository.consume_prepared_prompt_head(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=deadline,
    )
    assert consumed is not None
    assert consumed.kind is QueuedRootTurnAdmissionConfirmationKind.FULL
    assert consumed.accepted is not None
    assert consumed.accepted.turn_id == candidate.exact_turn_id
    with verified_postgres_provider(
        stage2_migrated_postgres_database.runtime_dsn
    ).connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT id, status, terminal_reason
            FROM pulsara_v3.prompt_queue_items
            WHERE session_id = %s ORDER BY queue_sequence
            """,
            (session_id,),
        ).fetchall() == [
            (first_item, "REJECTED", "TARGET_TURN_TERMINAL"),
            (second_item, "REJECTED", "TARGET_TURN_TERMINAL"),
            (third_item, "CONSUMED", "CONSUMED"),
        ]


def test_round3_1_future_new_turn_lane_does_not_block_active_steer_cut(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    root_turn = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=root_turn,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"initial"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    new_item = _name("queue")
    steer_item = _name("queue")
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command"),
        queue_item_id=new_item,
        client_submission_id=_name("client"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        content=InlineContent.from_bytes(b"next"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command"),
        queue_item_id=steer_item,
        client_submission_id=_name("client"),
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=root_turn,
        content=InlineContent.from_bytes(b"steer"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    steers = repository.read_pending_prompt_steer_facts(
        session_id=session_id,
        target_turn_id=root_turn,
        deadline_monotonic=deadline,
    )
    assert tuple(item.queue_item_id for item in steers) == (steer_item,)
    assert (
        repository.pending_prompt_head_mode(
            session_id=session_id, deadline_monotonic=deadline
        )
        is PromptDeliveryMode.NEW_TURN
    )
    assert steers[0].exact_target_turn_id == root_turn


def test_round3_1_prompt_ingress_confirmation_is_semantically_exact_and_stable(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    root_turn = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=root_turn,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"initial"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    command_id = _name("command-steer")
    queue_item_id = _name("queue-steer")
    client_submission_id = _name("submission")
    content = b"exact steer"
    candidate = build_prompt_ingress_command(
        session_id=session_id,
        command_id=command_id,
        queue_item_id=queue_item_id,
        client_submission_id=client_submission_id,
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=root_turn,
        permission_snapshot_id=None,
        requested_permission_mode=None,
        content_utf8=content,
    )
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=command_id,
        queue_item_id=queue_item_id,
        client_submission_id=client_submission_id,
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=root_turn,
        content=InlineContent.from_bytes(content),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    assert (
        repository.confirm_prompt_ingress(
            candidate=candidate, deadline_monotonic=deadline
        ).kind
        is PromptIngressConfirmationKind.FULL_COMPATIBLE
    )

    conflicting = build_prompt_ingress_command(
        session_id=session_id,
        command_id=command_id,
        queue_item_id=queue_item_id,
        client_submission_id=client_submission_id,
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=root_turn,
        permission_snapshot_id=None,
        requested_permission_mode=None,
        content_utf8=b"different steer",
    )
    assert (
        repository.confirm_prompt_ingress(
            candidate=conflicting, deadline_monotonic=deadline
        ).kind
        is PromptIngressConfirmationKind.CONFLICT
    )

    repository.interrupt_turn(
        lease.guard,
        turn_id=root_turn,
        reason="TEST_TERMINAL_AFTER_ACK_LOSS",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=deadline,
    )
    assert (
        repository.confirm_prompt_ingress(
            candidate=candidate, deadline_monotonic=deadline
        ).kind
        is PromptIngressConfirmationKind.FULL_COMPATIBLE
    )


def test_round3_1_steer_consumption_ack_confirmation_is_exact(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    revision_id = _name("revision")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=revision_id,
        content=InlineContent.from_bytes(b"initial"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command-steer"),
        queue_item_id=_name("queue-steer"),
        client_submission_id=_name("submission"),
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=turn_id,
        content=InlineContent.from_bytes(b"exact steer"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    fact = repository.read_pending_prompt_steer_facts(
        session_id=session_id,
        target_turn_id=turn_id,
        deadline_monotonic=deadline,
    )[0]
    owner = HostProviderInputContinuityOwner(session_id=session_id)
    planning = owner.freeze_planning_input(
        scope=ProviderInputContinuityScope(
            session_id=session_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        ),
        canonical_frontier=ProcessLocalCanonicalFrontier(
            latest_context_binding_revision_id=revision_id,
            context_base_semantic_identity=FULL_HISTORY_CONTEXT_BASE_IDENTITY,
            through_sequence=1,
            ordered_item_fingerprints=("sha256:" + "1" * 64,),
        ),
        dispatch_anchor=NoNewTriggerAnchor(None),
    )
    occurred_at = datetime.now(timezone.utc)
    canonical_base_fence = build_steer_canonical_base_fence(
        CanonicalProviderInputReader(provider).read_frozen_compile_snapshot(
            PreparedProviderInputCut(
                session_id=session_id,
                turn_id=turn_id,
                context_binding_revision_id=revision_id,
                provider_input_through_sequence=1,
            ),
            deadline_monotonic=deadline,
        )
    )
    candidate = build_steer_consumption_candidate(
        fact=fact,
        body_utf8=b"exact steer",
        expected_entry_sequence=2,
        predecessor=planning,
        canonical_base_fence=canonical_base_fence,
        occurred_at=occurred_at,
        actor_id=lease.guard.writer_owner_id,
    )
    assert (
        repository.confirm_prepared_prompt_steer(
            candidate=candidate, deadline_monotonic=deadline
        ).kind
        is SteerConsumptionConfirmationKind.NONE
    )
    accepted = repository.consume_prepared_prompt_steer(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=deadline,
    )
    assert accepted.user_steer_event_sequence == (
        accepted.prompt_consumed_event_sequence + 1
    )
    confirmation = repository.confirm_prepared_prompt_steer(
        candidate=candidate, deadline_monotonic=deadline
    )
    assert confirmation.kind is SteerConsumptionConfirmationKind.FULL
    assert confirmation.accepted == accepted

    conflicting = build_steer_consumption_candidate(
        fact=fact,
        body_utf8=b"exact steer",
        expected_entry_sequence=2,
        predecessor=planning,
        canonical_base_fence=canonical_base_fence,
        occurred_at=occurred_at + timedelta(microseconds=1),
        actor_id=lease.guard.writer_owner_id,
    )
    assert (
        repository.confirm_prepared_prompt_steer(
            candidate=conflicting, deadline_monotonic=deadline
        ).kind
        is SteerConsumptionConfirmationKind.CONFLICT
    )


def test_round3_1_steer_consume_rejects_canonical_base_drift_without_mutation(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    revision_id = _name("revision")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=revision_id,
        content=InlineContent.from_bytes(b"initial"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command-steer"),
        queue_item_id=_name("queue-steer"),
        client_submission_id=_name("submission"),
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=turn_id,
        content=InlineContent.from_bytes(b"must remain pending"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    fact = repository.read_pending_prompt_steer_facts(
        session_id=session_id,
        target_turn_id=turn_id,
        deadline_monotonic=deadline,
    )[0]
    base = CanonicalProviderInputReader(provider).read_frozen_compile_snapshot(
        PreparedProviderInputCut(
            session_id=session_id,
            turn_id=turn_id,
            context_binding_revision_id=revision_id,
            provider_input_through_sequence=1,
        ),
        deadline_monotonic=deadline,
    )
    planning = HostProviderInputContinuityOwner(
        session_id=session_id
    ).freeze_planning_input(
        scope=ProviderInputContinuityScope(
            session_id=session_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        ),
        canonical_frontier=ProcessLocalCanonicalFrontier(
            latest_context_binding_revision_id=revision_id,
            context_base_semantic_identity=FULL_HISTORY_CONTEXT_BASE_IDENTITY,
            through_sequence=1,
            ordered_item_fingerprints=(base.canonical_input.snapshot_fingerprint,),
        ),
        dispatch_anchor=NoNewTriggerAnchor(None),
    )
    candidate = build_steer_consumption_candidate(
        fact=fact,
        body_utf8=b"must remain pending",
        expected_entry_sequence=2,
        predecessor=planning,
        canonical_base_fence=build_steer_canonical_base_fence(base),
        occurred_at=datetime.now(timezone.utc),
        actor_id=lease.guard.writer_owner_id,
    )

    repository.adopt_context_snapshot(
        lease.guard,
        turn_id=turn_id,
        snapshot_id=_name("snapshot"),
        context_binding_revision_id=_name("replacement-revision"),
        source_through_sequence=0,
        source_digest="sha256:" + sha256(b"summary").hexdigest(),
        compiler_contract="test:compiler",
        prompt_contract="test:prompt",
        model_contract="test:model",
        content=InlineContent.from_bytes(b"summary"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=deadline,
    )

    with pytest.raises(ConversationKernelConflict, match="control base drifted"):
        repository.consume_prepared_prompt_steer(
            lease.guard,
            candidate=candidate,
            deadline_monotonic=deadline,
        )
    assert (
        repository.confirm_prepared_prompt_steer(
            candidate=candidate, deadline_monotonic=deadline
        ).kind
        is SteerConsumptionConfirmationKind.NONE
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            "SELECT status, consumed_entry_id FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s AND id = %s",
            (session_id, fact.queue_item_id),
        ).fetchone() == ("PENDING", None)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind = 'USER_STEER'",
            (session_id,),
        ).fetchone() == (0,)


def test_round3_1_resource_rejection_is_atomic_and_exactly_confirmable(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _FailingSteerRejectionRepository(provider)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"initial"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command-steer"),
        queue_item_id=_name("queue-steer"),
        client_submission_id=_name("submission"),
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=turn_id,
        content=InlineContent.from_bytes(b"too large for the fixed prefix"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    fact = repository.read_pending_prompt_steer_facts(
        session_id=session_id,
        target_turn_id=turn_id,
        deadline_monotonic=deadline,
    )[0]
    candidate = build_steer_resource_rejection(
        source_plan_fingerprint="sha256:" + "1" * 64,
        fact=fact,
        occurred_at=datetime.now(timezone.utc),
        actor_id=lease.guard.writer_owner_id,
    )

    repository.fail_event_append = True
    with pytest.raises(RuntimeError, match="injected event append failure"):
        repository.reject_prepared_prompt_steer_resource_exhaustion(
            lease.guard,
            candidate=candidate,
            deadline_monotonic=deadline,
        )
    repository.fail_event_append = False
    assert (
        repository.confirm_prepared_prompt_steer_resource_rejection(
            session_id=session_id,
            candidate=candidate,
            deadline_monotonic=deadline,
        ).kind
        is SteerResourceRejectionConfirmationKind.NONE
    )

    repository.reject_prepared_prompt_steer_resource_exhaustion(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=deadline,
    )
    assert (
        repository.confirm_prepared_prompt_steer_resource_rejection(
            session_id=session_id,
            candidate=candidate,
            deadline_monotonic=deadline,
        ).kind
        is SteerResourceRejectionConfirmationKind.FULL
    )


def test_stage2_prompt_cancel_is_single_terminal_cas(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    item_id = _name("queue")
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command"),
        queue_item_id=item_id,
        client_submission_id=_name("submission"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        content=InlineContent.from_bytes(b"cancel me"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    assert (
        repository.cancel_prompt(
            lease.guard,
            queue_item_id=item_id,
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=deadline,
        )
        == "CANCELLED"
    )
    assert (
        repository.cancel_prompt(
            lease.guard,
            queue_item_id=item_id,
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=deadline,
        )
        == "CANCELLED"
    )
    assert not repository.has_pending_prompt(
        session_id=session_id, deadline_monotonic=deadline
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.agent_events
            WHERE session_id = %s AND event_type = 'PromptCancelled'
            """,
            (session_id,),
        ).fetchone() == (1,)


def test_stage2_memory_candidate_and_tool_result_are_one_transaction(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"remember this"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=deadline
    )
    assistant_entry_id = _name("entry")
    first_call = _name("call")
    second_call = _name("call")
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b"memory proposals"),
        blocks=(
            AssistantToolCallBlock(
                _name("block"),
                first_call,
                "remember_claim",
                freeze_json({"statement": "a"}),
            ),
            AssistantToolCallBlock(
                _name("block"),
                second_call,
                "remember_claim",
                freeze_json({"statement": "b"}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=deadline,
    )
    first_attempt = _accept_tool_attempt(
        repository,
        lease.guard,
        attempt_id=_name("attempt"),
        assistant_entry_id=assistant_entry_id,
        tool_call_id=first_call,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="tool",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    candidate_id = _name("candidate")
    memory_candidate = prepare_memory_candidate(
        candidate_id=candidate_id,
        memory_domain_id="u_local",
        origin_workspace_id=workspace_id,
        origin_session_id=session_id,
        producer_kind=MemoryProducerKind.MAIN_AGENT_REMEMBER,
        producer_entry_id=assistant_entry_id,
        producer_tool_call_id=first_call,
        proposal=FrozenMemoryProposal(
            statement="a",
            scope_kind=MemoryScopeKind.USER,
            scope_id=CTX_USER,
            kind_hint=MemoryKindHint.FACT,
        ),
    )
    first_result_entry_id = _name("entry")
    first_candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_name("result"),
        result_entry_id=first_result_entry_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=first_call,
        attempt_id=first_attempt.attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(b"proposed"),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
        artifact_id=None,
        artifact_blob_descriptor=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None,
        artifact_unavailability_reason=None,
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
        actor_id="remember_claim",
        memory_candidate=memory_candidate,
    )
    repository.accept_tool_result(
        lease.guard,
        candidate=first_candidate,
        deadline_monotonic=deadline,
    )
    second_attempt = _accept_tool_attempt(
        repository,
        lease.guard,
        attempt_id=_name("attempt"),
        assistant_entry_id=assistant_entry_id,
        tool_call_id=second_call,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="tool",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    rolled_back_candidate = candidate_id
    conflicting_memory_candidate = prepare_memory_candidate(
        candidate_id=rolled_back_candidate,
        memory_domain_id="u_local",
        origin_workspace_id=workspace_id,
        origin_session_id=session_id,
        producer_kind=MemoryProducerKind.MAIN_AGENT_REMEMBER,
        producer_entry_id=assistant_entry_id,
        producer_tool_call_id=second_call,
        proposal=FrozenMemoryProposal(
            statement="b",
            scope_kind=MemoryScopeKind.USER,
            scope_id=CTX_USER,
            kind_hint=MemoryKindHint.FACT,
        ),
    )
    rolled_back_result = _name("result")
    rollback_candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=rolled_back_result,
        result_entry_id=_name("entry"),
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=second_call,
        attempt_id=second_attempt.attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(b"must rollback"),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
        artifact_id=None,
        artifact_blob_descriptor=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None,
        artifact_unavailability_reason=None,
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
        actor_id="remember_claim",
        memory_candidate=conflicting_memory_candidate,
    )
    with pytest.raises(Exception):
        repository.accept_tool_result(
            lease.guard,
            candidate=rollback_candidate,
            deadline_monotonic=deadline,
        )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE id = %s",
            (rolled_back_result,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.memory_candidates WHERE id = %s",
            (candidate_id,),
        ).fetchone() == (1,)
