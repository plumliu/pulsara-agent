from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.entities.memory import Preference
from pulsara_agent.event.candidates import PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory.candidates.pool import CandidateOrigin, PooledMemoryCandidate
from pulsara_agent.memory.canonical.query import PostgresMemoryQuery
from pulsara_agent.memory.canonical.vector_query import MemoryVectorQuery
from pulsara_agent.memory.governance.relatedness import (
    GovernanceRelatednessService,
    MemoryGovernanceRelatednessOptions,
)
from pulsara_agent.ontology import memory
from pulsara_agent.retrieval.runtime import build_retrieval_runtime_resources
from pulsara_agent.retrieval.tokenizer.factory import build_tokenizer
from pulsara_agent.settings import PulsaraSettings


pytestmark = pytest.mark.real_llm
FIXTURE = Path("evals/governance_relatedness/fixtures/v1_semantic.jsonl")


def test_real_embedding_reranker_relatedness_fixture_recall_and_noise() -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call configured real providers.")

    report = asyncio.run(_run_real_relatedness_fixture())

    print("\nREAL_RELATEDNESS_FIXTURE=" + json.dumps(report, ensure_ascii=False, indent=2))
    assert report["overall_recall_at_k"] >= 0.95
    assert report["miss_rate"] <= 0.05
    assert all(value >= 0.90 for value in report["slice_recall_at_k"].values())
    assert report["embed_batch_calls"] == 1


async def _run_real_relatedness_fixture() -> dict:
    settings = PulsaraSettings.from_env_file(".env")
    resources = build_retrieval_runtime_resources(settings.retrieval)
    if resources.embedding is None or resources.rerank is None:
        pytest.skip("Real embedding and rerank API keys are required.")
    dsn = settings.storage.postgres_dsn
    try:
        psycopg.connect(dsn, connect_timeout=2).close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")

    cases = [
        json.loads(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # This v1 end-to-end fixture exercises the currently lifecycle-enabled
    # Preference path. Decision/ActionBoundary expansion has separate type gates.
    cases = [
        case
        for case in cases
        if all(item["memory_id"].startswith("preference:") for item in case["canonical_memories"])
    ]
    graph_id = f"graph:real-relatedness-eval/{uuid4().hex}"
    graph = PostgresGraphStore(dsn)
    now = utc_now()
    candidates: list[PooledMemoryCandidate] = []
    try:
        for case in cases:
            for item in case["canonical_memories"]:
                graph.put_jsonld(
                    Preference(
                        id=item["memory_id"],
                        statement=item["statement"],
                        scope="ctx:user",
                        status=memory.NodeStatus.ACTIVE,
                        confidence_level=memory.ConfidenceLevel.HIGH,
                        verification_status=memory.VerificationStatus.USER_CONFIRMED,
                        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                        created_at=now,
                        updated_at=now,
                        gate_reason="real relatedness eval fixture",
                    ).to_jsonld(),
                    graph_id=graph_id,
                )
            candidates.append(
                PooledMemoryCandidate(
                    entry_id=f"pool:eval:{case['case_id']}",
                    payload=ValidCandidatePayload(
                        candidate=PreferenceCandidate(
                            candidate_id=f"candidate:eval:{case['case_id']}",
                            statement=case["query"],
                            scope="ctx:user",
                            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                            verification_status=memory.VerificationStatus.USER_CONFIRMED,
                        )
                    ),
                    origin=CandidateOrigin.MAIN_AGENT_TOOL,
                    source_session_id="runtime:real-relatedness-eval",
                    source_run_id=f"run:eval:{case['case_id']}",
                    source_turn_id=f"turn:eval:{case['case_id']}",
                    source_reply_id=f"reply:eval:{case['case_id']}",
                    user_quote=case["query"],
                )
            )

        embedding_calls = 0
        original_embed_batch = resources.embedding.embed_batch

        async def counted_embed_batch(texts):
            nonlocal embedding_calls
            embedding_calls += 1
            return await original_embed_batch(texts)

        # Provider instances are mutable dataclasses; counting here proves the
        # governance layer made one deduplicated batch request.
        resources.embedding.embed_batch = counted_embed_batch  # type: ignore[method-assign]
        service = GovernanceRelatednessService(
            memory_query=PostgresMemoryQuery(dsn=dsn),
            tokenizer=build_tokenizer(settings.retrieval.tokenizer),
            embedding=resources.embedding,
            vector_query=MemoryVectorQuery(dsn),
            reranker=resources.rerank,
            provider_name=settings.retrieval.embedding.provider,
            options=MemoryGovernanceRelatednessOptions(
                candidate_limit=settings.retrieval.governance_relatedness.candidate_limit,
                dense_candidate_min_score=(
                    settings.retrieval.governance_relatedness.dense_candidate_min_score
                ),
                max_inline_gap_embeds=20,
            ),
        )
        result = await service.collect_batch(candidates, graph_id=graph_id)

        slice_hits: dict[str, int] = defaultdict(int)
        slice_totals: dict[str, int] = defaultdict(int)
        total_hits = 0
        candidate_counts: list[int] = []
        predictions: dict[str, list[str]] = {}
        for case, candidate in zip(cases, candidates, strict=True):
            predicted = sorted(result.for_candidate(candidate.entry_id).allowlist)
            predictions[case["case_id"]] = predicted
            hit = bool(set(case["relevant_ids"]) & set(predicted))
            total_hits += int(hit)
            slice_hits[case["slice"]] += int(hit)
            slice_totals[case["slice"]] += 1
            candidate_counts.append(len(predicted))
        recall = total_hits / len(cases)
        return {
            "case_count": len(cases),
            "overall_recall_at_k": recall,
            "miss_rate": 1.0 - recall,
            "slice_recall_at_k": {
                name: slice_hits[name] / total for name, total in slice_totals.items()
            },
            "mean_candidate_count": sum(candidate_counts) / len(candidate_counts),
            "max_candidate_count": max(candidate_counts),
            "predictions": predictions,
            "embed_batch_calls": embedding_calls,
            "diagnostics": dict(result.diagnostics),
        }
    finally:
        await resources.aclose()
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("delete from memory_vector_index where graph_id = %s", (graph_id,))
                cursor.execute("delete from graph_documents where graph_id = %s", (graph_id,))
                cursor.execute("delete from memory_nodes where graph_id = %s", (graph_id,))
                cursor.execute("delete from memory_relations where graph_id = %s", (graph_id,))
