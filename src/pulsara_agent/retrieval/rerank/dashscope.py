"""Bounded DashScope-compatible ``qwen3-rerank`` client."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import httpx

from pulsara_agent.retrieval.errors import RerankServiceError

from .protocol import RerankResult


MAXIMUM_RERANK_RESPONSE_BYTES = 256 * 1024


class DashScopeRerankProvider:
    _PATH = "/compatible-api/v1/reranks"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        max_retries: int,
        maximum_concurrent: int,
    ) -> None:
        if model != "qwen3-rerank":
            raise ValueError("rerank model is outside the V1 contract")
        if maximum_concurrent != 1:
            raise ValueError("Round 8 rerank client is single-flight")
        self.model_id = model
        self._url = f"{base_url.rstrip('/')}{self._PATH}"
        self._model = model
        self._max_retries = max(0, max_retries)
        self._semaphore = asyncio.Semaphore(1)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def rerank(
        self,
        query: str,
        documents,
        *,
        instruction: str | None = None,
        top_n: int | None = None,
    ) -> list[RerankResult]:
        values = tuple(str(item) for item in documents)
        if not values:
            return []
        if len(values) > 20:
            raise RerankServiceError("rerank candidate count exceeds 20")
        requested = len(values) if top_n is None else int(top_n)
        if requested != len(values):
            raise RerankServiceError("V1 rerank requires the complete candidate set")
        payload: dict[str, Any] = {
            "model": self._model,
            "query": query,
            "documents": list(values),
            "top_n": requested,
        }
        if instruction:
            payload["instruct"] = instruction
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > 192 * 1024:
            raise RerankServiceError("rerank request exceeds its aggregate bound")
        async with self._semaphore:
            response: httpx.Response | None = None
            body: bytes | None = None
            for attempt in range(self._max_retries + 1):
                try:
                    request = self._client.build_request(
                        "POST",
                        self._url,
                        content=encoded,
                    )
                    response = await self._client.send(request, stream=True)
                except httpx.HTTPError as exc:
                    if attempt >= self._max_retries:
                        raise RerankServiceError("rerank transport failed") from exc
                    continue
                try:
                    if response.status_code == 200:
                        body = await _bounded_response_body(response)
                        break
                    if response.status_code != 429 and response.status_code < 500:
                        raise RerankServiceError(
                            f"rerank request failed with HTTP {response.status_code}"
                        )
                    if attempt >= self._max_retries:
                        raise RerankServiceError(
                            f"rerank request failed with HTTP {response.status_code}"
                        )
                finally:
                    await response.aclose()
            if body is None:
                raise RerankServiceError("rerank response body is absent")
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RerankServiceError("rerank response is malformed") from exc
        if not isinstance(decoded, dict) or not isinstance(decoded.get("results"), list):
            raise RerankServiceError("rerank response lacks results")
        rows: list[RerankResult] = []
        seen: set[int] = set()
        for value in decoded["results"]:
            if not isinstance(value, dict):
                raise RerankServiceError("rerank result row is malformed")
            try:
                index = int(value["index"])
                score = float(value["relevance_score"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RerankServiceError("rerank result row is malformed") from exc
            if index < 0 or index >= len(values) or index in seen or not math.isfinite(score):
                raise RerankServiceError("rerank result identity is invalid")
            seen.add(index)
            rows.append(RerankResult(index, score))
        if seen != set(range(len(values))):
            raise RerankServiceError("rerank response omitted candidates")
        rows.sort(key=lambda item: (-item.score, item.index))
        return rows


async def _bounded_response_body(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > MAXIMUM_RERANK_RESPONSE_BYTES:
            raise RerankServiceError("rerank response exceeds its byte bound")
        body.extend(chunk)
    return bytes(body)


__all__ = ["DashScopeRerankProvider"]
