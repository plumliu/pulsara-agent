"""Owner-scoped canonical-memory write unit of work."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from types import TracebackType
from typing import Self, Protocol
from uuid import uuid4

from pulsara_agent.event import EventContext
from pulsara_agent.graph import DEFAULT_GRAPH_ID
from pulsara_agent.memory.canonical.uow_contracts import (
    MemoryUowDecisionFacade,
    MemoryUowGraphFacade,
    MemoryUowLifecycleFacade,
    MemoryUowMutationOutboxFacade,
    MemoryUowRuntimeEventOutboxFacade,
    MemoryUowTransactionScope,
    MemoryUowTransactionScopeFactory,
    MemoryUowWriteServiceFacade,
    build_memory_uow_scope_request,
)
from pulsara_agent.memory.canonical.write_gate import (
    MEMORY_WRITE_GATE_CONTRACT_FINGERPRINT,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.ports.projection_jobs import RuntimeSessionOwnerBootstrapPort
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.projection_jobs.canonical_mutation import build_surface_plan
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationSurface,
    CanonicalMutationSurfacePlanFact,
    DurableProjectionCommitConfirmation,
)


class GovernanceWriteUnitOfWork(Protocol):
    """Structural contract required by the governance executor."""

    graph: MemoryUowGraphFacade
    decisions: MemoryUowDecisionFacade
    outbox: MemoryUowMutationOutboxFacade
    runtime_events: MemoryUowRuntimeEventOutboxFacade
    lifecycle: MemoryUowLifecycleFacade
    memory_write_service: MemoryUowWriteServiceFacade

    @property
    def resolved_graph_id(self) -> str: ...

    def ensure_event_context_rows(self, context: EventContext) -> None: ...

    def lock_canonical_memory(
        self, memory_id: str
    ) -> tuple[dict[str, object], int] | None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


@dataclass(slots=True)
class MemoryWriteUnitOfWork:
    """Borrow one revocable repository bundle from a transaction scope."""

    scope_factory: MemoryUowTransactionScopeFactory
    runtime_session_id: str
    archive: ArtifactStore
    session_bootstrap: RuntimeSessionOwnerBootstrapPort
    graph_id: str | None = None
    workspace_root: str | Path | None = None
    canonical_mutation_surface_plan: CanonicalMutationSurfacePlanFact = field(
        default_factory=lambda: build_surface_plan(
            (
                CanonicalMutationSurface.SEARCH_INDEX,
                CanonicalMutationSurface.OXIGRAPH,
            )
        )
    )

    graph: MemoryUowGraphFacade = field(init=False)
    decisions: MemoryUowDecisionFacade = field(init=False)
    outbox: MemoryUowMutationOutboxFacade = field(init=False)
    runtime_events: MemoryUowRuntimeEventOutboxFacade = field(init=False)
    lifecycle: MemoryUowLifecycleFacade = field(init=False)
    memory_write_service: MemoryUowWriteServiceFacade = field(init=False)
    _scope: MemoryUowTransactionScope | None = field(
        init=False, default=None, repr=False
    )
    _scope_owner: object | None = field(init=False, default=None, repr=False)

    def __enter__(self) -> "MemoryWriteUnitOfWork":
        deadline = monotonic() + 30.0
        workspace_root = (
            str(self.workspace_root) if self.workspace_root is not None else None
        )
        candidate = self.session_bootstrap.candidate(
            runtime_session_id=self.runtime_session_id,
            workspace_root=workspace_root,
        )
        outcome = self.session_bootstrap.bootstrap(
            candidate=candidate,
            deadline_monotonic=deadline,
        )
        if (
            outcome.confirmation is not DurableProjectionCommitConfirmation.FULL
            or outcome.resulting_state is None
        ):
            raise RuntimeError(
                "runtime session owner bootstrap did not reach FULL: "
                f"{outcome.confirmation.value}"
            )
        request = build_memory_uow_scope_request(
            runtime_session_id=self.runtime_session_id,
            workspace_root=workspace_root,
            graph_id=self.resolved_graph_id,
            session_bootstrap_state=outcome.resulting_state,
            transaction_owner_id=f"memory-uow:{uuid4().hex}",
            transaction_generation=1,
            surface_plan=self.canonical_mutation_surface_plan,
            memory_write_gate_contract_fingerprint=(
                MEMORY_WRITE_GATE_CONTRACT_FINGERPRINT
            ),
            deadline_monotonic=deadline,
        )
        scope_owner = self.scope_factory.open_scope(request=request)
        scope = scope_owner.__enter__()
        self._scope_owner = scope_owner
        self._scope = scope
        repositories = scope.repositories
        self.graph = repositories.graph
        self.decisions = repositories.decisions
        self.outbox = repositories.outbox
        self.runtime_events = repositories.runtime_events
        self.lifecycle = repositories.lifecycle
        self.memory_write_service = repositories.memory_write_service
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        owner = self._scope_owner
        self._scope_owner = None
        self._scope = None
        if owner is not None:
            owner.__exit__(exc_type, exc, traceback)  # type: ignore[attr-defined]

    @property
    def resolved_graph_id(self) -> str:
        return self.graph_id or DEFAULT_GRAPH_ID

    def ensure_event_context_rows(self, context: EventContext) -> None:
        self._require_scope().ensure_event_context_rows(context)

    def lock_canonical_memory(
        self, memory_id: str
    ) -> tuple[dict[str, object], int] | None:
        view = self._require_scope().lock_canonical_memory(memory_id)
        if view is None:
            return None
        document = thaw_json(view.frozen_document)
        if not isinstance(document, dict):
            raise TypeError("locked canonical memory must thaw to an object")
        return document, view.revision

    def _require_scope(self) -> MemoryUowTransactionScope:
        scope = self._scope
        if scope is None:
            raise RuntimeError("memory UOW is not active")
        scope.assert_active()
        return scope


__all__ = [
    "GovernanceWriteUnitOfWork",
    "MemoryWriteUnitOfWork",
]
