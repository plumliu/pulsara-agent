"""Explicit governance UOW fake for non-transactional logic tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from pulsara_agent.event import EventContext
from pulsara_agent.graph import DEFAULT_GRAPH_ID, GraphStore
from pulsara_agent.memory.candidates.pool import (
    CandidatePool,
    MemoryGovernanceDecisionRecord,
    PooledMemoryCandidate,
)
from pulsara_agent.memory.canonical.lifecycle import MemoryLifecycle
from pulsara_agent.memory.canonical.postgres_uow_scope import (
    PostgresMemoryUowTransactionScopeFactory,
)
from pulsara_agent.memory.canonical.unit_of_work import (
    GovernanceWriteUnitOfWork,
    MemoryWriteUnitOfWork,
)
from pulsara_agent.memory.canonical.write_gate import (
    MEMORY_WRITE_GATE_CONTRACT_FINGERPRINT,
    MemoryWriteGate,
)
from pulsara_agent.memory.canonical.write_service import MemoryWriteService
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.memory.governance.event_outbox import (
    EphemeralGovernanceEventOutboxRepository,
)
from pulsara_agent.ports.projection_jobs import (
    issue_memory_uow_scope_factory_authority,
)
from pulsara_agent.projection_jobs.contracts import CanonicalMutationSurface
from pulsara_agent.runtime.projection_jobs.mutation_writer import (
    PostgresCanonicalMutationTransactionDriver,
)
from pulsara_agent.storage.postgres_connection_provider import (
    VerifiedPostgresConnectionProviderProtocol,
)
from pulsara_agent.storage.session_bootstrap import (
    PostgresRuntimeSessionOwnerBootstrapPort,
)


_PostgresUowT = TypeVar("_PostgresUowT", bound=MemoryWriteUnitOfWork)


@dataclass(slots=True)
class _PoolDecisionRepository:
    candidate_pool: CandidatePool

    def append_candidate(
        self, candidate: PooledMemoryCandidate
    ) -> PooledMemoryCandidate:
        return self.candidate_pool.append_candidate(candidate)

    def append_decision(
        self, record: MemoryGovernanceDecisionRecord
    ) -> MemoryGovernanceDecisionRecord:
        return self.candidate_pool.append_decision(record)


@dataclass(slots=True)
class _NoopOutboxRepository:
    def append_decision(
        self,
        record: MemoryGovernanceDecisionRecord,
        *,
        graph_id: str,
        requested_surfaces: tuple[CanonicalMutationSurface, ...],
    ) -> str | None:
        del record, graph_id, requested_surfaces
        return None


@dataclass(slots=True)
class FakeMemoryWriteUnitOfWork:
    """Non-transactional component fake; never durable correctness evidence."""

    graph: GraphStore
    candidate_pool: CandidatePool
    memory_write_service: MemoryWriteService
    graph_id: str | None = None
    runtime_session_id: str = "runtime:test-governance"
    decisions: _PoolDecisionRepository = field(init=False)
    outbox: _NoopOutboxRepository = field(init=False)
    runtime_events: EphemeralGovernanceEventOutboxRepository = field(init=False)
    lifecycle: MemoryLifecycle = field(init=False)

    def __post_init__(self) -> None:
        self.decisions = _PoolDecisionRepository(self.candidate_pool)
        self.outbox = _NoopOutboxRepository()
        self.runtime_events = EphemeralGovernanceEventOutboxRepository(
            runtime_session_id=self.runtime_session_id
        )
        self.lifecycle = MemoryLifecycle(graph=self.graph, mutable=self.graph)

    def __enter__(self) -> "FakeMemoryWriteUnitOfWork":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        return None

    @property
    def resolved_graph_id(self) -> str:
        return self.graph_id or DEFAULT_GRAPH_ID

    def ensure_event_context_rows(self, context: EventContext) -> None:
        del context

    def lock_canonical_memory(
        self, memory_id: str
    ) -> tuple[dict[str, object], int] | None:
        try:
            return self.graph.get_jsonld(memory_id, graph_id=self.graph_id), 1
        except KeyError:
            return None


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


def postgres_memory_uow(
    *,
    connection_provider: VerifiedPostgresConnectionProviderProtocol,
    runtime_session_id: str,
    archive: ArtifactStore,
    graph_id: str | None = None,
    workspace_root: str | Path | None = None,
    uow_type: type[_PostgresUowT] = MemoryWriteUnitOfWork,
) -> _PostgresUowT:
    """Build the explicit PostgreSQL UOW composition used by integration tests."""

    return uow_type(
        scope_factory=PostgresMemoryUowTransactionScopeFactory(
            connection_provider=connection_provider,
            mutation_driver=PostgresCanonicalMutationTransactionDriver(),
            scope_factory_authority=issue_memory_uow_scope_factory_authority(),
            gate=MemoryWriteGate(),
            memory_write_gate_contract_fingerprint=(
                MEMORY_WRITE_GATE_CONTRACT_FINGERPRINT
            ),
        ),
        runtime_session_id=runtime_session_id,
        archive=archive,
        session_bootstrap=PostgresRuntimeSessionOwnerBootstrapPort(connection_provider),
        graph_id=graph_id,
        workspace_root=workspace_root,
    )


__all__ = [
    "FakeMemoryWriteUnitOfWork",
    "fake_memory_uow_factory",
    "postgres_memory_uow",
]
