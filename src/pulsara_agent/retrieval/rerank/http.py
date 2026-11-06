"""The two ordinary rerank request shapes using the provider SDK."""

import asyncio
import json
import math

from pulsara_agent.llm.adapters.openai.client import (
    admit_provider_request,
    openai_auth_request_options,
    prepare_openai_sdk,
)
from pulsara_agent.retrieval.config import RerankBackendConfig
from pulsara_agent.retrieval.errors import RerankServiceError
from pulsara_agent.retrieval.http import sdk_client
from .protocol import RerankPurpose, RerankResult

MAXIMUM_RERANK_RESPONSE_BYTES = 256 * 1024


class HttpRerankProvider:
    def __init__(self, connection, *, config=RerankBackendConfig(), semaphore=None):
        if (
            connection.shape
            not in {"flat_rerank", "nested_rerank"}
            or not connection.complete
        ):
            raise ValueError("rerank configuration is invalid")
        self.connection = connection
        self.config = config
        self.model_id = connection.model_id
        self._semaphore = semaphore or asyncio.Semaphore(config.max_concurrent)

    async def aclose(self):
        return None

    async def rerank(
        self, query, documents, *, candidate_ids=None, purpose: RerankPurpose = "recall"
    ):
        documents = tuple(documents)
        if not documents:
            return []
        if len(documents) > 20:
            raise RerankServiceError("rerank candidate count exceeds 20")
        # Call-local names also support connection tests without canonical facts.
        names = (
            tuple(candidate_ids)
            if candidate_ids is not None
            else tuple(f"m{i}" for i in range(len(documents)))
        )
        if (
            len(names) != len(documents)
            or len(set(names)) != len(names)
            or any(not isinstance(n, str) or not n for n in names)
        ):
            raise RerankServiceError("candidate identities are invalid")
        shape = self.connection.shape
        payload = {"model": self.model_id}
        if shape == "flat_rerank":
            payload.update(query=query, documents=list(documents))
        elif shape == "nested_rerank":
            payload["input"] = {"query": query, "documents": list(documents)}
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 192 * 1024:
            raise RerankServiceError("rerank request exceeds its byte bound")
        async with self._semaphore:
            await prepare_openai_sdk()
            import openai

            client, boundary = sdk_client(
                self.connection,
                timeout_seconds=self.config.timeout_seconds,
                maximum_response_bytes=MAXIMUM_RERANK_RESPONSE_BYTES,
            )
            try:
                async with client:
                    async with asyncio.timeout(self.config.timeout_seconds):
                        options = openai_auth_request_options(
                            requires_api_key=self.connection.authentication != "none"
                        )
                        decoded = await admit_provider_request(
                            credential_boundary=boundary,
                            payload=payload,
                            operation=lambda: client.post(
                                self.connection.endpoint,
                                cast_to=dict,
                                body=payload,
                                options={"headers": options.get("extra_headers", {})},
                            ),
                        )
            except (ValueError, openai.OpenAIError, TimeoutError) as exc:
                message = (
                    str(exc).replace(self.connection.api_key, "[REDACTED]")
                    if self.connection.api_key
                    else str(exc)
                )
                raise RerankServiceError(f"rerank request failed: {message}") from None
        try:
            rows = self._parse(decoded, names)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise RerankServiceError(f"rerank response invalid: {exc}") from exc
        return sorted(rows, key=lambda row: (-row.score, row.index))

    def _parse(self, decoded, names):
        if not isinstance(decoded, dict):
            raise ValueError("expected an object")
        rows = []
        positions = [decoded[key] for key in ("results", "data") if key in decoded]
        output = decoded.get("output")
        if isinstance(output, dict) and "results" in output:
            positions.append(output["results"])
        if len(positions) != 1 or not isinstance(positions[0], list):
            raise ValueError("expected exactly one supported result array")
        for item in positions[0]:
            index = item["index"]
            if type(index) is not int:
                raise ValueError("index must be an integer")
            rows.append(RerankResult(index, number(item["relevance_score"])))
        indices = [row.index for row in rows]
        if (
            len(indices) != len(names)
            or len(set(indices)) != len(indices)
            or set(indices) != set(range(len(names)))
        ):
            raise ValueError("response must cover every candidate exactly once")
        return rows


def number(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError("score must be a finite number")
    return float(value)
