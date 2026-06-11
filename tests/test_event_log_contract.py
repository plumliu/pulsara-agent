import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.event import (
    EventContext,
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
)
from pulsara_agent.event_log import EventLog, InMemoryEventLog, PostgresEventLog
from pulsara_agent.settings import StorageConfig


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def _cleanup_session(dsn: str, runtime_session_id: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def _runtime_session_id() -> str:
    return f"runtime:test:{uuid4().hex}"


def _ctx(label: str) -> EventContext:
    return EventContext(
        run_id=f"run:{label}",
        turn_id=f"turn:{label}",
        reply_id=f"reply:{label}",
    )


def _reply_events(ctx: EventContext):
    return [
        ReplyStartEvent(**ctx.event_fields(), name="assistant"),
        TextBlockStartEvent(**ctx.event_fields(), block_id="text:1"),
        TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:1", delta="hello "),
        TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:1", delta="world"),
        TextBlockEndEvent(**ctx.event_fields(), block_id="text:1"),
        ReplyEndEvent(**ctx.event_fields()),
    ]


@pytest.fixture(params=["memory", "postgres"])
def event_log(request, tmp_path) -> EventLog:
    if request.param == "memory":
        return InMemoryEventLog()

    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    log = PostgresEventLog(
        dsn=dsn,
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    request.addfinalizer(lambda: _cleanup_session(dsn, runtime_session_id))
    return log


def test_event_log_assigns_sequences_and_filters_events(event_log: EventLog) -> None:
    first = _ctx("contract:first")
    second = _ctx("contract:second")

    event_log.extend(_reply_events(first))
    stored = event_log.append(TextBlockDeltaEvent(**second.event_fields(), block_id="text:2", delta="other"))

    assert stored.sequence == 7
    assert [event.sequence for event in event_log.iter()] == list(range(1, 8))
    assert [event.reply_id for event in event_log.iter(run_id=first.run_id)] == [first.reply_id] * 6
    assert [event.run_id for event in event_log.iter(reply_id=second.reply_id)] == [second.run_id]


def test_event_log_replay_rebuilds_assistant_message(event_log: EventLog) -> None:
    ctx = _ctx("contract:replay")
    event_log.extend(_reply_events(ctx))

    message = event_log.replay(ctx.reply_id)

    assert message.id == ctx.reply_id
    assert message.name == "assistant"
    assert message.content[0].type == "text"
    assert message.content[0].text == "hello world"


def test_event_log_preassigned_sequence_advances_next_sequence(event_log: EventLog) -> None:
    ctx = _ctx("contract:preset")
    preset = event_log.append(TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:10", delta="preset", sequence=10))
    next_event = event_log.append(TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:11", delta="next"))

    assert preset.sequence == 10
    assert next_event.sequence == 11


def test_postgres_event_log_reloads_persisted_events(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        first_log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        ctx = _ctx("postgres:reload")
        first_log.extend(_reply_events(ctx))

        second_log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        assert [event.sequence for event in second_log.iter(reply_id=ctx.reply_id)] == list(range(1, 7))
        assert second_log.replay(ctx.reply_id).content[0].text == "hello world"
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_postgres_event_log_concurrent_append_keeps_unique_sequences(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        event_log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        ctx = _ctx("postgres:concurrent")
        events = [
            TextBlockDeltaEvent(**ctx.event_fields(), block_id=f"text:{index}", delta=str(index))
            for index in range(12)
        ]

        with ThreadPoolExecutor(max_workers=4) as executor:
            stored = list(executor.map(event_log.append, events))

        assert sorted(event.sequence for event in stored) == list(range(1, 13))
        assert [event.sequence for event in event_log.iter(reply_id=ctx.reply_id)] == list(range(1, 13))
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_postgres_event_log_rejects_cross_session_run_id_reuse(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    first_session_id = _runtime_session_id()
    second_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        first_log = PostgresEventLog(dsn=dsn, runtime_session_id=first_session_id, workspace_root=tmp_path)
        second_log = PostgresEventLog(dsn=dsn, runtime_session_id=second_session_id, workspace_root=tmp_path)
        first_ctx = EventContext(run_id="run:shared", turn_id=f"turn:{uuid4().hex}", reply_id=f"reply:{uuid4().hex}")
        second_ctx = EventContext(run_id="run:shared", turn_id=f"turn:{uuid4().hex}", reply_id=f"reply:{uuid4().hex}")

        first_log.append(TextBlockDeltaEvent(**first_ctx.event_fields(), block_id="text:1", delta="first"))

        with pytest.raises(ValueError, match="already belongs to runtime session"):
            second_log.append(TextBlockDeltaEvent(**second_ctx.event_fields(), block_id="text:2", delta="second"))
    finally:
        _cleanup_session(dsn, first_session_id)
        _cleanup_session(dsn, second_session_id)


def test_postgres_event_log_rejects_cross_run_turn_id_reuse(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        event_log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        turn_id = f"turn:shared:{uuid4().hex}"
        first_ctx = EventContext(run_id=f"run:first:{uuid4().hex}", turn_id=turn_id, reply_id=f"reply:{uuid4().hex}")
        second_ctx = EventContext(run_id=f"run:second:{uuid4().hex}", turn_id=turn_id, reply_id=f"reply:{uuid4().hex}")

        event_log.append(TextBlockDeltaEvent(**first_ctx.event_fields(), block_id="text:1", delta="first"))

        with pytest.raises(ValueError, match="already belongs to runtime session"):
            event_log.append(TextBlockDeltaEvent(**second_ctx.event_fields(), block_id="text:2", delta="second"))
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_postgres_event_log_rejects_concurrent_cross_session_run_id_reuse(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    first_session_id = _runtime_session_id()
    second_session_id = _runtime_session_id()
    shared_run_id = f"run:shared:{uuid4().hex}"
    barrier = Barrier(2)
    _connect_or_skip(dsn).close()

    def append_with(log: PostgresEventLog, ctx: EventContext) -> str:
        barrier.wait(timeout=2)
        try:
            log.append(TextBlockDeltaEvent(**ctx.event_fields(), block_id=f"text:{uuid4().hex}", delta="x"))
        except ValueError:
            return "error"
        return "ok"

    try:
        first_log = PostgresEventLog(dsn=dsn, runtime_session_id=first_session_id, workspace_root=tmp_path)
        second_log = PostgresEventLog(dsn=dsn, runtime_session_id=second_session_id, workspace_root=tmp_path)
        first_ctx = EventContext(run_id=shared_run_id, turn_id=f"turn:{uuid4().hex}", reply_id=f"reply:{uuid4().hex}")
        second_ctx = EventContext(run_id=shared_run_id, turn_id=f"turn:{uuid4().hex}", reply_id=f"reply:{uuid4().hex}")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(append_with, first_log, first_ctx),
                executor.submit(append_with, second_log, second_ctx),
            ]
            outcomes = sorted(future.result() for future in futures)

        assert outcomes == ["error", "ok"]
    finally:
        _cleanup_session(dsn, first_session_id)
        _cleanup_session(dsn, second_session_id)


def test_postgres_event_log_rejects_concurrent_cross_session_turn_id_reuse(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    first_session_id = _runtime_session_id()
    second_session_id = _runtime_session_id()
    shared_turn_id = f"turn:shared:{uuid4().hex}"
    barrier = Barrier(2)
    _connect_or_skip(dsn).close()

    def append_with(log: PostgresEventLog, ctx: EventContext) -> str:
        barrier.wait(timeout=2)
        try:
            log.append(TextBlockDeltaEvent(**ctx.event_fields(), block_id=f"text:{uuid4().hex}", delta="x"))
        except ValueError:
            return "error"
        return "ok"

    try:
        first_log = PostgresEventLog(dsn=dsn, runtime_session_id=first_session_id, workspace_root=tmp_path)
        second_log = PostgresEventLog(dsn=dsn, runtime_session_id=second_session_id, workspace_root=tmp_path)
        first_ctx = EventContext(
            run_id=f"run:first:{uuid4().hex}",
            turn_id=shared_turn_id,
            reply_id=f"reply:{uuid4().hex}",
        )
        second_ctx = EventContext(
            run_id=f"run:second:{uuid4().hex}",
            turn_id=shared_turn_id,
            reply_id=f"reply:{uuid4().hex}",
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(append_with, first_log, first_ctx),
                executor.submit(append_with, second_log, second_ctx),
            ]
            outcomes = sorted(future.result() for future in futures)

        assert outcomes == ["error", "ok"]
    finally:
        _cleanup_session(dsn, first_session_id)
        _cleanup_session(dsn, second_session_id)


def test_postgres_event_log_extend_rolls_back_entire_batch_on_parent_conflict(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    conflicting_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        event_log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        conflicting_log = PostgresEventLog(dsn=dsn, runtime_session_id=conflicting_session_id, workspace_root=tmp_path)
        conflicting_ctx = EventContext(
            run_id="run:batch-conflict",
            turn_id=f"turn:{uuid4().hex}",
            reply_id=f"reply:{uuid4().hex}",
        )
        valid_ctx = EventContext(
            run_id=f"run:valid:{uuid4().hex}",
            turn_id=f"turn:{uuid4().hex}",
            reply_id=f"reply:{uuid4().hex}",
        )
        invalid_ctx = EventContext(
            run_id=conflicting_ctx.run_id,
            turn_id=f"turn:{uuid4().hex}",
            reply_id=f"reply:{uuid4().hex}",
        )
        conflicting_log.append(
            TextBlockDeltaEvent(**conflicting_ctx.event_fields(), block_id="text:seed", delta="seed")
        )

        with pytest.raises(ValueError, match="already belongs to runtime session"):
            event_log.extend(
                [
                    TextBlockDeltaEvent(**valid_ctx.event_fields(), block_id="text:valid", delta="valid"),
                    TextBlockDeltaEvent(**invalid_ctx.event_fields(), block_id="text:invalid", delta="invalid"),
                ]
            )

        assert event_log.iter(reply_id=valid_ctx.reply_id) == []
        assert event_log.iter(reply_id=invalid_ctx.reply_id) == []
    finally:
        _cleanup_session(dsn, runtime_session_id)
        _cleanup_session(dsn, conflicting_session_id)


def test_runtime_session_can_emit_with_postgres_event_log(tmp_path: Path) -> None:
    from pulsara_agent.runtime import RuntimeSession

    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        event_log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        runtime = RuntimeSession(
            tmp_path,
            runtime_session_id=runtime_session_id,
            event_log=event_log,
        )
        ctx = _ctx("postgres:runtime")

        async def run() -> None:
            first = await runtime.emit(TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:1", delta="hello"))
            second = await runtime.emit(TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:1", delta=" world"))
            assert [first.sequence, second.sequence] == [1, 2]

        asyncio.run(run())

        reloaded = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        assert [event.sequence for event in reloaded.iter(reply_id=ctx.reply_id)] == [1, 2]
    finally:
        _cleanup_session(dsn, runtime_session_id)
