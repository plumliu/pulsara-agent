"""Runtime-owned canonical-mutation transaction writer."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any, Iterator, cast

from psycopg import Connection

from pulsara_agent.projection_jobs.canonical_mutation import (
    build_canonical_mutation_bundle,
    build_surface_handler_contract,
    build_surface_plan,
    canonical_mutation_semantics_for_payloads,
    subset_surface_plan,
)
from pulsara_agent.runtime.projection_jobs.postgres_canonical_mutation_repository import (
    PostgresCanonicalMutationRepository,
)
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationBundleAppendReceiptFact,
    CanonicalMutationKind,
    CanonicalMemoryMutationOperationKind,
    CanonicalMutationSurface,
    CanonicalMutationSurfacePlanFact,
    CanonicalMutationOwner,
    CanonicalMemoryWriteMutationOwnerFact,
    PreparedCanonicalMutationBundleFact,
    GovernanceCanonicalMutationOwnerFact,
    GraphMaintenanceMutationOwnerFact,
    build_projection_fact,
)
from pulsara_agent.ports.projection_jobs import (
    CanonicalMutationDriverAuthority,
    MemoryUowPhysicalTransactionCapability,
    issue_canonical_mutation_driver_authority,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


class PostgresCanonicalMutationTransactionDriver:
    """Closed append driver for an already admitted UOW transaction."""

    def __init__(self) -> None:
        self._driver_authority = issue_canonical_mutation_driver_authority()

    @property
    def driver_authority(self) -> CanonicalMutationDriverAuthority:
        return self._driver_authority

    def append_on_transaction(
        self,
        *,
        transaction: MemoryUowPhysicalTransactionCapability,
        bundle: PreparedCanonicalMutationBundleFact,
    ) -> CanonicalMutationBundleAppendReceiptFact:
        with transaction.borrow_for_mutation_driver(
            authority=self._driver_authority
        ) as connection:
            receipts = (
                PostgresCanonicalMutationRepository.append_candidates_in_transaction(
                    connection,
                    source_owner=bundle.source_owner,
                    surface_plan=bundle.surface_plan,
                    candidates=bundle.ordered_mutation_candidates,
                )
            )
        return cast(
            CanonicalMutationBundleAppendReceiptFact,
            build_projection_fact(
                CanonicalMutationBundleAppendReceiptFact,
                schema_version="canonical_mutation_bundle_append_receipt.v1",
                attempted_bundle_fingerprint=bundle.bundle_fingerprint,
                ordered_mutation_receipts=receipts,
            ),
        )


@dataclass(slots=True)
class CanonicalMutationV2Writer:
    """Append deterministic V2 mutation bundles, optionally in an outer UOW."""

    surface_plan: CanonicalMutationSurfacePlanFact
    connection_provider: VerifiedPostgresConnectionProviderProtocol | None = None
    connection: Connection | None = None

    def __post_init__(self) -> None:
        if (self.connection_provider is None) == (self.connection is None):
            raise ValueError(
                "CanonicalMutationV2Writer requires exactly one connection owner"
            )

    def append_governance_mutation(
        self,
        *,
        payload: dict[str, Any],
        graph_id: str,
        governance_batch_id: str,
        governance_batch_input_fingerprint: str,
        decision_id: str,
        decision_semantic_fingerprint: str,
        source_authority_fingerprints: tuple[str, ...],
        requested_surfaces: tuple[CanonicalMutationSurface, ...],
    ) -> str:
        owner = cast(
            GovernanceCanonicalMutationOwnerFact,
            build_projection_fact(
                GovernanceCanonicalMutationOwnerFact,
                schema_version="governance_canonical_mutation_owner.v1",
                owner_kind="memory_governance",
                governance_batch_id=governance_batch_id,
                governance_batch_input_fingerprint=(governance_batch_input_fingerprint),
                decision_id=decision_id,
                decision_semantic_fingerprint=(decision_semantic_fingerprint),
                ordered_source_event_reference_fingerprints=(
                    source_authority_fingerprints
                ),
            ),
        )
        return self._append(
            payload=payload,
            mutation_kind=CanonicalMutationKind.GOVERNED_MEMORY,
            graph_id=graph_id,
            owner=owner,
            source_authority_fingerprints=source_authority_fingerprints,
            requested_surfaces=requested_surfaces,
        )

    def append_graph_maintenance_mutation(
        self,
        *,
        graph_id: str,
        maintenance_operation_id: str,
        maintenance_kind: str,
        source_authority_fingerprints: tuple[str, ...] = (),
        requested_surfaces: tuple[CanonicalMutationSurface, ...] = (
            CanonicalMutationSurface.OXIGRAPH,
        ),
    ) -> str:
        if maintenance_kind not in {"graph_reset", "graph_delete"}:
            raise ValueError("unsupported graph maintenance mutation kind")
        owner = cast(
            GraphMaintenanceMutationOwnerFact,
            build_projection_fact(
                GraphMaintenanceMutationOwnerFact,
                schema_version="graph_maintenance_mutation_owner.v1",
                owner_kind="graph_maintenance",
                maintenance_operation_id=maintenance_operation_id,
                maintenance_kind=maintenance_kind,
                graph_id=graph_id,
                ordered_authority_fingerprints=(source_authority_fingerprints),
            ),
        )
        mutation_kind = (
            CanonicalMutationKind.GRAPH_RESET
            if maintenance_kind == "graph_reset"
            else CanonicalMutationKind.GRAPH_DELETE
        )
        return self._append(
            payload={
                "schema_version": "canonical-graph-maintenance-payload.v2",
                "graph_reset": True,
                "graph_id": graph_id,
                "maintenance_kind": maintenance_kind,
            },
            mutation_kind=mutation_kind,
            graph_id=graph_id,
            owner=owner,
            source_authority_fingerprints=source_authority_fingerprints,
            requested_surfaces=requested_surfaces,
        )

    def append_canonical_memory_write_mutation(
        self,
        *,
        payload: dict[str, Any],
        graph_id: str,
        operation_id: str,
        operation_kind: CanonicalMemoryMutationOperationKind,
        source_authority_fingerprints: tuple[str, ...] = (),
        requested_surfaces: tuple[CanonicalMutationSurface, ...],
    ) -> str:
        owner = cast(
            CanonicalMemoryWriteMutationOwnerFact,
            build_projection_fact(
                CanonicalMemoryWriteMutationOwnerFact,
                schema_version="canonical_memory_write_mutation_owner.v1",
                owner_kind="canonical_memory_write",
                operation_id=operation_id,
                operation_kind=operation_kind,
                ordered_authority_fingerprints=(source_authority_fingerprints),
            ),
        )
        return self._append(
            payload=payload,
            mutation_kind=CanonicalMutationKind.RUNTIME_SEMANTIC,
            graph_id=graph_id,
            owner=owner,
            source_authority_fingerprints=source_authority_fingerprints,
            requested_surfaces=requested_surfaces,
        )

    def _append(
        self,
        *,
        payload: dict[str, Any],
        mutation_kind: CanonicalMutationKind,
        graph_id: str,
        owner: CanonicalMutationOwner,
        source_authority_fingerprints: tuple[str, ...],
        requested_surfaces: tuple[CanonicalMutationSurface, ...],
    ) -> str:
        surface_plan = subset_surface_plan(
            self.surface_plan,
            requested_surfaces,
        )
        bundle = build_canonical_mutation_bundle(
            source_owner=owner,
            mutation_kind=mutation_kind,
            graph_id=graph_id,
            payloads=(dict(payload),),
            surface_plan=surface_plan,
            source_authority_fingerprints=source_authority_fingerprints,
        )
        receipt = self.append_bundle(
            bundle=bundle,
            deadline_monotonic=monotonic() + 30.0,
        )
        if len(receipt.ordered_mutation_receipts) != 1:
            raise RuntimeError("canonical mutation append returned wrong cardinality")
        return receipt.ordered_mutation_receipts[0].mutation_id

    def append_bundle(
        self,
        *,
        bundle: PreparedCanonicalMutationBundleFact,
        deadline_monotonic: float,
    ) -> CanonicalMutationBundleAppendReceiptFact:
        with self._connection(deadline_monotonic=deadline_monotonic) as connection:
            receipts = (
                PostgresCanonicalMutationRepository.append_candidates_in_transaction(
                    connection,
                    source_owner=bundle.source_owner,
                    surface_plan=bundle.surface_plan,
                    candidates=bundle.ordered_mutation_candidates,
                )
            )
        return cast(
            CanonicalMutationBundleAppendReceiptFact,
            build_projection_fact(
                CanonicalMutationBundleAppendReceiptFact,
                schema_version="canonical_mutation_bundle_append_receipt.v1",
                attempted_bundle_fingerprint=bundle.bundle_fingerprint,
                ordered_mutation_receipts=receipts,
            ),
        )

    @contextmanager
    def _connection(self, *, deadline_monotonic: float) -> Iterator[Connection]:
        if self.connection is not None:
            yield self.connection
            return
        assert self.connection_provider is not None
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.MEMORY_UOW,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            yield connection


__all__ = [
    "CanonicalMutationV2Writer",
    "PostgresCanonicalMutationTransactionDriver",
    "build_canonical_mutation_bundle",
    "build_surface_handler_contract",
    "build_surface_plan",
    "canonical_mutation_semantics_for_payloads",
]
