from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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
from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore
from pulsara_agent.conversation_kernel.memory import (
    MemoryIndexCoordinator,
    PostgresMemoryQuery,
)
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    JobAttemptTerminalized,
    StaleHostWriter,
)
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


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _repository(stage2_migrated_postgres_database) -> ConversationKernelRepository:
    return ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )


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
    repository.start_root_turn(
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


def test_stage2_schema_and_descriptor_oracles_are_exact(
    stage2_migrated_postgres_database,
) -> None:
    assert len(CONVERSATION_KERNEL_RELATIONS) == 24
    assert len(set(CONVERSATION_KERNEL_RELATIONS)) == 24
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 26
    assert len(LIVE_EVENT_TYPES) == 23
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
        ).fetchone() == ("sessions", "pulsara_v3.sessions")


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
        repository.start_root_turn(
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
    repository.start_root_turn(
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
        repository.start_root_turn(
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
    repository.start_root_turn(
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
    repository.enqueue_prompt(
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
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    repository.start_root_turn(
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
                arguments={"command": "true"},
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=deadline,
    )
    attempt = repository.accept_tool_attempt(
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
    repository.publish_tool_remote_identity(
        lease.guard,
        attempt_id=attempt.attempt_id,
        remote_identity=remote_identity,
        occurred_at=datetime.now(timezone.utc),
        actor_id="tool-executor",
        deadline_monotonic=deadline,
    )
    # The set-once publication is idempotent for the compatible winner and
    # owns the previously missing committed occurrence.
    repository.publish_tool_remote_identity(
        lease.guard,
        attempt_id=attempt.attempt_id,
        remote_identity=remote_identity,
        occurred_at=datetime.now(timezone.utc),
        actor_id="tool-executor",
        deadline_monotonic=deadline,
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

    job_id = _name("job")
    repository.enqueue_job(
        lease.guard,
        job_id=job_id,
        handler_type="MEMORY_INDEX_REFRESH",
        intent_schema_version="memory_index_refresh.v1",
        intent_payload={"workspace_id": "x", "generation": 1},
        automatic_intent_key=_name("intent"),
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=30_000,
        provider_input_token_limit_per_attempt=None,
        provider_output_token_limit_per_attempt=None,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    claimed = repository.claim_due_job(
        handler_type="MEMORY_INDEX_REFRESH",
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
    repository.start_root_turn(
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
                arguments={"command": "true"},
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
    accepted = repository.accept_tool_interaction_decision(
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
    confirmed = repository.accept_tool_interaction_decision(
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


def test_stage2_unqualified_product_sql_resolves_only_legacy_public_table(
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
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
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
        handler_type="MEMORY_GOVERNANCE",
        intent_schema_version="memory_governance.v1",
        intent_payload={"candidate_id": _name("candidate")},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=30_000,
        provider_input_token_limit_per_attempt=16_000,
        provider_output_token_limit_per_attempt=1_024,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    first = repository.claim_due_job(
        handler_type="MEMORY_GOVERNANCE",
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
            handler_type="MEMORY_GOVERNANCE",
            deadline_monotonic=deadline,
        )
        is None
    )
    assert (
        repository.claim_due_job(
            handler_type="MEMORY_GOVERNANCE",
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
            handler_type="MEMORY_GOVERNANCE",
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
        handler_type="MEMORY_GOVERNANCE",
        intent_schema_version="memory_governance.v1",
        intent_payload={"candidate_id": _name("candidate")},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=30_000,
        provider_input_token_limit_per_attempt=16_000,
        provider_output_token_limit_per_attempt=1_024,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert (
        repository.prepare_job_claim_candidate(
            handler_type="MEMORY_GOVERNANCE", deadline_monotonic=deadline
        )
        == job_id
    )
    worker_id = _name("worker")
    accepted = repository.claim_due_job(
        handler_type="MEMORY_GOVERNANCE",
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
        handler_type="MEMORY_GOVERNANCE",
        claim_owner_id=worker_id,
        deadline_monotonic=deadline,
    )
    assert confirmed is not None
    assert confirmed.guard == accepted.guard
    assert (
        repository.confirm_active_job_claim(
            job_id=job_id,
            handler_type="MEMORY_GOVERNANCE",
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
        handler_type="MEMORY_INDEX_REFRESH",
        intent_schema_version="memory_index_refresh.v1",
        intent_payload={"workspace_id": "workspace:test", "generation": 1},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=30_000,
        provider_input_token_limit_per_attempt=None,
        provider_output_token_limit_per_attempt=None,
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
        handler_type="MEMORY_INDEX_REFRESH",
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
        handler_type="MEMORY_INDEX_REFRESH",
        intent_schema_version="memory_index_refresh.v1",
        intent_payload={"workspace_id": "workspace:test", "generation": 1},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=30_000,
        provider_input_token_limit_per_attempt=None,
        provider_output_token_limit_per_attempt=None,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    start = Barrier(2)

    def claim():
        start.wait()
        return repository.claim_due_job(
            handler_type="MEMORY_INDEX_REFRESH",
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
        handler_type="MEMORY_INDEX_REFRESH",
        intent_schema_version="memory_index_refresh.v1",
        intent_payload={"workspace_id": "workspace:test", "generation": 1},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=30_000,
        provider_input_token_limit_per_attempt=None,
        provider_output_token_limit_per_attempt=None,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    for ordinal in (1, 2, 3):
        claimed = repository.claim_due_job(
            handler_type="MEMORY_INDEX_REFRESH",
            claim_owner_id=_name("worker"),
            lease_seconds=0.01,
            expected_job_id=job_id,
            deadline_monotonic=deadline,
        )
        assert claimed is not None and claimed.attempt_ordinal == ordinal
        sleep(0.03)
        assert (
            repository.claim_due_job(
                handler_type="MEMORY_INDEX_REFRESH",
                claim_owner_id=_name("reaper"),
                lease_seconds=15,
                expected_job_id=job_id,
                deadline_monotonic=deadline,
            )
            is None
        )
        if ordinal < 3:
            with repository.connection_provider.connection(
                lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
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
        handler_type="MEMORY_GOVERNANCE",
        intent_schema_version="memory_governance.v1",
        intent_payload={"candidate_id": _name("candidate")},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=30_000,
        provider_input_token_limit_per_attempt=16_000,
        provider_output_token_limit_per_attempt=1_024,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    attempt = repository.claim_due_job(
        handler_type="MEMORY_GOVERNANCE",
        claim_owner_id=_name("worker"),
        lease_seconds=15,
        deadline_monotonic=deadline,
    )
    assert attempt is not None
    with pytest.raises(JobAttemptTerminalized):
        repository.mark_job_provider_call_started(
            attempt.guard,
            input_tokens=16_001,
            requested_output_tokens=1_024,
            deadline_monotonic=deadline,
        )
    assert (
        repository.prepare_job_claim_candidate(
            handler_type="MEMORY_GOVERNANCE", deadline_monotonic=deadline
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
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    workspace_id = _name("workspace")
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.MEMORY_MAINTENANCE,
        deadline_monotonic=deadline,
    ) as connection:
        connection.execute(
            """
            INSERT INTO pulsara_v3.memory_index_state (
                workspace_id, channel, desired_generation,
                desired_handler_contract_id, desired_handler_contract_version,
                applied_generation, applied_handler_contract_id,
                applied_handler_contract_version
            ) VALUES
                (%s, 'FTS', 1, 'postgres-memory-index', 1,
                 0, 'postgres-memory-index', 1),
                (%s, 'VECTOR', 0, 'postgres-memory-index', 1,
                 0, 'postgres-memory-index', 1)
            """,
            (workspace_id, workspace_id),
        )
    index = MemoryIndexCoordinator(repository)
    query = PostgresMemoryQuery(repository.connection_provider)
    assert index.scan_lost_wakes(deadline_monotonic=deadline) == 1
    stale = query.search(
        workspace_id=workspace_id,
        query="anything",
        deadline_monotonic=deadline,
    )
    assert stale.disposition.value == "PARTIAL_STALE"

    terminal = None
    for ordinal in (1, 2, 3):
        attempt = repository.claim_due_job(
            handler_type="MEMORY_INDEX_REFRESH",
            claim_owner_id=_name("indexer"),
            lease_seconds=15,
            deadline_monotonic=deadline,
        )
        assert attempt is not None and attempt.attempt_ordinal == ordinal
        terminal = repository.settle_job_attempt(
            attempt.guard,
            terminal_status="FAILED",
            result_payload=None,
            error_code="INDEX_WRITE_FAILED",
            retryable=True,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=deadline,
        )
        if terminal.retry_scheduled:
            with repository.connection_provider.connection(
                lane=PostgresConnectionLane.MEMORY_MAINTENANCE,
                deadline_monotonic=deadline,
            ) as connection:
                connection.execute(
                    """
                    UPDATE pulsara_v3.durable_jobs
                    SET next_eligible_at = clock_timestamp() - interval '1 second'
                    WHERE id = %s
                    """,
                    (attempt.guard.job_id,),
                )
    assert terminal is not None and terminal.aggregate_status == "FAILED"
    assert index.scan_lost_wakes(deadline_monotonic=deadline) == 0
    assert index.scan_lost_wakes(deadline_monotonic=deadline) == 0
    unavailable = query.search(
        workspace_id=workspace_id,
        query="anything",
        deadline_monotonic=deadline,
    )
    assert unavailable.disposition.value == "PARTIAL_UNAVAILABLE"
    assert (
        next(item for item in unavailable.channels if item.channel == "FTS").reason
        == "INDEX_REFRESH_EXHAUSTED"
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.durable_jobs
            WHERE handler_type = 'MEMORY_INDEX_REFRESH'
              AND workspace_id = %s
            """,
            (workspace_id,),
        ).fetchone() == (1,)


def test_stage2_memory_governance_is_async_and_postgres_only(
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
    source_entry_id = _name("entry")
    repository.start_root_turn(
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=source_entry_id,
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"remember green apples"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    candidate_id = _name("candidate")
    governance_job_id = _name("job")
    repository.accept_memory_candidate_and_governance_job(
        lease.guard,
        candidate_id=candidate_id,
        source_entry_id=source_entry_id,
        proposal_kind="FACT",
        proposal_payload={"text": "green apples are preferred"},
        governance_job_id=governance_job_id,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.memory_facts WHERE workspace_id = %s",
            (workspace_id,),
        ).fetchone() == (0,)
    claimed = repository.claim_due_job(
        handler_type="MEMORY_GOVERNANCE",
        claim_owner_id=_name("worker"),
        lease_seconds=15,
        deadline_monotonic=deadline,
    )
    assert claimed is not None and claimed.guard.job_id == governance_job_id
    fact_id = _name("memory")
    accepted = repository.accept_memory_governance(
        claimed.guard,
        candidate_id=candidate_id,
        decision_id=_name("decision"),
        decision="SUBMIT",
        lineage_payload={"source": "test"},
        fact_id=fact_id,
        fact_kind="FACT",
        fact_payload={"text": "green apples are preferred"},
        relations=(),
        index_handler_contract_id="postgres-memory-index",
        index_handler_contract_version=1,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert accepted.fact_id == fact_id
    index = MemoryIndexCoordinator(repository)
    assert index.scan_lost_wakes(deadline_monotonic=deadline) == 2
    for _ in range(2):
        refresh = repository.claim_due_job(
            handler_type="MEMORY_INDEX_REFRESH",
            claim_owner_id=_name("indexer"),
            lease_seconds=15,
            deadline_monotonic=deadline,
        )
        assert refresh is not None
        if refresh.intent_payload["channel"] == "FTS":
            assert (
                index.apply_fts_refresh(refresh.guard, deadline_monotonic=deadline) == 1
            )
        else:
            source = repository.snapshot_memory_vector_source(
                refresh.guard,
                handler_contract_id="postgres-memory-index",
                handler_contract_version=1,
                deadline_monotonic=deadline,
            )
            assert (
                repository.apply_vector_memory_index(
                    refresh.guard,
                    source=source,
                    embeddings=((0.25, 0.75),),
                    deadline_monotonic=deadline,
                )
                == 1
            )
    result = PostgresMemoryQuery(repository.connection_provider).search(
        workspace_id=workspace_id,
        query="green apples",
        query_embedding=(0.25, 0.75),
        max_hops=2,
        deadline_monotonic=deadline,
    )
    assert result.disposition.value == "COMPLETE"
    assert [item.fact_id for item in result.facts] == [fact_id]


def test_stage2_memory_lifecycle_change_has_one_canonical_occurrence(
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
    source_entry_id = _name("entry")
    repository.start_root_turn(
        lease.guard,
        command_id=_name("command"),
        turn_id=_name("turn"),
        entry_id=source_entry_id,
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"memory source"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )

    first_candidate = _name("candidate")
    first_job = _name("job")
    repository.accept_memory_candidate_and_governance_job(
        lease.guard,
        candidate_id=first_candidate,
        source_entry_id=source_entry_id,
        proposal_kind="FACT",
        proposal_payload={"text": "old fact"},
        governance_job_id=first_job,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    first_attempt = repository.claim_due_job(
        handler_type="MEMORY_GOVERNANCE",
        claim_owner_id=_name("governor"),
        lease_seconds=15,
        expected_job_id=first_job,
        deadline_monotonic=deadline,
    )
    assert first_attempt is not None
    old_fact = _name("memory")
    repository.accept_memory_governance(
        first_attempt.guard,
        candidate_id=first_candidate,
        decision_id=_name("decision"),
        decision="SUBMIT",
        lineage_payload={"source": "test"},
        fact_id=old_fact,
        fact_kind="FACT",
        fact_payload={"text": "old fact"},
        relations=(),
        index_handler_contract_id="postgres-memory-index",
        index_handler_contract_version=1,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )

    second_candidate = _name("candidate")
    second_job = _name("job")
    repository.accept_memory_candidate_and_governance_job(
        lease.guard,
        candidate_id=second_candidate,
        source_entry_id=source_entry_id,
        proposal_kind="CORRECTION",
        proposal_payload={
            "text": "new fact",
            "superseded_fact_ids": (old_fact,),
        },
        governance_job_id=second_job,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    second_attempt = repository.claim_due_job(
        handler_type="MEMORY_GOVERNANCE",
        claim_owner_id=_name("governor"),
        lease_seconds=15,
        expected_job_id=second_job,
        deadline_monotonic=deadline,
    )
    assert second_attempt is not None
    new_fact = _name("memory")
    repository.accept_memory_governance(
        second_attempt.guard,
        candidate_id=second_candidate,
        decision_id=_name("decision"),
        decision="CORRECT",
        lineage_payload={
            "source": "test",
            "superseded_fact_ids": [old_fact],
        },
        fact_id=new_fact,
        fact_kind="FACT",
        fact_payload={"text": "new fact"},
        relations=(),
        index_handler_contract_id="postgres-memory-index",
        index_handler_contract_version=1,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT id, lifecycle FROM pulsara_v3.memory_facts
            WHERE id = ANY(%s) ORDER BY id
            """,
            ([old_fact, new_fact],),
        ).fetchall() == sorted([(old_fact, "SUPERSEDED"), (new_fact, "ACTIVE")])
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.agent_events
            WHERE session_id = %s
              AND event_type = 'MemoryFactLifecycleChanged'
              AND subject_memory_fact_id = %s
            """,
            (session_id, old_fact),
        ).fetchone() == (1,)


def test_stage2_postgres_memory_preserves_bounded_direct_and_two_hop_paths(
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
    source_entry_id = _name("entry")
    repository.start_root_turn(
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=source_entry_id,
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"memory graph source"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )

    def accept_fact(label: str, relations: tuple[tuple[str, str, str], ...]) -> str:
        candidate_id = _name("candidate")
        job_id = _name("job")
        repository.accept_memory_candidate_and_governance_job(
            lease.guard,
            candidate_id=candidate_id,
            source_entry_id=source_entry_id,
            proposal_kind="FACT",
            proposal_payload={"text": label},
            governance_job_id=job_id,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=deadline,
        )
        attempt = repository.claim_due_job(
            handler_type="MEMORY_GOVERNANCE",
            claim_owner_id=_name("governor"),
            lease_seconds=15,
            expected_job_id=job_id,
            deadline_monotonic=deadline,
        )
        assert attempt is not None
        fact_id = _name("memory")
        repository.accept_memory_governance(
            attempt.guard,
            candidate_id=candidate_id,
            decision_id=_name("decision"),
            decision="SUBMIT",
            lineage_payload={"source": "two-hop-test"},
            fact_id=fact_id,
            fact_kind="FACT",
            fact_payload={"text": label},
            relations=relations,
            index_handler_contract_id="postgres-memory-index",
            index_handler_contract_version=1,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=deadline,
        )
        return fact_id

    terminal = accept_fact("terminal node", ())
    middle = accept_fact("middle node", ((_name("relation"), terminal, "NEXT"),))
    source = accept_fact("source alpha", ((_name("relation"), middle, "NEXT"),))
    index = MemoryIndexCoordinator(repository)
    assert index.scan_lost_wakes(deadline_monotonic=deadline) >= 2
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        refresh_job_ids = tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT id FROM pulsara_v3.durable_jobs
                WHERE handler_type = 'MEMORY_INDEX_REFRESH'
                  AND workspace_id = %s
                ORDER BY id
                """,
                (workspace_id,),
            ).fetchall()
        )
    assert len(refresh_job_ids) == 2
    for refresh_job_id in refresh_job_ids:
        refresh = repository.claim_due_job(
            handler_type="MEMORY_INDEX_REFRESH",
            claim_owner_id=_name("indexer"),
            lease_seconds=15,
            expected_job_id=refresh_job_id,
            deadline_monotonic=deadline,
        )
        assert refresh is not None
        if refresh.intent_payload["channel"] == "FTS":
            assert (
                index.apply_fts_refresh(refresh.guard, deadline_monotonic=deadline) == 3
            )
        else:
            vector_source = repository.snapshot_memory_vector_source(
                refresh.guard,
                handler_contract_id="postgres-memory-index",
                handler_contract_version=1,
                deadline_monotonic=deadline,
            )
            assert (
                repository.apply_vector_memory_index(
                    refresh.guard,
                    source=vector_source,
                    embeddings=((1.0, 0.0), (0.0, 1.0), (0.5, 0.5)),
                    deadline_monotonic=deadline,
                )
                == 3
            )
    result = PostgresMemoryQuery(repository.connection_provider).search(
        workspace_id=workspace_id,
        query="source alpha",
        max_hops=2,
        deadline_monotonic=deadline,
    )
    assert result.disposition.value == "COMPLETE"
    assert result.facts[0].fact_id == source
    assert [(item.target_fact_id, item.hop_count) for item in result.paths] == [
        (middle, 1),
        (terminal, 2),
    ]


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
    initial = repository.start_root_turn(
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
    assert (
        repository.enqueue_prompt(
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
        repository.enqueue_prompt(
            lease.guard,
            command_id=_name("command"),
            queue_item_id=second_item,
            client_submission_id=_name("client"),
            delivery_mode=PromptDeliveryMode.NEW_TURN,
            target_turn_id=None,
            content=InlineContent.from_bytes(b"next"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=deadline,
        )
        == 2
    )
    assert initial.turn_id == root_turn
    assert repository.interrupt_turn(
        lease.guard,
        turn_id=root_turn,
        reason="TEST_TARGET_TERMINAL",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=deadline,
    )
    # The frozen steer is rejected, never redirected to a future ROOT turn.
    assert (
        repository.consume_prompt_steer_for_turn(
            lease.guard,
            target_turn_id=root_turn,
            new_entry_id=_name("entry"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="runtime",
            deadline_monotonic=deadline,
        )
        is None
    )
    next_turn = _name("turn")
    consumed = repository.consume_prompt_head(
        lease.guard,
        new_turn_id=next_turn,
        new_entry_id=_name("entry"),
        new_context_binding_revision_id=_name("revision"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime",
        deadline_monotonic=deadline,
    )
    assert consumed is not None and consumed.turn_id == next_turn
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
            (second_item, "CONSUMED", "CONSUMED"),
        ]


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
    repository.enqueue_prompt(
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
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    repository.start_root_turn(
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
                _name("block"), first_call, "remember_claim", {"statement": "a"}
            ),
            AssistantToolCallBlock(
                _name("block"), second_call, "remember_claim", {"statement": "b"}
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=deadline,
    )
    first_attempt = repository.accept_tool_attempt(
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
    job_id = _name("job")
    repository.accept_tool_result(
        lease.guard,
        result_id=_name("result"),
        result_entry_id=_name("entry"),
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=first_call,
        attempt_id=first_attempt.attempt_id,
        result_state="SUCCESS",
        content=InlineContent.from_bytes(b"proposed"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="remember_claim",
        memory_candidate_id=candidate_id,
        memory_proposal_kind="FACT",
        memory_proposal_payload={"statement": "a"},
        memory_governance_job_id=job_id,
        deadline_monotonic=deadline,
    )
    second_attempt = repository.accept_tool_attempt(
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
    rolled_back_candidate = _name("candidate")
    rolled_back_result = _name("result")
    with pytest.raises(Exception):
        repository.accept_tool_result(
            lease.guard,
            result_id=rolled_back_result,
            result_entry_id=_name("entry"),
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=second_call,
            attempt_id=second_attempt.attempt_id,
            result_state="SUCCESS",
            content=InlineContent.from_bytes(b"must rollback"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="remember_claim",
            memory_candidate_id=rolled_back_candidate,
            memory_proposal_kind="FACT",
            memory_proposal_payload={"statement": "b"},
            memory_governance_job_id=job_id,
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
            (rolled_back_candidate,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.memory_candidates WHERE id = %s",
            (candidate_id,),
        ).fetchone() == (1,)
