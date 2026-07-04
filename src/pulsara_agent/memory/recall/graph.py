"""Bounded typed graph expansion for explicit durable-memory search."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Sequence

from pulsara_agent.memory.canonical.query import CanonicalNodeView, MemoryQuery, MemoryRelationEdge
from pulsara_agent.memory.recall.candidates import CandidateBatch, ChannelCandidate
from pulsara_agent.ontology import memory


TraversalDirection = Literal["forward", "reverse"]


@dataclass(frozen=True, slots=True)
class RecallPathStep:
    from_id: str
    to_id: str
    predicate: str
    edge_source_id: str
    edge_target_id: str
    traversal: TraversalDirection


@dataclass(frozen=True, slots=True)
class RecallPath:
    seed_memory_id: str
    target_memory_id: str
    steps: tuple[RecallPathStep, ...]


@dataclass(frozen=True, slots=True)
class GraphCandidateOutcome:
    batch: CandidateBatch = field(default_factory=CandidateBatch)
    paths_by_id: dict[str, tuple[RecallPath, ...]] = field(default_factory=dict)


_EVIDENCE_PREDICATES = frozenset({memory.SUPPORTS.name, memory.HAS_EVIDENCE.name})
_BASIS_PREDICATES = frozenset({memory.BASED_ON.name, memory.DERIVED_FROM.name})
_TRAVERSABLE_PREDICATES = frozenset(
    {
        memory.CONTRADICTS.name,
        memory.SUPERSEDES.name,
        *_EVIDENCE_PREDICATES,
        *_BASIS_PREDICATES,
    }
)
_ONE_HOP_RESULT_PREDICATES = frozenset(
    {
        memory.CONTRADICTS.name,
        memory.SUPERSEDES.name,
        *_BASIS_PREDICATES,
    }
)


@dataclass(frozen=True, slots=True)
class GraphCandidateService:
    memory_query: MemoryQuery
    allow_needs_review: bool = False
    max_seeds: int = 5
    fanout_per_node: int = 20
    max_examined_edges: int = 100
    max_candidates: int = 20
    max_paths_per_candidate: int = 2

    def collect(
        self,
        *,
        seed_ids: Sequence[str],
        scopes: Sequence[str],
        types: Sequence[str],
        max_hops: int,
        graph_id: str | None,
        suppressed_ids: set[str] | None = None,
    ) -> GraphCandidateOutcome:
        if max_hops <= 0:
            return GraphCandidateOutcome()
        max_hops = min(max_hops, 2)
        suppressed = suppressed_ids or set()
        seed_views = {
            view.id: view
            for view in self.memory_query.fetch_nodes(seed_ids[: self.max_seeds], graph_id=graph_id)
        }
        seeds = [
            memory_id
            for memory_id in seed_ids[: self.max_seeds]
            if memory_id not in suppressed
            and (view := seed_views.get(memory_id)) is not None
            and self._visible(view, scopes=scopes, types=types)
        ]
        if not seeds:
            return GraphCandidateOutcome(metadata_batch(max_hops=max_hops, seed_ids=()))

        paths_by_id: dict[str, list[RecallPath]] = {}
        scores_by_id: dict[str, float] = {}
        frontier: list[tuple[str, str, tuple[RecallPathStep, ...], tuple[str, ...]]] = [
            (seed_id, seed_id, (), (seed_id,)) for seed_id in seeds
        ]
        examined_edges = 0
        truncated = False

        for hop in range(1, max_hops + 1):
            frontier_ids = tuple(dict.fromkeys(current_id for _, current_id, _, _ in frontier))
            # Per-source SQL ceiling is a coarse supernode safety valve, NOT the
            # fine-grained fanout cap. It sits at max_examined_edges (well above
            # fanout_per_node) so a high-degree node can't pull thousands of rows
            # onto the hot path, while leaving the Python-side fanout_per_node cap
            # and its graph_expand_truncated detection fully intact.
            edges = self.memory_query.relation_edges(
                frontier_ids,
                graph_id=graph_id,
                max_per_source=self.max_examined_edges,
            )
            edges_by_id = _edges_by_endpoint(edges)
            expanded: list[tuple[str, str, tuple[RecallPathStep, ...], tuple[str, ...]]] = []
            for seed_id, current_id, steps, visited in frontier:
                adjacent = edges_by_id.get(current_id, ())[: self.fanout_per_node]
                if len(edges_by_id.get(current_id, ())) > self.fanout_per_node:
                    truncated = True
                for edge in adjacent:
                    if examined_edges >= self.max_examined_edges:
                        truncated = True
                        break
                    examined_edges += 1
                    step = _step_from_edge(current_id, edge)
                    if step is None or step.predicate not in _TRAVERSABLE_PREDICATES:
                        continue
                    if step.to_id in visited:
                        continue
                    next_steps = (*steps, step)
                    if hop == 2 and not _allowed_two_hop_motif(next_steps):
                        continue
                    expanded.append((seed_id, step.to_id, next_steps, (*visited, step.to_id)))
                if examined_edges >= self.max_examined_edges:
                    break
            if not expanded:
                break

            next_ids = tuple(dict.fromkeys(current_id for _, current_id, _, _ in expanded))
            node_views = {
                view.id: view for view in self.memory_query.fetch_nodes(next_ids, graph_id=graph_id)
            }
            next_frontier: list[tuple[str, str, tuple[RecallPathStep, ...], tuple[str, ...]]] = []
            for seed_id, current_id, steps, visited in expanded:
                view = node_views.get(current_id)
                if view is None:
                    if hop < max_hops and steps[-1].predicate in _EVIDENCE_PREDICATES:
                        next_frontier.append((seed_id, current_id, steps, visited))
                    continue
                if current_id in suppressed or not self._visible(view, scopes=scopes, types=types):
                    continue
                if current_id != seed_id and _is_result_path(steps):
                    path = RecallPath(seed_memory_id=seed_id, target_memory_id=current_id, steps=steps)
                    paths = paths_by_id.setdefault(current_id, [])
                    if path not in paths and len(paths) < self.max_paths_per_candidate:
                        paths.append(path)
                    score = _path_score(seed_rank=seeds.index(seed_id) + 1, steps=steps)
                    scores_by_id[current_id] = max(scores_by_id.get(current_id, 0.0), score)
                if hop < max_hops:
                    next_frontier.append((seed_id, current_id, steps, visited))
            frontier = next_frontier
            if examined_edges >= self.max_examined_edges:
                break

        ordered = sorted(scores_by_id, key=lambda memory_id: (-scores_by_id[memory_id], memory_id))
        if len(ordered) > self.max_candidates:
            truncated = True
            ordered = ordered[: self.max_candidates]
        warnings = ("graph_expand_truncated",) if truncated else ()
        batch = CandidateBatch(
            candidates=tuple(
                ChannelCandidate(
                    memory_id=memory_id,
                    channel="graph",
                    raw_score=scores_by_id[memory_id],
                    rank=rank,
                )
                for rank, memory_id in enumerate(ordered, start=1)
            ),
            warnings=warnings,
            metadata={
                "graph_max_hops": max_hops,
                "graph_seed_ids": seeds,
                "graph_candidate_ids": ordered,
                "graph_path_count": sum(len(paths_by_id[memory_id]) for memory_id in ordered),
                "graph_examined_edges": examined_edges,
                "graph_truncated": truncated,
            },
        )
        return GraphCandidateOutcome(
            batch=batch,
            paths_by_id={memory_id: tuple(paths_by_id[memory_id]) for memory_id in ordered},
        )

    def _visible(
        self,
        view: CanonicalNodeView,
        *,
        scopes: Sequence[str],
        types: Sequence[str],
    ) -> bool:
        if scopes and view.scope not in scopes:
            return False
        if types and view.memory_type not in types:
            return False
        return view.status is memory.NodeStatus.ACTIVE or (
            self.allow_needs_review and view.status is memory.NodeStatus.NEEDS_REVIEW
        )


def metadata_batch(*, max_hops: int, seed_ids: Sequence[str]) -> CandidateBatch:
    return CandidateBatch(
        metadata={
            "graph_max_hops": max_hops,
            "graph_seed_ids": list(seed_ids),
            "graph_candidate_ids": [],
            "graph_path_count": 0,
            "graph_examined_edges": 0,
            "graph_truncated": False,
        }
    )


def _edges_by_endpoint(edges: Sequence[MemoryRelationEdge]) -> dict[str, tuple[MemoryRelationEdge, ...]]:
    mutable: dict[str, list[MemoryRelationEdge]] = {}
    for edge in edges:
        if edge.predicate not in _TRAVERSABLE_PREDICATES:
            continue
        mutable.setdefault(edge.source_id, []).append(edge)
        if edge.target_id != edge.source_id:
            mutable.setdefault(edge.target_id, []).append(edge)
    return {
        node_id: tuple(sorted(rows, key=lambda edge: (edge.predicate, edge.source_id, edge.target_id)))
        for node_id, rows in mutable.items()
    }


def _step_from_edge(current_id: str, edge: MemoryRelationEdge) -> RecallPathStep | None:
    if edge.source_id == current_id:
        return RecallPathStep(
            from_id=current_id,
            to_id=edge.target_id,
            predicate=edge.predicate,
            edge_source_id=edge.source_id,
            edge_target_id=edge.target_id,
            traversal="forward",
        )
    if edge.target_id == current_id:
        return RecallPathStep(
            from_id=current_id,
            to_id=edge.source_id,
            predicate=edge.predicate,
            edge_source_id=edge.source_id,
            edge_target_id=edge.target_id,
            traversal="reverse",
        )
    return None


def _allowed_two_hop_motif(steps: Sequence[RecallPathStep]) -> bool:
    if len(steps) != 2:
        return False
    first, second = steps
    if first.predicate in _EVIDENCE_PREDICATES and second.predicate in _EVIDENCE_PREDICATES:
        return True
    if first.predicate in _BASIS_PREDICATES and second.predicate in _BASIS_PREDICATES:
        return first.traversal != second.traversal
    return (
        first.predicate == memory.SUPERSEDES.name
        and second.predicate == memory.SUPERSEDES.name
        and first.traversal == second.traversal
    )


def _is_result_path(steps: Sequence[RecallPathStep]) -> bool:
    if len(steps) == 1:
        return steps[0].predicate in _ONE_HOP_RESULT_PREDICATES
    return _allowed_two_hop_motif(steps)


def _path_score(*, seed_rank: int, steps: Sequence[RecallPathStep]) -> float:
    if len(steps) == 1:
        relation_weight = 0.95 if steps[0].predicate in {
            memory.CONTRADICTS.name,
            memory.SUPERSEDES.name,
        } else 0.8
    elif all(step.predicate == memory.SUPERSEDES.name for step in steps):
        relation_weight = 0.75
    else:
        relation_weight = 0.7
    return relation_weight / max(seed_rank, 1)
