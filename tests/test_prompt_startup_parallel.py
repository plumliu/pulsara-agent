"""Startup concurrency must preserve results and own every cancelled worker."""
from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.context_sources import build_memory_context_source
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.memory.recall import MemoryQueryResult, MemoryRetrievalDisposition
from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.memory.scope import MemoryDomainContext, freeze_memory_read_context_binding
from pulsara_agent.model_input.contracts import ContextSourceKind, ContextSourceAbsenceKind
from pulsara_agent.retrieval.config import EmbeddingBackendConfig
from pulsara_agent.settings import LocalSettingsStore


@pytest.mark.parametrize("cancel", [False, True])
def test_embedding_overlaps_tokenizer_and_cancellation_drains_both(tmp_path, cancel):
    async def scenario():
        io = KernelSessionIO()
        port = KernelMemoryToolPort(
            repository=SimpleNamespace(connection_provider=None), session_id="session:parallel",
            read_binding=freeze_memory_read_context_binding(
                domain=MemoryDomainContext("u_local", "transient"), host_workspace_id="workspace:parallel",
            ), embedding_config=EmbeddingBackendConfig(), io_owner=io,
            settings=LocalSettingsStore(tmp_path / "settings.yaml"),
        )
        embedding_started, tokenize_started, release_tokenize, tokenize_done = Event(), Event(), Event(), Event()
        embedding_done = asyncio.Event()

        def tokenize(_text):
            tokenize_started.set()
            try:
                assert embedding_started.wait(5), "remote request waited for tokenization"
                if cancel:
                    assert release_tokenize.wait(5)
                return ("runtime",)
            finally:
                tokenize_done.set()

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

        async def recall(**kwargs):
            assert kwargs["terms"] == ("runtime",)
            assert kwargs["query_embedding"] == [1.0]
            return MemoryQueryResult(MemoryRetrievalDisposition.NO_MATCH, (), ())

        port._query.tokenize_query = tokenize
        port._embedding_provider = provider
        port._parallel_recall = recall
        task = asyncio.create_task(port.freeze_automatic_recall_source("请测试一下runtime的启动速度"))
        try:
            if cancel:
                assert await asyncio.to_thread(tokenize_started.wait, 5)
                assert await asyncio.to_thread(embedding_started.wait, 5)
                task.cancel()
                release_tokenize.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert tokenize_done.is_set()
                assert embedding_done.is_set()
                assert not port._remote_tasks
            else:
                result = await asyncio.wait_for(task, 5)
                assert result.absence_kind is ContextSourceAbsenceKind.EXPLICIT_EMPTY
        finally:
            release_tokenize.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await port.aclose()
            await io.aclose(deadline_monotonic=monotonic()+10)

    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize("failure", [False, True])
def test_real_admission_overlaps_compile_and_recall_without_detached_tasks(
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

            async def recall(_text):
                nonlocal recall_calls
                recall_calls += 1
                recall_started.set()
                try:
                    if failure:
                        await asyncio.Event().wait()
                    return build_memory_context_source(kind=ContextSourceKind.MEMORY_RECALL,
                        texts=None, absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY)
                finally:
                    recall_done.set()

            assembler = session._runner._provider_dispatch._cold_epoch_assembler
            original = assembler.prepare_semantic

            def compile_input(**kwargs):
                assert recall_started.wait(5), "recall waited for base compilation"
                if failure:
                    raise ValueError("test base compilation failure")
                return original(**kwargs)

            monkeypatch.setattr(session._memory_tools, "freeze_automatic_recall_source", recall)
            monkeypatch.setattr(assembler, "prepare_semantic", compile_input)
            if failure:
                with pytest.raises(ValueError, match="test base compilation failure"):
                    await session.run_turn(PromptContent.text("请测试消息启动并行准备"))
            else:
                result = await session.run_turn(PromptContent.text("请测试消息启动并行准备"))
                assert result.final_text == "STAGE2_DOGFOOD_OK"
            assert recall_calls == 1
            assert recall_done.is_set()
        finally:
            await core.shutdown()

    asyncio.run(scenario())
