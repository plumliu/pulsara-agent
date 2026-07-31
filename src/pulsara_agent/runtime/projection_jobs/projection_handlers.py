"""Authoritative run-timeline and tool-evidence projection handlers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from time import monotonic
from typing import Any, Iterable, Iterator, cast

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.entities.runtime import Artifact, RunTimelineRecord, ToolResult
from pulsara_agent.event import (
    EventType,
    ToolCallArgumentsSegmentEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTerminalProjectionCommittedEvent,
)
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.jsonld import NodeRef
from pulsara_agent.llm.terminal_projection import (
    hydrate_terminal_projection_text,
    stable_event_identity,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.ontology.registry import CORE_CONTEXT
from pulsara_agent.primitives._context_base import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.terminal_projection import (
    CanonicalToolResultDataBlockSemanticFact,
    TerminalArtifactContentReferenceFact,
    TerminalInlineContentFact,
    ToolResultSemanticSourceFact,
    ToolTerminalProjectionPayloadFact,
)
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationKind,
    DurableContentAddressedArtifactReferenceFact,
    DurableProjectionCommitConfirmation,
    DurableProjectionGraphRelationReferenceFact,
    DurableProjectionKind,
    DurableProjectionLedgerHorizonFact,
    DurableProjectionResultSemanticFact,
    DurableProjectionSourceEventReferenceFact,
    LeasedDurableProjectionJob,
    PreActivationHookResultOwnerFact,
    PreActivationProjectionCoveragePageFact,
    PreActivationProjectionCoverageReceiptFact,
    PreActivationProjectionCoverageSetReferenceFact,
    PreActivationProjectionHookContractFact,
    PreActivationProjectionSessionCutoverFact,
    PreActivationProjectionTargetCoverageItemFact,
    PreparedDurableProjectionArtifactDocumentFact,
    PreparedDurableProjectionGraphDocumentFact,
    PreparedDurableProjectionGraphRelationFact,
    PreparedDurableProjectionResultFact,
    ProjectionJobResultOwnerFact,
    RawRunProjectionSourcePage,
    RunTimelinePersistentStateSemanticFact,
    ToolCallArgumentsEvidenceProjectionFact,
    ToolResultArtifactRelationFact,
    ToolResultEvidenceOutputProjectionFact,
    ToolResultExecutionEvidenceSourceFact,
    TurnProducedToolResultRelationFact,
    build_projection_fact,
    projection_target_key,
)
from pulsara_agent.graph.projection_relations import (
    GRAPH_RELATION_LOWERING_CONTRACT,
)
from pulsara_agent.runtime.projection_jobs.mutation_writer import (
    build_canonical_mutation_bundle,
    canonical_mutation_semantics_for_payloads,
)
from pulsara_agent.runtime.projection_jobs.postgres_repository import (
    PostgresDurableProjectionRepository,
    _bound_event_from_row,
)
from pulsara_agent.runtime.projection_jobs.pre_activation import (
    ledger_horizon_for_session,
)
from pulsara_agent.runtime.projection_jobs.result import (
    build_projection_result_mutation_owner,
    document_semantic_fingerprint,
)
from pulsara_agent.runtime.projection_jobs.timeline import (
    IncrementalRunTimelineReducer,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)
from pulsara_agent.storage.runtime_write_admission import (
    acquire_maintenance_runtime_write_guard,
    read_runtime_write_epoch,
)


_SOURCE_PAGE_EVENTS = 512
_SOURCE_PAGE_BYTES = 8 * 1024 * 1024
_TIMELINE_LEAF_ITEMS = 128
_TIMELINE_LEAF_BYTES = 1024 * 1024
_TIMELINE_MANIFEST_BYTES = 256 * 1024
_EVIDENCE_EVENT_READS = 64
_EVIDENCE_ARTIFACTS = 64
_ARGUMENT_BYTES = 128 * 1024
_SUMMARY_BYTES = 2 * 1024
_OUTPUT_CODEPOINTS = 500
_COVERAGE_PAGE_ITEMS = 256
_COVERAGE_PAGE_BYTES = 8 * 1024 * 1024
_GRAPH_ID = "graph:default"


class _DuplicateJsonObjectKey(ValueError):
    pass


class _NonFiniteJsonNumber(ValueError):
    pass


def _strict_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonObjectKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise _NonFiniteJsonNumber(value)


def _strict_json_float(value: str) -> float:
    parsed = float(value)
    if not isfinite(parsed):
        raise _NonFiniteJsonNumber(value)
    return parsed


def _strict_tool_arguments_evidence(
    arguments_text: str,
) -> tuple[
    str,
    FrozenJsonObjectFact | None,
    str | None,
    bytes | None,
    str,
]:
    try:
        arguments_value = json.loads(
            arguments_text or "{}",
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_strict_json_float,
        )
    except json.JSONDecodeError:
        return (
            "invalid_json",
            None,
            "json_decode_error",
            None,
            arguments_text,
        )
    except _DuplicateJsonObjectKey:
        return (
            "invalid_json",
            None,
            "duplicate_object_key",
            None,
            arguments_text,
        )
    except _NonFiniteJsonNumber:
        return (
            "invalid_json",
            None,
            "non_finite_number",
            None,
            arguments_text,
        )
    if not isinstance(arguments_value, dict):
        return (
            "non_object_json",
            None,
            "top_level_non_object",
            None,
            canonical_json_bytes(arguments_value).decode("utf-8"),
        )
    frozen_arguments = freeze_json(arguments_value)
    if not isinstance(frozen_arguments, FrozenJsonObjectFact):
        raise TypeError("strict tool argument object did not freeze")
    canonical_arguments = canonical_json_bytes(arguments_value)
    return (
        "valid_object",
        frozen_arguments,
        None,
        canonical_arguments,
        canonical_arguments.decode("utf-8"),
    )


_TIMELINE_MANIFEST_MEDIA_TYPE = "application/vnd.pulsara.run-timeline-manifest+json"
_TIMELINE_LEAF_MEDIA_TYPE = "application/vnd.pulsara.run-timeline-leaf+json"
_TOOL_EVIDENCE_SOURCE_MEDIA_TYPE = (
    "application/vnd.pulsara.tool-result-evidence-source+json"
)
_ARTIFACT_STORE_CONTRACT = context_fingerprint(
    "durable-projection-postgres-artifact-store:v1", {}
)
_ARTIFACT_CODEC = context_fingerprint(
    "durable-projection-canonical-json-artifact-codec:v1", {}
)
_ARTIFACT_METADATA = context_fingerprint("durable-projection-artifact-metadata:v1", {})
_JSONLD_CODEC = context_fingerprint("durable-projection-jsonld-codec:v1", {})
_TIMELINE_ITEM_ACCUMULATOR_GENESIS = context_fingerprint(
    "run-timeline-item-accumulator:v1", "empty"
)
_TIMELINE_COMPLETED_LEAF_GENESIS = context_fingerprint(
    "run-timeline-completed-leaf-accumulator:v1", "empty"
)
_TIMELINE_EMPTY_ROOT = context_fingerprint(
    "run-timeline-persistent-vector-root:v1", "empty"
)
_TIMELINE_EMPTY_OPEN_STATE = context_fingerprint("run-timeline-open-state:v1", "empty")


@dataclass(frozen=True, slots=True)
class _TimelineBase:
    state: RunTimelinePersistentStateSemanticFact | None
    item_accumulator: str
    completed_leaf_accumulator: str
    completed_leaf_count: int
    tail_artifact_id: str | None
    tail_document_semantic_fingerprint: str | None
    tail_items: tuple[dict[str, object], ...]
    tail_previous_artifact_id: str | None
    created_at: str | None
    open_state: dict[str, Any] | None


@dataclass(slots=True)
class _TimelineLeafBuilder:
    source_reference: DurableProjectionSourceEventReferenceFact
    completed_leaf_accumulator: str
    completed_leaf_count: int
    tail_items: list[dict[str, object]]
    tail_previous_artifact_id: str | None
    tail_artifact_id: str | None
    tail_document_semantic_fingerprint: str | None
    item_accumulator: str
    documents: list[PreparedDurableProjectionArtifactDocumentFact]
    tail_dirty: bool = False

    @classmethod
    def from_base(
        cls,
        *,
        source_reference: DurableProjectionSourceEventReferenceFact,
        base: _TimelineBase,
    ) -> "_TimelineLeafBuilder":
        return cls(
            source_reference=source_reference,
            completed_leaf_accumulator=base.completed_leaf_accumulator,
            completed_leaf_count=base.completed_leaf_count,
            tail_items=list(base.tail_items),
            tail_previous_artifact_id=base.tail_previous_artifact_id,
            tail_artifact_id=base.tail_artifact_id,
            tail_document_semantic_fingerprint=(
                base.tail_document_semantic_fingerprint
            ),
            item_accumulator=base.item_accumulator,
            documents=[],
        )

    def append(self, items: Iterable[dict[str, object]]) -> None:
        for item in items:
            semantic = str(item["item_semantic_fingerprint"])
            self.item_accumulator = context_fingerprint(
                "run-timeline-item-accumulator:v1",
                {
                    "previous": self.item_accumulator,
                    "item_semantic_fingerprint": semantic,
                    "absolute_item_ordinal": int(item["absolute_item_ordinal"]),
                },
            )
            if len(self.tail_items) == _TIMELINE_LEAF_ITEMS:
                self._seal_completed_tail()
            self.tail_items.append(dict(item))
            self.tail_dirty = True

    def finish(self) -> dict[str, object]:
        if self.tail_dirty:
            self._materialize_tail()
        return {
            "tail_artifact_id": self.tail_artifact_id,
            "tail_document_semantic_fingerprint": (
                self.tail_document_semantic_fingerprint
            ),
            "completed_leaf_accumulator": self.completed_leaf_accumulator,
            "completed_leaf_count": self.completed_leaf_count,
        }

    def _seal_completed_tail(self) -> None:
        if not self.tail_items:
            raise ValueError("cannot seal an empty timeline tail")
        if self.tail_dirty:
            self._materialize_tail()
        if (
            self.tail_artifact_id is None
            or self.tail_document_semantic_fingerprint is None
        ):
            raise ValueError("timeline tail lacks durable semantic identity")
        self.completed_leaf_accumulator = context_fingerprint(
            "run-timeline-completed-leaf-accumulator:v1",
            {
                "previous": self.completed_leaf_accumulator,
                "leaf": self.tail_document_semantic_fingerprint,
            },
        )
        self.completed_leaf_count += 1
        self.tail_previous_artifact_id = self.tail_artifact_id
        self.tail_items.clear()
        self.tail_artifact_id = None
        self.tail_document_semantic_fingerprint = None
        self.tail_dirty = False

    def _materialize_tail(self) -> None:
        if not self.tail_items:
            raise ValueError("cannot materialize an empty timeline tail")
        leaf_payload = {
            "schema_version": "run_timeline_persistent_leaf.v1",
            "runtime_session_id": self.source_reference.runtime_session_id,
            "run_id": self.source_reference.run_id,
            "absolute_start_ordinal": min(
                int(item["absolute_item_ordinal"]) for item in self.tail_items
            ),
            "absolute_end_ordinal": max(
                int(item["absolute_item_ordinal"]) for item in self.tail_items
            ),
            "previous_leaf_artifact_id": self.tail_previous_artifact_id,
            "items": self.tail_items,
        }
        leaf_text = canonical_json_bytes(leaf_payload).decode("utf-8")
        if len(leaf_text.encode("utf-8")) > _TIMELINE_LEAF_BYTES:
            raise ValueError("timeline leaf exceeds its physical bound")
        leaf_id = "timeline-leaf:" + context_fingerprint(
            "run-timeline-leaf-id:v1", leaf_payload
        )
        document = _artifact_document(
            semantic_document_id=leaf_id,
            media_type=_TIMELINE_LEAF_MEDIA_TYPE,
            text=leaf_text,
        )
        self.documents.append(document)
        self.tail_artifact_id = leaf_id
        self.tail_document_semantic_fingerprint = document.document_semantic_fingerprint
        self.tail_dirty = False


@dataclass(frozen=True, slots=True)
class _PreparedProjection:
    target_key: str
    prepared_result: PreparedDurableProjectionResultFact


@dataclass(slots=True)
class PostgresRunTimelineProjectionHandler:
    connection_provider: VerifiedPostgresConnectionProviderProtocol

    def __call__(
        self,
        leased_job: LeasedDurableProjectionJob,
        *,
        deadline_monotonic: float,
    ) -> PreparedDurableProjectionResultFact:
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return _prepare_projection_for_job(
                connection,
                leased_job=leased_job,
            ).prepared_result


@dataclass(slots=True)
class PostgresToolResultEvidenceProjectionHandler:
    connection_provider: VerifiedPostgresConnectionProviderProtocol

    def __call__(
        self,
        leased_job: LeasedDurableProjectionJob,
        *,
        deadline_monotonic: float,
    ) -> PreparedDurableProjectionResultFact:
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return _prepare_projection_for_job(
                connection,
                leased_job=leased_job,
            ).prepared_result


def projection_executables(
    connection_provider: VerifiedPostgresConnectionProviderProtocol,
) -> dict[DurableProjectionKind, object]:
    return {
        DurableProjectionKind.RUN_TIMELINE: (
            PostgresRunTimelineProjectionHandler(connection_provider)
        ),
        DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE: (
            PostgresToolResultEvidenceProjectionHandler(connection_provider)
        ),
    }


def _prepare_projection_for_job(
    connection: Connection,
    *,
    leased_job: LeasedDurableProjectionJob,
) -> _PreparedProjection:
    job = leased_job.job
    owner = cast(
        ProjectionJobResultOwnerFact,
        build_projection_fact(
            ProjectionJobResultOwnerFact,
            schema_version="projection_job_result_owner.v1",
            owner_kind="durable_projection_job",
            job_id=job.job_id,
            job_semantic_fingerprint=job.job_semantic_fingerprint,
            job_candidate_fingerprint=leased_job.job_candidate_fingerprint,
            source_event_reference_fingerprint=(
                job.source_event_reference.reference_fingerprint
            ),
        ),
    )
    prepared = _prepare_projection(
        connection,
        kind=job.projection_kind,
        source_reference=job.source_event_reference,
        trigger_horizon=job.trigger_horizon,
        result_owner=owner,
        surface_plan=leased_job.canonical_mutation_surface_plan,
    )
    if prepared.target_key != job.target_key:
        raise ValueError("projection executable target resolver drifted")
    return prepared


def _prepare_projection(
    connection: Connection,
    *,
    kind: DurableProjectionKind,
    source_reference: DurableProjectionSourceEventReferenceFact,
    trigger_horizon: DurableProjectionLedgerHorizonFact,
    result_owner: ProjectionJobResultOwnerFact | PreActivationHookResultOwnerFact,
    surface_plan,
) -> _PreparedProjection:
    if (
        trigger_horizon.runtime_session_id != source_reference.runtime_session_id
        or trigger_horizon.through_sequence != source_reference.sequence
    ):
        raise ValueError("projection trigger horizon is not event-local")
    source_row = _read_exact_event_row(connection, source_reference)
    source_bound = _bound_event_from_row(source_row)
    if source_bound.source_reference != source_reference:
        raise ValueError("projection source exact rebind failed")
    if source_bound.trigger_horizon != trigger_horizon:
        raise ValueError("projection trigger horizon exact rebind failed")
    if kind is DurableProjectionKind.RUN_TIMELINE:
        target_key = projection_target_key(
            projection_kind=kind,
            runtime_session_id=source_reference.runtime_session_id,
            run_id=source_reference.run_id,
            tool_call_id=None,
        )
        documents, source_projection = _prepare_timeline_documents(
            connection,
            source_reference=source_reference,
            trigger_horizon=trigger_horizon,
            target_key=target_key,
        )
    else:
        decoded = source_bound.envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if not isinstance(decoded, ToolResultEndEvent):
            raise ValueError("evidence source is not ToolResultEndEvent")
        target_key = projection_target_key(
            projection_kind=kind,
            runtime_session_id=source_reference.runtime_session_id,
            run_id=source_reference.run_id,
            tool_call_id=decoded.tool_call_id,
        )
        documents, source_projection = _prepare_evidence_documents(
            connection,
            source_reference=source_reference,
            end_event=decoded,
        )
    # Artifact documents are already materialized in the PostgreSQL UOW. Only
    # graph documents and immutable graph relations have an external surface.
    mutation_documents = tuple(
        item
        for item in documents
        if not isinstance(item, PreparedDurableProjectionArtifactDocumentFact)
    )
    mutation_payloads = tuple(
        _mutation_payload_for_document(item) for item in mutation_documents
    )
    mutation_semantics = canonical_mutation_semantics_for_payloads(
        mutation_kind=CanonicalMutationKind.RUNTIME_SEMANTIC,
        graph_id=_GRAPH_ID,
        payloads=mutation_payloads,
    )
    result_semantic = cast(
        DurableProjectionResultSemanticFact,
        build_projection_fact(
            DurableProjectionResultSemanticFact,
            schema_version="durable_projection_result_semantic.v1",
            projection_kind=kind,
            source_projection_fingerprint=source_projection,
            ordered_document_semantic_fingerprints=tuple(
                document_semantic_fingerprint(item) for item in documents
            ),
            ordered_canonical_mutation_semantic_fingerprints=tuple(
                item.mutation_semantic_fingerprint for item in mutation_semantics
            ),
        ),
    )
    mutation_owner = build_projection_result_mutation_owner(
        result_owner=result_owner,
        result_semantic=result_semantic,
        source_event_reference=source_reference,
    )
    mutation_bundle = build_canonical_mutation_bundle(
        source_owner=mutation_owner,
        mutation_kind=CanonicalMutationKind.RUNTIME_SEMANTIC,
        graph_id=_GRAPH_ID,
        payloads=mutation_payloads,
        surface_plan=surface_plan,
        source_authority_fingerprints=(source_reference.reference_fingerprint,),
    )
    if (
        tuple(
            item.mutation_semantic.mutation_semantic_fingerprint
            for item in mutation_bundle.ordered_mutation_candidates
        )
        != result_semantic.ordered_canonical_mutation_semantic_fingerprints
    ):
        raise ValueError("projection mutation semantic factory drifted")
    prepared = cast(
        PreparedDurableProjectionResultFact,
        build_projection_fact(
            PreparedDurableProjectionResultFact,
            schema_version="prepared_durable_projection_result.v1",
            result_owner=result_owner,
            result_semantic=result_semantic,
            ordered_documents=documents,
            canonical_mutation_candidates=(mutation_bundle.ordered_mutation_candidates),
        ),
    )
    return _PreparedProjection(target_key=target_key, prepared_result=prepared)


def _prepare_timeline_documents(
    connection: Connection,
    *,
    source_reference: DurableProjectionSourceEventReferenceFact,
    trigger_horizon: DurableProjectionLedgerHorizonFact,
    target_key: str,
) -> tuple[
    tuple[
        PreparedDurableProjectionArtifactDocumentFact
        | PreparedDurableProjectionGraphDocumentFact,
        ...,
    ],
    str,
]:
    base = _read_timeline_base(
        connection,
        source_reference=source_reference,
        target_key=target_key,
    )
    base_sequence = base.state.through_sequence if base.state is not None else 0
    if base_sequence >= source_reference.sequence:
        raise ValueError("timeline job does not advance its applied target head")
    reducer = IncrementalRunTimelineReducer.restore(
        runtime_session_id=source_reference.runtime_session_id,
        run_id=source_reference.run_id,
        payload=base.open_state,
        next_item_ordinal=(base.state.item_count if base.state is not None else 0),
        status=base.state.status if base.state is not None else "running",
        start_sequence=(base.state.start_sequence if base.state is not None else None),
        terminal_sequence=(base.state.end_sequence if base.state is not None else None),
    )
    leaf_builder = _TimelineLeafBuilder.from_base(
        source_reference=source_reference,
        base=base,
    )
    source_event_accumulator = context_fingerprint(
        "run-timeline-source-event-accumulator:v1",
        {
            "base_through_sequence": base_sequence,
            "base_state_semantic_fingerprint": (
                base.state.state_semantic_fingerprint
                if base.state is not None
                else None
            ),
        },
    )
    page_count = 0
    last_reference: DurableProjectionSourceEventReferenceFact | None = None
    first_event_created_at: str | None = None
    for page in _iter_run_source_pages(
        connection,
        runtime_session_id=source_reference.runtime_session_id,
        run_id=source_reference.run_id,
        after_sequence_exclusive=base_sequence,
        through_sequence_inclusive=source_reference.sequence,
    ):
        page_count += 1
        for stored in page.ordered_stored_events:
            decoded = _decode_stored_event(stored)
            reducer.apply(decoded)
            source_event_accumulator = context_fingerprint(
                "run-timeline-source-event-accumulator:v1",
                {
                    "previous": source_event_accumulator,
                    "stored_event_fingerprint": (stored.stored_event_fingerprint),
                },
            )
            last_reference = stored.event_reference
            if first_event_created_at is None:
                first_event_created_at = decoded.created_at
        leaf_builder.append(reducer.take_completed_items())
    if page_count == 0 or last_reference != source_reference:
        raise ValueError("timeline source pages do not terminate at trigger")

    open_state = reducer.open_state_payload()
    open_state_fingerprint = context_fingerprint(
        "run-timeline-open-state:v1", open_state
    )
    tail = leaf_builder.finish()
    vector_root = context_fingerprint(
        "run-timeline-persistent-vector-root:v1",
        {
            "completed_leaf_accumulator": tail["completed_leaf_accumulator"],
            "completed_leaf_count": tail["completed_leaf_count"],
            "tail_document_semantic_fingerprint": (
                tail["tail_document_semantic_fingerprint"]
            ),
            "item_count": reducer.next_item_ordinal,
            "open_item_state_semantic_fingerprint": (open_state_fingerprint),
        },
    )
    state = cast(
        RunTimelinePersistentStateSemanticFact,
        build_projection_fact(
            RunTimelinePersistentStateSemanticFact,
            schema_version="run_timeline_persistent_state_semantic.v1",
            runtime_session_id=source_reference.runtime_session_id,
            run_id=source_reference.run_id,
            through_sequence=source_reference.sequence,
            status=reducer.status,
            start_sequence=int(reducer.start_sequence or source_reference.sequence),
            end_sequence=reducer.terminal_sequence,
            item_count=reducer.next_item_ordinal,
            ordered_item_semantic_accumulator=leaf_builder.item_accumulator,
            persistent_item_vector_root_semantic_fingerprint=vector_root,
            open_item_state_semantic_fingerprint=open_state_fingerprint,
        ),
    )
    manifest_payload = {
        "schema_version": "run_timeline_persistent_manifest.v1",
        "state": state.model_dump(mode="json"),
        "tail_artifact_id": tail["tail_artifact_id"],
        "tail_document_semantic_fingerprint": (
            tail["tail_document_semantic_fingerprint"]
        ),
        "completed_leaf_accumulator": tail["completed_leaf_accumulator"],
        "completed_leaf_count": tail["completed_leaf_count"],
        "created_at": base.created_at or first_event_created_at,
        "open_state": open_state,
        "trigger_event_reference": source_reference.model_dump(mode="json"),
        "source_event_accumulator": source_event_accumulator,
    }
    manifest_text = canonical_json_bytes(manifest_payload).decode("utf-8")
    if len(manifest_text.encode("utf-8")) > _TIMELINE_MANIFEST_BYTES:
        raise ValueError("timeline manifest exceeds its physical bound")
    manifest_id = "timeline-manifest:" + context_fingerprint(
        "run-timeline-manifest-id:v1",
        {
            "runtime_session_id": source_reference.runtime_session_id,
            "run_id": source_reference.run_id,
            "through_sequence": source_reference.sequence,
            "state": state.state_semantic_fingerprint,
        },
    )
    manifest_document = _artifact_document(
        semantic_document_id=manifest_id,
        media_type=_TIMELINE_MANIFEST_MEDIA_TYPE,
        text=manifest_text,
    )
    timeline_id = (
        f"run-timeline:{source_reference.runtime_session_id}:{source_reference.run_id}"
    )
    graph_payload = RunTimelineRecord(
        id=timeline_id,
        runtime_session_id=source_reference.runtime_session_id,
        run_id=source_reference.run_id,
        turn_id=source_reference.turn_id,
        reply_id=source_reference.reply_id,
        scope=f"ctx:{source_reference.turn_id}",
        status=state.status,
        item_count=state.item_count,
        created_at=str(manifest_payload["created_at"]),
        updated_at=_event_created_at(connection, source_reference.event_id),
        stored_as=NodeRef(manifest_id),
    ).to_jsonld()
    graph_document = _graph_document(
        semantic_document_id=timeline_id,
        graph_document_type=rt.RUN_TIMELINE.name,
        payload=graph_payload,
    )
    source_projection = context_fingerprint(
        "run-timeline-source-projection:v1",
        {
            "trigger_reference": source_reference.reference_fingerprint,
            "trigger_horizon": trigger_horizon.horizon_fingerprint,
            "interpretation_contract": "event-sequence-items.v1",
        },
    )
    return (
        (*leaf_builder.documents, manifest_document, graph_document),
        source_projection,
    )


def _read_timeline_base(
    connection: Connection,
    *,
    source_reference: DurableProjectionSourceEventReferenceFact,
    target_key: str,
) -> _TimelineBase:
    row = (
        connection.cursor(row_factory=dict_row)
        .execute(
            """
        SELECT h.head_payload, r.receipt_payload
        FROM durable_projection_target_heads AS h
        JOIN durable_projection_result_receipts AS r
          ON r.receipt_id =
             h.head_payload->'applied_result_receipt_reference'->>'receipt_id'
        WHERE h.projection_kind = %s AND h.target_key = %s
        """,
            (DurableProjectionKind.RUN_TIMELINE.value, target_key),
        )
        .fetchone()
    )
    if row is None:
        return _TimelineBase(
            state=None,
            item_accumulator=_TIMELINE_ITEM_ACCUMULATOR_GENESIS,
            completed_leaf_accumulator=_TIMELINE_COMPLETED_LEAF_GENESIS,
            completed_leaf_count=0,
            tail_artifact_id=None,
            tail_document_semantic_fingerprint=None,
            tail_items=(),
            tail_previous_artifact_id=None,
            created_at=None,
            open_state=None,
        )
    receipt_payload = dict(row["receipt_payload"])
    references = tuple(receipt_payload["result_document_references"])
    manifests = tuple(
        item
        for item in references
        if item.get("document_kind") == "artifact"
        and item.get("media_type") == _TIMELINE_MANIFEST_MEDIA_TYPE
    )
    if len(manifests) != 1:
        raise ValueError("timeline applied receipt lacks one manifest")
    manifest_ref = manifests[0]["artifact_reference"]
    manifest_text = _read_artifact_text(
        connection,
        artifact_id=str(manifest_ref["artifact_semantic_id"]),
        expected_sha=str(manifest_ref["content_sha256"]),
        expected_bytes=int(manifest_ref["content_utf8_bytes"]),
    )
    payload = json.loads(manifest_text)
    state = RunTimelinePersistentStateSemanticFact.model_validate(payload["state"])
    if (
        state.runtime_session_id != source_reference.runtime_session_id
        or state.run_id != source_reference.run_id
    ):
        raise ValueError("timeline base state target drifted")
    tail_id = payload.get("tail_artifact_id")
    tail_items: tuple[dict[str, object], ...] = ()
    tail_previous: str | None = None
    if tail_id is not None:
        tail_text = _read_artifact_text(connection, artifact_id=str(tail_id))
        tail_payload = json.loads(tail_text)
        tail_items = tuple(tail_payload["items"])
        tail_previous = tail_payload.get("previous_leaf_artifact_id")
    return _TimelineBase(
        state=state,
        item_accumulator=state.ordered_item_semantic_accumulator,
        completed_leaf_accumulator=str(payload["completed_leaf_accumulator"]),
        completed_leaf_count=int(payload["completed_leaf_count"]),
        tail_artifact_id=str(tail_id) if tail_id is not None else None,
        tail_document_semantic_fingerprint=payload.get(
            "tail_document_semantic_fingerprint"
        ),
        tail_items=tail_items,
        tail_previous_artifact_id=tail_previous,
        created_at=payload.get("created_at"),
        open_state=cast(dict[str, Any] | None, payload.get("open_state")),
    )


def _prepare_evidence_documents(
    connection: Connection,
    *,
    source_reference: DurableProjectionSourceEventReferenceFact,
    end_event: ToolResultEndEvent,
) -> tuple[
    tuple[
        PreparedDurableProjectionArtifactDocumentFact
        | PreparedDurableProjectionGraphDocumentFact
        | PreparedDurableProjectionGraphRelationFact,
        ...,
    ],
    str,
]:
    rows = tuple(
        dict(row)
        for row in connection.cursor(row_factory=dict_row)
        .execute(
            """
            SELECT id, session_id, run_id, turn_id, reply_id, sequence,
                   event_type, event_schema_version,
                   event_schema_fingerprint,
                   event_domain_contract_fingerprint,
                   transcript_semantic_prefix_count,
                   transcript_semantic_prefix_accumulator,
                   ledger_continuity_accumulator,
                   ledger_payload_prefix_bytes,
                   created_at, payload
            FROM agent_events
            WHERE session_id = %s AND run_id = %s
              AND sequence <= %s
              AND payload->>'tool_call_id' = %s
              AND event_type = ANY(%s)
            ORDER BY sequence
            LIMIT %s
            """,
            (
                source_reference.runtime_session_id,
                source_reference.run_id,
                source_reference.sequence,
                end_event.tool_call_id,
                [
                    str(EventType.TOOL_CALL_START),
                    str(EventType.TOOL_CALL_ARGUMENTS_SEGMENT),
                    str(EventType.TOOL_CALL_END),
                    str(EventType.TOOL_RESULT_START),
                    str(EventType.TOOL_RESULT_END),
                ],
                _EVIDENCE_EVENT_READS + 1,
            ),
        )
        .fetchall()
    )
    if len(rows) > _EVIDENCE_EVENT_READS:
        raise ValueError("tool evidence exact event bound exceeded")
    decoded = tuple(
        (
            _bound_event_from_row(row),
            _bound_event_from_row(row).envelope.decode_owned(
                DEFAULT_EVENT_SCHEMA_REGISTRY
            ),
        )
        for row in rows
    )
    call_starts = tuple(
        item for item in decoded if isinstance(item[1], ToolCallStartEvent)
    )
    call_ends = tuple(item for item in decoded if isinstance(item[1], ToolCallEndEvent))
    result_starts = tuple(
        item for item in decoded if isinstance(item[1], ToolResultStartEvent)
    )
    result_ends = tuple(
        item for item in decoded if isinstance(item[1], ToolResultEndEvent)
    )
    if not all(
        len(items) == 1
        for items in (call_starts, call_ends, result_starts, result_ends)
    ):
        raise ValueError("tool evidence lifecycle is not uniquely closed")
    if result_ends[0][0].source_reference != source_reference:
        raise ValueError("tool evidence End source mismatch")
    segments = tuple(
        item for item in decoded if isinstance(item[1], ToolCallArgumentsSegmentEvent)
    )
    ordered_lifecycle = (
        call_starts[0],
        *segments,
        call_ends[0],
        result_starts[0],
        result_ends[0],
    )
    lifecycle_sequences = tuple(
        item[0].source_reference.sequence for item in ordered_lifecycle
    )
    if lifecycle_sequences != tuple(sorted(lifecycle_sequences)) or len(
        lifecycle_sequences
    ) != len(set(lifecycle_sequences)):
        raise ValueError("tool evidence lifecycle sequence is not causal")
    call_stream = (call_starts[0], *segments, call_ends[0])
    call_stream_identities = {
        (
            item[1].model_stream_attribution.resolved_model_call_id,
            item[1].model_stream_attribution.model_call_start_event_id,
        )
        for item in call_stream
    }
    call_stream_indices = tuple(
        item[1].model_stream_attribution.durable_semantic_event_index
        for item in call_stream
    )
    if (
        len(call_stream_identities) != 1
        or call_stream_indices != tuple(sorted(call_stream_indices))
        or len(call_stream_indices) != len(set(call_stream_indices))
    ):
        raise ValueError("tool evidence model-stream attribution drifted")
    tool_name = cast(ToolCallStartEvent, call_starts[0][1]).tool_call_name
    if cast(ToolResultStartEvent, result_starts[0][1]).tool_call_name != tool_name:
        raise ValueError("tool evidence name drifted")
    arguments_text = "".join(
        cast(ToolCallArgumentsSegmentEvent, item[1]).arguments_json_fragment
        for item in segments
    )
    raw_argument_bytes = arguments_text.encode("utf-8")
    if len(raw_argument_bytes) > _ARGUMENT_BYTES:
        raise ValueError("tool arguments exceed evidence hard bound")
    parse_disposition: str
    parsed_arguments_object: FrozenJsonObjectFact | None
    parse_error_code: str | None
    canonical_arguments: bytes | None
    (
        parse_disposition,
        parsed_arguments_object,
        parse_error_code,
        canonical_arguments,
        summary_source,
    ) = _strict_tool_arguments_evidence(arguments_text)
    input_summary = _bounded_utf8_prefix(
        summary_source,
        maximum_bytes=_SUMMARY_BYTES,
    )
    argument_projection = cast(
        ToolCallArgumentsEvidenceProjectionFact,
        build_projection_fact(
            ToolCallArgumentsEvidenceProjectionFact,
            schema_version="tool_call_arguments_evidence_projection.v1",
            tool_call_start_reference=call_starts[0][0].source_reference,
            tool_call_end_reference=call_ends[0][0].source_reference,
            arguments_segment_count=len(segments),
            arguments_segment_reference_accumulator=context_fingerprint(
                "tool-call-arguments-segment-reference-accumulator:v1",
                tuple(
                    item[0].source_reference.reference_fingerprint for item in segments
                ),
            ),
            raw_arguments_json=arguments_text,
            raw_arguments_json_sha256=(
                f"sha256:{sha256(raw_argument_bytes).hexdigest()}"
            ),
            raw_arguments_json_utf8_bytes=len(raw_argument_bytes),
            parse_disposition=parse_disposition,
            parsed_arguments_object=parsed_arguments_object,
            parse_error_code=parse_error_code,
            canonical_arguments_json_sha256=(
                f"sha256:{sha256(canonical_arguments).hexdigest()}"
                if canonical_arguments is not None
                else None
            ),
            canonical_arguments_json_utf8_bytes=(
                len(canonical_arguments) if canonical_arguments is not None else None
            ),
            bounded_input_summary=input_summary,
            bounded_input_summary_sha256=_sha_text(input_summary),
            summary_contract_fingerprint=context_fingerprint(
                "tool-call-arguments-summary-contract:v1",
                {
                    "parse_contract": "object-or-raw-diagnostic.v1",
                    "maximum_utf8_bytes": _SUMMARY_BYTES,
                },
            ),
        ),
    )
    terminal_reference = end_event.terminal_projection.projection_reference
    projection_identity = (
        end_event.terminal_projection.projection_committed_event_identity
    )
    projection_row = (
        connection.cursor(row_factory=dict_row)
        .execute(
            """
        SELECT id, session_id, run_id, turn_id, reply_id, sequence,
               event_type, event_schema_version,
               event_schema_fingerprint,
               event_domain_contract_fingerprint,
               transcript_semantic_prefix_count,
               transcript_semantic_prefix_accumulator,
               ledger_continuity_accumulator,
               ledger_payload_prefix_bytes,
               created_at, payload
        FROM agent_events
        WHERE id = %s AND session_id = %s AND sequence <= %s
        """,
            (
                projection_identity.event_id,
                source_reference.runtime_session_id,
                source_reference.sequence,
            ),
        )
        .fetchone()
    )
    if projection_row is None:
        raise LookupError("tool terminal projection event is absent")
    projection_event = _bound_event_from_row(
        dict(projection_row)
    ).envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    if (
        not isinstance(projection_event, ToolResultTerminalProjectionCommittedEvent)
        or stable_event_identity(
            projection_event,
            runtime_session_id=source_reference.runtime_session_id,
        )
        != projection_identity
        or projection_event.projection_reference != terminal_reference
        or projection_event.tool_call_id != end_event.tool_call_id
    ):
        raise ValueError("tool terminal projection event exact join failed")
    terminal_text = _read_artifact_text(
        connection,
        artifact_id=terminal_reference.document_artifact_id,
        expected_sha=terminal_reference.document_sha256,
        expected_bytes=terminal_reference.document_byte_count,
    )
    terminal_document = hydrate_terminal_projection_text(
        terminal_reference,
        terminal_text,
    )
    if not isinstance(
        terminal_document.source_fact, ToolResultSemanticSourceFact
    ) or not isinstance(terminal_document.payload, ToolTerminalProjectionPayloadFact):
        raise ValueError("tool evidence terminal document kind drifted")
    if terminal_document.source_fact.source_event_identity != (
        stable_event_identity(
            cast(ToolResultStartEvent, result_starts[0][1]),
            runtime_session_id=source_reference.runtime_session_id,
        )
    ):
        raise ValueError("tool terminal source does not bind result start")
    block = terminal_document.payload.canonical_result_block
    if (
        block.semantic_identity.tool_call_id != end_event.tool_call_id
        or block.semantic_identity.model_tool_name != tool_name
        or block.semantic_identity.result_state.value != end_event.state.value
    ):
        raise ValueError("tool evidence terminal semantic join failed")
    output_summary = _tool_output_summary(connection, block)
    artifact_fingerprints = tuple(
        context_fingerprint(
            "tool-result-evidence-artifact-reference:v1",
            item.model_dump(mode="json"),
        )
        for item in end_event.artifacts
    )
    if len(artifact_fingerprints) > _EVIDENCE_ARTIFACTS:
        raise ValueError("tool evidence artifact bound exceeded")
    output_projection = cast(
        ToolResultEvidenceOutputProjectionFact,
        build_projection_fact(
            ToolResultEvidenceOutputProjectionFact,
            schema_version="tool_result_evidence_output_projection.v1",
            result_state=end_event.state,
            result_semantic_fingerprint=(block.semantic_identity.semantic_fingerprint),
            bounded_output_summary=output_summary,
            bounded_output_summary_sha256=_sha_text(output_summary),
            output_was_truncated=(
                _tool_output_text(connection, block) != output_summary
            ),
            ordered_artifact_reference_fingerprints=artifact_fingerprints,
            projection_contract_fingerprint=context_fingerprint(
                "tool-result-evidence-output-projection-contract:v1",
                {
                    "maximum_codepoints": _OUTPUT_CODEPOINTS,
                    "maximum_utf8_bytes": _SUMMARY_BYTES,
                },
            ),
        ),
    )
    source = cast(
        ToolResultExecutionEvidenceSourceFact,
        build_projection_fact(
            ToolResultExecutionEvidenceSourceFact,
            schema_version="tool_result_execution_evidence_source.v1",
            tool_result_start_reference=result_starts[0][0].source_reference,
            tool_result_end_reference=source_reference,
            terminal_projection=end_event.terminal_projection,
            tool_call_arguments=argument_projection,
            output_projection=output_projection,
            tool_call_id=end_event.tool_call_id,
            tool_name=tool_name,
            evidence_scope=f"ctx:{source_reference.turn_id}",
        ),
    )
    source_document = _artifact_document(
        semantic_document_id="tool-evidence-source:" + source.source_fingerprint,
        media_type=_TOOL_EVIDENCE_SOURCE_MEDIA_TYPE,
        text=canonical_json_bytes(source.model_dump(mode="json")).decode("utf-8"),
    )
    evidence_id = "tool-result:" + context_fingerprint(
        "pulsara:tool-result-execution-evidence-document:v1",
        {
            "runtime_session_id": source_reference.runtime_session_id,
            "run_id": source_reference.run_id,
            "tool_call_id": end_event.tool_call_id,
            "result_semantic_fingerprint": (
                output_projection.result_semantic_fingerprint
            ),
        },
    )
    primary_artifact = end_event.artifacts[0] if end_event.artifacts else None
    tool_document_payload = ToolResult(
        id=evidence_id,
        tool_name=tool_name,
        status=_tool_execution_status(end_event.state),
        input_summary=input_summary,
        output_summary=output_summary,
        truncated=output_projection.output_was_truncated,
        scope=source.evidence_scope,
        created_at=_event_created_at(connection, source_reference.event_id),
        stored_as=(
            NodeRef(primary_artifact.artifact_id)
            if primary_artifact is not None
            else None
        ),
    ).to_jsonld()
    tool_document = _graph_document(
        semantic_document_id=evidence_id,
        graph_document_type=rt.TOOL_RESULT.name,
        payload=tool_document_payload,
    )
    turn_document = _graph_document(
        semantic_document_id=source_reference.turn_id,
        graph_document_type=rt.TURN.name,
        payload={
            "@context": CORE_CONTEXT,
            "@id": source_reference.turn_id,
            "@type": [rt.TURN.name],
        },
    )
    turn_relation = cast(
        TurnProducedToolResultRelationFact,
        build_projection_fact(
            TurnProducedToolResultRelationFact,
            schema_version="turn_produced_tool_result_relation.v1",
            relation_document_id="graph-relation:"
            + context_fingerprint(
                "turn-produced-tool-result-relation-id:v1",
                {
                    "graph_id": _GRAPH_ID,
                    "turn_id": source_reference.turn_id,
                    "tool_result_id": evidence_id,
                },
            ),
            graph_id=_GRAPH_ID,
            turn_id=source_reference.turn_id,
            predicate_iri=rt.PRODUCED.value,
            tool_result_document_id=evidence_id,
            source_tool_result_end_reference_fingerprint=(
                source_reference.reference_fingerprint
            ),
        ),
    )
    artifact_documents: list[PreparedDurableProjectionGraphDocumentFact] = []
    seen_artifact_ids: set[str] = set()
    relation_documents: list[PreparedDurableProjectionGraphRelationFact] = [
        _prepared_relation(
            relation=turn_relation,
            source_authority_fingerprint=(source_reference.reference_fingerprint),
        )
    ]
    for ordinal, artifact in enumerate(end_event.artifacts):
        if artifact.artifact_id not in seen_artifact_ids:
            artifact_row = (
                connection.cursor(row_factory=dict_row)
                .execute(
                    """
                SELECT id, media_type, digest, size_bytes, stored_at, created_at
                FROM artifacts
                WHERE id = %s
                """,
                    (artifact.artifact_id,),
                )
                .fetchone()
            )
            if artifact_row is None:
                raise LookupError(
                    "tool evidence artifact base document authority is absent"
                )
            if (
                str(artifact_row["media_type"]) != artifact.media_type
                or int(artifact_row["size_bytes"]) != artifact.size_bytes
            ):
                raise ValueError("tool evidence artifact reference metadata drifted")
            artifact_payload = Artifact(
                id=artifact.artifact_id,
                stored_at=str(artifact_row["stored_at"]),
                digest=str(artifact_row["digest"]),
                summary=(
                    f"{artifact.media_type} artifact ({artifact.size_bytes} bytes)"
                ),
                created_at=artifact_row["created_at"].isoformat(),
                scope=f"ctx:{source_reference.runtime_session_id}",
            ).to_jsonld()
            artifact_documents.append(
                _graph_document(
                    semantic_document_id=artifact.artifact_id,
                    graph_document_type=rt.ARTIFACT.name,
                    payload=artifact_payload,
                )
            )
            seen_artifact_ids.add(artifact.artifact_id)
        relation = cast(
            ToolResultArtifactRelationFact,
            build_projection_fact(
                ToolResultArtifactRelationFact,
                schema_version="tool_result_artifact_relation.v1",
                relation_document_id="graph-relation:"
                + context_fingerprint(
                    "tool-result-artifact-relation-id:v1",
                    {
                        "graph_id": _GRAPH_ID,
                        "tool_result_id": evidence_id,
                        "artifact_id": artifact.artifact_id,
                        "role": artifact.role,
                        "ordinal": ordinal,
                    },
                ),
                graph_id=_GRAPH_ID,
                tool_result_document_id=evidence_id,
                predicate_iri=rt.PROVIDES.value,
                artifact_document_id=artifact.artifact_id,
                artifact_semantic_reference_fingerprint=(
                    artifact_fingerprints[ordinal]
                ),
                artifact_role=artifact.role,
                artifact_ordinal=ordinal,
            ),
        )
        relation_documents.append(
            _prepared_relation(
                relation=relation,
                source_authority_fingerprint=(source_reference.reference_fingerprint),
            )
        )
    return (
        (
            turn_document,
            tool_document,
            source_document,
            *artifact_documents,
            *relation_documents,
        ),
        source.source_fingerprint,
    )


def drain_pre_activation_kind(
    connection: Connection,
    *,
    kind: DurableProjectionKind,
    database_target_fingerprint: str,
    deadline_monotonic: float,
) -> tuple[PreActivationProjectionCoverageReceiptFact, ...]:
    """Freeze and cover every transitional target under one maintenance epoch."""

    del database_target_fingerprint
    epoch = read_runtime_write_epoch(connection, privileged=True)
    target_version = 7 if kind is DurableProjectionKind.RUN_TIMELINE else 8
    if (
        epoch.mode.value != "maintenance"
        or epoch.target_migration_version != target_version
        or epoch.maintenance_operation_id is None
    ):
        raise ValueError("pre-activation drain maintenance epoch mismatch")
    contract_row = connection.execute(
        """
        SELECT contract_payload FROM durable_projection_pre_activation_contracts
        WHERE projection_kind = %s
        """,
        (kind.value,),
    ).fetchone()
    if contract_row is None:
        raise ValueError("pre-activation contract is absent")
    contract = PreActivationProjectionHookContractFact.model_validate(contract_row[0])
    session_ids = tuple(
        str(row[0])
        for row in connection.execute("SELECT id FROM sessions ORDER BY id").fetchall()
    )
    receipts: list[PreActivationProjectionCoverageReceiptFact] = []
    for runtime_session_id in session_ids:
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("pre-activation drain deadline exceeded")
        with connection.transaction():
            guard = acquire_maintenance_runtime_write_guard(
                connection,
                expected_epoch=epoch,
                transaction_owner_id=(
                    f"pre-activation-drain:{kind.value}:{runtime_session_id}"
                ),
            )
            cutover_row = connection.execute(
                """
                SELECT cutover_payload
                FROM durable_projection_pre_activation_session_cutovers
                WHERE runtime_session_id = %s AND projection_kind = %s
                FOR UPDATE
                """,
                (runtime_session_id, kind.value),
            ).fetchone()
            if cutover_row is None:
                raise ValueError("pre-activation session cutover is absent")
            cutover = PreActivationProjectionSessionCutoverFact.model_validate(
                cutover_row[0]
            )
            horizon = ledger_horizon_for_session(connection, runtime_session_id)
            trigger_rows = _read_pre_activation_triggers(
                connection,
                runtime_session_id=runtime_session_id,
                kind=kind,
                start_exclusive=cutover.cutover_through_sequence,
                end_inclusive=horizon.through_sequence,
            )
            latest: dict[str, tuple[dict[str, object], object]] = {}
            for row in trigger_rows:
                bound = _bound_event_from_row(row)
                decoded = bound.envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
                tool_call_id = (
                    decoded.tool_call_id
                    if isinstance(decoded, ToolResultEndEvent)
                    else None
                )
                target_key = projection_target_key(
                    projection_kind=kind,
                    runtime_session_id=runtime_session_id,
                    run_id=bound.source_reference.run_id,
                    tool_call_id=tool_call_id,
                )
                if (
                    kind is DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE
                    and target_key in latest
                ):
                    previous = _bound_event_from_row(latest[target_key][0])
                    if (
                        previous.source_reference.reference_fingerprint
                        != bound.source_reference.reference_fingerprint
                    ):
                        raise ValueError(
                            "pre-activation evidence target has multiple "
                            "terminal source events"
                        )
                latest[target_key] = (row, decoded)
            coverage_items: list[PreActivationProjectionTargetCoverageItemFact] = []
            for target_key in sorted(latest):
                row, _decoded = latest[target_key]
                bound = _bound_event_from_row(row)
                owner = cast(
                    PreActivationHookResultOwnerFact,
                    build_projection_fact(
                        PreActivationHookResultOwnerFact,
                        schema_version="pre_activation_hook_result_owner.v1",
                        owner_kind="pre_activation_hook",
                        projection_kind=kind,
                        source_event_reference=bound.source_reference,
                        hook_contract_fingerprint=(contract.contract_fingerprint),
                    ),
                )
                prepared = _prepare_projection(
                    connection,
                    kind=kind,
                    source_reference=bound.source_reference,
                    trigger_horizon=bound.trigger_horizon,
                    result_owner=owner,
                    surface_plan=(
                        contract.contract_semantic.canonical_mutation_surface_plan
                    ),
                )
                if prepared.target_key != target_key:
                    raise ValueError("pre-activation target resolver drifted")
                outcome = PostgresDurableProjectionRepository.commit_pre_activation_in_transaction(
                    connection,
                    prepared_result=prepared.prepared_result,
                    admission_guard=guard,
                )
                if (
                    outcome.confirmation is not DurableProjectionCommitConfirmation.FULL
                    or outcome.result_receipt_reference is None
                ):
                    raise ValueError("pre-activation target could not be covered")
                coverage_items.append(
                    cast(
                        PreActivationProjectionTargetCoverageItemFact,
                        build_projection_fact(
                            PreActivationProjectionTargetCoverageItemFact,
                            schema_version=(
                                "pre_activation_projection_target_coverage_item.v1"
                            ),
                            projection_kind=kind,
                            target_key=target_key,
                            latest_trigger_event_reference=(bound.source_reference),
                            applied_result_receipt_reference=(
                                outcome.result_receipt_reference
                            ),
                        ),
                    )
                )
            pages = _write_coverage_pages(
                connection,
                runtime_session_id=runtime_session_id,
                kind=kind,
                items=tuple(coverage_items),
            )
            set_reference = cast(
                PreActivationProjectionCoverageSetReferenceFact,
                build_projection_fact(
                    PreActivationProjectionCoverageSetReferenceFact,
                    schema_version=(
                        "pre_activation_projection_coverage_set_reference.v1"
                    ),
                    page_count=len(pages),
                    target_count=len(coverage_items),
                    ordered_page_fingerprint_accumulator=context_fingerprint(
                        "pre-activation-coverage-page-accumulator:v1",
                        tuple(item.page_fingerprint for item in pages),
                    ),
                    ordered_target_item_accumulator=context_fingerprint(
                        "pre-activation-coverage-target-accumulator:v1",
                        tuple(item.item_fingerprint for item in coverage_items),
                    ),
                    last_page_fingerprint=(
                        pages[-1].page_fingerprint if pages else None
                    ),
                ),
            )
            trigger_accumulator = context_fingerprint(
                "pre-activation-scanned-trigger-accumulator:v1",
                tuple(
                    _bound_event_from_row(row).source_reference.reference_fingerprint
                    for row in trigger_rows
                ),
            )
            receipt_id = "pre-activation-coverage:" + context_fingerprint(
                "pre-activation-coverage-receipt-id:v1",
                {
                    "runtime_session_id": runtime_session_id,
                    "projection_kind": kind.value,
                    "pre_activation_contract_fingerprint": (
                        contract.contract_fingerprint
                    ),
                    "start_cutover_fingerprint": (cutover.cutover_fingerprint),
                    "frozen_horizon_fingerprint": (horizon.horizon_fingerprint),
                    "coverage_root": set_reference.reference_fingerprint,
                    "maintenance_operation_id": (epoch.maintenance_operation_id),
                },
            )
            receipt = cast(
                PreActivationProjectionCoverageReceiptFact,
                build_projection_fact(
                    PreActivationProjectionCoverageReceiptFact,
                    schema_version=("pre_activation_projection_coverage_receipt.v1"),
                    coverage_receipt_id=receipt_id,
                    runtime_session_id=runtime_session_id,
                    projection_kind=kind,
                    pre_activation_contract_fingerprint=(contract.contract_fingerprint),
                    start_cutover_fingerprint=cutover.cutover_fingerprint,
                    frozen_horizon=horizon,
                    scanned_trigger_event_count=len(trigger_rows),
                    scanned_trigger_event_accumulator=trigger_accumulator,
                    target_coverage_set=set_reference,
                    maintenance_operation_id=cast(str, epoch.maintenance_operation_id),
                    maintenance_authority_fingerprint=cast(
                        str, guard.maintenance_authority_fingerprint
                    ),
                ),
            )
            _insert_or_confirm_coverage_receipt(connection, receipt)
            receipts.append(receipt)
    return tuple(receipts)


def _iter_run_source_pages(
    connection: Connection,
    *,
    runtime_session_id: str,
    run_id: str,
    after_sequence_exclusive: int,
    through_sequence_inclusive: int,
) -> Iterator[RawRunProjectionSourcePage]:
    cursor = after_sequence_exclusive
    previous: str | None = None
    page_index = 0
    while cursor < through_sequence_inclusive:
        rows = tuple(
            dict(row)
            for row in connection.cursor(row_factory=dict_row)
            .execute(
                """
                SELECT id, session_id, run_id, turn_id, reply_id, sequence,
                       event_type, event_schema_version,
                       event_schema_fingerprint,
                       event_domain_contract_fingerprint,
                       transcript_semantic_prefix_count,
                       transcript_semantic_prefix_accumulator,
                       ledger_continuity_accumulator,
                       ledger_payload_prefix_bytes,
                       created_at, payload
                FROM agent_events
                WHERE session_id = %s AND run_id = %s
                  AND sequence > %s AND sequence <= %s
                ORDER BY sequence
                LIMIT %s
                """,
                (
                    runtime_session_id,
                    run_id,
                    cursor,
                    through_sequence_inclusive,
                    _SOURCE_PAGE_EVENTS + 1,
                ),
            )
            .fetchall()
        )
        if not rows:
            break
        selected = rows[:_SOURCE_PAGE_EVENTS]
        stored = tuple(_bound_event_from_row(row).stored_event for row in selected)
        payload_bytes = sum(item.canonical_payload_utf8_bytes for item in stored)
        if payload_bytes > _SOURCE_PAGE_BYTES:
            raise ValueError("timeline source page exceeds byte bound")
        has_more = len(rows) > _SOURCE_PAGE_EVENTS
        page = cast(
            RawRunProjectionSourcePage,
            build_projection_fact(
                RawRunProjectionSourcePage,
                schema_version="raw_run_projection_source_page.v1",
                runtime_session_id=runtime_session_id,
                run_id=run_id,
                after_sequence_exclusive=cursor,
                through_sequence_inclusive=int(selected[-1]["sequence"]),
                page_index=page_index,
                previous_page_fingerprint=previous,
                ordered_stored_events=stored,
                selected_event_count=len(stored),
                selected_payload_bytes=payload_bytes,
                selected_event_accumulator=context_fingerprint(
                    "run-projection-source-page-event-accumulator:v1",
                    tuple(item.stored_event_fingerprint for item in stored),
                ),
                has_more=has_more,
                next_after_sequence=(
                    int(selected[-1]["sequence"]) if has_more else None
                ),
            ),
        )
        yield page
        previous = page.page_fingerprint
        cursor = int(selected[-1]["sequence"])
        page_index += 1
        if not has_more:
            break


def _read_pre_activation_triggers(
    connection: Connection,
    *,
    runtime_session_id: str,
    kind: DurableProjectionKind,
    start_exclusive: int,
    end_inclusive: int,
) -> tuple[dict[str, object], ...]:
    event_types = (
        [
            str(EventType.REPLY_END),
            str(EventType.RUN_ERROR),
            str(EventType.RUN_END),
        ]
        if kind is DurableProjectionKind.RUN_TIMELINE
        else [str(EventType.TOOL_RESULT_END)]
    )
    rows: list[dict[str, object]] = []
    cursor = start_exclusive
    while cursor < end_inclusive:
        page = tuple(
            dict(row)
            for row in connection.cursor(row_factory=dict_row)
            .execute(
                """
                SELECT id, session_id, run_id, turn_id, reply_id, sequence,
                       event_type, event_schema_version,
                       event_schema_fingerprint,
                       event_domain_contract_fingerprint,
                       transcript_semantic_prefix_count,
                       transcript_semantic_prefix_accumulator,
                       ledger_continuity_accumulator,
                       ledger_payload_prefix_bytes,
                       created_at, payload
                FROM agent_events
                WHERE session_id = %s
                  AND sequence > %s AND sequence <= %s
                  AND event_type = ANY(%s)
                ORDER BY sequence
                LIMIT %s
                """,
                (
                    runtime_session_id,
                    cursor,
                    end_inclusive,
                    event_types,
                    _SOURCE_PAGE_EVENTS,
                ),
            )
            .fetchall()
        )
        if not page:
            break
        rows.extend(page)
        cursor = int(page[-1]["sequence"])
    return tuple(rows)


def _read_exact_event_row(
    connection: Connection,
    reference: DurableProjectionSourceEventReferenceFact,
) -> dict[str, object]:
    row = (
        connection.cursor(row_factory=dict_row)
        .execute(
            """
        SELECT id, session_id, run_id, turn_id, reply_id, sequence,
               event_type, event_schema_version,
               event_schema_fingerprint,
               event_domain_contract_fingerprint,
               transcript_semantic_prefix_count,
               transcript_semantic_prefix_accumulator,
               ledger_continuity_accumulator,
               ledger_payload_prefix_bytes,
               created_at, payload
        FROM agent_events WHERE id = %s
        """,
            (reference.event_id,),
        )
        .fetchone()
    )
    if row is None:
        raise LookupError("projection source event is absent")
    return dict(row)


def _artifact_document(
    *,
    semantic_document_id: str,
    media_type: str,
    text: str,
) -> PreparedDurableProjectionArtifactDocumentFact:
    encoded = text.encode("utf-8")
    digest = f"sha256:{sha256(encoded).hexdigest()}"
    semantic = context_fingerprint(
        "durable-projection-artifact-document-semantic:v1",
        {
            "semantic_document_id": semantic_document_id,
            "media_type": media_type,
            "content_sha256": digest,
            "codec": _ARTIFACT_CODEC,
        },
    )
    reference = cast(
        DurableContentAddressedArtifactReferenceFact,
        build_projection_fact(
            DurableContentAddressedArtifactReferenceFact,
            schema_version="durable_content_addressed_artifact_reference.v1",
            artifact_semantic_id=semantic_document_id,
            content_sha256=digest,
            content_utf8_bytes=len(encoded),
            artifact_store_contract_fingerprint=_ARTIFACT_STORE_CONTRACT,
            artifact_semantic_fingerprint=semantic,
        ),
    )
    return cast(
        PreparedDurableProjectionArtifactDocumentFact,
        build_projection_fact(
            PreparedDurableProjectionArtifactDocumentFact,
            schema_version=("prepared_durable_projection_artifact_document.v1"),
            document_kind="artifact",
            semantic_document_id=semantic_document_id,
            document_semantic_fingerprint=semantic,
            media_type=media_type,
            content_codec_contract_fingerprint=_ARTIFACT_CODEC,
            metadata_contract_fingerprint=_ARTIFACT_METADATA,
            content_sha256=digest,
            content_utf8_bytes=len(encoded),
            canonical_content_utf8=text,
            artifact_reference=reference,
        ),
    )


def _graph_document(
    *,
    semantic_document_id: str,
    graph_document_type: str,
    payload: dict[str, object],
) -> PreparedDurableProjectionGraphDocumentFact:
    encoded = canonical_json_bytes(payload)
    digest = f"sha256:{sha256(encoded).hexdigest()}"
    semantic = context_fingerprint(
        "durable-projection-graph-document-semantic:v1",
        {
            "graph_id": _GRAPH_ID,
            "semantic_document_id": semantic_document_id,
            "graph_document_type": graph_document_type,
            "canonical_json_sha256": digest,
            "jsonld_codec": _JSONLD_CODEC,
        },
    )
    return cast(
        PreparedDurableProjectionGraphDocumentFact,
        build_projection_fact(
            PreparedDurableProjectionGraphDocumentFact,
            schema_version=("prepared_durable_projection_graph_document.v1"),
            document_kind="graph_document",
            graph_id=_GRAPH_ID,
            semantic_document_id=semantic_document_id,
            graph_document_type=graph_document_type,
            document_semantic_fingerprint=semantic,
            canonical_json_sha256=digest,
            canonical_json_utf8_bytes=len(encoded),
            canonical_json_utf8=encoded.decode("utf-8"),
            jsonld_codec_contract_fingerprint=_JSONLD_CODEC,
        ),
    )


def _prepared_relation(
    *,
    relation: TurnProducedToolResultRelationFact | ToolResultArtifactRelationFact,
    source_authority_fingerprint: str,
) -> PreparedDurableProjectionGraphRelationFact:
    if isinstance(relation, TurnProducedToolResultRelationFact):
        source_id = relation.turn_id
        target_id = relation.tool_result_document_id
    else:
        source_id = relation.tool_result_document_id
        target_id = relation.artifact_document_id
    reference = cast(
        DurableProjectionGraphRelationReferenceFact,
        build_projection_fact(
            DurableProjectionGraphRelationReferenceFact,
            schema_version=("durable_projection_graph_relation_reference.v1"),
            document_kind="graph_relation",
            relation_id=relation.relation_document_id,
            graph_id=relation.graph_id,
            source_document_id=source_id,
            predicate_iri=relation.predicate_iri,
            target_document_id=target_id,
            relation_semantic_fingerprint=(relation.relation_semantic_fingerprint),
            lowering_contract_fingerprint=(
                GRAPH_RELATION_LOWERING_CONTRACT.contract_fingerprint
            ),
        ),
    )
    return cast(
        PreparedDurableProjectionGraphRelationFact,
        build_projection_fact(
            PreparedDurableProjectionGraphRelationFact,
            schema_version=("prepared_durable_projection_graph_relation.v1"),
            document_kind="graph_relation",
            relation_reference=reference,
            source_authority_fingerprint=source_authority_fingerprint,
        ),
    )


def _mutation_payload_for_document(
    document: PreparedDurableProjectionArtifactDocumentFact
    | PreparedDurableProjectionGraphDocumentFact
    | PreparedDurableProjectionGraphRelationFact,
) -> dict[str, object]:
    if isinstance(document, PreparedDurableProjectionArtifactDocumentFact):
        return {
            "mutation_lane": "runtime_semantic",
            "artifact_id": document.semantic_document_id,
            "artifact_reference": document.artifact_reference.model_dump(mode="json"),
        }
    if isinstance(document, PreparedDurableProjectionGraphDocumentFact):
        return {
            "mutation_lane": "runtime_semantic",
            "node_id": document.semantic_document_id,
            "document": json.loads(document.canonical_json_utf8),
        }
    return {
        "mutation_lane": "runtime_semantic",
        "relation": document.relation_reference.model_dump(mode="json"),
    }


def _write_coverage_pages(
    connection: Connection,
    *,
    runtime_session_id: str,
    kind: DurableProjectionKind,
    items: tuple[PreActivationProjectionTargetCoverageItemFact, ...],
) -> tuple[PreActivationProjectionCoveragePageFact, ...]:
    pages: list[PreActivationProjectionCoveragePageFact] = []
    previous: str | None = None
    for page_index, offset in enumerate(range(0, len(items), _COVERAGE_PAGE_ITEMS)):
        chunk = items[offset : offset + _COVERAGE_PAGE_ITEMS]
        encoded = canonical_json_bytes(
            tuple(item.model_dump(mode="json") for item in chunk)
        )
        if len(encoded) > _COVERAGE_PAGE_BYTES:
            raise ValueError("pre-activation coverage page exceeds byte bound")
        page = cast(
            PreActivationProjectionCoveragePageFact,
            build_projection_fact(
                PreActivationProjectionCoveragePageFact,
                schema_version=("pre_activation_projection_coverage_page.v1"),
                runtime_session_id=runtime_session_id,
                projection_kind=kind,
                page_index=page_index,
                previous_page_fingerprint=previous,
                ordered_items=chunk,
                item_count=len(chunk),
                item_accumulator=context_fingerprint(
                    "pre-activation-coverage-page-item-accumulator:v1",
                    tuple(item.item_fingerprint for item in chunk),
                ),
                canonical_utf8_bytes=len(encoded),
            ),
        )
        connection.execute(
            """
            INSERT INTO durable_projection_pre_activation_coverage_pages (
                page_fingerprint, runtime_session_id, projection_kind,
                page_index, page_payload
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (page_fingerprint) DO NOTHING
            """,
            (
                page.page_fingerprint,
                runtime_session_id,
                kind.value,
                page_index,
                Jsonb(page.model_dump(mode="json")),
            ),
        )
        pages.append(page)
        previous = page.page_fingerprint
    return tuple(pages)


def _insert_or_confirm_coverage_receipt(
    connection: Connection,
    receipt: PreActivationProjectionCoverageReceiptFact,
) -> None:
    inserted = connection.execute(
        """
        INSERT INTO durable_projection_pre_activation_coverage_receipts (
            coverage_receipt_id, runtime_session_id, projection_kind,
            frozen_through_sequence, receipt_payload, receipt_fingerprint
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (coverage_receipt_id) DO NOTHING
        RETURNING coverage_receipt_id
        """,
        (
            receipt.coverage_receipt_id,
            receipt.runtime_session_id,
            receipt.projection_kind.value,
            receipt.frozen_horizon.through_sequence,
            Jsonb(receipt.model_dump(mode="json")),
            receipt.receipt_fingerprint,
        ),
    ).fetchone()
    if inserted is not None:
        return
    row = connection.execute(
        """
        SELECT receipt_payload, receipt_fingerprint
        FROM durable_projection_pre_activation_coverage_receipts
        WHERE coverage_receipt_id = %s
        """,
        (receipt.coverage_receipt_id,),
    ).fetchone()
    if (
        row is None
        or PreActivationProjectionCoverageReceiptFact.model_validate(row[0]) != receipt
        or str(row[1]) != receipt.receipt_fingerprint
    ):
        raise ValueError("pre-activation coverage receipt identity conflict")


def _read_artifact_text(
    connection: Connection,
    *,
    artifact_id: str,
    expected_sha: str | None = None,
    expected_bytes: int | None = None,
) -> str:
    row = connection.execute(
        "SELECT text_body, digest, size_bytes FROM artifacts WHERE id = %s",
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"projection artifact is absent: {artifact_id}")
    if isinstance(row, dict):
        text = str(row["text_body"])
        stored_digest = str(row["digest"])
        stored_bytes = int(row["size_bytes"])
    else:
        text = str(row[0])
        stored_digest = str(row[1])
        stored_bytes = int(row[2])
    encoded = text.encode("utf-8")
    digest = f"sha256:{sha256(encoded).hexdigest()}"
    if (
        digest != stored_digest
        or len(encoded) != stored_bytes
        or expected_sha is not None
        and digest != expected_sha
        or expected_bytes is not None
        and len(encoded) != expected_bytes
    ):
        raise ValueError("projection artifact content drifted")
    return text


def _event_created_at(connection: Connection, event_id: str) -> str:
    row = connection.execute(
        "SELECT created_at FROM agent_events WHERE id = %s",
        (event_id,),
    ).fetchone()
    if row is None:
        raise LookupError("event timestamp source is absent")
    created_at = row["created_at"] if isinstance(row, dict) else row[0]
    return created_at.isoformat()


def _decode_stored_event(stored):
    reference = stored.event_reference
    binding = DEFAULT_EVENT_SCHEMA_REGISTRY.resolve_historical_binding(
        event_type=reference.event_type,
        event_schema_version=reference.event_schema_version,
        event_schema_fingerprint=reference.event_schema_fingerprint,
        event_domain_contract_fingerprint=(reference.event_domain_contract_fingerprint),
    )
    payload = stored.canonical_payload_json_utf8.encode("utf-8")
    if (
        len(payload) != stored.canonical_payload_utf8_bytes
        or f"sha256:{sha256(payload).hexdigest()}" != stored.canonical_payload_sha256
    ):
        raise ValueError("timeline stored-event payload identity drifted")
    event = binding.decode_owned_payload(payload)
    if getattr(event, "id", None) != reference.event_id:
        raise ValueError("timeline stored-event identity drifted")
    return event


def _tool_output_text(connection: Connection, block) -> str:
    parts: list[str] = []
    for item in block.content_blocks:
        semantic = item.semantic_identity
        content = item.content
        if content is None:
            assert isinstance(semantic, CanonicalToolResultDataBlockSemanticFact)
            parts.append(
                json.dumps(
                    {
                        "kind": "data",
                        "media_type": semantic.media_type,
                        "source_kind": semantic.source_kind,
                        "artifact_count": len(semantic.artifact_content_fingerprints),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif isinstance(content, TerminalInlineContentFact):
            parts.append(content.text)
        elif isinstance(content, TerminalArtifactContentReferenceFact):
            parts.append(
                _read_artifact_text(
                    connection,
                    artifact_id=content.artifact_id,
                    expected_sha=content.artifact_sha256,
                    expected_bytes=content.artifact_bytes,
                )
            )
    return "\n".join(parts).strip()


def _tool_output_summary(connection: Connection, block) -> str:
    text = _tool_output_text(connection, block)
    if len(text) > _OUTPUT_CODEPOINTS:
        text = text[: _OUTPUT_CODEPOINTS - 3] + "..."
    return _bounded_utf8_prefix(text, maximum_bytes=_SUMMARY_BYTES)


def _bounded_utf8_prefix(text: str, *, maximum_bytes: int) -> str:
    if len(text.encode("utf-8")) <= maximum_bytes:
        return text
    suffix = "..."
    budget = maximum_bytes - len(suffix.encode("utf-8"))
    selected: list[str] = []
    consumed = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if consumed + size > budget:
            break
        selected.append(character)
        consumed += size
    return "".join(selected) + suffix


def _tool_execution_status(state: ToolResultState) -> rt.ToolExecutionStatus:
    if state is ToolResultState.SUCCESS:
        return rt.ToolExecutionStatus.SUCCESS
    if state is ToolResultState.INTERRUPTED:
        return rt.ToolExecutionStatus.CANCELLED
    return rt.ToolExecutionStatus.ERROR


def _sha_text(text: str) -> str:
    return f"sha256:{sha256(text.encode('utf-8')).hexdigest()}"


__all__ = [
    "PostgresRunTimelineProjectionHandler",
    "PostgresToolResultEvidenceProjectionHandler",
    "drain_pre_activation_kind",
    "projection_executables",
]
