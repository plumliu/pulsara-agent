import asyncio
import hashlib
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.event import EventContext, ReplyEndEvent, TextBlockDeltaEvent
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.memory import (
    InMemoryArchiveStore,
    PostgresArtifactStore,
    RunTimelinePersistenceHook,
    load_run_timeline,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.runtime import RuntimeSession
from pulsara_agent.settings import StorageConfig


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def _runtime_session_id() -> str:
    return f"runtime:test:{uuid4().hex}"


def _event_context(label: str) -> EventContext:
    return EventContext(
        run_id=f"run:{label}:{uuid4().hex}",
        turn_id=f"turn:{label}:{uuid4().hex}",
        reply_id=f"reply:{label}:{uuid4().hex}",
    )


def _seed_runtime_parent_rows(dsn: str, tmp_path: Path, *, runtime_session_id: str | None = None):
    session_id = runtime_session_id or _runtime_session_id()
    ctx = _event_context("artifact-parent")
    event_log = PostgresEventLog(dsn=dsn, runtime_session_id=session_id, workspace_root=tmp_path)
    event_log.append(TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:parent", delta="parent"))
    return session_id, ctx


def _delete_session(dsn: str, runtime_session_id: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def _delete_artifact(dsn: str, blob_id: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from artifacts where id = %s", (blob_id,))


def _artifact_count(dsn: str, blob_id: str) -> int:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from artifacts where id = %s", (blob_id,))
            return cursor.fetchone()[0]


def _artifact_id_from_node_ref(node_id: str) -> str:
    prefix = "urn:pulsara:"
    if node_id.startswith(prefix):
        return urllib.parse.unquote(node_id[len(prefix) :])
    return node_id


@pytest.fixture(params=["memory", "postgres"])
def artifact_store(request) -> ArtifactStore:
    if request.param == "memory":
        return InMemoryArchiveStore()

    dsn = StorageConfig.from_env().postgres_dsn
    return PostgresArtifactStore(dsn=dsn)


def test_artifact_store_puts_and_gets_text(artifact_store: ArtifactStore) -> None:
    blob_id = f"artifact:test:{uuid4().hex}"
    content = "hello artifact"
    expected_digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    try:
        result = artifact_store.put_text(blob_id, content)

        assert result.id == blob_id
        assert result.digest == expected_digest
        assert result.size_bytes == len(content.encode("utf-8"))
        assert artifact_store.get_text(blob_id) == content
    finally:
        if isinstance(artifact_store, PostgresArtifactStore):
            _delete_artifact(artifact_store.dsn, blob_id)


def test_artifact_store_put_text_is_idempotent_for_same_content(artifact_store: ArtifactStore) -> None:
    blob_id = f"artifact:test:{uuid4().hex}"

    try:
        first = artifact_store.put_text(blob_id, "same")
        second = artifact_store.put_text(blob_id, "same")

        assert first == second
        assert artifact_store.get_text(blob_id) == "same"
    finally:
        if isinstance(artifact_store, PostgresArtifactStore):
            _delete_artifact(artifact_store.dsn, blob_id)


def test_postgres_artifact_store_reloads_persisted_text() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    blob_id = f"artifact:test:{uuid4().hex}"
    _connect_or_skip(dsn).close()

    try:
        first_store = PostgresArtifactStore(dsn=dsn)
        first_store.put_text(blob_id, "durable")
        second_store = PostgresArtifactStore(dsn=dsn)

        assert second_store.get_text(blob_id) == "durable"
    finally:
        _delete_artifact(dsn, blob_id)


def test_postgres_artifact_store_rejects_same_id_with_different_content() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    blob_id = f"artifact:test:{uuid4().hex}"
    store = PostgresArtifactStore(dsn=dsn)
    _connect_or_skip(dsn).close()

    try:
        store.put_text(blob_id, "first")

        with pytest.raises(ValueError, match="different content"):
            store.put_text(blob_id, "second")
    finally:
        _delete_artifact(dsn, blob_id)


def test_postgres_artifact_store_rejects_missing_session_owner() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    blob_id = f"artifact:test:{uuid4().hex}"
    store = PostgresArtifactStore(dsn=dsn)
    _connect_or_skip(dsn).close()

    try:
        with pytest.raises(ValueError, match="does not exist"):
            store.put_text(blob_id, "owned", session_id=f"runtime:missing:{uuid4().hex}")
    finally:
        _delete_artifact(dsn, blob_id)


def test_postgres_artifact_store_rejects_run_without_session() -> None:
    store = PostgresArtifactStore(dsn=StorageConfig.from_env().postgres_dsn)

    with pytest.raises(ValueError, match="requires session_id"):
        store.put_text(f"artifact:test:{uuid4().hex}", "owned", run_id=f"run:{uuid4().hex}")


def test_postgres_artifact_store_rejects_run_owned_by_another_session(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    owner_session_id, owner_ctx = _seed_runtime_parent_rows(dsn, tmp_path)
    other_session_id, _other_ctx = _seed_runtime_parent_rows(dsn, tmp_path)
    blob_id = f"artifact:test:{uuid4().hex}"
    store = PostgresArtifactStore(dsn=dsn)

    try:
        with pytest.raises(ValueError, match="already belongs to runtime session"):
            store.put_text(
                blob_id,
                "owned",
                session_id=other_session_id,
                run_id=owner_ctx.run_id,
            )
    finally:
        _delete_artifact(dsn, blob_id)
        _delete_session(dsn, owner_session_id)
        _delete_session(dsn, other_session_id)


def test_postgres_artifact_store_rejects_owner_conflict_for_same_id(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    first_session_id, first_ctx = _seed_runtime_parent_rows(dsn, tmp_path)
    second_session_id, second_ctx = _seed_runtime_parent_rows(dsn, tmp_path)
    blob_id = f"artifact:test:{uuid4().hex}"
    store = PostgresArtifactStore(dsn=dsn)

    try:
        store.put_text(blob_id, "same", session_id=first_session_id, run_id=first_ctx.run_id)

        with pytest.raises(ValueError, match="already belongs to runtime session"):
            store.put_text(blob_id, "same", session_id=second_session_id, run_id=second_ctx.run_id)
    finally:
        _delete_artifact(dsn, blob_id)
        _delete_session(dsn, first_session_id)
        _delete_session(dsn, second_session_id)


def test_postgres_artifact_store_concurrent_same_content_is_idempotent() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    blob_id = f"artifact:test:{uuid4().hex}"
    store = PostgresArtifactStore(dsn=dsn)
    barrier = Barrier(2)
    _connect_or_skip(dsn).close()

    def put() -> str:
        barrier.wait(timeout=2)
        store.put_text(blob_id, "same")
        return "ok"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(put), executor.submit(put)]
            assert sorted(future.result() for future in futures) == ["ok", "ok"]
        assert _artifact_count(dsn, blob_id) == 1
    finally:
        _delete_artifact(dsn, blob_id)


def test_postgres_artifact_store_concurrent_different_content_rejects_one_writer() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    blob_id = f"artifact:test:{uuid4().hex}"
    store = PostgresArtifactStore(dsn=dsn)
    barrier = Barrier(2)
    _connect_or_skip(dsn).close()

    def put(content: str) -> str:
        barrier.wait(timeout=2)
        try:
            store.put_text(blob_id, content)
        except ValueError:
            return "error"
        return "ok"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(put, "first"), executor.submit(put, "second")]
            assert sorted(future.result() for future in futures) == ["error", "ok"]
        assert _artifact_count(dsn, blob_id) == 1
    finally:
        _delete_artifact(dsn, blob_id)


def test_run_timeline_persistence_can_use_postgres_artifact_store(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    event_log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    runtime = RuntimeSession(
        tmp_path,
        runtime_session_id=runtime_session_id,
        event_log=event_log,
    )
    archive = PostgresArtifactStore(dsn=dsn)
    graph = InMemoryGraphStore()
    runtime.hook_manager.register_event(
        None,
        RunTimelinePersistenceHook(
            graph=graph,
            archive=archive,
            event_store=runtime.event_log,
        ),
    )
    ctx = _event_context("timeline-artifact")

    async def run() -> None:
        await runtime.emit(TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:1", delta="hello"))
        await runtime.emit(ReplyEndEvent(**ctx.event_fields()))

    try:
        asyncio.run(run())

        timeline = load_run_timeline(
            graph=graph,
            archive=archive,
            run_id=ctx.run_id,
            runtime_session_id=runtime_session_id,
        )
        record = graph.find_by_type(rt.RUN_TIMELINE)[0]
        blob_id = _artifact_id_from_node_ref(record[rt.STORED_AS.name]["@id"])

        assert timeline.run_id == ctx.run_id
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select session_id, run_id from artifacts where id = %s", (blob_id,))
                assert cursor.fetchone() == (runtime_session_id, ctx.run_id)
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("delete from artifacts where id like %s", (f"timeline:{runtime_session_id}:{ctx.run_id}:%",))
        _delete_session(dsn, runtime_session_id)
