from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pulsara_agent.retrieval import build_embedding_provider, build_rerank_provider
from pulsara_agent.settings import PulsaraSettings


pytestmark = pytest.mark.retrieval_live


def _load_settings() -> PulsaraSettings:
    env_file = Path(".env")
    if env_file.exists():
        return PulsaraSettings.from_env_file(env_file)
    return PulsaraSettings.from_env()


def test_live_bailian_embedding_api_smoke() -> None:
    settings = _load_settings()
    if not settings.retrieval.embedding.api_key:
        pytest.skip("Set PULSARA_EMBEDDING_API_KEY to run live embedding smoke.")

    async def _run() -> tuple[int, list[float]]:
        provider = build_embedding_provider(settings.retrieval.embedding)
        try:
            vector = await provider.embed("Pulsara live embedding smoke test")
            return len(vector), vector[:5]
        finally:
            await provider.aclose()

    dimensions, head = asyncio.run(_run())

    assert dimensions == settings.retrieval.embedding.dimensions
    assert any(abs(value) > 0 for value in head)


def test_live_bailian_rerank_api_smoke() -> None:
    settings = _load_settings()
    if not settings.retrieval.rerank.api_key:
        pytest.skip("Set PULSARA_RERANK_API_KEY to run live rerank smoke.")

    async def _run():
        provider = build_rerank_provider(settings.retrieval.rerank)
        try:
            return await provider.rerank(
                "Which passage is about memory recall?",
                [
                    "This document explains terminal output buffering and PTY behavior.",
                    "This note discusses memory recall, retrieval, and reranking in long-horizon agents.",
                    "This page is about CSS layout and responsive design.",
                ],
            )
        finally:
            await provider.aclose()

    results = asyncio.run(_run())

    assert results
    assert results[0].index == 1
    assert results[0].score >= results[-1].score
