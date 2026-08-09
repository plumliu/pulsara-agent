from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from time import monotonic
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.event import EventContext, RunStartEvent
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.ports.terminal_application import (
    SubmitPromptRequest,
    TerminalCommandBinding,
    TerminalCommandOutcome,
    terminal_command_outcome_fingerprint,
)
from pulsara_agent.runtime.terminal_application.command_receipt import (
    build_terminal_command_receipt_storage,
)
from pulsara_agent.runtime.terminal_application.prompt_queue import (
    PromptQueueSubmitRequest,
)
from pulsara_agent.runtime.terminal_application.services import (
    TerminalCommandOwner,
    terminal_request_semantic_fingerprint,
)
from tests.conftest import (
    persist_test_run_transcript_seed,
    run_start_permission_fields,
)
from tests.support.postgres import verified_postgres_provider
from tests.support.postgres_database import MigratedPostgresTestDatabase
from tests.support.runtime_session import (
    aclose_runtime_session_for_test,
    in_memory_runtime_session,
)


def _event_log(
    database: MigratedPostgresTestDatabase,
    *,
    runtime_session_id: str,
    workspace_root: Path,
) -> PostgresEventLog:
    event_log = PostgresEventLog(
        connection_provider=verified_postgres_provider(database.runtime_dsn),
        runtime_session_id=runtime_session_id,
        workspace_root=workspace_root,
    )
    event_log.ensure_runtime_session_owner()
    return event_log


async def _drain_runtime_for_close(runtime) -> None:
    await aclose_runtime_session_for_test(runtime, timeout_seconds=30.0)


def _command_request(
    *, runtime_session_id: str, command_id: str
) -> SubmitPromptRequest:
    binding = TerminalCommandBinding(
        client_instance_id="client:postgres-command",
        attachment_id="attachment:postgres-command",
        attachment_generation=1,
        command_id=command_id,
        runtime_session_id=runtime_session_id,
        expected_target_id="host:postgres-command",
        expected_target_generation=1,
        expected_controller_generation=1,
        request_semantic_fingerprint="placeholder",
    )
    request = SubmitPromptRequest(
        command_kind="submit_prompt",
        binding=binding,
        client_submission_id=f"submission:{command_id}",
        text="durable PostgreSQL command",
        requested_delivery_mode="auto",
        request_fingerprint="placeholder",
    )
    semantic = terminal_request_semantic_fingerprint(request)
    return replace(
        request,
        binding=replace(binding, request_semantic_fingerprint=semantic),
        request_fingerprint=semantic,
    )


def _command_outcome(
    request: SubmitPromptRequest,
    *,
    status: str,
    code: str,
    text: str,
) -> TerminalCommandOutcome:
    query_token = f"query:{request.binding.command_id}"
    return TerminalCommandOutcome(
        status=status,  # type: ignore[arg-type]
        command_id=request.binding.command_id,
        target_id=request.binding.expected_target_id,
        target_generation=request.binding.expected_target_generation,
        public_result_code=code,
        public_result_text=text,
        durable_reference_ids=(),
        query_token=query_token,
        outcome_fingerprint=terminal_command_outcome_fingerprint(
            status=status,  # type: ignore[arg-type]
            command_id=request.binding.command_id,
            target_id=request.binding.expected_target_id,
            target_generation=request.binding.expected_target_generation,
            public_result_code=code,
            public_result_text=text,
            durable_reference_ids=(),
            query_token=query_token,
        ),
    )


def test_postgres_prompt_queue_commit_checkpoint_and_reopen_are_one_authority(
    migrated_postgres_database: MigratedPostgresTestDatabase,
    tmp_path: Path,
) -> None:
    runtime_session_id = f"runtime:terminal-postgres:{uuid4().hex}"
    context = EventContext(
        run_id=f"run:terminal-postgres:{uuid4().hex}",
        turn_id=f"turn:terminal-postgres:{uuid4().hex}",
        reply_id=f"reply:terminal-postgres:{uuid4().hex}",
    )
    event_log = _event_log(
        migrated_postgres_database,
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    archive = PostgresArtifactStore(
        verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    )
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=runtime_session_id,
        event_log=event_log,
        archive=archive,
        allow_unbootstrapped_test_events=False,
    )
    seed = persist_test_run_transcript_seed(runtime, run_id=context.run_id)

    async def first_process() -> tuple[str, str]:
        run_start_fields = run_start_permission_fields(
            context.run_id,
            user_input="durable queue authority",
            turn_id=context.turn_id,
            reply_id=context.reply_id,
            ledger_runtime_session_id=runtime_session_id,
            mcp_installation_owner_runtime_session_id=runtime_session_id,
        )
        run_start_fields.update(
            run_transcript_seed_semantic=seed.seed_semantic,
            run_transcript_seed_reference=seed.seed_reference,
        )
        await runtime.write_event(
            RunStartEvent(
                id=f"run_start:test:{context.run_id}",
                **context.event_fields(),
                **run_start_fields,
                user_input_chars=len("durable queue authority"),
            )
        )
        item = await runtime.prompt_queue_mutation_service.submit(
            PromptQueueSubmitRequest(
                command_id="command:postgres-queue",
                client_instance_id="client:postgres-queue",
                client_submission_id="submission:postgres-queue",
                text="queued through the PostgreSQL transaction companion",
                requested_delivery_mode="auto",
                event_context=context,
            )
        )
        assert item.delivery_state == "accepted_pending"
        assert item.resolved_delivery_mode == "pending"
        await _drain_runtime_for_close(runtime)
        return item.queue_item_id, item.row_fingerprint

    queue_item_id, row_fingerprint = asyncio.run(first_process())
    with psycopg.connect(
        migrated_postgres_database.runtime_dsn,
        autocommit=True,
    ) as connection:
        row = connection.execute(
            """
            SELECT state_payload, row_fingerprint
            FROM prompt_queue_items
            WHERE session_id = %s AND queue_item_id = %s
            """,
            (runtime_session_id, queue_item_id),
        ).fetchone()
        account = connection.execute(
            """
            SELECT checkpoint_through_sequence, bounded_tail_count
            FROM prompt_queue_accounts
            WHERE session_id = %s
            """,
            (runtime_session_id,),
        ).fetchone()
    assert row is not None
    assert row[1] == row_fingerprint
    assert row[0]["resolved_delivery_mode"] == "pending"
    assert account is not None
    assert account[0] >= 2
    assert account[1] == 0

    reopened_log = _event_log(
        migrated_postgres_database,
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    reopened = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=runtime_session_id,
        event_log=reopened_log,
        archive=archive,
        allow_unbootstrapped_test_events=False,
    )
    restored = reopened.prompt_queue_projection_store.item(queue_item_id)
    assert restored is not None
    assert restored.row_fingerprint == row_fingerprint
    assert restored.delivery_state == "accepted_pending"
    assert reopened.prompt_queue_projection_store.snapshot().account_revision == 1
    asyncio.run(_drain_runtime_for_close(reopened))


def test_postgres_terminal_command_receipt_is_idempotent_and_recovers_pending(
    migrated_postgres_database: MigratedPostgresTestDatabase,
    tmp_path: Path,
) -> None:
    runtime_session_id = f"runtime:terminal-command-postgres:{uuid4().hex}"
    event_log = _event_log(
        migrated_postgres_database,
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    storage = build_terminal_command_receipt_storage(event_log)
    request = _command_request(
        runtime_session_id=runtime_session_id,
        command_id="command:postgres-completed",
    )
    pending = _command_outcome(
        request,
        status="pending_confirmation",
        code="COMMAND_OWNER_RUNNING",
        text="The command is durably admitted.",
    )
    terminal = _command_outcome(
        request,
        status="succeeded",
        code="COMMAND_SUCCEEDED",
        text="The command completed.",
    )

    async def scenario() -> None:
        deadline = monotonic() + 10.0
        first = await asyncio.to_thread(
            storage.admit_pending,
            runtime_session_id=runtime_session_id,
            client_instance_id=request.binding.client_instance_id,
            command_id=request.binding.command_id,
            command_kind=request.command_kind,
            request_semantic_fingerprint=request.request_fingerprint,
            target_id=request.binding.expected_target_id,
            target_generation=request.binding.expected_target_generation,
            pending_outcome=pending,
            deadline_monotonic=deadline,
        )
        duplicate = await asyncio.to_thread(
            storage.admit_pending,
            runtime_session_id=runtime_session_id,
            client_instance_id=request.binding.client_instance_id,
            command_id=request.binding.command_id,
            command_kind=request.command_kind,
            request_semantic_fingerprint=request.request_fingerprint,
            target_id=request.binding.expected_target_id,
            target_generation=request.binding.expected_target_generation,
            pending_outcome=pending,
            deadline_monotonic=deadline,
        )
        assert first.execution_owner_won
        assert not duplicate.execution_owner_won
        assert duplicate.receipt == first.receipt

        completed = await asyncio.to_thread(
            storage.complete,
            runtime_session_id=runtime_session_id,
            client_instance_id=request.binding.client_instance_id,
            command_id=request.binding.command_id,
            request_semantic_fingerprint=request.request_fingerprint,
            outcome=terminal,
            deadline_monotonic=deadline,
        )
        assert completed.outcome == terminal

        orphan = _command_request(
            runtime_session_id=runtime_session_id,
            command_id="command:postgres-orphan",
        )
        orphan_pending = _command_outcome(
            orphan,
            status="pending_confirmation",
            code="COMMAND_OWNER_RUNNING",
            text="The command is durably admitted.",
        )
        await asyncio.to_thread(
            storage.admit_pending,
            runtime_session_id=runtime_session_id,
            client_instance_id=orphan.binding.client_instance_id,
            command_id=orphan.binding.command_id,
            command_kind=orphan.command_kind,
            request_semantic_fingerprint=orphan.request_fingerprint,
            target_id=orphan.binding.expected_target_id,
            target_generation=orphan.binding.expected_target_generation,
            pending_outcome=orphan_pending,
            deadline_monotonic=deadline,
        )
        owner = TerminalCommandOwner(
            runtime_session_id=runtime_session_id,
            receipt_storage=storage,
        )
        await owner.recover_pending(deadline_monotonic=deadline)
        recovered = await owner.query(
            client_instance_id=orphan.binding.client_instance_id,
            command_id=orphan.binding.command_id,
        )
        assert recovered is not None
        assert recovered.status == "reconciliation_required"
        assert recovered.public_result_code == "COMMAND_OWNER_LOST_AFTER_RESTART"
        owner.close()

    asyncio.run(scenario())


def test_postgres_artifact_read_physically_exits_at_absolute_deadline(
    migrated_postgres_database: MigratedPostgresTestDatabase,
    tmp_path: Path,
) -> None:
    runtime_session_id = f"runtime:terminal-artifact-deadline:{uuid4().hex}"
    _event_log(
        migrated_postgres_database,
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    archive = PostgresArtifactStore(
        verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    )
    artifact_id = f"artifact:terminal-deadline:{uuid4().hex}"
    archive.put_text(
        artifact_id,
        "deadline-bound artifact",
        session_id=runtime_session_id,
    )

    blocker = psycopg.connect(
        migrated_postgres_database.admin_dsn,
        autocommit=False,
    )
    try:
        blocker.execute("lock table public.artifacts in access exclusive mode")
        started = monotonic()
        with pytest.raises(TimeoutError, match="statement deadline exceeded"):
            archive.get_text(
                artifact_id,
                session_id=runtime_session_id,
                deadline_monotonic=started + 0.15,
            )
        elapsed = monotonic() - started
        # The blocking transaction is deliberately still held here.  The target
        # physical read therefore exited because PostgreSQL enforced its own
        # statement deadline, not because the test released the blocker.
        assert elapsed < 1.0
        blocker.execute("select 1")
    finally:
        blocker.rollback()
        blocker.close()
