"""Session-owned renderer-neutral presentation projection service."""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Callable, Literal, TypeVar

from pulsara_agent.blocking_executor import auxiliary_io_executor
from pulsara_agent.primitives._context_base import canonical_json_bytes
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.presentation_history import (
    PresentationHistoryCheckpointStableCandidateFact,
    PresentationHistoryProjectionCheckpointFact,
    UpsertPresentationHistoryEntryMutationFact,
)
from pulsara_agent.event import RunEndEvent, RunStartEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored
from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.primitives.presentation_view import (
    BoundedOrderedResidentChangesFact,
    PresentationHistoryResidentRemoveFact,
    PresentationHistoryResidentUpsertFact,
    PresentationHistoryRootAdvancedFact,
    PresentationHistoryRootCursorRelationFact,
    PresentationHistoryViewportSnapshotFact,
    ResidentEntriesUnchangedFact,
    ResidentHistoryRebaseRequiredFact,
)
from pulsara_agent.primitives.stored_event import RawStoredEventEnvelope
from pulsara_agent.runtime.terminal_presentation.history_capacity import (
    PresentationHistoryCapacityError,
    PresentationHistoryCapacityOwner,
)
from pulsara_agent.runtime.terminal_presentation.history_retention import (
    PresentationHistoryRootRetentionOwner,
)
from pulsara_agent.runtime.terminal_presentation.history_checkpoint import (
    PreparedPresentationHistoryCheckpointCommitAttempt,
    PresentationHistoryCheckpointCommitReceipt,
)
from pulsara_agent.runtime.terminal_presentation.io_service import (
    PendingPresentationIoError,
    TerminalPresentationIoService,
)
from pulsara_agent.runtime.terminal_presentation.observation import (
    UiTapBootstrapReceipt,
)
from pulsara_agent.runtime.terminal_presentation.projection import (
    PresentationHistoryProjectionOwner,
)
from pulsara_agent.runtime.terminal_presentation.restore import (
    PresentationAwareTranscriptRestore,
    restore_transcript_with_presentation_spine,
)
from pulsara_agent.runtime.authority_materialization.transcript_restore import (
    restore_transcript_projection,
)
from pulsara_agent.runtime.terminal_presentation.viewport import (
    PresentationHistoryViewportService,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PresentationObservationRead:
    status: Literal["next", "no_change", "gap"]
    root_advanced: PresentationHistoryRootAdvancedFact | None
    latest_projection_revision: int
    latest_authority_high_water: int


class PresentationHistoryAdmissionRejected(PresentationHistoryCapacityError):
    """An ordinary operation cannot fit the frozen history-capacity policy."""

    def __init__(self, decision) -> None:
        super().__init__(
            f"presentation history admission rejected: {decision.disposition}"
        )
        self.decision = decision


class PresentationCheckpointResolutionPending(RuntimeError):
    """The stable checkpoint owner has not reached a terminal confirmation."""

    def __init__(self, receipt: PresentationHistoryCheckpointCommitReceipt) -> None:
        super().__init__(f"presentation checkpoint remains {receipt.disposition}")
        self.receipt = receipt


class TerminalPresentationCloseBlocked(RuntimeError):
    """Foundation teardown cannot cross an unfinished physical owner."""


@dataclass(frozen=True, slots=True)
class _CheckpointInstall:
    checkpoint: PresentationHistoryProjectionCheckpointFact
    candidate: PresentationHistoryCheckpointStableCandidateFact | None
    receipt: PresentationHistoryCheckpointCommitReceipt | None


@dataclass(slots=True)
class _PendingCheckpointOwner:
    generation: int
    attempt: PreparedPresentationHistoryCheckpointCommitAttempt
    last_receipt: PresentationHistoryCheckpointCommitReceipt | None = None


@dataclass(slots=True)
class _PendingCheckpointDelivery:
    delivery_kind: Literal["live_ack", "range_catch_up", "restore_install"]
    subscriber_id: str | None
    through_sequence: int
    before_viewport: PresentationHistoryViewportSnapshotFact
    from_sequence_exclusive: int | None = None
    covered_envelopes: tuple[RawStoredEventEnvelope, ...] = ()
    root_transition_installed: bool = False


@dataclass(slots=True)
class TerminalPresentationFoundationService:
    """Consumes the tap on a background lane and owns derived history roots."""

    runtime_session: RuntimeSession
    executor: Executor = field(default_factory=auxiliary_io_executor)
    projection_owner: PresentationHistoryProjectionOwner = field(init=False)
    capacity_owner: PresentationHistoryCapacityOwner = field(init=False)
    retention_owner: PresentationHistoryRootRetentionOwner = field(init=False)
    viewport_service: PresentationHistoryViewportService = field(init=False)
    io_service: TerminalPresentationIoService = field(init=False, repr=False)
    _subscriber_id: str | None = field(default=None, init=False, repr=False)
    _wakeup_callback_id: str | None = field(default=None, init=False, repr=False)
    _wakeup: asyncio.Event | None = field(default=None, init=False, repr=False)
    _worker: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _worker_error: BaseException | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)
    _close_deadline_monotonic: float | None = field(
        default=None, init=False, repr=False
    )
    _reconciliation_reason: str | None = field(default=None, init=False, repr=False)
    _observation_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _root_advance_ring: list[PresentationHistoryRootAdvancedFact] = field(
        default_factory=list, init=False, repr=False
    )
    _maximum_root_advance_ring_entries: int = field(default=128, init=False)
    _pending_capacity_terminalizations: dict[
        str,
        tuple[int, Literal["settled", "released", "reconciliation_required"]],
    ] = field(default_factory=dict, init=False, repr=False)
    _checkpoint_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _checkpoint_attempt_generation: int = field(default=0, init=False, repr=False)
    _pending_checkpoint_owner: _PendingCheckpointOwner | None = field(
        default=None, init=False, repr=False
    )
    _pending_confirmed_checkpoint_install: _CheckpointInstall | None = field(
        default=None, init=False, repr=False
    )
    _pending_checkpoint_delivery: _PendingCheckpointDelivery | None = field(
        default=None, init=False, repr=False
    )
    _latest_viewport_snapshot: object | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        session = self.runtime_session
        materialization_policy = session.presentation_history_materialization_policy
        self.projection_owner = PresentationHistoryProjectionOwner(
            runtime_session_id=session.runtime_session_id,
            placement_contract=(
                materialization_policy.tree_contract.placement_key_contract
            ),
            purpose_policy=session.presentation_purpose_policy_registry,
            audit_extractor=session.presentation_audit_extractor_binding,
            transcript_documents=session.transcript_projection_document_registry,
            archive=session.archive,
        )
        self.capacity_owner = PresentationHistoryCapacityOwner(
            runtime_session_id=session.runtime_session_id,
            materialization_policy=materialization_policy,
        )
        self.retention_owner = PresentationHistoryRootRetentionOwner(
            max_retained_root_generations=(
                materialization_policy.max_retained_root_generations
            ),
            root_retention_ttl_seconds=(
                materialization_policy.root_retention_ttl_seconds
            ),
        )
        self.viewport_service = PresentationHistoryViewportService(
            runtime_session_id=session.runtime_session_id,
            checkpoint_owner=session.presentation_history_checkpoint_owner,
            retention_owner=self.retention_owner,
            capacity_owner=self.capacity_owner,
            materialization_policy=materialization_policy,
        )
        self.io_service = TerminalPresentationIoService(executor=self.executor)

    @property
    def reconciliation_reason(self) -> str | None:
        return self._reconciliation_reason

    def initialize(self, *, deadline_monotonic: float | None) -> None:
        """Restore one checkpoint and a bounded raw suffix before live attach."""

        checkpoint_owner = self.runtime_session.presentation_history_checkpoint_owner
        checkpoint = checkpoint_owner.read_checkpoint(
            deadline_monotonic=deadline_monotonic
        )
        acceleration = checkpoint_owner.read_spine_acceleration(
            deadline_monotonic=deadline_monotonic
        )
        capacity_checkpoint = checkpoint_owner.read_capacity_checkpoint(
            deadline_monotonic=deadline_monotonic
        )
        if checkpoint is None or acceleration is None or capacity_checkpoint is None:
            raise RuntimeError("presentation foundation lacks durable genesis")
        self.projection_owner.restore_checkpoint_state(acceleration=acceleration)
        self.capacity_owner.restore_checkpoint(capacity_checkpoint)
        self.viewport_service.install_checkpoint(
            checkpoint, deadline_monotonic=deadline_monotonic
        )
        self._latest_viewport_snapshot = self.viewport_service.snapshot(
            deadline_monotonic=deadline_monotonic
        )
        restored_range = self.runtime_session._presentation_restore_range
        restored_fold = self.runtime_session._presentation_restore_fold
        if (restored_range is None) != (restored_fold is None):
            raise RuntimeError("presentation restore range/fold carrier is partial")
        if restored_range is not None:
            before_viewport = self.snapshot()
            applied = self.projection_owner.apply_restored_range(
                restored_range, restored_fold
            )
            self._apply_restored_capacity_transitions(
                restored_range=restored_range,
                ordered_segments=applied.ordered_segments,
            )
            self._pending_checkpoint_delivery = _PendingCheckpointDelivery(
                delivery_kind="restore_install",
                subscriber_id=None,
                through_sequence=restored_range.through_sequence,
                before_viewport=before_viewport,
            )
            try:
                installed = self._checkpoint_current_sync(
                    predecessor=checkpoint,
                    deadline_monotonic=deadline_monotonic,
                )
            except PresentationCheckpointResolutionPending as exc:
                self._reconciliation_reason = (
                    f"PRESENTATION_CHECKPOINT_{exc.receipt.disposition.upper()}"
                )
            else:
                self.viewport_service.install_checkpoint(
                    installed.checkpoint,
                    deadline_monotonic=deadline_monotonic,
                )
                self._latest_viewport_snapshot = self.viewport_service.snapshot(
                    deadline_monotonic=deadline_monotonic
                )
                if installed.candidate is not None:
                    self._mark_checkpoint_delivery_complete(installed)
                self._pending_checkpoint_delivery = None

    def start_background_if_possible(self) -> None:
        if self._closed or self._worker is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        snapshot = self.projection_owner.snapshot()
        bootstrap = self.runtime_session.ui_committed_event_tap.begin_bootstrap(
            snapshot_through_sequence=snapshot.through_authority_sequence
        )
        if bootstrap.status == "gap":
            self.runtime_session.ui_committed_event_tap.detach(
                bootstrap.subscriber_id,
                reason="presentation_bootstrap_gap",
            )
            self._reconciliation_reason = "PRESENTATION_TAP_BOOTSTRAP_GAP"
            return
        self._subscriber_id = bootstrap.subscriber_id
        if bootstrap.status == "ready":
            self.runtime_session.ui_committed_event_tap.mark_live(
                bootstrap.subscriber_id,
                through_sequence=snapshot.through_authority_sequence,
            )
        self._wakeup = asyncio.Event()

        def wake() -> None:
            loop.call_soon_threadsafe(self._signal_wakeup)

        self._wakeup_callback_id = (
            self.runtime_session.ui_committed_event_tap.register_wakeup_callback(wake)
        )
        self._worker = loop.create_task(
            self._run(bootstrap),
            name=f"terminal-presentation:{self.runtime_session.runtime_session_id}",
        )
        if bootstrap.retained_entries or bootstrap.status == "range_catch_up_required":
            self._signal_wakeup()

    def snapshot(self):
        with self._observation_lock:
            snapshot = self._latest_viewport_snapshot
        if snapshot is None:
            raise RuntimeError("presentation foundation has no installed viewport")
        return snapshot

    async def execute_bounded_io(
        self,
        *,
        operation_name: str,
        operation: Callable[[], T],
        deadline_monotonic: float,
    ) -> T:
        """Run a presentation-facing blocking read under the session owner."""

        return await self.io_service.execute(
            operation_name=operation_name,
            operation=operation,
            deadline_monotonic=deadline_monotonic,
        )

    async def read_history_page_async(
        self,
        *,
        cursor,
        direction,
        limits,
        absolute_deadline: float,
    ):
        return await self.execute_bounded_io(
            operation_name="terminal-presentation-history-page",
            operation=lambda: self.viewport_service.read_page(
                cursor=cursor,
                direction=direction,
                limits=limits,
                absolute_deadline=absolute_deadline,
            ),
            deadline_monotonic=absolute_deadline,
        )

    def reserve_ordinary_growth(
        self,
        *,
        admission_kind: str,
        source_authority_fingerprint: str,
        owner_kind: str,
        owner_id: str,
        owner_generation: int,
    ):
        """Atomically enforce the shared ordinary-history admission fence."""

        projection = self.projection_owner.snapshot()
        _, confirmed_root = self.retention_owner.latest()
        current_tail_growth = max(
            0, len(projection.ordered_entries) - confirmed_root.entry_count
        )
        quote = self.capacity_owner.derive_quote(
            admission_kind=admission_kind,
            source_authority_fingerprint=source_authority_fingerprint,
        )
        decision = self.capacity_owner.decide(
            quote=quote,
            confirmed_entry_count=confirmed_root.entry_count,
            current_tail_worst_case_entry_count=current_tail_growth,
        )
        if decision.disposition != "available":
            raise PresentationHistoryAdmissionRejected(decision)
        return self.capacity_owner.reserve(
            quote=quote,
            decision=decision,
            owner_kind=owner_kind,
            owner_id=owner_id,
            owner_generation=owner_generation,
        )

    def adopt_recovered_run_growth(self, run_id: str) -> str:
        reservation = self.capacity_owner.rebind_active_owner(
            owner_kind="host_run",
            owner_id=run_id,
        )
        return reservation.growth_reservation_id

    def terminalize_ordinary_growth(
        self,
        reservation_id: str,
        *,
        outcome: Literal["settled", "released", "reconciliation_required"],
    ) -> None:
        reservation = self.capacity_owner.reservation(reservation_id)
        if reservation is None or reservation.reservation_state in {
            "settled",
            "released",
        }:
            return
        self.capacity_owner.terminalize(reservation_id, outcome=outcome)

    def terminalize_ordinary_growth_after_sequence(
        self,
        reservation_id: str,
        *,
        through_sequence: int,
        outcome: Literal["settled", "released", "reconciliation_required"],
    ) -> None:
        """Keep reserve live until presentation has folded the terminal authority."""

        if through_sequence < 1:
            raise ValueError("history reservation terminal sequence is invalid")
        with self._observation_lock:
            if self.projection_owner.through_sequence >= through_sequence:
                self.terminalize_ordinary_growth(reservation_id, outcome=outcome)
                return
            existing = self._pending_capacity_terminalizations.get(reservation_id)
            candidate = (through_sequence, outcome)
            if existing is not None and existing != candidate:
                self.terminalize_ordinary_growth(
                    reservation_id, outcome="reconciliation_required"
                )
                raise ValueError("history reservation terminal authority conflicts")
            self._pending_capacity_terminalizations[reservation_id] = candidate

    def read_observation_after(
        self, *, projection_revision: int
    ) -> PresentationObservationRead:
        """Read one bounded durable transition without consulting Runtime internals."""

        viewport = self.snapshot()
        latest_revision = viewport.projection_revision
        latest_high_water = viewport.active_head.through_authority_sequence
        with self._observation_lock:
            if projection_revision == latest_revision:
                return PresentationObservationRead(
                    status="no_change",
                    root_advanced=None,
                    latest_projection_revision=latest_revision,
                    latest_authority_high_water=latest_high_water,
                )
            transition = next(
                (
                    item
                    for item in self._root_advance_ring
                    if item.base_projection_revision == projection_revision
                ),
                None,
            )
            if transition is not None:
                return PresentationObservationRead(
                    status="next",
                    root_advanced=transition,
                    latest_projection_revision=latest_revision,
                    latest_authority_high_water=latest_high_water,
                )
            return PresentationObservationRead(
                status="gap",
                root_advanced=None,
                latest_projection_revision=latest_revision,
                latest_authority_high_water=latest_high_water,
            )

    async def stop_admission_and_drain(self, *, deadline_monotonic: float) -> None:
        """Drain logical work and every underlying blocking operation."""

        if self._closed:
            return
        self._closing = True
        self._close_deadline_monotonic = deadline_monotonic
        if self._wakeup_callback_id is not None:
            self.runtime_session.ui_committed_event_tap.unregister_wakeup_callback(
                self._wakeup_callback_id
            )
            self._wakeup_callback_id = None
        self._signal_wakeup()
        worker = self._worker
        if worker is not None and not worker.done():
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TerminalPresentationCloseBlocked(
                    "presentation close deadline expired before worker drain"
                )
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=remaining)
            except TimeoutError as exc:
                raise TerminalPresentationCloseBlocked(
                    "presentation background owner did not physically drain"
                ) from exc
        try:
            await self.io_service.stop_admission_and_drain(
                deadline_monotonic=deadline_monotonic
            )
        except PendingPresentationIoError as exc:
            raise TerminalPresentationCloseBlocked(
                "presentation physical I/O did not exit before close"
            ) from exc
        self._discard_derived_close_state()
        self.io_service.close_if_idle()
        self._finish_close()

    def close(self) -> None:
        """Finalize only after the async Host close barrier drained owners."""

        if self._closed:
            return
        worker = self._worker
        if worker is not None and not worker.done():
            raise TerminalPresentationCloseBlocked(
                "cannot synchronously close a live presentation worker"
            )
        if self.io_service.pending_count():
            raise TerminalPresentationCloseBlocked(
                "cannot close presentation with physical I/O in flight"
            )
        self._discard_derived_close_state()
        self.io_service.close_if_idle()
        self._finish_close()

    def _discard_derived_close_state(self) -> None:
        """Drop presentation-only retry/delivery intent after physical drain."""

        if self.io_service.pending_count():
            raise TerminalPresentationCloseBlocked(
                "cannot discard presentation state with physical I/O in flight"
            )
        with self._checkpoint_lock:
            self._pending_checkpoint_owner = None
            self._pending_confirmed_checkpoint_install = None
            self._pending_checkpoint_delivery = None
        with self._observation_lock:
            self._pending_capacity_terminalizations.clear()

    def _finish_close(self) -> None:
        self._closed = True
        self._closing = True
        tap = self.runtime_session.ui_committed_event_tap
        if self._wakeup_callback_id is not None:
            tap.unregister_wakeup_callback(self._wakeup_callback_id)
            self._wakeup_callback_id = None
        if self._subscriber_id is not None:
            tap.detach(self._subscriber_id, reason="runtime_session_close")
            self._subscriber_id = None
        self._worker = None
        self.retention_owner.clear()

    def _signal_wakeup(self) -> None:
        if self._wakeup is not None:
            self._wakeup.set()

    async def _run_checkpoint_owned(
        self,
        *,
        predecessor: PresentationHistoryProjectionCheckpointFact | None,
        deadline_monotonic: float,
    ) -> _CheckpointInstall:
        handle = await self.io_service.start_owned(
            operation_name="terminal-presentation-checkpoint",
            operation=lambda: self._checkpoint_current_sync(
                predecessor=predecessor,
                deadline_monotonic=deadline_monotonic,
            ),
            deadline_monotonic=deadline_monotonic,
        )
        # Do not time out or cancel the owner after physical start.  The database
        # deadline is passed into the operation and Host close owns its true exit.
        return await handle.wait_physical_completion()

    async def _install_checkpoint_async(
        self,
        checkpoint: PresentationHistoryProjectionCheckpointFact,
        *,
        deadline_monotonic: float,
    ):
        def install_and_snapshot():
            self.viewport_service.install_checkpoint(
                checkpoint, deadline_monotonic=deadline_monotonic
            )
            return self.viewport_service.snapshot(deadline_monotonic=deadline_monotonic)

        handle = await self.io_service.start_owned(
            operation_name="terminal-presentation-viewport-install",
            operation=install_and_snapshot,
            deadline_monotonic=deadline_monotonic,
        )
        snapshot = await handle.wait_physical_completion()
        with self._observation_lock:
            self._latest_viewport_snapshot = snapshot
        return snapshot

    async def _install_and_record_transition(
        self,
        installed: _CheckpointInstall,
        *,
        before_viewport,
        deadline_monotonic: float,
    ) -> None:
        if installed.candidate is None or installed.receipt is None:
            raise RuntimeError("presentation checkpoint lacks its FULL proof")
        after_viewport = await self._install_checkpoint_async(
            installed.checkpoint,
            deadline_monotonic=deadline_monotonic,
        )
        transition = _build_root_advanced(
            before=before_viewport,
            after=after_viewport,
            candidate=installed.candidate,
            receipt=installed.receipt,
        )
        with self._observation_lock:
            duplicate = next(
                (
                    item
                    for item in self._root_advance_ring
                    if item.base_projection_revision
                    == transition.base_projection_revision
                ),
                None,
            )
            if duplicate is not None and duplicate != transition:
                raise RuntimeError(
                    "presentation root advance changed at the same base revision"
                )
            if duplicate is None:
                self._root_advance_ring.append(transition)
            del self._root_advance_ring[
                : max(
                    0,
                    len(self._root_advance_ring)
                    - self._maximum_root_advance_ring_entries,
                )
            ]

    def _mark_checkpoint_delivery_complete(self, installed: _CheckpointInstall) -> None:
        with self._checkpoint_lock:
            current = self._pending_confirmed_checkpoint_install
            if current is None:
                raise RuntimeError("presentation checkpoint delivery owner disappeared")
            if (
                current.candidate != installed.candidate
                or current.receipt != installed.receipt
                or current.checkpoint != installed.checkpoint
            ):
                raise RuntimeError("presentation checkpoint delivery identity changed")
            self._pending_confirmed_checkpoint_install = None

    def _checkpoint_deadline(self) -> float:
        if self._closing and self._close_deadline_monotonic is not None:
            return self._close_deadline_monotonic
        return monotonic() + 10.0

    async def _run(self, bootstrap: UiTapBootstrapReceipt) -> None:
        assert self._wakeup is not None
        retry_delay_seconds = 0.05
        try:
            if bootstrap.status == "range_catch_up_required":
                try:
                    await self._complete_range_catch_up(bootstrap)
                except PresentationCheckpointResolutionPending as exc:
                    self._reconciliation_reason = (
                        f"PRESENTATION_CHECKPOINT_{exc.receipt.disposition.upper()}"
                    )
            while not self._closed:
                try:
                    await self._drain_pending()
                except PresentationCheckpointResolutionPending as exc:
                    self._reconciliation_reason = (
                        f"PRESENTATION_CHECKPOINT_{exc.receipt.disposition.upper()}"
                    )
                    if self._closing:
                        return
                    deadline = self._close_deadline_monotonic
                    if self._closing and (deadline is None or monotonic() >= deadline):
                        raise TerminalPresentationCloseBlocked(
                            "presentation checkpoint did not resolve before close"
                        ) from exc
                    try:
                        await asyncio.wait_for(
                            self._wakeup.wait(), timeout=retry_delay_seconds
                        )
                    except TimeoutError:
                        pass
                    self._wakeup.clear()
                    retry_delay_seconds = min(1.0, retry_delay_seconds * 2.0)
                    continue
                retry_delay_seconds = 0.05
                if self._closing:
                    return
                await self._wakeup.wait()
                self._wakeup.clear()
        except asyncio.CancelledError:
            self._worker_error = TerminalPresentationCloseBlocked(
                "presentation worker cancellation bypassed physical drain"
            )
            return
        except BaseException as exc:
            self._worker_error = exc
            self._reconciliation_reason = (
                f"PRESENTATION_BACKGROUND_{type(exc).__name__.upper()}"
            )
            if self._subscriber_id is not None:
                self.runtime_session.ui_committed_event_tap.detach(
                    self._subscriber_id,
                    reason="presentation_background_failure",
                )

    async def _complete_range_catch_up(self, bootstrap: UiTapBootstrapReceipt) -> None:
        """Rebuild the missing interval from canonical rows, never fake receipts."""

        source_high_water = bootstrap.snapshot_through_sequence
        target_high_water = bootstrap.frozen_ring_head_sequence
        if target_high_water <= source_high_water:
            raise RuntimeError("presentation bootstrap catch-up range is empty")
        subscriber_id = self._subscriber_id
        if subscriber_id != bootstrap.subscriber_id:
            raise RuntimeError("presentation bootstrap subscriber identity changed")
        before_viewport = self.snapshot()
        deadline = self._checkpoint_deadline()
        restore_handle = await self.io_service.start_owned(
            operation_name="terminal-presentation-range-catch-up",
            operation=lambda: self._restore_bootstrap_range_sync(
                source_high_water=source_high_water,
                target_high_water=target_high_water,
                deadline_monotonic=deadline,
            ),
            deadline_monotonic=deadline,
        )
        restored = await restore_handle.wait_physical_completion()
        proof = restored.presentation_catch_up_range
        fold = restored.presentation_catch_up_fold
        if proof is None or fold is None:
            raise RuntimeError("presentation bootstrap catch-up proof is missing")
        applied = self.projection_owner.apply_restored_range(proof, fold)
        self._apply_restored_capacity_transitions(
            restored_range=proof,
            ordered_segments=applied.ordered_segments,
        )
        self._settle_pending_capacity_terminalizations(
            through_sequence=target_high_water
        )
        self._pending_checkpoint_delivery = _PendingCheckpointDelivery(
            delivery_kind="range_catch_up",
            subscriber_id=subscriber_id,
            from_sequence_exclusive=source_high_water,
            through_sequence=target_high_water,
            before_viewport=before_viewport,
            covered_envelopes=proof.raw_stored_envelopes,
        )
        installed = await self._run_checkpoint_owned(
            predecessor=None,
            deadline_monotonic=deadline,
        )
        await self._complete_confirmed_checkpoint_delivery(
            installed,
            deadline_monotonic=deadline,
        )
        current_subscriber_id = self._subscriber_id
        if current_subscriber_id is not None and (
            self.runtime_session.ui_committed_event_tap.snapshot_subscriber(
                current_subscriber_id
            ).pending_entries
        ):
            self._signal_wakeup()

    def _restore_bootstrap_range_sync(
        self,
        *,
        source_high_water: int,
        target_high_water: int,
        deadline_monotonic: float,
    ) -> PresentationAwareTranscriptRestore:
        runtime = self.runtime_session
        checkpoint_owner = runtime.presentation_history_checkpoint_owner
        checkpoint = checkpoint_owner.read_checkpoint(
            deadline_monotonic=deadline_monotonic
        )
        acceleration = checkpoint_owner.read_spine_acceleration(
            deadline_monotonic=deadline_monotonic
        )
        if (
            checkpoint is None
            or acceleration is None
            or checkpoint.through_authority_sequence != source_high_water
            or acceleration.through_authority_sequence != source_high_water
        ):
            raise RuntimeError("presentation bootstrap base identity changed")
        canonical_at_target = restore_transcript_projection(
            event_log=runtime.event_log,
            archive=runtime.archive,
            runtime_session_id=runtime.runtime_session_id,
            requested_through_sequence=target_high_water,
            event_domain_binding=runtime.authority_materialization_contracts.event_domain,
            materialization_contracts=(
                runtime.transcript_projection_materialization_contracts
            ),
            limits=runtime.authority_materialization_contracts.limits,
            deadline_monotonic=deadline_monotonic,
            allow_seedless_test_bootstrap=(
                runtime.allow_unbootstrapped_test_events
                and isinstance(runtime.event_log, InMemoryEventLog)
            ),
        )
        return restore_transcript_with_presentation_spine(
            current_restore=canonical_at_target,
            acceleration=acceleration,
            event_log=runtime.event_log,
            archive=runtime.archive,
            runtime_session_id=runtime.runtime_session_id,
            requested_through_sequence=target_high_water,
            authority_contracts=runtime.authority_materialization_contracts,
            materialization_contracts=(
                runtime.transcript_projection_materialization_contracts
            ),
            deadline_monotonic=deadline_monotonic,
            allow_seedless_test_bootstrap=(
                runtime.allow_unbootstrapped_test_events
                and isinstance(runtime.event_log, InMemoryEventLog)
            ),
        )

    async def _drain_pending(self) -> None:
        if (
            self._pending_checkpoint_owner is not None
            or self._pending_confirmed_checkpoint_install is not None
        ):
            await self._resolve_pending_checkpoint_delivery()
        while True:
            subscriber_id = self._subscriber_id
            if subscriber_id is None:
                return
            subscriber = (
                self.runtime_session.ui_committed_event_tap.snapshot_subscriber(
                    subscriber_id
                )
            )
            if subscriber.status == "gap":
                self._reconciliation_reason = "PRESENTATION_TAP_GAP"
                await self._rebootstrap_after_gap(subscriber_id)
                subscriber_id = self._subscriber_id
                if subscriber_id is None:
                    return
                continue
            if not subscriber.pending_entries:
                return
            # Fold the whole currently frozen subscriber batch and create one
            # checkpoint.  Physical writer transaction grouping remains irrelevant,
            # while a normal burst no longer performs one artifact/SQL round trip
            # per tap entry.
            entries = subscriber.pending_entries
            before_viewport = self.snapshot()
            for entry in entries:
                applied = self.projection_owner.apply_committed_tap_entry(entry)
                self._bind_live_run_start_sources(
                    raw_envelopes=entry.raw_stored_envelopes,
                )
                self.capacity_owner.settle_committed_growth(
                    positive_entry_growth=_positive_entry_growth(
                        applied.ordered_segments
                    )
                )
            after_projection = self.projection_owner.snapshot()
            self._settle_pending_capacity_terminalizations(
                through_sequence=after_projection.through_authority_sequence
            )
            deadline = self._checkpoint_deadline()
            through_sequence = entries[-1].source_last_sequence
            self._pending_checkpoint_delivery = _PendingCheckpointDelivery(
                delivery_kind="live_ack",
                subscriber_id=subscriber_id,
                through_sequence=through_sequence,
                before_viewport=before_viewport,
            )
            installed = await self._run_checkpoint_owned(
                predecessor=None,
                deadline_monotonic=deadline,
            )
            await self._complete_confirmed_checkpoint_delivery(
                installed,
                deadline_monotonic=deadline,
            )
            self._reconciliation_reason = None

    async def _resolve_pending_checkpoint_delivery(self) -> None:
        delivery = self._pending_checkpoint_delivery
        if delivery is None:
            raise RuntimeError(
                "presentation checkpoint owner lacks its exact delivery receipt"
            )
        deadline = self._checkpoint_deadline()
        with self._checkpoint_lock:
            installed = self._pending_confirmed_checkpoint_install
        if installed is None:
            installed = await self._run_checkpoint_owned(
                predecessor=None,
                deadline_monotonic=deadline,
            )
        await self._complete_confirmed_checkpoint_delivery(
            installed,
            deadline_monotonic=deadline,
        )

    async def _complete_confirmed_checkpoint_delivery(
        self,
        installed: _CheckpointInstall,
        *,
        deadline_monotonic: float,
    ) -> None:
        delivery = self._pending_checkpoint_delivery
        if delivery is None:
            raise RuntimeError(
                "presentation checkpoint owner lacks its exact delivery receipt"
            )
        if not delivery.root_transition_installed:
            await self._install_and_record_transition(
                installed,
                before_viewport=delivery.before_viewport,
                deadline_monotonic=deadline_monotonic,
            )
            delivery.root_transition_installed = True
        tap = self.runtime_session.ui_committed_event_tap
        if delivery.delivery_kind == "restore_install":
            self._mark_checkpoint_delivery_complete(installed)
            self._pending_checkpoint_delivery = None
            self._reconciliation_reason = None
            return
        if delivery.subscriber_id is None:
            raise RuntimeError("tap checkpoint delivery lacks its subscriber")
        try:
            subscriber = tap.snapshot_subscriber(delivery.subscriber_id)
        except KeyError:
            subscriber = None
        if subscriber is None or subscriber.status == "gap":
            # The durable root already covers the frozen delivery cut.  A tap
            # overflow must not roll that root back or repeatedly reinstall it;
            # retire the exact delivery owner and bootstrap the missing suffix
            # from canonical rows using the tap's durable observed high-water.
            self._mark_checkpoint_delivery_complete(installed)
            self._pending_checkpoint_delivery = None
            await self._rebootstrap_after_gap(delivery.subscriber_id)
            return
        if delivery.delivery_kind == "live_ack":
            tap.acknowledge(
                delivery.subscriber_id,
                through_sequence=delivery.through_sequence,
            )
        else:
            if delivery.from_sequence_exclusive is None:
                raise RuntimeError("range checkpoint delivery lacks its base")
            tap.confirm_range_catch_up(
                delivery.subscriber_id,
                from_sequence_exclusive=delivery.from_sequence_exclusive,
                through_sequence=delivery.through_sequence,
                covered_envelopes=delivery.covered_envelopes,
            )
        self._mark_checkpoint_delivery_complete(installed)
        self._pending_checkpoint_delivery = None
        self._reconciliation_reason = None

    async def _rebootstrap_after_gap(self, subscriber_id: str) -> None:
        tap = self.runtime_session.ui_committed_event_tap
        tap.detach(subscriber_id, reason="presentation_tap_gap")
        if self._subscriber_id == subscriber_id:
            self._subscriber_id = None
        snapshot = self.projection_owner.snapshot()
        bootstrap = tap.begin_bootstrap(
            snapshot_through_sequence=snapshot.through_authority_sequence
        )
        self._subscriber_id = bootstrap.subscriber_id
        if bootstrap.status == "range_catch_up_required":
            await self._complete_range_catch_up(bootstrap)
        elif bootstrap.status == "ready":
            tap.mark_live(
                bootstrap.subscriber_id,
                through_sequence=snapshot.through_authority_sequence,
            )
        else:
            raise RuntimeError("presentation rebootstrap could not freeze a range")

    def _settle_pending_capacity_terminalizations(
        self, *, through_sequence: int
    ) -> None:
        with self._observation_lock:
            ready = tuple(
                (reservation_id, outcome)
                for reservation_id, (terminal_sequence, outcome) in (
                    self._pending_capacity_terminalizations.items()
                )
                if terminal_sequence <= through_sequence
            )
            for reservation_id, _ in ready:
                self._pending_capacity_terminalizations.pop(reservation_id, None)
        for reservation_id, outcome in ready:
            self.terminalize_ordinary_growth(reservation_id, outcome=outcome)

    def _checkpoint_current_sync(
        self,
        *,
        predecessor: PresentationHistoryProjectionCheckpointFact | None,
        deadline_monotonic: float | None,
    ) -> _CheckpointInstall:
        owner = self.runtime_session.presentation_history_checkpoint_owner
        with self._checkpoint_lock:
            pending = self._pending_checkpoint_owner
        if pending is None:
            current = predecessor or owner.read_checkpoint(
                deadline_monotonic=deadline_monotonic
            )
            if current is None:
                raise RuntimeError("presentation checkpoint predecessor disappeared")
            snapshot = self.projection_owner.snapshot()
            if (
                snapshot.through_authority_sequence
                == current.through_authority_sequence
            ):
                return _CheckpointInstall(
                    checkpoint=current, candidate=None, receipt=None
                )
            candidate, tree_artifacts, root_artifact = owner.prepare_candidate(
                snapshot=snapshot,
                predecessor=current,
                deadline_monotonic=deadline_monotonic,
            )
            attempt = owner.freeze_commit_attempt(
                candidate,
                tree_artifacts,
                root_artifact,
                self.capacity_owner.checkpoint_snapshot(
                    through_authority_sequence=snapshot.through_authority_sequence
                ),
                deadline_monotonic=deadline_monotonic,
            )
            with self._checkpoint_lock:
                if self._pending_checkpoint_owner is not None:
                    raise RuntimeError(
                        "presentation checkpoint acquired two stable owners"
                    )
                self._checkpoint_attempt_generation += 1
                pending = _PendingCheckpointOwner(
                    generation=self._checkpoint_attempt_generation,
                    attempt=attempt,
                )
                self._pending_checkpoint_owner = pending
        try:
            receipt = owner.commit_prepared_attempt(
                pending.attempt,
                deadline_monotonic=deadline_monotonic,
            )
        except BaseException:
            receipt = owner.confirm_prepared_attempt(
                pending.attempt,
                deadline_monotonic=deadline_monotonic,
            )
        with self._checkpoint_lock:
            current_pending = self._pending_checkpoint_owner
            if (
                current_pending is None
                or current_pending.generation != pending.generation
                or current_pending.attempt is not pending.attempt
            ):
                raise RuntimeError("presentation checkpoint attempt owner changed")
            current_pending.last_receipt = receipt
        if receipt.disposition != "full" or receipt.installed_checkpoint is None:
            raise PresentationCheckpointResolutionPending(receipt)
        candidate = pending.attempt.candidate
        self.projection_owner.acknowledge_checkpoint(
            through_sequence=candidate.candidate_cut.cut_through_sequence,
            projection_revision=receipt.installed_checkpoint.projection_revision,
        )
        with self._checkpoint_lock:
            current_pending = self._pending_checkpoint_owner
            if (
                current_pending is None
                or current_pending.generation != pending.generation
            ):
                raise RuntimeError("presentation checkpoint FULL lost its exact owner")
            self._pending_checkpoint_owner = None
            installed = _CheckpointInstall(
                checkpoint=receipt.installed_checkpoint,
                candidate=candidate,
                receipt=receipt,
            )
            self._pending_confirmed_checkpoint_install = installed
        return installed

    def _bind_live_run_start_sources(self, *, raw_envelopes) -> None:
        for raw in raw_envelopes:
            event = decode_raw_stored_event_envelope(raw, DEFAULT_EVENT_SCHEMA_REGISTRY)
            if not isinstance(event, RunStartEvent):
                continue
            reservation = self.capacity_owner.active_reservation_for_owner(
                owner_kind="host_run",
                owner_id=event.run_id,
            )
            if reservation is None:
                # Child ledgers and maintenance-only histories do not receive a
                # Host ordinary-growth reservation.
                if event.child_rollout_subaccount is not None:
                    continue
                raise PresentationHistoryCapacityError(
                    "committed Host RunStart lacks pre-commit history reservation"
                )
            self.capacity_owner.bind_run_start_source(
                reservation.growth_reservation_id,
                source_run_start_event_reference=event_reference_from_stored(
                    event,
                    runtime_session_id=self.runtime_session.runtime_session_id,
                ),
            )

    def _apply_restored_capacity_transitions(
        self, *, restored_range, ordered_segments
    ) -> None:
        raw_by_sequence = {
            item.sequence: item for item in restored_range.raw_stored_envelopes
        }
        for segment in ordered_segments:
            raw = raw_by_sequence[segment.through_sequence]
            event = decode_raw_stored_event_envelope(raw, DEFAULT_EVENT_SCHEMA_REGISTRY)
            if (
                isinstance(event, RunStartEvent)
                and event.child_rollout_subaccount is None
            ):
                self.capacity_owner.ensure_recovered_host_run_reservation(
                    run_id=event.run_id,
                    source_run_start_event_reference=event_reference_from_stored(
                        event,
                        runtime_session_id=self.runtime_session.runtime_session_id,
                    ),
                )
            self.capacity_owner.settle_committed_growth(
                positive_entry_growth=_positive_entry_growth((segment,))
            )
            if isinstance(event, RunEndEvent):
                reservation = self.capacity_owner.active_reservation_for_owner(
                    owner_kind="host_run",
                    owner_id=event.run_id,
                )
                if reservation is not None:
                    self.capacity_owner.terminalize(
                        reservation.growth_reservation_id,
                        outcome="settled",
                    )


def _build_root_advanced(*, before, after, candidate, receipt):
    if after.projection_revision != before.projection_revision + 1:
        raise ValueError("presentation checkpoint did not advance one client revision")
    relation = build_frozen_fact(
        PresentationHistoryRootCursorRelationFact,
        schema_version="presentation_history_root_cursor_relation.v1",
        previous_root_identity=before.active_head.confirmed_root_identity,
        resulting_root_identity=after.active_head.confirmed_root_identity,
        # Proving a full persistent-tree prefix is deliberately stricter than
        # comparing the bounded viewport.  Until that proof is present, use the
        # safe rewrite branch while retaining the immutable previous root.
        relation_kind="rewritten_generation",
        previous_cursor_disposition="retained_pinned",
        shared_prefix_entry_count=0,
        shared_prefix_accumulator=context_fingerprint(
            "presentation-history-shared-prefix:v1", ()
        ),
    )
    resident_transition = _build_resident_transition(before=before, after=after)
    cut = candidate.candidate_cut
    empty_source = context_fingerprint("presentation-history-tail-source-range:v1", ())
    empty_segments = context_fingerprint("presentation-history-tail-segments:v1", ())
    empty_mutations = context_fingerprint("presentation-history-tail-mutations:v1", ())
    return build_frozen_fact(
        PresentationHistoryRootAdvancedFact,
        schema_version="presentation_history_root_advanced.v1",
        base_projection_revision=before.projection_revision,
        resulting_projection_revision=after.projection_revision,
        previous_active_head_fingerprint=before.active_head.active_head_fingerprint,
        resulting_active_head=after.active_head,
        latest_root_cursor_pair=after.latest_root_cursor_pair,
        previous_root_relation=relation,
        resident_transition=resident_transition,
        consumed_checkpoint_candidate_cut_fingerprint=cut.candidate_cut_fingerprint,
        consumed_tail_prefix_through_sequence=cut.cut_through_sequence,
        consumed_tail_prefix_source_range_accumulator=cut.source_range_accumulator,
        consumed_tail_prefix_segment_count=cut.segment_count,
        consumed_tail_prefix_segment_accumulator=cut.segment_accumulator,
        consumed_tail_prefix_mutation_count=cut.mutation_count,
        consumed_tail_prefix_mutation_accumulator=cut.mutation_accumulator,
        retained_tail_suffix_from_sequence_exclusive=cut.cut_through_sequence,
        retained_tail_suffix_through_sequence=cut.cut_through_sequence,
        retained_tail_suffix_source_range_accumulator=empty_source,
        retained_tail_suffix_segment_count=0,
        retained_tail_suffix_segment_accumulator=empty_segments,
        retained_tail_suffix_mutation_count=0,
        retained_tail_suffix_mutation_accumulator=empty_mutations,
        checkpoint_full_confirmation_fingerprint=receipt.confirmation_fingerprint,
    )


def _positive_entry_growth(ordered_segments) -> int:
    """Count insertions, never netting retirement/removal against spent quote."""

    return sum(
        isinstance(mutation, UpsertPresentationHistoryEntryMutationFact)
        and mutation.expected_previous_entry_fingerprint is None
        for segment in ordered_segments
        for mutation in segment.ordered_mutations
    )


def _build_resident_transition(*, before, after):
    if before.resident_vector_fingerprint == after.resident_vector_fingerprint:
        return build_frozen_fact(
            ResidentEntriesUnchangedFact,
            schema_version="presentation_resident_entries_unchanged.v1",
            transition_kind="unchanged",
            before_resident_vector_fingerprint=before.resident_vector_fingerprint,
            after_resident_vector_fingerprint=after.resident_vector_fingerprint,
            exact_equivalence_proof_fingerprint=context_fingerprint(
                "presentation-resident-vector-equivalence:v1",
                tuple(
                    (
                        item.history_entry.history_entry_id,
                        item.history_entry.entry_fingerprint,
                        item.root_local_display_rank,
                    )
                    for item in after.ordered_resident_entries
                ),
            ),
        )
    before_by_id = {
        item.history_entry.history_entry_id: item
        for item in before.ordered_resident_entries
    }
    after_by_id = {
        item.history_entry.history_entry_id: item
        for item in after.ordered_resident_entries
    }
    changes = []
    for entry_id in sorted(set(before_by_id) - set(after_by_id)):
        previous = before_by_id[entry_id]
        changes.append(
            build_frozen_fact(
                PresentationHistoryResidentRemoveFact,
                schema_version="presentation_history_resident_remove.v1",
                change_kind="remove",
                history_entry_id=entry_id,
                placement_key=previous.history_entry.placement_key,
                expected_previous_entry_fingerprint=(
                    previous.history_entry.entry_fingerprint
                ),
            )
        )
    for resulting in after.ordered_resident_entries:
        entry_id = resulting.history_entry.history_entry_id
        previous = before_by_id.get(entry_id)
        if previous is not None and previous == resulting:
            continue
        changes.append(
            build_frozen_fact(
                PresentationHistoryResidentUpsertFact,
                schema_version="presentation_history_resident_upsert.v1",
                change_kind="upsert",
                history_entry_id=entry_id,
                placement_key=resulting.history_entry.placement_key,
                expected_previous_entry_fingerprint=(
                    previous.history_entry.entry_fingerprint
                    if previous is not None
                    else None
                ),
                resulting_ranked_entry=resulting,
            )
        )
    encoded_bytes = sum(
        len(canonical_json_bytes(item.model_dump(mode="json"))) for item in changes
    )
    if len(changes) <= 256 and encoded_bytes <= 1024 * 1024:
        return build_frozen_fact(
            BoundedOrderedResidentChangesFact,
            schema_version="presentation_bounded_resident_changes.v1",
            transition_kind="bounded_ordered_changes",
            before_resident_vector_fingerprint=before.resident_vector_fingerprint,
            after_resident_vector_fingerprint=after.resident_vector_fingerprint,
            ordered_changes=tuple(changes),
            change_count=len(changes),
            encoded_change_bytes=encoded_bytes,
            transition_limits_policy_fingerprint=context_fingerprint(
                "presentation-resident-transition-limits:v1",
                {"maximum_changes": 256, "maximum_encoded_bytes": 1024 * 1024},
            ),
            ordered_change_accumulator=context_fingerprint(
                "presentation-resident-change-order:v1",
                tuple(item.change_fingerprint for item in changes),
            ),
        )
    return build_frozen_fact(
        ResidentHistoryRebaseRequiredFact,
        schema_version="presentation_resident_history_rebase_required.v1",
        transition_kind="rebase_required",
        before_resident_vector_fingerprint=before.resident_vector_fingerprint,
        target_root_identity=after.active_head.confirmed_root_identity,
        target_active_head_fingerprint=after.active_head.active_head_fingerprint,
        stable_reason=(
            "RESIDENT_CHANGE_COUNT_EXCEEDED"
            if len(changes) > 256
            else "RESIDENT_CHANGE_BYTES_EXCEEDED"
        ),
        bounded_rebase_or_snapshot_token=(
            "presentation-rebase:"
            + after.active_head.active_head_fingerprint.removeprefix("sha256:")[:32]
        ),
        token_generation=after.projection_revision,
        expires_at_utc=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )


__all__ = [
    "PresentationHistoryAdmissionRejected",
    "PresentationObservationRead",
    "TerminalPresentationFoundationService",
]
