"""DashScope / Bailian rerank provider for ``qwen3-rerank``."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import httpx

from pulsara_agent.retrieval.errors import RerankServiceError

from .protocol import RerankResult


class DashScopeRerankProvider:
    """Call Bailian's native qwen3-rerank endpoint."""

    _PATH = "/compatible-api/v1/reranks"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        batch_size: int = 50,
        max_concurrent: int = 4,
    ) -> None:
        self.model_id = model
        self._model = model
        self._url = f"{base_url.rstrip('/')}{self._PATH}"
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._batch_size = batch_size
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        instruction: str | None = None,
        top_n: int | None = None,
    ) -> list[RerankResult]:
        if not documents:
            return []

        chunks: list[tuple[int, list[str]]] = [
            (offset, list(documents[offset : offset + self._batch_size]))
            for offset in range(0, len(documents), self._batch_size)
        ]
        results = await asyncio.gather(
            *(
                self._score_chunk(
                    query,
                    docs,
                    instruction=instruction,
                    top_n=min(top_n, len(docs)) if top_n is not None else None,
                )
                for _, docs in chunks
            )
        )
        merged: list[RerankResult] = []
        for (offset, _docs), chunk_results in zip(chunks, results, strict=True):
            merged.extend(
                RerankResult(index=offset + item.index, score=item.score)
                for item in chunk_results
            )
        merged.sort(key=lambda item: item.score, reverse=True)
        if top_n is not None:
            return merged[:top_n]
        return merged

    async def _score_chunk(
        self,
        query: str,
        documents: list[str],
        *,
        instruction: str | None,
        top_n: int | None,
    ) -> list[RerankResult]:
        payload: dict[str, Any] = {
            "model": self._model,
            "query": query,
            "documents": documents,
        }
        if top_n is not None:
            payload["top_n"] = top_n
        if instruction:
            payload["instruct"] = instruction

        async with self._semaphore:
            for attempt in range(self._max_retries + 1):
                try:
                    response = await self._http_client().post(self._url, json=payload)
                except httpx.HTTPError as exc:
                    if attempt == self._max_retries:
                        raise RerankServiceError(
                            f"DashScope rerank transport failure: {exc}"
                        ) from exc
                    await asyncio.sleep(_retry_delay_seconds(None, attempt))
                    continue

                if response.status_code == 200:
                    return _parse_qwen3_rerank_response(response.json())
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == self._max_retries:
                        raise RerankServiceError(
                            f"DashScope rerank HTTP {response.status_code}: {response.text[:200]}"
                        )
                    await asyncio.sleep(_retry_delay_seconds(response, attempt))
                    continue
                raise RerankServiceError(
                    f"DashScope rerank HTTP {response.status_code}: {response.text[:200]}"
                )

        raise RerankServiceError(
            f"DashScope rerank exhausted retries ({self._max_retries})"
        )

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_seconds,
                headers=self._headers,
            )
        return self._client


def _parse_qwen3_rerank_response(payload: dict[str, Any]) -> list[RerankResult]:
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise RerankServiceError(f"DashScope rerank response missing results: {payload!r}")
    parsed: list[RerankResult] = []
    for row in rows:
        try:
            parsed.append(
                RerankResult(
                    index=int(row["index"]),
                    score=float(row["relevance_score"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankServiceError(f"Malformed rerank result row: {row!r}") from exc
    parsed.sort(key=lambda item: item.score, reverse=True)
    return parsed


def _retry_delay_seconds(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 5.0)
            except ValueError:
                pass
    return min(0.25 * (2**attempt), 5.0)
