"""Privileged physical retention for discardable checkpoint artifacts."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from pulsara_agent.event import EventType, SubagentGraphCheckpointCommittedEvent
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY, EventLog
from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
    CheckpointMaintenanceAuthority,
)


class CheckpointArtifactMaintenanceStore(Protocol):
    def delete_if_identity(
        self,
        blob_id: str,
        *,
        session_id: str,
        digest: str,
        media_type: str,
        semantic_metadata_fingerprint: str,
    ) -> bool: ...


class SubagentGraphCheckpointGcReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_session_id: str = Field(min_length=1)
    catalog_event_count: int = Field(ge=0)
    retained_checkpoint_ids: tuple[str, ...]
    deleted_checkpoint_ids: tuple[str, ...]
    already_missing_checkpoint_ids: tuple[str, ...]


def garbage_collect_subagent_graph_checkpoint_artifacts(
    *,
    runtime_session_id: str,
    event_log: EventLog,
    archive: CheckpointArtifactMaintenanceStore,
    maintenance_authority: CheckpointMaintenanceAuthority,
    retained_checkpoint_min_count: int,
    max_catalog_events: int = 10_000,
) -> SubagentGraphCheckpointGcReport:
    """Delete old bytes while preserving every durable checkpoint event."""

    if retained_checkpoint_min_count < 1:
        raise ValueError("checkpoint GC must retain at least one checkpoint")
    if max_catalog_events < retained_checkpoint_min_count:
        raise ValueError("checkpoint GC catalog bound is too small")
    with maintenance_authority.acquire_exclusive(runtime_session_id) as permit:
        if not permit.exclusive or permit.runtime_session_id != runtime_session_id:
            raise RuntimeError("checkpoint maintenance permit identity mismatch")
        raw_events = event_log.read_raw_events_by_type(
            str(EventType.SUBAGENT_GRAPH_CHECKPOINT_COMMITTED),
            limit=max_catalog_events,
            deadline_monotonic=None,
        )
        checkpoints: list[SubagentGraphCheckpointCommittedEvent] = []
        for raw in raw_events:
            decoded = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            if not isinstance(decoded, SubagentGraphCheckpointCommittedEvent):
                raise RuntimeError("checkpoint GC catalog contains wrong event type")
            if decoded.checkpoint.parent_runtime_session_id != runtime_session_id:
                raise RuntimeError("checkpoint GC catalog attribution mismatch")
            checkpoints.append(decoded)
        checkpoints.sort(
            key=lambda item: (
                item.checkpoint.through_sequence,
                item.sequence or 0,
                item.checkpoint.checkpoint_id,
            ),
            reverse=True,
        )
        retained = checkpoints[:retained_checkpoint_min_count]
        stale = checkpoints[retained_checkpoint_min_count:]
        deleted: list[str] = []
        missing: list[str] = []
        for event in stale:
            artifact = event.artifact
            removed = archive.delete_if_identity(
                artifact.artifact_id,
                session_id=runtime_session_id,
                digest=artifact.content_sha256,
                media_type=artifact.media_type,
                semantic_metadata_fingerprint=(
                    artifact.semantic_metadata_fingerprint
                ),
            )
            target = deleted if removed else missing
            target.append(event.checkpoint.checkpoint_id)
        return SubagentGraphCheckpointGcReport(
            runtime_session_id=runtime_session_id,
            catalog_event_count=len(checkpoints),
            retained_checkpoint_ids=tuple(
                event.checkpoint.checkpoint_id for event in retained
            ),
            deleted_checkpoint_ids=tuple(deleted),
            already_missing_checkpoint_ids=tuple(missing),
        )
