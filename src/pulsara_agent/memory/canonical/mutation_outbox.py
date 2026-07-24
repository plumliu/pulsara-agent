"""Canonical mutation outbox payloads, writers, and surface-state helpers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from enum import StrEnum
from typing import Iterator
from typing import Any
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pulsara_agent.graph import GraphStore
from pulsara_agent.memory.candidates.pool import (
    GovernanceWriteOutcome,
    MemoryGovernanceDecisionRecord,
    WriteSucceededOutcome,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


class CanonicalMutationLane(StrEnum):
    GOVERNED_MEMORY = "governed_memory"
    RUNTIME_SEMANTIC = "runtime_semantic"
    GRAPH_RESET = "graph_reset"


class CanonicalMutationSurface(StrEnum):
    SEARCH_INDEX = "search_index"
    VECTOR_INDEX = "vector_index"
    OXIGRAPH = "oxigraph"


class CanonicalMutationSurfaceState(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"


class CanonicalMutationDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    document: dict[str, Any]


class CanonicalMutationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "canonical_mutation"
    mutation_id: str | None = None
    mutation_lane: CanonicalMutationLane
    decision_record: dict[str, Any] | None = None
    dirty_memory_ids: tuple[str, ...] = ()
    documents: tuple[CanonicalMutationDocument, ...] = ()
    surface_apply_status: dict[str, str] = Field(default_factory=dict)
    source_runtime_session_id: str | None = None
    source_run_id: str | None = None
    source_turn_id: str | None = None
    source_reply_id: str | None = None
    source_artifact_ids: tuple[str, ...] = ()
    graph_reset: bool = False


_PAYLOAD_ADAPTER = TypeAdapter(CanonicalMutationPayload)


def governed_memory_mutation_payload(
    *,
    record: MemoryGovernanceDecisionRecord,
    graph: GraphStore,
    graph_id: str | None,
    async_surfaces: tuple[str, ...],
) -> CanonicalMutationPayload | None:
    affected_ids = affected_memory_ids(record.write_outcome)
    if not affected_ids:
        return None
    documents = tuple(
        CanonicalMutationDocument(
            node_id=node_id,
            document=graph.get_jsonld(node_id, graph_id=graph_id),
        )
        for node_id in affected_ids
    )
    return CanonicalMutationPayload(
        mutation_lane=CanonicalMutationLane.GOVERNED_MEMORY,
        decision_record=record.model_dump(mode="json"),
        dirty_memory_ids=affected_ids,
        documents=documents,
        surface_apply_status={
            surface: CanonicalMutationSurfaceState.PENDING.value for surface in async_surfaces
        },
    )


def runtime_semantic_mutation_payload(
    *,
    node_id: str,
    document: dict[str, Any],
    source_runtime_session_id: str,
    source_run_id: str,
    source_turn_id: str,
    source_reply_id: str,
    source_artifact_ids: tuple[str, ...] = (),
    async_surfaces: tuple[str, ...] = (CanonicalMutationSurface.OXIGRAPH.value,),
) -> CanonicalMutationPayload:
    return CanonicalMutationPayload(
        mutation_lane=CanonicalMutationLane.RUNTIME_SEMANTIC,
        dirty_memory_ids=(),
        documents=(CanonicalMutationDocument(node_id=node_id, document=document),),
        surface_apply_status={
            surface: CanonicalMutationSurfaceState.PENDING.value for surface in async_surfaces
        },
        source_runtime_session_id=source_runtime_session_id,
        source_run_id=source_run_id,
        source_turn_id=source_turn_id,
        source_reply_id=source_reply_id,
        source_artifact_ids=source_artifact_ids,
    )


def graph_reset_mutation_payload(
    *,
    async_surfaces: tuple[str, ...] = (CanonicalMutationSurface.OXIGRAPH.value,),
) -> CanonicalMutationPayload:
    return CanonicalMutationPayload(
        mutation_lane=CanonicalMutationLane.GRAPH_RESET,
        dirty_memory_ids=(),
        documents=(),
        surface_apply_status={
            surface: CanonicalMutationSurfaceState.PENDING.value for surface in async_surfaces
        },
        graph_reset=True,
    )


def parse_mutation_payload(value: Any) -> CanonicalMutationPayload:
    return _PAYLOAD_ADAPTER.validate_python(value)


def payload_json(payload: CanonicalMutationPayload) -> dict[str, Any]:
    return payload.model_dump(mode="json")


def affected_memory_ids(write_outcome: GovernanceWriteOutcome) -> tuple[str, ...]:
    if not isinstance(write_outcome, WriteSucceededOutcome):
        return ()
    memory_ids = [
        write_outcome.memory_id,
        *write_outcome.superseded_memory_ids,
        *write_outcome.contradicted_memory_ids,
    ]
    return tuple(dict.fromkeys(memory_ids))


def pending_surface_names(payload: CanonicalMutationPayload, surface: str) -> bool:
    state = payload.surface_apply_status.get(surface)
    if state is None:
        return False
    return state in {
        CanonicalMutationSurfaceState.PENDING.value,
        CanonicalMutationSurfaceState.FAILED.value,
    }


def mark_surface_applied(payload: CanonicalMutationPayload, surface: str) -> tuple[dict[str, Any], str]:
    statuses = dict(payload.surface_apply_status)
    statuses[surface] = CanonicalMutationSurfaceState.APPLIED.value
    top_level = summarize_outbox_status(statuses)
    updated = payload.model_copy(update={"surface_apply_status": statuses})
    return payload_json(updated), top_level


def mark_surface_failed(payload: CanonicalMutationPayload, surface: str) -> tuple[dict[str, Any], str]:
    statuses = dict(payload.surface_apply_status)
    statuses[surface] = CanonicalMutationSurfaceState.FAILED.value
    top_level = summarize_outbox_status(statuses)
    updated = payload.model_copy(update={"surface_apply_status": statuses})
    return payload_json(updated), top_level


def summarize_outbox_status(surface_apply_status: dict[str, str]) -> str:
    if not surface_apply_status:
        return "applied"
    values = list(surface_apply_status.values())
    if all(value == CanonicalMutationSurfaceState.APPLIED.value for value in values):
        return "applied"
    if all(value == CanonicalMutationSurfaceState.PENDING.value for value in values):
        return "pending"
    if any(value == CanonicalMutationSurfaceState.FAILED.value for value in values):
        return "failed"
    return "partial"


@dataclass(slots=True)
class MutationOutboxWriter:
    connection_provider: VerifiedPostgresConnectionProviderProtocol | None = None
    connection: Connection | None = None

    def __post_init__(self) -> None:
        if (self.connection_provider is None) == (self.connection is None):
            raise ValueError(
                "MutationOutboxWriter requires exactly one verified provider or transaction connection"
            )

    def append_payload(
        self,
        payload: CanonicalMutationPayload | dict[str, Any],
        *,
        graph_id: str,
        target_entry_key: str,
        governance_batch_id: str | None = None,
        decision_id: str | None = None,
        sequence_key: str | None = None,
    ) -> str:
        outbox_id = f"outbox:{uuid4().hex}"
        if isinstance(payload, CanonicalMutationPayload):
            payload_model = payload
        else:
            payload_model = parse_mutation_payload(payload)
        payload_model = payload_model.model_copy(update={"mutation_id": outbox_id})
        body = payload_json(payload_model)
        dirty_memory_ids = list(payload_model.dirty_memory_ids)
        mutation_lane = payload_model.mutation_lane.value
        batch_id = governance_batch_id
        resolved_decision_id = decision_id
        resolved_sequence_key = sequence_key or graph_id
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_write_outbox (
                    outbox_id,
                    graph_id,
                    governance_batch_id,
                    decision_id,
                    mutation_lane,
                    sequence_key,
                    target_entry_key,
                    dirty_memory_ids,
                    payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (governance_batch_id, decision_id)
                WHERE governance_batch_id IS NOT NULL AND decision_id IS NOT NULL
                DO NOTHING
                """,
                (
                    outbox_id,
                    graph_id,
                    batch_id,
                    resolved_decision_id,
                    mutation_lane,
                    resolved_sequence_key,
                    target_entry_key,
                    Jsonb(dirty_memory_ids),
                    Jsonb(body),
                ),
            )
        return outbox_id

    def mark_surface_applied(self, outbox_id: str, surface: str) -> None:
        with self._cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT payload FROM memory_write_outbox WHERE outbox_id = %s FOR UPDATE",
                (outbox_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return
            payload_model = parse_mutation_payload(row["payload"])
            payload, top_level_status = mark_surface_applied(payload_model, surface)
            cursor.execute(
                """
                UPDATE memory_write_outbox
                SET payload = %s,
                    status = %s,
                    attempt_count = attempt_count + 1,
                    last_error = NULL,
                    applied_at = CASE WHEN %s = 'applied' THEN now() ELSE applied_at END
                WHERE outbox_id = %s
                """,
                (Jsonb(payload), top_level_status, top_level_status, outbox_id),
            )

    def mark_surface_failed(self, outbox_id: str, surface: str, *, error_text: str) -> None:
        with self._cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT payload FROM memory_write_outbox WHERE outbox_id = %s FOR UPDATE",
                (outbox_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return
            payload_model = parse_mutation_payload(row["payload"])
            payload, top_level_status = mark_surface_failed(payload_model, surface)
            cursor.execute(
                """
                UPDATE memory_write_outbox
                SET payload = %s,
                    status = %s,
                    attempt_count = attempt_count + 1,
                    last_error = %s
                WHERE outbox_id = %s
                """,
                (Jsonb(payload), top_level_status, error_text, outbox_id),
            )

    @contextmanager
    def _cursor(self, *, row_factory=None) -> Iterator:
        if self.connection is not None:
            cursor_context = (
                self.connection.cursor(row_factory=row_factory)
                if row_factory is not None
                else self.connection.cursor()
            )
            with cursor_context as cursor:
                yield cursor
            return

        assert self.connection_provider is not None
        connection_context = self.connection_provider.connection(
            lane=PostgresConnectionLane.MEMORY_UOW,
            row_factory=row_factory,
            deadline_monotonic=monotonic() + 30.0,
        )
        with connection_context as connection:
            with connection.cursor() as cursor:
                yield cursor
