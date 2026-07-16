"""Privileged reachability GC for transcript checkpoint acceleration artifacts."""

from __future__ import annotations

from time import monotonic
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from pulsara_agent.event import (
    ContextCompiledEvent,
    EventType,
    RunStartEvent,
    TranscriptProjectionCheckpointCommittedEvent,
)
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY, EventLog
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives.context import ContextCompileInputManifestFact
from pulsara_agent.primitives.transcript_projection import CheckpointProjectionBaseFact
from pulsara_agent.runtime.authority_materialization.transcript_hydrator import (
    TranscriptProjectionHydrationError,
    hydrate_run_transcript_seed,
    hydrate_transcript_projection_materialization,
)
from pulsara_agent.runtime.authority_materialization.transcript_tree import (
    TranscriptProjectionMaterializationContracts,
)
from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
    CheckpointMaintenanceAuthority,
)


class TranscriptArtifactMaintenanceStore(ArtifactStore, Protocol):
    def delete_if_identity(
        self,
        blob_id: str,
        *,
        session_id: str,
        digest: str,
        media_type: str,
        semantic_metadata_fingerprint: str,
    ) -> bool: ...


class TranscriptProjectionCheckpointGcReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_session_id: str = Field(min_length=1)
    catalog_checkpoint_count: int = Field(ge=0)
    catalog_run_seed_count: int = Field(ge=0)
    catalog_manifest_count: int = Field(ge=0)
    verified_fallback_checkpoint_ids: tuple[str, ...]
    manifest_protected_checkpoint_ids: tuple[str, ...]
    unavailable_checkpoint_ids: tuple[str, ...]
    deleted_artifact_ids: tuple[str, ...]
    already_missing_artifact_ids: tuple[str, ...]
    retained_reachable_artifact_count: int = Field(ge=0)


def garbage_collect_transcript_projection_artifacts(
    *,
    runtime_session_id: str,
    event_log: EventLog,
    archive: TranscriptArtifactMaintenanceStore,
    maintenance_authority: CheckpointMaintenanceAuthority,
    materialization_contracts: TranscriptProjectionMaterializationContracts,
    retained_checkpoint_min_count: int = 1,
    max_catalog_events_per_type: int = 10_000,
    operation_timeout_seconds: float = 30.0,
) -> TranscriptProjectionCheckpointGcReport:
    """Delete only unreachable memoization bytes under exclusive authority."""

    if retained_checkpoint_min_count < 1:
        raise ValueError("transcript checkpoint GC must retain a verified fallback")
    if max_catalog_events_per_type < retained_checkpoint_min_count:
        raise ValueError("transcript checkpoint GC catalog bound is too small")
    if operation_timeout_seconds <= 0:
        raise ValueError("transcript checkpoint GC timeout must be positive")
    deadline = monotonic() + operation_timeout_seconds
    with maintenance_authority.acquire_exclusive(runtime_session_id) as permit:
        if not permit.exclusive or permit.runtime_session_id != runtime_session_id:
            raise RuntimeError("transcript checkpoint maintenance permit mismatch")
        account = event_log.read_materialization_account_state(
            deadline_monotonic=deadline
        )
        if account is not None and (
            account.active_checkpoint_barrier is not None
            or account.active_reservations
        ):
            raise RuntimeError(
                "transcript checkpoint GC requires a drained materialization account"
            )
        checkpoints = _read_typed_catalog(
            event_log=event_log,
            event_type=EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED,
            expected_type=TranscriptProjectionCheckpointCommittedEvent,
            max_events=max_catalog_events_per_type,
            deadline_monotonic=deadline,
        )
        run_starts = _read_typed_catalog(
            event_log=event_log,
            event_type=EventType.RUN_START,
            expected_type=RunStartEvent,
            max_events=max_catalog_events_per_type,
            deadline_monotonic=deadline,
        )
        compiled_events = _read_typed_catalog(
            event_log=event_log,
            event_type=EventType.CONTEXT_COMPILED,
            expected_type=ContextCompiledEvent,
            max_events=max_catalog_events_per_type,
            deadline_monotonic=deadline,
        )

        protected_artifacts: set[str] = set()
        for run_start in run_starts:
            hydrated_seed = hydrate_run_transcript_seed(
                archive=archive,
                runtime_session_id=runtime_session_id,
                seed_semantic=run_start.run_transcript_seed_semantic,
                seed_reference=run_start.run_transcript_seed_reference,
                contracts=materialization_contracts,
                deadline_monotonic=deadline,
            )
            protected_artifacts.update(hydrated_seed.reachable_artifact_ids)

        manifest_protected_roots = _manifest_protected_checkpoint_roots(
            runtime_session_id=runtime_session_id,
            archive=archive,
            compiled_events=compiled_events,
            deadline_monotonic=deadline,
        )
        manifest_protected_checkpoint_ids: set[str] = set()
        seen_manifest_protected_roots: set[str] = set()
        hydrated_by_checkpoint: dict[str, frozenset[str]] = {}
        unavailable: list[str] = []
        ordered_checkpoints = sorted(
            checkpoints,
            key=lambda item: (
                item.checkpoint.candidate_ledger_through_sequence,
                item.sequence or 0,
                item.checkpoint_id,
            ),
            reverse=True,
        )
        verified_fallbacks: list[str] = []
        for checkpoint in ordered_checkpoints:
            root_ref = checkpoint.checkpoint.materialization.root_manifest_ref
            try:
                hydrated = hydrate_transcript_projection_materialization(
                    archive=archive,
                    runtime_session_id=runtime_session_id,
                    root_reference=root_ref,
                    contracts=materialization_contracts,
                    deadline_monotonic=deadline,
                )
            except (KeyError, TranscriptProjectionHydrationError):
                unavailable.append(checkpoint.checkpoint_id)
                continue
            reachable = hydrated.reachable_artifact_ids
            hydrated_by_checkpoint[checkpoint.checkpoint_id] = reachable
            if root_ref.root_artifact_id in manifest_protected_roots:
                seen_manifest_protected_roots.add(root_ref.root_artifact_id)
                manifest_protected_checkpoint_ids.add(checkpoint.checkpoint_id)
                protected_artifacts.update(reachable)
            if len(verified_fallbacks) < retained_checkpoint_min_count:
                verified_fallbacks.append(checkpoint.checkpoint_id)
                protected_artifacts.update(reachable)
        if manifest_protected_roots != seen_manifest_protected_roots:
            raise RuntimeError(
                "context manifest references an unavailable transcript checkpoint"
            )
        if checkpoints and len(verified_fallbacks) < retained_checkpoint_min_count:
            raise RuntimeError(
                "transcript checkpoint GC cannot prove its fallback generation"
            )

        deletion_candidates: set[str] = set()
        for checkpoint_id, reachable in hydrated_by_checkpoint.items():
            if checkpoint_id in verified_fallbacks:
                continue
            deletion_candidates.update(reachable - protected_artifacts)

        deleted: list[str] = []
        missing: list[str] = []
        for artifact_id in sorted(deletion_candidates):
            try:
                record = archive.get_info(
                    artifact_id,
                    session_id=runtime_session_id,
                    deadline_monotonic=deadline,
                )
            except KeyError:
                missing.append(artifact_id)
                continue
            metadata = record.metadata or {}
            semantic_metadata_fingerprint = metadata.get(
                "semantic_metadata_fingerprint"
            )
            if not isinstance(semantic_metadata_fingerprint, str):
                raise RuntimeError(
                    "transcript artifact lacks its maintenance identity"
                )
            removed = archive.delete_if_identity(
                artifact_id,
                session_id=runtime_session_id,
                digest=record.digest,
                media_type=record.media_type,
                semantic_metadata_fingerprint=semantic_metadata_fingerprint,
            )
            (deleted if removed else missing).append(artifact_id)
        return TranscriptProjectionCheckpointGcReport(
            runtime_session_id=runtime_session_id,
            catalog_checkpoint_count=len(checkpoints),
            catalog_run_seed_count=len(run_starts),
            catalog_manifest_count=len(compiled_events),
            verified_fallback_checkpoint_ids=tuple(verified_fallbacks),
            manifest_protected_checkpoint_ids=tuple(
                sorted(manifest_protected_checkpoint_ids)
            ),
            unavailable_checkpoint_ids=tuple(sorted(unavailable)),
            deleted_artifact_ids=tuple(deleted),
            already_missing_artifact_ids=tuple(missing),
            retained_reachable_artifact_count=len(protected_artifacts),
        )


def _read_typed_catalog(
    *,
    event_log: EventLog,
    event_type: EventType,
    expected_type: type,
    max_events: int,
    deadline_monotonic: float,
) -> tuple:
    raw_events = event_log.read_raw_events_by_type(
        str(event_type),
        limit=max_events + 1,
        deadline_monotonic=deadline_monotonic,
    )
    if len(raw_events) > max_events:
        raise RuntimeError("transcript checkpoint GC catalog bound exceeded")
    decoded = tuple(
        raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY) for raw in raw_events
    )
    if any(not isinstance(event, expected_type) for event in decoded):
        raise RuntimeError("transcript checkpoint GC catalog type mismatch")
    return decoded


def _manifest_protected_checkpoint_roots(
    *,
    runtime_session_id: str,
    archive: ArtifactStore,
    compiled_events: tuple[ContextCompiledEvent, ...],
    deadline_monotonic: float,
) -> set[str]:
    protected: set[str] = set()
    for event in compiled_events:
        audit = event.input_audit
        if audit is None:
            continue
        payload = archive.get_text(
            audit.input_manifest_artifact_id,
            session_id=runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        manifest = ContextCompileInputManifestFact.model_validate_json(payload)
        if manifest.manifest_fingerprint != audit.input_manifest_fingerprint:
            raise RuntimeError("context manifest audit fingerprint mismatch")
        projection_base = manifest.transcript_authority.projection_base
        if isinstance(projection_base, CheckpointProjectionBaseFact):
            protected.add(
                projection_base.checkpoint_materialization.root_manifest_ref.root_artifact_id
            )
    return protected


__all__ = [
    "TranscriptProjectionCheckpointGcReport",
    "garbage_collect_transcript_projection_artifacts",
]
