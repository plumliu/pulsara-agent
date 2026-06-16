from __future__ import annotations

import json
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.entities.memory import Preference
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory import PostgresMemoryQuery
from pulsara_agent.memory.recall.explain import ClaimKind, Explanation, ExplanationClaim, explain_memory, validate_explanation
from pulsara_agent.memory.canonical.lifecycle import MemoryLifecycle
from pulsara_agent.memory.recall.service import LexicalMemoryRecallService, RecallQuery
from pulsara_agent.ontology import memory
from pulsara_agent.settings import StorageConfig
from pulsara_agent.tools.base import ToolCall
from pulsara_agent.tools.builtins.memory_query import MemoryExplainTool, MemoryRelatedTool


def test_explainer_only_claims_superseded_when_materialized_edge_exists() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    lifecycle = MemoryLifecycle(graph=store, mutable=store)
    try:
        store.put_jsonld(_preference("preference:old", "The user prefers verbose summaries."), graph_id=graph_id)
        store.put_jsonld(_preference("preference:new", "The user prefers concise summaries."), graph_id=graph_id)

        old_before = query.fetch_nodes(["preference:old"], graph_id=graph_id)[0]
        before = explain_memory(old_before)
        assert ClaimKind.SUPERSEDED_BY not in {claim.kind for claim in before.claims}

        lifecycle.supersede(
            old_id="preference:old",
            new_id="preference:new",
            governance_batch_id="governance:test:explain",
            graph_id=graph_id,
        )
        old_after = query.fetch_nodes(["preference:old"], graph_id=graph_id)[0]
        after = explain_memory(old_after)

        superseded_claims = [claim for claim in after.claims if claim.kind is ClaimKind.SUPERSEDED_BY]
        assert len(superseded_claims) == 1
        assert superseded_claims[0].grounded_on == (
            f"rel:preference:new|{memory.SUPERSEDES.name}|preference:old",
        )
    finally:
        store.delete_graph(graph_id)


def test_explanation_validator_rejects_ungrounded_claim() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    try:
        store.put_jsonld(_preference("preference:plain", "The user prefers concise summaries."), graph_id=graph_id)
        view = query.fetch_nodes(["preference:plain"], graph_id=graph_id)[0]

        with pytest.raises(ValueError):
            validate_explanation(
                Explanation(
                    memory_id=view.id,
                    claims=(
                        ExplanationClaim(
                            text="This is not grounded.",
                            kind=ClaimKind.SUPERSEDED_BY,
                            grounded_on=("rel:missing|supersedes|preference:plain",),
                        ),
                    ),
                ),
                view=view,
            )
    finally:
        store.delete_graph(graph_id)


def test_memory_related_and_explain_tools_return_grounded_payloads() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    lifecycle = MemoryLifecycle(graph=store, mutable=store)
    try:
        store.put_jsonld(_preference("preference:old", "The user prefers verbose summaries."), graph_id=graph_id)
        store.put_jsonld(_preference("preference:new", "The user prefers concise summaries."), graph_id=graph_id)
        lifecycle.supersede(
            old_id="preference:old",
            new_id="preference:new",
            governance_batch_id="governance:test:tools",
            graph_id=graph_id,
        )
        related = MemoryRelatedTool(memory_query=query, graph_id=graph_id).execute(
            ToolCall(
                id="call:related",
                name="memory_related",
                arguments={"memory_id": "preference:new"},
            )
        )
        explained = MemoryExplainTool(memory_query=query, graph_id=graph_id).execute(
            ToolCall(
                id="call:explain",
                name="memory_explain",
                arguments={"memory_id": "preference:old"},
            )
        )

        related_payload = json.loads(related.output)
        explained_payload = json.loads(explained.output)
        assert related_payload["status"] == "ok"
        assert related_payload["outgoing"] == [
            {"predicate": memory.SUPERSEDES.name, "target_id": "preference:old"}
        ]
        assert explained_payload["status"] == "ok"
        claims = explained_payload["explanation"]["claims"]
        assert any(claim["kind"] == "superseded_by" for claim in claims)
        assert all(claim["grounded_on"] for claim in claims)
    finally:
        store.delete_graph(graph_id)


def test_graph_rerank_adds_grounded_reason_for_superseding_memory() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    lifecycle = MemoryLifecycle(graph=store, mutable=store)
    try:
        store.put_jsonld(_preference("preference:old", "The user prefers verbose summaries."), graph_id=graph_id)
        store.put_jsonld(_preference("preference:new", "The user prefers concise summaries."), graph_id=graph_id)
        lifecycle.supersede(
            old_id="preference:old",
            new_id="preference:new",
            governance_batch_id="governance:test:rerank",
            graph_id=graph_id,
        )

        result = asyncio_run_recall(query, graph_id=graph_id)

        assert result.items[0].memory_id == "preference:new"
        assert "supersedes_edge" in result.items[0].why
    finally:
        store.delete_graph(graph_id)


def asyncio_run_recall(query: PostgresMemoryQuery, *, graph_id: str):
    import asyncio

    return asyncio.run(
        LexicalMemoryRecallService(query).recall(
            RecallQuery(text="concise summaries preference", scopes=("ctx:user",)),
            graph_id=graph_id,
        )
    )


def _preference(memory_id: str, statement: str) -> dict:
    now = utc_now()
    return Preference(
        id=memory_id,
        statement=statement,
        scope="ctx:user",
        status=memory.NodeStatus.ACTIVE,
        confidence_level=memory.ConfidenceLevel.HIGH,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        created_at=now,
        updated_at=now,
        gate_reason="test",
    ).to_jsonld()


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")
