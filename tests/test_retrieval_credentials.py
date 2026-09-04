from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.retrieval.config import (
    EmbeddingBackendConfig,
    RerankBackendConfig,
)
from pulsara_agent.retrieval.embedding.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)
from pulsara_agent.settings import LocalSettingsStore


def _memory_port(settings: LocalSettingsStore) -> KernelMemoryToolPort:
    return KernelMemoryToolPort(
        repository=SimpleNamespace(connection_provider=object()),
        session_id="session:test",
        read_binding=object(),
        embedding_config=EmbeddingBackendConfig(),
        rerank_config=RerankBackendConfig(),
        io_owner=object(),
        settings=settings,
    )


def test_embedding_and_rerank_keys_are_independent(tmp_path: Path) -> None:
    async def exercise() -> None:
        settings = LocalSettingsStore(tmp_path / "local-settings.yaml")
        port = _memory_port(settings)
        assert await port._embedding_provider() is None
        assert await port._rerank_provider() is None

        await settings.save_dashscope_api_key("embedding", "embedding-secret")
        assert await port._embedding_provider() is not None
        assert await port._rerank_provider() is None

        await settings.save_dashscope_api_key("rerank", "rerank-secret")
        assert await port._rerank_provider() is not None
        await port.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("unreadable", (False, True))
def test_missing_or_unreadable_retrieval_settings_degrade_without_opening_provider(
    tmp_path: Path,
    unreadable: bool,
) -> None:
    async def exercise() -> None:
        path = tmp_path / "local-settings.yaml"
        if unreadable:
            path.write_text("schema: [\n", encoding="utf-8")
        settings = LocalSettingsStore(path)
        port = _memory_port(settings)
        assert await port._embedding_provider() is None
        assert await port._rerank_provider() is None
        pre_rerank = SimpleNamespace(facts=("pre-rerank-result",))
        assert (
            await port._rerank_explicit(
                "query",
                pre_rerank,
                total_deadline=999_999_999.0,
            )
            is pre_rerank
        )
        await port.aclose()

    asyncio.run(exercise())


def test_retrieval_operation_reads_once_and_next_operation_uses_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.retrieval.embedding.openai_compatible as embedding_module

    started = asyncio.Event()
    release = asyncio.Event()
    observed_keys: list[str] = []

    class _Embeddings:
        def __init__(self, key: str) -> None:
            self._key = key

        async def create(self, **_kwargs):
            observed_keys.append(self._key)
            started.set()
            await release.wait()
            vector = [1.0, *([0.0] * 1023)]
            return SimpleNamespace(
                model="text-embedding-v4",
                data=(SimpleNamespace(index=0, embedding=vector),),
            )

    class _Client:
        def __init__(self, *, api_key: str, http_client, **_kwargs) -> None:
            self.embeddings = _Embeddings(api_key)
            self._http_client = http_client

        async def close(self) -> None:
            await self._http_client.aclose()

    monkeypatch.setattr(embedding_module.openai, "AsyncOpenAI", _Client)

    async def exercise() -> None:
        settings = LocalSettingsStore(tmp_path / "local-settings.yaml")
        await settings.save_dashscope_api_key("embedding", "first-secret")
        provider = OpenAICompatibleEmbeddingProvider(
            model="text-embedding-v4",
            base_url="https://dashscope.example/v1",
            settings=settings,
        )

        first = asyncio.create_task(provider.embed("first operation"))
        await started.wait()
        await settings.save_dashscope_api_key("embedding", "second-secret")
        release.set()
        assert len(await first) == 1024

        started.clear()
        release.clear()
        second = asyncio.create_task(provider.embed("second operation"))
        await started.wait()
        release.set()
        assert len(await second) == 1024
        assert observed_keys == ["first-secret", "second-secret"]

    asyncio.run(exercise())
