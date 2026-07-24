"""Durable terminal notification projection and reservation ownership.

The notification account is ledger-derived: every capacity mutation carries
the exact before/after account and projection states.  Candidate factories run
under RuntimeSession's writer serialization and the reducer independently
replays the same transitions after restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from threading import RLock
from typing import TYPE_CHECKING, Iterable, Literal, Sequence

from pulsara_agent.event import (
    AgentEvent,
    ModelCallStartEvent,
    RunStartEvent,
    TerminalNotificationReservationCreatedEvent,
    TerminalNotificationReservationReleasedEvent,
    TerminalProcessCompletedEvent,
    TerminalProcessMonitorObservationCommittedEvent,
    TerminalProcessMonitorReceiptAppliedEvent,
    TerminalProcessMonitorRegisteredEvent,
    TerminalProcessMonitorTerminatedEvent,
    TerminalProcessObservationDeliveryDeferredEvent,
    TerminalProcessObservationDeliveryDispositionEvent,
    ToolResultEndEvent,
)
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.event_log.serialization import dump_agent_event, load_agent_event
from pulsara_agent.llm.user_carrier import (
    encode_runtime_observation,
    terminal_monitor_observation_payload,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import StableEventIdentityFact, build_frozen_fact
from pulsara_agent.primitives.host_ingress import (
    HostActiveRunMonitorDeliveryFact,
    HostRuntimeNotificationAttachmentFact,
    RuntimeRequestRunIngressFact,
)
from pulsara_agent.primitives.terminal_observation import (
    HostIngressNotificationProjectionStateFact,
    TerminalMonitorNotificationHeadFact,
    TerminalAutonomousDeliveryChainAttributionFact,
    TerminalAutonomyChainStateFact,
    TerminalNotificationAccountTransitionFact,
    TerminalNotificationProcessHeadFact,
    TerminalNotificationReservationAccountStateFact,
    TerminalNotificationReservationFact,
    TerminalOutputDeltaSemanticFact,
    TerminalOutputStreamIdentityFact,
    TerminalProcessLifecycleOutcomeFact,
    TerminalProcessMonitorCompletionObservationSemanticFact,
    UnavailableRecoveredTerminalOutputDeltaFact,
    terminal_receipt_dominates_observation,
)
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored

if TYPE_CHECKING:
    from pulsara_agent.runtime.terminal.process import TerminalProcessState


NOTIFICATION_REDUCER_CONTRACT_FINGERPRINT = context_fingerprint(
    "terminal-notification-projection-reducer:v1",
    {
        "account": "ledger-derived-cas",
        "process_heads": "stream-identity-ordered",
        "pending_progress": "one-per-monitor",
        "completion": "lifecycle-authority-only",
    },
)
TERMINAL_NOTIFICATION_CHECKPOINT_KIND = "terminal_notification_projection.v1"
TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION = (
    "terminal_notification_projection_checkpoint.v2"
)
DEFAULT_MAXIMUM_COMPLETION_PROCESS_HEADS = 8
DEFAULT_MAXIMUM_ACTIVE_MONITOR_SLOTS = 8


class TerminalNotificationContractError(RuntimeError):
    """A notification candidate does not follow the canonical state machine."""


class TerminalNotificationAdmissionStale(TerminalNotificationContractError):
    """A valid delivery candidate lost its mutable notification CAS."""


class TerminalNotificationCapacityError(RuntimeError):
    """A background process or monitor cannot acquire its durable slot."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PreparedTerminalNotificationReservation:
    reservation: TerminalNotificationReservationFact
    expected_account_revision: int
    expected_account_state_fingerprint: str


@dataclass(frozen=True, slots=True)
class PendingTerminalNotification:
    source_event: (
        TerminalProcessCompletedEvent | TerminalProcessMonitorObservationCommittedEvent
    )
    process_head_fingerprint: str
    attachment: HostRuntimeNotificationAttachmentFact
    wake_chain_id: str | None


@dataclass(frozen=True, slots=True)
class TerminalAutonomyChainSnapshot:
    attribution: TerminalAutonomousDeliveryChainAttributionFact
    state: TerminalAutonomyChainStateFact


@dataclass(slots=True)
class _LocalReservationOwner:
    prepared: PreparedTerminalNotificationReservation
    process: TerminalProcessState | None
    owner_state: Literal[
        "prepared", "candidate_frozen", "committed", "reconciliation_required"
    ] = "prepared"


def initial_notification_account_state(
    runtime_session_id: str,
    *,
    maximum_completion_process_heads: int = DEFAULT_MAXIMUM_COMPLETION_PROCESS_HEADS,
    maximum_active_monitor_slots: int = DEFAULT_MAXIMUM_ACTIVE_MONITOR_SLOTS,
) -> TerminalNotificationReservationAccountStateFact:
    return build_frozen_fact(
        TerminalNotificationReservationAccountStateFact,
        schema_version="terminal_notification_reservation_account_state.v1",
        ledger_runtime_session_id=runtime_session_id,
        account_revision=0,
        maximum_completion_process_heads=maximum_completion_process_heads,
        maximum_active_monitor_slots=maximum_active_monitor_slots,
        active_completion_reservations=(),
        active_monitor_reservations=(),
        latest_transition_event_id=None,
    )


def initial_notification_projection_state(
    runtime_session_id: str,
    account: TerminalNotificationReservationAccountStateFact,
) -> HostIngressNotificationProjectionStateFact:
    return build_frozen_fact(
        HostIngressNotificationProjectionStateFact,
        schema_version="host_ingress_notification_projection_state.v1",
        ledger_runtime_session_id=runtime_session_id,
        source_through_sequence=0,
        process_heads=(),
        reservation_account_revision=account.account_revision,
        reservation_account_state_fingerprint=account.state_fingerprint,
        reducer_contract_fingerprint=NOTIFICATION_REDUCER_CONTRACT_FINGERPRINT,
    )


class HostIngressNotificationProjectionStore:
    """Single exact reducer for account balance and pending notifications."""

    def __init__(
        self,
        events: Sequence[AgentEvent] = (),
        *,
        runtime_session_id: str,
        maximum_completion_process_heads: int = (
            DEFAULT_MAXIMUM_COMPLETION_PROCESS_HEADS
        ),
        maximum_active_monitor_slots: int = DEFAULT_MAXIMUM_ACTIVE_MONITOR_SLOTS,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self._maximum_completion_process_heads = maximum_completion_process_heads
        self._maximum_active_monitor_slots = maximum_active_monitor_slots
        self._lock = RLock()
        self.through_sequence = 0
        self._account = initial_notification_account_state(
            runtime_session_id,
            maximum_completion_process_heads=maximum_completion_process_heads,
            maximum_active_monitor_slots=maximum_active_monitor_slots,
        )
        self._projection = initial_notification_projection_state(
            runtime_session_id, self._account
        )
        self._observation_events: dict[
            str, TerminalProcessMonitorObservationCommittedEvent
        ] = {}
        self._registration_events: dict[str, TerminalProcessMonitorRegisteredEvent] = {}
        self._completion_events: dict[str, TerminalProcessCompletedEvent] = {}
        self._tool_result_by_identity: dict[str, ToolResultEndEvent] = {}
        self._automatic_delivery_deferred_source_ids: set[str] = set()
        self._autonomy_chain_attributions: dict[
            str, TerminalAutonomousDeliveryChainAttributionFact
        ] = {}
        self._autonomy_chain_states: dict[str, TerminalAutonomyChainStateFact] = {}
        if events:
            self.apply_committed(tuple(events))

    @classmethod
    def canonical_checkpoint_genesis_payload(
        cls,
        *,
        runtime_session_id: str,
    ) -> dict[str, object]:
        """Return the only valid sequence-zero checkpoint basis."""

        return cls(runtime_session_id=runtime_session_id).checkpoint_payload()

    @classmethod
    def from_checkpoint_payload(
        cls,
        payload: dict[str, object],
        *,
        runtime_session_id: str,
    ) -> "HostIngressNotificationProjectionStore":
        if (
            payload.get("schema_version")
            != TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION
        ):
            raise TerminalNotificationContractError(
                "terminal notification checkpoint schema is unsupported"
            )
        if payload.get("runtime_session_id") != runtime_session_id:
            raise TerminalNotificationContractError(
                "terminal notification checkpoint runtime identity drifted"
            )
        store = cls(runtime_session_id=runtime_session_id)
        account = TerminalNotificationReservationAccountStateFact.model_validate(
            payload.get("account")
        )
        projection = HostIngressNotificationProjectionStateFact.model_validate(
            payload.get("projection")
        )
        through_sequence = int(payload.get("through_sequence", -1))
        if (
            through_sequence < 0
            or account.ledger_runtime_session_id != runtime_session_id
            or projection.ledger_runtime_session_id != runtime_session_id
            or projection.reservation_account_revision != account.account_revision
            or projection.reservation_account_state_fingerprint
            != account.state_fingerprint
            or projection.source_through_sequence > through_sequence
        ):
            raise TerminalNotificationContractError(
                "terminal notification checkpoint state join failed"
            )

        def events(key: str, expected_type):
            decoded = tuple(load_agent_event(item) for item in payload.get(key, ()))
            if any(not isinstance(item, expected_type) for item in decoded):
                raise TerminalNotificationContractError(
                    f"terminal notification checkpoint {key} type drifted"
                )
            return decoded

        observations = events(
            "observation_events", TerminalProcessMonitorObservationCommittedEvent
        )
        registrations = events(
            "registration_events", TerminalProcessMonitorRegisteredEvent
        )
        completions = events("completion_events", TerminalProcessCompletedEvent)
        attributions = tuple(
            TerminalAutonomousDeliveryChainAttributionFact.model_validate(item)
            for item in payload.get("autonomy_chain_attributions", ())
        )
        chain_states = tuple(
            TerminalAutonomyChainStateFact.model_validate(item)
            for item in payload.get("autonomy_chain_states", ())
        )
        with store._lock:
            store.through_sequence = through_sequence
            store._account = account
            store._projection = projection
            store._observation_events = {item.id: item for item in observations}
            store._registration_events = {item.id: item for item in registrations}
            store._completion_events = {item.id: item for item in completions}
            store._automatic_delivery_deferred_source_ids = set(
                str(item)
                for item in payload.get("automatic_delivery_deferred_source_ids", ())
            )
            store._autonomy_chain_attributions = {
                item.wake_chain_id: item for item in attributions
            }
            store._autonomy_chain_states = {
                item.wake_chain_id: item for item in chain_states
            }
            store._validate_checkpoint_references()
        return store

    def checkpoint_payload(self) -> dict[str, object]:
        with self._lock:
            observation_ids = {
                monitor.pending_observation_event_reference.event_id
                for head in self._projection.process_heads
                for monitor in head.monitor_heads
                if monitor.pending_observation_event_reference is not None
            }
            completion_ids = {
                head.latest_completion_event_reference.event_id
                for head in self._projection.process_heads
                if head.latest_completion_event_reference is not None
            }
            registration_ids = {
                monitor.registration_event_identity.event_id
                for head in self._projection.process_heads
                for monitor in head.monitor_heads
            }
            registrations = tuple(
                self._registration_events[item]
                for item in sorted(registration_ids)
                if item in self._registration_events
            )
            chain_ids = {
                item.registration_attribution.wake_chain.wake_chain_id
                for item in registrations
            }
            return {
                "schema_version": TERMINAL_NOTIFICATION_CHECKPOINT_SCHEMA_VERSION,
                "runtime_session_id": self.runtime_session_id,
                "through_sequence": self.through_sequence,
                "account": self._account.model_dump(mode="json"),
                "projection": self._projection.model_dump(mode="json"),
                "observation_events": tuple(
                    dump_agent_event(self._observation_events[item])
                    for item in sorted(observation_ids)
                ),
                "registration_events": tuple(
                    dump_agent_event(item) for item in registrations
                ),
                "completion_events": tuple(
                    dump_agent_event(self._completion_events[item])
                    for item in sorted(completion_ids)
                ),
                "automatic_delivery_deferred_source_ids": tuple(
                    sorted(
                        self._automatic_delivery_deferred_source_ids & observation_ids
                    )
                ),
                "autonomy_chain_attributions": tuple(
                    self._autonomy_chain_attributions[item].model_dump(mode="json")
                    for item in sorted(chain_ids)
                ),
                "autonomy_chain_states": tuple(
                    self._autonomy_chain_states[item].model_dump(mode="json")
                    for item in sorted(chain_ids)
                ),
            }

    def _validate_checkpoint_references(self) -> None:
        through_sequence = self.through_sequence
        pending_observation_ids = {
            monitor.pending_observation_event_reference.event_id
            for head in self._projection.process_heads
            for monitor in head.monitor_heads
            if monitor.pending_observation_event_reference is not None
        }
        completion_ids = {
            head.latest_completion_event_reference.event_id
            for head in self._projection.process_heads
            if head.latest_completion_event_reference is not None
        }
        registration_ids = {
            monitor.registration_event_identity.event_id
            for head in self._projection.process_heads
            for monitor in head.monitor_heads
        }
        if (
            pending_observation_ids != set(self._observation_events)
            or completion_ids != set(self._completion_events)
            or registration_ids != set(self._registration_events)
            or set(self._autonomy_chain_attributions)
            != set(self._autonomy_chain_states)
        ):
            raise TerminalNotificationContractError(
                "terminal notification checkpoint references are incomplete"
            )

        heads_by_stream = {
            item.stream_identity.stream_identity_fingerprint: item
            for item in self._projection.process_heads
        }
        completion_reservations = {
            item.stream_identity.stream_identity_fingerprint: item
            for item in self._account.active_completion_reservations
        }
        if set(heads_by_stream) != set(completion_reservations):
            raise TerminalNotificationContractError(
                "terminal notification checkpoint completion account/head join failed"
            )
        monitor_reservations = {
            item.monitor_id: item for item in self._account.active_monitor_reservations
        }
        monitor_heads = {
            monitor.monitor_id: (head, monitor)
            for head in self._projection.process_heads
            for monitor in head.monitor_heads
        }
        if None in monitor_reservations or set(monitor_heads) != set(
            monitor_reservations
        ):
            raise TerminalNotificationContractError(
                "terminal notification checkpoint monitor account/head join failed"
            )
        for monitor_id, reservation in monitor_reservations.items():
            head, monitor = monitor_heads[monitor_id]
            if reservation.stream_identity != head.stream_identity:
                raise TerminalNotificationContractError(
                    "terminal notification checkpoint monitor stream drifted"
                )
            registration = self._registration_events[
                monitor.registration_event_identity.event_id
            ]
            if (
                registration.sequence is None
                or registration.sequence > through_sequence
                or stable_event_identity(
                    registration,
                    runtime_session_id=self.runtime_session_id,
                )
                != monitor.registration_event_identity
            ):
                raise TerminalNotificationContractError(
                    "terminal notification checkpoint registration authority drifted"
                )
            pending_ref = monitor.pending_observation_event_reference
            if pending_ref is None:
                continue
            pending = self._observation_events[pending_ref.event_id]
            output = pending.observation.output_authority
            if isinstance(output, TerminalOutputDeltaSemanticFact):
                end_cursor = output.end_cursor
            elif isinstance(output, UnavailableRecoveredTerminalOutputDeltaFact):
                end_cursor = output.terminal_cursor
            else:  # pragma: no cover - the durable union is closed
                raise TypeError(type(output))
            if (
                pending.sequence is None
                or pending.sequence > through_sequence
                or event_reference_from_stored(
                    pending,
                    runtime_session_id=self.runtime_session_id,
                )
                != pending_ref
                or pending.observation.monitor_id != monitor_id
                or pending.monitor_state_transition.after_core_state_fingerprint
                != monitor.monitor_core_state_fingerprint
                or pending.observation.observation_ordinal
                != monitor.last_committed_observation_ordinal
                or end_cursor.cursor_fingerprint
                != monitor.last_observation_cursor_fingerprint
            ):
                raise TerminalNotificationContractError(
                    "terminal notification checkpoint pending monitor head drifted"
                )
        for stream_fingerprint, head in heads_by_stream.items():
            completion_ref = head.latest_completion_event_reference
            if completion_ref is None:
                continue
            completion = self._completion_events[completion_ref.event_id]
            if (
                completion.sequence is None
                or completion.sequence > through_sequence
                or event_reference_from_stored(
                    completion,
                    runtime_session_id=self.runtime_session_id,
                )
                != completion_ref
                or completion.terminal_output_cursor.stream_identity.stream_identity_fingerprint
                != stream_fingerprint
                or (
                    head.pending_completion_without_monitor_reference is not None
                    and head.pending_completion_without_monitor_reference
                    != completion_ref
                )
            ):
                raise TerminalNotificationContractError(
                    "terminal notification checkpoint completion head drifted"
                )

    def rebuild(self, events: tuple[AgentEvent, ...]) -> None:
        with self._lock:
            self.through_sequence = 0
            self._account = initial_notification_account_state(
                self.runtime_session_id,
                maximum_completion_process_heads=(
                    self._maximum_completion_process_heads
                ),
                maximum_active_monitor_slots=self._maximum_active_monitor_slots,
            )
            self._projection = initial_notification_projection_state(
                self.runtime_session_id, self._account
            )
            self._observation_events.clear()
            self._registration_events.clear()
            self._completion_events.clear()
            self._tool_result_by_identity.clear()
            self._automatic_delivery_deferred_source_ids.clear()
            self._autonomy_chain_attributions.clear()
            self._autonomy_chain_states.clear()
        self.apply_committed(events)

    def account_snapshot(self) -> TerminalNotificationReservationAccountStateFact:
        with self._lock:
            return self._account

    def projection_snapshot(self) -> HostIngressNotificationProjectionStateFact:
        with self._lock:
            return self._projection

    def pending_observations(
        self,
    ) -> tuple[TerminalProcessMonitorObservationCommittedEvent, ...]:
        with self._lock:
            event_ids = tuple(
                monitor.pending_observation_event_reference.event_id
                for head in self._projection.process_heads
                for monitor in head.monitor_heads
                if monitor.pending_observation_event_reference is not None
            )
            return tuple(self._observation_events[item] for item in event_ids)

    def pending_notifications(
        self,
        *,
        include_unmonitored_completions: bool,
        maximum_items: int = 8,
        wake_chain_id: str | None = None,
        include_automatic_delivery_deferred: bool = True,
    ) -> tuple[PendingTerminalNotification, ...]:
        if maximum_items < 1 or maximum_items > 8:
            raise ValueError("terminal notification selection bound is invalid")
        with self._lock:
            selected: list[PendingTerminalNotification] = []
            for head in self._projection.process_heads:
                for monitor in head.monitor_heads:
                    reference = monitor.pending_observation_event_reference
                    if reference is None:
                        continue
                    event = self._observation_events.get(reference.event_id)
                    if event is None:
                        raise TerminalNotificationContractError(
                            "pending monitor notification cannot be hydrated"
                        )
                    if (
                        wake_chain_id is not None
                        and event.wake_chain_id != wake_chain_id
                    ):
                        continue
                    if (
                        not include_automatic_delivery_deferred
                        and event.id in self._automatic_delivery_deferred_source_ids
                    ):
                        continue
                    selected.append(
                        PendingTerminalNotification(
                            source_event=event,
                            process_head_fingerprint=head.head_fingerprint,
                            attachment=_notification_attachment(
                                event,
                                runtime_session_id=self.runtime_session_id,
                            ),
                            wake_chain_id=event.wake_chain_id,
                        )
                    )
                completion = head.pending_completion_without_monitor_reference
                if (
                    not include_unmonitored_completions
                    or completion is None
                    or wake_chain_id is not None
                ):
                    continue
                event = self._completion_events.get(completion.event_id)
                if event is None:
                    raise TerminalNotificationContractError(
                        "pending process completion cannot be hydrated"
                    )
                selected.append(
                    PendingTerminalNotification(
                        source_event=event,
                        process_head_fingerprint=head.head_fingerprint,
                        attachment=_notification_attachment(
                            event,
                            runtime_session_id=self.runtime_session_id,
                        ),
                        wake_chain_id=None,
                    )
                )
            selected.sort(
                key=lambda item: (
                    item.source_event.sequence or 0,
                    item.source_event.id,
                )
            )
            return tuple(selected[:maximum_items])

    def process_head(
        self, stream_identity: TerminalOutputStreamIdentityFact
    ) -> TerminalNotificationProcessHeadFact | None:
        key = stream_identity.stream_identity_fingerprint
        with self._lock:
            return next(
                (
                    item
                    for item in self._projection.process_heads
                    if item.stream_identity.stream_identity_fingerprint == key
                ),
                None,
            )

    def monitor_head(
        self, monitor_id: str
    ) -> TerminalMonitorNotificationHeadFact | None:
        with self._lock:
            return next(
                (
                    monitor
                    for head in self._projection.process_heads
                    for monitor in head.monitor_heads
                    if monitor.monitor_id == monitor_id
                ),
                None,
            )

    def autonomy_chain_snapshot(
        self, wake_chain_id: str
    ) -> TerminalAutonomyChainSnapshot:
        with self._lock:
            attribution = self._autonomy_chain_attributions.get(wake_chain_id)
            state = self._autonomy_chain_states.get(wake_chain_id)
            if attribution is None or state is None:
                raise TerminalNotificationContractError(
                    f"terminal autonomy chain is unavailable: {wake_chain_id}"
                )
            return TerminalAutonomyChainSnapshot(
                attribution=attribution,
                state=state,
            )

    def receipt_dominated_notifications(
        self, tool_result_end: ToolResultEndEvent
    ) -> tuple[PendingTerminalNotification, ...]:
        receipt = tool_result_end.terminal_process_observation_receipt
        if receipt is None:
            return ()
        stream = receipt.observation_semantic.observed_end_cursor.stream_identity
        with self._lock:
            head = next(
                (
                    item
                    for item in self._projection.process_heads
                    if item.stream_identity == stream
                ),
                None,
            )
            if head is None:
                return ()
            dominated: list[PendingTerminalNotification] = []
            for monitor in head.monitor_heads:
                pending_ref = monitor.pending_observation_event_reference
                if pending_ref is None:
                    continue
                pending = self._observation_events.get(pending_ref.event_id)
                if pending is None:
                    raise TerminalNotificationContractError(
                        "pending monitor observation cannot be hydrated"
                    )
                if not terminal_receipt_dominates_observation(
                    receipt=receipt,
                    pending=pending.observation,
                ):
                    continue
                if (
                    isinstance(
                        pending.observation,
                        TerminalProcessMonitorCompletionObservationSemanticFact,
                    )
                    and receipt.completion_event_reference
                    != pending.completion_event_reference
                ):
                    continue
                dominated.append(
                    PendingTerminalNotification(
                        source_event=pending,
                        process_head_fingerprint=head.head_fingerprint,
                        attachment=_notification_attachment(
                            pending,
                            runtime_session_id=self.runtime_session_id,
                        ),
                        wake_chain_id=pending.wake_chain_id,
                    )
                )
            completion_ref = head.pending_completion_without_monitor_reference
            if completion_ref is None and receipt.action_kind == "kill":
                completion_ref = head.latest_completion_event_reference
            completion = (
                None
                if completion_ref is None
                else self._completion_events.get(completion_ref.event_id)
            )
            observed_state = receipt.observation_semantic.observed_state
            if completion_ref is not None and completion is None:
                raise TerminalNotificationContractError(
                    "pending process completion cannot be hydrated"
                )
            if (
                completion is not None
                and receipt.completion_event_reference == completion_ref
                and isinstance(observed_state, TerminalProcessLifecycleOutcomeFact)
                and observed_state == completion.completion_semantic.outcome
                and (
                    head.pending_completion_without_monitor_reference is not None
                    or (
                        receipt.action_kind == "kill"
                        and observed_state.status == "killed"
                        and observed_state.kill_reason == "user_tool_kill"
                    )
                )
            ):
                dominated.append(
                    PendingTerminalNotification(
                        source_event=completion,
                        process_head_fingerprint=head.head_fingerprint,
                        attachment=_notification_attachment(
                            completion,
                            runtime_session_id=self.runtime_session_id,
                        ),
                        wake_chain_id=None,
                    )
                )
            return tuple(dominated)

    def completion_events_for_reservations(
        self,
        reservation_ids: tuple[str, ...],
    ) -> tuple[TerminalProcessCompletedEvent, ...]:
        """Resolve bounded active completion slots without scanning the ledger."""

        requested = set(reservation_ids)
        if len(requested) != len(reservation_ids):
            raise TerminalNotificationContractError(
                "terminal completion reservation IDs are duplicated"
            )
        with self._lock:
            resolved: list[TerminalProcessCompletedEvent] = []
            for head in self._projection.process_heads:
                reservation_id = (
                    f"terminal_completion_head:{head.stream_identity.process_id}"
                )
                if reservation_id not in requested:
                    continue
                reference = head.latest_completion_event_reference
                event = (
                    None
                    if reference is None
                    else self._completion_events.get(reference.event_id)
                )
                if event is None:
                    raise TerminalNotificationContractError(
                        "active completion reservation lacks its reducer authority"
                    )
                resolved.append(event)
            if {
                f"terminal_completion_head:{event.process_id}" for event in resolved
            } != requested:
                raise TerminalNotificationContractError(
                    "completion reservation set does not match process heads"
                )
            return tuple(
                sorted(resolved, key=lambda item: (item.sequence or 0, item.id))
            )

    def completion_event_for_stream(
        self,
        stream_identity: TerminalOutputStreamIdentityFact,
    ) -> TerminalProcessCompletedEvent | None:
        """Resolve the reducer-owned completion authority for one output stream."""

        with self._lock:
            head = next(
                (
                    item
                    for item in self._projection.process_heads
                    if item.stream_identity == stream_identity
                ),
                None,
            )
            if head is None or head.latest_completion_event_reference is None:
                return None
            reference = head.latest_completion_event_reference
            completion = self._completion_events.get(reference.event_id)
            if completion is None:
                raise TerminalNotificationContractError(
                    "terminal completion head cannot be hydrated"
                )
            if (
                completion.completion_semantic.terminal_output_cursor.stream_identity
                != stream_identity
            ):
                raise TerminalNotificationContractError(
                    "terminal completion stream identity drifted"
                )
            return completion

    def validate_candidate_batch(self, events: tuple[AgentEvent, ...]) -> None:
        """Replay candidate account transitions against the current CAS head."""

        account = self.account_snapshot()
        projection = self.projection_snapshot()
        chain_states = dict(self._autonomy_chain_states)
        for event in events:
            if not isinstance(
                event,
                (
                    TerminalNotificationReservationCreatedEvent,
                    TerminalNotificationReservationReleasedEvent,
                ),
            ):
                if isinstance(event, RunStartEvent):
                    _validate_run_start_notification_batch(
                        event=event,
                        events=events,
                        projection=projection,
                        observation_events=self._observation_events,
                        chain_states=chain_states,
                        runtime_session_id=self.runtime_session_id,
                    )
                    _advance_candidate_chain_state(event, chain_states)
                elif isinstance(event, ModelCallStartEvent):
                    _validate_model_start_notification_batch(
                        event=event,
                        events=events,
                        projection=projection,
                        observation_events=self._observation_events,
                        chain_states=chain_states,
                        chain_attributions=self._autonomy_chain_attributions,
                        runtime_session_id=self.runtime_session_id,
                    )
                    _advance_candidate_model_start_chain_state(event, chain_states)
                elif isinstance(event, ToolResultEndEvent):
                    _validate_tool_receipt_batch(
                        event=event,
                        events=events,
                        projection=projection,
                        observation_events=self._observation_events,
                        completion_events=self._completion_events,
                        runtime_session_id=self.runtime_session_id,
                    )
                continue
            if event.source_state != account:
                raise TerminalNotificationContractError(
                    "terminal notification account candidate source is stale"
                )
            account = event.resulting_state

    def apply_committed(self, events: tuple[AgentEvent, ...]) -> None:
        required_tool_result_identities = {
            event.tool_result_end_event_identity.identity_fingerprint
            for event in events
            if isinstance(event, TerminalProcessMonitorReceiptAppliedEvent)
        }
        required_tool_result_identities.update(
            event.tool_result_end_event_identity.identity_fingerprint
            for event in events
            if isinstance(event, TerminalProcessObservationDeliveryDispositionEvent)
            and event.tool_result_end_event_identity is not None
        )
        with self._lock:
            for event in events:
                if not isinstance(event, ToolResultEndEvent):
                    continue
                identity = stable_event_identity(
                    event,
                    runtime_session_id=self.runtime_session_id,
                )
                if identity.identity_fingerprint in required_tool_result_identities:
                    self._tool_result_by_identity[identity.identity_fingerprint] = event
            for event in events:
                if event.sequence is None:
                    raise TerminalNotificationContractError(
                        "notification reducer requires committed events"
                    )
                if event.sequence <= self.through_sequence:
                    continue
                self._apply_one(event)
                self.through_sequence = event.sequence
            for identity in required_tool_result_identities:
                self._tool_result_by_identity.pop(identity, None)

    def _apply_one(self, event: AgentEvent) -> None:
        if isinstance(
            event,
            (
                TerminalNotificationReservationCreatedEvent,
                TerminalNotificationReservationReleasedEvent,
            ),
        ):
            if event.source_state != self._account:
                raise TerminalNotificationContractError(
                    "terminal notification account replay CAS failed"
                )
            self._account = event.resulting_state
            if isinstance(event, TerminalNotificationReservationCreatedEvent):
                self._apply_reservation_created(event)
            else:
                self._apply_reservation_released(event)
            self._replace_all_heads(
                self._projection.process_heads,
                through_sequence=event.sequence or self.through_sequence,
            )
            return
        if isinstance(event, TerminalProcessCompletedEvent):
            self._apply_completion(event)
        elif isinstance(event, TerminalProcessMonitorRegisteredEvent):
            self._registration_events[event.id] = event
            self._apply_chain_registration(event)
        elif isinstance(event, TerminalProcessMonitorObservationCommittedEvent):
            self._apply_observation(event)
        elif isinstance(event, TerminalProcessMonitorReceiptAppliedEvent):
            self._apply_monitor_receipt(event)
        elif isinstance(event, TerminalProcessObservationDeliveryDispositionEvent):
            self._apply_disposition(event)
        elif isinstance(event, TerminalProcessObservationDeliveryDeferredEvent):
            self._automatic_delivery_deferred_source_ids.update(
                item.event_id for item in event.observation_source_references
            )
        elif isinstance(event, TerminalProcessMonitorTerminatedEvent):
            self._apply_termination(event)
        elif isinstance(event, ToolResultEndEvent):
            self._apply_receipt(event)
        elif isinstance(event, RunStartEvent):
            self._apply_run_start(event)
        elif isinstance(event, ModelCallStartEvent):
            self._apply_model_start(event)

    def _apply_chain_registration(
        self, event: TerminalProcessMonitorRegisteredEvent
    ) -> None:
        attribution = event.registration_attribution.wake_chain
        chain_id = attribution.wake_chain_id
        existing = self._autonomy_chain_attributions.get(chain_id)
        if existing is None:
            self._autonomy_chain_attributions[chain_id] = attribution
            self._autonomy_chain_states[chain_id] = build_frozen_fact(
                TerminalAutonomyChainStateFact,
                schema_version="terminal_autonomy_chain_state.v1",
                wake_chain_id=chain_id,
                state_revision=0,
                last_automatic_delivery_ordinal=0,
                last_automatic_delivery_at_utc=None,
                chain_policy_fingerprint=(
                    attribution.resolved_policy.policy_fingerprint
                ),
            )
            return
        if (
            existing.root_human_run_event_reference
            != attribution.root_human_run_event_reference
            or existing.resolved_policy != attribution.resolved_policy
        ):
            raise TerminalNotificationContractError(
                "terminal monitor registration changed its autonomy chain authority"
            )

    def _apply_reservation_created(
        self, event: TerminalNotificationReservationCreatedEvent
    ) -> None:
        reservation = event.transition.reservation
        stream = reservation.stream_identity
        head = self.process_head(stream)
        if reservation.reservation_kind == "completion_process_head":
            if head is not None:
                raise TerminalNotificationContractError(
                    "completion process head already exists"
                )
            self._replace_process_head(
                build_frozen_fact(
                    TerminalNotificationProcessHeadFact,
                    schema_version="terminal_notification_process_head.v1",
                    stream_identity=stream,
                    latest_completion_event_reference=None,
                    monitor_heads=(),
                    latest_dominant_receipt_reference=None,
                    pending_completion_without_monitor_reference=None,
                ),
                through_sequence=event.sequence or self.through_sequence,
            )
            return
        if head is None:
            raise TerminalNotificationContractError(
                "monitor reservation lacks completion process head"
            )
        registration = self._registration_events.get(reservation.created_by_event_id)
        if registration is None or reservation.monitor_id is None:
            raise TerminalNotificationContractError(
                "monitor reservation lacks its same-batch registration"
            )
        if registration.notification_reservation_id != reservation.reservation_id:
            raise TerminalNotificationContractError(
                "monitor registration reservation identity drifted"
            )
        if head.monitor_heads:
            raise TerminalNotificationContractError(
                "process already has an active monitor"
            )
        baseline = registration.registration_semantic.initial_baseline_cursor
        monitor = build_frozen_fact(
            TerminalMonitorNotificationHeadFact,
            schema_version="terminal_monitor_notification_head.v1",
            monitor_id=registration.registration_semantic.monitor_id,
            registration_event_identity=stable_event_identity(
                registration, runtime_session_id=self.runtime_session_id
            ),
            monitor_core_state_fingerprint=(
                registration.resulting_monitor_core_state_fingerprint
            ),
            last_committed_observation_ordinal=0,
            last_observation_cursor_fingerprint=baseline.cursor_fingerprint,
            last_consumed_cursor_fingerprint=baseline.cursor_fingerprint,
            pending_observation_event_reference=None,
            latest_delivery_event_reference=None,
        )
        self._replace_process_head(
            build_frozen_fact(
                TerminalNotificationProcessHeadFact,
                schema_version="terminal_notification_process_head.v1",
                stream_identity=head.stream_identity,
                latest_completion_event_reference=(
                    head.latest_completion_event_reference
                ),
                monitor_heads=(monitor,),
                latest_dominant_receipt_reference=(
                    head.latest_dominant_receipt_reference
                ),
                pending_completion_without_monitor_reference=None,
            ),
            through_sequence=event.sequence or self.through_sequence,
        )

    def _apply_reservation_released(
        self, event: TerminalNotificationReservationReleasedEvent
    ) -> None:
        reservation = event.transition.reservation
        if reservation.reservation_kind != "completion_process_head":
            return
        head = self.process_head(reservation.stream_identity)
        if head is None:
            raise TerminalNotificationContractError(
                "completion reservation release lacks process head"
            )
        if (
            head.monitor_heads
            or head.pending_completion_without_monitor_reference is not None
        ):
            raise TerminalNotificationContractError(
                "completion reservation released before notifications were consumed"
            )
        key = reservation.stream_identity.stream_identity_fingerprint
        self._replace_all_heads(
            tuple(
                item
                for item in self._projection.process_heads
                if item.stream_identity.stream_identity_fingerprint != key
            ),
            through_sequence=event.sequence or self.through_sequence,
        )

    def _apply_completion(self, event: TerminalProcessCompletedEvent) -> None:
        # A bare RuntimeSession has no Host ingress and therefore owns no
        # completion-notification slot. Its lifecycle event remains durable,
        # but it is outside this Host-only projection.
        if event.owner_host_session_id is None:
            return
        semantic = event.completion_semantic
        stream = semantic.terminal_output_cursor.stream_identity
        head = self.process_head(stream)
        if head is None:
            raise TerminalNotificationContractError(
                "terminal completion lacks a reserved process head"
            )
        reference = event_reference_from_stored(
            event, runtime_session_id=self.runtime_session_id
        )
        self._completion_events[event.id] = event
        pending_without_monitor = (
            reference
            if not head.monitor_heads and _completion_is_delivery_eligible(event)
            else None
        )
        self._replace_process_head(
            build_frozen_fact(
                TerminalNotificationProcessHeadFact,
                schema_version="terminal_notification_process_head.v1",
                stream_identity=head.stream_identity,
                latest_completion_event_reference=reference,
                monitor_heads=head.monitor_heads,
                latest_dominant_receipt_reference=head.latest_dominant_receipt_reference,
                pending_completion_without_monitor_reference=pending_without_monitor,
            ),
            through_sequence=event.sequence or self.through_sequence,
        )

    def _apply_observation(
        self, event: TerminalProcessMonitorObservationCommittedEvent
    ) -> None:
        stream = _observation_stream(event)
        head = self.process_head(stream)
        if head is None:
            raise TerminalNotificationContractError(
                "terminal monitor observation lacks process head"
            )
        monitor = next(
            (
                item
                for item in head.monitor_heads
                if item.monitor_id == event.observation.monitor_id
            ),
            None,
        )
        if monitor is None:
            raise TerminalNotificationContractError(
                "terminal monitor observation lacks monitor head"
            )
        reference = event_reference_from_stored(
            event, runtime_session_id=self.runtime_session_id
        )
        output = event.observation.output_authority
        end = getattr(output, "end_cursor", getattr(output, "terminal_cursor", None))
        if end is None:
            raise TerminalNotificationContractError(
                "monitor observation lacks end cursor"
            )
        monitor = build_frozen_fact(
            TerminalMonitorNotificationHeadFact,
            schema_version="terminal_monitor_notification_head.v1",
            monitor_id=monitor.monitor_id,
            registration_event_identity=monitor.registration_event_identity,
            monitor_core_state_fingerprint=(
                event.monitor_state_transition.after_core_state_fingerprint
            ),
            last_committed_observation_ordinal=event.observation.observation_ordinal,
            last_observation_cursor_fingerprint=end.cursor_fingerprint,
            last_consumed_cursor_fingerprint=monitor.last_consumed_cursor_fingerprint,
            pending_observation_event_reference=reference,
            latest_delivery_event_reference=monitor.latest_delivery_event_reference,
        )
        self._observation_events[event.id] = event
        self._replace_process_head(
            build_frozen_fact(
                TerminalNotificationProcessHeadFact,
                schema_version="terminal_notification_process_head.v1",
                stream_identity=head.stream_identity,
                latest_completion_event_reference=head.latest_completion_event_reference,
                monitor_heads=(monitor,),
                latest_dominant_receipt_reference=head.latest_dominant_receipt_reference,
                pending_completion_without_monitor_reference=None,
            ),
            through_sequence=event.sequence or self.through_sequence,
        )

    def _apply_monitor_receipt(
        self, event: TerminalProcessMonitorReceiptAppliedEvent
    ) -> None:
        stream = event.observed_end_cursor.stream_identity
        head = self.process_head(stream)
        if head is None:
            raise TerminalNotificationContractError(
                "terminal monitor receipt lacks process head"
            )
        matches = tuple(
            monitor
            for monitor in head.monitor_heads
            if monitor.registration_event_identity.event_id
            == event.registration_event_reference.event_id
        )
        if len(matches) != 1:
            raise TerminalNotificationContractError(
                "terminal monitor receipt lacks one monitor head"
            )
        monitor = matches[0]
        if (
            monitor.pending_observation_event_reference
            != event.pending_observation_event_reference
        ):
            raise TerminalNotificationContractError(
                "terminal monitor receipt pending head drifted"
            )
        tool_result = self._tool_result_by_identity.get(
            event.tool_result_end_event_identity.identity_fingerprint
        )
        if (
            tool_result is None
            or stable_event_identity(
                tool_result,
                runtime_session_id=self.runtime_session_id,
            )
            != event.tool_result_end_event_identity
        ):
            raise TerminalNotificationContractError(
                "terminal monitor receipt cannot hydrate ToolResult"
            )
        receipt = tool_result.terminal_process_observation_receipt
        if receipt is None or receipt.receipt_fingerprint != event.receipt_fingerprint:
            raise TerminalNotificationContractError(
                "terminal monitor receipt identity drifted"
            )
        replacement = build_frozen_fact(
            TerminalMonitorNotificationHeadFact,
            schema_version="terminal_monitor_notification_head.v1",
            monitor_id=monitor.monitor_id,
            registration_event_identity=monitor.registration_event_identity,
            monitor_core_state_fingerprint=(
                event.monitor_state_transition.after_core_state_fingerprint
            ),
            last_committed_observation_ordinal=(
                monitor.last_committed_observation_ordinal
            ),
            last_observation_cursor_fingerprint=(
                event.observed_end_cursor.cursor_fingerprint
            ),
            last_consumed_cursor_fingerprint=(
                event.observed_end_cursor.cursor_fingerprint
            ),
            pending_observation_event_reference=(
                monitor.pending_observation_event_reference
            ),
            latest_delivery_event_reference=monitor.latest_delivery_event_reference,
        )
        self._replace_process_head(
            build_frozen_fact(
                TerminalNotificationProcessHeadFact,
                schema_version="terminal_notification_process_head.v1",
                stream_identity=head.stream_identity,
                latest_completion_event_reference=(
                    head.latest_completion_event_reference
                ),
                monitor_heads=(replacement,),
                latest_dominant_receipt_reference=(
                    event_reference_from_stored(
                        tool_result,
                        runtime_session_id=self.runtime_session_id,
                    )
                ),
                pending_completion_without_monitor_reference=(
                    head.pending_completion_without_monitor_reference
                ),
            ),
            through_sequence=event.sequence or self.through_sequence,
        )

    def _apply_disposition(
        self, event: TerminalProcessObservationDeliveryDispositionEvent
    ) -> None:
        source_ids = {item.event_id for item in event.observation_source_references}
        self._automatic_delivery_deferred_source_ids.difference_update(source_ids)
        heads: list[TerminalNotificationProcessHeadFact] = []
        for head in self._projection.process_heads:
            monitors: list[TerminalMonitorNotificationHeadFact] = []
            for monitor in head.monitor_heads:
                pending = monitor.pending_observation_event_reference
                if pending is None or pending.event_id not in source_ids:
                    monitors.append(monitor)
                    continue
                observation = self._observation_events.get(pending.event_id)
                if observation is None:
                    raise TerminalNotificationContractError(
                        "notification disposition source cannot be hydrated"
                    )
                output = observation.observation.output_authority
                end = getattr(
                    output, "end_cursor", getattr(output, "terminal_cursor", None)
                )
                assert end is not None
                consumed_cursor_fingerprint = end.cursor_fingerprint
                if event.outcome == "explicitly_observed":
                    identity = event.tool_result_end_event_identity
                    if identity is None:
                        raise TerminalNotificationContractError(
                            "explicit disposition lacks ToolResult identity"
                        )
                    tool_result = self._tool_result_by_identity.get(
                        identity.identity_fingerprint
                    )
                    if (
                        tool_result is None
                        or stable_event_identity(
                            tool_result,
                            runtime_session_id=self.runtime_session_id,
                        )
                        != identity
                    ):
                        raise TerminalNotificationContractError(
                            "explicit disposition ToolResult cannot be hydrated"
                        )
                    receipt = tool_result.terminal_process_observation_receipt
                    if receipt is None or not terminal_receipt_dominates_observation(
                        receipt=receipt,
                        pending=observation.observation,
                    ):
                        raise TerminalNotificationContractError(
                            "explicit disposition receipt does not dominate source"
                        )
                    if (
                        isinstance(
                            observation.observation,
                            TerminalProcessMonitorCompletionObservationSemanticFact,
                        )
                        and receipt.completion_event_reference
                        != observation.completion_event_reference
                    ):
                        raise TerminalNotificationContractError(
                            "explicit completion receipt identity drifted"
                        )
                    consumed_cursor_fingerprint = receipt.observation_semantic.observed_end_cursor.cursor_fingerprint
                if observation.observation.observation_kind not in {
                    "process_completed",
                    "monitor_expired",
                }:
                    monitors.append(
                        build_frozen_fact(
                            TerminalMonitorNotificationHeadFact,
                            schema_version="terminal_monitor_notification_head.v1",
                            monitor_id=monitor.monitor_id,
                            registration_event_identity=monitor.registration_event_identity,
                            monitor_core_state_fingerprint=(
                                monitor.monitor_core_state_fingerprint
                            ),
                            last_committed_observation_ordinal=(
                                monitor.last_committed_observation_ordinal
                            ),
                            last_observation_cursor_fingerprint=(
                                monitor.last_observation_cursor_fingerprint
                            ),
                            last_consumed_cursor_fingerprint=(
                                consumed_cursor_fingerprint
                            ),
                            pending_observation_event_reference=None,
                            latest_delivery_event_reference=event_reference_from_stored(
                                event, runtime_session_id=self.runtime_session_id
                            ),
                        )
                    )
            pending_completion = head.pending_completion_without_monitor_reference
            if (
                pending_completion is not None
                and pending_completion.event_id in source_ids
            ):
                pending_completion = None
            heads.append(
                build_frozen_fact(
                    TerminalNotificationProcessHeadFact,
                    schema_version="terminal_notification_process_head.v1",
                    stream_identity=head.stream_identity,
                    latest_completion_event_reference=head.latest_completion_event_reference,
                    monitor_heads=tuple(monitors),
                    latest_dominant_receipt_reference=head.latest_dominant_receipt_reference,
                    pending_completion_without_monitor_reference=pending_completion,
                )
            )
        self._replace_all_heads(
            tuple(heads), through_sequence=event.sequence or self.through_sequence
        )
        delivery = event.autonomy_delivery
        if delivery is not None:
            state = self._autonomy_chain_states.get(delivery.wake_chain_id)
            if state is None:
                raise TerminalNotificationContractError(
                    "terminal delivery disposition lacks durable chain state"
                )
            if (
                delivery.automatic_delivery_ordinal
                != state.last_automatic_delivery_ordinal + 1
                or delivery.chain_policy_fingerprint != state.chain_policy_fingerprint
            ):
                raise TerminalNotificationContractError(
                    "terminal delivery disposition chain CAS failed"
                )
            self._autonomy_chain_states[delivery.wake_chain_id] = build_frozen_fact(
                TerminalAutonomyChainStateFact,
                schema_version="terminal_autonomy_chain_state.v1",
                wake_chain_id=state.wake_chain_id,
                state_revision=state.state_revision + 1,
                last_automatic_delivery_ordinal=(delivery.automatic_delivery_ordinal),
                last_automatic_delivery_at_utc=event.created_at,
                chain_policy_fingerprint=state.chain_policy_fingerprint,
            )

    def _apply_termination(self, event: TerminalProcessMonitorTerminatedEvent) -> None:
        monitor_id = event.termination_semantic.monitor_id
        heads: list[TerminalNotificationProcessHeadFact] = []
        for head in self._projection.process_heads:
            monitors = tuple(
                item for item in head.monitor_heads if item.monitor_id != monitor_id
            )
            heads.append(
                build_frozen_fact(
                    TerminalNotificationProcessHeadFact,
                    schema_version="terminal_notification_process_head.v1",
                    stream_identity=head.stream_identity,
                    latest_completion_event_reference=head.latest_completion_event_reference,
                    monitor_heads=monitors,
                    latest_dominant_receipt_reference=head.latest_dominant_receipt_reference,
                    pending_completion_without_monitor_reference=(
                        head.latest_completion_event_reference
                        if not monitors
                        and head.latest_completion_event_reference is not None
                        and event.termination_semantic.terminal_reason
                        not in {"explicit_process_kill", "session_closed"}
                        else head.pending_completion_without_monitor_reference
                    ),
                )
            )
        self._replace_all_heads(
            tuple(heads), through_sequence=event.sequence or self.through_sequence
        )

    def _apply_receipt(self, event: ToolResultEndEvent) -> None:
        receipt = event.terminal_process_observation_receipt
        if receipt is None:
            return
        # Delivery disposition is the consuming authority.  The receipt itself
        # is represented by its exact durable reference after this batch.
        reference = event_reference_from_stored(
            event, runtime_session_id=self.runtime_session_id
        )
        stream = receipt.observation_semantic.observed_end_cursor.stream_identity
        head = self.process_head(stream)
        if head is None:
            return
        self._replace_process_head(
            build_frozen_fact(
                TerminalNotificationProcessHeadFact,
                schema_version="terminal_notification_process_head.v1",
                stream_identity=head.stream_identity,
                latest_completion_event_reference=head.latest_completion_event_reference,
                monitor_heads=head.monitor_heads,
                latest_dominant_receipt_reference=reference,
                pending_completion_without_monitor_reference=(
                    head.pending_completion_without_monitor_reference
                ),
            ),
            through_sequence=event.sequence or self.through_sequence,
        )

    def _apply_run_start(self, event: RunStartEvent) -> None:
        # The same-batch delivery disposition owns the durable chain transition.
        del event

    def _apply_model_start(self, event: ModelCallStartEvent) -> None:
        # The same-batch delivery disposition owns the durable chain transition.
        del event

    def _replace_process_head(
        self,
        replacement: TerminalNotificationProcessHeadFact,
        *,
        through_sequence: int,
    ) -> None:
        key = replacement.stream_identity.stream_identity_fingerprint
        items = [
            item
            for item in self._projection.process_heads
            if item.stream_identity.stream_identity_fingerprint != key
        ]
        items.append(replacement)
        self._replace_all_heads(tuple(items), through_sequence=through_sequence)

    def _replace_all_heads(
        self,
        heads: tuple[TerminalNotificationProcessHeadFact, ...],
        *,
        through_sequence: int,
    ) -> None:
        self._projection = build_frozen_fact(
            HostIngressNotificationProjectionStateFact,
            schema_version="host_ingress_notification_projection_state.v1",
            ledger_runtime_session_id=self.runtime_session_id,
            source_through_sequence=max(
                self._projection.source_through_sequence, through_sequence
            ),
            process_heads=tuple(
                sorted(
                    heads,
                    key=lambda item: item.stream_identity.stream_identity_fingerprint,
                )
            ),
            reservation_account_revision=self._account.account_revision,
            reservation_account_state_fingerprint=self._account.state_fingerprint,
            reducer_contract_fingerprint=NOTIFICATION_REDUCER_CONTRACT_FINGERPRINT,
        )


class TerminalNotificationAccountCoordinator:
    """Factory and process owner for account reservation transitions."""

    def __init__(
        self,
        *,
        runtime_session_id: str,
        store: HostIngressNotificationProjectionStore,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self.store = store
        self._lock = RLock()
        self._owners: dict[str, _LocalReservationOwner] = {}

    def prepare_completion_reservation(
        self,
        *,
        process: TerminalProcessState,
        tool_result_end_event_id: str,
    ) -> PreparedTerminalNotificationReservation:
        return self._prepare(
            reservation_id=f"terminal_completion_head:{process.process_id}",
            reservation_kind="completion_process_head",
            stream_identity=process.output.stream_identity,
            monitor_id=None,
            created_by_event_id=tool_result_end_event_id,
            process=process,
        )

    def prepare_monitor_reservation(
        self,
        *,
        monitor_id: str,
        stream_identity: TerminalOutputStreamIdentityFact,
        registration_event_id: str,
    ) -> PreparedTerminalNotificationReservation:
        return self._prepare(
            reservation_id=f"terminal_monitor_slot:{monitor_id}",
            reservation_kind="monitor_lifecycle",
            stream_identity=stream_identity,
            monitor_id=monitor_id,
            created_by_event_id=registration_event_id,
            process=None,
        )

    def discard_prepared_reservation(
        self,
        prepared: PreparedTerminalNotificationReservation,
    ) -> None:
        """Release a reservation candidate that never reached a durable batch."""

        reservation = prepared.reservation
        with self._lock:
            owner = self._owners.get(reservation.reservation_id)
            if owner is None or owner.prepared != prepared:
                return
            account = self.store.account_snapshot()
            active = (
                account.active_completion_reservations
                if reservation.reservation_kind == "completion_process_head"
                else account.active_monitor_reservations
            )
            if any(
                item.reservation_id == reservation.reservation_id for item in active
            ):
                raise TerminalNotificationContractError(
                    "cannot discard a durably active terminal notification reservation"
                )
            self._owners.pop(reservation.reservation_id, None)

    def _prepare(
        self,
        *,
        reservation_id: str,
        reservation_kind: Literal["completion_process_head", "monitor_lifecycle"],
        stream_identity: TerminalOutputStreamIdentityFact,
        monitor_id: str | None,
        created_by_event_id: str,
        process: TerminalProcessState | None,
    ) -> PreparedTerminalNotificationReservation:
        with self._lock:
            existing = self._owners.get(reservation_id)
            if existing is not None:
                return existing.prepared
            account = self.store.account_snapshot()
            active = (
                account.active_completion_reservations
                if reservation_kind == "completion_process_head"
                else account.active_monitor_reservations
            )
            maximum = (
                account.maximum_completion_process_heads
                if reservation_kind == "completion_process_head"
                else account.maximum_active_monitor_slots
            )
            if len(active) >= maximum:
                raise TerminalNotificationCapacityError(
                    f"terminal notification {reservation_kind} capacity exhausted",
                    reason_code="terminal_notification_capacity_exhausted",
                )
            if reservation_kind == "monitor_lifecycle" and any(
                item.stream_identity == stream_identity for item in active
            ):
                raise TerminalNotificationCapacityError(
                    "terminal monitor is already active for this process",
                    reason_code="terminal_monitor_already_active_for_process",
                )
            reservation = build_frozen_fact(
                TerminalNotificationReservationFact,
                schema_version="terminal_notification_reservation.v1",
                reservation_id=reservation_id,
                reservation_kind=reservation_kind,
                stream_identity=stream_identity,
                monitor_id=monitor_id,
                created_by_event_id=created_by_event_id,
            )
            prepared = PreparedTerminalNotificationReservation(
                reservation=reservation,
                expected_account_revision=account.account_revision,
                expected_account_state_fingerprint=account.state_fingerprint,
            )
            self._owners[reservation_id] = _LocalReservationOwner(
                prepared=prepared, process=process
            )
            return prepared

    def freeze_created_event(
        self,
        *,
        prepared: PreparedTerminalNotificationReservation,
        cause_events: tuple[AgentEvent, ...],
        registration_event: TerminalProcessMonitorRegisteredEvent | None = None,
    ) -> TerminalNotificationReservationCreatedEvent:
        with self._lock:
            owner = self._owners.get(prepared.reservation.reservation_id)
            if owner is None or owner.prepared != prepared:
                raise TerminalNotificationContractError(
                    "terminal notification reservation owner is unavailable"
                )
            if prepared.reservation.reservation_kind == "monitor_lifecycle":
                if (
                    registration_event is None
                    or registration_event.id != prepared.reservation.created_by_event_id
                ):
                    raise TerminalNotificationContractError(
                        "monitor notification reservation lacks exact registration"
                    )
            source = self.store.account_snapshot()
            if (
                source.account_revision != prepared.expected_account_revision
                or source.state_fingerprint
                != prepared.expected_account_state_fingerprint
            ):
                raise TerminalNotificationContractError(
                    "terminal notification reservation source became stale"
                )
            result = _account_after(
                source,
                reservation=prepared.reservation,
                created=True,
                transition_event_id=(
                    "terminal_notification_reservation_created:"
                    f"{prepared.reservation.reservation_id}"
                ),
            )
            identities = _stable_identities(
                cause_events, runtime_session_id=self.runtime_session_id
            )
            transition = build_frozen_fact(
                TerminalNotificationAccountTransitionFact,
                schema_version="terminal_notification_account_transition.v1",
                source_revision=source.account_revision,
                result_revision=result.account_revision,
                before_state_fingerprint=source.state_fingerprint,
                after_state_fingerprint=result.state_fingerprint,
                reservation=prepared.reservation,
                cause_event_identities=identities,
            )
            event = TerminalNotificationReservationCreatedEvent(
                id=result.latest_transition_event_id or "",
                run_id=cause_events[0].run_id,
                turn_id=cause_events[0].turn_id,
                reply_id=cause_events[0].reply_id,
                source_state=source,
                resulting_state=result,
                transition=transition,
            )
            owner.owner_state = "candidate_frozen"
            return event

    def freeze_released_event(
        self,
        *,
        reservation_id: str,
        cause_events: tuple[AgentEvent, ...],
    ) -> TerminalNotificationReservationReleasedEvent:
        return self._freeze_released_event_from_source(
            source=self.store.account_snapshot(),
            reservation_id=reservation_id,
            cause_events=cause_events,
        )

    def freeze_released_events(
        self,
        *,
        reservation_ids: tuple[str, ...],
        cause_events: tuple[AgentEvent, ...],
    ) -> tuple[TerminalNotificationReservationReleasedEvent, ...]:
        if len(reservation_ids) != len(set(reservation_ids)):
            raise TerminalNotificationContractError(
                "terminal notification release IDs are duplicated"
            )
        source = self.store.account_snapshot()
        events: list[TerminalNotificationReservationReleasedEvent] = []
        for reservation_id in reservation_ids:
            event = self._freeze_released_event_from_source(
                source=source,
                reservation_id=reservation_id,
                cause_events=cause_events,
            )
            events.append(event)
            source = event.resulting_state
        return tuple(events)

    def _freeze_released_event_from_source(
        self,
        *,
        source: TerminalNotificationReservationAccountStateFact,
        reservation_id: str,
        cause_events: tuple[AgentEvent, ...],
    ) -> TerminalNotificationReservationReleasedEvent:
        reservation = next(
            (
                item
                for item in (
                    *source.active_completion_reservations,
                    *source.active_monitor_reservations,
                )
                if item.reservation_id == reservation_id
            ),
            None,
        )
        if reservation is None:
            raise TerminalNotificationContractError(
                "terminal notification release reservation is absent"
            )
        event_id = f"terminal_notification_reservation_released:{reservation_id}"
        result = _account_after(
            source,
            reservation=reservation,
            created=False,
            transition_event_id=event_id,
        )
        transition = build_frozen_fact(
            TerminalNotificationAccountTransitionFact,
            schema_version="terminal_notification_account_transition.v1",
            source_revision=source.account_revision,
            result_revision=result.account_revision,
            before_state_fingerprint=source.state_fingerprint,
            after_state_fingerprint=result.state_fingerprint,
            reservation=reservation,
            cause_event_identities=_stable_identities(
                cause_events, runtime_session_id=self.runtime_session_id
            ),
        )
        return TerminalNotificationReservationReleasedEvent(
            id=event_id,
            run_id=cause_events[0].run_id,
            turn_id=cause_events[0].turn_id,
            reply_id=cause_events[0].reply_id,
            source_state=source,
            resulting_state=result,
            transition=transition,
        )

    def freeze_monitor_release(
        self,
        *,
        monitor_id: str,
        cause_events: tuple[AgentEvent, ...],
    ) -> TerminalNotificationReservationReleasedEvent:
        reservation_id = f"terminal_monitor_slot:{monitor_id}"
        return self.freeze_released_event(
            reservation_id=reservation_id,
            cause_events=cause_events,
        )

    def on_committed(self, events: tuple[AgentEvent, ...]) -> None:
        for event in events:
            if isinstance(event, TerminalNotificationReservationReleasedEvent):
                reservation = event.transition.reservation
                with self._lock:
                    self._owners.pop(reservation.reservation_id, None)
                continue
            if not isinstance(event, TerminalNotificationReservationCreatedEvent):
                continue
            reservation = event.transition.reservation
            with self._lock:
                owner = self._owners.get(reservation.reservation_id)
                if owner is None:
                    continue
                owner.owner_state = "committed"
                process = owner.process
            if process is not None:
                from pulsara_agent.runtime.terminal.process import (
                    confirm_completion_notification_reservation,
                )

                confirm_completion_notification_reservation(process)


def _account_after(
    source: TerminalNotificationReservationAccountStateFact,
    *,
    reservation: TerminalNotificationReservationFact,
    created: bool,
    transition_event_id: str,
) -> TerminalNotificationReservationAccountStateFact:
    completion = list(source.active_completion_reservations)
    monitors = list(source.active_monitor_reservations)
    target = (
        completion
        if reservation.reservation_kind == "completion_process_head"
        else monitors
    )
    if created:
        if any(item.reservation_id == reservation.reservation_id for item in target):
            raise TerminalNotificationContractError(
                "notification reservation already active"
            )
        target.append(reservation)
    else:
        before = len(target)
        target[:] = [
            item for item in target if item.reservation_id != reservation.reservation_id
        ]
        if len(target) == before:
            raise TerminalNotificationContractError(
                "notification reservation release is absent"
            )
    return build_frozen_fact(
        TerminalNotificationReservationAccountStateFact,
        schema_version="terminal_notification_reservation_account_state.v1",
        ledger_runtime_session_id=source.ledger_runtime_session_id,
        account_revision=source.account_revision + 1,
        maximum_completion_process_heads=source.maximum_completion_process_heads,
        maximum_active_monitor_slots=source.maximum_active_monitor_slots,
        active_completion_reservations=tuple(
            sorted(completion, key=lambda item: item.reservation_id)
        ),
        active_monitor_reservations=tuple(
            sorted(monitors, key=lambda item: item.reservation_id)
        ),
        latest_transition_event_id=transition_event_id,
    )


def _stable_identities(
    events: Iterable[AgentEvent], *, runtime_session_id: str
) -> tuple[StableEventIdentityFact, ...]:
    identities = tuple(
        stable_event_identity(event, runtime_session_id=runtime_session_id)
        for event in events
    )
    return tuple(sorted(identities, key=lambda item: item.identity_fingerprint))


def _observation_stream(
    event: TerminalProcessMonitorObservationCommittedEvent,
) -> TerminalOutputStreamIdentityFact:
    output = event.observation.output_authority
    cursor = getattr(output, "end_cursor", getattr(output, "terminal_cursor", None))
    if cursor is None:
        raise TerminalNotificationContractError("monitor observation lacks cursor")
    return cursor.stream_identity


def _completion_is_delivery_eligible(event: TerminalProcessCompletedEvent) -> bool:
    outcome = event.completion_semantic.outcome
    return not (
        outcome.status == "killed"
        and outcome.kill_reason in {"user_tool_kill", "teardown"}
    )


def _pending_sources_by_id(
    projection: HostIngressNotificationProjectionStateFact,
) -> dict[str, tuple[str, bool, str]]:
    result: dict[str, tuple[str, bool, str]] = {}
    for head in projection.process_heads:
        for monitor in head.monitor_heads:
            reference = monitor.pending_observation_event_reference
            if reference is not None:
                result[reference.event_id] = (
                    head.head_fingerprint,
                    False,
                    head.stream_identity.process_id,
                )
        completion = head.pending_completion_without_monitor_reference
        if completion is not None:
            result[completion.event_id] = (
                head.head_fingerprint,
                True,
                head.stream_identity.process_id,
            )
    return result


def _validate_run_start_notification_batch(
    *,
    event: RunStartEvent,
    events: tuple[AgentEvent, ...],
    projection: HostIngressNotificationProjectionStateFact,
    observation_events: dict[str, TerminalProcessMonitorObservationCommittedEvent],
    chain_states: dict[str, TerminalAutonomyChainStateFact],
    runtime_session_id: str,
) -> None:
    ingress = event.host_run_ingress
    proof = event.host_ingress_admission_proof
    if ingress is None or proof is None:
        return
    attachments = (
        ingress.source_notifications
        if isinstance(ingress, RuntimeRequestRunIngressFact)
        else ingress.attached_runtime_notifications
    )
    pending = _pending_sources_by_id(projection)
    selected_ids: list[str] = []
    selected_heads: list[str] = []
    for attachment in attachments:
        matches = tuple(
            item.event_id
            for item in attachment.source_event_references
            if item.event_id in pending
        )
        if not matches:
            raise TerminalNotificationAdmissionStale(
                "Host ingress notification source is no longer pending"
            )
        if len(matches) != 1:
            raise TerminalNotificationContractError(
                "Host ingress notification does not identify one pending source"
            )
        selected_ids.append(matches[0])
        selected_heads.append(pending[matches[0]][0])
    if tuple(selected_heads) != proof.selected_notification_head_fingerprints:
        raise TerminalNotificationAdmissionStale(
            "Host ingress notification head CAS failed"
        )
    dispositions = tuple(
        item
        for item in events
        if isinstance(item, TerminalProcessObservationDeliveryDispositionEvent)
        and {ref.event_id for ref in item.observation_source_references}
        == set(selected_ids)
    )
    if attachments:
        if len(dispositions) != 1:
            raise TerminalNotificationContractError(
                "Host RunStart must atomically disposition selected notifications"
            )
        disposition = dispositions[0]
        expected_outcome = (
            "autonomous_dispatched"
            if isinstance(ingress, RuntimeRequestRunIngressFact)
            else "merged_into_human_run"
        )
        if (
            disposition.outcome != expected_outcome
            or disposition.run_start_event_identity
            != stable_event_identity(event, runtime_session_id=runtime_session_id)
        ):
            raise TerminalNotificationContractError(
                "Host RunStart notification disposition identity drifted"
            )
        _validate_terminal_completion_releases(
            selected_source_ids=tuple(selected_ids),
            pending=pending,
            observation_events=observation_events,
            events=events,
        )
    elif dispositions:
        raise TerminalNotificationContractError(
            "Host RunStart dispositions notifications absent from ingress"
        )
    if not isinstance(ingress, RuntimeRequestRunIngressFact):
        if (
            proof.expected_autonomy_chain_state_fingerprint is not None
            or proof.proposed_automatic_delivery_ordinal is not None
        ):
            raise TerminalNotificationContractError(
                "human Host ingress cannot consume autonomy chain budget"
            )
        return
    delivery = ingress.autonomy_delivery
    state = chain_states.get(delivery.wake_chain_id)
    if state is None:
        raise TerminalNotificationContractError(
            "autonomous Host ingress lacks chain authority"
        )
    if (
        proof.expected_autonomy_chain_state_fingerprint != state.state_fingerprint
        or proof.proposed_automatic_delivery_ordinal
        != state.last_automatic_delivery_ordinal + 1
        or delivery.automatic_delivery_ordinal
        != proof.proposed_automatic_delivery_ordinal
        or delivery.chain_policy_fingerprint != state.chain_policy_fingerprint
    ):
        raise TerminalNotificationAdmissionStale(
            "autonomous Host ingress chain CAS failed"
        )


def _advance_candidate_chain_state(
    event: RunStartEvent,
    chain_states: dict[str, TerminalAutonomyChainStateFact],
) -> None:
    ingress = event.host_run_ingress
    if not isinstance(ingress, RuntimeRequestRunIngressFact):
        return
    delivery = ingress.autonomy_delivery
    before = chain_states[delivery.wake_chain_id]
    chain_states[delivery.wake_chain_id] = build_frozen_fact(
        TerminalAutonomyChainStateFact,
        schema_version="terminal_autonomy_chain_state.v1",
        wake_chain_id=before.wake_chain_id,
        state_revision=before.state_revision + 1,
        last_automatic_delivery_ordinal=delivery.automatic_delivery_ordinal,
        last_automatic_delivery_at_utc=event.created_at,
        chain_policy_fingerprint=before.chain_policy_fingerprint,
    )


def _validate_model_start_notification_batch(
    *,
    event: ModelCallStartEvent,
    events: tuple[AgentEvent, ...],
    projection: HostIngressNotificationProjectionStateFact,
    observation_events: dict[str, TerminalProcessMonitorObservationCommittedEvent],
    chain_states: dict[str, TerminalAutonomyChainStateFact],
    chain_attributions: dict[str, TerminalAutonomousDeliveryChainAttributionFact],
    runtime_session_id: str,
) -> None:
    prepared = event.active_run_monitor_delivery
    if prepared is None:
        return
    if not isinstance(prepared, HostActiveRunMonitorDeliveryFact):
        raise TerminalNotificationContractError(
            "ModelStart active-run monitor delivery has an invalid carrier"
        )
    guard = prepared.commit_guard
    if projection.state_fingerprint != guard.expected_notification_state_fingerprint:
        raise TerminalNotificationAdmissionStale(
            "active-run monitor notification projection CAS failed"
        )
    pending = _pending_sources_by_id(projection)
    dispositions = tuple(
        item
        for item in events
        if isinstance(item, TerminalProcessObservationDeliveryDispositionEvent)
        and item.outcome == "active_run_safe_point"
        and item.model_call_start_event_identity
        == stable_event_identity(event, runtime_session_id=runtime_session_id)
    )
    if len(dispositions) != 1:
        raise TerminalNotificationContractError(
            "active-run monitor ModelStart requires one exact disposition"
        )
    disposition = dispositions[0]
    source_ids = tuple(
        item.event_id for item in disposition.observation_source_references
    )
    if not source_ids or any(item not in pending for item in source_ids):
        raise TerminalNotificationContractError(
            "active-run monitor disposition does not cover pending observations"
        )
    selected_heads = tuple(pending[item][0] for item in source_ids)
    if selected_heads != guard.expected_selected_notification_head_fingerprints:
        raise TerminalNotificationAdmissionStale(
            "active-run monitor notification-head CAS failed"
        )
    attachments = tuple(
        _notification_attachment(
            observation_events[source_id],
            runtime_session_id=runtime_session_id,
        )
        for source_id in source_ids
        if source_id in observation_events
    )
    if (
        len(attachments) != len(source_ids)
        or tuple(item.attachment_fingerprint for item in attachments)
        != prepared.ordered_attachment_fingerprints
    ):
        raise TerminalNotificationContractError(
            "active-run monitor attachment identity drifted"
        )
    delivery = prepared.autonomy_delivery
    state = chain_states.get(delivery.wake_chain_id)
    attribution = chain_attributions.get(delivery.wake_chain_id)
    if state is None or attribution is None:
        raise TerminalNotificationContractError(
            "active-run monitor delivery lacks chain authority"
        )
    policy = attribution.resolved_policy
    if (
        state.state_fingerprint != guard.expected_autonomy_chain_state_fingerprint
        or delivery.automatic_delivery_ordinal
        != state.last_automatic_delivery_ordinal + 1
        or delivery.automatic_delivery_ordinal > policy.maximum_automatic_deliveries
        or delivery.chain_policy_fingerprint != policy.policy_fingerprint
        or disposition.autonomy_delivery != delivery
    ):
        raise TerminalNotificationAdmissionStale(
            "active-run monitor automatic-delivery chain CAS failed"
        )
    if state.last_automatic_delivery_at_utc is not None:
        previous = datetime.fromisoformat(
            state.last_automatic_delivery_at_utc.replace("Z", "+00:00")
        )
        current = datetime.fromisoformat(event.created_at.replace("Z", "+00:00"))
        if (
            current < previous
            or (current - previous).total_seconds()
            < policy.minimum_automatic_delivery_interval_seconds
        ):
            raise TerminalNotificationContractError(
                "active-run monitor automatic-delivery interval is not eligible"
            )
    _validate_terminal_completion_releases(
        selected_source_ids=source_ids,
        pending=pending,
        observation_events=observation_events,
        events=events,
    )


def _advance_candidate_model_start_chain_state(
    event: ModelCallStartEvent,
    chain_states: dict[str, TerminalAutonomyChainStateFact],
) -> None:
    prepared = event.active_run_monitor_delivery
    if prepared is None:
        return
    delivery = prepared.autonomy_delivery
    before = chain_states[delivery.wake_chain_id]
    chain_states[delivery.wake_chain_id] = build_frozen_fact(
        TerminalAutonomyChainStateFact,
        schema_version="terminal_autonomy_chain_state.v1",
        wake_chain_id=before.wake_chain_id,
        state_revision=before.state_revision + 1,
        last_automatic_delivery_ordinal=delivery.automatic_delivery_ordinal,
        last_automatic_delivery_at_utc=event.created_at,
        chain_policy_fingerprint=before.chain_policy_fingerprint,
    )


def _validate_tool_receipt_batch(
    *,
    event: ToolResultEndEvent,
    events: tuple[AgentEvent, ...],
    projection: HostIngressNotificationProjectionStateFact,
    observation_events: dict[str, TerminalProcessMonitorObservationCommittedEvent],
    completion_events: dict[str, TerminalProcessCompletedEvent],
    runtime_session_id: str,
) -> None:
    receipt = event.terminal_process_observation_receipt
    if receipt is None:
        return
    stream = receipt.observation_semantic.observed_end_cursor.stream_identity
    head = next(
        (item for item in projection.process_heads if item.stream_identity == stream),
        None,
    )
    if head is None:
        return
    dominated_ids: set[str] = set()
    for monitor in head.monitor_heads:
        reference = monitor.pending_observation_event_reference
        if reference is None:
            continue
        pending = observation_events.get(reference.event_id)
        if pending is None:
            raise TerminalNotificationContractError(
                "receipt validation cannot hydrate pending observation"
            )
        if terminal_receipt_dominates_observation(
            receipt=receipt,
            pending=pending.observation,
        ):
            if (
                not isinstance(
                    pending.observation,
                    TerminalProcessMonitorCompletionObservationSemanticFact,
                )
                or receipt.completion_event_reference
                == pending.completion_event_reference
            ):
                dominated_ids.add(pending.id)
    completion_ref = head.pending_completion_without_monitor_reference
    if completion_ref is None and receipt.action_kind == "kill":
        completion_ref = head.latest_completion_event_reference
    if completion_ref is not None:
        completion = completion_events.get(completion_ref.event_id)
        observed = receipt.observation_semantic.observed_state
        if completion is None or not isinstance(
            observed, TerminalProcessLifecycleOutcomeFact
        ):
            pass
        elif (
            receipt.completion_event_reference == completion_ref
            and observed == completion.completion_semantic.outcome
            and (
                head.pending_completion_without_monitor_reference is not None
                or (
                    receipt.action_kind == "kill"
                    and observed.status == "killed"
                    and observed.kill_reason == "user_tool_kill"
                )
            )
        ):
            dominated_ids.add(completion.id)
    matching = tuple(
        item
        for item in events
        if isinstance(item, TerminalProcessObservationDeliveryDispositionEvent)
        and item.outcome == "explicitly_observed"
        and {ref.event_id for ref in item.observation_source_references}
        == dominated_ids
    )
    if dominated_ids:
        if len(matching) != 1:
            raise TerminalNotificationContractError(
                "dominant terminal receipt requires one same-batch disposition"
            )
        if matching[0].tool_result_end_event_identity != stable_event_identity(
            event,
            runtime_session_id=runtime_session_id,
        ):
            raise TerminalNotificationContractError(
                "terminal receipt disposition ToolResult identity drifted"
            )
        _validate_terminal_completion_releases(
            selected_source_ids=tuple(sorted(dominated_ids)),
            pending=_pending_sources_by_id(projection),
            observation_events=observation_events,
            completion_events=completion_events,
            events=events,
        )
    elif matching:
        raise TerminalNotificationContractError(
            "non-dominant terminal receipt cannot consume a notification"
        )


def _validate_terminal_completion_releases(
    *,
    selected_source_ids: tuple[str, ...],
    pending: dict[str, tuple[str, bool, str]],
    observation_events: dict[str, TerminalProcessMonitorObservationCommittedEvent],
    events: tuple[AgentEvent, ...],
    completion_events: dict[str, TerminalProcessCompletedEvent] | None = None,
) -> None:
    terminal_process_ids: set[str] = set()
    for source_id in selected_source_ids:
        pending_entry = pending.get(source_id)
        if pending_entry is None:
            completion = (
                None if completion_events is None else completion_events.get(source_id)
            )
            if completion is not None:
                terminal_process_ids.add(completion.process_id)
            continue
        is_unmonitored_completion = pending_entry[1]
        observation = observation_events.get(source_id)
        is_monitored_completion = (
            observation is not None
            and observation.observation.observation_kind == "process_completed"
        )
        if not (is_unmonitored_completion or is_monitored_completion):
            completion = (
                None if completion_events is None else completion_events.get(source_id)
            )
            if completion is None:
                continue
            terminal_process_ids.add(completion.process_id)
        else:
            terminal_process_ids.add(pending_entry[2])
    release_ids = {
        item.transition.reservation.reservation_id
        for item in events
        if isinstance(item, TerminalNotificationReservationReleasedEvent)
        and item.transition.reservation.reservation_kind == "completion_process_head"
    }
    expected = {
        f"terminal_completion_head:{process_id}" for process_id in terminal_process_ids
    }
    if not expected.issubset(release_ids):
        raise TerminalNotificationContractError(
            "terminal notification delivery lacks completion reservation release"
        )


def _notification_attachment(
    event: TerminalProcessCompletedEvent
    | TerminalProcessMonitorObservationCommittedEvent,
    *,
    runtime_session_id: str,
) -> HostRuntimeNotificationAttachmentFact:
    if isinstance(event, TerminalProcessMonitorObservationCommittedEvent):
        semantic = event.observation
        output = semantic.output_authority
        process_id = (
            output.end_cursor.stream_identity.process_id
            if hasattr(output, "end_cursor")
            else output.terminal_cursor.stream_identity.process_id
        )
        monitor_id = semantic.monitor_id
        observation_kind = semantic.observation_kind
        source_semantic_fingerprint = semantic.observation_semantic_fingerprint
        content = _monitor_observation_content(event)
        references = [
            event_reference_from_stored(
                event,
                runtime_session_id=runtime_session_id,
            )
        ]
        if event.completion_event_reference is not None:
            references.append(event.completion_event_reference)
        wake_chain_id = event.wake_chain_id
    else:
        process_id = event.process_id
        monitor_id = None
        observation_kind = "process_completed"
        source_semantic_fingerprint = (
            event.completion_semantic.completion_semantic_fingerprint
        )
        content = _completion_content(event)
        references = [
            event_reference_from_stored(
                event,
                runtime_session_id=runtime_session_id,
            )
        ]
        wake_chain_id = None
    payload = terminal_monitor_observation_payload(
        process_id=process_id,
        monitor_id=monitor_id,
        observation_kind=observation_kind,
        source_semantic_fingerprint=source_semantic_fingerprint,
        model_visible_content=content,
    )
    occurrence = context_fingerprint(
        "terminal-monitor-notification-occurrence:v1",
        source_semantic_fingerprint,
    )
    encoded = encode_runtime_observation(
        payload,
        observation_kind="terminal_process_monitor_observation",
        source_instance_id=(
            f"terminal-monitor:{monitor_id}"
            if monitor_id is not None
            else f"terminal-process:{process_id}:completion"
        ),
        lifecycle_class="causal_append_once",
        authority_class="runtime_fact",
        causal_occurrence_semantic_fingerprint=occurrence,
    )
    return build_frozen_fact(
        HostRuntimeNotificationAttachmentFact,
        schema_version="host_runtime_notification_attachment.v1",
        observation_wire_semantic=encoded.semantic_fact,
        source_event_references=tuple(
            sorted(
                {item.event_id: item for item in references}.values(),
                key=lambda item: (
                    item.runtime_session_id,
                    item.sequence,
                    item.event_id,
                ),
            )
        ),
        wake_chain_id=wake_chain_id,
    )


def _monitor_observation_content(
    event: TerminalProcessMonitorObservationCommittedEvent,
) -> str:
    semantic = event.observation
    output = semantic.output_authority
    if hasattr(output, "output_preview"):
        preview = output.output_preview
        unavailable_reason = None
    else:
        preview = ""
        unavailable_reason = output.recovery_reason
    payload: dict[str, object] = {
        "kind": semantic.observation_kind,
        "monitor_id": semantic.monitor_id,
        "observation_ordinal": semantic.observation_ordinal,
        "output": preview,
        "output_unavailable_reason": unavailable_reason,
    }
    if semantic.observation_kind == "process_completed":
        outcome = semantic.completion_semantic.outcome
        payload["process_status"] = outcome.status
        payload["exit_code"] = outcome.exit_code
        payload["kill_reason"] = outcome.kill_reason
    else:
        payload["process_status"] = "running"
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _completion_content(event: TerminalProcessCompletedEvent) -> str:
    outcome = event.completion_semantic.outcome
    return json.dumps(
        {
            "exit_code": outcome.exit_code,
            "kill_reason": outcome.kill_reason,
            "kind": "process_completed",
            "output": event.output_preview,
            "output_truncated": event.output_truncated,
            "process_id": event.process_id,
            "process_status": outcome.status,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "HostIngressNotificationProjectionStore",
    "NOTIFICATION_REDUCER_CONTRACT_FINGERPRINT",
    "PreparedTerminalNotificationReservation",
    "PendingTerminalNotification",
    "TerminalNotificationAccountCoordinator",
    "TerminalNotificationAdmissionStale",
    "TerminalNotificationCapacityError",
    "TerminalNotificationContractError",
    "initial_notification_account_state",
    "initial_notification_projection_state",
]
