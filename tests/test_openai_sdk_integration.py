"""Exercise Pulsara's injected client against the real OpenAI SDK."""

import asyncio
import json

import httpx2
import pytest

from pulsara_agent.llm.adapters.openai import client as openai_client
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialBoundHttpx2Client,
)


@pytest.mark.parametrize("native", (True, False))
@pytest.mark.parametrize("authenticated", (True, False))
def test_decisions_sdk_entrypoints_preserve_request_and_auth_boundary(
    monkeypatch: pytest.MonkeyPatch, native: bool, authenticated: bool
) -> None:
    observed = []
    body = {
        "model": "decision-fixture",
        "input": "query and candidate memory",
        "questions": [
            {"type": "predicate", "name": "m1", "instructions": "Is m1 relevant?"}
        ],
    }

    def respond(request: httpx2.Request) -> httpx2.Response:
        observed.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "decision-fixture",
                "model": "decision-fixture",
                "answers": [
                    {"type": "predicate", "name": "m1", "probability": 0.8}
                ],
            },
        )

    def injected_client(**kwargs):
        return ProcessCredentialBoundHttpx2Client(
            **kwargs, transport=httpx2.MockTransport(respond), trust_env=False
        )

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-sent")
    monkeypatch.setattr(
        openai_client, "ProcessCredentialBoundHttpx2Client", injected_client
    )

    async def exercise() -> None:
        key = "fixture-api-key" if authenticated else None
        client = openai_client.build_async_openai_client(
            api_key=key,
            base_url="https://provider.example/v1",
            timeout_policy=openai_client.OpenAITransportTimeoutPolicy(2, 2, 2, 2, 2),
            credential_boundary=ProcessCredentialBoundary(key or ""),
            max_retries=0,
        )
        options = openai_client.openai_auth_request_options(
            requires_api_key=authenticated
        )
        async with client:
            if native:
                result = await client.decisions.create(**body, **options)
                assert result.answers[0].probability == 0.8
            else:
                result = await client.post(
                    "https://provider.example/custom/decisions",
                    cast_to=dict,
                    body=body,
                    options={"headers": options.get("extra_headers", {})},
                )
                assert result["answers"][0]["probability"] == 0.8
        assert client.is_closed()

    asyncio.run(exercise())
    assert len(observed) == 1
    request = observed[0]
    assert request.url.path == ("/v1/decisions" if native else "/custom/decisions")
    assert json.loads(request.content) == body
    assert request.headers.get("authorization") == (
        "Bearer fixture-api-key" if authenticated else None
    )


def test_embedding_provider_uses_sdk_httpx2_client(tmp_path, monkeypatch) -> None:
    from pulsara_agent.retrieval.embedding import openai_compatible as embedding

    observed = []
    vector = [1.0, *([0.0] * 1023)]

    def respond(request: httpx2.Request) -> httpx2.Response:
        observed.append(request)
        return httpx2.Response(
            200,
            json={
                "model": "text-embedding-v4",
                "data": [{"index": 0, "embedding": vector}],
            },
        )

    def injected_client(**kwargs):
        return ProcessCredentialBoundHttpx2Client(
            **kwargs, transport=httpx2.MockTransport(respond), trust_env=False
        )

    monkeypatch.setattr(
        openai_client, "ProcessCredentialBoundHttpx2Client", injected_client
    )

    async def exercise() -> None:
        from pulsara_agent.retrieval.config import RetrievalConnection
        provider = embedding.HttpEmbeddingProvider(RetrievalConnection(
            "https://provider.example/v1/embeddings", "text-embedding-v4", "openai_embedding",
            "bearer_api_key", "fixture-embedding-key",
        ))
        assert await provider.embed("memory query") == vector

    asyncio.run(exercise())
    assert len(observed) == 1
    assert observed[0].url.path == "/v1/embeddings"
    assert observed[0].headers["authorization"] == "Bearer fixture-embedding-key"
    assert json.loads(observed[0].content) == {
        "model": "text-embedding-v4",
        "input": ["memory query"],
    }
