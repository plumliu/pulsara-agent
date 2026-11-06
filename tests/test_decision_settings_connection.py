"""Decision settings testing uses the common protocol outside any Session."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.desktop_app.http_server import LocalHttpServer
from pulsara_agent.retrieval.config import RetrievalConnection
from tests.test_memory_retrieval_adapters import install_transport


@pytest.mark.parametrize("shape", ["system_one", "openai_decisions"])
def test_decision_connection_test_uses_common_predicates_without_memory_ranking(monkeypatch, shape):
    response = (
        {"answers": {"input_0": {"type": "noul", "noul": 1}, "input_1": {"type": "noul", "noul": 0}}}
        if shape == "system_one" else
        {"answers": [{"type": "predicate", "name": "input_1", "probability": 0},
                     {"type": "predicate", "name": "input_0", "probability": 1}]}
    )
    observed = install_transport(monkeypatch, response)
    connection = RetrievalConnection("https://provider.example/decisions", "configured", shape, "none")

    def no_memory_provider(*args, **kwargs):
        raise AssertionError("Decision settings testing used the memory rerank adapter")

    monkeypatch.setattr("pulsara_agent.retrieval.rerank.factory.build_rerank_provider", no_memory_provider)
    settings = SimpleNamespace(read=lambda: object(), retrieval_draft=lambda *_: connection)
    server = SimpleNamespace(settings=settings, _json_body=AsyncMock(return_value={}))
    request = SimpleNamespace(match_info={"slot": "decision"})
    result = asyncio.run(LocalHttpServer._test_memory_model(server, request))
    payload = json.loads(result.body)
    assert result.status == 200 and payload["status"] == "ready" and payload["result_count"] == 2
    assert len(observed) == 1
