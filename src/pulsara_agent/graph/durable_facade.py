"""Durable graph facade for Postgres truth plus Oxigraph cleanup mirroring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pulsara_agent.graph.postgres import PostgresGraphStore
from pulsara_agent.jsonld import Term
from pulsara_agent.ontology import memory
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.projection_jobs.mutation_writer import (
        CanonicalMutationV2Writer,
    )


@dataclass(slots=True)
class DurableGraphFacade:
    """Use PostgreSQL as truth and emit external projections in the same UOW."""

    postgres: PostgresGraphStore
    mutation_writer: "CanonicalMutationV2Writer | None" = None

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
        from pulsara_agent.runtime.projection_jobs.contracts import (
            CanonicalMutationSurface,
        )
        from pulsara_agent.runtime.projection_jobs.mutation_writer import (
            CanonicalMutationV2Writer,
        )

        writer = self.mutation_writer
        provider = self.postgres.connection_provider
        if writer is None or provider is None:
            self.postgres.delete_graph(graph_id)
            return
        with provider.connection(
            lane=PostgresConnectionLane.MEMORY_UOW,
            deadline_monotonic=monotonic() + 30.0,
        ) as connection:
            PostgresGraphStore(connection=connection).delete_graph(graph_id)
            CanonicalMutationV2Writer(
                connection=connection,
                surface_plan=writer.surface_plan,
            ).append_graph_maintenance_mutation(
                graph_id=graph_id,
                maintenance_operation_id=f"graph-delete:{uuid4().hex}",
                maintenance_kind="graph_delete",
                requested_surfaces=(CanonicalMutationSurface.OXIGRAPH,),
            )
