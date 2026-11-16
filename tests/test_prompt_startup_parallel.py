"""Startup concurrency must preserve results and own every cancelled worker."""
from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.memory.scope import MemoryDomainContext, freeze_memory_read_context_binding
from tests.retrieval_fixtures import save_model
from pulsara_agent.retrieval.config import EmbeddingBackendConfig
from pulsara_agent.settings import LocalSettingsStore


@pytest.mark.parametrize("cancel", [False, True])
def test_embedding_overlaps_sparse_and_history_and_cancellation_drains_all(tmp_path, cancel):
    async def scenario():
        io = KernelSessionIO()
        port = KernelMemoryToolPort(
            repository=SimpleNamespace(connection_provider=None), session_id='session:dff7c3a6mm2a62dhz64vsis7u4',
            read_binding=freeze_memory_read_context_binding(
                domain=MemoryDomainContext("u_local", "transient"), host_workspace_id='workspace:3yjy5wqzn4db25trsruald6dbi',
            ), embedding_config=EmbeddingBackendConfig(), io_owner=io,
            settings=LocalSettingsStore(tmp_path / "settings.yaml"),
        )
        await save_model(port._settings, "embedding", "test-key", activate=True)
        embedding_started, sparse_started, release_search, sparse_done = Event(), Event(), Event(), Event()
        embedding_done = asyncio.Event()

        def sparse(**kwargs):
            sparse_started.set()
            try:
                assert embedding_started.wait(5), "remote request waited for local sparse search"
                if cancel:
                    assert release_search.wait(5)
                assert kwargs["query"] == "请测试一下runtime的启动速度"
                return ()
            finally:
                sparse_done.set()

        async def embed(_text):
            embedding_started.set()
            try:
                if cancel:
                    await asyncio.Event().wait()
                return [1.0]
            finally:
                embedding_done.set()

        async def provider():
            return SimpleNamespace(embed=embed)

        history_started, history_done = Event(), Event()
        def history(**kwargs):
            history_started.set()
            try:
                assert embedding_started.wait(5), "remote request waited for history"
                if cancel:
                    assert release_search.wait(5)
                return ()
            finally:
                history_done.set()

        from pulsara_agent.model_input.contracts import ContextBindingBaseKind
        canonical_facts = SimpleNamespace(
            context_binding_fact=SimpleNamespace(base_kind=ContextBindingBaseKind.FULL_HISTORY),
            canonical_input=SimpleNamespace(items=()),
        )
        port._history.automatic_candidates = history
        port._query.sparse_candidates = sparse
        port._query.canonical_refetch = lambda **kwargs: ()
        port._embedding_provider = provider

        task = asyncio.create_task(port._mixed_candidates("请测试一下runtime的启动速度", canonical_facts))
        try:
            if cancel:
                assert await asyncio.to_thread(sparse_started.wait, 5)
                assert await asyncio.to_thread(embedding_started.wait, 5)
                assert await asyncio.to_thread(history_started.wait, 5)
                task.cancel()
                release_search.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert sparse_done.is_set()
                assert history_done.is_set()
                assert embedding_done.is_set()
                assert not port._remote_tasks
            else:
                result = await asyncio.wait_for(task, 5)
                assert result == ()
                assert history_done.is_set()
        finally:
            release_search.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await port.aclose()
            await io.aclose(deadline_monotonic=monotonic()+10)

    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize("failure", [False, True])
def test_real_admission_installs_completed_memory_candidate_without_detached_tasks(
    tmp_path: Path, monkeypatch, stage2_migrated_postgres_database, failure,
):
    import pulsara_agent.conversation_kernel.host as host
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_binding, test_model_runtime
    from tests.test_stage2_kernel_host_dogfood import _DogfoodModelPort

    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(host, "DirectKernelModelPort", _DogfoodModelPort)
    monkeypatch.setattr(host.LocalMcpManagementService, "load_configs", lambda *a, **kw: ())
    runtime = test_model_runtime(postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)

    async def scenario():
        core = host.KernelHostCore.production(model_runtime=runtime)
        try:
            session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
            await session.update_model_call_binding(test_model_binding(runtime))
            recall_started, recall_done = Event(), Event()
            recall_calls = 0

            async def recall(_text, *, previous_state, canonical_facts, trigger_policy=None):
                nonlocal recall_calls
                recall_calls += 1
                recall_started.set()
                try:
                    from pulsara_agent.conversation_kernel.memory.contracts import MemoryObservationCandidate,MemoryObservationText
                    return MemoryObservationCandidate((MemoryObservationText("读取失败") ,),None) if failure else MemoryObservationCandidate((),previous_state)
                finally:
                    recall_done.set()

            assembler = session._runner._provider_dispatch._cold_epoch_assembler
            original = assembler.prepare_semantic

            def compile_input(**kwargs):
                assert recall_started.is_set(), "memory candidate must be complete before final compilation"
                return original(**kwargs)

            monkeypatch.setattr(session._memory_tools, "prepare_memory_observation", recall)
            monkeypatch.setattr(assembler, "prepare_semantic", compile_input)
            result=await session.run_turn(PromptContent.text("请测试消息启动准备"))
            assert result.final_text == "STAGE2_DOGFOOD_OK"
            assert recall_calls == 1
            assert recall_done.is_set()
        finally:
            await core.shutdown()

    asyncio.run(scenario())
