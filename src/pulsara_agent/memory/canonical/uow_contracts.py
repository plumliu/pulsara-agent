"""Owner-scoped transaction contracts for canonical memory writes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pulsara_agent.event import AgentEvent, EventContext, MemoryCandidate
from pulsara_agent.graph import GraphStore
from pulsara_agent.memory.candidates.pool import (
    MemoryGovernanceDecisionRecord,
    PooledMemoryCandidate,
)
from pulsara_agent.memory.canonical.write_service import MemoryWriteOutcome
from pulsara_agent.memory.governance.event_outbox import GovernanceEventDispatchTicket
from pulsara_agent.ports.projection_jobs import CanonicalMutationTransactionIdentity
from pulsara_agent.primitives.context import FrozenJsonObjectFact, context_fingerprint
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationSurface,
    CanonicalMutationSurfacePlanFact,
    RuntimeSessionBootstrapStateFact,
)


class MemoryUowScopeLeaseReleasedError(RuntimeError):
    """A retained UOW facade attempted work outside its lexical transaction."""


class MemoryUowFacadeKind(StrEnum):
    GRAPH = "graph"
    DECISIONS = "decisions"
    MUTATION_OUTBOX = "mutation_outbox"
    RUNTIME_EVENT_OUTBOX = "runtime_event_outbox"
    LIFECYCLE = "lifecycle"
    WRITE_SERVICE = "write_service"


class MemoryUowScopeLeaseState(StrEnum):
    ACTIVE = "active"
    REVOKING = "revoking"
    REVOKED = "revoked"
    RELEASED = "released"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class MemoryUowScopeLeaseIdentity:
    scope_id: str
    scope_generation: int
    transaction_identity: CanonicalMutationTransactionIdentity
    owner_thread_identity: str
    identity_fingerprint: str

    def __post_init__(self) -> None:
        if self.scope_generation < 1:
            raise ValueError("memory UOW scope generation must be positive")
        _validate_fingerprint(
            self,
            "identity_fingerprint",
            "memory-uow-scope-lease-identity:v1",
        )


@dataclass(frozen=True, slots=True)
class MemoryUowScopedFacadeIdentity:
    facade_id: str
    facade_kind: MemoryUowFacadeKind
    scope_lease_identity_fingerprint: str
    facade_generation: int
    identity_fingerprint: str

    def __post_init__(self) -> None:
        if self.facade_generation < 1:
            raise ValueError("memory UOW facade generation must be positive")
        _validate_fingerprint(
            self,
            "identity_fingerprint",
            "memory-uow-scoped-facade-identity:v1",
        )


class MemoryUowScopeLease(Protocol):
    @property
    def identity(self) -> MemoryUowScopeLeaseIdentity: ...

    @property
    def state(self) -> MemoryUowScopeLeaseState: ...

    def borrow_operation(
        self, *, facade_identity: MemoryUowScopedFacadeIdentity
    ) -> AbstractContextManager[None]: ...


class MemoryUowScopedFacade(Protocol):
    @property
    def facade_identity(self) -> MemoryUowScopedFacadeIdentity: ...


class MemoryUowGraphFacade(GraphStore, MemoryUowScopedFacade, Protocol):
    pass


class MemoryUowDecisionFacade(MemoryUowScopedFacade, Protocol):
    def append_candidate(
        self, candidate: PooledMemoryCandidate
    ) -> PooledMemoryCandidate: ...

    def append_decision(
        self, record: MemoryGovernanceDecisionRecord
    ) -> MemoryGovernanceDecisionRecord: ...


class MemoryUowMutationOutboxFacade(MemoryUowScopedFacade, Protocol):
    def append_decision(
        self,
        record: MemoryGovernanceDecisionRecord,
        *,
        graph_id: str,
        requested_surfaces: tuple[CanonicalMutationSurface, ...],
    ) -> str | None: ...


class MemoryUowRuntimeEventOutboxFacade(MemoryUowScopedFacade, Protocol):
    def append_batch(
        self,
        events: Sequence[AgentEvent],
        *,
        governance_batch_id: str,
        decision_id: str,
    ) -> GovernanceEventDispatchTicket: ...


class MemoryUowLifecycleFacade(MemoryUowScopedFacade, Protocol):
    def supersede(
        self,
        *,
        old_id: str,
        new_id: str,
        governance_batch_id: str,
        graph_id: str | None = None,
    ) -> list[AgentEvent]: ...

    def mark_stale(
        self,
        *,
        node_id: str,
        governance_batch_id: str,
        graph_id: str | None = None,
    ) -> list[AgentEvent]: ...

    def link_contradiction(
        self,
        *,
        left_id: str,
        right_id: str,
        governance_batch_id: str,
        graph_id: str | None = None,
    ) -> list[AgentEvent]: ...


class MemoryUowWriteServiceFacade(MemoryUowScopedFacade, Protocol):
    def submit(
        self,
        candidate: MemoryCandidate | Mapping[str, Any],
        *,
        event_context: EventContext,
    ) -> MemoryWriteOutcome: ...


@dataclass(frozen=True, slots=True)
class MemoryUowRepositoryBundle:
    scope_lease_identity: MemoryUowScopeLeaseIdentity
    graph: MemoryUowGraphFacade
    decisions: MemoryUowDecisionFacade
    outbox: MemoryUowMutationOutboxFacade
    runtime_events: MemoryUowRuntimeEventOutboxFacade
    lifecycle: MemoryUowLifecycleFacade
    memory_write_service: MemoryUowWriteServiceFacade
    bundle_identity_fingerprint: str

    def __post_init__(self) -> None:
        facades = (
            self.graph,
            self.decisions,
            self.outbox,
            self.runtime_events,
            self.lifecycle,
            self.memory_write_service,
        )
        expected_kinds = tuple(MemoryUowFacadeKind)
        actual_kinds = tuple(item.facade_identity.facade_kind for item in facades)
        if actual_kinds != expected_kinds:
            raise ValueError("memory UOW repository facade inventory mismatch")
        for facade in facades:
            identity = facade.facade_identity
            if (
                identity.scope_lease_identity_fingerprint
                != self.scope_lease_identity.identity_fingerprint
            ):
                raise ValueError("memory UOW facade scope identity mismatch")
        expected = context_fingerprint(
            "memory-uow-repository-bundle:v1",
            {
                "scope_lease_identity_fingerprint": (
                    self.scope_lease_identity.identity_fingerprint
                ),
                "ordered_facade_identity_fingerprints": tuple(
                    item.facade_identity.identity_fingerprint for item in facades
                ),
            },
        )
        if self.bundle_identity_fingerprint != expected:
            raise ValueError("memory UOW repository bundle fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class LockedCanonicalMemoryView:
    memory_id: str
    frozen_document: FrozenJsonObjectFact
    revision: int
    view_fingerprint: str

    def __post_init__(self) -> None:
        _validate_fingerprint(
            self,
            "view_fingerprint",
            "locked-canonical-memory-view:v1",
        )


class MemoryUowTransactionScope(Protocol):
    @property
    def transaction_identity(self) -> CanonicalMutationTransactionIdentity: ...

    @property
    def repositories(self) -> MemoryUowRepositoryBundle: ...

    @property
    def scope_lease_identity(self) -> MemoryUowScopeLeaseIdentity: ...

    @property
    def active(self) -> bool: ...

    @property
    def resolved_graph_id(self) -> str: ...

    def ensure_event_context_rows(self, context: EventContext) -> None: ...

    def lock_canonical_memory(
        self, memory_id: str
    ) -> LockedCanonicalMemoryView | None: ...

    def assert_active(self) -> None: ...


@dataclass(frozen=True, slots=True)
class MemoryUowScopeRequest:
    runtime_session_id: str
    workspace_root: str | None
    graph_id: str
    session_bootstrap_state: RuntimeSessionBootstrapStateFact
    transaction_owner_id: str
    transaction_generation: int
    surface_plan: CanonicalMutationSurfacePlanFact
    memory_write_gate_contract_fingerprint: str
    deadline_monotonic: float
    request_fingerprint: str

    def __post_init__(self) -> None:
        if self.transaction_generation < 1 or self.deadline_monotonic <= 0:
            raise ValueError("memory UOW scope request bounds are invalid")
        if (
            self.session_bootstrap_state.session_owner.runtime_session_id
            != self.runtime_session_id
        ):
            raise ValueError("memory UOW bootstrap session identity mismatch")
        _validate_fingerprint(
            self,
            "request_fingerprint",
            "memory-uow-scope-request:v1",
        )


class MemoryUowTransactionScopeFactory(Protocol):
    def open_scope(
        self, *, request: MemoryUowScopeRequest
    ) -> AbstractContextManager[MemoryUowTransactionScope]: ...


def build_memory_uow_scope_request(
    *,
    runtime_session_id: str,
    workspace_root: str | None,
    graph_id: str,
    session_bootstrap_state: RuntimeSessionBootstrapStateFact,
    transaction_owner_id: str | None,
    transaction_generation: int,
    surface_plan: CanonicalMutationSurfacePlanFact,
    memory_write_gate_contract_fingerprint: str,
    deadline_monotonic: float,
) -> MemoryUowScopeRequest:
    payload = {
        "runtime_session_id": runtime_session_id,
        "workspace_root": workspace_root,
        "graph_id": graph_id,
        "session_bootstrap_state": session_bootstrap_state.model_dump(mode="json"),
        "transaction_owner_id": transaction_owner_id or f"memory-uow:{uuid4().hex}",
        "transaction_generation": transaction_generation,
        "surface_plan": surface_plan.model_dump(mode="json"),
        "memory_write_gate_contract_fingerprint": (
            memory_write_gate_contract_fingerprint
        ),
        "deadline_monotonic": deadline_monotonic,
    }
    return MemoryUowScopeRequest(
        runtime_session_id=runtime_session_id,
        workspace_root=workspace_root,
        graph_id=graph_id,
        session_bootstrap_state=session_bootstrap_state,
        transaction_owner_id=str(payload["transaction_owner_id"]),
        transaction_generation=transaction_generation,
        surface_plan=surface_plan,
        memory_write_gate_contract_fingerprint=(memory_write_gate_contract_fingerprint),
        deadline_monotonic=deadline_monotonic,
        request_fingerprint=context_fingerprint("memory-uow-scope-request:v1", payload),
    )


def build_memory_uow_scope_lease_identity(
    *,
    scope_id: str,
    scope_generation: int,
    transaction_identity: CanonicalMutationTransactionIdentity,
    owner_thread_identity: str,
) -> MemoryUowScopeLeaseIdentity:
    payload = {
        "scope_id": scope_id,
        "scope_generation": scope_generation,
        "transaction_identity": asdict(transaction_identity),
        "owner_thread_identity": owner_thread_identity,
    }
    return MemoryUowScopeLeaseIdentity(
        scope_id=scope_id,
        scope_generation=scope_generation,
        transaction_identity=transaction_identity,
        owner_thread_identity=owner_thread_identity,
        identity_fingerprint=context_fingerprint(
            "memory-uow-scope-lease-identity:v1", payload
        ),
    )


def build_memory_uow_scoped_facade_identity(
    *,
    facade_id: str,
    facade_kind: MemoryUowFacadeKind,
    scope_lease_identity_fingerprint: str,
    facade_generation: int,
) -> MemoryUowScopedFacadeIdentity:
    payload = {
        "facade_id": facade_id,
        "facade_kind": facade_kind.value,
        "scope_lease_identity_fingerprint": scope_lease_identity_fingerprint,
        "facade_generation": facade_generation,
    }
    return MemoryUowScopedFacadeIdentity(
        facade_id=facade_id,
        facade_kind=facade_kind,
        scope_lease_identity_fingerprint=scope_lease_identity_fingerprint,
        facade_generation=facade_generation,
        identity_fingerprint=context_fingerprint(
            "memory-uow-scoped-facade-identity:v1", payload
        ),
    )


def _validate_fingerprint(value: object, field_name: str, namespace: str) -> None:
    payload = asdict(value)
    actual = payload.pop(field_name)
    if actual != context_fingerprint(namespace, payload):
        raise ValueError(f"{field_name} mismatch")


__all__ = [name for name in globals() if name.startswith("MemoryUow")]
__all__ += ["LockedCanonicalMemoryView"]
