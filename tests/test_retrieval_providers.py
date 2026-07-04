from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from pulsara_agent.retrieval.embedding.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)
from pulsara_agent.retrieval.rerank.dashscope import DashScopeRerankProvider


def test_embedding_provider_preserves_input_order_when_api_reorders_results() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        model="text-embedding-v4",
        api_key="test-key",
        base_url="https://example.com/v1",
        dimensions=3,
        batch_size=3,
    )

    async def fake_create(**_kwargs):
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=2, embedding=[2.0, 2.0, 2.0]),
                SimpleNamespace(index=0, embedding=[0.0, 0.0, 0.0]),
                SimpleNamespace(index=1, embedding=[1.0, 1.0, 1.0]),
            ]
        )

    provider._client = SimpleNamespace(  # type: ignore[assignment]
        embeddings=SimpleNamespace(create=fake_create)
    )

    vectors = asyncio.run(provider.embed_batch(["zero", "one", "two"]))

    assert vectors == [
        [0.0, 0.0, 0.0],
        [1.0, 1.0, 1.0],
        [2.0, 2.0, 2.0],
    ]


def test_rerank_provider_retries_429_with_retry_after(monkeypatch) -> None:
    provider = DashScopeRerankProvider(
        model="qwen3-rerank",
        api_key="test-key",
        base_url="https://dashscope.aliyuncs.com",
        max_retries=1,
    )
    calls = 0
    sleeps: list[float] = []

    async def fake_post(_url: str, *, json: dict):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0.01"},
                json={"message": "rate limited"},
            )
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.75}]},
        )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    provider._client = SimpleNamespace(post=fake_post)  # type: ignore[assignment]
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = asyncio.run(provider.rerank("query", ["document"]))

    assert result[0].index == 0
    assert result[0].score == 0.75
    assert calls == 2
    assert sleeps == [0.01]


def test_embedding_provider_runs_concurrent_chunks_on_one_event_loop() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        model="text-embedding-v4",
        api_key="test-key",
        base_url="https://example.com/v1",
        dimensions=3,
        batch_size=1,
        max_concurrent=2,
    )
    active = 0
    max_active = 0

    async def fake_create(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            value = float(kwargs["input"][0])
            return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[value] * 3)])
        finally:
            active -= 1

    provider._client = SimpleNamespace(  # type: ignore[assignment]
        embeddings=SimpleNamespace(create=fake_create)
    )

    vectors = asyncio.run(provider.embed_batch(["1", "2", "3"]))

    assert vectors == [[1.0] * 3, [2.0] * 3, [3.0] * 3]
    assert max_active == 2


def test_rerank_provider_runs_concurrent_chunks_through_shared_client() -> None:
    provider = DashScopeRerankProvider(
        model="qwen3-rerank",
        api_key="test-key",
        base_url="https://dashscope.aliyuncs.com",
        batch_size=1,
        max_concurrent=2,
    )
    active = 0
    max_active = 0

    async def fake_post(_url: str, *, json: dict):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            return httpx.Response(
                200,
                json={"results": [{"index": 0, "relevance_score": float(json["documents"][0])}]},
            )
        finally:
            active -= 1

    provider._client = SimpleNamespace(post=fake_post)  # type: ignore[assignment]

    result = asyncio.run(provider.rerank("query", ["1", "2", "3"]))

    assert [item.score for item in result] == [3.0, 2.0, 1.0]
    assert max_active == 2
