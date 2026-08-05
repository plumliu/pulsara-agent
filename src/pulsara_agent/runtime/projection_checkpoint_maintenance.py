"""Session-owned acceleration checkpoint maintenance for committed reducers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from time import monotonic
from typing import Callable, Literal

from pulsara_agent.event import AgentEvent
from pulsara_agent.ports.stored_event import (
    JoinedRawStoredEventRangeProof,
    StoredEventBatchCommitReceipt,
)

from pulsara_agent.blocking_executor import projection_maintenance_executor
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.stored_event import (
    CanonicalJsonObjectCarrier,
    RawRuntimeProjectionCheckpoint,
    build_raw_runtime_projection_checkpoint,
)


CHECKPOINT_RETRY_BASE_SECONDS = 0.025
CHECKPOINT_RETRY_MAX_SECONDS = 0.5
CHECKPOINT_PHYSICAL_DEADLINE_SECONDS = 10.0
CHECKPOINT_RECOVERY_SOFT_EVENTS = 2_048
CHECKPOINT_RECOVERY_HARD_EVENTS = 4_096
CHECKPOINT_RECOVERY_SOFT_BYTES = 8 * 1024 * 1024
CHECKPOINT_RECOVERY_HARD_BYTES = 16 * 1024 * 1024
CHECKPOINT_RECOVERY_PAGE_EVENTS = 256


class RuntimeProjectionRecoveryBoundExceeded(RuntimeError):
    """A contiguous online projection-recovery suffix exceeded its hard cap."""


def read_bounded_runtime_projection_recovery_delta(
    event_log,
    *,
    from_sequence_exclusive: int,
    through_sequence: int,
    deadline_monotonic: float | None,
) -> tuple[tuple[AgentEvent, ...], int, int]:
    """Read one exact contiguous recovery suffix in bounded joined pages."""

    if from_sequence_exclusive < 0 or through_sequence < from_sequence_exclusive:
        raise ValueError("runtime projection recovery range is invalid")
    current = from_sequence_exclusive
    events: list[AgentEvent] = []
    payload_bytes = 0
    while current < through_sequence:
        remaining_events = CHECKPOINT_RECOVERY_HARD_EVENTS - len(events)
        remaining_bytes = CHECKPOINT_RECOVERY_HARD_BYTES - payload_bytes
        if remaining_events <= 0 or remaining_bytes <= 0:
            raise RuntimeProjectionRecoveryBoundExceeded(
                "runtime projection recovery suffix exceeds its online bound"
            )
        page_end = min(
            through_sequence,
            current
            + min(CHECKPOINT_RECOVERY_PAGE_EVENTS, remaining_events),
        )
        try:
            proof = event_log.read_joined_raw_range(
                source_kind="repair",
                from_sequence_exclusive=current,
                through_sequence=page_end,
                max_events=min(CHECKPOINT_RECOVERY_PAGE_EVENTS, remaining_events),
                max_payload_bytes=remaining_bytes,
                deadline_monotonic=deadline_monotonic,
            )
        except ValueError as exc:
            message = str(exc).lower()
            if "bound" in message or "exceed" in message or "large" in message:
                raise RuntimeProjectionRecoveryBoundExceeded(
                    "runtime projection recovery suffix exceeds its online bound"
                ) from exc
            raise
        if proof is None or proof.through_sequence != page_end:
            raise RuntimeError(
                "runtime projection recovery range lacks its exact joined proof"
            )
        page_events = proof.owned_stored_events
        if len(page_events) != page_end - current:
            raise RuntimeError(
                "runtime projection recovery proof is not contiguous"
            )
        events.extend(page_events)
        payload_bytes += sum(
            len(item.canonical_payload_bytes)
            for item in proof.raw_stored_envelopes
        )
        current = page_end
    return tuple(events), len(events), payload_bytes


class RuntimeProjectionCheckpointDisposition(StrEnum):
    FULL = "full"
    NONE = "none"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class RuntimeProjectionCheckpointOwnerState(StrEnum):
    CLEAN = "clean"
    DIRTY = "dirty"
    CANDIDATE_READY = "candidate_ready"
    WRITING = "writing"
    RETRY_WAIT = "retry_wait"
    CONFIRMING = "confirming"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CLOSING = "closing"
    CLOSED = "closed"
    CLOSE_BLOCKED = "close_blocked"


@dataclass(frozen=True, slots=True)
class CommittedReducerFoldReceipt:
    reducer_id: str
    base_through_sequence: int
    resulting_through_sequence: int
    source_kind: Literal["live_batch", "restored_range", "restore_bootstrap"]
    source_ordered_join_fingerprint: str
    base_semantic_state_fingerprint: str
    resulting_semantic_state_fingerprint: str
    checkpoint_state: CanonicalJsonObjectCarrier | None
    checkpoint_requested: bool
    checkpoint_delta_event_count: int
    checkpoint_delta_payload_bytes: int
    fold_receipt_fingerprint: str

    def __post_init__(self) -> None:
        if not self.reducer_id or self.base_through_sequence < 0:
            raise ValueError("committed reducer fold identity is invalid")
        if self.resulting_through_sequence < self.base_through_sequence:
            raise ValueError("committed reducer fold moved backwards")
        if self.checkpoint_delta_event_count < 0 or self.checkpoint_delta_payload_bytes < 0:
            raise ValueError("committed reducer checkpoint delta bounds are invalid")
        if self.checkpoint_requested and self.checkpoint_state is None:
            raise ValueError("checkpoint request lacks its semantic state")
        expected = context_fingerprint(
            "committed-reducer-fold-receipt:v1",
            {
                "reducer_id": self.reducer_id,
                "base_through_sequence": self.base_through_sequence,
                "resulting_through_sequence": self.resulting_through_sequence,
                "source_kind": self.source_kind,
                "source_ordered_join_fingerprint": (
                    self.source_ordered_join_fingerprint
                ),
                "base_semantic_state_fingerprint": (
                    self.base_semantic_state_fingerprint
                ),
                "resulting_semantic_state_fingerprint": (
                    self.resulting_semantic_state_fingerprint
                ),
                "checkpoint_state_fingerprint": (
                    None
                    if self.checkpoint_state is None
                    else self.checkpoint_state.canonical_payload_fingerprint
                ),
                "checkpoint_requested": self.checkpoint_requested,
                "checkpoint_delta_event_count": self.checkpoint_delta_event_count,
                "checkpoint_delta_payload_bytes": (
                    self.checkpoint_delta_payload_bytes
                ),
            },
        )
        if self.fold_receipt_fingerprint != expected:
            raise ValueError("committed reducer fold receipt fingerprint mismatch")


def build_committed_reducer_fold_receipt(
    *,
    reducer_id: str,
    base_through_sequence: int,
    resulting_through_sequence: int,
    source_kind: Literal["live_batch", "restored_range", "restore_bootstrap"],
    source_ordered_join_fingerprint: str,
    base_state: CanonicalJsonObjectCarrier,
    resulting_state: CanonicalJsonObjectCarrier,
    include_checkpoint_state: bool = True,
    checkpoint_requested: bool | None = None,
    checkpoint_delta_event_count: int = 0,
    checkpoint_delta_payload_bytes: int = 0,
) -> CommittedReducerFoldReceipt:
    requested = (
        include_checkpoint_state
        if checkpoint_requested is None
        else checkpoint_requested
    )
    if requested and not include_checkpoint_state:
        raise ValueError("checkpoint request cannot omit checkpoint state")
    payload = {
        "reducer_id": reducer_id,
        "base_through_sequence": base_through_sequence,
        "resulting_through_sequence": resulting_through_sequence,
        "source_kind": source_kind,
        "source_ordered_join_fingerprint": source_ordered_join_fingerprint,
        "base_semantic_state_fingerprint": (
            base_state.canonical_payload_fingerprint
        ),
        "resulting_semantic_state_fingerprint": (
            resulting_state.canonical_payload_fingerprint
        ),
        "checkpoint_state_fingerprint": (
            resulting_state.canonical_payload_fingerprint
            if include_checkpoint_state
            else None
        ),
        "checkpoint_requested": requested,
        "checkpoint_delta_event_count": checkpoint_delta_event_count,
        "checkpoint_delta_payload_bytes": checkpoint_delta_payload_bytes,
    }
    return CommittedReducerFoldReceipt(
        reducer_id=reducer_id,
        base_through_sequence=base_through_sequence,
        resulting_through_sequence=resulting_through_sequence,
        source_kind=source_kind,
        source_ordered_join_fingerprint=source_ordered_join_fingerprint,
        base_semantic_state_fingerprint=base_state.canonical_payload_fingerprint,
        resulting_semantic_state_fingerprint=(
            resulting_state.canonical_payload_fingerprint
        ),
        checkpoint_state=resulting_state if include_checkpoint_state else None,
        checkpoint_requested=requested,
        checkpoint_delta_event_count=checkpoint_delta_event_count,
        checkpoint_delta_payload_bytes=checkpoint_delta_payload_bytes,
        fold_receipt_fingerprint=context_fingerprint(
            "committed-reducer-fold-receipt:v1", payload
        ),
    )


@dataclass(frozen=True, slots=True)
class StableRuntimeProjectionCheckpointCandidate:
    reducer_id: str
    projection_kind: str
    projection_schema_version: str
    base_checkpoint_sequence: int
    base_state: CanonicalJsonObjectCarrier
    target_through_sequence: int
    target_state: CanonicalJsonObjectCarrier
    source_fold_receipt_fingerprint: str
    recovery_event_count: int
    recovery_payload_bytes: int
    raw_checkpoint: RawRuntimeProjectionCheckpoint
    candidate_fingerprint: str


@dataclass(frozen=True, slots=True)
class RuntimeProjectionCheckpointAttemptReceipt:
    disposition: RuntimeProjectionCheckpointDisposition
    candidate_fingerprint: str
    physical_generation: int
    observed_checkpoint: RawRuntimeProjectionCheckpoint | None
    error_code: str | None


@dataclass(slots=True)
class _ProjectionOwner:
    reducer_id: str
    projection_kind: str
    projection_schema_version: str
    confirmed_head: RawRuntimeProjectionCheckpoint | None
    genesis_state: CanonicalJsonObjectCarrier
    relevant_event_types: frozenset[str]
    latest_fold: CommittedReducerFoldReceipt | None = None
    active_candidate: StableRuntimeProjectionCheckpointCandidate | None = None
    state: RuntimeProjectionCheckpointOwnerState = (
        RuntimeProjectionCheckpointOwnerState.CLEAN
    )
    physical_generation: int = 0
    retry_not_before: float = 0.0
    last_error_code: str | None = None
    first_failure_monotonic: float | None = None
    last_failure_monotonic: float | None = None
    pending_recovery_event_count: int = 0
    pending_recovery_payload_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _CheckpointCandidateBuildInput:
    reducer_id: str
    projection_kind: str
    projection_schema_version: str
    confirmed_head: RawRuntimeProjectionCheckpoint | None
    genesis_state: CanonicalJsonObjectCarrier
    fold: CommittedReducerFoldReceipt
    recovery_event_count: int
    recovery_payload_bytes: int


class RuntimeProjectionCheckpointMaintenanceService:
    """Own stable checkpoint candidates outside the critical event writer lane."""

    def __init__(self, *, runtime_session_id: str, event_log) -> None:
        self.runtime_session_id = runtime_session_id
        self.event_log = event_log
        self._lock = RLock()
        self._owners: dict[str, _ProjectionOwner] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None
        self._worker: asyncio.Task[None] | None = None
        self._accepting = True

    def register_projection(
        self,
        *,
        reducer_id: str,
        projection_kind: str,
        projection_schema_version: str,
        confirmed_head: RawRuntimeProjectionCheckpoint | None,
        genesis_state: CanonicalJsonObjectCarrier,
        current_through_sequence: int,
        current_state: CanonicalJsonObjectCarrier,
        relevant_event_types: tuple[str, ...] = (),
        deadline_monotonic: float | None = None,
    ) -> None:
        with self._lock:
            if reducer_id in self._owners:
                raise ValueError("runtime checkpoint projection is already registered")
            owner = _ProjectionOwner(
                reducer_id=reducer_id,
                projection_kind=projection_kind,
                projection_schema_version=projection_schema_version,
                confirmed_head=confirmed_head,
                genesis_state=genesis_state,
                relevant_event_types=frozenset(relevant_event_types),
            )
            self._owners[reducer_id] = owner
            confirmed_sequence = (
                0 if confirmed_head is None else confirmed_head.through_sequence
            )
            confirmed_state = (
                genesis_state if confirmed_head is None else confirmed_head.state
            )
            if current_through_sequence > confirmed_sequence:
                (
                    _events,
                    owner.pending_recovery_event_count,
                    owner.pending_recovery_payload_bytes,
                ) = read_bounded_runtime_projection_recovery_delta(
                    self.event_log,
                    from_sequence_exclusive=confirmed_sequence,
                    through_sequence=current_through_sequence,
                    deadline_monotonic=deadline_monotonic,
                )
                source = context_fingerprint(
                    "runtime-projection-restore-bootstrap:v1",
                    {
                        "reducer_id": reducer_id,
                        "base": confirmed_sequence,
                        "target": current_through_sequence,
                        "state": current_state.canonical_payload_fingerprint,
                    },
                )
                owner.latest_fold = build_committed_reducer_fold_receipt(
                    reducer_id=reducer_id,
                    base_through_sequence=confirmed_sequence,
                    resulting_through_sequence=current_through_sequence,
                    source_kind="restore_bootstrap",
                    source_ordered_join_fingerprint=source,
                    base_state=confirmed_state,
                    resulting_state=current_state,
                    checkpoint_delta_event_count=(
                        owner.pending_recovery_event_count
                    ),
                    checkpoint_delta_payload_bytes=(
                        owner.pending_recovery_payload_bytes
                    ),
                )
                owner.state = RuntimeProjectionCheckpointOwnerState.DIRTY

    def bind_running_loop(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is not None and self._loop is not loop:
                if not self._loop.is_closed():
                    raise RuntimeError(
                        "runtime checkpoint owner loop identity changed"
                    )
                if self._worker is not None and not self._worker.done():
                    raise RuntimeError(
                        "closed runtime checkpoint loop still owns a live worker"
                    )
                # RuntimeSession supports sequential short-lived event loops
                # (notably sync adapters and deterministic recovery tests).
                # A loop teardown cancels only the waiter/driver task; the
                # stable candidate and physical outcome remain exact and can
                # be confirmed by a successor worker.
                for owner in self._owners.values():
                    if (
                        owner.state
                        is RuntimeProjectionCheckpointOwnerState.WRITING
                    ):
                        if owner.active_candidate is None:
                            raise RuntimeError(
                                "interrupted checkpoint write lacks its stable candidate"
                            )
                        owner.state = (
                            RuntimeProjectionCheckpointOwnerState.CONFIRMING
                        )
                self._wakeup = None
                self._worker = None
            self._loop = loop
            if self._wakeup is None:
                self._wakeup = asyncio.Event()
            if self._worker is None or self._worker.done():
                self._worker = loop.create_task(
                    self._run(),
                    name=f"runtime-projection-checkpoint:{self.runtime_session_id}",
                )
                self._worker.add_done_callback(_consume_task_exception)
            wakeup = self._wakeup
        if wakeup is not None:
            wakeup.set()

    def offer(self, receipt: CommittedReducerFoldReceipt) -> None:
        if receipt.checkpoint_state is None:
            return
        with self._lock:
            if not self._accepting:
                raise RuntimeError("runtime checkpoint owner is closing")
            owner = self._owners.get(receipt.reducer_id)
            if owner is None:
                raise ValueError("runtime checkpoint reducer is not registered")
            prior = owner.latest_fold
            if prior is not None:
                if receipt.resulting_through_sequence < prior.resulting_through_sequence:
                    raise ValueError("runtime checkpoint fold offer moved backwards")
                if (
                    receipt.resulting_through_sequence
                    == prior.resulting_through_sequence
                    and receipt == prior
                ):
                    return
                if (
                    receipt.resulting_through_sequence
                    == prior.resulting_through_sequence
                    and receipt != prior
                ):
                    owner.state = (
                        RuntimeProjectionCheckpointOwnerState.RECONCILIATION_REQUIRED
                    )
                    owner.last_error_code = "CHECKPOINT_FOLD_OFFER_CONFLICT"
                    now = monotonic()
                    owner.first_failure_monotonic = (
                        now
                        if owner.first_failure_monotonic is None
                        else owner.first_failure_monotonic
                    )
                    owner.last_failure_monotonic = now
                    raise ValueError("runtime checkpoint fold offer conflicts")
            owner.latest_fold = receipt
            if receipt.source_kind == "restore_bootstrap":
                # A repair/bootstrap fold covers the complete current suffix
                # from its validated checkpoint base.  Replace the estimate;
                # adding it would double-count earlier live receipts.
                owner.pending_recovery_event_count = (
                    receipt.checkpoint_delta_event_count
                )
                owner.pending_recovery_payload_bytes = (
                    receipt.checkpoint_delta_payload_bytes
                )
            else:
                owner.pending_recovery_event_count += (
                    receipt.checkpoint_delta_event_count
                )
                owner.pending_recovery_payload_bytes += (
                    receipt.checkpoint_delta_payload_bytes
                )
            if (
                owner.pending_recovery_event_count > CHECKPOINT_RECOVERY_HARD_EVENTS
                or owner.pending_recovery_payload_bytes
                > CHECKPOINT_RECOVERY_HARD_BYTES
            ):
                owner.state = (
                    RuntimeProjectionCheckpointOwnerState.RECONCILIATION_REQUIRED
                )
                owner.last_error_code = "CHECKPOINT_RECOVERY_BOUND_EXCEEDED"
                now = monotonic()
                owner.first_failure_monotonic = (
                    now
                    if owner.first_failure_monotonic is None
                    else owner.first_failure_monotonic
                )
                owner.last_failure_monotonic = now
                return
            pressure_requires_checkpoint = (
                owner.pending_recovery_event_count
                >= CHECKPOINT_RECOVERY_SOFT_EVENTS
                or owner.pending_recovery_payload_bytes
                >= CHECKPOINT_RECOVERY_SOFT_BYTES
            )
            if owner.state not in {
                RuntimeProjectionCheckpointOwnerState.WRITING,
                RuntimeProjectionCheckpointOwnerState.CONFIRMING,
                RuntimeProjectionCheckpointOwnerState.RETRY_WAIT,
            } and (receipt.checkpoint_requested or pressure_requires_checkpoint):
                owner.state = RuntimeProjectionCheckpointOwnerState.DIRTY
            loop = self._loop
            wakeup = self._wakeup
        if (
            owner.state is not RuntimeProjectionCheckpointOwnerState.CLEAN
            and loop is not None
            and not loop.is_closed()
            and wakeup is not None
        ):
            loop.call_soon_threadsafe(wakeup.set)

    def checkpoint_handoff_accepted(self, reducer_id: str, through_sequence: int) -> bool:
        with self._lock:
            owner = self._owners.get(reducer_id)
            return bool(
                owner is not None
                and owner.state
                not in {
                    RuntimeProjectionCheckpointOwnerState.RECONCILIATION_REQUIRED,
                    RuntimeProjectionCheckpointOwnerState.CLOSE_BLOCKED,
                }
                and (
                    (
                        owner.latest_fold is not None
                        and owner.latest_fold.resulting_through_sequence
                        >= through_sequence
                    )
                    or (
                        owner.confirmed_head is not None
                        and owner.confirmed_head.through_sequence >= through_sequence
                    )
                )
            )

    def diagnostics(self, reducer_id: str) -> dict[str, object]:
        with self._lock:
            owner = self._owners[reducer_id]
            return {
                "state": owner.state.value,
                "confirmed_through_sequence": (
                    0
                    if owner.confirmed_head is None
                    else owner.confirmed_head.through_sequence
                ),
                "target_through_sequence": (
                    None
                    if owner.latest_fold is None
                    else owner.latest_fold.resulting_through_sequence
                ),
                "candidate_fingerprint": (
                    None
                    if owner.active_candidate is None
                    else owner.active_candidate.candidate_fingerprint
                ),
                "physical_generation": owner.physical_generation,
                "retry_not_before": owner.retry_not_before,
                "last_error_code": owner.last_error_code,
                "first_failure_monotonic": owner.first_failure_monotonic,
                "last_failure_monotonic": owner.last_failure_monotonic,
                "pending_recovery_event_count": (
                    owner.pending_recovery_event_count
                ),
                "pending_recovery_payload_bytes": (
                    owner.pending_recovery_payload_bytes
                ),
                "soft_pressure": (
                    owner.pending_recovery_event_count
                    >= CHECKPOINT_RECOVERY_SOFT_EVENTS
                    or owner.pending_recovery_payload_bytes
                    >= CHECKPOINT_RECOVERY_SOFT_BYTES
                ),
            }

    def assert_event_admission(self, events: tuple[AgentEvent, ...]) -> None:
        """Prevent any physical suffix from crossing the online repair bound."""

        with self._lock:
            for owner in self._owners.values():
                if not events:
                    continue
                estimated_bytes = sum(
                    len(event.model_dump_json().encode("utf-8")) + 512
                    for event in events
                )
                if (
                    owner.pending_recovery_event_count + len(events)
                    > CHECKPOINT_RECOVERY_HARD_EVENTS
                    or owner.pending_recovery_payload_bytes + estimated_bytes
                    > CHECKPOINT_RECOVERY_HARD_BYTES
                    or owner.state
                    is RuntimeProjectionCheckpointOwnerState.RECONCILIATION_REQUIRED
                ):
                    raise RuntimeProjectionCheckpointAdmissionBlocked(
                        f"runtime checkpoint recovery bound reached for {owner.reducer_id}"
                    )
                if (
                    owner.pending_recovery_event_count + len(events)
                    >= CHECKPOINT_RECOVERY_SOFT_EVENTS
                    or owner.pending_recovery_payload_bytes + estimated_bytes
                    >= CHECKPOINT_RECOVERY_SOFT_BYTES
                ):
                    loop = self._loop
                    wakeup = self._wakeup
                    if (
                        loop is not None
                        and not loop.is_closed()
                        and wakeup is not None
                    ):
                        loop.call_soon_threadsafe(wakeup.set)

    async def _run(self) -> None:
        assert self._wakeup is not None
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            while True:
                owner = self._next_owner()
                if owner is None:
                    break
                delay = max(0.0, owner.retry_not_before - monotonic())
                if delay:
                    try:
                        await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    else:
                        self._wakeup.clear()
                    continue
                loop = asyncio.get_running_loop()
                receipt = await loop.run_in_executor(
                    projection_maintenance_executor(),
                    lambda selected=owner: self._drive_once(selected),
                )
                if receipt is not None:
                    self._install_attempt_receipt(owner.reducer_id, receipt)
            with self._lock:
                if not self._accepting and self._all_clean_locked():
                    for owner in self._owners.values():
                        owner.state = RuntimeProjectionCheckpointOwnerState.CLOSED
                    return

    def _next_owner(self) -> _ProjectionOwner | None:
        with self._lock:
            for owner in self._owners.values():
                if owner.state in {
                    RuntimeProjectionCheckpointOwnerState.DIRTY,
                    RuntimeProjectionCheckpointOwnerState.CANDIDATE_READY,
                    RuntimeProjectionCheckpointOwnerState.RETRY_WAIT,
                    RuntimeProjectionCheckpointOwnerState.CONFIRMING,
                }:
                    return owner
        return None

    def _drive_once(
        self, owner: _ProjectionOwner
    ) -> RuntimeProjectionCheckpointAttemptReceipt | None:
        deadline = monotonic() + CHECKPOINT_PHYSICAL_DEADLINE_SECONDS
        build_input: _CheckpointCandidateBuildInput | None = None
        with self._lock:
            current = self._owners[owner.reducer_id]
            fold = current.latest_fold
            if fold is None:
                raise RuntimeError("dirty runtime checkpoint owner lacks fold receipt")
            candidate = current.active_candidate
            if candidate is None:
                build_input = _CheckpointCandidateBuildInput(
                    reducer_id=current.reducer_id,
                    projection_kind=current.projection_kind,
                    projection_schema_version=current.projection_schema_version,
                    confirmed_head=current.confirmed_head,
                    genesis_state=current.genesis_state,
                    fold=fold,
                    recovery_event_count=current.pending_recovery_event_count,
                    recovery_payload_bytes=current.pending_recovery_payload_bytes,
                )
                current.state = RuntimeProjectionCheckpointOwnerState.CANDIDATE_READY
        if build_input is not None:
            try:
                candidate = self._build_candidate(
                    build_input,
                    deadline_monotonic=deadline,
                )
            except BaseException as exc:
                with self._lock:
                    current = self._owners[owner.reducer_id]
                    now = monotonic()
                    current.physical_generation += 1
                    current.last_error_code = (
                        f"PREPARE_{type(exc).__name__.upper()}"
                    )
                    current.retry_not_before = monotonic() + self._retry_delay(
                        current.physical_generation
                    )
                    current.first_failure_monotonic = (
                        now
                        if current.first_failure_monotonic is None
                        else current.first_failure_monotonic
                    )
                    current.last_failure_monotonic = now
                    current.state = RuntimeProjectionCheckpointOwnerState.RETRY_WAIT
                return None
            with self._lock:
                current = self._owners[owner.reducer_id]
                if current.active_candidate is not None:
                    raise RuntimeError(
                        "runtime checkpoint candidate owner changed during preparation"
                    )
                current.active_candidate = candidate
        with self._lock:
            current = self._owners[owner.reducer_id]
            candidate = current.active_candidate
            if candidate is None:
                raise RuntimeError("runtime checkpoint candidate preparation was lost")
            current.physical_generation += 1
            generation = current.physical_generation
            confirming_only = (
                current.state is RuntimeProjectionCheckpointOwnerState.CONFIRMING
            )
            current.state = (
                RuntimeProjectionCheckpointOwnerState.CONFIRMING
                if confirming_only
                else RuntimeProjectionCheckpointOwnerState.WRITING
            )
        if confirming_only:
            return self._confirm_candidate(
                candidate,
                generation=generation,
                deadline_monotonic=deadline,
            )
        try:
            self.event_log.write_runtime_projection_checkpoint(
                candidate.raw_checkpoint,
                deadline_monotonic=deadline,
            )
        except BaseException as write_error:
            confirmation = self._confirm_candidate(
                candidate,
                generation=generation,
                deadline_monotonic=deadline,
            )
            if confirmation.disposition is RuntimeProjectionCheckpointDisposition.FULL:
                return confirmation
            return RuntimeProjectionCheckpointAttemptReceipt(
                disposition=confirmation.disposition,
                candidate_fingerprint=confirmation.candidate_fingerprint,
                physical_generation=confirmation.physical_generation,
                observed_checkpoint=confirmation.observed_checkpoint,
                error_code=(
                    confirmation.error_code
                    or type(write_error).__name__.upper()
                ),
            )
        return self._confirm_candidate(
            candidate,
            generation=generation,
            deadline_monotonic=deadline,
        )

    def _confirm_candidate(
        self,
        candidate: StableRuntimeProjectionCheckpointCandidate,
        *,
        generation: int,
        deadline_monotonic: float,
    ) -> RuntimeProjectionCheckpointAttemptReceipt:
        try:
            observed = self.event_log.read_runtime_projection_checkpoint(
                candidate.projection_kind,
                deadline_monotonic=deadline_monotonic,
            )
        except BaseException as confirm_error:
            return RuntimeProjectionCheckpointAttemptReceipt(
                disposition=RuntimeProjectionCheckpointDisposition.UNKNOWN,
                candidate_fingerprint=candidate.candidate_fingerprint,
                physical_generation=generation,
                observed_checkpoint=None,
                error_code=f"CONFIRM_{type(confirm_error).__name__.upper()}",
            )
        return RuntimeProjectionCheckpointAttemptReceipt(
            disposition=self._classify_observed(candidate, observed),
            candidate_fingerprint=candidate.candidate_fingerprint,
            physical_generation=generation,
            observed_checkpoint=observed,
            error_code=None,
        )

    def _build_candidate(
        self,
        build_input: _CheckpointCandidateBuildInput,
        *,
        deadline_monotonic: float,
    ) -> StableRuntimeProjectionCheckpointCandidate:
        fold = build_input.fold
        head = build_input.confirmed_head
        base_sequence = 0 if head is None else head.through_sequence
        base_state = build_input.genesis_state if head is None else head.state
        if fold.resulting_through_sequence <= base_sequence:
            raise RuntimeError("runtime checkpoint fold is not ahead of its base")
        ledger_prefix = self.event_log.read_raw_ledger_prefix(
            through_sequence=fold.resulting_through_sequence,
            deadline_monotonic=deadline_monotonic,
        )
        target_state = fold.checkpoint_state
        assert target_state is not None
        raw = build_raw_runtime_projection_checkpoint(
            projection_kind=build_input.projection_kind,
            through_sequence=fold.resulting_through_sequence,
            projection_schema_version=build_input.projection_schema_version,
            ledger_prefix=ledger_prefix,
            validation_base_through_sequence=base_sequence,
            validation_base_state=base_state,
            state=target_state,
        )
        candidate_payload = {
            "reducer_id": build_input.reducer_id,
            "projection_kind": build_input.projection_kind,
            "projection_schema_version": build_input.projection_schema_version,
            "base_checkpoint_sequence": base_sequence,
            "base_state_fingerprint": base_state.canonical_payload_fingerprint,
            "target_through_sequence": fold.resulting_through_sequence,
            "target_state_fingerprint": target_state.canonical_payload_fingerprint,
            "source_fold_receipt_fingerprint": fold.fold_receipt_fingerprint,
            "recovery_event_count": build_input.recovery_event_count,
            "recovery_payload_bytes": build_input.recovery_payload_bytes,
            "raw_checkpoint_fingerprint": raw.payload_fingerprint,
        }
        return StableRuntimeProjectionCheckpointCandidate(
            reducer_id=build_input.reducer_id,
            projection_kind=build_input.projection_kind,
            projection_schema_version=build_input.projection_schema_version,
            base_checkpoint_sequence=base_sequence,
            base_state=base_state,
            target_through_sequence=fold.resulting_through_sequence,
            target_state=target_state,
            source_fold_receipt_fingerprint=fold.fold_receipt_fingerprint,
            recovery_event_count=build_input.recovery_event_count,
            recovery_payload_bytes=build_input.recovery_payload_bytes,
            raw_checkpoint=raw,
            candidate_fingerprint=context_fingerprint(
                "stable-runtime-projection-checkpoint-candidate:v1",
                candidate_payload,
            ),
        )

    @staticmethod
    def _classify_observed(
        candidate: StableRuntimeProjectionCheckpointCandidate,
        observed: RawRuntimeProjectionCheckpoint | None,
    ) -> RuntimeProjectionCheckpointDisposition:
        if observed == candidate.raw_checkpoint:
            return RuntimeProjectionCheckpointDisposition.FULL
        if observed is None:
            return RuntimeProjectionCheckpointDisposition.NONE
        if (
            observed.through_sequence == candidate.base_checkpoint_sequence
            and observed.state == candidate.base_state
        ):
            return RuntimeProjectionCheckpointDisposition.NONE
        if (
            observed.projection_kind == candidate.projection_kind
            and observed.projection_schema_version
            == candidate.projection_schema_version
            and observed.through_sequence > candidate.target_through_sequence
            and observed.validation_base_through_sequence
            == candidate.target_through_sequence
            and observed.validation_base_state == candidate.target_state
        ):
            return RuntimeProjectionCheckpointDisposition.FULL
        return RuntimeProjectionCheckpointDisposition.CONFLICT

    def _install_attempt_receipt(
        self,
        reducer_id: str,
        receipt: RuntimeProjectionCheckpointAttemptReceipt,
    ) -> None:
        with self._lock:
            owner = self._owners[reducer_id]
            candidate = owner.active_candidate
            if (
                candidate is None
                or candidate.candidate_fingerprint != receipt.candidate_fingerprint
                or receipt.physical_generation != owner.physical_generation
            ):
                raise RuntimeError("runtime checkpoint attempt receipt is stale")
            if receipt.disposition is RuntimeProjectionCheckpointDisposition.FULL:
                observed = receipt.observed_checkpoint
                if observed is None:
                    raise RuntimeError("FULL runtime checkpoint lacks confirmed head")
                owner.confirmed_head = observed
                owner.pending_recovery_event_count = max(
                    0,
                    owner.pending_recovery_event_count
                    - candidate.recovery_event_count,
                )
                owner.pending_recovery_payload_bytes = max(
                    0,
                    owner.pending_recovery_payload_bytes
                    - candidate.recovery_payload_bytes,
                )
                owner.active_candidate = None
                owner.last_error_code = None
                latest = owner.latest_fold
                if (
                    latest is not None
                    and latest.resulting_through_sequence
                    > observed.through_sequence
                ):
                    owner.state = (
                        RuntimeProjectionCheckpointOwnerState.DIRTY
                        if latest.checkpoint_requested
                        or owner.pending_recovery_event_count
                        >= CHECKPOINT_RECOVERY_SOFT_EVENTS
                        or owner.pending_recovery_payload_bytes
                        >= CHECKPOINT_RECOVERY_SOFT_BYTES
                        else RuntimeProjectionCheckpointOwnerState.CLEAN
                    )
                else:
                    owner.latest_fold = None
                    owner.pending_recovery_event_count = 0
                    owner.pending_recovery_payload_bytes = 0
                    owner.state = RuntimeProjectionCheckpointOwnerState.CLEAN
                owner.retry_not_before = 0.0
                return
            if receipt.disposition is RuntimeProjectionCheckpointDisposition.CONFLICT:
                now = monotonic()
                owner.state = (
                    RuntimeProjectionCheckpointOwnerState.RECONCILIATION_REQUIRED
                )
                owner.last_error_code = receipt.error_code or "CHECKPOINT_CONFLICT"
                owner.first_failure_monotonic = (
                    now
                    if owner.first_failure_monotonic is None
                    else owner.first_failure_monotonic
                )
                owner.last_failure_monotonic = now
                return
            now = monotonic()
            owner.state = (
                RuntimeProjectionCheckpointOwnerState.CONFIRMING
                if receipt.disposition
                is RuntimeProjectionCheckpointDisposition.UNKNOWN
                else RuntimeProjectionCheckpointOwnerState.RETRY_WAIT
            )
            owner.last_error_code = receipt.error_code
            owner.first_failure_monotonic = (
                now
                if owner.first_failure_monotonic is None
                else owner.first_failure_monotonic
            )
            owner.last_failure_monotonic = now
            owner.retry_not_before = monotonic() + self._retry_delay(
                owner.physical_generation
            )

    def all_diagnostics(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            reducer_ids = tuple(self._owners)
        return tuple(
            {
                "reducer_id": reducer_id,
                **self.diagnostics(reducer_id),
            }
            for reducer_id in reducer_ids
        )

    @staticmethod
    def _retry_delay(physical_generation: int) -> float:
        return min(
            CHECKPOINT_RETRY_MAX_SECONDS,
            CHECKPOINT_RETRY_BASE_SECONDS
            * (2 ** min(max(physical_generation - 1, 0), 4)),
        )

    def _all_clean_locked(self) -> bool:
        return all(
            owner.state
            in {
                RuntimeProjectionCheckpointOwnerState.CLEAN,
                RuntimeProjectionCheckpointOwnerState.CLOSING,
                RuntimeProjectionCheckpointOwnerState.CLOSED,
            }
            for owner in self._owners.values()
        )

    async def stop_admission_and_drain(self, *, deadline_monotonic: float) -> None:
        self.bind_running_loop()
        with self._lock:
            self._accepting = False
            for owner in self._owners.values():
                if owner.state is RuntimeProjectionCheckpointOwnerState.CLEAN:
                    owner.state = RuntimeProjectionCheckpointOwnerState.CLOSING
            wakeup = self._wakeup
            worker = self._worker
        assert wakeup is not None and worker is not None
        wakeup.set()
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise TimeoutError("runtime checkpoint maintenance close blocked")
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=remaining)
        except TimeoutError:
            with self._lock:
                for owner in self._owners.values():
                    if owner.state is not RuntimeProjectionCheckpointOwnerState.CLOSED:
                        owner.state = RuntimeProjectionCheckpointOwnerState.CLOSE_BLOCKED
            raise TimeoutError("runtime checkpoint maintenance close blocked") from None

    def close_if_idle(self) -> None:
        """Close an unbound/already-drained owner without starting new I/O."""

        with self._lock:
            if not self._all_clean_locked():
                raise RuntimeError("runtime checkpoint maintenance is not idle")
            self._accepting = False
            for owner in self._owners.values():
                owner.state = RuntimeProjectionCheckpointOwnerState.CLOSED
            loop = self._loop
            wakeup = self._wakeup
        if loop is not None and not loop.is_closed() and wakeup is not None:
            loop.call_soon_threadsafe(wakeup.set)


@dataclass(frozen=True, slots=True)
class PreparedCheckpointedCommittedReducerFold:
    """Process-local transition prepared without mutating semantic authority."""

    reducer_id: str
    fold_receipt: CommittedReducerFoldReceipt
    expected_base_state: CanonicalJsonObjectCarrier
    prepared_semantic_state: object


@dataclass(frozen=True, slots=True)
class CheckpointedCommittedReducerIngress:
    """Prepare/commit ingress for an atomic semantic fold and registration head."""

    reducer_id: str
    prepare_owned_events: Callable[
        [tuple[AgentEvent, ...]],
        tuple[CanonicalJsonObjectCarrier, CanonicalJsonObjectCarrier, object],
    ]
    install_prepared_owned_events: Callable[
        [object, CanonicalJsonObjectCarrier], None
    ]
    reset_owned_events: Callable[[], None]
    checkpoint_relevant: Callable[[tuple[AgentEvent, ...]], bool]

    def apply_live_committed(
        self, receipt: StoredEventBatchCommitReceipt
    ) -> PreparedCheckpointedCommittedReducerFold:
        base, resulting, prepared = self.prepare_owned_events(
            receipt.owned_stored_events
        )
        checkpoint_requested = self.checkpoint_relevant(
            receipt.owned_stored_events
        )
        fold_receipt = build_committed_reducer_fold_receipt(
            reducer_id=self.reducer_id,
            base_through_sequence=receipt.raw_stored_envelopes[0].sequence - 1,
            resulting_through_sequence=receipt.raw_stored_envelopes[-1].sequence,
            source_kind="live_batch",
            source_ordered_join_fingerprint=receipt.ordered_join_fingerprint,
            base_state=base,
            resulting_state=resulting,
            include_checkpoint_state=True,
            checkpoint_requested=checkpoint_requested,
            checkpoint_delta_event_count=len(receipt.raw_stored_envelopes),
            checkpoint_delta_payload_bytes=sum(
                len(item.canonical_payload_bytes)
                for item in receipt.raw_stored_envelopes
            ),
        )
        return PreparedCheckpointedCommittedReducerFold(
            reducer_id=self.reducer_id,
            fold_receipt=fold_receipt,
            expected_base_state=base,
            prepared_semantic_state=prepared,
        )

    def fold_restored_range(
        self, range_proof: JoinedRawStoredEventRangeProof
    ) -> PreparedCheckpointedCommittedReducerFold:
        base, resulting, prepared = self.prepare_owned_events(
            range_proof.owned_stored_events
        )
        checkpoint_requested = self.checkpoint_relevant(
            range_proof.owned_stored_events
        )
        fold_receipt = build_committed_reducer_fold_receipt(
            reducer_id=self.reducer_id,
            base_through_sequence=range_proof.from_sequence_exclusive,
            resulting_through_sequence=range_proof.through_sequence,
            source_kind="restored_range",
            source_ordered_join_fingerprint=range_proof.range_proof_fingerprint,
            base_state=base,
            resulting_state=resulting,
            include_checkpoint_state=True,
            checkpoint_requested=checkpoint_requested,
            checkpoint_delta_event_count=len(range_proof.raw_stored_envelopes),
            checkpoint_delta_payload_bytes=sum(
                len(item.canonical_payload_bytes)
                for item in range_proof.raw_stored_envelopes
            ),
        )
        return PreparedCheckpointedCommittedReducerFold(
            reducer_id=self.reducer_id,
            fold_receipt=fold_receipt,
            expected_base_state=base,
            prepared_semantic_state=prepared,
        )

    def commit_prepared(
        self, prepared: PreparedCheckpointedCommittedReducerFold
    ) -> None:
        if prepared.reducer_id != self.reducer_id:
            raise ValueError("prepared committed reducer fold owner drifted")
        self.install_prepared_owned_events(
            prepared.prepared_semantic_state,
            prepared.expected_base_state,
        )

    def reset_for_rebuild(self) -> None:
        self.reset_owned_events()


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return


class RuntimeProjectionCheckpointAdmissionBlocked(RuntimeError):
    """A relevant write would exceed bounded online checkpoint recovery."""


__all__ = [
    "CheckpointedCommittedReducerIngress",
    "CommittedReducerFoldReceipt",
    "PreparedCheckpointedCommittedReducerFold",
    "RuntimeProjectionCheckpointAttemptReceipt",
    "RuntimeProjectionCheckpointAdmissionBlocked",
    "RuntimeProjectionCheckpointDisposition",
    "RuntimeProjectionCheckpointMaintenanceService",
    "RuntimeProjectionCheckpointOwnerState",
    "RuntimeProjectionRecoveryBoundExceeded",
    "StableRuntimeProjectionCheckpointCandidate",
    "build_committed_reducer_fold_receipt",
    "read_bounded_runtime_projection_recovery_delta",
]
