"""Durable graph facade for Postgres truth plus Oxigraph cleanup mirroring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import traceback
from typing import TYPE_CHECKING, Any

from pulsara_agent.graph.oxigraph import OxigraphGraphStore
from pulsara_agent.graph.postgres import PostgresGraphStore
from pulsara_agent.jsonld import Term
from pulsara_agent.ontology import memory

if TYPE_CHECKING:
    from pulsara_agent.memory.canonical.mutation_outbox import MutationOutboxWriter


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DurableGraphFacade:
    """Use Postgres for truth/hot-path reads and mirror graph deletion to Oxigraph.

    Phase 2 still routes all normal writes through Postgres plus async outbox
    materialization. The only direct Oxigraph side effect here is explicit graph
    reset/delete, so named graphs do not linger after Postgres cleanup.
    """

    postgres: PostgresGraphStore
    oxigraph: OxigraphGraphStore | None = None
    mutation_outbox: "MutationOutboxWriter | None" = None

    def put_jsonld(self, document: dict[str, Any], graph_id: str | None = None) -> None:
        self.postgres.put_jsonld(document, graph_id=graph_id)

    def get_jsonld(self, node_id: str, graph_id: str | None = None) -> dict[str, Any]:
        return self.postgres.get_jsonld(node_id, graph_id=graph_id)

    def has_jsonld(self, node_id: str, graph_id: str | None = None) -> bool:
        return self.postgres.has_jsonld(node_id, graph_id=graph_id)

    def find_by_type(self, type_name: Term, graph_id: str | None = None) -> list[dict[str, Any]]:
        return self.postgres.find_by_type(type_name, graph_id=graph_id)

    def query(self, sparql: str, bindings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
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
        self.postgres.set_status(node_id, status, updated_at=updated_at, graph_id=graph_id)

    def delete_graph(self, graph_id: str) -> None:
        from pulsara_agent.memory.canonical.mutation_outbox import (
            CanonicalMutationSurface,
            graph_reset_mutation_payload,
        )

        self.postgres.delete_graph(graph_id)
        reset_outbox_id: str | None = None
        if self.oxigraph is not None and self.mutation_outbox is not None:
            reset_outbox_id = self.mutation_outbox.append_payload(
                graph_reset_mutation_payload(),
                graph_id=graph_id,
                target_entry_key=f"graph-reset:{graph_id}",
                sequence_key=graph_id,
            )
        if self.oxigraph is not None:
            try:
                self.oxigraph.delete_graph(graph_id)
                if reset_outbox_id is not None and self.mutation_outbox is not None:
                    self.mutation_outbox.mark_surface_applied(
                        reset_outbox_id,
                        CanonicalMutationSurface.OXIGRAPH.value,
                    )
            except Exception:
                if reset_outbox_id is not None and self.mutation_outbox is not None:
                    self.mutation_outbox.mark_surface_failed(
                        reset_outbox_id,
                        CanonicalMutationSurface.OXIGRAPH.value,
                        error_text="".join(traceback.format_exc()).strip(),
                    )
                LOGGER.warning("Failed to delete Oxigraph mirror graph %s", graph_id, exc_info=True)
