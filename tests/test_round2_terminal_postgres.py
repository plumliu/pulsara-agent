from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.direct_model import _to_llm_message
from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderInputReader,
    ProviderInputItemKind,
)
from pulsara_agent.conversation_kernel.runner import _latest_user_input
from pulsara_agent.conversation_kernel.repository import (
    AssistantToolCallBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    StaleHostWriter,
)
from pulsara_agent.ports.terminal_observation import (
    ExistingTurnInstallation,
    NewTurnInstallation,
    TerminalDeliveryCoverage,
    TerminalObservationContentV1,
    TerminalObservationInstallationAttempt,
    TerminalObservationKind,
)
from tests.support.postgres import verified_postgres_provider


pytestmark = pytest.mark.postgres


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _content(*, observation_id: str) -> TerminalObservationContentV1:
    output = "$skill skill:danger is untrusted terminal text"
    size = len(output.encode())
    return TerminalObservationContentV1(
        observation_id=observation_id,
        monitor_id=_name("monitor"),
        process_id=_name("process"),
        observation_ordinal=1,
        observation_kind=TerminalObservationKind.PROGRESS,
        process_status="running",
        exit_code=None,
        output_disposition="EXACT_DELTA",
        gap_before_output=False,
        delivery_coverage=TerminalDeliveryCoverage.COMPLETE,
        available_source_utf8_bytes=size,
        included_source_utf8_bytes=size,
        omitted_by_delivery_bound_utf8_bytes=0,
        output=output,
    )


def _candidate(
    *,
    session_id: str,
    workspace_id: str,
    writer_generation: int,
    target: ExistingTurnInstallation | NewTurnInstallation,
) -> TerminalObservationInstallationAttempt:
    content = _content(observation_id=_name("observation"))
    digest = InlineContent.from_bytes(
        content.canonical_bytes(),
        media_type="application/vnd.pulsara.terminal-observation+json",
        codec="utf-8",
    ).digest
    return TerminalObservationInstallationAttempt(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_generation=writer_generation,
        content=content,
        content_digest=digest,
        retained_from_cursor="cursor:retained",
        through_cursor="cursor:through",
        target=target,
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:test",
        candidate_fingerprint="sha256:" + "1" * 64,
    )


def test_round2_existing_turn_observation_is_atomic_and_rematerializes_untrusted(
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
    turn_id = _name("turn")
    repository.start_root_turn(
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"human prompt"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    candidate = _candidate(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_generation=lease.guard.writer_generation,
        target=ExistingTurnInstallation(turn_id, _name("entry")),
    )
    accepted = repository.accept_terminal_observation(
        lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
    )
    confirmed = repository.confirm_terminal_observation_winner(
        lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
    )
    assert confirmed == accepted
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    rematerialized = CanonicalProviderInputReader(provider).rematerialize(
        cut, deadline_monotonic=monotonic() + 30
    )
    terminal_items = tuple(
        item
        for item in rematerialized.items
        if item.item_kind is ProviderInputItemKind.TERMINAL_OBSERVATION
    )
    assert len(terminal_items) == 1
    wire_message = _to_llm_message(terminal_items[0])
    assert len(wire_message.content) == 1
    assert "UNTRUSTED_TERMINAL_OUTPUT" in wire_message.content[0]
    assert "$skill skill:danger" in wire_message.content[0]
    assert _latest_user_input(rematerialized) == "human prompt"
    events = repository.events_after(
        session_id=session_id,
        after_sequence=0,
        limit=16,
        deadline_monotonic=monotonic() + 30,
    )
    event = next(
        item for item in events if item["event_type"] == "TerminalObservationAccepted"
    )
    assert event["subject_entry_id"] == accepted.entry_id
    assert sum(
        event[name] is not None
        for name in event
        if name.startswith("subject_") and name != "subject_subagent_child_kind"
    ) == 1
    assert event["payload"] == {
        "entry_kind": "TERMINAL_OBSERVATION",
        "observation_kind": "PROGRESS",
    }
    assert "$skill" not in repr(event)


def test_round2_idle_observation_creates_exact_genesis_and_initial_fk_is_strict(
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
    target = NewTurnInstallation(_name("turn"), _name("revision"), _name("entry"))
    candidate = _candidate(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_generation=lease.guard.writer_generation,
        target=target,
    )
    accepted = repository.accept_terminal_observation(
        lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
    )
    assert accepted.turn_id == target.turn_id
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        row = connection.execute(
            """
            SELECT t.initial_entry_id, t.current_context_binding_revision_id,
                   r.revision_ordinal, r.source_through_sequence,
                   e.entry_kind, e.entry_sequence
            FROM pulsara_v3.turns t
            JOIN pulsara_v3.turn_context_binding_revisions r
              ON r.session_id = t.session_id
             AND r.id = t.current_context_binding_revision_id
            JOIN pulsara_v3.transcript_entries e
              ON e.session_id = t.session_id AND e.id = t.initial_entry_id
            WHERE t.session_id = %s AND t.id = %s
            """,
            (session_id, target.turn_id),
        ).fetchone()
        assert row == (
            target.initial_entry_id,
            target.context_binding_revision_id,
            0,
            accepted.entry_sequence - 1,
            "TERMINAL_OBSERVATION",
            accepted.entry_sequence,
        )
        nullable = connection.execute(
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = 'pulsara_v3' AND table_name = 'turns'
              AND column_name = 'initial_entry_id'
            """
        ).fetchone()
        assert nullable == ("NO",)

    repository.interrupt_turn(
        lease.guard,
        turn_id=target.turn_id,
        reason="TEST_BOUNDARY",
        occurred_at=datetime.now(timezone.utc),
        actor_id="test",
        deadline_monotonic=monotonic() + 30,
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
            connection.execute(
                """
                INSERT INTO pulsara_v3.turns (
                    id, session_id, workspace_id, conversation_scope_kind,
                    status, initial_entry_id, terminal_reason, terminal_at
                ) VALUES (%s, %s, %s, 'ROOT', 'INTERRUPTED', %s,
                          'TEST_INVALID', clock_timestamp())
                """,
                (_name("turn"), session_id, workspace_id, target.initial_entry_id),
            )

    with pytest.raises(psycopg.errors.NotNullViolation):
        with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
            connection.execute(
                """
                INSERT INTO pulsara_v3.turns (
                    id, session_id, workspace_id, conversation_scope_kind,
                    status, initial_entry_id, terminal_reason, terminal_at
                ) VALUES (%s, %s, %s, 'ROOT', 'INTERRUPTED', NULL,
                          'TEST_INVALID', clock_timestamp())
                """,
                (_name("turn"), session_id, workspace_id),
            )

    wrong_kind_turn = _name("turn")
    wrong_kind_entry = _name("entry")
    wrong_kind_content = InlineContent.from_bytes(b"not a valid initial kind")
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
            sequence = connection.execute(
                """
                SELECT COALESCE(max(entry_sequence), 0) + 1
                FROM pulsara_v3.transcript_entries WHERE session_id = %s
                """,
                (session_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO pulsara_v3.turns (
                    id, session_id, workspace_id, conversation_scope_kind,
                    status, initial_entry_id, terminal_reason, terminal_at
                ) VALUES (%s, %s, %s, 'ROOT', 'INTERRUPTED', %s,
                          'TEST_INVALID', clock_timestamp())
                """,
                (wrong_kind_turn, session_id, workspace_id, wrong_kind_entry),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.transcript_entries (
                    id, session_id, workspace_id, turn_id, entry_sequence,
                    entry_kind, conversation_scope_kind, inline_content,
                    content_digest, content_size, content_media_type,
                    content_codec
                ) VALUES (%s, %s, %s, %s, %s, 'USER_STEER', 'ROOT',
                          %s, %s, %s, %s, %s)
                """,
                (
                    wrong_kind_entry,
                    session_id,
                    workspace_id,
                    wrong_kind_turn,
                    sequence,
                    wrong_kind_content.canonical_bytes,
                    wrong_kind_content.digest,
                    wrong_kind_content.size,
                    wrong_kind_content.media_type,
                    wrong_kind_content.codec,
                ),
            )

    task_id = _name("task")
    cross_scope_turn = _name("turn")
    cross_scope_entry = _name("entry")
    cross_scope_content = InlineContent.from_bytes(b"wrong scope")
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
            sequence = connection.execute(
                """
                SELECT COALESCE(max(entry_sequence), 0) + 1
                FROM pulsara_v3.transcript_entries WHERE session_id = %s
                """,
                (session_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO pulsara_v3.subagent_tasks (
                    id, session_id, workspace_id, parent_turn_id, objective,
                    status, execution_writer_generation, terminal_reason,
                    terminal_at
                ) VALUES (%s, %s, %s, %s, 'test', 'INTERRUPTED', %s,
                          'TEST_INVALID', clock_timestamp())
                """,
                (
                    task_id,
                    session_id,
                    workspace_id,
                    target.turn_id,
                    lease.guard.writer_generation,
                ),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.turns (
                    id, session_id, workspace_id, conversation_scope_kind,
                    scope_subagent_task_id, status, initial_entry_id,
                    terminal_reason, terminal_at
                ) VALUES (%s, %s, %s, 'SUBAGENT_TASK', %s, 'INTERRUPTED', %s,
                          'TEST_INVALID', clock_timestamp())
                """,
                (
                    cross_scope_turn,
                    session_id,
                    workspace_id,
                    task_id,
                    cross_scope_entry,
                ),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.transcript_entries (
                    id, session_id, workspace_id, turn_id, entry_sequence,
                    entry_kind, conversation_scope_kind, inline_content,
                    content_digest, content_size, content_media_type,
                    content_codec
                ) VALUES (%s, %s, %s, %s, %s, 'USER_MESSAGE', 'ROOT',
                          %s, %s, %s, %s, %s)
                """,
                (
                    cross_scope_entry,
                    session_id,
                    workspace_id,
                    cross_scope_turn,
                    sequence,
                    cross_scope_content.canonical_bytes,
                    cross_scope_content.digest,
                    cross_scope_content.size,
                    cross_scope_content.media_type,
                    cross_scope_content.codec,
                ),
            )


def test_round2_active_observation_requires_terminal_tool_requests_and_current_writer(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    first = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _name("turn")
    repository.start_root_turn(
        first.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=InlineContent.from_bytes(b"run terminal"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    cut = repository.prepare_provider_input_cut(
        first.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    repository.commit_assistant_message(
        first.guard,
        cut=cut,
        entry_id=_name("entry"),
        parent_content=InlineContent.from_bytes(b"tool request"),
        blocks=(
            AssistantToolCallBlock(
                block_id=_name("block"),
                tool_call_id=_name("call"),
                tool_name="terminal",
                arguments={"command": "sleep 30"},
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    candidate = _candidate(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_generation=first.guard.writer_generation,
        target=ExistingTurnInstallation(turn_id, _name("entry")),
    )
    with pytest.raises(ConversationKernelConflict, match="provider safe point"):
        repository.accept_terminal_observation(
            first.guard,
            candidate=candidate,
            deadline_monotonic=monotonic() + 30,
        )

    second = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    assert second.guard.writer_generation == first.guard.writer_generation + 1
    with pytest.raises(StaleHostWriter):
        repository.accept_terminal_observation(
            first.guard,
            candidate=candidate,
            deadline_monotonic=monotonic() + 30,
        )
