"""Explicit governance UOW fake for non-transactional logic tests."""

from __future__ import annotations

from collections.abc import Callable

from pulsara_agent.graph import GraphStore
from pulsara_agent.memory.candidates.pool import CandidatePool
from pulsara_agent.memory.canonical.unit_of_work import (
    GovernanceWriteUnitOfWork,
    InMemoryMemoryWriteUnitOfWork,
)
from pulsara_agent.memory.canonical.write_service import MemoryWriteService


class FakeMemoryWriteUnitOfWork(InMemoryMemoryWriteUnitOfWork):
    """Non-transactional fake; never evidence for production atomicity."""


def fake_memory_uow_factory(
    *,
    graph: GraphStore,
    candidate_pool: CandidatePool,
    memory_write_service: MemoryWriteService,
    graph_id: str | None = None,
) -> Callable[[], GovernanceWriteUnitOfWork]:
    return lambda: FakeMemoryWriteUnitOfWork(
        graph=graph,
        candidate_pool=candidate_pool,
        memory_write_service=memory_write_service,
        graph_id=graph_id,
    )
