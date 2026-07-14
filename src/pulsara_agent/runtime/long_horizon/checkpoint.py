"""Pure subagent graph checkpoint construction and bounded restore."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from pulsara_agent.event import SubagentGraphCheckpointCommittedEvent
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.event_log.protocol import RawStoredEventEnvelope
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    SubagentGraphAccelerationFact,
    SubagentGraphCheckpointArtifactFact,
    SubagentGraphCheckpointStateFact,
    SubagentGraphReducerContractFact,
    SubagentGraphSemanticSourceFact,
)
from pulsara_agent.runtime.long_horizon.reducer_contract import (
    SubagentGraphReducerBinding,
    graph_semantic_payload_fingerprint,
    graph_state_semantic_fingerprint,
)
from pulsara_agent.runtime.subagent.facts import SubagentGraphState


GRAPH_SEMANTIC_ACCUMULATOR_SEED = sha256(
    b"pulsara-subagent-graph-semantic:v1"
).hexdigest()
LEDGER_CONTINUITY_ACCUMULATOR_SEED = sha256(
    b"pulsara-subagent-graph-ledger-continuity:v1"
).hexdigest()
CHECKPOINT_MEDIA_TYPE = "application/vnd.pulsara.subagent-graph-checkpoint+json"


class SubagentGraphCheckpointError(RuntimeError):
    pass


class SubagentGraphCheckpointLedgerUntrusted(SubagentGraphCheckpointError):
    pass


class SubagentGraphCheckpointContractMismatch(SubagentGraphCheckpointError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedSubagentGraphCheckpoint:
    state: SubagentGraphState
    checkpoint: SubagentGraphCheckpointStateFact
    artifact: SubagentGraphCheckpointArtifactFact
    artifact_payload_bytes: bytes
    event: SubagentGraphCheckpointCommittedEvent


@dataclass(frozen=True, slots=True)
class SubagentGraphCheckpointDeltaSnapshot:
    requested_through_sequence: int
    checkpoint_event: RawStoredEventEnvelope
    checkpoint_payload_bytes: bytes
    checkpoint_materialization_sequence: int
    delta_events: tuple[RawStoredEventEnvelope, ...]
    ledger_high_water_observed: int
    preferred_checkpoint_id: str | None
    selected_checkpoint_id: str
    rebased: bool


class SubagentGraphCheckpointReadUnavailable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_session_id: str = Field(min_length=1)
    requested_through_sequence: int = Field(ge=1)
    reason_code: Literal[
        "no_confirmed_checkpoint",
        "no_compatible_artifact",
        "delta_bound_exceeded",
        "reducer_contract_mismatch",
    ]
    confirmed_checkpoint_count: int = Field(ge=0)
    contract_compatible_checkpoint_count: int = Field(ge=0)
    readable_artifact_count: int = Field(ge=0)
    nearest_compatible_checkpoint_id: str | None = None
    nearest_compatible_checkpoint_through_sequence: int | None = Field(
        default=None, ge=1
    )


SubagentGraphCheckpointReadResult: TypeAlias = (
    SubagentGraphCheckpointDeltaSnapshot | SubagentGraphCheckpointReadUnavailable
)


def _hash_parts(*parts: object) -> str:
    payload = "\x00".join(str(part) for part in parts).encode("utf-8")
    return sha256(payload).hexdigest()


def extend_graph_semantic_accumulator(
    accumulator: str,
    *,
    event_id: str,
    semantic_payload_fingerprint: str,
) -> str:
    return _hash_parts(accumulator, event_id, semantic_payload_fingerprint)


def extend_ledger_continuity_accumulator(
    accumulator: str,
    envelope: RawStoredEventEnvelope,
) -> str:
    return _hash_parts(
        accumulator,
        envelope.sequence,
        envelope.event_id,
        envelope.payload_fingerprint,
    )


def _stable_id(domain: str, *parts: object) -> str:
    digest = _hash_parts(domain, *parts)
    return f"{domain}:{digest}"


def _validate_contiguous(
    events: tuple[RawStoredEventEnvelope, ...],
    *,
    first_sequence: int,
    through_sequence: int,
) -> None:
    expected = tuple(range(first_sequence, through_sequence + 1))
    actual = tuple(event.sequence for event in events)
    if actual != expected:
        raise SubagentGraphCheckpointLedgerUntrusted(
            "checkpoint event range is not contiguous"
        )
    ids = tuple(event.event_id for event in events)
    if len(ids) != len(set(ids)):
        raise SubagentGraphCheckpointLedgerUntrusted(
            "checkpoint event range contains duplicate IDs"
        )


def _fold_prefix(
    *,
    runtime_session_id: str,
    events: tuple[RawStoredEventEnvelope, ...],
    reducer_binding: SubagentGraphReducerBinding,
) -> tuple[SubagentGraphState, int, str, str]:
    if not events:
        raise SubagentGraphCheckpointLedgerUntrusted(
            "checkpoint prefix cannot be empty"
        )
    through = events[-1].sequence
    _validate_contiguous(events, first_sequence=1, through_sequence=through)
    state = reducer_binding.empty_state_factory()
    graph_event_count = 0
    graph_accumulator = GRAPH_SEMANTIC_ACCUMULATOR_SEED
    ledger_accumulator = LEDGER_CONTINUITY_ACCUMULATOR_SEED
    for envelope in events:
        if envelope.runtime_session_id != runtime_session_id:
            raise SubagentGraphCheckpointLedgerUntrusted(
                "checkpoint event runtime-session attribution mismatch"
            )
        ledger_accumulator = extend_ledger_continuity_accumulator(
            ledger_accumulator, envelope
        )
        semantic = graph_semantic_payload_fingerprint(
            envelope=envelope,
            contract=reducer_binding.contract,
        )
        if semantic is not None:
            graph_event_count += 1
            graph_accumulator = extend_graph_semantic_accumulator(
                graph_accumulator,
                event_id=envelope.event_id,
                semantic_payload_fingerprint=semantic,
            )
        state = reducer_binding.fold_stored_event(state, envelope)
    if not state.consistent or state.through_sequence != through:
        raise SubagentGraphCheckpointLedgerUntrusted(
            "checkpoint prefix reduced to an inconsistent graph"
        )
    return state, graph_event_count, graph_accumulator, ledger_accumulator


def prepare_subagent_graph_checkpoint(
    *,
    runtime_session_id: str,
    prefix_events: tuple[RawStoredEventEnvelope, ...],
    reducer_binding: SubagentGraphReducerBinding,
) -> PreparedSubagentGraphCheckpoint:
    state, graph_count, graph_accumulator, ledger_accumulator = _fold_prefix(
        runtime_session_id=runtime_session_id,
        events=prefix_events,
        reducer_binding=reducer_binding,
    )
    return _prepare_checkpoint_from_materialized_state(
        runtime_session_id=runtime_session_id,
        state=state,
        graph_event_count=graph_count,
        graph_semantic_accumulator=graph_accumulator,
        ledger_continuity_accumulator=ledger_accumulator,
        through_event=prefix_events[-1],
        reducer_binding=reducer_binding,
    )


def prepare_subagent_graph_checkpoint_from_restore(
    *,
    runtime_session_id: str,
    state: SubagentGraphState,
    semantic_source: SubagentGraphSemanticSourceFact,
    acceleration: SubagentGraphAccelerationFact,
    through_event: RawStoredEventEnvelope,
    reducer_binding: SubagentGraphReducerBinding,
) -> PreparedSubagentGraphCheckpoint:
    """Materialize a new memoization point without folding the prefix again."""

    contract = reducer_binding.contract
    if (
        semantic_source.runtime_session_id != runtime_session_id
        or semantic_source.graph_reducer_id != contract.graph_reducer_id
        or semantic_source.graph_reducer_version != contract.graph_reducer_version
        or semantic_source.graph_reducer_contract_fingerprint
        != contract.graph_reducer_contract_fingerprint
        or semantic_source.graph_state_semantic_fingerprint
        != graph_state_semantic_fingerprint(state)
        or acceleration.ledger_through_sequence != state.through_sequence
        or through_event.runtime_session_id != runtime_session_id
        or through_event.sequence != state.through_sequence
    ):
        raise SubagentGraphCheckpointContractMismatch(
            "restored graph materialization inputs are inconsistent"
        )
    return _prepare_checkpoint_from_materialized_state(
        runtime_session_id=runtime_session_id,
        state=state,
        graph_event_count=semantic_source.graph_event_count,
        graph_semantic_accumulator=semantic_source.graph_semantic_accumulator,
        ledger_continuity_accumulator=(
            acceleration.ledger_continuity_accumulator
        ),
        through_event=through_event,
        reducer_binding=reducer_binding,
    )


def _prepare_checkpoint_from_materialized_state(
    *,
    runtime_session_id: str,
    state: SubagentGraphState,
    graph_event_count: int,
    graph_semantic_accumulator: str,
    ledger_continuity_accumulator: str,
    through_event: RawStoredEventEnvelope,
    reducer_binding: SubagentGraphReducerBinding,
) -> PreparedSubagentGraphCheckpoint:
    through = through_event.sequence
    if (
        not state.consistent
        or state.through_sequence != through
        or through_event.runtime_session_id != runtime_session_id
    ):
        raise SubagentGraphCheckpointLedgerUntrusted(
            "checkpoint materialization state is not a trusted ledger prefix"
        )
    state_fingerprint = graph_state_semantic_fingerprint(state)
    contract = reducer_binding.contract
    checkpoint_id = _stable_id(
        "subagent_graph_checkpoint:v1",
        runtime_session_id,
        through,
        ledger_continuity_accumulator,
        contract.graph_reducer_id,
        contract.graph_reducer_version,
        contract.graph_reducer_contract_fingerprint,
        state_fingerprint,
    )
    checkpoint = SubagentGraphCheckpointStateFact(
        parent_runtime_session_id=runtime_session_id,
        checkpoint_id=checkpoint_id,
        through_sequence=through,
        graph_reducer_id=contract.graph_reducer_id,
        graph_reducer_version=contract.graph_reducer_version,
        graph_reducer_contract_fingerprint=(
            contract.graph_reducer_contract_fingerprint
        ),
        graph_schema_version=contract.graph_schema_version,
        graph_state_semantic_fingerprint=state_fingerprint,
        graph_event_count=graph_event_count,
        graph_semantic_accumulator=graph_semantic_accumulator,
        ledger_continuity_accumulator=ledger_continuity_accumulator,
        run_count=len(state.runs),
        task_count=len(state.tasks),
        result_count=len(state.results),
        edge_count=len(state.edges),
        delivery_count=len(state.deliveries),
        consistent=True,
    )
    artifact_payload = reducer_binding.export_canonical_state(state)
    content_hash = f"sha256:{sha256(artifact_payload).hexdigest()}"
    artifact_id = _stable_id(
        "subagent_graph_checkpoint_artifact:v1", checkpoint_id
    )
    metadata_payload = {
        "artifact_id": artifact_id,
        "media_type": CHECKPOINT_MEDIA_TYPE,
        "content_sha256": content_hash,
        "byte_count": len(artifact_payload),
        "checkpoint_state": checkpoint,
    }
    artifact = SubagentGraphCheckpointArtifactFact(
        **metadata_payload,
        semantic_metadata_fingerprint=context_fingerprint(
            "subagent-graph-checkpoint-artifact-metadata:v1", metadata_payload
        ),
    )
    event = SubagentGraphCheckpointCommittedEvent(
        id=_stable_id("subagent_graph_checkpoint_committed:v1", checkpoint_id),
        created_at=through_event.created_at_utc,
        run_id=through_event.run_id,
        turn_id=through_event.turn_id,
        reply_id=through_event.reply_id,
        checkpoint=checkpoint,
        artifact=artifact,
    )
    return PreparedSubagentGraphCheckpoint(
        state=state,
        checkpoint=checkpoint,
        artifact=artifact,
        artifact_payload_bytes=artifact_payload,
        event=event,
    )


def _semantic_source(
    *,
    runtime_session_id: str,
    state: SubagentGraphState,
    graph_event_count: int,
    graph_semantic_accumulator: str,
    contract: SubagentGraphReducerContractFact,
) -> SubagentGraphSemanticSourceFact:
    payload = {
        "schema_version": "subagent_graph_semantic_source.v1",
        "runtime_session_id": runtime_session_id,
        "graph_event_count": graph_event_count,
        "graph_semantic_accumulator": graph_semantic_accumulator,
        "graph_reducer_id": contract.graph_reducer_id,
        "graph_reducer_version": contract.graph_reducer_version,
        "graph_reducer_contract_fingerprint": (
            contract.graph_reducer_contract_fingerprint
        ),
        "graph_state_semantic_fingerprint": graph_state_semantic_fingerprint(state),
    }
    return SubagentGraphSemanticSourceFact(
        **payload,
        semantic_source_fingerprint=context_fingerprint(
            "subagent-graph-semantic-source:v1", payload
        ),
    )


def restore_subagent_graph_from_checkpoint(
    *,
    snapshot: SubagentGraphCheckpointDeltaSnapshot,
    reducer_binding: SubagentGraphReducerBinding,
) -> tuple[
    SubagentGraphState,
    SubagentGraphSemanticSourceFact,
    SubagentGraphAccelerationFact,
]:
    event = snapshot.checkpoint_event.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    if not isinstance(event, SubagentGraphCheckpointCommittedEvent):
        raise SubagentGraphCheckpointContractMismatch(
            "checkpoint catalog row is not a checkpoint event"
        )
    checkpoint = event.checkpoint
    if snapshot.selected_checkpoint_id != checkpoint.checkpoint_id:
        raise SubagentGraphCheckpointContractMismatch(
            "selected checkpoint identity differs from event"
        )
    if (
        checkpoint.graph_reducer_id != reducer_binding.contract.graph_reducer_id
        or checkpoint.graph_reducer_version
        != reducer_binding.contract.graph_reducer_version
        or checkpoint.graph_reducer_contract_fingerprint
        != reducer_binding.contract.graph_reducer_contract_fingerprint
    ):
        raise SubagentGraphCheckpointContractMismatch(
            "checkpoint reducer contract differs from process binding"
        )
    if (
        f"sha256:{sha256(snapshot.checkpoint_payload_bytes).hexdigest()}"
        != event.artifact.content_sha256
        or len(snapshot.checkpoint_payload_bytes) != event.artifact.byte_count
    ):
        raise SubagentGraphCheckpointContractMismatch(
            "checkpoint artifact content identity mismatch"
        )
    state = reducer_binding.restore_canonical_state(snapshot.checkpoint_payload_bytes)
    if (
        state.through_sequence != checkpoint.through_sequence
        or graph_state_semantic_fingerprint(state)
        != checkpoint.graph_state_semantic_fingerprint
    ):
        raise SubagentGraphCheckpointContractMismatch(
            "checkpoint state does not match event fact"
        )
    first_delta = checkpoint.through_sequence + 1
    if snapshot.requested_through_sequence < checkpoint.through_sequence:
        raise SubagentGraphCheckpointLedgerUntrusted(
            "checkpoint is newer than requested authority prefix"
        )
    if snapshot.delta_events:
        _validate_contiguous(
            snapshot.delta_events,
            first_sequence=first_delta,
            through_sequence=snapshot.requested_through_sequence,
        )
    elif first_delta != snapshot.requested_through_sequence + 1:
        raise SubagentGraphCheckpointLedgerUntrusted(
            "checkpoint delta is unexpectedly empty"
        )
    graph_count = checkpoint.graph_event_count
    graph_accumulator = checkpoint.graph_semantic_accumulator
    ledger_accumulator = checkpoint.ledger_continuity_accumulator
    delta_bytes = 0
    for envelope in snapshot.delta_events:
        delta_bytes += len(envelope.canonical_payload_bytes)
        ledger_accumulator = extend_ledger_continuity_accumulator(
            ledger_accumulator, envelope
        )
        semantic = graph_semantic_payload_fingerprint(
            envelope=envelope,
            contract=reducer_binding.contract,
        )
        if semantic is not None:
            graph_count += 1
            graph_accumulator = extend_graph_semantic_accumulator(
                graph_accumulator,
                event_id=envelope.event_id,
                semantic_payload_fingerprint=semantic,
            )
        state = reducer_binding.fold_stored_event(state, envelope)
    if (
        not state.consistent
        or state.through_sequence != snapshot.requested_through_sequence
    ):
        raise SubagentGraphCheckpointLedgerUntrusted(
            "checkpoint delta reduced to an inconsistent graph"
        )
    semantic_source = _semantic_source(
        runtime_session_id=checkpoint.parent_runtime_session_id,
        state=state,
        graph_event_count=graph_count,
        graph_semantic_accumulator=graph_accumulator,
        contract=reducer_binding.contract,
    )
    acceleration_payload = {
        "schema_version": "subagent_graph_acceleration.v1",
        "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_materialization_event_id": snapshot.checkpoint_event.event_id,
        "checkpoint_through_sequence": checkpoint.through_sequence,
        "checkpoint_ledger_continuity_accumulator": (
            checkpoint.ledger_continuity_accumulator
        ),
        "delta_from_sequence": first_delta,
        "delta_through_sequence": snapshot.requested_through_sequence,
        "delta_count": len(snapshot.delta_events),
        "delta_byte_count": delta_bytes,
        "ledger_through_sequence": snapshot.requested_through_sequence,
        "ledger_continuity_accumulator": ledger_accumulator,
    }
    acceleration = SubagentGraphAccelerationFact(
        **acceleration_payload,
        acceleration_fingerprint=context_fingerprint(
            "subagent-graph-acceleration:v1", acceleration_payload
        ),
    )
    return state, semantic_source, acceleration
