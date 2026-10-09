from __future__ import annotations

import asyncio
from types import SimpleNamespace
import httpx2
import pytest

from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.retrieval.config import EmbeddingBackendConfig, RerankBackendConfig
from pulsara_agent.settings import LocalSettingsStore
from tests.retrieval_fixtures import save_model
from tests.test_memory_retrieval_adapters import VECTOR


def _memory_port(settings):
    return KernelMemoryToolPort(
        repository=SimpleNamespace(connection_provider=object()),
        session_id="session:test",
        read_binding=object(),
        embedding_config=EmbeddingBackendConfig(),
        rerank_config=RerankBackendConfig(),
        io_owner=object(),
        settings=settings,
    )


def test_embedding_and_rerank_slots_are_independent_and_switches_keep_config(tmp_path):
    async def exercise():
        settings = LocalSettingsStore(tmp_path / "local-settings.yaml")
        port = _memory_port(settings)
        assert await port._embedding_provider() is None
        assert await port._rerank_provider() is None
        await save_model(settings, "embedding", "embedding-secret", activate=True)
        assert await port._embedding_provider() is not None
        assert await port._rerank_provider() is None
        await save_model(settings, "rerank", "rerank-secret", activate=True)
        assert await port._rerank_provider() is not None
        await settings.set_retrieval_mode(embedding_enabled=False)
        await settings.set_retrieval_mode(ranking_mode="off")
        assert settings.read().memory_retrieval.embedding.api_key == "embedding-secret"
        assert settings.read().memory_retrieval.rerank.api_key == "rerank-secret"
        assert await port._embedding_provider() is None
        assert await port._rerank_provider() is None
        await port.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("unreadable", (False, True))
def test_missing_or_unreadable_settings_degrade_without_opening_provider(
    tmp_path, unreadable
):
    async def exercise():
        path = tmp_path / "local-settings.yaml"
        if unreadable:
            path.write_text("schema: [\n")
        port = _memory_port(LocalSettingsStore(path))
        assert await port._embedding_provider() is None
        assert await port._rerank_provider() is None
        result = SimpleNamespace(facts=("original",))
        assert (
            await port._rerank_explicit("query", result, total_deadline=999999999)
            is result
        )
        await port.aclose()

    asyncio.run(exercise())


def test_frozen_request_replacement_and_late_result_discard(tmp_path, monkeypatch):
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def respond(request):
            observed_keys.append(request.headers["authorization"])
            started.set()
            await release.wait()
            return httpx2.Response(
                200, json={"data": [{"index": 0, "embedding": VECTOR}]}
            )

        def injected(**kwargs):
            from pulsara_agent.process_credential_boundary import (
                ProcessCredentialBoundHttpx2Client,
            )

            return ProcessCredentialBoundHttpx2Client(
                **kwargs, transport=httpx2.MockTransport(respond), trust_env=False
            )

        from pulsara_agent.llm.adapters.openai import client

        monkeypatch.setattr(client, "ProcessCredentialBoundHttpx2Client", injected)
        settings = LocalSettingsStore(tmp_path / "settings.yaml")
        port = _memory_port(settings)
        await save_model(settings, "embedding", "first-secret", activate=True)
        provider = await port._embedding_provider()
        first = asyncio.create_task(port._frozen_query_embedding(provider, "first"))
        await started.wait()
        await save_model(settings, "embedding", "second-secret")
        release.set()
        assert await first is None
        second_provider = await port._embedding_provider()
        second = await port._frozen_query_embedding(second_provider, "second")
        assert second.vector == tuple(VECTOR)
        assert observed_keys == ["Bearer first-secret", "Bearer second-secret"]
        # Frozen operation still retains its own key; only a new operation uses replacement.
        assert provider.connection.api_key == "first-secret"
        await port.aclose()

    observed_keys = []
    asyncio.run(exercise())


def test_embedding_write_and_settings_publication_share_mutation_lane(tmp_path):
    async def exercise():
        settings = LocalSettingsStore(tmp_path / "settings.yaml")
        await save_model(settings, "embedding", "key", activate=True)
        old = settings.read().memory_retrieval.embedding
        entered, release = asyncio.Event(), asyncio.Event()

        async def install():
            entered.set()
            await release.wait()
            return True

        write = asyncio.create_task(settings.install_embedding_if_current(old, install))
        await entered.wait()
        change = asyncio.create_task(
            save_model(settings, "embedding", "new-key", model="new-model")
        )
        await asyncio.sleep(0)
        assert not change.done()
        release.set()
        assert await write is True
        await change
        called = False

        async def forbidden():
            nonlocal called
            called = True

        assert await settings.install_embedding_if_current(old, forbidden) is False
        assert called is False

    asyncio.run(exercise())
