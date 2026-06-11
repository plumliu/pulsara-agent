import asyncio
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.event import EventContext, ReplyEndEvent, TextBlockDeltaEvent
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.memory import load_run_timeline, summarize_run_timeline
from pulsara_agent.ontology import memory
from pulsara_agent.runtime import build_durable_runtime_wiring, build_in_memory_runtime_wiring
from pulsara_agent.settings import PulsaraSettings, StorageConfig


OXIGRAPH_URL = "http://localhost:7878"


def test_in_memory_runtime_wiring_persists_run_timeline(tmp_path) -> None:
    wiring = build_in_memory_runtime_wiring(tmp_path, runtime_session_id=f"runtime:test:{uuid4().hex}")
    ctx = _event_context("in-memory-wiring")

    asyncio.run(_emit_timeline_events(wiring.runtime_session, ctx, "hello wiring"))

    timeline = load_run_timeline(
        graph=wiring.graph,
        archive=wiring.archive,
        run_id=ctx.run_id,
        runtime_session_id=wiring.runtime_session.runtime_session_id,
        graph_id=wiring.graph_id,
    )
    summary = summarize_run_timeline(timeline)

    assert wiring.event_log is wiring.runtime_session.event_log
    assert wiring.graph_id is None
    assert summary.assistant_text == "hello wiring"
    assert summary.status == "completed"


def test_durable_runtime_wiring_uses_postgres_oxigraph_and_artifacts(tmp_path) -> None:
    if not _oxigraph_available():
        pytest.skip("Oxigraph is not running at http://localhost:7878")

    storage = StorageConfig.from_env()
    _connect_or_skip(storage.postgres_dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    ctx = _event_context("durable-wiring")
    wiring = build_durable_runtime_wiring(
        _settings_for_storage(storage),
        tmp_path,
        runtime_session_id=runtime_session_id,
        graph_id=graph_id,
    )
    timeline_blob_id: str | None = None

    try:
        asyncio.run(_emit_timeline_events(wiring.runtime_session, ctx, "hello durable wiring"))
        events = wiring.event_log.iter(run_id=ctx.run_id)
        records = wiring.graph.find_by_type(memory.RUN_TIMELINE, graph_id=graph_id)
        timeline_blob_id = _artifact_id_from_node_ref(records[0][memory.STORED_AS.name]["@id"])
        timeline = load_run_timeline(
            graph=wiring.graph,
            archive=wiring.archive,
            run_id=ctx.run_id,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
        )
        summary = summarize_run_timeline(timeline)

        assert wiring.graph_id == graph_id
        assert [event.sequence for event in events] == [1, 2]
        assert len(records) == 1
        assert records[0][memory.SOURCE_RUN.name] == ctx.run_id
        assert records[0][memory.SOURCE_SESSION.name] == runtime_session_id
        assert records[0][memory.STATUS.name] == "completed"
        assert timeline_blob_id.startswith(f"timeline:{runtime_session_id}:{ctx.run_id}:")
        assert "hello durable wiring" in wiring.archive.get_text(timeline_blob_id)
        assert summary.assistant_text == "hello durable wiring"
    finally:
        wiring.graph.delete_graph(graph_id)
        _delete_postgres_artifacts_with_prefix(storage.postgres_dsn, f"timeline:{runtime_session_id}:{ctx.run_id}:")
        _delete_postgres_runtime_session(storage.postgres_dsn, runtime_session_id)


async def _emit_timeline_events(runtime_session, ctx: EventContext, text: str) -> None:
    await runtime_session.emit(TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:1", delta=text))
    await runtime_session.emit(ReplyEndEvent(**ctx.event_fields()))


def _event_context(label: str) -> EventContext:
    return EventContext(
        run_id=f"run:{label}:{uuid4().hex}",
        turn_id=f"turn:{label}:{uuid4().hex}",
        reply_id=f"reply:{label}:{uuid4().hex}",
    )


def _settings_for_storage(storage: StorageConfig) -> PulsaraSettings:
    return PulsaraSettings(
        llm=LLMConfig(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
        ),
        storage=storage,
    )


def _oxigraph_available() -> bool:
    query = urllib.parse.urlencode({"query": "ASK { ?s ?p ?o }"}).encode("utf-8")
    request = urllib.request.Request(
        f"{OXIGRAPH_URL}/query",
        data=query,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1):
            return True
    except (OSError, urllib.error.URLError):
        return False


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def _delete_postgres_runtime_session(dsn: str, runtime_session_id: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def _delete_postgres_artifacts_with_prefix(dsn: str, blob_id_prefix: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from artifacts where id like %s", (f"{blob_id_prefix}%",))


def _artifact_id_from_node_ref(node_id: str) -> str:
    prefix = "urn:pulsara:"
    if node_id.startswith(prefix):
        return urllib.parse.unquote(node_id[len(prefix) :])
    return node_id
