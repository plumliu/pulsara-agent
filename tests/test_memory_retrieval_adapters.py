"""Observable SDK wire shapes, complete candidate joins and failure boundaries."""

import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest

from pulsara_agent.llm.adapters.openai import client as openai_client
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundHttpx2Client
from pulsara_agent.retrieval.config import RetrievalConnection
from pulsara_agent.retrieval.embedding.openai_compatible import HttpEmbeddingProvider
from pulsara_agent.retrieval.rerank.factory import build_rerank_provider
from pulsara_agent.retrieval.rerank.decision_prompt import decision_question
from pulsara_agent.retrieval.errors import EmbeddingServiceError, RerankServiceError
from pulsara_agent.conversation_kernel.memory_tools import (
    _rerank_text,
)
from pulsara_agent.conversation_kernel.memory.contracts import memory_retrieval_text

VECTOR = [1.0, *([0.0] * 1023)]


@pytest.mark.parametrize("purpose", ["recall", "automatic_recall"])
def test_decision_question_binds_candidate_and_carries_both_criteria(purpose):
    name = "candidate:fixture-identity-73"
    instructions, positive, negative = decision_question(name, purpose)
    assert name in instructions
    assert positive.strip() and negative.strip() and positive != negative
    assert positive in instructions
    assert negative in instructions








@pytest.mark.parametrize("shape", ["flat_rerank", "nested_rerank", "system_one", "openai_decisions"])
def test_twenty_complete_memory_inputs_remain_exact_on_each_sdk_wire(monkeypatch, shape):
    names = tuple(f"fact{i}" for i in range(20))
    facts = [SimpleNamespace(retrieval_text=memory_retrieval_text('标题', '汉' * 8000)) for _ in names]
    query, documents = '测试查询', tuple(_rerank_text(fact) for fact in facts)
    def response(request):
        payload=json.loads(request.content)
        questions=payload.get("questions")
        requested=list(questions) if isinstance(questions,dict) else [item["name"] for item in questions] if questions else []
        return httpx2.Response(200,json=(
            {"answers":{name:{"type":"noul","noul":.5} for name in requested}} if shape=="system_one" else
            {"answers":[{"type":"predicate","name":name,"probability":.5} for name in requested]} if shape=="openai_decisions" else
            {"results":[{"index":i,"relevance_score":.5} for i in range(20)]}))
    observed=install_transport(monkeypatch,response)
    endpoint = "https://provider.example/decisions" if shape == "openai_decisions" else "https://provider.example/ranking"
    connection = RetrievalConnection(endpoint, "fixture", shape, "none")
    result = asyncio.run(build_rerank_provider(connection).rerank(query, documents, candidate_ids=names))
    assert [item.index for item in result] == list(range(20))
    wire_documents=[]
    candidate_names=[]
    for request in observed:
        body=json.loads(request.content)
        if shape=="flat_rerank":
            wire_documents.extend(body["documents"])
        elif shape=="nested_rerank":
            wire_documents.extend(body["input"]["documents"])
        else:
            state=body["state"] if shape=="system_one" else json.loads(body["input"])
            candidate_names.extend(item["candidate_number"] for item in state["candidates"])
            wire_documents.extend(item["candidate"] for item in state["candidates"])
    if candidate_names:
        assert candidate_names==list(names)
    assert wire_documents==list(documents)
    assert all(text=='title: 标题\nstatement: '+'汉'*8000 for text in wire_documents)



def install_transport(monkeypatch, response):
    observed = []

    def respond(request):
        observed.append(request)
        return (
            response(request)
            if callable(response)
            else httpx2.Response(200, json=response)
        )

    def injected(**kwargs):
        return ProcessCredentialBoundHttpx2Client(
            **kwargs, transport=httpx2.MockTransport(respond), trust_env=False
        )

    monkeypatch.setattr(openai_client, "ProcessCredentialBoundHttpx2Client", injected)
    return observed


@pytest.mark.parametrize("shape", ["flat_rerank", "nested_rerank"])
@pytest.mark.parametrize("position", ["results", "data", "output"])
@pytest.mark.parametrize("authentication", ["bearer_api_key", "none"])
@pytest.mark.parametrize("purpose", ["recall", "automatic_recall"])
def test_rerank_requests_have_only_subject_fields(
    monkeypatch, shape, position, authentication, purpose
):
    rows = [{"index": 1, "relevance_score": 3.5}, {"index": 0, "relevance_score": -2}]
    response = {position: {"results": rows} if position == "output" else rows}
    observed = install_transport(monkeypatch, response)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-must-not-be-used")
    connection = RetrievalConnection(
        "https://provider.example/custom/ranking",
        "rank-fixture",
        shape,
        authentication,
        "fixture-key" if authentication != "none" else None,
    )
    result = asyncio.run(
        build_rerank_provider(connection).rerank(
            "query", ["first", "second"], candidate_ids=("a", "b"), purpose=purpose
        )
    )
    assert [(r.index, r.score) for r in result] == [(1, 3.5), (0, -2)]
    assert len(observed) == 1
    request = observed[0]
    assert str(request.url) == connection.endpoint
    assert request.headers.get("authorization") == (
        "Bearer fixture-key" if authentication != "none" else None
    )
    body = json.loads(request.content)
    content = {"query": "query", "documents": ["first", "second"]}
    assert body == {
        "model": "rank-fixture",
        **(content if shape == "flat_rerank" else {"input": content}),
    }


@pytest.mark.parametrize("shape", ["system_one", "openai_decisions"])
@pytest.mark.parametrize("authentication", ["bearer_api_key", "none"])
@pytest.mark.parametrize("purpose", ["recall", "automatic_recall"])
def test_decisions_batch_named_candidates_native_or_public_sdk(
    monkeypatch, shape, authentication, purpose
):
    question_calls = []

    def question_for_candidate(name, selected_purpose):
        question_calls.append((name, selected_purpose))
        return (
            f"question:{selected_purpose}:{name}",
            f"positive:{name}",
            f"negative:{name}",
        )

    # Exercise purpose/candidate routing and both wire formats independently of
    # the editorial wording owned by decision_prompt.
    monkeypatch.setattr(
        "pulsara_agent.retrieval.rerank.decision.decision_question",
        question_for_candidate,
    )
    response = {
        "model": "decision-fixture",
        "answers": (
            {"a": {"type": "noul", "noul": 0.1}, "b": {"type": "noul", "noul": 0.9}}
            if shape == "system_one"
            else [
                {"type": "predicate", "name": "b", "probability": 0.9},
                {"type": "predicate", "name": "a", "probability": 0.1},
            ]
        ),
    }
    observed = install_transport(monkeypatch, response)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-must-not-be-used")
    endpoint = (
        "https://provider.example/v1/decisions"
        if shape == "openai_decisions"
        else "https://provider.example/v1/systemone"
    )
    connection = RetrievalConnection(
        endpoint,
        "decision-fixture",
        shape,
        authentication,
        "fixture-key" if authentication != "none" else None,
    )
    result = asyncio.run(
        build_rerank_provider(connection).rerank(
            "query", ["first", "second"], candidate_ids=("a", "b"), purpose=purpose
        )
    )
    assert [r.index for r in result] == [1, 0]
    assert len(observed) == 1 and str(observed[0].url) == endpoint
    assert observed[0].headers.get("authorization") == (
        "Bearer fixture-key" if authentication != "none" else None
    )
    body = json.loads(observed[0].content)
    assert set(body) == (
        {"model", "state", "questions"}
        if shape == "system_one"
        else {"model", "input", "questions"}
    )
    questions = body["questions"]
    if shape == "openai_decisions":
        assert [q["name"] for q in questions] == ["a", "b"]
        assert all(q["type"] == "predicate" for q in questions)
        state = json.loads(body["input"])
    else:
        assert list(questions) == ["a", "b"]
        assert all(q["type"] == "noul" for q in questions.values())
        questions = list(questions.values())
        state = body["state"]
    for name, question in zip(("a", "b"), questions, strict=True):
        assert question["instructions"] == f"question:{purpose}:{name}"
        if shape == "system_one":
            assert question["criteria"] == {
                "true": f"positive:{name}",
                "false": f"negative:{name}",
            }
    assert question_calls == [("a", purpose), ("b", purpose)]
    assert state == {
        "user_task": "query",
        "candidates": [{"candidate_number": "a", "candidate": "first"}, {"candidate_number": "b", "candidate": "second"}],
    }
    assert [c["candidate_number"] for c in state["candidates"]] == ["a", "b"]


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"results": []},
        {"results": [{"index": 0, "relevance_score": 1}]},
        {
            "results": [
                {"index": True, "relevance_score": 1},
                {"index": 1, "relevance_score": 2},
            ]
        },
        {
            "results": [
                {"index": 0, "relevance_score": "0.5"},
                {"index": 1, "relevance_score": 2},
            ]
        },
        {
            "results": [
                {"index": 0, "relevance_score": float("nan")},
                {"index": 1, "relevance_score": 2},
            ]
        },
        {
            "results": [
                {"index": 0, "relevance_score": 1},
                {"index": 0, "relevance_score": 2},
            ]
        },
        {"results": None, "data": []},
        {"results": [], "output": {"results": []}},
    ],
)
def test_invalid_rerank_fails_whole_call_without_retry(monkeypatch, response):
    observed = install_transport(
        monkeypatch, lambda request: httpx2.Response(200, content=json.dumps(response))
    )
    connection = RetrievalConnection(
        "https://provider.example/rerank", "fixture", "flat_rerank", "none"
    )
    with pytest.raises(RerankServiceError):
        asyncio.run(build_rerank_provider(connection).rerank("query", ["first", "second"]))
    assert len(observed) == 1


@pytest.mark.parametrize(
    "shape,answers",
    [
        (
            "system_one",
            {"a": {"type": "noul", "noul": 1.1}, "b": {"type": "noul", "noul": 0.2}},
        ),
        ("system_one", {"a": {"type": "noul", "noul": 0.1}}),
        (
            "openai_decisions",
            [{"type": "predicate", "name": "a", "probability": 0.1}] * 2,
        ),
        (
            "openai_decisions",
            [{"type": "refusal", "name": "a", "refusal": "cannot decide"}],
        ),
        ("openai_decisions", [{"type": "predicate", "name": None, "probability": 0.1}]),
    ],
)
def test_invalid_decision_identity_and_probability_fail(monkeypatch, shape, answers):
    observed = install_transport(monkeypatch, {"answers": answers})
    endpoint = (
        "https://provider.example/decisions"
        if shape == "openai_decisions"
        else "https://provider.example/systemone"
    )
    with pytest.raises(RerankServiceError):
        asyncio.run(
            build_rerank_provider(
                RetrievalConnection(endpoint, "fixture", shape, "none")
            ).rerank("query", ["first", "second"], candidate_ids=("a", "b"))
        )
    assert len(observed) == 1


@pytest.mark.parametrize(
    "body", [b'{"results":[],"results":[]}', b"x" * (256 * 1024 + 1)]
)
def test_response_byte_and_duplicate_key_boundaries_precede_sdk_parsing(
    monkeypatch, body
):
    observed = install_transport(
        monkeypatch, lambda request: httpx2.Response(200, content=body)
    )
    with pytest.raises(RerankServiceError):
        asyncio.run(
            build_rerank_provider(
                RetrievalConnection(
                    "https://provider.example/rerank", "fixture", "flat_rerank", "none"
                )
            ).rerank("q", ["a"])
        )
    assert len(observed) == 1


@pytest.mark.parametrize("authentication", ["bearer_api_key", "none"])
def test_embedding_minimal_body_accepts_metadata_alias_and_orders_indices(
    monkeypatch, authentication
):
    observed = install_transport(
        monkeypatch,
        {
            "model": "returned-snapshot-alias",
            "data": [
                {"index": 1, "embedding": VECTOR},
                {"index": 0, "embedding": VECTOR},
            ],
        },
    )
    connection = RetrievalConnection(
        "https://provider.example/custom/embed",
        "configured-model",
        "openai_embedding",
        authentication,
        "key" if authentication != "none" else None,
    )
    result = asyncio.run(
        HttpEmbeddingProvider(connection).embed_batch(["first", "second"])
    )
    assert result == [VECTOR, VECTOR]
    assert json.loads(observed[0].content) == {
        "model": "configured-model",
        "input": ["first", "second"],
    }
    assert str(observed[0].url) == connection.endpoint


@pytest.mark.parametrize(
    "data",
    [
        [{"index": 0, "embedding": [1, 2]}],
        [{"index": 0, "embedding": [0] * 1024}],
        [{"index": 0, "embedding": VECTOR}, {"index": 0, "embedding": VECTOR}],
        [{"index": True, "embedding": VECTOR}],
        [],
    ],
)
def test_embedding_rejects_invalid_vectors_and_indices(monkeypatch, data):
    install_transport(monkeypatch, {"data": data})
    with pytest.raises(EmbeddingServiceError):
        asyncio.run(
            HttpEmbeddingProvider(
                RetrievalConnection(
                    "https://provider.example/embed",
                    "fixture",
                    "openai_embedding",
                    "none",
                )
            ).embed("query")
        )


def test_compressed_response_is_bounded_after_httpx2_decompression(monkeypatch):
    import gzip

    compressed = gzip.compress(json.dumps({"padding": "x" * (256 * 1024 + 1)}).encode())
    assert len(compressed) < 1024

    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield compressed

    observed = install_transport(
        monkeypatch,
        lambda request: httpx2.Response(
            200, headers={"content-encoding": "gzip"}, stream=Stream()
        ),
    )
    with pytest.raises(RerankServiceError, match="byte bound"):
        asyncio.run(
            build_rerank_provider(
                RetrievalConnection(
                    "https://provider.example/rerank", "fixture", "flat_rerank", "none"
                )
            ).rerank("q", ["a"])
        )
    assert len(observed) == 1
