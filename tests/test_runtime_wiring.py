import asyncio
import urllib.parse
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.event import EventContext, ReplyEndEvent, TextBlockDeltaEvent
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.memory import load_run_timeline, summarize_run_timeline
from pulsara_agent.memory.scope import MemoryDomainContext, workspace_scope
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.runtime import (
    AgentRuntimeWiring,
    build_agent_runtime_wiring,
    build_durable_runtime_wiring,
    build_in_memory_runtime_wiring,
)
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionProfile,
    TerminalAccess,
)
from pulsara_agent.capability import LocalSkillResolver
from pulsara_agent.settings import PulsaraSettings, StorageConfig
from pulsara_agent.retrieval.runtime import RetrievalRuntimeResources
from pulsara_agent.memory.canonical.mutation_outbox import CanonicalMutationSurface


class _WiringEmbeddingProvider:
    model_id = "wiring-fake"
    dimensions = 1024

    async def embed(self, text):
        return [0.0] * 1024

    async def embed_batch(self, texts):
        return [[0.0] * 1024 for _ in texts]

    async def aclose(self):
        return None


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


def test_vector_enabled_durable_wiring_explicitly_registers_vector_outbox_surface(tmp_path) -> None:
    storage = StorageConfig.from_env()
    _connect_or_skip(storage.postgres_dsn).close()
    resources = RetrievalRuntimeResources(embedding=_WiringEmbeddingProvider())
    wiring = build_durable_runtime_wiring(
        _settings_for_storage(storage),
        tmp_path,
        graph_id=f"graph:test/{uuid4().hex}",
        retrieval_resources=resources,
    )

    assert wiring.memory_governance_executor.async_surfaces == (
        CanonicalMutationSurface.SEARCH_INDEX.value,
        CanonicalMutationSurface.OXIGRAPH.value,
        CanonicalMutationSurface.VECTOR_INDEX.value,
    )


def test_agent_runtime_wiring_uses_in_memory_runtime_wiring_without_external_services(tmp_path) -> None:
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    settings = _settings_for_storage(StorageConfig(postgres_dsn="", oxigraph_url="http://127.0.0.1:1"))
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=False,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=32),
        system_prompt="test prompt",
        runtime_session_id=runtime_session_id,
        graph_id=graph_id,
    )

    assert isinstance(wiring, AgentRuntimeWiring)
    assert wiring.agent_runtime.runtime_session is wiring.runtime_wiring.runtime_session
    assert wiring.runtime_wiring.runtime_session.runtime_session_id == runtime_session_id
    assert wiring.runtime_wiring.graph_id == graph_id
    assert wiring.agent_runtime.model_role.name == "FLASH"
    assert wiring.agent_runtime.options == LLMOptions(temperature=0, max_output_tokens=32)
    assert wiring.agent_runtime.system_prompt == "test prompt"
    assert isinstance(wiring.agent_runtime.capability_resolver, LocalSkillResolver)
    assert wiring.agent_runtime.workspace_kind == "transient"
    assert wiring.agent_runtime.permission_policy.profile is PermissionProfile.TRUSTED_HOST
    assert wiring.agent_runtime.permission_policy.approval is ApprovalPolicy.NEVER
    assert wiring.agent_runtime.permission_policy.terminal is TerminalAccess.ALLOW
    assert "terminal" in wiring.agent_runtime.tool_executor.registry.names()


def test_in_memory_runtime_wiring_uses_domain_graph_and_write_scopes(tmp_path) -> None:
    project_root = tmp_path / "repo_test"
    domain = MemoryDomainContext(
        memory_domain_id="u_test",
        workspace_kind="project",
        stable_project_key=str(project_root),
    )

    wiring = build_in_memory_runtime_wiring(
        tmp_path,
        runtime_session_id=f"runtime:test:{uuid4().hex}",
        memory_domain=domain,
    )

    assert wiring.graph_id == "graph:user/u_test"
    assert wiring.memory_governance_executor.allowed_write_scopes == frozenset(
        {"ctx:user", workspace_scope(str(project_root))}
    )


def test_agent_runtime_wiring_threads_memory_domain_to_capability_context(tmp_path) -> None:
    project_root = tmp_path / "repo_test"
    domain = MemoryDomainContext(
        memory_domain_id="u_test",
        workspace_kind="project",
        stable_project_key=str(project_root),
    )
    settings = _settings_for_storage(StorageConfig(postgres_dsn="", oxigraph_url="http://127.0.0.1:1"))

    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=False,
        model_role=ModelRole.FLASH,
        memory_domain=domain,
        enable_workspace_skills=False,
    )

    assert wiring.agent_runtime.memory_domain == domain
    assert wiring.agent_runtime.workspace_kind == "project"
    # workspace_kind no longer influences the default; project gets bypass like everything else.
    assert wiring.agent_runtime.permission_policy.profile is PermissionProfile.TRUSTED_HOST
    assert wiring.agent_runtime.permission_policy.approval is ApprovalPolicy.NEVER
    assert wiring.agent_runtime.permission_policy.terminal is TerminalAccess.ALLOW


def test_agent_runtime_wiring_threads_permission_policy_to_session_registry(tmp_path) -> None:
    settings = _settings_for_storage(StorageConfig(postgres_dsn="", oxigraph_url="http://127.0.0.1:1"))
    policy = EffectivePermissionPolicy(
        profile=PermissionProfile.READ_ONLY,
        approval=ApprovalPolicy.ON_REQUEST,
        terminal=TerminalAccess.OFF,
    )

    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=False,
        model_role=ModelRole.FLASH,
        permission_policy=policy,
    )

    assert wiring.agent_runtime.permission_policy is policy
    assert "read_file" in wiring.agent_runtime.tool_executor.registry.names()
    # Visible-but-blocked: gate is the sole authority; tools stay registered
    # under read-only and are denied at call time, not hidden from the registry.
    assert "write_file" in wiring.agent_runtime.tool_executor.registry.names()
    assert "terminal" in wiring.agent_runtime.tool_executor.registry.names()


def test_in_memory_runtime_wiring_rejects_user_graph_without_domain(tmp_path) -> None:
    with pytest.raises(ValueError, match="graph:user"):
        build_in_memory_runtime_wiring(
            tmp_path,
            runtime_session_id=f"runtime:test:{uuid4().hex}",
            graph_id="graph:user/u_test",
        )


def test_durable_runtime_wiring_rejects_user_graph_without_domain(tmp_path) -> None:
    storage = StorageConfig(postgres_dsn="", oxigraph_url="http://127.0.0.1:1")

    with pytest.raises(ValueError, match="graph:user"):
        build_durable_runtime_wiring(
            _settings_for_storage(storage),
            tmp_path,
            runtime_session_id=f"runtime:test:{uuid4().hex}",
            graph_id="graph:user/u_test",
        )


def test_durable_runtime_wiring_uses_postgres_graph_event_log_and_artifacts(tmp_path) -> None:
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
        records = wiring.graph.find_by_type(rt.RUN_TIMELINE, graph_id=graph_id)
        timeline_blob_id = _artifact_id_from_node_ref(records[0][rt.STORED_AS.name]["@id"])
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
        assert records[0][rt.SOURCE_RUN.name] == ctx.run_id
        assert records[0][rt.SOURCE_SESSION.name] == runtime_session_id
        assert records[0][rt.STATUS.name] == "completed"
        assert timeline_blob_id.startswith(f"timeline:{runtime_session_id}:{ctx.run_id}:")
        assert "hello durable wiring" in wiring.archive.get_text(timeline_blob_id)
        assert summary.assistant_text == "hello durable wiring"
        with _connect_or_skip(storage.postgres_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT mutation_lane, target_entry_key, payload, status, governance_batch_id, decision_id
                    FROM memory_write_outbox
                    WHERE graph_id = %s
                    ORDER BY created_at DESC, outbox_id DESC
                    LIMIT 1
                    """,
                    (graph_id,),
                )
                row = cursor.fetchone()
        assert row is not None
        mutation_lane, target_entry_key, payload, status, governance_batch_id, decision_id = row
        assert mutation_lane == "runtime_semantic"
        assert target_entry_key == f"run-timeline:{runtime_session_id}:{ctx.run_id}"
        assert payload["kind"] == "canonical_mutation"
        assert payload["mutation_lane"] == "runtime_semantic"
        assert payload["surface_apply_status"] == {"oxigraph": "applied"}
        assert payload["source_runtime_session_id"] == runtime_session_id
        assert payload["source_run_id"] == ctx.run_id
        assert payload["source_artifact_ids"] == [timeline_blob_id]
        assert payload["mutation_lane"] == "runtime_semantic"
        assert "decision_id" not in payload
        assert status == "applied"
        assert governance_batch_id is None
        assert decision_id is None
    finally:
        wiring.graph.delete_graph(graph_id)
        _delete_postgres_artifacts_with_prefix(storage.postgres_dsn, f"timeline:{runtime_session_id}:{ctx.run_id}:")
        _delete_postgres_outbox_by_graph(storage.postgres_dsn, graph_id)
        _delete_postgres_runtime_session(storage.postgres_dsn, runtime_session_id)


def test_durable_runtime_wiring_replays_runtime_semantic_outbox_on_run_end(tmp_path) -> None:
    storage = StorageConfig.from_env()
    _connect_or_skip(storage.postgres_dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    ctx = _event_context("durable-outbox-replay")
    wiring = build_durable_runtime_wiring(
        _settings_for_storage(storage),
        tmp_path,
        runtime_session_id=runtime_session_id,
        graph_id=graph_id,
    )
    try:
        asyncio.run(_emit_timeline_events(wiring.runtime_session, ctx, "hello outbox replay"))
        with _connect_or_skip(storage.postgres_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status, payload
                    FROM memory_write_outbox
                    WHERE graph_id = %s AND mutation_lane = 'runtime_semantic'
                    ORDER BY created_at DESC, outbox_id DESC
                    LIMIT 1
                    """,
                    (graph_id,),
                )
                row = cursor.fetchone()
        assert row is not None
        status, payload = row
        assert status in {"applied", "partial"}
        assert payload["surface_apply_status"]["oxigraph"] == "applied"
    finally:
        wiring.graph.delete_graph(graph_id)
        _delete_postgres_artifacts_with_prefix(storage.postgres_dsn, f"timeline:{runtime_session_id}:{ctx.run_id}:")
        _delete_postgres_outbox_by_graph(storage.postgres_dsn, graph_id)
        _delete_postgres_runtime_session(storage.postgres_dsn, runtime_session_id)


def test_agent_runtime_wiring_uses_durable_runtime_wiring(tmp_path) -> None:
    storage = StorageConfig.from_env()
    _connect_or_skip(storage.postgres_dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    ctx = _event_context("agent-durable-wiring")
    wiring = build_agent_runtime_wiring(
        _settings_for_storage(storage),
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        runtime_session_id=runtime_session_id,
        graph_id=graph_id,
    )

    try:
        asyncio.run(_emit_timeline_events(wiring.runtime_wiring.runtime_session, ctx, "hello agent durable wiring"))
        events = wiring.runtime_wiring.event_log.iter(run_id=ctx.run_id)
        records = wiring.runtime_wiring.graph.find_by_type(rt.RUN_TIMELINE, graph_id=graph_id)
        timeline_blob_id = _artifact_id_from_node_ref(records[0][rt.STORED_AS.name]["@id"])
        timeline = load_run_timeline(
            graph=wiring.runtime_wiring.graph,
            archive=wiring.runtime_wiring.archive,
            run_id=ctx.run_id,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
        )
        summary = summarize_run_timeline(timeline)

        assert wiring.agent_runtime.runtime_session is wiring.runtime_wiring.runtime_session
        assert "memory_search" in wiring.agent_runtime.tool_executor.registry.names()
        assert "memory_get" in wiring.agent_runtime.tool_executor.registry.names()
        assert "memory_related" not in wiring.agent_runtime.tool_executor.registry.names()
        assert "memory_get" in wiring.agent_runtime.tool_executor.registry.names()
        assert [event.sequence for event in events] == [1, 2]
        assert len(records) == 1
        assert records[0][rt.SOURCE_SESSION.name] == runtime_session_id
        assert records[0][rt.STATUS.name] == "completed"
        assert timeline_blob_id.startswith(f"timeline:{runtime_session_id}:{ctx.run_id}:")
        assert "hello agent durable wiring" in wiring.runtime_wiring.archive.get_text(timeline_blob_id)
        assert summary.assistant_text == "hello agent durable wiring"
    finally:
        wiring.runtime_wiring.graph.delete_graph(graph_id)
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


def _delete_postgres_outbox_by_graph(dsn: str, graph_id: str) -> None:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from memory_write_outbox where graph_id = %s", (graph_id,))


def _artifact_id_from_node_ref(node_id: str) -> str:
    prefix = "urn:pulsara:"
    if node_id.startswith(prefix):
        return urllib.parse.unquote(node_id[len(prefix) :])
    return node_id
