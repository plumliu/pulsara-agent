"""Two rerank request shapes and two decision protocols using one SDK."""

import asyncio
import json
import math
import openai

from pulsara_agent.llm.adapters.openai.client import (
    admit_provider_request,
    openai_auth_request_options,
)
from pulsara_agent.retrieval.config import RerankBackendConfig
from pulsara_agent.retrieval.errors import RerankServiceError
from pulsara_agent.retrieval.http import sdk_client
from .decision_prompt import decision_question
from .protocol import RerankPurpose, RerankResult

MAXIMUM_RERANK_RESPONSE_BYTES = 256 * 1024


class HttpRerankProvider:
    def __init__(self, connection, *, config=RerankBackendConfig(), semaphore=None):
        if (
            connection.shape
            not in {"flat_rerank", "nested_rerank", "system_one", "openai_decisions"}
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
        else:
            state = {
                "user_task": query,
                "candidates": [
                    {"memory_id": name, "memory": text}
                    for name, text in zip(names, documents, strict=True)
                ],
            }
            questions = {
                name: decision_question(name, purpose)
                for name in names
            }
            if shape == "system_one":
                payload.update(
                    state=state,
                    questions={
                        name: {
                            "type": "noul",
                            "instructions": questions[name][0],
                            "criteria": {"true": questions[name][1], "false": questions[name][2]},
                        }
                        for name in names
                    },
                )
            else:
                payload.update(
                    input=json.dumps(state, ensure_ascii=False),
                    questions=[
                        {
                            "type": "predicate",
                            "name": name,
                            "instructions": questions[name][0],
                        }
                        for name in names
                    ],
                )
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 192 * 1024:
            raise RerankServiceError("rerank request exceeds its byte bound")
        async with self._semaphore:
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
                        if shape == "openai_decisions":
                            response = await admit_provider_request(
                                credential_boundary=boundary,
                                payload=payload,
                                operation=lambda: client.decisions.create(
                                    **payload, **options
                                ),
                            )
                            decoded = response.model_dump()
                        else:
                            decoded = await admit_provider_request(
                                credential_boundary=boundary,
                                payload=payload,
                                operation=lambda: client.post(
                                    self.connection.endpoint,
                                    cast_to=dict,
                                    body=payload,
                                    options={
                                        "headers": options.get("extra_headers", {})
                                    },
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
        shape = self.connection.shape
        if not isinstance(decoded, dict):
            raise ValueError("expected an object")
        rows = []
        if shape in {"flat_rerank", "nested_rerank"}:
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
        elif shape == "system_one":
            answers = decoded["answers"]
            if not isinstance(answers, dict) or set(answers) != set(names):
                raise ValueError("answers do not match candidates")
            for i, name in enumerate(names):
                answer = answers[name]
                if answer["type"] != "noul":
                    raise ValueError("expected noul")
                rows.append(RerankResult(i, number(answer["noul"], probability=True)))
        else:
            answers = decoded["answers"]
            if not isinstance(answers, list):
                raise ValueError("expected answers array")
            for answer in answers:
                if answer.get("type") != "predicate" or answer.get("name") not in names:
                    raise ValueError("invalid predicate identity or refusal")
                rows.append(
                    RerankResult(
                        names.index(answer["name"]),
                        number(answer["probability"], probability=True),
                    )
                )
        indices = [row.index for row in rows]
        if (
            len(indices) != len(names)
            or len(set(indices)) != len(indices)
            or set(indices) != set(range(len(names)))
        ):
            raise ValueError("response must cover every candidate exactly once")
        return rows


def number(value, *, probability=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError("score must be a finite number")
    if probability and not 0 <= value <= 1:
        raise ValueError("probability is outside 0..1")
    return float(value)
