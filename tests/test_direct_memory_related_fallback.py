"""Optional dense failure must not erase valid sparse relation suggestions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from time import monotonic

from pulsara_agent.conversation_kernel.memory.recall import (
    MemoryDenseCandidateDisposition,
    MemoryQueryRow,
    MemoryQueryResult,
    MemoryRetrievalDisposition,
    PostgresMemoryQuery,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.memory.scope import (
    CTX_GLOBAL,
    MemoryDomainContext,
    freeze_memory_read_context_binding,
)
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV2
from pulsara_agent.model_input.lowering import source_variant_message
from pulsara_agent.retrieval.config import (
    AdvisoryMemoryFeatureConfig,
    EmbeddingBackendConfig,
)
from pulsara_agent.settings import LocalSettingsStore


def test_related_search_keeps_sparse_results_when_dense_query_fails(monkeypatch):
    query = PostgresMemoryQuery(None)
    row = MemoryQueryRow(
        fact_id="memory:sparse",
        memory_domain_id="u_local",
        context_id=CTX_GLOBAL,
        fact_kind="FACT",
        lifecycle="ACTIVE",
        statement="Project Orion revenue was 10.",
        recorded_at="2026-01-01T00:00:00Z",
        fact_semantic_digest="sha256:" + "0" * 64,
    )
    monkeypatch.setattr(query, "_sparse", lambda **_: (row,))

    def fail_dense(**_):
        raise RuntimeError("embedding index unavailable")

    monkeypatch.setattr(query, "_dense", fail_dense)
    monkeypatch.setattr(query, "_canonical_refetch", lambda **kwargs: kwargs["ranked"])
    binding = freeze_memory_read_context_binding(
        domain=MemoryDomainContext("u_local", "transient"),
        host_workspace_id="workspace:related-fallback",
    )

    result = query.related_candidates(
        read_binding=binding,
        context_id=CTX_GLOBAL,
        query="Project Orion revenue",
        query_embedding=(0.1,),
        exclude_fact_id=None,
        deadline_monotonic=monotonic() + 10,
    )

    assert [item.fact_id for item in result.facts] == [row.fact_id]
    assert result.dense_disposition is MemoryDenseCandidateDisposition.UNAVAILABLE


def test_five_recalled_facts_keep_source_variants_monotone(tmp_path, monkeypatch):
    binding = freeze_memory_read_context_binding(
        domain=MemoryDomainContext("u_local", "transient"),
        host_workspace_id="workspace:recall-source",
    )
    facts = tuple(
        MemoryQueryRow(
            fact_id="memory:" + str(index) * 64,
            memory_domain_id="u_local",
            context_id=CTX_GLOBAL,
            fact_kind="FACT",
            lifecycle="ACTIVE",
            statement=f"Project Orion quarterly revenue is {index}.",
            recorded_at="2026-01-01T00:00:00Z",
            fact_semantic_digest="sha256:" + str(index) * 64,
        )
        for index in range(1, 6)
    )

    async def exercise():
        io = KernelSessionIO()
        port = KernelMemoryToolPort(
            repository=SimpleNamespace(connection_provider=None),
            session_id="session:source-variants",
            read_binding=binding,
            embedding_config=EmbeddingBackendConfig(),
            feature_config=AdvisoryMemoryFeatureConfig(automatic_dense=False),
            io_owner=io,
            settings=LocalSettingsStore(tmp_path / "settings.yaml"),
        )

        async def recall(**_):
            return MemoryQueryResult(
                disposition=MemoryRetrievalDisposition.COMPLETE,
                facts=facts,
                attempted_stages=(),
            )

        monkeypatch.setattr(port, "_parallel_recall", recall)
        monkeypatch.setattr(port._query, "active_contradictions", lambda **_: ())
        try:
            return await port.freeze_automatic_recall_source(
                "Project Orion quarterly revenue"
            )
        finally:
            await port.aclose()
            await io.aclose(deadline_monotonic=monotonic() + 10)

    source = asyncio.run(exercise())
    estimator = PulsaraHeuristicTokenEstimatorV2()
    costs = tuple(
        estimator.estimate_message(
            source_variant_message(source, variant.text, mode=variant.mode)
        )
        for variant in source.variants
    )
    assert costs == tuple(sorted(costs, reverse=True))
