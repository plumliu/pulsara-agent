"""Deterministic manifest and audit construction for compiler inputs."""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor, Future as ConcurrentFuture
from dataclasses import dataclass
import hashlib
from enum import StrEnum
from time import monotonic
from typing import TYPE_CHECKING, Callable, Literal
from uuid import uuid4

from pulsara_agent.runtime.blocking_executor import auxiliary_io_executor

from pulsara_agent.primitives.context import (
    ContextCompileInputAuditFact,
    ContextCompileInputManifestFact,
    ContextEventReferenceFact,
    ContextFactSnapshotFact,
    FrozenJsonObjectFact,
    PreparedContextCandidateSet,
    ProjectedToolResultCompileRefFact,
    LongHorizonContextAttributionFact,
    TranscriptCompileInput,
    TranscriptToolResultRefFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.long_horizon import (
    ContextWindowFact,
    ContextWindowProjectionState,
    LongHorizonContextAllocationPolicyFact,
    LongHorizonContextBudgetDecisionFact,
    LongHorizonProjectionPressureShadowFact,
    PreparedObservationRollupUnit,
    ProjectionTargetUnreachableAuditFact,
    RolloutBudgetStateFact,
    SubagentGraphSemanticSourceFact,
)
from pulsara_agent.primitives.tool_result import PreparedToolResultRenderInput
from pulsara_agent.runtime.context_input.render import PreparedToolResultRenderOutput

if TYPE_CHECKING:
    from pulsara_agent.memory.foundation.protocols import ArtifactStore


CONTEXT_INPUT_MANIFEST_MEDIA_TYPE = (
    "application/vnd.pulsara.context-input-manifest+json; version=2"
)


@dataclass(frozen=True, slots=True)
class ContextInputManifestWriteCandidate:
    runtime_session_id: str
    run_id: str
    context_id: str
    artifact_id: str
    canonical_bytes: bytes
    semantic_metadata: FrozenJsonObjectFact
    content_fingerprint: str
    metadata_fingerprint: str


@dataclass(frozen=True, slots=True)
class ContextInputManifestWriteResult:
    outcome: str
    artifact_id: str
    content_fingerprint: str

    def __post_init__(self) -> None:
        if self.outcome not in {"stored", "confirmed_existing"}:
            raise ValueError("invalid context input manifest write outcome")


class ContextInputManifestWriteConflict(RuntimeError):
    pass


class ContextInputManifestConfirmedAbsent(RuntimeError):
    pass


class ContextInputManifestWriteOutcomeUnknown(RuntimeError):
    pass


class ContextInputManifestWriteDeadlineExceeded(TimeoutError):
    pass


class PendingContextInputManifestWriteError(RuntimeError):
    pass


class ContextInputManifestAttemptState(StrEnum):
    PENDING = "pending"
    WRITING = "writing"
    CONFIRMING = "confirming"
    STORED = "stored"
    ABSENT = "absent"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class ContextInputManifestPhysicalDrainState(StrEnum):
    IDLE = "idle"
    DRAINING = "draining"
    DRAINED = "drained"


class ContextInputManifestPostTerminalVerificationState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    VERIFIED = "verified"
    CONSISTENCY_FAILED = "consistency_failed"
    UNKNOWN = "unknown"


class ContextInputManifestPhysicalOperationKind(StrEnum):
    WRITE = "write"
    CONFIRM = "confirm"


class ContextInputManifestPhysicalOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    EXITED = "exited"


@dataclass(slots=True)
class ContextInputManifestPhysicalOperation:
    operation_id: str
    artifact_id: str
    started_by_generation: int
    kind: ContextInputManifestPhysicalOperationKind
    state: ContextInputManifestPhysicalOperationState
    executor_future: ConcurrentFuture[object] | None
    submitted_at_monotonic: float
    deadline_monotonic: float
    exited_at_monotonic: float | None = None
    result_status: str | None = None


@dataclass(slots=True)
class PendingContextInputManifestWrite:
    candidate: ContextInputManifestWriteCandidate
    attempt_generation: int
    attempt_id: str
    logical_state: ContextInputManifestAttemptState
    physical_drain_state: ContextInputManifestPhysicalDrainState
    post_terminal_verification_state: ContextInputManifestPostTerminalVerificationState
    current_operation_id: str | None
    physical_operation_ids: set[str]
    completion: asyncio.Future[ContextInputManifestWriteResult]
    attempt_deadline_monotonic: float
    last_error_code: str | None = None
    provisional_confirmation: Literal["absent"] | None = None


class ContextInputManifestWriteService:
    """Own blocking artifact writes across waiter cancellation and retry."""

    def __init__(
        self,
        *,
        archive: ArtifactStore,
        max_pending: int = 8,
        max_physical_operations: int = 16,
        executor: Executor | None = None,
        on_consistency_failure: Callable[[], None] | None = None,
    ) -> None:
        self._archive = archive
        self._max_pending = max_pending
        self._max_physical_operations = max_physical_operations
        self._executor = executor or auxiliary_io_executor()
        self._lock = asyncio.Lock()
        self._entries: dict[str, PendingContextInputManifestWrite] = {}
        self._operations: dict[str, ContextInputManifestPhysicalOperation] = {}
        self._closed = False
        self._on_consistency_failure = on_consistency_failure

    async def persist(
        self,
        candidate: ContextInputManifestWriteCandidate,
        *,
        deadline_monotonic: float,
    ) -> ContextInputManifestWriteResult:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._closed:
                raise RuntimeError("context manifest service is closed")
            entry = self._entries.get(candidate.artifact_id)
            if entry is not None:
                if entry.candidate != candidate:
                    raise ContextInputManifestWriteConflict(
                        "same manifest artifact ID has different candidate"
                    )
            else:
                if len(self._entries) >= self._max_pending:
                    raise PendingContextInputManifestWriteError(
                        "max pending context input manifests reached"
                    )
                entry = PendingContextInputManifestWrite(
                    candidate=candidate,
                    attempt_generation=1,
                    attempt_id=f"context-manifest-attempt:{uuid4().hex}",
                    logical_state=ContextInputManifestAttemptState.PENDING,
                    physical_drain_state=(ContextInputManifestPhysicalDrainState.IDLE),
                    post_terminal_verification_state=(
                        ContextInputManifestPostTerminalVerificationState.NOT_REQUIRED
                    ),
                    current_operation_id=None,
                    physical_operation_ids=set(),
                    completion=loop.create_future(),
                    attempt_deadline_monotonic=deadline_monotonic,
                )
                self._entries[candidate.artifact_id] = entry
                self._start_operation_locked(
                    entry,
                    kind=ContextInputManifestPhysicalOperationKind.WRITE,
                    deadline_monotonic=deadline_monotonic,
                )
            completion = entry.completion
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise ContextInputManifestWriteDeadlineExceeded(
                "context input manifest waiter deadline exceeded"
            )
        try:
            return await asyncio.wait_for(asyncio.shield(completion), remaining)
        except TimeoutError as exc:
            await self._mark_waiter_deadline_unknown(candidate.artifact_id)
            raise ContextInputManifestWriteDeadlineExceeded(
                "context input manifest waiter deadline exceeded"
            ) from exc

    async def retry_confirmation(
        self,
        *,
        artifact_id: str,
        expected_generation: int,
        deadline_monotonic: float,
    ) -> ContextInputManifestWriteResult | Literal["absent", "conflict"]:
        async with self._lock:
            entry = self._entries.get(artifact_id)
            if entry is None:
                raise KeyError(artifact_id)
            if entry.attempt_generation != expected_generation:
                raise ContextInputManifestWriteOutcomeUnknown(
                    "manifest generation changed"
                )
            if entry.logical_state is ContextInputManifestAttemptState.STORED:
                return entry.completion.result()
            if entry.logical_state is ContextInputManifestAttemptState.CONFLICT:
                return "conflict"
            if entry.logical_state is ContextInputManifestAttemptState.ABSENT:
                return "absent"
            if entry.current_operation_id is None:
                entry.attempt_generation += 1
                entry.attempt_id = f"context-manifest-attempt:{uuid4().hex}"
                entry.attempt_deadline_monotonic = deadline_monotonic
                self._start_operation_locked(
                    entry,
                    kind=ContextInputManifestPhysicalOperationKind.CONFIRM,
                    deadline_monotonic=deadline_monotonic,
                )
            else:
                current = self._operations.get(entry.current_operation_id)
                if (
                    current is not None
                    and current.kind is ContextInputManifestPhysicalOperationKind.WRITE
                    and entry.logical_state is ContextInputManifestAttemptState.UNKNOWN
                ):
                    entry.current_operation_id = None
                    entry.attempt_generation += 1
                    entry.attempt_id = f"context-manifest-attempt:{uuid4().hex}"
                    entry.attempt_deadline_monotonic = deadline_monotonic
                    self._start_operation_locked(
                        entry,
                        kind=ContextInputManifestPhysicalOperationKind.CONFIRM,
                        deadline_monotonic=deadline_monotonic,
                    )
            completion = entry.completion
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise ContextInputManifestWriteDeadlineExceeded(
                "context input manifest confirmation deadline exceeded"
            )
        try:
            return await asyncio.wait_for(asyncio.shield(completion), remaining)
        except ContextInputManifestConfirmedAbsent:
            return "absent"
        except ContextInputManifestWriteConflict:
            return "conflict"
        except TimeoutError as exc:
            raise ContextInputManifestWriteDeadlineExceeded(
                "context input manifest confirmation deadline exceeded"
            ) from exc

    async def drain_pending(self, *, deadline_monotonic: float) -> None:
        while True:
            async with self._lock:
                pending = tuple(self._entries.values())
                if not pending:
                    return
                if any(
                    entry.post_terminal_verification_state
                    is ContextInputManifestPostTerminalVerificationState.CONSISTENCY_FAILED
                    for entry in pending
                ):
                    raise PendingContextInputManifestWriteError(
                        "context input manifest consistency failure requires session reconciliation"
                    )
                retry = tuple(
                    entry
                    for entry in pending
                    if entry.current_operation_id is None
                    and entry.logical_state
                    in {
                        ContextInputManifestAttemptState.UNKNOWN,
                        ContextInputManifestAttemptState.CONFIRMING,
                    }
                )
                for entry in retry:
                    entry.attempt_generation += 1
                    entry.attempt_id = f"context-manifest-attempt:{uuid4().hex}"
                    self._start_operation_locked(
                        entry,
                        kind=ContextInputManifestPhysicalOperationKind.CONFIRM,
                        deadline_monotonic=deadline_monotonic,
                    )
                futures = tuple(entry.completion for entry in pending)
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise PendingContextInputManifestWriteError(
                    "context input manifest drain deadline exceeded"
                )
            done, _ = await asyncio.wait(
                tuple(asyncio.shield(item) for item in futures),
                timeout=remaining,
            )
            if not done:
                raise PendingContextInputManifestWriteError(
                    "context input manifest drain deadline exceeded"
                )
            if all(item.done() for item in futures):
                await asyncio.sleep(0)
            async with self._lock:
                self._remove_drained_entries_locked()

    async def _mark_waiter_deadline_unknown(self, artifact_id: str) -> None:
        async with self._lock:
            entry = self._entries.get(artifact_id)
            if entry is None or entry.completion.done():
                return
            entry.logical_state = ContextInputManifestAttemptState.UNKNOWN
            entry.last_error_code = "waiter_deadline_exceeded"
            entry.post_terminal_verification_state = (
                ContextInputManifestPostTerminalVerificationState.UNKNOWN
            )

    def pending_count(self) -> int:
        return len(self._entries)

    def inflight_operation_count(self) -> int:
        return len(self._operations)

    async def aclose(self, *, deadline_monotonic: float) -> None:
        await self.drain_pending(deadline_monotonic=deadline_monotonic)
        async with self._lock:
            self._closed = True

    def close_if_idle(self) -> None:
        if self._entries or self._operations:
            raise PendingContextInputManifestWriteError(
                "context input manifest service still owns pending operations"
            )
        self._closed = True

    def _start_operation_locked(
        self,
        entry: PendingContextInputManifestWrite,
        *,
        kind: ContextInputManifestPhysicalOperationKind,
        deadline_monotonic: float,
    ) -> None:
        if len(self._operations) >= self._max_physical_operations:
            raise PendingContextInputManifestWriteError(
                "max context input manifest physical operations reached"
            )
        if entry.current_operation_id is not None:
            raise RuntimeError("manifest attempt already has an active operation")
        loop = asyncio.get_running_loop()
        operation_id = f"context-manifest-operation:{uuid4().hex}"
        operation = ContextInputManifestPhysicalOperation(
            operation_id=operation_id,
            artifact_id=entry.candidate.artifact_id,
            started_by_generation=entry.attempt_generation,
            kind=kind,
            state=ContextInputManifestPhysicalOperationState.QUEUED,
            executor_future=None,
            submitted_at_monotonic=monotonic(),
            deadline_monotonic=deadline_monotonic,
        )
        self._operations[operation_id] = operation
        entry.physical_operation_ids.add(operation_id)
        entry.current_operation_id = operation_id
        entry.physical_drain_state = ContextInputManifestPhysicalDrainState.DRAINING
        entry.logical_state = (
            ContextInputManifestAttemptState.WRITING
            if kind is ContextInputManifestPhysicalOperationKind.WRITE
            else ContextInputManifestAttemptState.CONFIRMING
        )
        try:
            future = self._executor.submit(
                self._run_write
                if kind is ContextInputManifestPhysicalOperationKind.WRITE
                else self._run_confirm,
                entry.candidate,
                deadline_monotonic,
            )
        except BaseException:
            operation.state = ContextInputManifestPhysicalOperationState.EXITED
            operation.exited_at_monotonic = monotonic()
            entry.current_operation_id = None
            entry.physical_operation_ids.discard(operation_id)
            self._operations.pop(operation_id, None)
            entry.logical_state = ContextInputManifestAttemptState.UNKNOWN
            entry.physical_drain_state = ContextInputManifestPhysicalDrainState.DRAINED
            raise
        operation.executor_future = future
        operation.state = ContextInputManifestPhysicalOperationState.RUNNING
        future.add_done_callback(
            lambda done, oid=operation_id, owner_loop=loop: (
                owner_loop.call_soon_threadsafe(
                    self._schedule_operation_finalizer, oid, done
                )
            )
        )

    def _schedule_operation_finalizer(
        self, operation_id: str, future: ConcurrentFuture[object]
    ) -> None:
        asyncio.create_task(self._finalize_operation(operation_id, future))

    async def _finalize_operation(
        self, operation_id: str, future: ConcurrentFuture[object]
    ) -> None:
        try:
            result = future.result()
            error: BaseException | None = None
        except BaseException as exc:
            result = None
            error = exc
        async with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                return
            entry = self._entries.get(operation.artifact_id)
            operation.state = ContextInputManifestPhysicalOperationState.EXITED
            operation.exited_at_monotonic = monotonic()
            if entry is None:
                self._operations.pop(operation_id, None)
                return
            operation.result_status = _operation_result_status(result, error)
            entry.physical_operation_ids.discard(operation_id)
            if entry.current_operation_id == operation_id:
                entry.current_operation_id = None
            self._operations.pop(operation_id, None)
            if not entry.physical_operation_ids:
                entry.physical_drain_state = (
                    ContextInputManifestPhysicalDrainState.DRAINED
                )
            if operation.started_by_generation != entry.attempt_generation:
                if not entry.physical_operation_ids:
                    entry.post_terminal_verification_state = (
                        ContextInputManifestPostTerminalVerificationState.PENDING
                    )
                    entry.attempt_generation += 1
                    entry.attempt_id = f"context-manifest-attempt:{uuid4().hex}"
                    entry.attempt_deadline_monotonic = max(
                        entry.attempt_deadline_monotonic,
                        monotonic() + 1.0,
                    )
                    self._start_operation_locked(
                        entry,
                        kind=ContextInputManifestPhysicalOperationKind.CONFIRM,
                        deadline_monotonic=entry.attempt_deadline_monotonic,
                    )
                return
            await self._apply_operation_result_locked(
                entry,
                operation=operation,
                result=result,
                error=error,
            )
            self._remove_drained_entries_locked()

    async def _apply_operation_result_locked(
        self,
        entry: PendingContextInputManifestWrite,
        *,
        operation: ContextInputManifestPhysicalOperation,
        result: object,
        error: BaseException | None,
    ) -> None:
        from pulsara_agent.memory.foundation.records import ArtifactContentConflict

        if operation.kind is ContextInputManifestPhysicalOperationKind.WRITE:
            if error is None:
                status = getattr(result, "status", None)
                outcome = "stored" if status == "inserted" else "confirmed_existing"
                self._complete_stored_locked(entry, outcome=outcome)
                return
            if isinstance(error, ArtifactContentConflict):
                self._complete_conflict_locked(entry, error)
                return
            entry.last_error_code = type(error).__name__
            entry.logical_state = ContextInputManifestAttemptState.CONFIRMING
            entry.attempt_generation += 1
            entry.attempt_id = f"context-manifest-attempt:{uuid4().hex}"
            self._start_operation_locked(
                entry,
                kind=ContextInputManifestPhysicalOperationKind.CONFIRM,
                deadline_monotonic=entry.attempt_deadline_monotonic,
            )
            return
        if error is not None:
            entry.last_error_code = type(error).__name__
            entry.post_terminal_verification_state = (
                ContextInputManifestPostTerminalVerificationState.UNKNOWN
            )
            if entry.completion.done():
                self._latch_consistency_failure_locked(entry)
            else:
                entry.logical_state = ContextInputManifestAttemptState.UNKNOWN
            return
        if result == "identical":
            if entry.completion.done():
                entry.post_terminal_verification_state = (
                    ContextInputManifestPostTerminalVerificationState.VERIFIED
                )
                return
            self._complete_stored_locked(entry, outcome="confirmed_existing")
            return
        if result == "conflict":
            if entry.completion.done():
                self._latch_consistency_failure_locked(entry)
                return
            self._complete_conflict_locked(
                entry,
                ContextInputManifestWriteConflict(
                    "stored context input manifest differs from candidate"
                ),
            )
            return
        if result == "absent":
            if self._has_inflight_write_locked(entry.candidate.artifact_id):
                entry.provisional_confirmation = "absent"
                entry.logical_state = ContextInputManifestAttemptState.CONFIRMING
                return
            if entry.completion.done():
                self._latch_consistency_failure_locked(entry)
                return
            entry.logical_state = ContextInputManifestAttemptState.ABSENT
            entry.post_terminal_verification_state = (
                ContextInputManifestPostTerminalVerificationState.VERIFIED
            )
            if not entry.completion.done():
                entry.completion.set_exception(
                    ContextInputManifestConfirmedAbsent(
                        "context input manifest is confirmed absent"
                    )
                )

    def _complete_stored_locked(
        self,
        entry: PendingContextInputManifestWrite,
        *,
        outcome: Literal["stored", "confirmed_existing"],
    ) -> None:
        entry.logical_state = ContextInputManifestAttemptState.STORED
        entry.provisional_confirmation = None
        entry.post_terminal_verification_state = (
            ContextInputManifestPostTerminalVerificationState.VERIFIED
            if not entry.physical_operation_ids
            else ContextInputManifestPostTerminalVerificationState.PENDING
        )
        if not entry.completion.done():
            entry.completion.set_result(
                ContextInputManifestWriteResult(
                    outcome=outcome,
                    artifact_id=entry.candidate.artifact_id,
                    content_fingerprint=entry.candidate.content_fingerprint,
                )
            )

    def _complete_conflict_locked(
        self,
        entry: PendingContextInputManifestWrite,
        error: BaseException,
    ) -> None:
        entry.logical_state = ContextInputManifestAttemptState.CONFLICT
        entry.post_terminal_verification_state = (
            ContextInputManifestPostTerminalVerificationState.CONSISTENCY_FAILED
        )
        if not entry.completion.done():
            entry.completion.set_exception(
                ContextInputManifestWriteConflict(str(error))
            )

    def _latch_consistency_failure_locked(
        self, entry: PendingContextInputManifestWrite
    ) -> None:
        entry.post_terminal_verification_state = (
            ContextInputManifestPostTerminalVerificationState.CONSISTENCY_FAILED
        )
        self._notify_consistency_failure_locked()

    def _notify_consistency_failure_locked(self) -> None:
        callback = self._on_consistency_failure
        if callback is not None:
            callback()

    def _has_inflight_write_locked(self, artifact_id: str) -> bool:
        return any(
            operation.artifact_id == artifact_id
            and operation.kind is ContextInputManifestPhysicalOperationKind.WRITE
            and operation.state is not ContextInputManifestPhysicalOperationState.EXITED
            for operation in self._operations.values()
        )

    def _remove_drained_entries_locked(self) -> None:
        removable = tuple(
            artifact_id
            for artifact_id, entry in self._entries.items()
            if entry.completion.done()
            and not entry.physical_operation_ids
            and entry.physical_drain_state
            is ContextInputManifestPhysicalDrainState.DRAINED
            and entry.post_terminal_verification_state
            not in {
                ContextInputManifestPostTerminalVerificationState.PENDING,
                ContextInputManifestPostTerminalVerificationState.UNKNOWN,
                ContextInputManifestPostTerminalVerificationState.CONSISTENCY_FAILED,
            }
        )
        for artifact_id in removable:
            self._entries.pop(artifact_id, None)

    def _run_write(
        self,
        candidate: ContextInputManifestWriteCandidate,
        deadline_monotonic: float,
    ) -> object:
        return self._archive.put_text_if_absent_or_confirm_identical(
            candidate.artifact_id,
            candidate.canonical_bytes.decode("utf-8"),
            session_id=candidate.runtime_session_id,
            run_id=candidate.run_id,
            media_type=CONTEXT_INPUT_MANIFEST_MEDIA_TYPE,
            semantic_metadata=thaw_json(candidate.semantic_metadata),
            deadline_monotonic=deadline_monotonic,
        )

    def _run_confirm(
        self,
        candidate: ContextInputManifestWriteCandidate,
        deadline_monotonic: float,
    ) -> str:
        try:
            info = self._archive.get_info(
                candidate.artifact_id,
                session_id=candidate.runtime_session_id,
                deadline_monotonic=deadline_monotonic,
            )
            text = self._archive.get_text(
                candidate.artifact_id,
                session_id=candidate.runtime_session_id,
                deadline_monotonic=deadline_monotonic,
            )
        except KeyError:
            return "absent"
        actual_metadata = freeze_json(info.metadata or {})
        if (
            text.encode("utf-8") == candidate.canonical_bytes
            and info.media_type == CONTEXT_INPUT_MANIFEST_MEDIA_TYPE
            and actual_metadata == candidate.semantic_metadata
        ):
            return "identical"
        return "conflict"


def _operation_result_status(result: object, error: BaseException | None) -> str:
    if error is not None:
        return f"error:{type(error).__name__}"
    return str(getattr(result, "status", result))


def build_context_input_manifest(
    *,
    snapshot: ContextFactSnapshotFact,
    transcript: TranscriptCompileInput,
    prepared_tool_results: PreparedToolResultRenderInput,
    rendered_tool_results: PreparedToolResultRenderOutput,
    active_window: ContextWindowFact,
    window_policy: LongHorizonContextAllocationPolicyFact,
    projection_state: ContextWindowProjectionState,
    prepared_rollups: tuple[PreparedObservationRollupUnit, ...],
    rollout_state: RolloutBudgetStateFact,
    context_budget_decision: LongHorizonContextBudgetDecisionFact,
    projection_pressure_shadow: LongHorizonProjectionPressureShadowFact,
    projection_target_unreachable: ProjectionTargetUnreachableAuditFact | None,
    safe_point_revision: int,
    prepared_candidates: PreparedContextCandidateSet,
) -> ContextCompileInputManifestFact:
    """Build the exact event-safe input aggregate consumed by one compile."""

    if prepared_tool_results.resolved_policy.basis != (
        snapshot.compile_policy.tool_result_basis
    ):
        raise ValueError("manifest render policy differs from snapshot policy")
    if prepared_candidates.policy != snapshot.compile_policy.candidate_collection:
        raise ValueError("manifest candidate policy differs from snapshot policy")
    units_fingerprint = context_fingerprint(
        "tool-result-units:v1",
        tuple(unit.unit_fingerprint for unit in prepared_tool_results.units),
    )
    projected_refs = build_projected_tool_result_compile_refs(
        transcript=transcript,
        rendered_tool_results=rendered_tool_results,
        projection_state=projection_state,
    )
    aggregate = context_fingerprint(
        "context-compile-input-aggregate:v2",
        [
            snapshot.snapshot_semantic_fingerprint,
            transcript.transcript_fingerprint,
            prepared_tool_results.render_input_fingerprint,
            prepared_candidates.candidate_set_fingerprint,
            active_window.window_semantic_fingerprint,
            window_policy.policy_fingerprint,
            projection_state.state_semantic_fingerprint,
            tuple(item.prepared_fingerprint for item in prepared_rollups),
            rollout_state.state_fingerprint,
            context_budget_decision.decision_fingerprint,
            (
                projection_target_unreachable.audit_fingerprint
                if projection_target_unreachable is not None
                else None
            ),
            snapshot.identity.compiler_contract_version,
        ],
    )
    payload = {
        "schema_version": "context-input-manifest:v2",
        "input_aggregate_fingerprint": aggregate,
        "snapshot": snapshot,
        "subagent_graph_semantic_source": (
            snapshot.subagent_graph_semantic_source
        ),
        "subagent_graph_acceleration": snapshot.subagent_graph_acceleration,
        "prepared_candidate_set": prepared_candidates,
        "transcript_fingerprint": transcript.transcript_fingerprint,
        "tool_result_units_fingerprint": units_fingerprint,
        "tool_result_render_policy": prepared_tool_results.resolved_policy,
        "tool_result_render_input_fingerprint": (
            prepared_tool_results.render_input_fingerprint
        ),
        "active_window": active_window,
        "window_policy": window_policy,
        "projection_state": projection_state,
        "projected_tool_result_refs": projected_refs,
        "prepared_rollup_units": prepared_rollups,
        "rollout_state": rollout_state,
        "context_budget_decision": context_budget_decision,
        "projection_pressure_shadow": projection_pressure_shadow,
        "projection_target_unreachable": projection_target_unreachable,
        "safe_point_revision": safe_point_revision,
        "compiler_contract_version": snapshot.identity.compiler_contract_version,
    }
    return ContextCompileInputManifestFact.from_trusted_factory_payload(
        payload
    )


def build_long_horizon_context_attribution(
    *,
    run_contract_fingerprint: str,
    active_window: ContextWindowFact,
    projection_state: ContextWindowProjectionState,
    projection_rewrite_event_refs: tuple[ContextEventReferenceFact, ...],
    rollout_account_owner_runtime_session_id: str,
    rollout_state: RolloutBudgetStateFact,
    subagent_graph_semantic_source: SubagentGraphSemanticSourceFact,
    context_budget_decision: LongHorizonContextBudgetDecisionFact,
) -> LongHorizonContextAttributionFact:
    payload = {
        "schema_version": "long-horizon-context-attribution:v1",
        "run_contract_fingerprint": run_contract_fingerprint,
        "window_id": active_window.window_id,
        "window_generation": active_window.generation,
        "window_semantic_fingerprint": active_window.window_semantic_fingerprint,
        "projection_generation": projection_state.projection_generation,
        "projection_state_fingerprint": (
            projection_state.state_semantic_fingerprint
        ),
        "projection_rewrite_event_refs": projection_rewrite_event_refs,
        "rollout_account_id": rollout_state.account_id,
        "rollout_account_owner_runtime_session_id": (
            rollout_account_owner_runtime_session_id
        ),
        "rollout_state_through_sequence": rollout_state.through_sequence,
        "rollout_phase": rollout_state.phase,
        "rollout_state_fingerprint": rollout_state.state_fingerprint,
        "subagent_graph_semantic_source": subagent_graph_semantic_source,
        "budget_decision": context_budget_decision,
        "summary_artifact_id": active_window.source_summary_artifact_id,
        "summary_content_sha256": active_window.source_summary_fingerprint,
    }
    return LongHorizonContextAttributionFact(
        **payload,
        attribution_fingerprint=context_fingerprint(
            "long-horizon-context-attribution:v1", payload
        ),
    )


def build_projected_tool_result_compile_refs(
    *,
    transcript: TranscriptCompileInput,
    rendered_tool_results: PreparedToolResultRenderOutput,
    projection_state: ContextWindowProjectionState,
) -> tuple[ProjectedToolResultCompileRefFact, ...]:
    projections = {item.unit_id: item for item in projection_state.unit_projections}
    fragments = {item.unit_id: item for item in rendered_tool_results.fragments}
    if len(projections) != len(projection_state.unit_projections):
        raise ValueError("projection state contains duplicate unit IDs")
    if len(fragments) != len(rendered_tool_results.fragments):
        raise ValueError("rendered output contains duplicate unit IDs")
    refs: list[ProjectedToolResultCompileRefFact] = []
    for message in transcript.messages:
        for block_index, block in enumerate(message.blocks):
            if not isinstance(block, TranscriptToolResultRefFact):
                continue
            projection = projections.get(block.tool_result_unit_id)
            fragment = fragments.get(block.tool_result_unit_id)
            if projection is None or fragment is None:
                raise ValueError("projected tool-result ref lacks projection or fragment")
            if (
                projection.tool_call_id != block.tool_call_id
                or fragment.tool_call_id != block.tool_call_id
                or fragment.source_message_id != message.message_id
                or fragment.content_block_index != block_index
                or fragment.rendered_text_fingerprint
                != projection.rendered_fragment_fingerprint
            ):
                raise ValueError("projected tool-result ref identity mismatch")
            refs.append(
                ProjectedToolResultCompileRefFact(
                    transcript_message_id=message.message_id,
                    block_index=block_index,
                    tool_call_id=block.tool_call_id,
                    tool_result_unit_id=block.tool_result_unit_id,
                    window_id=projection.window_id,
                    projection_generation=projection.projection_generation,
                    projected_fragment_fingerprint=(
                        fragment.rendered_text_fingerprint
                    ),
                    representation=projection.representation,
                    rollup_id=projection.source_rollup_id,
                )
            )
    ref_ids = tuple(item.tool_result_unit_id for item in refs)
    if len(ref_ids) != len(set(ref_ids)) or set(ref_ids) != set(projections):
        raise ValueError("projection state differs from transcript result units")
    return tuple(refs)


def build_context_input_manifest_candidate(
    manifest: ContextCompileInputManifestFact,
) -> ContextInputManifestWriteCandidate:
    canonical_bytes = canonical_json_bytes(manifest)
    max_chars = (
        manifest.snapshot.compile_policy.candidate_collection.max_input_manifest_chars
    )
    if len(canonical_bytes) > max_chars:
        raise ValueError(
            "context input manifest exceeds max_input_manifest_chars: "
            f"{len(canonical_bytes)} > {max_chars}"
        )
    content_fingerprint = "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()
    artifact_digest = hashlib.sha256(
        canonical_json_bytes(
            [manifest.schema_version, manifest.input_aggregate_fingerprint]
        )
    ).hexdigest()
    artifact_id = f"context-input-manifest:{artifact_digest}"
    identity = manifest.snapshot.identity
    metadata_payload = {
        "artifact_kind": "context_input_manifest",
        "schema_version": manifest.schema_version,
        "runtime_session_id": identity.runtime_session_id,
        "run_id": identity.run_id,
        "context_id": identity.context_id,
        "resolved_model_call_id": (
            manifest.snapshot.resolved_model_call.resolved_model_call_id
        ),
        "model_call_index": identity.model_call_index,
        "compile_attempt_index": identity.compile_attempt_index,
        "context_retry_index": identity.context_retry_index,
        "compiler_contract_version": manifest.compiler_contract_version,
        "input_aggregate_fingerprint": manifest.input_aggregate_fingerprint,
        "manifest_fingerprint": manifest.manifest_fingerprint,
        "content_fingerprint": content_fingerprint,
    }
    metadata = freeze_json(metadata_payload)
    if not isinstance(metadata, FrozenJsonObjectFact):
        raise TypeError("context input manifest metadata must be a JSON object")
    return ContextInputManifestWriteCandidate(
        runtime_session_id=identity.runtime_session_id,
        run_id=identity.run_id,
        context_id=identity.context_id,
        artifact_id=artifact_id,
        canonical_bytes=canonical_bytes,
        semantic_metadata=metadata,
        content_fingerprint=content_fingerprint,
        metadata_fingerprint=context_fingerprint(
            "context-input-manifest-metadata:v2", metadata
        ),
    )


def build_context_compile_input_audit(
    *,
    manifest: ContextCompileInputManifestFact,
    candidate: ContextInputManifestWriteCandidate,
    write_result: ContextInputManifestWriteResult,
    transcript_message_count: int,
    transcript_pair_count: int,
    tool_result_unit_count: int,
) -> ContextCompileInputAuditFact:
    """Create a full audit only after a stored/identical acknowledgement."""

    if write_result.artifact_id != candidate.artifact_id:
        raise ValueError("manifest acknowledgement artifact ID mismatch")
    if write_result.content_fingerprint != candidate.content_fingerprint:
        raise ValueError("manifest acknowledgement content mismatch")
    snapshot = manifest.snapshot
    continuation = snapshot.continuation
    return ContextCompileInputAuditFact(
        snapshot_id=snapshot.identity.snapshot_id,
        snapshot_semantic_fingerprint=snapshot.snapshot_semantic_fingerprint,
        snapshot_fact_fingerprint=snapshot.snapshot_fact_fingerprint,
        snapshot_schema_version="context-snapshot:v2",
        compiler_contract_version=manifest.compiler_contract_version,
        source_runtime_session_id=snapshot.identity.runtime_session_id,
        authority_from_sequence=(snapshot.authority_slice_plan.authority_from_sequence),
        source_through_sequence=snapshot.identity.source_through_sequence,
        authority_slice_plan_fingerprint=(
            snapshot.authority_slice_plan.plan_fingerprint
        ),
        transcript_projection_window_fingerprint=(
            snapshot.authority_slice_plan.transcript_window.window_fingerprint
        ),
        run_start_event_id=snapshot.run_entry.run_start.event_id,
        run_start_sequence=snapshot.run_entry.run_start.sequence,
        continuation_event_id=(
            continuation.resume_boundary.event_id if continuation is not None else None
        ),
        continuation_sequence=(
            continuation.resume_boundary.sequence if continuation is not None else None
        ),
        continuation_count=snapshot.continuation_count,
        resolved_model_call_id=(snapshot.resolved_model_call.resolved_model_call_id),
        model_call_index=snapshot.identity.model_call_index,
        compile_attempt_index=snapshot.identity.compile_attempt_index,
        context_retry_index=snapshot.identity.context_retry_index,
        transcript_fingerprint=manifest.transcript_fingerprint,
        transcript_message_count=transcript_message_count,
        transcript_pair_count=transcript_pair_count,
        tool_result_units_fingerprint=manifest.tool_result_units_fingerprint,
        tool_result_unit_count=tool_result_unit_count,
        tool_result_render_policy_fingerprint=(
            manifest.tool_result_render_policy.policy_fingerprint
        ),
        tool_result_render_input_fingerprint=(
            manifest.tool_result_render_input_fingerprint
        ),
        prepared_candidate_set_fingerprint=(
            manifest.prepared_candidate_set.candidate_set_fingerprint
        ),
        section_candidate_count=len(manifest.prepared_candidate_set.entries),
        input_aggregate_fingerprint=manifest.input_aggregate_fingerprint,
        input_manifest_artifact_id=candidate.artifact_id,
        input_manifest_fingerprint=manifest.manifest_fingerprint,
        long_horizon_attribution_fingerprint=(
            snapshot.long_horizon_attribution.attribution_fingerprint
        ),
        input_manifest_write_outcome=write_result.outcome,
    )


__all__ = [
    "CONTEXT_INPUT_MANIFEST_MEDIA_TYPE",
    "ContextInputManifestAttemptState",
    "ContextInputManifestConfirmedAbsent",
    "ContextInputManifestPhysicalDrainState",
    "ContextInputManifestPhysicalOperation",
    "ContextInputManifestPhysicalOperationKind",
    "ContextInputManifestPhysicalOperationState",
    "ContextInputManifestPostTerminalVerificationState",
    "ContextInputManifestWriteCandidate",
    "ContextInputManifestWriteConflict",
    "ContextInputManifestWriteDeadlineExceeded",
    "ContextInputManifestWriteOutcomeUnknown",
    "ContextInputManifestWriteResult",
    "ContextInputManifestWriteService",
    "PendingContextInputManifestWrite",
    "PendingContextInputManifestWriteError",
    "build_context_compile_input_audit",
    "build_context_input_manifest",
    "build_context_input_manifest_candidate",
]
