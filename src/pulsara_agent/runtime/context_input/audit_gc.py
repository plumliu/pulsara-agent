"""Privileged bounded GC for incomplete optional context-input audits."""

from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulsara_agent.event import ContextCompiledEvent, EventType
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY, EventLog
from pulsara_agent.event_log.historical_decoder import (
    decode_raw_stored_event_envelope,
)
from pulsara_agent.primitives.context_input_audit_storage import (
    ContextInputAuditMaterializationPlanFact,
    ContextInputAuditPageFact,
)
from pulsara_agent.runtime.context_input.audit_storage import (
    ContextInputAuditArtifactIntegrityError,
    ContextInputAuditArtifactMissing,
    ContextInputAuditMaintenanceRepository,
    ContextInputAuditMaintenanceStore,
    validate_context_input_audit_plan_reference,
)
from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
    CheckpointMaintenanceAuthority,
)


class ResolvedContextInputAuditMaintenancePolicy(BaseModel):
    """Process-local bounds; intentionally absent from semantic identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incomplete_plan_retention_seconds: int = Field(default=86_400, ge=1)
    catalog_page_max_events: int = Field(default=32, ge=1, le=32)
    catalog_page_max_payload_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1,
        le=8 * 1024 * 1024,
    )
    maximum_delete_candidates_per_invocation: int = Field(
        default=4_096,
        ge=1,
        le=4_096,
    )
    completed_root_retention: str = "retained"

    @model_validator(mode="after")
    def _frozen_v1(self) -> "ResolvedContextInputAuditMaintenancePolicy":
        if self.completed_root_retention != "retained":
            raise ValueError("completed context-input audit roots must be retained")
        return self


class ContextInputAuditGcEligibility(BaseModel):
    """Caller proof that normal owners can no longer create audit artifacts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_session_id: str = Field(min_length=1)
    session_close_confirmed: bool
    run_owners_drained: bool
    context_input_io_drained: bool

    @model_validator(mode="after")
    def _closed(self) -> "ContextInputAuditGcEligibility":
        if not (
            self.session_close_confirmed
            and self.run_owners_drained
            and self.context_input_io_drained
        ):
            raise ValueError("context-input audit GC requires a fully drained session")
        return self


class ContextInputAuditGcReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_session_id: str = Field(min_length=1)
    dry_run: bool
    catalog_event_count: int = Field(ge=0)
    catalog_payload_bytes: int = Field(ge=0)
    retained_completed_root_ids: tuple[str, ...]
    retained_recent_plan_ids: tuple[str, ...]
    deletion_candidate_artifact_ids: tuple[str, ...]
    deleted_artifact_ids: tuple[str, ...]
    already_missing_artifact_ids: tuple[str, ...]
    continuation_through_sequence: int | None = Field(default=None, ge=0)


def _created_at_timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ContextInputAuditArtifactIntegrityError(
            "context-input audit plan age lacks UTC attribution"
        )
    return parsed.astimezone(timezone.utc).timestamp()


def _validate_root_join(event: ContextCompiledEvent, root) -> None:
    commit = event.semantic_commit
    expectation = event.audit_expectation
    assert commit is not None and expectation is not None
    if (
        root.source_runtime_session_id != commit.runtime_session_id
        or root.source_run_id != commit.run_id
        or root.source_context_id != commit.context_id
        or root.source_resolved_model_call_id != commit.resolved_model_call_id
        or root.semantic_commit_fingerprint != commit.commit_fingerprint
        or root.materialization_key != expectation.materialization_key
        or root.materialization_contract_fingerprint
        != expectation.audit_contract_fingerprint
        or root.root_semantic_fingerprint
        != expectation.expected_root_semantic_fingerprint
    ):
        raise ContextInputAuditArtifactIntegrityError(
            "completed context-input audit root does not join its event"
        )


def _validate_plan_join(
    event: ContextCompiledEvent,
    plan: ContextInputAuditMaterializationPlanFact,
) -> None:
    commit = event.semantic_commit
    expectation = event.audit_expectation
    assert commit is not None and expectation is not None
    if (
        plan.source_runtime_session_id != commit.runtime_session_id
        or plan.source_run_id != commit.run_id
        or plan.source_context_id != commit.context_id
        or plan.source_resolved_model_call_id != commit.resolved_model_call_id
        or plan.semantic_commit_fingerprint != commit.commit_fingerprint
        or plan.expectation_fingerprint != expectation.expectation_fingerprint
        or plan.materialization_key != expectation.materialization_key
        or plan.expected_root_artifact_id != expectation.expected_root_artifact_id
        or plan.expected_root_semantic_fingerprint
        != expectation.expected_root_semantic_fingerprint
        or plan.audit_contract_fingerprint != expectation.audit_contract_fingerprint
    ):
        raise ContextInputAuditArtifactIntegrityError(
            "incomplete context-input audit plan does not join its event"
        )


def garbage_collect_incomplete_context_input_audits(
    *,
    runtime_session_id: str,
    event_log: EventLog,
    archive: ContextInputAuditMaintenanceStore,
    maintenance_authority: CheckpointMaintenanceAuthority,
    eligibility: ContextInputAuditGcEligibility,
    policy: ResolvedContextInputAuditMaintenancePolicy | None = None,
    dry_run: bool = True,
    through_sequence: int | None = None,
    operation_timeout_seconds: float = 30.0,
) -> ContextInputAuditGcReport:
    """Delete only old plan-owned pages whose completion root is absent."""

    if runtime_session_id != eligibility.runtime_session_id:
        raise ValueError("context-input audit GC eligibility owner mismatch")
    if operation_timeout_seconds <= 0:
        raise ValueError("context-input audit GC timeout must be positive")
    resolved = policy or ResolvedContextInputAuditMaintenancePolicy()
    deadline = monotonic() + operation_timeout_seconds
    repository = ContextInputAuditMaintenanceRepository(archive)
    retained_roots: list[str] = []
    retained_recent: list[str] = []
    candidates: list[str] = []
    deleted: list[str] = []
    missing: list[str] = []
    catalog_events = 0
    catalog_bytes = 0
    continuation: int | None = None
    cursor = through_sequence
    now_timestamp = datetime.now(timezone.utc).timestamp()

    with maintenance_authority.acquire_exclusive(runtime_session_id) as permit:
        if not permit.exclusive or permit.runtime_session_id != runtime_session_id:
            raise RuntimeError("context-input audit maintenance permit mismatch")
        account = event_log.read_materialization_account_state(
            deadline_monotonic=deadline
        )
        if account is not None and (
            account.active_checkpoint_barrier is not None or account.active_reservations
        ):
            raise RuntimeError(
                "context-input audit GC requires a drained materialization account"
            )
        while monotonic() < deadline:
            rows = event_log.read_raw_events_by_type(
                EventType.CONTEXT_COMPILED.value,
                limit=resolved.catalog_page_max_events,
                through_sequence=cursor,
                deadline_monotonic=deadline,
            )
            if not rows:
                break
            page_bytes = sum(len(row.canonical_payload_bytes) for row in rows)
            if page_bytes > resolved.catalog_page_max_payload_bytes:
                raise RuntimeError("context-input audit GC catalog page exceeds 8 MiB")
            if any(len(row.canonical_payload_bytes) > 256 * 1024 for row in rows):
                raise RuntimeError(
                    "context-input audit GC observed an oversized compiled event"
                )
            catalog_events += len(rows)
            catalog_bytes += page_bytes
            stop_for_capacity = False
            for raw in rows:
                event = decode_raw_stored_event_envelope(
                    raw, DEFAULT_EVENT_SCHEMA_REGISTRY
                )
                if not isinstance(event, ContextCompiledEvent):
                    raise RuntimeError("context-input audit GC catalog type mismatch")
                if (
                    event.status != "compiled"
                    or event.semantic_commit is None
                    or event.audit_expectation is None
                ):
                    continue
                commit = event.semantic_commit
                expectation = event.audit_expectation
                if commit.runtime_session_id != runtime_session_id:
                    raise RuntimeError("context-input audit GC event owner mismatch")
                try:
                    root, _root_reference = repository.get_expected_root(
                        artifact_id=expectation.expected_root_artifact_id,
                        source_runtime_session_id=runtime_session_id,
                        source_run_id=commit.run_id,
                        deadline_monotonic=deadline,
                    )
                except ContextInputAuditArtifactMissing:
                    pass
                else:
                    _validate_root_join(event, root)
                    completed_plan = repository.get_exact(
                        reference=root.plan_artifact_reference,
                        source_runtime_session_id=runtime_session_id,
                        source_run_id=commit.run_id,
                        fact_type=ContextInputAuditMaterializationPlanFact,
                        deadline_monotonic=deadline,
                    )
                    validate_context_input_audit_plan_reference(
                        root=root,
                        plan=completed_plan,
                        expected_plan_artifact_id=(
                            expectation.expected_plan_artifact_id
                        ),
                    )
                    _validate_plan_join(event, completed_plan)
                    retained_roots.append(expectation.expected_root_artifact_id)
                    continue
                try:
                    stored_plan = repository.get_expected_plan(
                        artifact_id=expectation.expected_plan_artifact_id,
                        source_runtime_session_id=runtime_session_id,
                        source_run_id=commit.run_id,
                        deadline_monotonic=deadline,
                    )
                except ContextInputAuditArtifactMissing:
                    continue
                plan = stored_plan.fact
                if not isinstance(plan, ContextInputAuditMaterializationPlanFact):
                    raise ContextInputAuditArtifactIntegrityError(
                        "context-input audit expected plan decoded to another type"
                    )
                _validate_plan_join(event, plan)
                age_seconds = now_timestamp - _created_at_timestamp(
                    stored_plan.created_at_utc
                )
                if age_seconds < resolved.incomplete_plan_retention_seconds:
                    retained_recent.append(expectation.expected_plan_artifact_id)
                    continue
                potential_count = 1 + len(plan.page_references)
                if (
                    len(candidates) + potential_count
                    > resolved.maximum_delete_candidates_per_invocation
                ):
                    continuation = raw.sequence
                    stop_for_capacity = True
                    break
                present_page_references = []
                for page_reference in plan.page_references:
                    try:
                        repository.get_exact(
                            reference=page_reference,
                            source_runtime_session_id=runtime_session_id,
                            source_run_id=commit.run_id,
                            fact_type=ContextInputAuditPageFact,
                            deadline_monotonic=deadline,
                        )
                    except ContextInputAuditArtifactMissing:
                        missing.append(page_reference.artifact_id)
                    else:
                        present_page_references.append(page_reference)
                ordered_references = (
                    *present_page_references,
                    stored_plan.reference,
                )
                candidates.extend(item.artifact_id for item in ordered_references)
                if not dry_run:
                    for reference in ordered_references:
                        removed = repository.delete_exact(
                            reference=reference,
                            source_runtime_session_id=runtime_session_id,
                            deadline_monotonic=deadline,
                        )
                        (deleted if removed else missing).append(reference.artifact_id)
            if stop_for_capacity:
                break
            cursor = rows[-1].sequence - 1
            if cursor < 1:
                break
        else:
            continuation = cursor
        if monotonic() >= deadline and continuation is None:
            continuation = cursor

    return ContextInputAuditGcReport(
        runtime_session_id=runtime_session_id,
        dry_run=dry_run,
        catalog_event_count=catalog_events,
        catalog_payload_bytes=catalog_bytes,
        retained_completed_root_ids=tuple(dict.fromkeys(retained_roots)),
        retained_recent_plan_ids=tuple(dict.fromkeys(retained_recent)),
        deletion_candidate_artifact_ids=tuple(candidates),
        deleted_artifact_ids=tuple(deleted),
        already_missing_artifact_ids=tuple(dict.fromkeys(missing)),
        continuation_through_sequence=continuation,
    )


__all__ = [
    "ContextInputAuditGcEligibility",
    "ContextInputAuditGcReport",
    "ResolvedContextInputAuditMaintenancePolicy",
    "garbage_collect_incomplete_context_input_audits",
]
