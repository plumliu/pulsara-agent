"""Bounded transcript projection restore from a run seed or checkpoint."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from pulsara_agent.event import (
    EventType,
    ModelCallStartEvent,
    ModelCallTerminalProjectionCommittedEvent,
    RunStartEvent,
    TranscriptProjectionCheckpointCommittedEvent,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives.authority_materialization import (
    AuthorityMaterializationLimits,
    TranscriptEventDomainRegistryContractFact,
    TranscriptProjectionStableSemanticStateFact,
)
from pulsara_agent.primitives.transcript_projection import (
    CheckpointProjectionBaseFact,
    NormalizedMessageContentArtifactFact,
    ProjectionBaseCommonFact,
    ProjectionBaseSemanticIdentityFact,
    RunSeedProjectionBaseFact,
    TranscriptProjectionBaseFact,
    TranscriptProjectionLeafEntryFact,
    TranscriptProjectionAccelerationFact,
    TranscriptProjectionSemanticSourceFact,
)
from pulsara_agent.primitives.authority_materialization import (
    TranscriptDomainSparseReadProofFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.event_log.protocol import RawStoredEventEnvelope
from pulsara_agent.event_log.protocol import RawTranscriptDomainPrefixFact
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.runtime.authority_materialization.checkpoint import (
    TRANSCRIPT_CHECKPOINT_BUILD_CONTRACT_FINGERPRINT,
)
from pulsara_agent.runtime.authority_materialization.transcript_hydrator import (
    TranscriptProjectionHydrationError,
    hydrate_run_transcript_seed,
    hydrate_transcript_projection_materialization,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
    TranscriptProjectionDocumentRegistry,
    TranscriptProjectionStateStore,
    projection_references,
    stable_entry_projection_references,
)
from pulsara_agent.runtime.authority_materialization.evidence_cursor import (
    CheckpointCursorBaseIdentity,
    RunSeedCursorBaseIdentity,
    TranscriptProjectionCursorBaseIdentity,
    ValidatedCursorSnapshotFactory,
    raw_prefix_fingerprint,
)
from pulsara_agent.runtime.authority_materialization.transcript_tree import (
    TranscriptProjectionMaterializationContracts,
)
from pulsara_agent.runtime.authority_materialization.contracts import (
    TranscriptEventDomainRegistryBinding,
    materialize_transcript_sparse_read_proof,
)
from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
    CheckpointMaintenanceAuthority,
    checkpoint_maintenance_authority_for_event_log,
)


@dataclass(frozen=True, slots=True)
class RestoredTranscriptProjection:
    base_kind: Literal["empty", "run_seed", "checkpoint", "test_genesis"]
    base_sequence: int
    base_id: str | None
    state_store: TranscriptProjectionStateStore
    document_registry: TranscriptProjectionDocumentRegistry
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...]
    hydrated_message_contents: tuple[NormalizedMessageContentArtifactFact, ...]
    reachable_artifact_ids: frozenset[str]
    projection_base: TranscriptProjectionBaseFact | None
    semantic_source: TranscriptProjectionSemanticSourceFact
    domain_completeness_proof: TranscriptDomainSparseReadProofFact
    semantic_delta_events: tuple[RawStoredEventEnvelope, ...]
    active_run_start: RunStartEvent | None
    anchor_carrier_event: (
        RunStartEvent | TranscriptProjectionCheckpointCommittedEvent | None
    )
    delta_before: RawTranscriptDomainPrefixFact
    delta_after: RawTranscriptDomainPrefixFact


@dataclass(frozen=True, slots=True)
class _RestoredProjectionDelta:
    store: TranscriptProjectionStateStore
    documents: TranscriptProjectionDocumentRegistry
    semantic_source: TranscriptProjectionSemanticSourceFact
    domain_completeness_proof: TranscriptDomainSparseReadProofFact
    semantic_delta_events: tuple[RawStoredEventEnvelope, ...]
    delta: object


_MAX_TRANSCRIPT_CHECKPOINT_RESTORE_CANDIDATES = 8


def restore_transcript_projection(
    *,
    event_log: EventLog,
    archive: ArtifactStore,
    runtime_session_id: str,
    requested_through_sequence: int,
    event_domain_binding: TranscriptEventDomainRegistryBinding,
    materialization_contracts: TranscriptProjectionMaterializationContracts,
    limits: AuthorityMaterializationLimits,
    deadline_monotonic: float | None = None,
    allow_seedless_test_bootstrap: bool = False,
    maintenance_authority: CheckpointMaintenanceAuthority | None = None,
) -> RestoredTranscriptProjection:
    """Restore the newest compatible memoization base plus one bounded delta."""

    authority = maintenance_authority
    if authority is None:
        authority = checkpoint_maintenance_authority_for_event_log(event_log)
    guard = (
        authority.acquire_shared(runtime_session_id)
        if authority is not None
        else nullcontext()
    )
    with guard:
        return _restore_transcript_projection_locked(
            event_log=event_log,
            archive=archive,
            runtime_session_id=runtime_session_id,
            requested_through_sequence=requested_through_sequence,
            event_domain_binding=event_domain_binding,
            materialization_contracts=materialization_contracts,
            limits=limits,
            deadline_monotonic=deadline_monotonic,
            allow_seedless_test_bootstrap=allow_seedless_test_bootstrap,
        )


def _restore_transcript_projection_locked(
    *,
    event_log: EventLog,
    archive: ArtifactStore,
    runtime_session_id: str,
    requested_through_sequence: int,
    event_domain_binding: TranscriptEventDomainRegistryBinding,
    materialization_contracts: TranscriptProjectionMaterializationContracts,
    limits: AuthorityMaterializationLimits,
    deadline_monotonic: float | None,
    allow_seedless_test_bootstrap: bool,
) -> RestoredTranscriptProjection:

    if requested_through_sequence < 0:
        raise ValueError("transcript restore high-water cannot be negative")
    event_domain_contract = event_domain_binding.contract
    deadline = (
        monotonic() + limits.operation_timeout_seconds
        if deadline_monotonic is None
        else deadline_monotonic
    )
    checkpoints = _checkpoint_candidates(
        event_log=event_log,
        runtime_session_id=runtime_session_id,
        through_sequence=requested_through_sequence,
        event_domain_contract=event_domain_contract,
        deadline_monotonic=deadline,
    )
    run_start = _latest_run_start(
        event_log=event_log,
        runtime_session_id=runtime_session_id,
        through_sequence=requested_through_sequence,
        event_domain_contract=event_domain_contract,
        deadline_monotonic=deadline,
    )

    seed_sequence = (
        run_start.run_transcript_seed_reference.source_ledger_through_sequence
        if run_start is not None
        else -1
    )
    base_kind: Literal["empty", "run_seed", "checkpoint", "test_genesis"]
    base_id: str | None
    hydrated_contents: tuple[NormalizedMessageContentArtifactFact, ...]
    reachable_artifacts: frozenset[str]
    checkpoint: TranscriptProjectionCheckpointCommittedEvent | None = None
    hydrated = None
    for candidate_event in checkpoints:
        candidate = candidate_event.checkpoint
        if candidate.candidate_ledger_through_sequence < seed_sequence:
            continue
        try:
            candidate_hydrated = hydrate_transcript_projection_materialization(
                archive=archive,
                runtime_session_id=runtime_session_id,
                root_reference=candidate.materialization.root_manifest_ref,
                contracts=materialization_contracts,
                deadline_monotonic=deadline,
            )
        except (KeyError, TranscriptProjectionHydrationError):
            continue
        checkpoint = candidate_event
        hydrated = candidate_hydrated
        break
    if checkpoint is not None and hydrated is not None:
        candidate = checkpoint.checkpoint
        stable_state = candidate.stable_semantic_state
        stable_entries = hydrated.entries
        base_sequence = candidate.candidate_ledger_through_sequence
        base_continuity = candidate.candidate_ledger_continuity_accumulator
        hydrated_contents = hydrated.hydrated_message_contents
        reachable_artifacts = hydrated.reachable_artifact_ids
        base_kind = "checkpoint"
        base_id = candidate.checkpoint_id
    elif run_start is not None:
        hydrated = hydrate_run_transcript_seed(
            archive=archive,
            runtime_session_id=runtime_session_id,
            seed_semantic=run_start.run_transcript_seed_semantic,
            seed_reference=run_start.run_transcript_seed_reference,
            contracts=materialization_contracts,
            deadline_monotonic=deadline,
        )
        stable_state = (
            run_start.run_transcript_seed_semantic.prior_stable_semantic_state
        )
        stable_entries = hydrated.entries
        base_sequence = (
            run_start.run_transcript_seed_reference.source_ledger_through_sequence
        )
        base_continuity = (
            run_start.run_transcript_seed_reference.source_ledger_continuity_accumulator
        )
        hydrated_contents = hydrated.hydrated_message_contents
        reachable_artifacts = hydrated.reachable_artifact_ids
        base_kind = "run_seed"
        base_id = run_start.id
    elif requested_through_sequence == 0 or allow_seedless_test_bootstrap:
        base_sequence = 0
        base_continuity = _empty_continuity()
        stable_state = _empty_stable_state()
        stable_entries = ()
        hydrated_contents = ()
        reachable_artifacts = frozenset()
        base_kind = "empty" if requested_through_sequence == 0 else "test_genesis"
        base_id = None
    else:
        raise ValueError("non-empty transcript ledger has no durable restore base")

    restored_delta = _restore_projection_delta(
        event_log=event_log,
        archive=archive,
        runtime_session_id=runtime_session_id,
        requested_through_sequence=requested_through_sequence,
        event_domain_binding=event_domain_binding,
        limits=limits,
        deadline_monotonic=deadline,
        base_sequence=base_sequence,
        base_continuity=base_continuity,
        stable_state=stable_state,
        stable_entries=stable_entries,
    )
    projection_base = _projection_base(
        run_start=run_start,
        checkpoint=checkpoint,
        stable_state=stable_state,
        requested_through_sequence=requested_through_sequence,
        delta=restored_delta.delta,
    )
    return RestoredTranscriptProjection(
        base_kind=base_kind,
        base_sequence=base_sequence,
        base_id=base_id,
        state_store=restored_delta.store,
        document_registry=restored_delta.documents,
        stable_entries=restored_delta.store.stable_entries(),
        hydrated_message_contents=hydrated_contents,
        reachable_artifact_ids=reachable_artifacts,
        projection_base=projection_base,
        semantic_source=restored_delta.semantic_source,
        domain_completeness_proof=(restored_delta.domain_completeness_proof),
        semantic_delta_events=restored_delta.semantic_delta_events,
        active_run_start=run_start,
        anchor_carrier_event=checkpoint or run_start,
        delta_before=restored_delta.delta.before,
        delta_after=restored_delta.delta.after,
    )


def restore_transcript_projection_from_base(
    *,
    event_log: EventLog,
    archive: ArtifactStore,
    runtime_session_id: str,
    requested_through_sequence: int,
    projection_base: TranscriptProjectionBaseFact,
    frozen_anchor_identity: TranscriptProjectionCursorBaseIdentity | None = None,
    event_domain_binding: TranscriptEventDomainRegistryBinding,
    materialization_contracts: TranscriptProjectionMaterializationContracts,
    limits: AuthorityMaterializationLimits,
    deadline_monotonic: float | None = None,
    maintenance_authority: CheckpointMaintenanceAuthority | None = None,
) -> RestoredTranscriptProjection:
    """Restore the exact manifest-owned base instead of selecting a newer one."""

    authority = maintenance_authority
    if authority is None:
        authority = checkpoint_maintenance_authority_for_event_log(event_log)
    guard = (
        authority.acquire_shared(runtime_session_id)
        if authority is not None
        else nullcontext()
    )
    with guard:
        return _restore_transcript_projection_from_base_locked(
            event_log=event_log,
            archive=archive,
            runtime_session_id=runtime_session_id,
            requested_through_sequence=requested_through_sequence,
            projection_base=projection_base,
            frozen_anchor_identity=frozen_anchor_identity,
            event_domain_binding=event_domain_binding,
            materialization_contracts=materialization_contracts,
            limits=limits,
            deadline_monotonic=deadline_monotonic,
        )


def _restore_transcript_projection_from_base_locked(
    *,
    event_log: EventLog,
    archive: ArtifactStore,
    runtime_session_id: str,
    requested_through_sequence: int,
    projection_base: TranscriptProjectionBaseFact,
    frozen_anchor_identity: TranscriptProjectionCursorBaseIdentity | None,
    event_domain_binding: TranscriptEventDomainRegistryBinding,
    materialization_contracts: TranscriptProjectionMaterializationContracts,
    limits: AuthorityMaterializationLimits,
    deadline_monotonic: float | None,
) -> RestoredTranscriptProjection:

    deadline = (
        monotonic() + limits.operation_timeout_seconds
        if deadline_monotonic is None
        else deadline_monotonic
    )
    common = projection_base.common
    seed_reference = common.run_seed_reference
    if seed_reference.source_runtime_session_id != runtime_session_id:
        raise ValueError("transcript projection base ledger attribution drifted")
    domain_fingerprint = event_domain_binding.contract.registry_contract_fingerprint
    seed_source = common.run_seed_semantic.prior_semantic_source
    if (
        seed_source.reducer_id != "pulsara.transcript-projection"
        or seed_source.reducer_version != "1"
        or seed_source.reducer_contract_fingerprint
        != TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        or seed_source.transcript_semantic_domain_contract_fingerprint
        != domain_fingerprint
    ):
        raise ValueError("transcript projection seed contract is unsupported")

    if isinstance(projection_base, RunSeedProjectionBaseFact):
        anchor_carrier_event = _load_frozen_run_seed_carrier(
            event_log=event_log,
            runtime_session_id=runtime_session_id,
            requested_through_sequence=requested_through_sequence,
            projection_base=projection_base,
            frozen_anchor_identity=frozen_anchor_identity,
            deadline_monotonic=deadline,
        )
        hydrated = hydrate_run_transcript_seed(
            archive=archive,
            runtime_session_id=runtime_session_id,
            seed_semantic=common.run_seed_semantic,
            seed_reference=seed_reference,
            contracts=materialization_contracts,
            deadline_monotonic=deadline,
        )
        base_kind: Literal["run_seed", "checkpoint"] = "run_seed"
        base_id = seed_reference.seed_artifact_id
        base_sequence = seed_reference.source_ledger_through_sequence
        base_continuity = seed_reference.source_ledger_continuity_accumulator
    else:
        acceleration = projection_base.checkpoint_acceleration
        rows = event_log.read_raw_events_by_id(
            (acceleration.checkpoint_committed_event_id,),
            deadline_monotonic=deadline,
        )
        if len(rows) != 1:
            raise ValueError("manifest transcript checkpoint event is unavailable")
        raw = rows[0]
        event = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if (
            raw.sequence != acceleration.checkpoint_committed_event_sequence
            or not isinstance(event, TranscriptProjectionCheckpointCommittedEvent)
        ):
            raise ValueError("manifest transcript checkpoint identity drifted")
        candidate = event.checkpoint
        if (
            candidate.checkpoint_id != acceleration.checkpoint_id
            or candidate.run_seed_semantic != common.run_seed_semantic
            or candidate.run_seed_reference != seed_reference
            or candidate.stable_semantic_state != common.stable_semantic_state
            or candidate.materialization != projection_base.checkpoint_materialization
            or candidate.candidate_ledger_through_sequence
            != acceleration.checkpoint_candidate_ledger_through_sequence
            or candidate.candidate_ledger_continuity_accumulator
            != acceleration.checkpoint_candidate_ledger_continuity_accumulator
            or candidate.scope != acceleration.scope
            or candidate.build_contract_fingerprint
            != acceleration.build_contract_fingerprint
        ):
            raise ValueError("manifest transcript checkpoint candidate drifted")
        anchor_carrier_event = event
        _validate_frozen_checkpoint_carrier(
            runtime_session_id=runtime_session_id,
            requested_through_sequence=requested_through_sequence,
            projection_base=projection_base,
            event=event,
            frozen_anchor_identity=frozen_anchor_identity,
        )
        hydrated = hydrate_transcript_projection_materialization(
            archive=archive,
            runtime_session_id=runtime_session_id,
            root_reference=(
                projection_base.checkpoint_materialization.root_manifest_ref
            ),
            contracts=materialization_contracts,
            deadline_monotonic=deadline,
        )
        base_kind = "checkpoint"
        base_id = acceleration.checkpoint_id
        base_sequence = acceleration.checkpoint_candidate_ledger_through_sequence
        base_continuity = (
            acceleration.checkpoint_candidate_ledger_continuity_accumulator
        )
        if (
            acceleration.delta_through_sequence != requested_through_sequence
            or acceleration.ledger_through_sequence != requested_through_sequence
        ):
            raise ValueError("manifest checkpoint delta high-water drifted")

    if requested_through_sequence < base_sequence:
        raise ValueError("manifest transcript high-water precedes its base")
    stable_state = common.stable_semantic_state
    stable_entries = hydrated.entries
    restored_delta = _restore_projection_delta(
        event_log=event_log,
        archive=archive,
        runtime_session_id=runtime_session_id,
        requested_through_sequence=requested_through_sequence,
        event_domain_binding=event_domain_binding,
        limits=limits,
        deadline_monotonic=deadline,
        base_sequence=base_sequence,
        base_continuity=base_continuity,
        stable_state=stable_state,
        stable_entries=stable_entries,
    )
    _validate_frozen_anchor_base_prefix(
        frozen_anchor_identity=frozen_anchor_identity,
        runtime_session_id=runtime_session_id,
        base_prefix=restored_delta.delta.before,
        event_domain_registry_contract_fingerprint=domain_fingerprint,
    )
    return RestoredTranscriptProjection(
        base_kind=base_kind,
        base_sequence=base_sequence,
        base_id=base_id,
        state_store=restored_delta.store,
        document_registry=restored_delta.documents,
        stable_entries=restored_delta.store.stable_entries(),
        hydrated_message_contents=hydrated.hydrated_message_contents,
        reachable_artifact_ids=hydrated.reachable_artifact_ids,
        projection_base=projection_base,
        semantic_source=restored_delta.semantic_source,
        domain_completeness_proof=(restored_delta.domain_completeness_proof),
        semantic_delta_events=restored_delta.semantic_delta_events,
        active_run_start=(
            anchor_carrier_event
            if isinstance(anchor_carrier_event, RunStartEvent)
            else None
        ),
        anchor_carrier_event=anchor_carrier_event,
        delta_before=restored_delta.delta.before,
        delta_after=restored_delta.delta.after,
    )


def _load_frozen_run_seed_carrier(
    *,
    event_log: EventLog,
    runtime_session_id: str,
    requested_through_sequence: int,
    projection_base: RunSeedProjectionBaseFact,
    frozen_anchor_identity: TranscriptProjectionCursorBaseIdentity | None,
    deadline_monotonic: float,
) -> RunStartEvent | None:
    if frozen_anchor_identity is None:
        return None
    ValidatedCursorSnapshotFactory.validate_base_identity(frozen_anchor_identity)
    if not isinstance(frozen_anchor_identity, RunSeedCursorBaseIdentity):
        raise ValueError("run-seed restore received a checkpoint anchor identity")
    common = projection_base.common
    carrier = frozen_anchor_identity.anchor_carrier
    if (
        frozen_anchor_identity.runtime_session_id != runtime_session_id
        or carrier.carrier_kind != "run_start"
        or frozen_anchor_identity.anchor_available_from_sequence
        != carrier.committed_sequence
        or carrier.committed_sequence > requested_through_sequence
        or frozen_anchor_identity.run_seed_semantic_fingerprint
        != common.run_seed_semantic.seed_semantic_fingerprint
        or frozen_anchor_identity.run_seed_reference_fingerprint
        != common.run_seed_reference.reference_fingerprint
        or frozen_anchor_identity.stable_state_semantic_fingerprint
        != common.stable_semantic_state.state_semantic_fingerprint
        or frozen_anchor_identity.base_ledger_through_sequence
        != common.run_seed_reference.source_ledger_through_sequence
        or frozen_anchor_identity.base_ledger_continuity_accumulator
        != common.run_seed_reference.source_ledger_continuity_accumulator
    ):
        raise ValueError("frozen run-seed anchor identity drifted")
    rows = event_log.read_raw_events_by_id(
        (carrier.stable_event_identity.event_id,),
        deadline_monotonic=deadline_monotonic,
    )
    if len(rows) != 1:
        raise ValueError("frozen RunStart anchor carrier is unavailable")
    raw = rows[0]
    event = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    if (
        not isinstance(event, RunStartEvent)
        or raw.sequence != carrier.committed_sequence
        or event.sequence != carrier.committed_sequence
        or event.run_transcript_seed_semantic != common.run_seed_semantic
        or event.run_transcript_seed_reference != common.run_seed_reference
        or stable_event_identity(event, runtime_session_id=runtime_session_id)
        != carrier.stable_event_identity
    ):
        raise ValueError("frozen RunStart anchor carrier identity drifted")
    return event


def _validate_frozen_checkpoint_carrier(
    *,
    runtime_session_id: str,
    requested_through_sequence: int,
    projection_base: CheckpointProjectionBaseFact,
    event: TranscriptProjectionCheckpointCommittedEvent,
    frozen_anchor_identity: TranscriptProjectionCursorBaseIdentity | None,
) -> None:
    if frozen_anchor_identity is None:
        return
    ValidatedCursorSnapshotFactory.validate_base_identity(frozen_anchor_identity)
    if not isinstance(frozen_anchor_identity, CheckpointCursorBaseIdentity):
        raise ValueError("checkpoint restore received a run-seed anchor identity")
    common = projection_base.common
    acceleration = projection_base.checkpoint_acceleration
    materialization = projection_base.checkpoint_materialization
    carrier = frozen_anchor_identity.anchor_carrier
    if (
        frozen_anchor_identity.runtime_session_id != runtime_session_id
        or carrier.carrier_kind != "transcript_checkpoint_committed"
        or frozen_anchor_identity.anchor_available_from_sequence
        != carrier.committed_sequence
        or carrier.committed_sequence > requested_through_sequence
        or frozen_anchor_identity.checkpoint_id != acceleration.checkpoint_id
        or frozen_anchor_identity.checkpoint_committed_event_id != event.id
        or frozen_anchor_identity.checkpoint_committed_event_sequence
        != event.sequence
        or frozen_anchor_identity.checkpoint_candidate_fingerprint
        != event.checkpoint_candidate_fingerprint
        or frozen_anchor_identity.checkpoint_candidate_ledger_through_sequence
        != acceleration.checkpoint_candidate_ledger_through_sequence
        or frozen_anchor_identity.checkpoint_candidate_ledger_continuity_accumulator
        != acceleration.checkpoint_candidate_ledger_continuity_accumulator
        or frozen_anchor_identity.checkpoint_materialization_fingerprint
        != materialization.materialization_fingerprint
        or frozen_anchor_identity.previous_checkpoint_id
        != acceleration.previous_checkpoint_id
        or frozen_anchor_identity.ledger_materialization_generation
        != acceleration.ledger_materialization_generation
        or frozen_anchor_identity.consumer_horizon_revision
        != acceleration.consumer_horizon_revision
        or frozen_anchor_identity.checkpoint_build_contract_fingerprint
        != acceleration.build_contract_fingerprint
        or frozen_anchor_identity.run_seed_semantic_fingerprint
        != common.run_seed_semantic.seed_semantic_fingerprint
        or frozen_anchor_identity.run_seed_reference_fingerprint
        != common.run_seed_reference.reference_fingerprint
        or frozen_anchor_identity.stable_state_semantic_fingerprint
        != common.stable_semantic_state.state_semantic_fingerprint
        or frozen_anchor_identity.base_ledger_through_sequence
        != acceleration.checkpoint_candidate_ledger_through_sequence
        or frozen_anchor_identity.base_ledger_continuity_accumulator
        != acceleration.checkpoint_candidate_ledger_continuity_accumulator
        or carrier.stable_event_identity.event_id != event.id
        or carrier.committed_sequence != event.sequence
        or stable_event_identity(event, runtime_session_id=runtime_session_id)
        != carrier.stable_event_identity
    ):
        raise ValueError("frozen checkpoint anchor carrier identity drifted")


def _validate_frozen_anchor_base_prefix(
    *,
    frozen_anchor_identity: TranscriptProjectionCursorBaseIdentity | None,
    runtime_session_id: str,
    base_prefix: RawTranscriptDomainPrefixFact,
    event_domain_registry_contract_fingerprint: str,
) -> None:
    if frozen_anchor_identity is None:
        return
    if (
        frozen_anchor_identity.runtime_session_id != runtime_session_id
        or frozen_anchor_identity.base_ledger_through_sequence
        != base_prefix.through_sequence
        or frozen_anchor_identity.base_ledger_continuity_accumulator
        != base_prefix.ledger_continuity_accumulator
        or frozen_anchor_identity.canonical_base_prefix_fingerprint
        != raw_prefix_fingerprint(base_prefix)
        or frozen_anchor_identity.event_domain_registry_contract_fingerprint
        != event_domain_registry_contract_fingerprint
        or frozen_anchor_identity.reducer_contract_fingerprint
        != TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
    ):
        raise ValueError("frozen transcript anchor base prefix drifted")


def _restore_projection_delta(
    *,
    event_log: EventLog,
    archive: ArtifactStore,
    runtime_session_id: str,
    requested_through_sequence: int,
    event_domain_binding: TranscriptEventDomainRegistryBinding,
    limits: AuthorityMaterializationLimits,
    deadline_monotonic: float,
    base_sequence: int,
    base_continuity: str,
    stable_state: TranscriptProjectionStableSemanticStateFact,
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...],
) -> _RestoredProjectionDelta:
    event_domain_contract = event_domain_binding.contract
    delta = event_log.read_transcript_domain_delta(
        after_sequence=base_sequence,
        through_sequence=requested_through_sequence,
        max_events=limits.max_unreclaimable_ledger_events,
        max_payload_bytes=limits.max_unreclaimable_charged_payload_bytes,
        registry_contract_fingerprint=(
            event_domain_contract.registry_contract_fingerprint
        ),
        deadline_monotonic=deadline_monotonic,
    )
    semantic_events = tuple(
        raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY) for raw in delta.semantic_events
    )
    documents = TranscriptProjectionDocumentRegistry()
    from pulsara_agent.llm.terminal_projection import hydrate_terminal_projection_text

    references_by_fingerprint = {
        reference.reference_fingerprint: reference
        for reference in (
            *stable_entry_projection_references(stable_entries),
            *projection_references(semantic_events),
        )
    }
    for reference in references_by_fingerprint.values():
        text = archive.get_text(
            reference.document_artifact_id,
            session_id=runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        documents.register(
            reference,
            hydrate_terminal_projection_text(reference, text),
        )
    start_ids = tuple(
        dict.fromkeys(
            event.model_call_start_event_identity.event_id
            for event in semantic_events
            if isinstance(event, ModelCallTerminalProjectionCommittedEvent)
        )
    )
    start_raw = event_log.read_raw_events_by_id(
        start_ids,
        deadline_monotonic=deadline_monotonic,
    )
    if len(start_raw) != len(start_ids):
        raise ValueError("transcript projection delta is missing model Start")
    starts: list[ModelCallStartEvent] = []
    by_id = {item.event_id: item for item in start_raw}
    for event_id in start_ids:
        raw = by_id[event_id]
        event = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if not isinstance(event, ModelCallStartEvent):
            raise ValueError("transcript projection Start reference has wrong type")
        starts.append(event)

    store = TranscriptProjectionStateStore(
        runtime_session_id=runtime_session_id,
        documents=documents,
    )
    store.restore_from_stable_base(
        stable_state=stable_state,
        stable_entries=stable_entries,
        ledger_through_sequence=base_sequence,
        ledger_continuity_accumulator=base_continuity,
        delta=delta,
        model_start_events=tuple(starts),
    )
    final_state = store.snapshot().stable_semantic_state
    semantic_source = build_frozen_fact(
        TranscriptProjectionSemanticSourceFact,
        schema_version="transcript_projection_semantic_source.v1",
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            event_domain_contract.registry_contract_fingerprint
        ),
        semantic_source_event_count=final_state.semantic_source_event_count,
        semantic_source_accumulator=final_state.semantic_source_accumulator,
        resulting_state_fingerprint=final_state.state_semantic_fingerprint,
    )
    return _RestoredProjectionDelta(
        store=store,
        documents=documents,
        semantic_source=semantic_source,
        domain_completeness_proof=materialize_transcript_sparse_read_proof(
            delta,
            binding=event_domain_binding,
        ),
        semantic_delta_events=delta.semantic_events,
        delta=delta,
    )


def _projection_base(
    *,
    run_start: RunStartEvent | None,
    checkpoint: TranscriptProjectionCheckpointCommittedEvent | None,
    stable_state: TranscriptProjectionStableSemanticStateFact,
    requested_through_sequence: int,
    delta,
) -> TranscriptProjectionBaseFact | None:
    if checkpoint is not None:
        candidate = checkpoint.checkpoint
        seed_semantic = candidate.run_seed_semantic
        seed_reference = candidate.run_seed_reference
    elif run_start is not None:
        seed_semantic = run_start.run_transcript_seed_semantic
        seed_reference = run_start.run_transcript_seed_reference
    else:
        return None
    semantic_identity = build_frozen_fact(
        ProjectionBaseSemanticIdentityFact,
        schema_version="projection_base_semantic_identity.v2",
        run_seed_semantic_fingerprint=seed_semantic.seed_semantic_fingerprint,
        stable_state_semantic_fingerprint=stable_state.state_semantic_fingerprint,
    )
    common = build_frozen_fact(
        ProjectionBaseCommonFact,
        schema_version="projection_base_common.v2",
        run_seed_semantic=seed_semantic,
        run_seed_reference=seed_reference,
        stable_semantic_state=stable_state,
        semantic_identity=semantic_identity,
    )
    if checkpoint is None:
        return build_frozen_fact(
            RunSeedProjectionBaseFact,
            schema_version="run_seed_projection_base.v2",
            base_kind="run_seed",
            common=common,
        )
    candidate = checkpoint.checkpoint
    if checkpoint.sequence is None:
        raise ValueError("checkpoint restore requires committed event sequence")
    acceleration = build_frozen_fact(
        TranscriptProjectionAccelerationFact,
        schema_version="transcript_projection_acceleration.v1",
        scope=candidate.scope,
        checkpoint_id=candidate.checkpoint_id,
        checkpoint_committed_event_id=checkpoint.id,
        checkpoint_committed_event_sequence=checkpoint.sequence,
        checkpoint_candidate_ledger_through_sequence=(
            candidate.candidate_ledger_through_sequence
        ),
        checkpoint_candidate_ledger_continuity_accumulator=(
            candidate.candidate_ledger_continuity_accumulator
        ),
        checkpoint_artifact_ref=candidate.materialization.root_manifest_ref,
        previous_checkpoint_id=candidate.previous_checkpoint_id,
        ledger_materialization_generation=(
            candidate.source_ledger_materialization_generation
        ),
        consumer_horizon_revision=(candidate.source_consumer_horizon_revision + 1),
        delta_from_sequence=candidate.candidate_ledger_through_sequence + 1,
        delta_through_sequence=requested_through_sequence,
        delta_event_count=(
            requested_through_sequence - candidate.candidate_ledger_through_sequence
        ),
        delta_payload_bytes=(
            delta.after.ledger_payload_bytes - delta.before.ledger_payload_bytes
        ),
        ledger_through_sequence=requested_through_sequence,
        ledger_continuity_accumulator=delta.after.ledger_continuity_accumulator,
        event_domain_registry_contract_fingerprint=(
            delta.registry_contract_fingerprint
        ),
        build_contract_fingerprint=candidate.build_contract_fingerprint,
    )
    return build_frozen_fact(
        CheckpointProjectionBaseFact,
        schema_version="checkpoint_projection_base.v2",
        base_kind="checkpoint",
        common=common,
        checkpoint_acceleration=acceleration,
        checkpoint_materialization=candidate.materialization,
    )


def _checkpoint_candidates(
    *,
    event_log: EventLog,
    runtime_session_id: str,
    through_sequence: int,
    event_domain_contract: TranscriptEventDomainRegistryContractFact,
    deadline_monotonic: float,
) -> tuple[TranscriptProjectionCheckpointCommittedEvent, ...]:
    rows = event_log.read_raw_events_by_type(
        EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED.value,
        limit=_MAX_TRANSCRIPT_CHECKPOINT_RESTORE_CANDIDATES,
        through_sequence=through_sequence,
        deadline_monotonic=deadline_monotonic,
    )
    if not rows:
        return ()
    events: list[TranscriptProjectionCheckpointCommittedEvent] = []
    for raw in rows:
        if raw.sequence > through_sequence:
            raise ValueError("transcript checkpoint lies beyond restore high-water")
        event = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if not isinstance(event, TranscriptProjectionCheckpointCommittedEvent):
            raise ValueError("transcript checkpoint event schema drifted")
        candidate = event.checkpoint
        if candidate.scope.runtime_session_id != runtime_session_id:
            raise ValueError("transcript checkpoint ledger attribution drifted")
        source = candidate.semantic_source
        if (
            source.reducer_id != "pulsara.transcript-projection"
            or source.reducer_version != "1"
            or source.reducer_contract_fingerprint
            != TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
            or source.transcript_semantic_domain_contract_fingerprint
            != event_domain_contract.registry_contract_fingerprint
            or candidate.build_contract_fingerprint
            != TRANSCRIPT_CHECKPOINT_BUILD_CONTRACT_FINGERPRINT
        ):
            raise ValueError("transcript checkpoint contract is unsupported")
        events.append(event)
    return tuple(events)


def _latest_run_start(
    *,
    event_log: EventLog,
    runtime_session_id: str,
    through_sequence: int,
    event_domain_contract: TranscriptEventDomainRegistryContractFact,
    deadline_monotonic: float,
) -> RunStartEvent | None:
    rows = event_log.read_raw_events_by_type(
        EventType.RUN_START.value,
        limit=1,
        through_sequence=through_sequence,
        deadline_monotonic=deadline_monotonic,
    )
    if not rows:
        return None
    raw = rows[0]
    if raw.sequence > through_sequence:
        raise ValueError("run transcript seed lies beyond restore high-water")
    event = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    if not isinstance(event, RunStartEvent):
        raise ValueError("run transcript seed event schema drifted")
    seed = event.run_transcript_seed_semantic
    if (
        event.run_transcript_seed_reference.source_runtime_session_id
        != runtime_session_id
        or seed.prior_semantic_source.reducer_id != "pulsara.transcript-projection"
        or seed.prior_semantic_source.reducer_version != "1"
        or seed.prior_semantic_source.reducer_contract_fingerprint
        != TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        or seed.prior_semantic_source.transcript_semantic_domain_contract_fingerprint
        != event_domain_contract.registry_contract_fingerprint
    ):
        raise ValueError("run transcript seed contract is unsupported")
    return event


def _empty_stable_state() -> TranscriptProjectionStableSemanticStateFact:
    from pulsara_agent.event_log.transcript_prefix import (
        EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
    )
    from pulsara_agent.primitives import context_fingerprint
    from pulsara_agent.primitives.frozen import build_frozen_fact

    return build_frozen_fact(
        TranscriptProjectionStableSemanticStateFact,
        schema_version="transcript_projection_stable_semantic_state.v1",
        semantic_source_event_count=0,
        semantic_source_accumulator=EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
        normalized_transcript_fingerprint=context_fingerprint(
            "normalized-transcript-semantic:v1", ()
        ),
    )


def _empty_continuity() -> str:
    from pulsara_agent.event_log.transcript_prefix import (
        EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
    )

    return EMPTY_LEDGER_CONTINUITY_ACCUMULATOR


__all__ = [
    "RestoredTranscriptProjection",
    "restore_transcript_projection",
    "restore_transcript_projection_from_base",
]
