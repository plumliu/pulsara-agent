from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from pulsara_agent.memory.canonical.query import CanonicalNodeView, MemoryRelationEdge
from pulsara_agent.memory.recall.graph import GraphCandidateService, RecallPath, RecallPathStep
from pulsara_agent.memory.recall.service import (
    LexicalMemoryRecallService,
    RecallItem,
    RecallQuery,
    RecallResult,
    RecallStatus,
    RecallTrigger,
)
from pulsara_agent.ontology import memory
from pulsara_agent.tools.base import ToolCall
from pulsara_agent.tools.builtins.memory_query import MemorySearchTool


def test_graph_candidates_find_one_hop_and_preserve_reverse_edge_direction() -> None:
    query = _MemoryQuery(
        views={
            "preference:new": _view("preference:new", "new preference"),
            "preference:old": _view("preference:old", "old preference"),
        },
        edges=[
            MemoryRelationEdge(
                source_id="preference:new",
                predicate=memory.SUPERSEDES.name,
                target_id="preference:old",
            )
        ],
    )
    outcome = GraphCandidateService(query).collect(
        seed_ids=["preference:old"],
        scopes=["ctx:user"],
        types=[],
        max_hops=1,
        graph_id="graph:test",
    )

    assert [candidate.memory_id for candidate in outcome.batch.candidates] == ["preference:new"]
    step = outcome.paths_by_id["preference:new"][0].steps[0]
    assert step.traversal == "reverse"
    assert step.from_id == "preference:old"
    assert step.to_id == "preference:new"
    assert step.edge_source_id == "preference:new"
    assert step.edge_target_id == "preference:old"


def test_graph_candidates_find_two_hop_shared_evidence_only_at_depth_two() -> None:
    query = _MemoryQuery(
        views={
            "claim:a": _view("claim:a", "first claim", memory_type=memory.CLAIM.name),
            "claim:b": _view("claim:b", "second claim", memory_type=memory.CLAIM.name),
        },
        edges=[
            MemoryRelationEdge("evidence:shared", memory.SUPPORTS.name, "claim:a"),
            MemoryRelationEdge("evidence:shared", memory.SUPPORTS.name, "claim:b"),
        ],
    )
    service = GraphCandidateService(query)

    one_hop = service.collect(
        seed_ids=["claim:a"], scopes=["ctx:user"], types=[], max_hops=1, graph_id=None
    )
    two_hop = service.collect(
        seed_ids=["claim:a"], scopes=["ctx:user"], types=[], max_hops=2, graph_id=None
    )

    assert one_hop.batch.candidates == ()
    assert [candidate.memory_id for candidate in two_hop.batch.candidates] == ["claim:b"]
    path = two_hop.paths_by_id["claim:b"][0]
    assert [step.predicate for step in path.steps] == [memory.SUPPORTS.name, memory.SUPPORTS.name]
    assert [step.traversal for step in path.steps] == ["reverse", "forward"]
    assert query.relation_calls == 3  # one call for depth 1, then two batched levels for depth 2


def test_graph_candidates_allow_shared_basis_and_supersede_lineage_but_not_mixed_walks() -> None:
    query = _MemoryQuery(
        views={
            "decision:a": _view("decision:a", "decision a", memory_type=memory.DECISION.name),
            "claim:basis": _view("claim:basis", "basis", memory_type=memory.CLAIM.name),
            "decision:b": _view("decision:b", "decision b", memory_type=memory.DECISION.name),
            "preference:new": _view("preference:new", "new"),
            "preference:middle": _view("preference:middle", "middle"),
            "preference:old": _view("preference:old", "old"),
            "claim:mixed": _view("claim:mixed", "mixed", memory_type=memory.CLAIM.name),
        },
        edges=[
            MemoryRelationEdge("decision:a", memory.BASED_ON.name, "claim:basis"),
            MemoryRelationEdge("decision:b", memory.BASED_ON.name, "claim:basis"),
            MemoryRelationEdge("claim:basis", memory.CONTRADICTS.name, "claim:mixed"),
            MemoryRelationEdge("preference:new", memory.SUPERSEDES.name, "preference:middle"),
            MemoryRelationEdge("preference:middle", memory.SUPERSEDES.name, "preference:old"),
        ],
    )
    service = GraphCandidateService(query)

    shared_basis = service.collect(
        seed_ids=["decision:a"], scopes=["ctx:user"], types=[], max_hops=2, graph_id=None
    )
    lineage = service.collect(
        seed_ids=["preference:new"], scopes=["ctx:user"], types=[], max_hops=2, graph_id=None
    )

    assert "decision:b" in {candidate.memory_id for candidate in shared_basis.batch.candidates}
    assert "claim:mixed" not in {candidate.memory_id for candidate in shared_basis.batch.candidates}
    assert "preference:old" in {candidate.memory_id for candidate in lineage.batch.candidates}


def test_graph_candidates_do_not_traverse_hidden_canonical_intermediate() -> None:
    query = _MemoryQuery(
        views={
            "preference:a": _view("preference:a", "a"),
            "preference:hidden": _view("preference:hidden", "hidden", scope="ctx:workspace/hidden"),
            "preference:c": _view("preference:c", "c"),
        },
        edges=[
            MemoryRelationEdge("preference:a", memory.SUPERSEDES.name, "preference:hidden"),
            MemoryRelationEdge("preference:hidden", memory.SUPERSEDES.name, "preference:c"),
        ],
    )
    outcome = GraphCandidateService(query).collect(
        seed_ids=["preference:a"],
        scopes=["ctx:user"],
        types=[],
        max_hops=2,
        graph_id=None,
    )

    assert outcome.batch.candidates == ()


def test_graph_candidates_exclude_inactive_one_hop_neighbor() -> None:
    # A 1-hop neighbor that is SUPERSEDED (not ACTIVE) must not surface as a
    # graph candidate. Before this guard the only status check lived on the
    # deleted companion path; hop-surfaced neighbor status filtering must hold.
    query = _MemoryQuery(
        views={
            "preference:seed": _view("preference:seed", "seed"),
            "preference:retired": _view(
                "preference:retired",
                "retired",
                status=memory.NodeStatus.SUPERSEDED,
            ),
        },
        edges=[
            MemoryRelationEdge(
                "preference:seed", memory.SUPERSEDES.name, "preference:retired"
            )
        ],
    )
    outcome = GraphCandidateService(query).collect(
        seed_ids=["preference:seed"],
        scopes=["ctx:user"],
        types=[],
        max_hops=1,
        graph_id=None,
    )

    assert outcome.batch.candidates == ()


def test_graph_candidates_exclude_hidden_endpoint_behind_visible_intermediate() -> None:
    # Highest-risk cross-scope case: the 2-hop intermediate is visible, but the
    # final endpoint sits in a scope the caller cannot read. The endpoint must
    # be dropped as a result even though the path to it is reachable.
    query = _MemoryQuery(
        views={
            "claim:seed": _view("claim:seed", "seed", memory_type=memory.CLAIM.name),
            "evidence:shared": _view(
                "evidence:shared", "shared evidence", memory_type=memory.CLAIM.name
            ),
            "claim:hidden": _view(
                "claim:hidden",
                "hidden endpoint",
                memory_type=memory.CLAIM.name,
                scope="ctx:workspace/hidden",
            ),
        },
        edges=[
            MemoryRelationEdge("evidence:shared", memory.SUPPORTS.name, "claim:seed"),
            MemoryRelationEdge("evidence:shared", memory.SUPPORTS.name, "claim:hidden"),
        ],
    )
    outcome = GraphCandidateService(query).collect(
        seed_ids=["claim:seed"],
        scopes=["ctx:user"],
        types=[],
        max_hops=2,
        graph_id=None,
    )

    assert "claim:hidden" not in {
        candidate.memory_id for candidate in outcome.batch.candidates
    }


def test_graph_candidates_pass_fanout_bound_to_relation_query() -> None:
    # The per-source SQL cap must be threaded into relation_edges so a supernode
    # cannot over-fetch on the hot path. It is a coarse ceiling (max_examined_edges),
    # kept above the Python fanout cap so it never masks fanout truncation.
    query = _MemoryQuery(
        views={
            "preference:seed": _view("preference:seed", "seed"),
            "preference:a": _view("preference:a", "a"),
        },
        edges=[
            MemoryRelationEdge("preference:seed", memory.SUPERSEDES.name, "preference:a"),
        ],
    )
    GraphCandidateService(query, fanout_per_node=7, max_examined_edges=42).collect(
        seed_ids=["preference:seed"],
        scopes=["ctx:user"],
        types=[],
        max_hops=1,
        graph_id=None,
    )

    assert query.last_max_per_source == 42
    query = _MemoryQuery(
        views={
            "preference:seed": _view("preference:seed", "seed"),
            "preference:a": _view("preference:a", "a"),
            "preference:b": _view("preference:b", "b"),
        },
        edges=[
            MemoryRelationEdge("preference:seed", "provides", "capability:noise"),
            MemoryRelationEdge("preference:seed", memory.SUPERSEDES.name, "preference:a"),
            MemoryRelationEdge("preference:seed", memory.SUPERSEDES.name, "preference:b"),
        ],
    )
    outcome = GraphCandidateService(query, fanout_per_node=1).collect(
        seed_ids=["preference:seed"],
        scopes=["ctx:user"],
        types=[],
        max_hops=1,
        graph_id=None,
    )

    assert len(outcome.batch.candidates) == 1
    assert outcome.batch.candidates[0].memory_id in {"preference:a", "preference:b"}
    assert outcome.batch.warnings == ("graph_expand_truncated",)
    assert outcome.batch.metadata["graph_examined_edges"] == 1


def test_graph_failure_degrades_to_direct_lexical_results() -> None:
    query = _MemoryQuery(
        views={"preference:seed": _view("preference:seed", "concise summaries")},
        lexical_ids=["preference:seed"],
        fail_relations=True,
    )
    service = LexicalMemoryRecallService(
        query,
        graph_candidates=GraphCandidateService(query),
    )

    result = asyncio.run(
        service.recall(
            RecallQuery(
                text="concise summaries",
                scopes=("ctx:user",),
                max_hops=2,
                trigger=RecallTrigger.EXPLICIT_SEARCH,
            )
        )
    )

    assert result.status is RecallStatus.OK
    assert [item.memory_id for item in result.items] == ["preference:seed"]
    assert any(warning.startswith("graph_expand_degraded:") for warning in result.warnings)


def test_zero_hop_surfaces_active_contradiction_companion() -> None:
    # Governance keeps both sides of a contradiction ACTIVE; recall surfaces only
    # "tabs" lexically, but the CONTRADICTS partner "spaces" must be pulled in
    # even at 0 hops (automatic path) so the model sees the conflict.
    query = _MemoryQuery(
        views={
            "preference:tabs": _view(
                "preference:tabs",
                "prefers tabs",
                outgoing=((memory.CONTRADICTS.name, "preference:spaces"),),
            ),
            "preference:spaces": _view(
                "preference:spaces",
                "prefers spaces",
                incoming=((memory.CONTRADICTS.name, "preference:tabs"),),
            ),
        },
        lexical_ids=["preference:tabs"],
    )
    service = LexicalMemoryRecallService(query, graph_candidates=GraphCandidateService(query))

    result = asyncio.run(
        service.recall(
            RecallQuery(
                text="prefers tabs",
                scopes=("ctx:user",),
                max_hops=0,
                trigger=RecallTrigger.CHEAP_AUTO,
            )
        )
    )

    by_id = {item.memory_id: item for item in result.items}
    assert "preference:spaces" in by_id  # companion surfaced at 0 hops
    companion = by_id["preference:spaces"]
    assert companion.direct_match is False
    assert "contradiction_companion" in companion.why
    assert companion.conflicts_with == ("preference:tabs",)
    # The surfaced source is annotated as conflicting too.
    assert by_id["preference:tabs"].conflicts_with == ("preference:spaces",)


def test_zero_hop_contradiction_companion_respects_scope_isolation() -> None:
    # The contradiction partner lives in a scope the caller cannot read; it must
    # NOT surface even though the contradiction edge exists.
    query = _MemoryQuery(
        views={
            "preference:tabs": _view(
                "preference:tabs",
                "prefers tabs",
                outgoing=((memory.CONTRADICTS.name, "preference:hidden"),),
            ),
            "preference:hidden": _view(
                "preference:hidden",
                "hidden side",
                scope="ctx:workspace/hidden",
                incoming=((memory.CONTRADICTS.name, "preference:tabs"),),
            ),
        },
        lexical_ids=["preference:tabs"],
    )
    service = LexicalMemoryRecallService(query, graph_candidates=GraphCandidateService(query))

    result = asyncio.run(
        service.recall(
            RecallQuery(
                text="prefers tabs",
                scopes=("ctx:user",),
                max_hops=0,
                trigger=RecallTrigger.CHEAP_AUTO,
            )
        )
    )

    assert "preference:hidden" not in {item.memory_id for item in result.items}


def test_zero_hop_contradiction_companion_excludes_inactive_partner() -> None:
    # A SUPERSEDED contradiction partner must not surface as a companion.
    query = _MemoryQuery(
        views={
            "preference:tabs": _view(
                "preference:tabs",
                "prefers tabs",
                outgoing=((memory.CONTRADICTS.name, "preference:retired"),),
            ),
            "preference:retired": _view(
                "preference:retired",
                "retired side",
                status=memory.NodeStatus.SUPERSEDED,
                incoming=((memory.CONTRADICTS.name, "preference:tabs"),),
            ),
        },
        lexical_ids=["preference:tabs"],
    )
    service = LexicalMemoryRecallService(query, graph_candidates=GraphCandidateService(query))

    result = asyncio.run(
        service.recall(
            RecallQuery(
                text="prefers tabs",
                scopes=("ctx:user",),
                max_hops=0,
                trigger=RecallTrigger.CHEAP_AUTO,
            )
        )
    )

    assert "preference:retired" not in {item.memory_id for item in result.items}


def test_lexical_recall_fuses_graph_channel_and_attaches_grounded_path() -> None:
    query = _MemoryQuery(
        views={
            "preference:seed": _view("preference:seed", "concise summaries"),
            "preference:neighbor": _view("preference:neighbor", "avoid long reports"),
        },
        edges=[
            MemoryRelationEdge(
                "preference:seed",
                memory.CONTRADICTS.name,
                "preference:neighbor",
            )
        ],
        lexical_ids=["preference:seed"],
    )
    service = LexicalMemoryRecallService(
        query,
        graph_candidates=GraphCandidateService(query),
        enable_graph_rerank=False,
    )

    result = asyncio.run(
        service.recall(
            RecallQuery(
                text="concise summaries",
                scopes=("ctx:user",),
                limit=5,
                max_hops=1,
                trigger=RecallTrigger.EXPLICIT_SEARCH,
            )
        )
    )

    by_id = {item.memory_id: item for item in result.items}
    assert by_id["preference:seed"].direct_match is True
    assert by_id["preference:seed"].hop_count == 0
    assert by_id["preference:neighbor"].direct_match is False
    assert by_id["preference:neighbor"].hop_count == 1
    assert by_id["preference:neighbor"].paths[0].seed_memory_id == "preference:seed"


def test_automatic_recall_forces_zero_hop_even_when_query_requests_two() -> None:
    query = _MemoryQuery(
        views={"preference:seed": _view("preference:seed", "concise summaries")},
        lexical_ids=["preference:seed"],
    )
    service = LexicalMemoryRecallService(query, graph_candidates=GraphCandidateService(query))

    asyncio.run(
        service.recall(
            RecallQuery(
                text="concise summaries",
                scopes=("ctx:user",),
                max_hops=2,
                trigger=RecallTrigger.CHEAP_AUTO,
            )
        )
    )

    assert query.relation_calls == 0


def test_memory_search_protocol_defaults_to_zero_and_serializes_paths() -> None:
    path = RecallPath(
        seed_memory_id="preference:seed",
        target_memory_id="preference:target",
        steps=(
            RecallPathStep(
                from_id="preference:seed",
                to_id="preference:target",
                predicate=memory.CONTRADICTS.name,
                edge_source_id="preference:target",
                edge_target_id="preference:seed",
                traversal="reverse",
            ),
        ),
    )
    recall = _RecallService(
        RecallResult(
            status=RecallStatus.OK,
            items=(
                RecallItem(
                    memory_id="preference:target",
                    memory_type=memory.PREFERENCE.name,
                    scope="ctx:user",
                    status=memory.NodeStatus.ACTIVE,
                    snippet="target",
                    score=0.5,
                    why=("graph", "contradiction_warning"),
                    deep_recall="memory_get preference:target",
                    conflicts_with=("preference:seed",),
                    direct_match=False,
                    hop_count=1,
                    paths=(path,),
                ),
            ),
        )
    )
    tool = MemorySearchTool(recall=recall)

    payload = json.loads(
        tool.execute(
            ToolCall(id="call:search", name="memory_search", arguments={"query": "target"})
        ).output
    )

    assert recall.query is not None
    assert recall.query.max_hops == 0
    assert payload["results"][0]["direct_match"] is False
    assert payload["results"][0]["hop_count"] == 1
    assert payload["results"][0]["paths"][0]["steps"][0]["traversal"] == "reverse"
    # conflicts_with must be serialized so the model can pair multi-conflict
    # results even at 0 hops (where no grounded path is attached).
    assert payload["results"][0]["conflicts_with"] == ["preference:seed"]


def test_memory_search_relaxes_inferred_kind_when_it_would_empty_recall() -> None:
    recall = _KindSensitiveRecallService()
    tool = MemorySearchTool(recall=recall)

    payload = json.loads(
        tool.execute(
            ToolCall(
                id="call:search",
                name="memory_search",
                arguments={
                    "query": "weekly digest schedule timezone",
                    "kind": "Claim",
                },
            )
        ).output
    )

    assert payload["status"] == "ok"
    assert payload["warnings"] == ["kind_filter_relaxed"]
    assert payload["results"][0]["memory_id"] == "observation:timezone"
    assert [query.types for query in recall.queries] == [("Claim",), (), ()]
    assert [query.trace for query in recall.queries] == [False, False, True]


def test_memory_search_relaxes_visible_scope_when_it_would_empty_recall() -> None:
    recall = _ScopeSensitiveRecallService()
    tool = MemorySearchTool(
        recall=recall,
        read_scopes=frozenset({"ctx:user", "ctx:workspace/current"}),
    )

    payload = json.loads(
        tool.execute(
            ToolCall(
                id="call:search",
                name="memory_search",
                arguments={
                    "query": "Project Lumen persistence stack",
                    "scope": "ctx:workspace/current",
                },
            )
        ).output
    )

    assert payload["status"] == "ok"
    assert payload["warnings"] == ["scope_filter_relaxed"]
    assert payload["results"][0]["memory_id"] == "decision:persistence"
    assert [query.scopes for query in recall.queries] == [
        ("ctx:workspace/current",),
        ("ctx:user", "ctx:workspace/current"),
        ("ctx:user", "ctx:workspace/current"),
    ]
    assert [query.trace for query in recall.queries] == [False, False, True]


@pytest.mark.parametrize("max_hops", [-1, 3])
def test_memory_search_rejects_out_of_range_hops(max_hops: int) -> None:
    tool = MemorySearchTool(recall=_RecallService(RecallResult(status=RecallStatus.EMPTY)))
    with pytest.raises(ValueError):
        tool.execute(
            ToolCall(
                id="call:search",
                name="memory_search",
                arguments={"query": "target", "max_hops": max_hops},
            )
        )


@dataclass
class _RecallService:
    result: RecallResult
    query: RecallQuery | None = None

    async def recall(self, query: RecallQuery, *, graph_id: str | None = None) -> RecallResult:
        self.query = query
        return self.result


@dataclass
class _KindSensitiveRecallService:
    queries: list[RecallQuery] = field(default_factory=list)

    async def recall(self, query: RecallQuery, *, graph_id: str | None = None) -> RecallResult:
        self.queries.append(query)
        if query.types:
            return RecallResult(status=RecallStatus.EMPTY, guidance=("no match",))
        return RecallResult(
            status=RecallStatus.OK,
            items=(
                RecallItem(
                    memory_id="observation:timezone",
                    memory_type=memory.OBSERVATION.name,
                    scope="ctx:user",
                    status=memory.NodeStatus.ACTIVE,
                    snippet="The weekly digest schedule is interpreted in Asia/Shanghai local time.",
                    score=0.5,
                    why=("vector",),
                    deep_recall="memory_get observation:timezone",
                ),
            ),
        )


@dataclass
class _ScopeSensitiveRecallService:
    queries: list[RecallQuery] = field(default_factory=list)

    async def recall(self, query: RecallQuery, *, graph_id: str | None = None) -> RecallResult:
        self.queries.append(query)
        if query.scopes == ("ctx:workspace/current",):
            return RecallResult(status=RecallStatus.EMPTY, guidance=("no match",))
        return RecallResult(
            status=RecallStatus.OK,
            items=(
                RecallItem(
                    memory_id="decision:persistence",
                    memory_type=memory.DECISION.name,
                    scope="ctx:user",
                    status=memory.NodeStatus.ACTIVE,
                    snippet="Project Lumen selected PostgreSQL with pgvector.",
                    score=0.5,
                    why=("vector",),
                    deep_recall="memory_get decision:persistence",
                ),
            ),
        )


@dataclass
class _MemoryQuery:
    views: dict[str, CanonicalNodeView]
    edges: list[MemoryRelationEdge] = field(default_factory=list)
    lexical_ids: list[str] = field(default_factory=list)
    fail_relations: bool = False
    relation_calls: int = 0
    last_max_per_source: int | None = None

    def fetch_nodes(self, ids, *, graph_id=None):
        return [self.views[memory_id] for memory_id in ids if memory_id in self.views]

    def relation_edges(self, node_ids, *, graph_id=None, max_per_source=None):
        self.relation_calls += 1
        if self.fail_relations:
            raise RuntimeError("graph unavailable")
        selected = set(node_ids)
        matched = [
            edge for edge in self.edges if edge.source_id in selected or edge.target_id in selected
        ]
        if max_per_source is not None and max_per_source > 0:
            self.last_max_per_source = max_per_source
            per_endpoint: dict[str, int] = {}
            bounded: list[MemoryRelationEdge] = []
            for edge in matched:
                kept = False
                for endpoint in {edge.source_id, edge.target_id} & selected:
                    if per_endpoint.get(endpoint, 0) < max_per_source:
                        per_endpoint[endpoint] = per_endpoint.get(endpoint, 0) + 1
                        kept = True
                if kept:
                    bounded.append(edge)
            return bounded
        return matched

    def lexical_candidates(self, *, terms, scopes, types, limit, graph_id=None):
        return [(memory_id, 1.0) for memory_id in self.lexical_ids[:limit]]

    def fts_candidates(self, *, query_text, scopes, types, limit, graph_id=None):
        return []

    def exact_candidates(self, *, statement, scope, memory_type, graph_id=None):
        return []

    def missing_vector_ids(self, *, embedding_fingerprint, scopes, types, limit, graph_id=None):
        return []


def _view(
    memory_id: str,
    statement: str,
    *,
    memory_type: str = memory.PREFERENCE.name,
    scope: str = "ctx:user",
    status: memory.NodeStatus = memory.NodeStatus.ACTIVE,
    outgoing: tuple[tuple[str, str], ...] = (),
    incoming: tuple[tuple[str, str], ...] = (),
) -> CanonicalNodeView:
    now = datetime.now(UTC)
    return CanonicalNodeView(
        id=memory_id,
        memory_type=memory_type,
        scope=scope,
        status=status,
        statement=statement,
        summary=None,
        source_authority=None,
        verification_status=None,
        confidence_level=None,
        applies_when=None,
        do_not_apply_when=None,
        created_at=now,
        updated_at=now,
        evidence_ids=(),
        outgoing=outgoing,
        incoming=incoming,
    )
