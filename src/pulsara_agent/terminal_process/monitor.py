"""Same-Host Terminal monitor coordinator.

The coordinator owns process-local registrations, cursor observations and
bounded drafts.  It never imports a repository, chooses a conversation turn,
or performs canonical I/O.  A Host scheduler may freeze an immutable attempt
by supplying a pre-minted installation target.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from threading import Condition, Lock, RLock, Thread
from time import monotonic
from typing import Callable
from uuid import uuid4

from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveBlockKind,
    LiveChannelKind,
)
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.ports.live_agent_event import (
    TerminalMonitorClosedPayload,
    TerminalMonitorObservationPayload,
    TerminalMonitorOpenedPayload,
)
from pulsara_agent.ports.terminal_observation import (
    ExistingTurnInstallation,
    NewTurnInstallation,
    PreparedInstallationTarget,
    TerminalDeliveryCoverage,
    TerminalObservationContentV1,
    TerminalObservationInstallationAttempt,
    TerminalObservationKind,
)
from pulsara_agent.terminal_process.manager import ProcessRegistry


MAXIMUM_ACTIVE_MONITORS = 8
MAXIMUM_PROGRESS_OBSERVATIONS = 119
MAXIMUM_PROGRESS_PER_WINDOW = 60
MAXIMUM_AUTONOMOUS_CONTINUATIONS = 12
MAXIMUM_MONITOR_LIFETIME_SECONDS = 36_000
_OBSERVATION_OUTPUT_HARD_BYTES = 28_000


class TerminalMonitorState(StrEnum):
    DORMANT = "DORMANT"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class TerminalMonitorCloseReason(StrEnum):
    CANCELLED = "CANCELLED"
    PROCESS_TERMINAL = "PROCESS_TERMINAL"
    EXPIRED = "EXPIRED"
    DELIVERY_BUDGET_EXHAUSTED = "DELIVERY_BUDGET_EXHAUSTED"
    HOST_CLOSE = "HOST_CLOSE"
    ORIGIN_DISCARDED = "ORIGIN_DISCARDED"


class TerminalMonitorRejectionReason(StrEnum):
    OWNER_CLOSED = "OWNER_CLOSED"
    CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"
    DUPLICATE_PROCESS_MONITOR = "DUPLICATE_PROCESS_MONITOR"
    PROCESS_NOT_FOUND = "PROCESS_NOT_FOUND"
    PROCESS_ALREADY_TERMINAL = "PROCESS_ALREADY_TERMINAL"


class TerminalMonitorRejected(RuntimeError):
    """A normal closed product rejection, not a system failure."""

    def __init__(self, reason: TerminalMonitorRejectionReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class TerminalMonitorPolicy:
    min_new_output_chars: int | None = None
    quiet_period_ms: int = 500
    heartbeat_interval_seconds: int | None = None
    max_output_chars: int = 4000
    minimum_progress_interval_seconds: int = 5
    maximum_duration_seconds: int = 36_000

    def __post_init__(self) -> None:
        if self.min_new_output_chars is not None and not (
            1 <= self.min_new_output_chars <= 65_536
        ):
            raise ValueError("monitor output threshold is invalid")
        if not 0 <= self.quiet_period_ms <= 10_000:
            raise ValueError("monitor quiet period is invalid")
        if (
            self.heartbeat_interval_seconds is not None
            and not 5 <= self.heartbeat_interval_seconds <= 1800
        ):
            raise ValueError("monitor heartbeat interval is invalid")
        if not 512 <= self.max_output_chars <= 32_000:
            raise ValueError("monitor output bound is invalid")
        if not 5 <= self.minimum_progress_interval_seconds <= 1800:
            raise ValueError("monitor progress interval is invalid")
        if not 1 <= self.maximum_duration_seconds <= MAXIMUM_MONITOR_LIFETIME_SECONDS:
            raise ValueError("monitor lifetime is invalid")


@dataclass(frozen=True, slots=True)
class MutableObservationDraft:
    monitor_id: str
    process_id: str
    draft_revision: int
    content: TerminalObservationContentV1
    retained_from_cursor: str
    through_cursor: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class PreparedTerminalMonitorRegistration:
    token_id: str
    token_fingerprint: str
    monitor_id: str
    process_id: str
    baseline_cursor: str
    expires_at: datetime


@dataclass(slots=True)
class _Registration:
    monitor_id: str
    process_id: str
    stream_id: str
    owner_epoch: str
    origin_turn_id: str
    origin_attempt_id: str
    origin_result_entry_id: str
    writer_generation: int
    authorization_reference: str
    policy: TerminalMonitorPolicy
    state: TerminalMonitorState
    baseline_cursor: str
    last_accepted_cursor: str
    opened_at_monotonic: float
    expires_at_monotonic: float
    subscription_token: str
    observation_ordinal: int = 0
    draft_revision: int = 0
    draft: MutableObservationDraft | None = None
    in_flight: TerminalObservationInstallationAttempt | None = None
    successor: MutableObservationDraft | None = None
    new_output_chars: int = 0
    last_output_monotonic: float = 0.0
    last_progress_monotonic: float = 0.0
    last_heartbeat_monotonic: float = 0.0
    progress_count: int = 0
    progress_window: list[float] = None  # type: ignore[assignment]
    autonomy_count: int = 0
    completion_pending: bool = False
    process_status: str = "running"
    exit_code: int | None = None
    output_generation: int = 0

    def __post_init__(self) -> None:
        if self.progress_window is None:
            self.progress_window = []


class TerminalMonitorCoordinator:
    def __init__(
        self,
        *,
        session_id: str,
        owner_epoch: str,
        registry: ProcessRegistry,
        live_bus: LiveAgentEventBus,
        wake_scheduler: Callable[[], None],
    ) -> None:
        self._session_id = session_id
        self._owner_epoch = owner_epoch
        self._registry = registry
        self._live_bus = live_bus
        self._wake_scheduler = wake_scheduler
        self._registrations: dict[str, _Registration] = {}
        self._tokens: dict[str, str] = {}
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._accepting = True
        self._closed = False
        self._worker = Thread(
            target=self._run,
            name=f"terminal-monitor:{session_id}",
            daemon=False,
        )
        self._worker.start()

    def prepare_registration(
        self,
        *,
        process_id: str,
        origin_turn_id: str,
        origin_attempt_id: str,
        origin_result_entry_id: str,
        writer_generation: int,
        authorization_reference: str,
        policy: TerminalMonitorPolicy,
    ) -> PreparedTerminalMonitorRegistration:
        with self._lock:
            if not self._accepting:
                raise TerminalMonitorRejected(
                    TerminalMonitorRejectionReason.OWNER_CLOSED
                )
            if (
                len(
                    [
                        item
                        for item in self._registrations.values()
                        if item.state is not TerminalMonitorState.CLOSED
                    ]
                )
                >= MAXIMUM_ACTIVE_MONITORS
            ):
                raise TerminalMonitorRejected(
                    TerminalMonitorRejectionReason.CAPACITY_EXHAUSTED
                )
            if any(
                item.process_id == process_id
                and item.state is not TerminalMonitorState.CLOSED
                for item in self._registrations.values()
            ):
                raise TerminalMonitorRejected(
                    TerminalMonitorRejectionReason.DUPLICATE_PROCESS_MONITOR
                )
        monitor_id = f"terminal-monitor:{uuid4().hex}"
        callback_gate = Lock()
        buffered_callbacks: list[tuple[bytes, int, int]] = []
        callback_active = False

        def callback(value: bytes, start: int, end: int) -> None:
            nonlocal callback_active
            with callback_gate:
                if not callback_active:
                    buffered_callbacks.append((value, start, end))
                    return
            self._output_changed(monitor_id, value)

        try:
            snapshot, subscription = self._registry.snapshot_and_subscribe(
                process_id,
                owner_host_session_id=self._owner_epoch,
                callback=callback,
            )
        except KeyError as exc:
            raise TerminalMonitorRejected(
                TerminalMonitorRejectionReason.PROCESS_NOT_FOUND
            ) from exc
        if snapshot.process_status != "running":
            self._registry.unsubscribe_output(
                process_id, subscription, owner_host_session_id=self._owner_epoch
            )
            raise TerminalMonitorRejected(
                TerminalMonitorRejectionReason.PROCESS_ALREADY_TERMINAL
            )
        now = monotonic()
        token_id = f"terminal-monitor-settlement:{uuid4().hex}"
        token_fingerprint = _fingerprint(
            "terminal-monitor-registration:v1",
            token_id,
            monitor_id,
            process_id,
            origin_attempt_id,
            origin_result_entry_id,
            str(writer_generation),
            authorization_reference,
            snapshot.output_cursor,
        )
        registration = _Registration(
            monitor_id=monitor_id,
            process_id=process_id,
            stream_id=snapshot.stream_id,
            owner_epoch=self._owner_epoch,
            origin_turn_id=origin_turn_id,
            origin_attempt_id=origin_attempt_id,
            origin_result_entry_id=origin_result_entry_id,
            writer_generation=writer_generation,
            authorization_reference=authorization_reference,
            policy=policy,
            state=TerminalMonitorState.DORMANT,
            baseline_cursor=snapshot.output_cursor,
            last_accepted_cursor=snapshot.output_cursor,
            opened_at_monotonic=now,
            expires_at_monotonic=now + policy.maximum_duration_seconds,
            subscription_token=subscription,
            last_output_monotonic=now,
            last_progress_monotonic=now,
            last_heartbeat_monotonic=now,
        )
        with self._lock:
            if not self._accepting:
                self._registry.unsubscribe_output(
                    process_id, subscription, owner_host_session_id=self._owner_epoch
                )
                raise TerminalMonitorRejected(
                    TerminalMonitorRejectionReason.OWNER_CLOSED
                )
            capacity_exhausted = (
                len(
                    [
                        item
                        for item in self._registrations.values()
                        if item.state is not TerminalMonitorState.CLOSED
                    ]
                )
                >= MAXIMUM_ACTIVE_MONITORS
            )
            duplicate = any(
                item.process_id == process_id
                and item.state is not TerminalMonitorState.CLOSED
                for item in self._registrations.values()
            )
            if capacity_exhausted or duplicate:
                self._registry.unsubscribe_output(
                    process_id, subscription, owner_host_session_id=self._owner_epoch
                )
                raise TerminalMonitorRejected(
                    TerminalMonitorRejectionReason.DUPLICATE_PROCESS_MONITOR
                    if duplicate
                    else TerminalMonitorRejectionReason.CAPACITY_EXHAUSTED
                )
            self._registrations[monitor_id] = registration
            self._tokens[token_id] = monitor_id
            self._condition.notify_all()
        # Publish the registration before releasing buffered callbacks.  Any
        # callback whose end was already included in the baseline snapshot is
        # overlap, not new output; later offsets are exact post-baseline data.
        with callback_gate:
            callback_active = True
            pending = tuple(buffered_callbacks)
            buffered_callbacks.clear()
        for value, _start, end in pending:
            if end > snapshot.through_offset:
                self._output_changed(monitor_id, value)
        # Completion can race between the running snapshot and registration
        # publication.  Re-read the same process-local owner after publication;
        # future completion callbacks now see the registration directly.
        current = self._registry.output_owner(
            process_id, owner_host_session_id=self._owner_epoch
        ).snapshot(maximum_chars=1)
        if current.process_status != "running":
            self.process_completed(
                process_id,
                status=current.process_status,
                exit_code=current.exit_code,
            )
        expires = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + policy.maximum_duration_seconds,
            tz=timezone.utc,
        )
        return PreparedTerminalMonitorRegistration(
            token_id,
            token_fingerprint,
            monitor_id,
            process_id,
            snapshot.output_cursor,
            expires,
        )

    def settle_registration(
        self, token_id: str, token_fingerprint: str, *, committed: bool
    ) -> None:
        with self._lock:
            monitor_id = self._tokens.get(token_id)
            registration = (
                None if monitor_id is None else self._registrations.get(monitor_id)
            )
            if registration is None:
                return
            expected = _fingerprint(
                "terminal-monitor-registration:v1",
                token_id,
                registration.monitor_id,
                registration.process_id,
                registration.origin_attempt_id,
                registration.origin_result_entry_id,
                str(registration.writer_generation),
                registration.authorization_reference,
                registration.baseline_cursor,
            )
            if expected != token_fingerprint:
                raise RuntimeError("terminal monitor settlement token conflicts")
            self._tokens.pop(token_id, None)
            if registration.state is not TerminalMonitorState.DORMANT:
                return
            if committed:
                registration.state = TerminalMonitorState.ACTIVE
                self._offer_opened(registration)
                if registration.completion_pending:
                    self._condition.notify_all()
                return
            self._close_locked(
                registration, TerminalMonitorCloseReason.ORIGIN_DISCARDED
            )

    def list_current(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(
                {
                    "monitor_id": item.monitor_id,
                    "process_id": item.process_id,
                    "state": item.state.value,
                    "observation_ordinal": item.observation_ordinal,
                    "pending": item.draft is not None or item.in_flight is not None,
                }
                for item in sorted(
                    self._registrations.values(), key=lambda value: value.monitor_id
                )
                if item.state is not TerminalMonitorState.CLOSED
            )

    def cancel(self, monitor_id: str) -> str:
        with self._lock:
            registration = self._registrations.get(monitor_id)
            if registration is None:
                return "not_found"
            if registration.state is TerminalMonitorState.CLOSED:
                return "already_terminal"
            self._close_locked(registration, TerminalMonitorCloseReason.CANCELLED)
            return "cancelled"

    def process_completed(
        self, process_id: str, *, status: str, exit_code: int | None
    ) -> None:
        with self._lock:
            for registration in self._registrations.values():
                if (
                    registration.process_id != process_id
                    or registration.state is TerminalMonitorState.CLOSED
                ):
                    continue
                registration.process_status = status
                registration.exit_code = exit_code
                registration.completion_pending = True
            self._condition.notify_all()

    def freeze(
        self,
        *,
        monitor_id: str,
        target: PreparedInstallationTarget,
        workspace_id: str,
        writer_generation: int,
        actor_id: str,
    ) -> TerminalObservationInstallationAttempt | None:
        with self._lock:
            registration = self._registrations.get(monitor_id)
            if (
                registration is None
                or registration.state is not TerminalMonitorState.ACTIVE
            ):
                return None
            if registration.writer_generation != writer_generation:
                return None
            if registration.in_flight is not None or registration.draft is None:
                return None
            draft = registration.draft
            digest = f"sha256:{sha256(draft.content.canonical_bytes()).hexdigest()}"
            occurred_at = datetime.now(timezone.utc)
            fingerprint = _fingerprint(
                "terminal-observation-installation:v1",
                self._session_id,
                workspace_id,
                str(writer_generation),
                registration.origin_turn_id,
                digest,
                draft.through_cursor,
                repr(target),
            )
            attempt = TerminalObservationInstallationAttempt(
                session_id=self._session_id,
                workspace_id=workspace_id,
                writer_generation=writer_generation,
                origin_turn_id=registration.origin_turn_id,
                content=draft.content,
                content_digest=digest,
                retained_from_cursor=draft.retained_from_cursor,
                through_cursor=draft.through_cursor,
                target=target,
                occurred_at=occurred_at,
                actor_id=actor_id,
                candidate_fingerprint=fingerprint,
            )
            registration.in_flight = attempt
            registration.draft = None
            return attempt

    def current_installation_attempt(
        self, monitor_id: str
    ) -> TerminalObservationInstallationAttempt | None:
        """Expose the immutable in-flight attempt for exact re-drive only."""

        with self._lock:
            registration = self._registrations.get(monitor_id)
            if registration is None:
                return None
            # Cancel only wins against a mutable draft.  Once freeze installed
            # an immutable candidate, a CLOSED registration keeps that exact
            # process-local owner until canonical FULL/NONE/conflict settles
            # it.  This is the ACK-unknown confirmation seam, not a retry
            # queue or durable recovery owner.
            return registration.in_flight

    def settle_installation(
        self,
        attempt: TerminalObservationInstallationAttempt,
        *,
        accepted: bool,
    ) -> None:
        with self._lock:
            registration = self._registrations.get(attempt.content.monitor_id)
            if registration is None or registration.in_flight != attempt:
                raise RuntimeError("terminal observation installation is stale")
            registration.in_flight = None
            if accepted:
                registration.last_accepted_cursor = attempt.through_cursor
                registration.observation_ordinal = attempt.content.observation_ordinal
                registration.autonomy_count += 1
                if registration.state is TerminalMonitorState.CLOSED:
                    return
                if attempt.content.observation_kind in {
                    TerminalObservationKind.COMPLETION,
                    TerminalObservationKind.EXPIRY,
                }:
                    reason = (
                        TerminalMonitorCloseReason.PROCESS_TERMINAL
                        if attempt.content.observation_kind
                        is TerminalObservationKind.COMPLETION
                        else TerminalMonitorCloseReason.EXPIRED
                    )
                    self._close_locked(registration, reason)
                    return
                if registration.autonomy_count >= MAXIMUM_AUTONOMOUS_CONTINUATIONS:
                    self._close_locked(
                        registration,
                        TerminalMonitorCloseReason.DELIVERY_BUDGET_EXHAUSTED,
                    )
                    return
            else:
                # A proven NONE/conflict may expose the same immutable content
                # again; UNKNOWN callers must not invoke this settlement.  A
                # successor was cut strictly after this attempt's through
                # cursor, so it cannot replace the rejected predecessor.  It
                # stays queued until that exact predecessor is accepted.
                if registration.state is TerminalMonitorState.CLOSED:
                    return
                registration.draft = MutableObservationDraft(
                    monitor_id=registration.monitor_id,
                    process_id=registration.process_id,
                    draft_revision=registration.draft_revision + 1,
                    content=attempt.content,
                    retained_from_cursor=attempt.retained_from_cursor,
                    through_cursor=attempt.through_cursor,
                    observed_at=attempt.occurred_at,
                )
                registration.draft_revision += 1
            if accepted and registration.successor is not None:
                registration.draft = registration.successor
                registration.successor = None
            if registration.draft is not None:
                self._wake_scheduler()

    def pending_monitor_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                item.monitor_id
                for item in self._registrations.values()
                if (
                    item.in_flight is not None
                    or (
                        item.state is TerminalMonitorState.ACTIVE
                        and item.draft is not None
                    )
                )
            )

    def pending_observation_id(self, monitor_id: str) -> str | None:
        """Return the stable draft identity without transferring ownership."""

        with self._lock:
            registration = self._registrations.get(monitor_id)
            if registration is None:
                return None
            if registration.in_flight is not None:
                return registration.in_flight.content.observation_id
            if registration.state is not TerminalMonitorState.ACTIVE:
                return None
            if registration.draft is None:
                return None
            return registration.draft.content.observation_id

    def stop_admission_and_close(self, *, timeout_seconds: float) -> None:
        with self._lock:
            self._accepting = False
            for registration in tuple(self._registrations.values()):
                if registration.state is not TerminalMonitorState.CLOSED:
                    self._close_locked(
                        registration, TerminalMonitorCloseReason.HOST_CLOSE
                    )
            self._closed = True
            self._condition.notify_all()
        self._worker.join(timeout=timeout_seconds)
        if self._worker.is_alive():
            raise TimeoutError("terminal monitor coordinator did not physically join")

    def _output_changed(self, monitor_id: str, value: bytes) -> None:
        with self._lock:
            registration = self._registrations.get(monitor_id)
            if (
                registration is None
                or registration.state is TerminalMonitorState.CLOSED
            ):
                return
            registration.new_output_chars += len(value.decode("utf-8"))
            registration.output_generation += 1
            registration.last_output_monotonic = monotonic()
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
                self._condition.wait(timeout=0.05)
                if self._closed:
                    return
                registrations = tuple(self._registrations.values())
            now = monotonic()
            for registration in registrations:
                self._evaluate(registration.monitor_id, now)

    def _evaluate(self, monitor_id: str, now: float) -> None:
        with self._lock:
            registration = self._registrations.get(monitor_id)
            if (
                registration is None
                or registration.state is TerminalMonitorState.CLOSED
            ):
                return
            if registration.state is TerminalMonitorState.DORMANT:
                return
            kind: TerminalObservationKind | None = None
            if registration.completion_pending:
                kind = TerminalObservationKind.COMPLETION
            elif now >= registration.expires_at_monotonic:
                kind = TerminalObservationKind.EXPIRY
            elif (
                registration.policy.min_new_output_chars is not None
                and registration.new_output_chars
                >= registration.policy.min_new_output_chars
                and now - registration.last_output_monotonic
                >= registration.policy.quiet_period_ms / 1000
                and now - registration.last_progress_monotonic
                >= registration.policy.minimum_progress_interval_seconds
            ):
                kind = TerminalObservationKind.PROGRESS
            elif (
                registration.policy.heartbeat_interval_seconds is not None
                and now - registration.last_heartbeat_monotonic
                >= registration.policy.heartbeat_interval_seconds
            ):
                kind = TerminalObservationKind.HEARTBEAT
            if kind is None:
                return
            if kind is TerminalObservationKind.PROGRESS:
                registration.progress_window = [
                    item for item in registration.progress_window if now - item <= 600
                ]
                if (
                    registration.progress_count >= MAXIMUM_PROGRESS_OBSERVATIONS
                    or len(registration.progress_window) >= MAXIMUM_PROGRESS_PER_WINDOW
                ):
                    self._close_locked(
                        registration,
                        TerminalMonitorCloseReason.DELIVERY_BUDGET_EXHAUSTED,
                    )
                    return
            process_id = registration.process_id
            policy = registration.policy
            base_cursor = (
                registration.in_flight.through_cursor
                if registration.in_flight is not None
                else registration.last_accepted_cursor
            )
            ordinal = (
                registration.in_flight.content.observation_ordinal + 1
                if registration.in_flight is not None
                else registration.observation_ordinal + 1
            )
            evaluation_identity = (
                None
                if registration.in_flight is None
                else registration.in_flight.candidate_fingerprint,
                registration.last_accepted_cursor,
                registration.observation_ordinal,
                registration.draft_revision,
                registration.output_generation,
                registration.completion_pending,
                registration.process_status,
                registration.exit_code,
            )
            process_status = registration.process_status
            exit_code = registration.exit_code
        try:
            snapshot = self._registry.observation_slice(
                process_id,
                maximum_chars=policy.max_output_chars,
                maximum_bytes=_OBSERVATION_OUTPUT_HARD_BYTES,
                owner_host_session_id=self._owner_epoch,
                since_cursor=base_cursor,
            )
        except (KeyError, ValueError):
            with self._lock:
                current = self._registrations.get(monitor_id)
                if (
                    current is not None
                    and current.state is not TerminalMonitorState.CLOSED
                ):
                    self._close_locked(
                        current, TerminalMonitorCloseReason.PROCESS_TERMINAL
                    )
            return
        content = _build_content(
            monitor_id=monitor_id,
            process_id=process_id,
            ordinal=ordinal,
            kind=kind,
            status=process_status,
            exit_code=exit_code,
            output_disposition=snapshot.disposition.value,
            gap=snapshot.gap_before_output,
            source=snapshot.text,
            available_source_utf8_bytes=snapshot.available_source_utf8_bytes,
            included_source_utf8_bytes=snapshot.included_source_utf8_bytes,
            omitted_by_delivery_bound_utf8_bytes=(
                snapshot.omitted_by_delivery_bound_utf8_bytes
            ),
        )
        draft = MutableObservationDraft(
            monitor_id=monitor_id,
            process_id=registration.process_id,
            draft_revision=registration.draft_revision + 1,
            content=content,
            retained_from_cursor=snapshot.retained_from_cursor,
            through_cursor=snapshot.output_cursor,
            observed_at=datetime.now(timezone.utc),
        )
        with self._lock:
            current = self._registrations.get(monitor_id)
            if current is None or current.state is not TerminalMonitorState.ACTIVE:
                return
            current_identity = (
                None
                if current.in_flight is None
                else current.in_flight.candidate_fingerprint,
                current.last_accepted_cursor,
                current.observation_ordinal,
                current.draft_revision,
                current.output_generation,
                current.completion_pending,
                current.process_status,
                current.exit_code,
            )
            if current_identity != evaluation_identity:
                # The storage read deliberately runs outside the coordinator
                # lock.  Freeze/settlement/completion may have advanced its
                # exact base meanwhile, so discard this stale range and read
                # again from the new authoritative cursor.
                self._condition.notify_all()
                return
            current.draft_revision += 1
            if current.in_flight is not None:
                current.successor = _coalesce(current.successor, draft)
            else:
                current.draft = _coalesce(current.draft, draft)
            current.new_output_chars = 0
            if kind is TerminalObservationKind.PROGRESS:
                current.progress_count += 1
                current.progress_window.append(now)
                current.last_progress_monotonic = now
            if kind is TerminalObservationKind.HEARTBEAT:
                current.last_heartbeat_monotonic = now
            if kind is TerminalObservationKind.COMPLETION:
                current.completion_pending = False
            self._offer_observation(
                current, content, source_digest=snapshot.source_digest
            )
        self._wake_scheduler()

    def _close_locked(
        self, registration: _Registration, reason: TerminalMonitorCloseReason
    ) -> None:
        if registration.state is TerminalMonitorState.CLOSED:
            return
        registration.state = TerminalMonitorState.CLOSED
        registration.draft = None
        registration.successor = None
        self._registry.unsubscribe_output(
            registration.process_id,
            registration.subscription_token,
            owner_host_session_id=self._owner_epoch,
        )
        self._live_bus.offer_nowait(
            event_type=LiveEventType.TERMINAL_MONITOR_CLOSED,
            session_id=self._session_id,
            turn_id=registration.origin_turn_id,
            draft_identity=registration.monitor_id,
            payload=TerminalMonitorClosedPayload(
                registration.monitor_id, registration.process_id, reason.value
            ),
            channel_kind=LiveChannelKind.TERMINAL_EXTENSION,
            generation_id=registration.monitor_id,
            block_id=registration.monitor_id,
            block_kind=LiveBlockKind.OPERATIONAL,
        )

    def _offer_opened(self, registration: _Registration) -> None:
        self._live_bus.offer_nowait(
            event_type=LiveEventType.TERMINAL_MONITOR_OPENED,
            session_id=self._session_id,
            turn_id=registration.origin_turn_id,
            draft_identity=registration.monitor_id,
            payload=TerminalMonitorOpenedPayload(
                registration.monitor_id, registration.process_id
            ),
            channel_kind=LiveChannelKind.TERMINAL_EXTENSION,
            generation_id=registration.monitor_id,
            block_id=registration.monitor_id,
            block_kind=LiveBlockKind.OPERATIONAL,
        )

    def _offer_observation(
        self,
        registration: _Registration,
        content: TerminalObservationContentV1,
        *,
        source_digest: str,
    ) -> None:
        self._live_bus.offer_nowait(
            event_type=LiveEventType.TERMINAL_MONITOR_OBSERVATION,
            session_id=self._session_id,
            turn_id=registration.origin_turn_id,
            draft_identity=registration.monitor_id,
            payload=TerminalMonitorObservationPayload(
                monitor_id=registration.monitor_id,
                process_id=registration.process_id,
                observation_kind=content.observation_kind.value,
                public_preview=content.output,
                complete_utf8_bytes=content.available_source_utf8_bytes,
                complete_digest=source_digest,
            ),
            channel_kind=LiveChannelKind.TERMINAL_EXTENSION,
            generation_id=registration.monitor_id,
            block_id=registration.monitor_id,
            block_kind=LiveBlockKind.OPERATIONAL,
        )


def _build_content(
    *,
    monitor_id: str,
    process_id: str,
    ordinal: int,
    kind: TerminalObservationKind,
    status: str,
    exit_code: int | None,
    output_disposition: str,
    gap: bool,
    source: str,
    available_source_utf8_bytes: int,
    included_source_utf8_bytes: int,
    omitted_by_delivery_bound_utf8_bytes: int,
) -> TerminalObservationContentV1:
    observation_id = f"terminal-observation:{uuid4().hex}"
    return TerminalObservationContentV1(
        observation_id=observation_id,
        monitor_id=monitor_id,
        process_id=process_id,
        observation_ordinal=ordinal,
        observation_kind=kind,
        process_status=status,
        exit_code=exit_code,
        output_disposition=output_disposition,
        gap_before_output=gap,
        delivery_coverage=(
            TerminalDeliveryCoverage.HEAD_TAIL
            if omitted_by_delivery_bound_utf8_bytes
            else TerminalDeliveryCoverage.COMPLETE
        ),
        available_source_utf8_bytes=available_source_utf8_bytes,
        included_source_utf8_bytes=included_source_utf8_bytes,
        omitted_by_delivery_bound_utf8_bytes=(omitted_by_delivery_bound_utf8_bytes),
        output=source,
    )


def _coalesce(
    current: MutableObservationDraft | None, candidate: MutableObservationDraft
) -> MutableObservationDraft:
    if current is None:
        return candidate
    if candidate.content.observation_kind is TerminalObservationKind.COMPLETION:
        return candidate
    if current.content.observation_kind is TerminalObservationKind.COMPLETION:
        return current
    return candidate


def _fingerprint(namespace: str, *parts: str) -> str:
    return f"sha256:{sha256((namespace + chr(0) + chr(0).join(parts)).encode()).hexdigest()}"


__all__ = [
    "ExistingTurnInstallation",
    "MAXIMUM_ACTIVE_MONITORS",
    "MutableObservationDraft",
    "NewTurnInstallation",
    "PreparedInstallationTarget",
    "PreparedTerminalMonitorRegistration",
    "TerminalDeliveryCoverage",
    "TerminalMonitorCoordinator",
    "TerminalMonitorPolicy",
    "TerminalMonitorRejected",
    "TerminalMonitorRejectionReason",
    "TerminalMonitorState",
    "TerminalObservationContentV1",
    "TerminalObservationInstallationAttempt",
    "TerminalObservationKind",
]
