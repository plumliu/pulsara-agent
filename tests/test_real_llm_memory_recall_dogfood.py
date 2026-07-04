from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from pulsara_agent.settings import PulsaraSettings


pytestmark = pytest.mark.real_llm

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "evals" / "recall" / "real_llm_dogfood.py"
_SPEC = importlib.util.spec_from_file_location("real_llm_memory_recall_dogfood", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runner
_SPEC.loader.exec_module(runner)


def test_real_llm_long_memory_recall_dogfood(tmp_path, capsys) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to run the long memory recall dogfood.")
    settings = _load_settings()
    if not settings.retrieval.embedding.api_key or not settings.retrieval.rerank.api_key:
        pytest.skip("Long recall dogfood requires embedding and rerank API keys.")

    report = asyncio.run(runner.run_memory_recall_dogfood(tmp_path, settings=settings))
    print("\nREAL_LLM_MEMORY_RECALL_DOGFOOD=" + report.to_json())

    turns = {turn.label: turn for turn in report.turns}
    ids = report.target_ids
    assert report.vector_row_count == 6
    assert report.resources_closed is True
    assert all(turn.status == "finished" for turn in report.turns)

    assert turns["auto_timezone"].projection_ids == (ids["timezone"],)
    assert turns["auto_timezone"].tool_names == ()
    assert "shanghai" in turns["auto_timezone"].final_text.casefold()

    _assert_explicit_hit(turns["explicit_persistence"], ids["persistence"])
    assert "postgres" in turns["explicit_persistence"].final_text.casefold()
    assert "pgvector" in turns["explicit_persistence"].final_text.casefold()

    _assert_explicit_hit(turns["explicit_billing_guardrail"], ids["billing"])
    billing_text = turns["explicit_billing_guardrail"].final_text.casefold()
    assert "confirm" in billing_text
    assert "backup" in billing_text

    _assert_explicit_hit(turns["explicit_timezone_repeat"], ids["timezone"])
    _assert_explicit_hit(turns["cross_dialogue_persistence"], ids["persistence"])

    assert turns["unrelated_negative"].final_text.strip() == "323"
    assert turns["unrelated_negative"].tool_names == ()
    assert turns["unrelated_negative"].projection_ids == ()

    hidden_id = ids["hidden"]
    assert all(hidden_id not in turn.projection_ids for turn in report.turns)
    assert all(hidden_id not in text for turn in report.turns for text in turn.tool_result_texts)

    explicit_traces = [trace for trace in report.traces if trace["trigger_kind"] == "explicit_search"]
    assert len(explicit_traces) == 4
    assert all(trace["metadata"].get("reranker_model") for trace in explicit_traces)
    assert all(trace["metadata"].get("vector_candidate_ids") for trace in explicit_traces)
    assert report.usage_count >= 5


def _assert_explicit_hit(turn, memory_id: str) -> None:
    assert turn.tool_names == ("memory_search",)
    assert any(memory_id in payload for payload in turn.tool_result_texts)
    assert memory_id in turn.final_text
    payload = json.loads(turn.tool_result_texts[0])
    assert [item["memory_id"] for item in payload["results"]] == [memory_id]


def _load_settings() -> PulsaraSettings:
    env_file = Path(".env")
    return PulsaraSettings.from_env_file(env_file) if env_file.exists() else PulsaraSettings.from_env()
