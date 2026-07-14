"""Privileged, offline subagent graph checkpoint verification and repair."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pulsara_agent.event_log import EventIdConflict, EventLog
from pulsara_agent.memory.foundation.records import ArtifactContentConflict
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives.long_horizon import (
    LongHorizonDiagnosticFact,
    LongHorizonPreparationStage,
    SubagentGraphReducerContractFact,
)
from pulsara_agent.runtime.long_horizon.checkpoint import (
    SubagentGraphCheckpointLedgerUntrusted,
    prepare_subagent_graph_checkpoint,
)
from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
    CheckpointMaintenanceAuthority,
)
from pulsara_agent.runtime.long_horizon.reducer_contract import (
    SubagentGraphReducerContractMismatch,
    SubagentGraphReducerRegistry,
)


class SubagentGraphCheckpointRepairOutcome(StrEnum):
    VERIFIED = "verified"
    REBUILT = "rebuilt"
    REDUCER_BINDING_UNAVAILABLE = "reducer_binding_unavailable"
    LEDGER_UNTRUSTED = "ledger_untrusted"
    ARTIFACT_CONFLICT = "artifact_conflict"


class SubagentGraphCheckpointRepairReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_session_id: str = Field(min_length=1)
    through_sequence: int = Field(ge=1)
    graph_reducer_id: str = Field(min_length=1)
    graph_reducer_version: str = Field(min_length=1)
    graph_reducer_contract_fingerprint: str = Field(min_length=1)
    graph_event_count: int | None = Field(default=None, ge=0)
    graph_semantic_accumulator: str | None = None
    ledger_continuity_accumulator: str | None = None
    graph_state_semantic_fingerprint: str | None = None
    checkpoint_id: str | None = None
    checkpoint_artifact_id: str | None = None
    scanned_event_count: int = Field(ge=0)
    first_inconsistent_sequence: int | None = Field(default=None, ge=1)
    outcome: SubagentGraphCheckpointRepairOutcome
    diagnostics: tuple[LongHorizonDiagnosticFact, ...] = ()


def verify_or_rebuild_subagent_graph_checkpoint(
    *,
    runtime_session_id: str,
    through_sequence: int,
    reducer_contract: SubagentGraphReducerContractFact,
    mode: Literal["verify", "rebuild"],
    event_log: EventLog,
    archive: ArtifactStore,
    reducer_registry: SubagentGraphReducerRegistry,
    maintenance_authority: CheckpointMaintenanceAuthority,
) -> SubagentGraphCheckpointRepairReport:
    """Full-fold one closed ledger under an exclusive maintenance permit."""

    if mode not in {"verify", "rebuild"}:
        raise ValueError("checkpoint doctor mode is invalid")
    with maintenance_authority.acquire_exclusive(runtime_session_id) as permit:
        if (
            not permit.exclusive
            or permit.runtime_session_id != runtime_session_id
        ):
            raise RuntimeError("checkpoint maintenance permit identity mismatch")
        try:
            binding = reducer_registry.resolve_binding(
                reducer_id=reducer_contract.graph_reducer_id,
                reducer_version=reducer_contract.graph_reducer_version,
                reducer_contract_fingerprint=(
                    reducer_contract.graph_reducer_contract_fingerprint
                ),
            )
        except SubagentGraphReducerContractMismatch:
            return _empty_report(
                runtime_session_id=runtime_session_id,
                through_sequence=through_sequence,
                reducer_contract=reducer_contract,
                outcome=(
                    SubagentGraphCheckpointRepairOutcome.REDUCER_BINDING_UNAVAILABLE
                ),
                code="subagent_graph_reducer_binding_unavailable",
            )
        try:
            raw = event_log.read_raw_range_snapshot(
                minimum_sequence=1,
                through_sequence=through_sequence,
                deadline_monotonic=None,
            )
            prepared = prepare_subagent_graph_checkpoint(
                runtime_session_id=runtime_session_id,
                prefix_events=raw.events,
                reducer_binding=binding,
            )
        except (ValueError, SubagentGraphCheckpointLedgerUntrusted) as exc:
            return _empty_report(
                runtime_session_id=runtime_session_id,
                through_sequence=through_sequence,
                reducer_contract=reducer_contract,
                outcome=SubagentGraphCheckpointRepairOutcome.LEDGER_UNTRUSTED,
                code="subagent_graph_checkpoint_ledger_untrusted",
                message=type(exc).__name__,
                scanned_event_count=0,
            )

        if mode == "rebuild":
            artifact = prepared.artifact
            try:
                archive.put_text_if_absent_or_confirm_identical(
                    artifact.artifact_id,
                    prepared.artifact_payload_bytes.decode("utf-8"),
                    session_id=runtime_session_id,
                    run_id=None,
                    media_type=artifact.media_type,
                    semantic_metadata={
                        "artifact_kind": "subagent_graph_checkpoint",
                        "checkpoint_id": prepared.checkpoint.checkpoint_id,
                        "content_sha256": artifact.content_sha256,
                        "semantic_metadata_fingerprint": (
                            artifact.semantic_metadata_fingerprint
                        ),
                    },
                    deadline_monotonic=None,
                )
                stored = event_log.append(prepared.event)
                confirmation = event_log.confirm_batch((prepared.event,))
                confirmed = next(
                    (
                        event
                        for event in confirmation.committed_events
                        if event.id == prepared.event.id
                    ),
                    None,
                )
                if stored.sequence is None or confirmed is None:
                    raise RuntimeError("offline checkpoint commit was not confirmed")
            except (ArtifactContentConflict, EventIdConflict, RuntimeError) as exc:
                return _report_for_prepared(
                    runtime_session_id=runtime_session_id,
                    through_sequence=through_sequence,
                    reducer_contract=reducer_contract,
                    prepared=prepared,
                    scanned_event_count=len(raw.events),
                    outcome=SubagentGraphCheckpointRepairOutcome.ARTIFACT_CONFLICT,
                    diagnostics=(
                        _diagnostic(
                            "subagent_graph_checkpoint_rebuild_conflict",
                            type(exc).__name__,
                        ),
                    ),
                )

        return _report_for_prepared(
            runtime_session_id=runtime_session_id,
            through_sequence=through_sequence,
            reducer_contract=reducer_contract,
            prepared=prepared,
            scanned_event_count=len(raw.events),
            outcome=(
                SubagentGraphCheckpointRepairOutcome.REBUILT
                if mode == "rebuild"
                else SubagentGraphCheckpointRepairOutcome.VERIFIED
            ),
        )


def _report_for_prepared(
    *,
    runtime_session_id: str,
    through_sequence: int,
    reducer_contract: SubagentGraphReducerContractFact,
    prepared,
    scanned_event_count: int,
    outcome: SubagentGraphCheckpointRepairOutcome,
    diagnostics: tuple[LongHorizonDiagnosticFact, ...] = (),
) -> SubagentGraphCheckpointRepairReport:
    checkpoint = prepared.checkpoint
    return SubagentGraphCheckpointRepairReport(
        runtime_session_id=runtime_session_id,
        through_sequence=through_sequence,
        graph_reducer_id=reducer_contract.graph_reducer_id,
        graph_reducer_version=reducer_contract.graph_reducer_version,
        graph_reducer_contract_fingerprint=(
            reducer_contract.graph_reducer_contract_fingerprint
        ),
        graph_event_count=checkpoint.graph_event_count,
        graph_semantic_accumulator=checkpoint.graph_semantic_accumulator,
        ledger_continuity_accumulator=checkpoint.ledger_continuity_accumulator,
        graph_state_semantic_fingerprint=(
            checkpoint.graph_state_semantic_fingerprint
        ),
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_artifact_id=prepared.artifact.artifact_id,
        scanned_event_count=scanned_event_count,
        first_inconsistent_sequence=None,
        outcome=outcome,
        diagnostics=diagnostics,
    )


def _empty_report(
    *,
    runtime_session_id: str,
    through_sequence: int,
    reducer_contract: SubagentGraphReducerContractFact,
    outcome: SubagentGraphCheckpointRepairOutcome,
    code: str,
    message: str = "checkpoint repair could not establish a trusted graph",
    scanned_event_count: int = 0,
) -> SubagentGraphCheckpointRepairReport:
    return SubagentGraphCheckpointRepairReport(
        runtime_session_id=runtime_session_id,
        through_sequence=through_sequence,
        graph_reducer_id=reducer_contract.graph_reducer_id,
        graph_reducer_version=reducer_contract.graph_reducer_version,
        graph_reducer_contract_fingerprint=(
            reducer_contract.graph_reducer_contract_fingerprint
        ),
        scanned_event_count=scanned_event_count,
        outcome=outcome,
        diagnostics=(_diagnostic(code, message),),
    )


def _diagnostic(code: str, message: str) -> LongHorizonDiagnosticFact:
    return LongHorizonDiagnosticFact(
        code=code,
        message=message,
        stage=LongHorizonPreparationStage.CHECKPOINT_RESTORE,
        attributes=(),
    )
