"""Durable graph facade for Postgres truth plus Oxigraph cleanup mirroring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Any, cast
from uuid import uuid4

from pulsara_agent.graph.postgres import PostgresGraphStore
from pulsara_agent.jsonld import Term
from pulsara_agent.ontology import memory
from pulsara_agent.ports.projection_jobs import CanonicalMutationWriterPort
from pulsara_agent.projection_jobs.canonical_mutation import (
    build_canonical_mutation_bundle,
    subset_surface_plan,
)
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationKind,
    CanonicalMutationSurface,
    CanonicalMutationSurfacePlanFact,
    GraphMaintenanceMutationOwnerFact,
    build_projection_fact,
)


@dataclass(slots=True)
class DurableGraphFacade:
    """Use PostgreSQL as truth and emit external projections in the same UOW."""

    postgres: PostgresGraphStore
    mutation_writer: CanonicalMutationWriterPort | None = None
    mutation_surface_plan: CanonicalMutationSurfacePlanFact | None = None

    def put_jsonld(self, document: dict[str, Any], graph_id: str | None = None) -> None:
        self.postgres.put_jsonld(document, graph_id=graph_id)

    def get_jsonld(self, node_id: str, graph_id: str | None = None) -> dict[str, Any]:
        return self.postgres.get_jsonld(node_id, graph_id=graph_id)

    def has_jsonld(self, node_id: str, graph_id: str | None = None) -> bool:
        return self.postgres.has_jsonld(node_id, graph_id=graph_id)

    def find_by_type(
        self, type_name: Term, graph_id: str | None = None
    ) -> list[dict[str, Any]]:
        return self.postgres.find_by_type(type_name, graph_id=graph_id)

    def query(
        self, sparql: str, bindings: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return self.postgres.query(sparql, bindings=bindings)

    def update(self, sparql: str) -> None:
        self.postgres.update(sparql)

    def set_status(
        self,
        node_id: str,
        status: memory.NodeStatus,
        *,
        updated_at: datetime,
        graph_id: str | None = None,
    ) -> None:
        self.postgres.set_status(
            node_id, status, updated_at=updated_at, graph_id=graph_id
        )

    def delete_graph(self, graph_id: str) -> None:
        writer = self.mutation_writer
        surface_plan = self.mutation_surface_plan
        self.postgres.delete_graph(graph_id)
        if writer is None or surface_plan is None:
            return
        operation_id = f"graph-delete:{uuid4().hex}"
        owner = cast(
            GraphMaintenanceMutationOwnerFact,
            build_projection_fact(
                GraphMaintenanceMutationOwnerFact,
                schema_version="graph_maintenance_mutation_owner.v1",
                owner_kind="graph_maintenance",
                maintenance_operation_id=operation_id,
                maintenance_kind="graph_delete",
                graph_id=graph_id,
                ordered_authority_fingerprints=(),
            ),
        )
        plan = subset_surface_plan(
            surface_plan,
            (CanonicalMutationSurface.OXIGRAPH,),
        )
        bundle = build_canonical_mutation_bundle(
            source_owner=owner,
            mutation_kind=CanonicalMutationKind.GRAPH_DELETE,
            graph_id=graph_id,
            payloads=(
                {
                    "schema_version": "canonical-graph-maintenance-payload.v2",
                    "graph_reset": True,
                    "graph_id": graph_id,
                    "maintenance_kind": "graph_delete",
                },
            ),
            surface_plan=plan,
        )
        writer.append_bundle(
            bundle=bundle,
            deadline_monotonic=monotonic() + 30.0,
        )
