from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.local_credentials import (
    CredentialState,
    CredentialStoreError,
    DashScopeEmbeddingCredential,
    DashScopeRerankCredential,
    InMemoryCredentialStore,
)
from pulsara_agent.retrieval.config import (
    EmbeddingBackendConfig,
    RerankBackendConfig,
)
from pulsara_agent.retrieval.embedding.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)


def _memory_port(credentials: InMemoryCredentialStore) -> KernelMemoryToolPort:
    return KernelMemoryToolPort(
        repository=SimpleNamespace(connection_provider=object()),
        session_id="session:test",
        read_binding=object(),
        embedding_config=EmbeddingBackendConfig(),
        rerank_config=RerankBackendConfig(),
        io_owner=object(),
        credentials=credentials,
    )


def test_embedding_and_rerank_credentials_are_independent() -> None:
    async def exercise() -> None:
        credentials = InMemoryCredentialStore()
        port = _memory_port(credentials)
        assert await port._embedding_provider() is None
        assert await port._rerank_provider() is None

        credentials.put(DashScopeEmbeddingCredential(), "embedding-secret")
        assert await port._embedding_provider() is not None
        assert await port._rerank_provider() is None

        credentials.put(DashScopeRerankCredential(), "rerank-secret")
        assert await port._rerank_provider() is not None
        await port.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("unavailable", "denied", "expected"),
    (
        (False, False, CredentialState.MISSING),
        (True, False, CredentialState.UNAVAILABLE),
        (False, True, CredentialState.DENIED),
    ),
)
def test_missing_or_blocked_retrieval_credentials_degrade_without_borrow(
    unavailable: bool,
    denied: bool,
    expected: CredentialState,
) -> None:
    async def exercise() -> None:
        credentials = InMemoryCredentialStore(
            unavailable=unavailable,
            denied=denied,
        )
        port = _memory_port(credentials)
        assert credentials.state(DashScopeEmbeddingCredential()) is expected
        assert credentials.state(DashScopeRerankCredential()) is expected
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
        assert credentials.borrow_count == 0
        await port.aclose()

    asyncio.run(exercise())


def test_retrieval_borrow_is_a_snapshot_across_replace_and_clear() -> None:
    credentials = InMemoryCredentialStore()
    key = DashScopeEmbeddingCredential()
    credentials.put(key, "first-secret")
    active = credentials.borrow(key)
    credentials.put(key, "second-secret")
    assert active.value == "first-secret"
    with credentials.borrow(key) as next_operation:
        assert next_operation.value == "second-secret"
    credentials.delete(key)
    assert active.value == "first-secret"
    active.close()
    with pytest.raises(CredentialStoreError) as raised:
        credentials.borrow(key)
    assert raised.value.state is CredentialState.MISSING


def test_embedding_operation_borrows_once_and_next_operation_reads_replacement(
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
        credentials = InMemoryCredentialStore()
        key = DashScopeEmbeddingCredential()
        credentials.put(key, "first-secret")
        provider = OpenAICompatibleEmbeddingProvider(
            model="text-embedding-v4",
            base_url="https://dashscope.example/v1",
            credentials=credentials,
        )

        first = asyncio.create_task(provider.embed("first operation"))
        await started.wait()
        credentials.put(key, "second-secret")
        release.set()
        assert len(await first) == 1024
        assert credentials.borrow_count == 1

        started.clear()
        release.clear()
        second = asyncio.create_task(provider.embed("second operation"))
        await started.wait()
        release.set()
        assert len(await second) == 1024
        assert credentials.borrow_count == 2
        assert observed_keys == ["first-secret", "second-secret"]

    asyncio.run(exercise())
