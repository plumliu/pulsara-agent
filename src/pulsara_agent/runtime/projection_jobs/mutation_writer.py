"""Deterministic canonical-mutation V2 factories and transaction writer."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Any, Iterator, cast

from psycopg import Connection

from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.runtime.projection_jobs.canonical_mutation import (
    PostgresCanonicalMutationRepository,
)
from pulsara_agent.runtime.projection_jobs.contracts import (
    CanonicalInlineJsonDocumentFact,
    CanonicalMutationCandidateFact,
    CanonicalMutationKind,
    CanonicalMemoryMutationOperationKind,
    CanonicalMutationPlannedSurfaceFact,
    CanonicalMutationSemanticFact,
    CanonicalMutationSurface,
    CanonicalMutationSurfaceHandlerContractFact,
    CanonicalMutationSurfacePlanFact,
    CanonicalMutationOwner,
    CanonicalMemoryWriteMutationOwnerFact,
    DurableProjectionDeliveryPolicyFact,
    GovernanceCanonicalMutationOwnerFact,
    GraphMaintenanceMutationOwnerFact,
    PreparedCanonicalMutationBundleFact,
    build_projection_fact,
)
from pulsara_agent.runtime.projection_jobs.registry import (
    default_projection_delivery_policy,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


_MUTATION_CODEC = context_fingerprint(
    "canonical-mutation-inline-json-codec:v2",
    {"codec": "canonical-json", "ensure_ascii": True},
)
_MUTATION_CONTRACT = context_fingerprint(
    "canonical-mutation-payload-contract:v2",
    {"carrier": "inline_json", "surface_state": "external"},
)


def build_surface_handler_contract(
    surface: CanonicalMutationSurface,
    *,
    target_compatibility_fingerprint: str | None = None,
) -> CanonicalMutationSurfaceHandlerContractFact:
    return cast(
        CanonicalMutationSurfaceHandlerContractFact,
        build_projection_fact(
            CanonicalMutationSurfaceHandlerContractFact,
            schema_version="canonical_mutation_surface_handler_contract.v1",
            surface=surface,
            handler_id=f"pulsara.{surface.value}-materializer",
            handler_version="2",
            accepted_mutation_kinds=tuple(CanonicalMutationKind),
            payload_codec_fingerprint=context_fingerprint(
                "canonical-mutation-surface-payload-codec:v2",
                {"surface": surface.value, "codec": _MUTATION_CODEC},
            ),
            target_compatibility_fingerprint=(
                target_compatibility_fingerprint
                or context_fingerprint(
                    "canonical-mutation-surface-target:v2",
                    {"surface": surface.value},
                )
            ),
            idempotency_contract_fingerprint=context_fingerprint(
                "canonical-mutation-surface-idempotency:v2",
                {
                    "surface": surface.value,
                    "identity": "delivery_identity_fingerprint",
                },
            ),
        ),
    )


def build_surface_plan(
    surfaces: tuple[CanonicalMutationSurface, ...],
    *,
    delivery_policy: DurableProjectionDeliveryPolicyFact | None = None,
    target_compatibility_fingerprints: dict[
        CanonicalMutationSurface, str
    ] | None = None,
) -> CanonicalMutationSurfacePlanFact:
    if len(surfaces) != len(set(surfaces)):
        raise ValueError("canonical mutation surface plan contains duplicates")
    policy = delivery_policy or default_projection_delivery_policy()
    compatibilities = target_compatibility_fingerprints or {}
    planned = tuple(
        cast(
            CanonicalMutationPlannedSurfaceFact,
            build_projection_fact(
                CanonicalMutationPlannedSurfaceFact,
                schema_version="canonical_mutation_planned_surface.v1",
                handler_contract=build_surface_handler_contract(
                    surface,
                    target_compatibility_fingerprint=compatibilities.get(
                        surface
                    ),
                ),
                delivery_policy=policy,
            ),
        )
        for surface in surfaces
    )
    return cast(
        CanonicalMutationSurfacePlanFact,
        build_projection_fact(
            CanonicalMutationSurfacePlanFact,
            schema_version="canonical_mutation_surface_plan.v1",
            ordered_surfaces=planned,
            composition_fingerprint=context_fingerprint(
                "canonical-mutation-surface-composition:v2",
                tuple(
                    item.handler_contract.contract_fingerprint
                    for item in planned
                ),
            ),
        ),
    )


def build_canonical_mutation_bundle(
    *,
    source_owner: CanonicalMutationOwner,
    mutation_kind: CanonicalMutationKind,
    graph_id: str,
    payloads: tuple[dict[str, Any], ...],
    surface_plan: CanonicalMutationSurfacePlanFact,
    source_authority_fingerprints: tuple[str, ...] = (),
    mutation_ids: tuple[str, ...] | None = None,
) -> PreparedCanonicalMutationBundleFact:
    if mutation_ids is not None and len(mutation_ids) != len(payloads):
        raise ValueError("canonical mutation ID cardinality mismatch")
    semantics = canonical_mutation_semantics_for_payloads(
        mutation_kind=mutation_kind,
        graph_id=graph_id,
        payloads=payloads,
    )
    candidates: list[CanonicalMutationCandidateFact] = []
    requested_surfaces = tuple(
        item.handler_contract.surface for item in surface_plan.ordered_surfaces
    )
    for ordinal, semantic in enumerate(semantics):
        mutation_id = (
            mutation_ids[ordinal]
            if mutation_ids is not None
            else "canonical-mutation:"
            + context_fingerprint(
                "canonical-mutation-id:v2",
                {
                    "mutation_kind": mutation_kind.value,
                    "source_owner_fingerprint": source_owner.owner_fingerprint,
                    "mutation_ordinal": ordinal,
                    "graph_id": graph_id,
                    "mutation_semantic_fingerprint": (
                        semantic.mutation_semantic_fingerprint
                    ),
                },
            )
        )
        candidates.append(
            cast(
                CanonicalMutationCandidateFact,
                build_projection_fact(
                    CanonicalMutationCandidateFact,
                    schema_version="canonical_mutation_candidate.v2",
                    mutation_id=mutation_id,
                    mutation_ordinal=ordinal,
                    mutation_semantic=semantic,
                    source_owner_fingerprint=source_owner.owner_fingerprint,
                    source_authority_fingerprints=(
                        source_authority_fingerprints
                    ),
                    requested_surfaces=requested_surfaces,
                    surface_plan_fingerprint=surface_plan.plan_fingerprint,
                ),
            )
        )
    return cast(
        PreparedCanonicalMutationBundleFact,
        build_projection_fact(
            PreparedCanonicalMutationBundleFact,
            schema_version="prepared_canonical_mutation_bundle.v1",
            source_owner=source_owner,
            surface_plan=surface_plan,
            ordered_mutation_candidates=tuple(candidates),
        ),
    )


def canonical_mutation_semantics_for_payloads(
    *,
    mutation_kind: CanonicalMutationKind,
    graph_id: str,
    payloads: tuple[dict[str, Any], ...],
) -> tuple[CanonicalMutationSemanticFact, ...]:
    """Freeze mutation semantics before the projection result owner exists."""

    semantics: list[CanonicalMutationSemanticFact] = []
    for payload in payloads:
        encoded = canonical_json_bytes(payload)
        content_sha = f"sha256:{sha256(encoded).hexdigest()}"
        carrier = cast(
            CanonicalInlineJsonDocumentFact,
            build_projection_fact(
                CanonicalInlineJsonDocumentFact,
                schema_version="canonical_inline_json_document.v1",
                carrier_kind="inline_json",
                canonical_json_utf8=encoded.decode("utf-8"),
                canonical_utf8_bytes=len(encoded),
                canonical_sha256=content_sha,
            ),
        )
        semantics.append(
            cast(
                CanonicalMutationSemanticFact,
                build_projection_fact(
                    CanonicalMutationSemanticFact,
                    schema_version="canonical_mutation_semantic.v2",
                    mutation_kind=mutation_kind,
                    graph_id=graph_id,
                    graph_document_semantic_fingerprint=(
                        carrier.document_semantic_fingerprint
                    ),
                    mutation_payload=carrier,
                    mutation_contract_fingerprint=_MUTATION_CONTRACT,
                ),
            )
        )
    return tuple(semantics)


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
                governance_batch_input_fingerprint=(
                    governance_batch_input_fingerprint
                ),
                decision_id=decision_id,
                decision_semantic_fingerprint=(
                    decision_semantic_fingerprint
                ),
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
                ordered_authority_fingerprints=(
                    source_authority_fingerprints
                ),
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
                ordered_authority_fingerprints=(
                    source_authority_fingerprints
                ),
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
        surface_plan = _subset_surface_plan(
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
        with self._connection() as connection:
            receipts = (
                PostgresCanonicalMutationRepository
                .append_candidates_in_transaction(
                    connection,
                    source_owner=bundle.source_owner,
                    surface_plan=bundle.surface_plan,
                    candidates=bundle.ordered_mutation_candidates,
                )
            )
        if len(receipts) != 1:
            raise RuntimeError("canonical mutation append returned wrong cardinality")
        return receipts[0].mutation_id

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        if self.connection is not None:
            yield self.connection
            return
        assert self.connection_provider is not None
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.MEMORY_UOW,
            deadline_monotonic=monotonic() + 30.0,
        ) as connection:
            yield connection


def _subset_surface_plan(
    plan: CanonicalMutationSurfacePlanFact,
    requested: tuple[CanonicalMutationSurface, ...],
) -> CanonicalMutationSurfacePlanFact:
    if not requested:
        return plan
    by_surface = {
        item.handler_contract.surface: item for item in plan.ordered_surfaces
    }
    try:
        selected = tuple(by_surface[surface] for surface in requested)
    except KeyError as error:
        raise ValueError(
            "canonical mutation requested an unbound surface"
        ) from error
    return cast(
        CanonicalMutationSurfacePlanFact,
        build_projection_fact(
            CanonicalMutationSurfacePlanFact,
            schema_version="canonical_mutation_surface_plan.v1",
            ordered_surfaces=selected,
            composition_fingerprint=context_fingerprint(
                "canonical-mutation-surface-composition:v2",
                tuple(
                    item.handler_contract.contract_fingerprint
                    for item in selected
                ),
            ),
        ),
    )


__all__ = [
    "CanonicalMutationV2Writer",
    "build_canonical_mutation_bundle",
    "build_surface_handler_contract",
    "build_surface_plan",
    "canonical_mutation_semantics_for_payloads",
]
