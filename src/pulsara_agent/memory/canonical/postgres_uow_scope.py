"""PostgreSQL owner-scoped transaction scope for canonical memory writes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, RLock, get_ident
from time import monotonic
from typing import Any, Iterator, cast
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from pulsara_agent.event import AgentEvent, EventContext, MemoryCandidate
from pulsara_agent.graph.postgres import PostgresGraphStore
from pulsara_agent.jsonld import Term
from pulsara_agent.memory.candidates.pool import (
    GovernanceDecision,
    GovernanceWriteOutcome,
    MemoryGovernanceDecisionRecord,
    PooledMemoryCandidate,
    WriteSucceededOutcome,
    decision_target_entry_ids,
)
from pulsara_agent.memory.canonical.ledger import CanonicalMemoryLedger
from pulsara_agent.memory.canonical.lifecycle import MemoryLifecycle
from pulsara_agent.memory.canonical.uow_contracts import (
    LockedCanonicalMemoryView,
    MemoryUowFacadeKind,
    MemoryUowRepositoryBundle,
    MemoryUowScopeLeaseIdentity,
    MemoryUowScopeLeaseReleasedError,
    MemoryUowScopeLeaseState,
    MemoryUowScopeRequest,
    MemoryUowScopedFacadeIdentity,
    MemoryUowTransactionScope,
    build_memory_uow_scope_lease_identity,
    build_memory_uow_scoped_facade_identity,
)
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.memory.canonical.write_service import (
    MemoryWriteOutcome,
    MemoryWriteService,
)
from pulsara_agent.memory.governance.event_outbox import (
    GovernanceEventDispatchTicket,
    GovernanceEventOutboxRepository,
)
from pulsara_agent.ports.projection_jobs import (
    CanonicalMutationCommitPort,
    MemoryUowScopeFactoryAuthority,
    PostgresCanonicalMutationTransactionDriverPort,
    build_memory_uow_physical_transaction_request,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.memory_candidate import CandidatePayload
from pulsara_agent.projection_jobs.canonical_mutation import (
    build_canonical_mutation_bundle,
    subset_surface_plan,
)
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationKind,
    CanonicalMutationSurface,
    GovernanceCanonicalMutationOwnerFact,
    build_projection_fact,
)
from pulsara_agent.storage.postgres_connection_provider import (
    VerifiedPostgresConnectionProviderProtocol,
)


class _MemoryUowScopeLease:
    __slots__ = ("_condition", "_identity", "_in_flight", "_state")

    def __init__(self, identity: MemoryUowScopeLeaseIdentity) -> None:
        self._identity = identity
        self._condition = Condition(RLock())
        self._state = MemoryUowScopeLeaseState.ACTIVE
        self._in_flight = 0

    @property
    def identity(self) -> MemoryUowScopeLeaseIdentity:
        return self._identity

    @property
    def state(self) -> MemoryUowScopeLeaseState:
        with self._condition:
            return self._state

    @contextmanager
    def borrow_operation(
        self, *, facade_identity: MemoryUowScopedFacadeIdentity
    ) -> Iterator[None]:
        with self._condition:
            self._validate_facade(facade_identity)
            if self._state is not MemoryUowScopeLeaseState.ACTIVE:
                raise MemoryUowScopeLeaseReleasedError(
                    "memory UOW scope lease is no longer active"
                )
            if str(get_ident()) != self._identity.owner_thread_identity:
                raise MemoryUowScopeLeaseReleasedError(
                    "memory UOW facade crossed its owner thread"
                )
            self._in_flight += 1
        try:
            yield
        finally:
            with self._condition:
                self._in_flight -= 1
                self._condition.notify_all()

    def revoke_and_drain(self, *, deadline_monotonic: float) -> None:
        with self._condition:
            if self._state in {
                MemoryUowScopeLeaseState.REVOKED,
                MemoryUowScopeLeaseState.RELEASED,
            }:
                return
            self._state = MemoryUowScopeLeaseState.REVOKING
            while self._in_flight:
                remaining = deadline_monotonic - monotonic()
                if remaining <= 0:
                    self._state = MemoryUowScopeLeaseState.RECONCILIATION_REQUIRED
                    raise MemoryUowScopeLeaseReleasedError(
                        "memory UOW facade drain deadline exceeded"
                    )
                self._condition.wait(timeout=remaining)
            self._state = MemoryUowScopeLeaseState.REVOKED

    def mark_released(self) -> None:
        with self._condition:
            if self._state is not MemoryUowScopeLeaseState.REVOKED:
                raise RuntimeError("memory UOW lease released before revoke")
            self._state = MemoryUowScopeLeaseState.RELEASED

    def _validate_facade(self, identity: MemoryUowScopedFacadeIdentity) -> None:
        if (
            identity.scope_lease_identity_fingerprint
            != self._identity.identity_fingerprint
            or identity.facade_generation != self._identity.scope_generation
        ):
            raise MemoryUowScopeLeaseReleasedError(
                "memory UOW facade identity does not belong to this scope"
            )

    def __reduce__(self) -> object:
        raise TypeError("memory UOW scope lease is not serializable")


class _ScopedFacade:
    __slots__ = ("_facade_identity", "_lease")

    def __init__(
        self,
        *,
        lease: _MemoryUowScopeLease,
        facade_identity: MemoryUowScopedFacadeIdentity,
    ) -> None:
        self._lease = lease
        self._facade_identity = facade_identity

    @property
    def facade_identity(self) -> MemoryUowScopedFacadeIdentity:
        return self._facade_identity

    @contextmanager
    def _operation(self) -> Iterator[None]:
        with self._lease.borrow_operation(facade_identity=self._facade_identity):
            yield

    def __reduce__(self) -> object:
        raise TypeError("memory UOW facade is not serializable")


class _GraphFacade(_ScopedFacade):
    __slots__ = ("_graph",)

    def __init__(self, *, graph: PostgresGraphStore, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._graph = graph

    def put_jsonld(self, document: dict[str, Any], graph_id: str | None = None) -> None:
        with self._operation():
            self._graph.put_jsonld(document, graph_id=graph_id)

    def get_jsonld(self, node_id: str, graph_id: str | None = None) -> dict[str, Any]:
        with self._operation():
            return self._graph.get_jsonld(node_id, graph_id=graph_id)

    def has_jsonld(self, node_id: str, graph_id: str | None = None) -> bool:
        with self._operation():
            return self._graph.has_jsonld(node_id, graph_id=graph_id)

    def find_by_type(
        self, type_name: Term, graph_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._operation():
            return self._graph.find_by_type(type_name, graph_id=graph_id)

    def query(
        self, sparql: str, bindings: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        with self._operation():
            return self._graph.query(sparql, bindings=bindings)

    def update(self, sparql: str) -> None:
        with self._operation():
            self._graph.update(sparql)

    def delete_graph(self, graph_id: str) -> None:
        with self._operation():
            self._graph.delete_graph(graph_id)


class _DecisionFacade(_ScopedFacade):
    __slots__ = ("_repository",)

    def __init__(
        self, *, repository: "_CandidateDecisionRepository", **kwargs: object
    ) -> None:
        super().__init__(**kwargs)
        self._repository = repository

    def append_candidate(
        self, candidate: PooledMemoryCandidate
    ) -> PooledMemoryCandidate:
        with self._operation():
            return self._repository.append_candidate(candidate)

    def append_decision(
        self, record: MemoryGovernanceDecisionRecord
    ) -> MemoryGovernanceDecisionRecord:
        with self._operation():
            return self._repository.append_decision(record)


class _MutationOutboxFacade(_ScopedFacade):
    __slots__ = ("_repository",)

    def __init__(
        self, *, repository: "_CanonicalMutationOutboxRepository", **kwargs: object
    ) -> None:
        super().__init__(**kwargs)
        self._repository = repository

    def append_decision(
        self,
        record: MemoryGovernanceDecisionRecord,
        *,
        graph_id: str,
        requested_surfaces: tuple[CanonicalMutationSurface, ...],
    ) -> str | None:
        with self._operation():
            return self._repository.append_decision(
                record,
                graph_id=graph_id,
                requested_surfaces=requested_surfaces,
            )


class _RuntimeEventOutboxFacade(_ScopedFacade):
    __slots__ = ("_repository",)

    def __init__(
        self, *, repository: GovernanceEventOutboxRepository, **kwargs: object
    ) -> None:
        super().__init__(**kwargs)
        self._repository = repository

    def append_batch(
        self,
        events: Sequence[AgentEvent],
        *,
        governance_batch_id: str,
        decision_id: str,
    ) -> GovernanceEventDispatchTicket:
        with self._operation():
            return self._repository.append_batch(
                events,
                governance_batch_id=governance_batch_id,
                decision_id=decision_id,
            )


class _LifecycleFacade(_ScopedFacade):
    __slots__ = ("_lifecycle",)

    def __init__(self, *, lifecycle: MemoryLifecycle, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._lifecycle = lifecycle

    def supersede(
        self,
        *,
        old_id: str,
        new_id: str,
        governance_batch_id: str,
        graph_id: str | None = None,
    ) -> list[AgentEvent]:
        with self._operation():
            return self._lifecycle.supersede(
                old_id=old_id,
                new_id=new_id,
                governance_batch_id=governance_batch_id,
                graph_id=graph_id,
            )

    def mark_stale(
        self, *, node_id: str, governance_batch_id: str, graph_id: str | None = None
    ) -> list[AgentEvent]:
        with self._operation():
            return self._lifecycle.mark_stale(
                node_id=node_id,
                governance_batch_id=governance_batch_id,
                graph_id=graph_id,
            )

    def link_contradiction(
        self,
        *,
        left_id: str,
        right_id: str,
        governance_batch_id: str,
        graph_id: str | None = None,
    ) -> list[AgentEvent]:
        with self._operation():
            return self._lifecycle.link_contradiction(
                left_id=left_id,
                right_id=right_id,
                governance_batch_id=governance_batch_id,
                graph_id=graph_id,
            )


class _WriteServiceFacade(_ScopedFacade):
    __slots__ = ("_service",)

    def __init__(self, *, service: MemoryWriteService, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._service = service

    def submit(
        self,
        candidate: MemoryCandidate | Mapping[str, Any],
        *,
        event_context: EventContext,
    ) -> MemoryWriteOutcome:
        with self._operation():
            return self._service.submit(candidate, event_context=event_context)


class _PostgresMemoryUowTransactionScope:
    __slots__ = (
        "_connection",
        "_graph_identity",
        "_lease",
        "_repositories",
        "_request",
        "_transaction_identity",
    )

    def __init__(
        self,
        *,
        connection: Connection,
        request: MemoryUowScopeRequest,
        lease: _MemoryUowScopeLease,
        repositories: MemoryUowRepositoryBundle,
    ) -> None:
        self._connection = connection
        self._request = request
        self._lease = lease
        self._repositories = repositories
        self._transaction_identity = lease.identity.transaction_identity
        self._graph_identity = repositories.graph.facade_identity

    @property
    def transaction_identity(self):
        return self._transaction_identity

    @property
    def repositories(self) -> MemoryUowRepositoryBundle:
        return self._repositories

    @property
    def scope_lease_identity(self) -> MemoryUowScopeLeaseIdentity:
        return self._lease.identity

    @property
    def active(self) -> bool:
        return self._lease.state is MemoryUowScopeLeaseState.ACTIVE

    @property
    def resolved_graph_id(self) -> str:
        return self._request.graph_id

    def assert_active(self) -> None:
        if not self.active:
            raise MemoryUowScopeLeaseReleasedError("memory UOW scope is not active")

    def ensure_event_context_rows(self, context: EventContext) -> None:
        with self._lease.borrow_operation(facade_identity=self._graph_identity):
            expected_workspace = self._request.workspace_root
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "SELECT workspace_root FROM sessions WHERE id = %s",
                    (self._request.runtime_session_id,),
                )
                session_row = cursor.fetchone()
                if session_row is None or session_row[0] != expected_workspace:
                    raise RuntimeError(
                        "memory UOW requires an exact bootstrapped session owner"
                    )
                cursor.execute(
                    "INSERT INTO runs (id, session_id) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                    (context.run_id, self._request.runtime_session_id),
                )
                cursor.execute(
                    "SELECT session_id FROM runs WHERE id = %s", (context.run_id,)
                )
                row = cursor.fetchone()
                if row is not None and row[0] != self._request.runtime_session_id:
                    raise ValueError("run identity belongs to another runtime session")
                cursor.execute(
                    """
                    INSERT INTO turns (id, session_id, run_id, turn_index)
                    SELECT %s, %s, %s, coalesce(max(turn_index), 0) + 1
                    FROM turns WHERE run_id = %s
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        context.turn_id,
                        self._request.runtime_session_id,
                        context.run_id,
                        context.run_id,
                    ),
                )
                cursor.execute(
                    "SELECT session_id, run_id FROM turns WHERE id = %s",
                    (context.turn_id,),
                )
                row = cursor.fetchone()
                if row is not None and row != (
                    self._request.runtime_session_id,
                    context.run_id,
                ):
                    raise ValueError("turn identity belongs to another owner")

    def lock_canonical_memory(self, memory_id: str) -> LockedCanonicalMemoryView | None:
        with self._lease.borrow_operation(facade_identity=self._graph_identity):
            with self._connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT payload FROM graph_documents WHERE graph_id = %s AND id = %s FOR UPDATE",
                    (self.resolved_graph_id, memory_id),
                )
                document_row = cursor.fetchone()
                if document_row is None:
                    return None
                frozen = freeze_json(document_row["payload"])
                if not isinstance(frozen, FrozenJsonObjectFact):
                    raise TypeError("stored canonical memory payload is not an object")
                cursor.execute(
                    "SELECT node_revision FROM memory_nodes WHERE graph_id = %s AND id = %s FOR UPDATE",
                    (self.resolved_graph_id, memory_id),
                )
                projection_row = cursor.fetchone()
                if projection_row is None:
                    raise ValueError("canonical memory projection is missing")
            payload = {
                "memory_id": memory_id,
                "frozen_document": frozen,
                "revision": int(projection_row["node_revision"]),
            }
            return LockedCanonicalMemoryView(
                **payload,
                view_fingerprint=context_fingerprint(
                    "locked-canonical-memory-view:v1", payload
                ),
            )

    def revoke(self, *, deadline_monotonic: float) -> None:
        self._lease.revoke_and_drain(deadline_monotonic=deadline_monotonic)

    def release(self) -> None:
        self._lease.mark_released()

    def __reduce__(self) -> object:
        raise TypeError("memory UOW transaction scope is not serializable")


@dataclass(slots=True)
class PostgresMemoryUowTransactionScopeFactory:
    connection_provider: VerifiedPostgresConnectionProviderProtocol
    mutation_driver: PostgresCanonicalMutationTransactionDriverPort
    scope_factory_authority: MemoryUowScopeFactoryAuthority
    gate: MemoryWriteGate
    memory_write_gate_contract_fingerprint: str

    @contextmanager
    def open_scope(
        self, *, request: MemoryUowScopeRequest
    ) -> Iterator[MemoryUowTransactionScope]:
        if (
            request.memory_write_gate_contract_fingerprint
            != self.memory_write_gate_contract_fingerprint
        ):
            raise ValueError("memory write gate contract mismatch")
        physical_request = build_memory_uow_physical_transaction_request(
            transaction_owner_id=request.transaction_owner_id,
            transaction_generation=request.transaction_generation,
            deadline_monotonic=request.deadline_monotonic,
            scope_request_fingerprint=request.request_fingerprint,
        )
        with self.connection_provider.memory_uow_physical_transaction(
            request=physical_request,
            scope_factory_authority=self.scope_factory_authority,
            mutation_driver=self.mutation_driver,
        ) as transaction:
            with transaction.borrow_for_scope_factory(
                authority=self.scope_factory_authority
            ) as connection:
                scope = self._build_scope(
                    connection=connection,
                    request=request,
                    transaction=transaction,
                )
                try:
                    yield scope
                finally:
                    scope.revoke(deadline_monotonic=request.deadline_monotonic)
            scope.release()

    def _build_scope(
        self,
        *,
        connection: Connection,
        request: MemoryUowScopeRequest,
        transaction: object,
    ) -> _PostgresMemoryUowTransactionScope:
        capability = cast(Any, transaction)
        lease_identity = build_memory_uow_scope_lease_identity(
            scope_id=f"memory-uow-scope:{uuid4().hex}",
            scope_generation=request.transaction_generation,
            transaction_identity=capability.transaction_identity,
            owner_thread_identity=str(get_ident()),
        )
        lease = _MemoryUowScopeLease(lease_identity)
        graph = PostgresGraphStore(connection=connection)
        decisions = _CandidateDecisionRepository(connection)
        commit_port = capability.issue_canonical_mutation_commit_port(
            authority=self.scope_factory_authority
        )
        outbox = _CanonicalMutationOutboxRepository(
            graph=graph,
            commit_port=commit_port,
            surface_plan=request.surface_plan,
        )
        runtime_events = GovernanceEventOutboxRepository(
            connection,
            runtime_session_id=request.runtime_session_id,
        )
        lifecycle = MemoryLifecycle(graph=graph, mutable=graph)
        write_service = MemoryWriteService(
            ledger=CanonicalMemoryLedger(
                graph=graph,
                gate=self.gate,
                graph_id=request.graph_id,
            )
        )
        identities = {
            kind: build_memory_uow_scoped_facade_identity(
                facade_id=f"memory-uow-facade:{kind.value}:{uuid4().hex}",
                facade_kind=kind,
                scope_lease_identity_fingerprint=lease_identity.identity_fingerprint,
                facade_generation=request.transaction_generation,
            )
            for kind in MemoryUowFacadeKind
        }
        facades = (
            _GraphFacade(
                lease=lease,
                facade_identity=identities[MemoryUowFacadeKind.GRAPH],
                graph=graph,
            ),
            _DecisionFacade(
                lease=lease,
                facade_identity=identities[MemoryUowFacadeKind.DECISIONS],
                repository=decisions,
            ),
            _MutationOutboxFacade(
                lease=lease,
                facade_identity=identities[MemoryUowFacadeKind.MUTATION_OUTBOX],
                repository=outbox,
            ),
            _RuntimeEventOutboxFacade(
                lease=lease,
                facade_identity=identities[MemoryUowFacadeKind.RUNTIME_EVENT_OUTBOX],
                repository=runtime_events,
            ),
            _LifecycleFacade(
                lease=lease,
                facade_identity=identities[MemoryUowFacadeKind.LIFECYCLE],
                lifecycle=lifecycle,
            ),
            _WriteServiceFacade(
                lease=lease,
                facade_identity=identities[MemoryUowFacadeKind.WRITE_SERVICE],
                service=write_service,
            ),
        )
        bundle_fingerprint = context_fingerprint(
            "memory-uow-repository-bundle:v1",
            {
                "scope_lease_identity_fingerprint": lease_identity.identity_fingerprint,
                "ordered_facade_identity_fingerprints": tuple(
                    item.facade_identity.identity_fingerprint for item in facades
                ),
            },
        )
        bundle = MemoryUowRepositoryBundle(
            scope_lease_identity=lease_identity,
            graph=facades[0],
            decisions=facades[1],
            outbox=facades[2],
            runtime_events=facades[3],
            lifecycle=facades[4],
            memory_write_service=facades[5],
            bundle_identity_fingerprint=bundle_fingerprint,
        )
        return _PostgresMemoryUowTransactionScope(
            connection=connection,
            request=request,
            lease=lease,
            repositories=bundle,
        )


@dataclass(slots=True)
class _CandidateDecisionRepository:
    connection: Connection

    def append_candidate(
        self, candidate: PooledMemoryCandidate
    ) -> PooledMemoryCandidate:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_candidates (
                    entry_id, payload, candidate_semantic_fingerprint,
                    origin, source_session_id, source_run_id,
                    source_turn_id, source_reply_id, source_tool_call_id,
                    user_quote, quoted_evidence_locator, source_event_id,
                    source_artifact_id, intent_fingerprint, metadata, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
                """,
                (
                    candidate.entry_id,
                    Jsonb(_payload_adapter.dump_python(candidate.payload, mode="json")),
                    (
                        candidate.candidate_semantic.semantic_fingerprint
                        if candidate.candidate_semantic is not None
                        else None
                    ),
                    candidate.origin.value,
                    candidate.source_session_id,
                    candidate.source_run_id,
                    candidate.source_turn_id,
                    candidate.source_reply_id,
                    candidate.source_tool_call_id,
                    candidate.user_quote,
                    Jsonb(
                        candidate.quoted_evidence_locator.model_dump(mode="json")
                        if candidate.quoted_evidence_locator is not None
                        else None
                    ),
                    candidate.source_event_id,
                    candidate.source_artifact_id,
                    candidate.intent_fingerprint,
                    Jsonb(candidate.metadata),
                    candidate.created_at,
                ),
            )
        return candidate

    def append_decision(
        self, record: MemoryGovernanceDecisionRecord
    ) -> MemoryGovernanceDecisionRecord:
        target_ids = decision_target_entry_ids(record.decision)
        if target_ids:
            with self.connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT entry_id FROM memory_candidates WHERE entry_id = ANY(%s)",
                    (list(target_ids),),
                )
                existing = {row["entry_id"] for row in cursor.fetchall()}
            missing = [entry_id for entry_id in target_ids if entry_id not in existing]
            if missing:
                raise KeyError(
                    f"governance decision references missing candidate entries: {missing}"
                )
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_governance_decisions (
                    decision_id, governance_batch_id, batch_input_fingerprint,
                    batch_input_reference_fingerprint, governance_model_call_id,
                    decision_index, requested_decision_payload_fingerprint,
                    decision_payload_fingerprint, decision, write_outcome, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
                """,
                (
                    record.decision_id,
                    record.governance_batch_id,
                    record.batch_input_fingerprint,
                    record.batch_input_reference_fingerprint,
                    record.governance_model_call_id,
                    record.decision_index,
                    record.requested_decision_payload_fingerprint,
                    record.decision_payload_fingerprint,
                    Jsonb(_decision_adapter.dump_python(record.decision, mode="json")),
                    Jsonb(
                        _outcome_adapter.dump_python(record.write_outcome, mode="json")
                    ),
                    record.created_at,
                ),
            )
        return record


@dataclass(slots=True)
class _CanonicalMutationOutboxRepository:
    graph: PostgresGraphStore
    commit_port: CanonicalMutationCommitPort
    surface_plan: Any

    def append_decision(
        self,
        record: MemoryGovernanceDecisionRecord,
        *,
        graph_id: str,
        requested_surfaces: tuple[CanonicalMutationSurface, ...],
    ) -> str | None:
        if not isinstance(record.write_outcome, WriteSucceededOutcome):
            return None
        affected_ids = tuple(
            dict.fromkeys(
                (
                    record.write_outcome.memory_id,
                    *record.write_outcome.superseded_memory_ids,
                    *record.write_outcome.contradicted_memory_ids,
                )
            )
        )
        documents = tuple(
            {
                "node_id": node_id,
                "document": self.graph.get_jsonld(node_id, graph_id=graph_id),
            }
            for node_id in affected_ids
        )
        payload = {
            "schema_version": "governed-memory-mutation-payload.v2",
            "mutation_lane": "governed_memory",
            "decision_record": record.model_dump(mode="json"),
            "dirty_memory_ids": affected_ids,
            "documents": documents,
        }
        owner = cast(
            GovernanceCanonicalMutationOwnerFact,
            build_projection_fact(
                GovernanceCanonicalMutationOwnerFact,
                schema_version="governance_canonical_mutation_owner.v1",
                owner_kind="memory_governance",
                governance_batch_id=record.governance_batch_id,
                governance_batch_input_fingerprint=record.batch_input_fingerprint,
                decision_id=record.decision_id,
                decision_semantic_fingerprint=record.decision_payload_fingerprint,
                ordered_source_event_reference_fingerprints=record.write_outcome.write_event_ids,
            ),
        )
        bundle = build_canonical_mutation_bundle(
            source_owner=owner,
            mutation_kind=CanonicalMutationKind.GOVERNED_MEMORY,
            graph_id=graph_id,
            payloads=(payload,),
            surface_plan=subset_surface_plan(self.surface_plan, requested_surfaces),
            source_authority_fingerprints=record.write_outcome.write_event_ids,
        )
        receipt = self.commit_port.append_bundle(bundle=bundle)
        if len(receipt.ordered_mutation_receipts) != 1:
            raise RuntimeError("governance mutation append returned wrong cardinality")
        return receipt.ordered_mutation_receipts[0].mutation_id


_payload_adapter = TypeAdapter(CandidatePayload)
_decision_adapter = TypeAdapter(GovernanceDecision)
_outcome_adapter = TypeAdapter(GovernanceWriteOutcome)


__all__ = [
    "PostgresMemoryUowTransactionScopeFactory",
]
