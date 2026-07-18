import asyncio
import hashlib
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from tests.support.runtime_session import in_memory_runtime_session

from tests.support.model_stream import (
    make_text_block_segment_event,
)

from pulsara_agent.event import EventContext, ReplyEndEvent
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.memory import (
    ArtifactContentConflict,
    PostgresArtifactStore,
    RunTimelinePersistenceHook,
    load_run_timeline,
)
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.ontology import runtime as rt
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
    event_log.append(make_text_block_segment_event(**ctx.event_fields(), block_id="text:parent", delta="parent"))
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


@pytest.fixture
def artifact_store() -> ArtifactStore:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
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
        _delete_artifact(artifact_store.dsn, blob_id)


def test_artifact_store_put_text_is_idempotent_for_same_content(artifact_store: ArtifactStore) -> None:
    blob_id = f"artifact:test:{uuid4().hex}"

    try:
        first = artifact_store.put_text(blob_id, "same")
        second = artifact_store.put_text(blob_id, "same")

        assert first == second
        assert artifact_store.get_text(blob_id) == "same"
    finally:
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


def test_in_memory_deterministic_artifact_confirms_semantic_identity() -> None:
    store = InMemoryArchiveStore()
    first = store.put_text_if_absent_or_confirm_identical(
        "artifact:deterministic",
        "payload",
        session_id="runtime:1",
        run_id="run:1",
        media_type="application/json",
        semantic_metadata={"renderer": "v1", "nested": {"b": 2, "a": 1}},
    )
    second = store.put_text_if_absent_or_confirm_identical(
        "artifact:deterministic",
        "payload",
        session_id="runtime:1",
        run_id="run:1",
        media_type="application/json",
        semantic_metadata={"nested": {"a": 1, "b": 2}, "renderer": "v1"},
    )
    assert first.status == "inserted"
    assert second.status == "confirmed_identical"
    assert first.result == second.result


def test_in_memory_deterministic_artifact_rejects_metadata_only_conflict() -> None:
    store = InMemoryArchiveStore()
    store.put_text_if_absent_or_confirm_identical(
        "artifact:metadata-conflict",
        "same bytes",
        session_id=None,
        run_id=None,
        media_type="text/plain",
        semantic_metadata={"renderer": "v1"},
    )
    with pytest.raises(ArtifactContentConflict, match="semantic_metadata"):
        store.put_text_if_absent_or_confirm_identical(
            "artifact:metadata-conflict",
            "same bytes",
            session_id=None,
            run_id=None,
            media_type="text/plain",
            semantic_metadata={"renderer": "v2"},
        )


def test_in_memory_deterministic_artifact_is_thread_safe() -> None:
    store = InMemoryArchiveStore()
    barrier = Barrier(2)

    def put() -> str:
        barrier.wait(timeout=2)
        return store.put_text_if_absent_or_confirm_identical(
            "artifact:concurrent",
            "same",
            session_id=None,
            run_id=None,
            media_type="text/plain",
            semantic_metadata={"policy": "v1"},
        ).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(
            future.result() for future in (executor.submit(put), executor.submit(put))
        )
    assert statuses == ["confirmed_identical", "inserted"]


def test_postgres_deterministic_artifact_rejects_metadata_only_conflict() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    blob_id = f"artifact:test:{uuid4().hex}"
    store = PostgresArtifactStore(dsn=dsn)
    try:
        store.put_text_if_absent_or_confirm_identical(
            blob_id,
            "same",
            session_id=None,
            run_id=None,
            media_type="text/plain",
            semantic_metadata={"renderer": "v1"},
        )
        with pytest.raises(ArtifactContentConflict, match="semantic_metadata"):
            store.put_text_if_absent_or_confirm_identical(
                blob_id,
                "same",
                session_id=None,
                run_id=None,
                media_type="text/plain",
                semantic_metadata={"renderer": "v2"},
            )
    finally:
        _delete_artifact(dsn, blob_id)


def test_postgres_deterministic_artifact_concurrent_writers_confirm_identity() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    blob_id = f"artifact:test:{uuid4().hex}"
    barrier = Barrier(2)

    def put() -> str:
        barrier.wait(timeout=2)
        return PostgresArtifactStore(
            dsn=dsn
        ).put_text_if_absent_or_confirm_identical(
            blob_id,
            "same",
            session_id=None,
            run_id=None,
            media_type="text/plain",
            semantic_metadata={"renderer": "v1", "cap": 17},
        ).status

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = sorted(
                future.result()
                for future in (executor.submit(put), executor.submit(put))
            )
        assert statuses == ["confirmed_identical", "inserted"]
        assert _artifact_count(dsn, blob_id) == 1
    finally:
        _delete_artifact(dsn, blob_id)


def test_run_timeline_persistence_can_use_postgres_artifact_store(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    event_log = PostgresEventLog(
        dsn=dsn,
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    event_log.ensure_runtime_session_owner()
    ctx = _event_context("timeline-artifact")
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into runs (id, session_id) values (%s, %s)",
                (ctx.run_id, runtime_session_id),
            )
            cursor.execute(
                """
                insert into turns (id, session_id, run_id, turn_index)
                values (%s, %s, %s, 0)
                """,
                (ctx.turn_id, runtime_session_id, ctx.run_id),
            )
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=runtime_session_id,
    )
    archive = PostgresArtifactStore(dsn=dsn)
    graph_id = f"graph:test:{uuid4().hex}"
    graph = PostgresGraphStore(dsn=dsn)
    runtime.hook_manager.register_event(
        None,
        RunTimelinePersistenceHook(
            graph=graph,
            archive=archive,
            event_store=runtime.event_log,
            graph_id=graph_id,
        ),
    )

    async def run() -> None:
        await runtime.emit(make_text_block_segment_event(**ctx.event_fields(), block_id="text:1", delta="hello"))
        await runtime.emit(ReplyEndEvent(**ctx.event_fields(), model_terminal_outcome="completed"))

    try:
        asyncio.run(run())

        timeline = load_run_timeline(
            graph=graph,
            archive=archive,
            run_id=ctx.run_id,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
        )
        record = graph.find_by_type(rt.RUN_TIMELINE, graph_id=graph_id)[0]
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
        graph.delete_graph(graph_id)
        _delete_session(dsn, runtime_session_id)
