"""Durable repeated terminal monitor ownership and reduction.

This module deliberately has no Host scheduling policy.  It owns the monitor
lifecycle up to a confirmed notification projection; Host ingress consumes
that projection in the following implementation stage.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Event, RLock, Thread
from time import monotonic, sleep
from typing import TYPE_CHECKING, Literal, Sequence

from pulsara_agent.event import (
    AgentEvent,
    RunStartEvent,
    TerminalProcessCompletedEvent,
    TerminalNotificationReservationReleasedEvent,
    TerminalProcessMonitorObservationCommittedEvent,
    TerminalProcessMonitorReceiptAppliedEvent,
    TerminalProcessMonitorRegisteredEvent,
    TerminalProcessMonitorTerminatedEvent,
    TerminalProcessObservationDeliveryDispositionEvent,
    ToolResultEndEvent,
)
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.event_log.serialization import dump_agent_event, load_agent_event
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.host_ingress import RuntimeRequestRunIngressFact
from pulsara_agent.primitives.terminal_observation import (
    MAXIMUM_TERMINAL_MONITOR_DURATION_SECONDS,
    ResolvedTerminalAutonomyChainPolicyFact,
    TerminalAutonomousDeliveryChainAttributionFact,
    TerminalAutonomyPermissionAuthorityFact,
    TerminalOutputDeltaAttributionFact,
    TerminalOutputDeltaSemanticFact,
    TerminalOutputCursorFact,
    TerminalProcessLifecycleOutcomeFact,
    TerminalProcessCompletionSemanticFact,
    TerminalProcessMonitorCompletionObservationSemanticFact,
    TerminalProcessMonitorConditionsFact,
    TerminalProcessMonitorCancelIntentFact,
    TerminalProcessMonitorCancellationSemanticFact,
    TerminalProcessMonitorCoreStateFact,
    TerminalProcessMonitorDeliveryPolicyFact,
    TerminalProcessMonitorExpiryObservationSemanticFact,
    TerminalProcessMonitorLifetimeFact,
    TerminalProcessMonitorObservationSemanticFact,
    TerminalProcessMonitorOutputConditionFact,
    TerminalProcessMonitorPolicyFact,
    TerminalProcessMonitorProgressLimiterStateFact,
    TerminalProcessMonitorProgressObservationSemanticFact,
    TerminalProcessMonitorRegistrationAttributionFact,
    TerminalProcessMonitorRegistrationSemanticFact,
    TerminalProcessMonitorStateTransitionFact,
    TerminalProcessMonitorTerminationSemanticFact,
    TerminalProcessObservationReceiptFact,
    UnavailableRecoveredTerminalOutputDeltaFact,
    advance_progress_limiter,
    build_running_terminal_process_state,
    progress_limiter_decision,
    terminal_receipt_dominates_observation,
)
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored
from pulsara_agent.runtime.event_write_service import (
    PendingRuntimeEventWriteError,
    RuntimeEventWriteCancelled,
)
from pulsara_agent.runtime.terminal.output import (
    _SpoolAuthorityConflict,
    recover_terminal_output_delta,
)
from pulsara_agent.runtime.terminal.process import TerminalProcessState
from pulsara_agent.runtime.terminal.models import TerminalResult
from pulsara_agent.runtime.terminal.process import (
    snapshot_process_for_monitor_registration,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession
    from pulsara_agent.runtime.terminal.notification import (
        PreparedTerminalNotificationReservation,
    )
    from pulsara_agent.tools.base import ToolRuntimeContext
    from pulsara_agent.runtime.terminal.ui_stream import TerminalMonitorEventChannel


_MONITOR_RETRY_SECONDS = 0.2
_MONITOR_POLL_SECONDS = 0.1
_DEFAULT_MAX_OUTPUT_CHARS = 4_000
_DEFAULT_MAX_PROGRESS = 119
TERMINAL_MONITOR_CHECKPOINT_KIND = "terminal_monitor_projection.v1"
TERMINAL_MONITOR_CHECKPOINT_SCHEMA_VERSION = "terminal_monitor_projection_checkpoint.v2"
MONITOR_REDUCER_CONTRACT_FINGERPRINT = context_fingerprint(
    "terminal-monitor-projection-reducer:v1",
    {
        "records": "monitor-id-ordered-current-only",
        "lifecycle": "registered-observed-receipted-terminated-cas",
        "pending_delivery": "one-confirmed-observation-per-monitor",
    },
)


class TerminalMonitorContractError(RuntimeError):
    """A monitor candidate cannot be joined to its durable authority."""


class TerminalMonitorRecoveryBlocked(TerminalMonitorContractError):
    """Restart recovery could not settle before the Host open deadline."""


@dataclass(frozen=True, slots=True)
class PreparedTerminalProcessMonitorRegistration:
    registration_semantic: TerminalProcessMonitorRegistrationSemanticFact
    registration_attribution: TerminalProcessMonitorRegistrationAttributionFact
    initial_core_state: TerminalProcessMonitorCoreStateFact
    registered_event: TerminalProcessMonitorRegisteredEvent
    notification_reservation: "PreparedTerminalNotificationReservation | None"
    initial_observation_result: TerminalResult


@dataclass(frozen=True, slots=True)
class PreparedTerminalProcessMonitorCancellation:
    monitor_id: str
    outcome: Literal["cancelled", "already_terminal"]
    cancellation_semantic: TerminalProcessMonitorCancellationSemanticFact | None
    stable_candidates: tuple[AgentEvent, ...]


@dataclass(slots=True)
class _DormantRegistrationOwner:
    prepared: PreparedTerminalProcessMonitorRegistration
    process: TerminalProcessState
    owner_state: Literal[
        "dormant", "active", "terminated", "reconciliation_required"
    ] = "dormant"


@dataclass(slots=True)
class _FiringOwner:
    monitor_id: str
    stable_candidates: tuple[AgentEvent, ...]
    source_state_fingerprint: str
    attempts: int = 0
    cancel_intent: TerminalProcessMonitorCancelIntentFact | None = None


@dataclass(slots=True)
class TerminalMonitorRecord:
    registration_event: TerminalProcessMonitorRegisteredEvent
    registration_reference: object
    core_state: TerminalProcessMonitorCoreStateFact
    pending_observation_event: (
        TerminalProcessMonitorObservationCommittedEvent | None
    ) = None
    latest_observation_event: TerminalProcessMonitorObservationCommittedEvent | None = (
        None
    )
    latest_termination_event: TerminalProcessMonitorTerminatedEvent | None = None


class TerminalMonitorStore:
    """Single incremental reducer for durable monitor state."""

    def __init__(
        self,
        events: Sequence[AgentEvent] = (),
        *,
        runtime_session_id: str,
        through_sequence: int = 0,
        retain_terminal_history: bool = False,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self._retain_terminal_history = retain_terminal_history
        self.through_sequence = 0
        self._lock = RLock()
        self._records: dict[str, TerminalMonitorRecord] = {}
        self._observation_by_event_id: dict[
            str, TerminalProcessMonitorObservationCommittedEvent
        ] = {}
        self._tool_result_by_identity: dict[str, ToolResultEndEvent] = {}
        if events:
            self.apply_committed(tuple(events))
        self.through_sequence = max(self.through_sequence, through_sequence)

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
    ) -> "TerminalMonitorStore":
        if payload.get("schema_version") != TERMINAL_MONITOR_CHECKPOINT_SCHEMA_VERSION:
            raise TerminalMonitorContractError(
                "terminal monitor checkpoint schema is unsupported"
            )
        if payload.get("runtime_session_id") != runtime_session_id:
            raise TerminalMonitorContractError(
                "terminal monitor checkpoint runtime identity drifted"
            )
        if (
            payload.get("reducer_contract_fingerprint")
            != MONITOR_REDUCER_CONTRACT_FINGERPRINT
        ):
            raise TerminalMonitorContractError(
                "terminal monitor checkpoint reducer contract drifted"
            )
        through_sequence = int(payload.get("through_sequence", -1))
        if through_sequence < 0:
            raise TerminalMonitorContractError(
                "terminal monitor checkpoint sequence is invalid"
            )
        store = cls(
            runtime_session_id=runtime_session_id,
            through_sequence=through_sequence,
        )
        for item in payload.get("current_records", ()):
            registration = load_agent_event(item["registration_event"])
            pending = (
                None
                if item.get("pending_observation_event") is None
                else load_agent_event(item["pending_observation_event"])
            )
            latest = (
                None
                if item.get("latest_observation_event") is None
                else load_agent_event(item["latest_observation_event"])
            )
            termination = (
                None
                if item.get("latest_termination_event") is None
                else load_agent_event(item["latest_termination_event"])
            )
            if (
                not isinstance(registration, TerminalProcessMonitorRegisteredEvent)
                or (
                    pending is not None
                    and not isinstance(
                        pending, TerminalProcessMonitorObservationCommittedEvent
                    )
                )
                or (
                    latest is not None
                    and not isinstance(
                        latest, TerminalProcessMonitorObservationCommittedEvent
                    )
                )
                or (
                    termination is not None
                    and not isinstance(
                        termination, TerminalProcessMonitorTerminatedEvent
                    )
                )
            ):
                raise TerminalMonitorContractError(
                    "terminal monitor checkpoint event type drifted"
                )
            core = TerminalProcessMonitorCoreStateFact.model_validate(
                item["core_state"]
            )
            if core.lifecycle_state == "terminated":
                raise TerminalMonitorContractError(
                    "terminal monitor checkpoint retained historical records"
                )
            monitor_id = registration.registration_semantic.monitor_id
            if core.monitor_id != monitor_id or monitor_id in store._records:
                raise TerminalMonitorContractError(
                    "terminal monitor checkpoint identity is ambiguous"
                )
            if (
                registration.sequence is None
                or registration.sequence > through_sequence
            ):
                raise TerminalMonitorContractError(
                    "terminal monitor checkpoint registration authority is invalid"
                )
            reference = event_reference_from_stored(
                registration,
                runtime_session_id=runtime_session_id,
            )
            for observation in (pending, latest):
                if observation is None:
                    continue
                if (
                    observation.sequence is None
                    or observation.sequence > through_sequence
                    or observation.registration_event_reference != reference
                    or observation.observation.monitor_id != monitor_id
                ):
                    raise TerminalMonitorContractError(
                        "terminal monitor checkpoint observation authority drifted"
                    )
            pending_required = core.lifecycle_state in {
                "active_pending_delivery",
                "terminal_pending_delivery",
            }
            if pending_required != (pending is not None):
                raise TerminalMonitorContractError(
                    "terminal monitor checkpoint pending lifecycle matrix drifted"
                )
            if (core.pending_observation_semantic_fingerprint is None) != (
                pending is None
            ):
                raise TerminalMonitorContractError(
                    "terminal monitor checkpoint pending fingerprint matrix drifted"
                )
            if pending is not None and (
                latest is None
                or latest.id != pending.id
                or pending.observation.observation_semantic_fingerprint
                != core.pending_observation_semantic_fingerprint
                or pending.monitor_state_transition.after_core_state_fingerprint
                != core.core_state_fingerprint
            ):
                raise TerminalMonitorContractError(
                    "terminal monitor checkpoint pending core join failed"
                )
            if latest is not None and (
                latest.observation.observation_ordinal
                != core.last_committed_observation_ordinal
            ):
                raise TerminalMonitorContractError(
                    "terminal monitor checkpoint observation frontier drifted"
                )
            store._records[monitor_id] = TerminalMonitorRecord(
                registration_event=registration,
                registration_reference=reference,
                core_state=core,
                pending_observation_event=pending,
                latest_observation_event=latest,
                latest_termination_event=termination,
            )
            for observation in (pending, latest):
                if observation is not None:
                    store._observation_by_event_id[observation.id] = observation
        return store

    def checkpoint_payload(self) -> dict[str, object]:
        current, omitted = self.current_snapshots(maximum_items=8)
        if omitted:
            raise TerminalMonitorContractError(
                "terminal monitor checkpoint exceeds the active monitor bound"
            )
        return {
            "schema_version": TERMINAL_MONITOR_CHECKPOINT_SCHEMA_VERSION,
            "runtime_session_id": self.runtime_session_id,
            "reducer_contract_fingerprint": MONITOR_REDUCER_CONTRACT_FINGERPRINT,
            "through_sequence": self.through_sequence,
            "current_records": tuple(
                {
                    "registration_event": dump_agent_event(item.registration_event),
                    "core_state": item.core_state.model_dump(mode="json"),
                    "pending_observation_event": (
                        None
                        if item.pending_observation_event is None
                        else dump_agent_event(item.pending_observation_event)
                    ),
                    "latest_observation_event": (
                        None
                        if item.latest_observation_event is None
                        else dump_agent_event(item.latest_observation_event)
                    ),
                    "latest_termination_event": (
                        None
                        if item.latest_termination_event is None
                        else dump_agent_event(item.latest_termination_event)
                    ),
                }
                for item in current
            ),
        }

    def rebuild(self, events: tuple[AgentEvent, ...]) -> None:
        with self._lock:
            self.through_sequence = 0
            self._records.clear()
            self._observation_by_event_id.clear()
            self._tool_result_by_identity.clear()
        self.apply_committed(events)

    def apply_committed(self, events: tuple[AgentEvent, ...]) -> None:
        required_tool_result_identities = {
            event.tool_result_end_event_identity.identity_fingerprint
            for event in events
            if isinstance(event, TerminalProcessMonitorReceiptAppliedEvent)
        }
        with self._lock:
            for event in events:
                if event.sequence is None:
                    raise TerminalMonitorContractError(
                        "terminal monitor reducer requires committed events"
                    )
                if event.sequence <= self.through_sequence:
                    continue
                if isinstance(event, TerminalProcessMonitorRegisteredEvent):
                    self._apply_registration(event)
                elif isinstance(event, TerminalProcessMonitorObservationCommittedEvent):
                    self._apply_observation(event)
                elif isinstance(
                    event, TerminalProcessObservationDeliveryDispositionEvent
                ):
                    self._apply_disposition(event)
                elif isinstance(event, TerminalProcessMonitorTerminatedEvent):
                    self._apply_termination(event)
                elif isinstance(event, ToolResultEndEvent):
                    identity = stable_event_identity(
                        event,
                        runtime_session_id=self.runtime_session_id,
                    )
                    if identity.identity_fingerprint in required_tool_result_identities:
                        self._tool_result_by_identity[identity.identity_fingerprint] = (
                            event
                        )
                elif isinstance(event, TerminalProcessMonitorReceiptAppliedEvent):
                    self._apply_receipt_application(event)
                self.through_sequence = event.sequence

    def snapshot(self, monitor_id: str) -> TerminalMonitorRecord:
        with self._lock:
            try:
                record = self._records[monitor_id]
            except KeyError as exc:
                raise KeyError(f"terminal monitor not found: {monitor_id}") from exc
            return TerminalMonitorRecord(
                registration_event=record.registration_event,
                registration_reference=record.registration_reference,
                core_state=record.core_state,
                pending_observation_event=record.pending_observation_event,
                latest_observation_event=record.latest_observation_event,
                latest_termination_event=record.latest_termination_event,
            )

    def snapshots(self) -> tuple[TerminalMonitorRecord, ...]:
        with self._lock:
            ids = tuple(sorted(self._records))
        return tuple(self.snapshot(item) for item in ids)

    def current_snapshots(
        self,
        *,
        maximum_items: int = 8,
    ) -> tuple[tuple[TerminalMonitorRecord, ...], int]:
        if maximum_items < 1 or maximum_items > 8:
            raise ValueError("terminal monitor inventory bound is invalid")
        current = tuple(
            item
            for item in self.snapshots()
            if item.core_state.lifecycle_state != "terminated"
        )
        return current[:maximum_items], max(0, len(current) - maximum_items)

    def validate_receipt_application_batch(
        self, events: tuple[AgentEvent, ...]
    ) -> None:
        """Validate receipt cursor transitions against the pre-commit reducer head."""

        records = {
            item.registration_event.registration_semantic.monitor_id: item
            for item in self.snapshots()
        }
        with self._lock:
            tool_results = dict(self._tool_result_by_identity)
        for event in events:
            if isinstance(event, ToolResultEndEvent):
                identity = stable_event_identity(
                    event,
                    runtime_session_id=self.runtime_session_id,
                )
                tool_results[identity.identity_fingerprint] = event
                continue
            if not isinstance(event, TerminalProcessMonitorReceiptAppliedEvent):
                continue
            record = next(
                (
                    item
                    for item in records.values()
                    if item.registration_reference == event.registration_event_reference
                ),
                None,
            )
            if record is None:
                raise TerminalMonitorContractError(
                    "terminal receipt candidate lacks monitor registration"
                )
            tool_result = tool_results.get(
                event.tool_result_end_event_identity.identity_fingerprint
            )
            after = self._validated_receipt_application_state(
                record=record,
                event=event,
                tool_result=tool_result,
            )
            record.core_state = after

    def _apply_registration(self, event: TerminalProcessMonitorRegisteredEvent) -> None:
        monitor_id = event.registration_semantic.monitor_id
        if monitor_id in self._records:
            existing = self._records[monitor_id].registration_event
            if existing.id != event.id:
                raise TerminalMonitorContractError(
                    "terminal monitor registration identity is ambiguous"
                )
            return
        core = initial_monitor_core_state(event.registration_semantic)
        if (
            core.core_state_fingerprint
            != event.resulting_monitor_core_state_fingerprint
        ):
            raise TerminalMonitorContractError(
                "terminal monitor registration core state drifted"
            )
        reference = event_reference_from_stored(
            event,
            runtime_session_id=self.runtime_session_id,
        )
        self._records[monitor_id] = TerminalMonitorRecord(
            registration_event=event,
            registration_reference=reference,
            core_state=core,
        )

    def _apply_observation(
        self, event: TerminalProcessMonitorObservationCommittedEvent
    ) -> None:
        monitor_id = event.observation.monitor_id
        record = self._records.get(monitor_id)
        if record is None:
            raise TerminalMonitorContractError(
                "terminal monitor observation precedes registration"
            )
        if event.registration_event_reference != record.registration_reference:
            raise TerminalMonitorContractError(
                "terminal monitor observation registration reference drifted"
            )
        before = record.core_state
        if (
            event.monitor_state_transition.source_revision != before.state_revision
            or event.monitor_state_transition.before_core_state_fingerprint
            != before.core_state_fingerprint
            or event.observation.observation_ordinal
            != before.last_committed_observation_ordinal + 1
        ):
            raise TerminalMonitorContractError(
                "terminal monitor observation CAS failed"
            )
        after = resulting_observation_core_state(
            before=before,
            observation=event.observation,
            observed_at_utc=event.observed_at_utc,
            delivery_policy=record.registration_event.registration_semantic.policy.delivery,
        )
        if (
            event.monitor_state_transition.result_revision != after.state_revision
            or event.monitor_state_transition.after_core_state_fingerprint
            != after.core_state_fingerprint
        ):
            raise TerminalMonitorContractError(
                "terminal monitor observation resulting state drifted"
            )
        record.core_state = after
        record.latest_observation_event = event
        record.pending_observation_event = event
        self._observation_by_event_id[event.id] = event

    def _apply_disposition(
        self, event: TerminalProcessObservationDeliveryDispositionEvent
    ) -> None:
        retired_monitor_ids: set[str] = set()
        for source in event.observation_source_references:
            observation = self._observation_by_event_id.get(source.event_id)
            if observation is None:
                # Unmonitored process-completion notifications share the Host
                # disposition vocabulary but are not monitor reducer inputs.
                continue
            record = self._records[observation.observation.monitor_id]
            pending = record.pending_observation_event
            # A completion can supersede an older progress in the same batch.
            # In that case the old disposition is audit-only for the current
            # terminal pending state.
            if pending is None or pending.id != observation.id:
                if event.outcome == "superseded_by_terminal_observation":
                    continue
                raise TerminalMonitorContractError(
                    "terminal delivery disposition is not current"
                )
            record.core_state = resulting_disposition_core_state(
                before=record.core_state,
                observation=observation.observation,
                delivery_policy=(
                    record.registration_event.registration_semantic.policy.delivery
                ),
                consumed_through_cursor=self._explicit_receipt_end_cursor(
                    event=event,
                    observation=observation,
                ),
            )
            record.pending_observation_event = None
            if record.core_state.lifecycle_state == "terminated":
                retired_monitor_ids.add(record.core_state.monitor_id)
        for monitor_id in retired_monitor_ids:
            self._retire_terminal_record(monitor_id)

    def _explicit_receipt_end_cursor(
        self,
        *,
        event: TerminalProcessObservationDeliveryDispositionEvent,
        observation: TerminalProcessMonitorObservationCommittedEvent,
    ) -> TerminalOutputCursorFact | None:
        if event.outcome != "explicitly_observed":
            return None
        identity = event.tool_result_end_event_identity
        if identity is None:
            raise TerminalMonitorContractError(
                "explicit terminal observation disposition lacks ToolResult identity"
            )
        tool_result = self._tool_result_by_identity.get(identity.identity_fingerprint)
        if (
            tool_result is None
            or stable_event_identity(
                tool_result,
                runtime_session_id=self.runtime_session_id,
            )
            != identity
        ):
            raise TerminalMonitorContractError(
                "explicit terminal observation ToolResult cannot be hydrated"
            )
        receipt = tool_result.terminal_process_observation_receipt
        if receipt is None or not terminal_receipt_dominates_observation(
            receipt=receipt,
            pending=observation.observation,
        ):
            raise TerminalMonitorContractError(
                "explicit terminal observation receipt does not dominate its source"
            )
        if (
            isinstance(
                observation.observation,
                TerminalProcessMonitorCompletionObservationSemanticFact,
            )
            and receipt.completion_event_reference
            != observation.completion_event_reference
        ):
            raise TerminalMonitorContractError(
                "explicit terminal completion receipt identity drifted"
            )
        return receipt.observation_semantic.observed_end_cursor

    def _apply_termination(self, event: TerminalProcessMonitorTerminatedEvent) -> None:
        monitor_id = event.termination_semantic.monitor_id
        record = self._records.get(monitor_id)
        if record is None:
            raise TerminalMonitorContractError(
                "terminal monitor termination precedes registration"
            )
        before = record.core_state
        after = build_frozen_fact(
            TerminalProcessMonitorCoreStateFact,
            schema_version="terminal_process_monitor_core_state.v1",
            monitor_id=monitor_id,
            state_revision=before.state_revision + 1,
            lifecycle_state="terminated",
            last_observation_cursor=before.last_observation_cursor,
            last_consumed_cursor=before.last_consumed_cursor,
            last_committed_observation_ordinal=(
                before.last_committed_observation_ordinal
            ),
            committed_progress_observation_count=(
                before.committed_progress_observation_count
            ),
            progress_limiter_state=before.progress_limiter_state,
            pending_observation_semantic_fingerprint=None,
            terminal_reason=event.termination_semantic.terminal_reason,
        )
        transition = event.monitor_state_transition
        if (
            transition.source_revision != before.state_revision
            or transition.before_core_state_fingerprint != before.core_state_fingerprint
            or transition.result_revision != after.state_revision
            or transition.after_core_state_fingerprint != after.core_state_fingerprint
        ):
            raise TerminalMonitorContractError(
                "terminal monitor termination CAS failed"
            )
        record.core_state = after
        record.pending_observation_event = None
        record.latest_termination_event = event
        self._retire_terminal_record(monitor_id)

    def _retire_terminal_record(self, monitor_id: str) -> None:
        if self._retain_terminal_history:
            return
        self._records.pop(monitor_id, None)
        self._observation_by_event_id = {
            event_id: observation
            for event_id, observation in self._observation_by_event_id.items()
            if observation.observation.monitor_id != monitor_id
        }

    def _apply_receipt_application(
        self, event: TerminalProcessMonitorReceiptAppliedEvent
    ) -> None:
        registration = next(
            (
                item
                for item in self._records.values()
                if item.registration_reference == event.registration_event_reference
            ),
            None,
        )
        if registration is None:
            raise TerminalMonitorContractError(
                "terminal receipt application lacks monitor registration"
            )
        tool_result = self._tool_result_by_identity.get(
            event.tool_result_end_event_identity.identity_fingerprint
        )
        registration.core_state = self._validated_receipt_application_state(
            record=registration,
            event=event,
            tool_result=tool_result,
        )
        self._tool_result_by_identity.pop(
            event.tool_result_end_event_identity.identity_fingerprint,
            None,
        )

    def _validated_receipt_application_state(
        self,
        *,
        record: TerminalMonitorRecord,
        event: TerminalProcessMonitorReceiptAppliedEvent,
        tool_result: ToolResultEndEvent | None,
    ) -> TerminalProcessMonitorCoreStateFact:
        if (
            tool_result is None
            or stable_event_identity(
                tool_result,
                runtime_session_id=self.runtime_session_id,
            )
            != event.tool_result_end_event_identity
        ):
            raise TerminalMonitorContractError(
                "terminal receipt application cannot hydrate ToolResult"
            )
        receipt = tool_result.terminal_process_observation_receipt
        if receipt is None or receipt.receipt_fingerprint != event.receipt_fingerprint:
            raise TerminalMonitorContractError(
                "terminal receipt application identity drifted"
            )
        pending = record.pending_observation_event
        pending_reference = (
            None
            if pending is None
            else event_reference_from_stored(
                pending,
                runtime_session_id=self.runtime_session_id,
            )
        )
        if pending_reference != event.pending_observation_event_reference:
            raise TerminalMonitorContractError(
                "terminal receipt application pending observation drifted"
            )
        before = record.core_state
        after = resulting_receipt_core_state(
            before=before,
            receipt=receipt,
            pending=None if pending is None else pending.observation,
        )
        transition = event.monitor_state_transition
        if (
            transition.source_revision != before.state_revision
            or transition.before_core_state_fingerprint != before.core_state_fingerprint
            or transition.result_revision != after.state_revision
            or transition.after_core_state_fingerprint != after.core_state_fingerprint
            or event.observed_end_cursor
            != receipt.observation_semantic.observed_end_cursor
        ):
            raise TerminalMonitorContractError(
                "terminal receipt application state transition drifted"
            )
        return after


class TerminalMonitorCoordinator:
    """Session-owned physical owner for monitor registrations and observations."""

    def __init__(
        self,
        *,
        runtime_session: RuntimeSession,
        store: TerminalMonitorStore,
        event_channel: TerminalMonitorEventChannel | None = None,
    ) -> None:
        self.runtime_session = runtime_session
        self.store = store
        self.event_channel = event_channel
        self._lock = RLock()
        self._dormant: dict[str, _DormantRegistrationOwner] = {}
        self._processes: dict[str, TerminalProcessState] = {}
        self._firing: dict[str, _FiringOwner] = {}
        self._workers: dict[str, tuple[Thread, Event]] = {}
        self._restart_pending_delivery: dict[str, str] = {}
        self._restart_recovery_workers: dict[str, Thread] = {}
        self._closed = False

    def prepare_registration(
        self,
        *,
        process_id: str,
        origin_tool_call_id: str,
        runtime_context: ToolRuntimeContext,
        conditions: TerminalProcessMonitorConditionsFact,
        delivery: TerminalProcessMonitorDeliveryPolicyFact,
        lifetime: TerminalProcessMonitorLifetimeFact,
        registered_at_utc: str | None = None,
    ) -> PreparedTerminalProcessMonitorRegistration:
        with self._lock:
            if self._closed:
                raise TerminalMonitorContractError(
                    "terminal monitor coordinator is closed"
                )
        if runtime_context.run_entry_kind != "host_main_run":
            raise TerminalMonitorContractError(
                "terminal_monitor_child_registration_unsupported"
            )
        owner_host_session_id = self.runtime_session.terminal_owner_host_session_id
        if owner_host_session_id is None:
            raise TerminalMonitorContractError(
                "terminal_monitor_requires_host_session_owner"
            )
        process = self.runtime_session.terminal_sessions.monitorable_process(
            process_id,
            owner_host_session_id=owner_host_session_id,
            origin_runtime_session_id=runtime_context.runtime_session_id,
        )
        initial_observation_result = snapshot_process_for_monitor_registration(
            process,
            max_output_chars=delivery.max_output_chars,
        )
        initial_observation = initial_observation_result.observation_semantic
        if initial_observation is None:
            raise TerminalMonitorContractError(
                "terminal monitor registration lacks an initial observation"
            )
        run_start = self._run_start(runtime_context.event_context.run_id)
        run_reference = event_reference_from_stored(
            run_start,
            runtime_session_id=self.runtime_session.runtime_session_id,
        )
        registered_at = _canonical_now(registered_at_utc)
        expires_at = _add_seconds(
            registered_at,
            lifetime.maximum_duration_seconds,
        )
        monitor_id = _stable_id(
            "terminal_monitor",
            self.runtime_session.runtime_session_id,
            runtime_context.event_context.run_id,
            origin_tool_call_id,
            process_id,
        )
        policy = build_frozen_fact(
            TerminalProcessMonitorPolicyFact,
            schema_version="terminal_process_monitor_policy.v1",
            conditions=conditions,
            delivery=delivery,
            lifetime=lifetime,
        )
        semantic = build_frozen_fact(
            TerminalProcessMonitorRegistrationSemanticFact,
            schema_version="terminal_process_monitor_registration_semantic.v1",
            monitor_id=monitor_id,
            initial_baseline_cursor=initial_observation.observed_end_cursor,
            policy=policy,
        )
        chain = self._registration_chain(
            run_start=run_start,
            run_reference=run_reference,
        )
        permission_policy_fingerprint = context_fingerprint(
            "terminal-monitor-permission-policy:v1",
            runtime_context.permission_policy or {},
        )
        permission = build_frozen_fact(
            TerminalAutonomyPermissionAuthorityFact,
            schema_version="terminal_autonomy_permission_authority.v1",
            registration_permission_snapshot_id=(
                runtime_context.permission_snapshot_id or "permission:implicit"
            ),
            registration_permission_mode=(runtime_context.permission_mode or "default"),
            registration_permission_policy_fingerprint=(permission_policy_fingerprint),
            scheduling_policy_id="terminal-monitor-autonomy",
            scheduling_policy_version=1,
            scheduling_policy_fingerprint=context_fingerprint(
                "terminal-monitor-scheduling-policy:v1",
                {"human_priority": True, "automatic_delivery_bound": 12},
            ),
            caller_owner_kind="host_main_run",
        )
        attribution = build_frozen_fact(
            TerminalProcessMonitorRegistrationAttributionFact,
            schema_version="terminal_process_monitor_registration_attribution.v1",
            owner_host_session_id=owner_host_session_id,
            owner_conversation_id=(self.runtime_session.terminal_owner_conversation_id),
            origin_runtime_session_id=runtime_context.runtime_session_id,
            process_origin_runtime_session_id=(process.origin_runtime_session_id or ""),
            process_origin_run_entry_kind="host_main_run",
            origin_run_event_reference=run_reference,
            origin_tool_call_id=origin_tool_call_id,
            registered_at_utc=registered_at,
            expires_at_utc=expires_at,
            permission_authority=permission,
            wake_chain=chain,
        )
        initial = initial_monitor_core_state(semantic)
        event_id = f"terminal_monitor_registered:{monitor_id}"
        event = TerminalProcessMonitorRegisteredEvent(
            id=event_id,
            **runtime_context.event_context.event_fields(),
            registration_semantic=semantic,
            registration_attribution=attribution,
            resulting_monitor_core_state_fingerprint=initial.core_state_fingerprint,
            tool_result_end_event_id=(
                f"tool_result_end:{runtime_context.event_context.run_id}:"
                f"{origin_tool_call_id}"
            ),
            notification_reservation_id=f"terminal_monitor_slot:{monitor_id}",
        )
        prepared = PreparedTerminalProcessMonitorRegistration(
            registration_semantic=semantic,
            registration_attribution=attribution,
            initial_core_state=initial,
            registered_event=event,
            notification_reservation=(
                self.runtime_session.terminal_notification_account_coordinator.prepare_monitor_reservation(
                    monitor_id=monitor_id,
                    stream_identity=semantic.initial_baseline_cursor.stream_identity,
                    registration_event_id=event.id,
                )
            ),
            initial_observation_result=initial_observation_result,
        )
        closed = False
        with self._lock:
            closed = self._closed
            if not closed:
                existing = self._dormant.get(monitor_id)
                if existing is not None:
                    if existing.prepared.registered_event != event:
                        raise TerminalMonitorContractError(
                            "terminal monitor retry candidate drifted"
                        )
                    return existing.prepared
                self._dormant[monitor_id] = _DormantRegistrationOwner(
                    prepared=prepared,
                    process=process,
                )
        if closed:
            self.runtime_session.terminal_notification_account_coordinator.discard_prepared_reservation(
                prepared.notification_reservation
            )
            raise TerminalMonitorContractError("terminal monitor coordinator is closed")
        return prepared

    def on_committed(self, events: tuple[AgentEvent, ...]) -> None:
        """Adopt FULL outcomes after the reducer has folded the same batch."""

        recovered_deliveries: list[
            tuple[str, TerminalProcessObservationDeliveryDispositionEvent]
        ] = []
        for event in events:
            if isinstance(event, TerminalProcessMonitorRegisteredEvent):
                self._activate_registration(event)
            elif isinstance(event, TerminalProcessMonitorObservationCommittedEvent):
                with self._lock:
                    self._firing.pop(event.observation.monitor_id, None)
            elif isinstance(event, TerminalNotificationReservationReleasedEvent):
                reservation = event.transition.reservation
                if reservation.reservation_kind != "monitor_lifecycle":
                    continue
                monitor_id = reservation.monitor_id
                if monitor_id is None:
                    continue
                with self._lock:
                    worker = self._workers.pop(monitor_id, None)
                    self._processes.pop(monitor_id, None)
                    self._dormant.pop(monitor_id, None)
                if worker is not None:
                    worker[1].set()
            elif isinstance(event, TerminalProcessObservationDeliveryDispositionEvent):
                with self._lock:
                    monitor_ids = {
                        self._restart_pending_delivery.pop(reference.event_id)
                        for reference in event.observation_source_references
                        if reference.event_id in self._restart_pending_delivery
                    }
                recovered_deliveries.extend(
                    (monitor_id, event) for monitor_id in sorted(monitor_ids)
                )
        for monitor_id, disposition in recovered_deliveries:
            self._start_post_restart_delivery_terminalization(
                monitor_id=monitor_id,
                disposition=disposition,
            )

    def _start_post_restart_delivery_terminalization(
        self,
        *,
        monitor_id: str,
        disposition: TerminalProcessObservationDeliveryDispositionEvent,
    ) -> None:
        def run() -> None:
            try:
                try:
                    record = self.store.snapshot(monitor_id)
                except KeyError:
                    return
                with self._lock:
                    if self._closed or monitor_id in self._processes:
                        return
                candidates = self._restart_recovery_candidates(
                    record,
                    interruption_cause=disposition,
                )
                owner = _FiringOwner(
                    monitor_id=monitor_id,
                    stable_candidates=candidates,
                    source_state_fingerprint=record.core_state.core_state_fingerprint,
                )
                with self._lock:
                    if self._closed or monitor_id in self._firing:
                        return
                    self._firing[monitor_id] = owner
                self._commit_firing(owner)
            finally:
                with self._lock:
                    self._restart_recovery_workers.pop(monitor_id, None)

        with self._lock:
            if self._closed or monitor_id in self._restart_recovery_workers:
                return
            worker = Thread(
                target=run,
                daemon=True,
                name=f"terminal-monitor-restart-finalize-{monitor_id[-8:]}",
            )
            self._restart_recovery_workers[monitor_id] = worker
            worker.start()

    def discard_prepared_registration(
        self,
        prepared: PreparedTerminalProcessMonitorRegistration,
    ) -> None:
        """Drop a dormant registration when tool-result construction never completed."""

        monitor_id = prepared.registration_semantic.monitor_id
        discarded = False
        with self._lock:
            owner = self._dormant.get(monitor_id)
            if owner is None:
                return
            if owner.prepared != prepared:
                raise TerminalMonitorContractError(
                    "terminal monitor dormant registration identity drifted"
                )
            if owner.owner_state != "dormant":
                raise TerminalMonitorContractError(
                    "cannot discard an activated terminal monitor registration"
                )
            self._dormant.pop(monitor_id, None)
            discarded = True
        if discarded:
            self.runtime_session.terminal_notification_account_coordinator.discard_prepared_reservation(
                prepared.notification_reservation
            )

    async def ensure_terminal_receipt_observation(
        self,
        receipt: TerminalProcessObservationReceiptFact | None,
    ) -> None:
        """Close the completion/ToolResult race before freezing its terminal batch."""

        if receipt is None or not isinstance(
            receipt.observation_semantic.observed_state,
            TerminalProcessLifecycleOutcomeFact,
        ):
            return
        stream = receipt.observation_semantic.observed_end_cursor.stream_identity
        completion = self.runtime_session.terminal_notification_store.completion_event_for_stream(
            stream
        )
        if completion is None:
            raise TerminalMonitorContractError(
                "terminal receipt lacks durable process completion authority"
            )
        completion_reference = event_reference_from_stored(
            completion,
            runtime_session_id=self.runtime_session.runtime_session_id,
        )
        if (
            receipt.completion_event_reference != completion_reference
            or receipt.observation_semantic.observed_state
            != completion.completion_semantic.outcome
        ):
            raise TerminalMonitorContractError(
                "terminal receipt completion authority drifted"
            )

        while True:
            matching = tuple(
                record
                for record in self.store.snapshots()
                if record.registration_event.registration_semantic.initial_baseline_cursor.stream_identity
                == stream
                and record.core_state.lifecycle_state
                not in {
                    "terminal_pending_delivery",
                    "terminated",
                    "reconciliation_required",
                }
            )
            if not matching:
                return
            record = matching[0]
            monitor_id = record.registration_event.registration_semantic.monitor_id
            with self._lock:
                firing = self._firing.get(monitor_id)
                process = self._processes.get(monitor_id)
            if firing is not None:
                await asyncio.sleep(_MONITOR_RETRY_SECONDS)
                if self.runtime_session.reconciliation_required:
                    raise TerminalMonitorContractError(
                        "terminal receipt monitor observation requires reconciliation"
                    )
                continue
            if process is None:
                raise TerminalMonitorContractError(
                    "terminal receipt monitor lost its live process owner"
                )
            candidates = self._completion_candidates(
                record=record,
                process=process,
                completion=completion,
                observed_at=max(
                    record.registration_event.created_at, completion.created_at
                ),
            )
            owner = _FiringOwner(
                monitor_id=monitor_id,
                stable_candidates=candidates,
                source_state_fingerprint=record.core_state.core_state_fingerprint,
            )
            with self._lock:
                if monitor_id in self._firing:
                    continue
                self._firing[monitor_id] = owner
            await asyncio.to_thread(self._commit_firing, owner)
            if self.runtime_session.reconciliation_required:
                raise TerminalMonitorContractError(
                    "terminal receipt monitor observation requires reconciliation"
                )

    def prepare_receipt_applications(
        self,
        tool_result_end: ToolResultEndEvent,
    ) -> tuple[TerminalProcessMonitorReceiptAppliedEvent, ...]:
        """Freeze every monitor baseline transition owned by one ToolResult receipt."""

        receipt = tool_result_end.terminal_process_observation_receipt
        if receipt is None:
            return ()
        stream = receipt.observation_semantic.observed_end_cursor.stream_identity
        candidates: list[TerminalProcessMonitorReceiptAppliedEvent] = []
        for record in self.store.snapshots():
            registration = record.registration_event
            if (
                registration.registration_semantic.initial_baseline_cursor.stream_identity
                != stream
                or record.core_state.lifecycle_state
                in {"terminated", "reconciliation_required"}
            ):
                continue
            pending = record.pending_observation_event
            if pending is not None and not terminal_receipt_dominates_observation(
                receipt=receipt,
                pending=pending.observation,
            ):
                continue
            if pending is None and isinstance(
                receipt.observation_semantic.observed_state,
                TerminalProcessLifecycleOutcomeFact,
            ):
                raise TerminalMonitorContractError(
                    "terminal receipt reached ToolResult without a terminal monitor observation"
                )
            before = record.core_state
            end = receipt.observation_semantic.observed_end_cursor
            if pending is None and _cursor_equal_or_after(
                before.last_consumed_cursor, end
            ):
                continue
            after = resulting_receipt_core_state(
                before=before,
                receipt=receipt,
                pending=None if pending is None else pending.observation,
            )
            transition = build_frozen_fact(
                TerminalProcessMonitorStateTransitionFact,
                schema_version="terminal_process_monitor_state_transition.v1",
                source_revision=before.state_revision,
                result_revision=after.state_revision,
                before_core_state_fingerprint=before.core_state_fingerprint,
                after_core_state_fingerprint=after.core_state_fingerprint,
                observation_ordinal=None,
            )
            pending_reference = (
                None
                if pending is None
                else event_reference_from_stored(
                    pending,
                    runtime_session_id=self.runtime_session.runtime_session_id,
                )
            )
            candidates.append(
                TerminalProcessMonitorReceiptAppliedEvent(
                    id=context_fingerprint(
                        "terminal-monitor-receipt-applied-id:v1",
                        (
                            registration.registration_semantic.monitor_id,
                            tool_result_end.id,
                        ),
                    ).replace("sha256:", "terminal_monitor_receipt_applied:"),
                    run_id=tool_result_end.run_id,
                    turn_id=tool_result_end.turn_id,
                    reply_id=tool_result_end.reply_id,
                    registration_event_reference=record.registration_reference,
                    tool_result_end_event_identity=stable_event_identity(
                        tool_result_end,
                        runtime_session_id=self.runtime_session.runtime_session_id,
                    ),
                    receipt_fingerprint=receipt.receipt_fingerprint,
                    observed_end_cursor=end,
                    pending_observation_event_reference=pending_reference,
                    monitor_state_transition=transition,
                )
            )
        return tuple(candidates)

    def prepare_cancellation(
        self,
        *,
        monitor_id: str,
        origin_tool_call_id: str,
        runtime_context: ToolRuntimeContext,
    ) -> PreparedTerminalProcessMonitorCancellation:
        if runtime_context.run_entry_kind != "host_main_run":
            raise TerminalMonitorContractError(
                "terminal_monitor_child_cancellation_unsupported"
            )
        termination_event_id = (
            f"terminal_monitor_terminated:{monitor_id}:explicit_cancel"
        )
        tool_result_end_event_id = (
            f"tool_result_end:{runtime_context.event_context.run_id}:"
            f"{origin_tool_call_id}"
        )
        intent = build_frozen_fact(
            TerminalProcessMonitorCancelIntentFact,
            schema_version="terminal_process_monitor_cancel_intent.v1",
            monitor_id=monitor_id,
            origin_cancel_tool_call_id=origin_tool_call_id,
            monitor_termination_event_id=termination_event_id,
            tool_result_end_event_id=tool_result_end_event_id,
        )
        # A frozen observation owns its candidate until confirmation.  Install
        # the intent on that owner, then derive cancellation from the resulting
        # committed monitor state; never replace the in-flight candidate.
        while True:
            with self._lock:
                firing = self._firing.get(monitor_id)
                if firing is None:
                    break
                if firing.cancel_intent not in (None, intent):
                    raise TerminalMonitorContractError(
                        "terminal monitor already has another cancel intent"
                    )
                firing.cancel_intent = intent
            sleep(_MONITOR_RETRY_SECONDS)
            if self.runtime_session.reconciliation_required:
                raise TerminalMonitorContractError(
                    "terminal monitor cancellation requires reconciliation"
                )
        record = self.store.snapshot(monitor_id)
        if record.core_state.lifecycle_state in {
            "terminal_pending_delivery",
            "terminated",
            "reconciliation_required",
        }:
            return PreparedTerminalProcessMonitorCancellation(
                monitor_id=monitor_id,
                outcome="already_terminal",
                cancellation_semantic=None,
                stable_candidates=(),
            )
        before = record.core_state
        pending = record.pending_observation_event
        termination_before = (
            resulting_disposition_core_state(
                before=before,
                observation=pending.observation,
                delivery_policy=(
                    record.registration_event.registration_semantic.policy.delivery
                ),
            )
            if pending is not None
            else before
        )
        cancellation = build_frozen_fact(
            TerminalProcessMonitorCancellationSemanticFact,
            schema_version="terminal_process_monitor_cancellation_semantic.v1",
            cancel_intent=intent,
            expected_monitor_state_revision=before.state_revision,
            expected_monitor_core_state_fingerprint=before.core_state_fingerprint,
        )
        after = _terminated_core_state(
            termination_before,
            reason="explicit_cancel",
        )
        transition = build_frozen_fact(
            TerminalProcessMonitorStateTransitionFact,
            schema_version="terminal_process_monitor_state_transition.v1",
            source_revision=termination_before.state_revision,
            result_revision=after.state_revision,
            before_core_state_fingerprint=(termination_before.core_state_fingerprint),
            after_core_state_fingerprint=after.core_state_fingerprint,
            observation_ordinal=None,
        )
        registration_ref = record.registration_reference
        termination = TerminalProcessMonitorTerminatedEvent(
            id=termination_event_id,
            **runtime_context.event_context.event_fields(),
            registration_event_reference=registration_ref,
            termination_semantic=build_frozen_fact(
                TerminalProcessMonitorTerminationSemanticFact,
                schema_version="terminal_process_monitor_termination_semantic.v1",
                monitor_id=monitor_id,
                terminal_reason="explicit_cancel",
                terminal_cursor=termination_before.last_observation_cursor,
                last_committed_observation_ordinal=(
                    termination_before.last_committed_observation_ordinal
                ),
            ),
            monitor_state_transition=transition,
            notification_reservation_id=f"terminal_monitor_slot:{monitor_id}",
            cause_event_references=(registration_ref,),
            terminated_at_utc=_canonical_now(None),
        )
        candidates: list[AgentEvent] = []
        if pending is not None:
            candidates.append(
                TerminalProcessObservationDeliveryDispositionEvent(
                    id=f"terminal_monitor_cancelled:{pending.id}:{termination.id}",
                    **runtime_context.event_context.event_fields(),
                    observation_source_references=(
                        event_reference_from_stored(
                            pending,
                            runtime_session_id=self.runtime_session.runtime_session_id,
                        ),
                    ),
                    outcome="monitor_cancelled",
                )
            )
        candidates.append(termination)
        candidates.append(
            self.runtime_session.terminal_notification_account_coordinator.freeze_monitor_release(
                monitor_id=monitor_id,
                cause_events=(termination,),
            )
        )
        return PreparedTerminalProcessMonitorCancellation(
            monitor_id=monitor_id,
            outcome="cancelled",
            cancellation_semantic=cancellation,
            stable_candidates=tuple(candidates),
        )

    def close(self, *, timeout_seconds: float = 2.0) -> None:
        self.stop_admission_and_drain_workers(timeout_seconds=timeout_seconds)

    def stop_admission_and_drain_workers(self, *, timeout_seconds: float = 2.0) -> None:
        """Stop monitor I/O before process finalization creates close facts."""

        with self._lock:
            self._closed = True
            workers = tuple(self._workers.values())
            recovery_workers = tuple(self._restart_recovery_workers.values())
        for _thread, stop in workers:
            stop.set()
        deadline = monotonic() + max(0.0, timeout_seconds)
        for thread, _stop in workers:
            thread.join(timeout=max(0.0, deadline - monotonic()))
        for thread in recovery_workers:
            thread.join(timeout=max(0.0, deadline - monotonic()))
        with self._lock:
            if any(thread.is_alive() for thread, _stop in workers) or any(
                thread.is_alive() for thread in recovery_workers
            ):
                raise TerminalMonitorContractError(
                    "terminal monitor workers did not stop before close deadline"
                )
            if self._firing:
                raise TerminalMonitorContractError(
                    "terminal monitor close found unresolved FIRING owners"
                )

    def terminate_all_for_session_close(self, *, timeout_seconds: float = 2.0) -> None:
        """Stop physical workers and durably close every nonterminal monitor."""

        self.stop_admission_and_drain_workers(timeout_seconds=timeout_seconds)
        for record in self.store.snapshots():
            state = record.core_state
            if state.lifecycle_state in {"terminated", "reconciliation_required"}:
                continue
            candidates = self._session_close_candidates(record)
            if not candidates:
                continue
            result = self.runtime_session.write_events_from_thread(candidates)
            if result.reconciliation_required:
                raise TerminalMonitorContractError(
                    "terminal monitor close requires reconciliation"
                )

    def _session_close_candidates(
        self, record: TerminalMonitorRecord
    ) -> tuple[AgentEvent, ...]:
        before = record.core_state
        registration = record.registration_event
        pending = record.pending_observation_event
        candidates: list[AgentEvent] = []
        if pending is not None:
            candidates.append(
                TerminalProcessObservationDeliveryDispositionEvent(
                    id=(
                        f"terminal_monitor_session_closed:{pending.id}:"
                        f"{registration.registration_semantic.monitor_id}"
                    ),
                    run_id=registration.run_id,
                    turn_id=registration.turn_id,
                    reply_id=registration.reply_id,
                    observation_source_references=(
                        event_reference_from_stored(
                            pending,
                            runtime_session_id=self.runtime_session.runtime_session_id,
                        ),
                    ),
                    outcome="session_closed",
                )
            )
        if before.lifecycle_state == "terminal_pending_delivery":
            return tuple(candidates)
        termination_before = (
            resulting_disposition_core_state(
                before=before,
                observation=pending.observation,
                delivery_policy=registration.registration_semantic.policy.delivery,
            )
            if pending is not None
            else before
        )
        after = _terminated_core_state(
            termination_before,
            reason="session_closed",
        )
        transition = build_frozen_fact(
            TerminalProcessMonitorStateTransitionFact,
            schema_version="terminal_process_monitor_state_transition.v1",
            source_revision=termination_before.state_revision,
            result_revision=after.state_revision,
            before_core_state_fingerprint=(termination_before.core_state_fingerprint),
            after_core_state_fingerprint=after.core_state_fingerprint,
            observation_ordinal=None,
        )
        termination = TerminalProcessMonitorTerminatedEvent(
            id=(
                "terminal_monitor_terminated:"
                f"{registration.registration_semantic.monitor_id}:session_closed"
            ),
            run_id=registration.run_id,
            turn_id=registration.turn_id,
            reply_id=registration.reply_id,
            registration_event_reference=record.registration_reference,
            termination_semantic=build_frozen_fact(
                TerminalProcessMonitorTerminationSemanticFact,
                schema_version="terminal_process_monitor_termination_semantic.v1",
                monitor_id=registration.registration_semantic.monitor_id,
                terminal_reason="session_closed",
                terminal_cursor=termination_before.last_observation_cursor,
                last_committed_observation_ordinal=(
                    termination_before.last_committed_observation_ordinal
                ),
            ),
            monitor_state_transition=transition,
            notification_reservation_id=(
                f"terminal_monitor_slot:{registration.registration_semantic.monitor_id}"
            ),
            cause_event_references=(record.registration_reference,),
            terminated_at_utc=_canonical_now(None),
        )
        candidates.append(termination)
        candidates.append(
            self.runtime_session.terminal_notification_account_coordinator.freeze_monitor_release(
                monitor_id=registration.registration_semantic.monitor_id,
                cause_events=(termination,),
            )
        )
        return tuple(candidates)

    def list_current_snapshots(
        self,
        *,
        maximum_items: int = 8,
    ) -> tuple[tuple[TerminalMonitorRecord, ...], int]:
        return self.store.current_snapshots(maximum_items=maximum_items)

    def _activate_registration(
        self, event: TerminalProcessMonitorRegisteredEvent
    ) -> None:
        monitor_id = event.registration_semantic.monitor_id
        with self._lock:
            owner = self._dormant.get(monitor_id)
            if owner is None:
                # V1 never adopts an OS process merely because the PID remains
                # reachable after RuntimeSession reconstruction.  The explicit
                # startup recovery owner below closes or terminalizes this fact.
                return
            owner.owner_state = "active"
            self._processes[monitor_id] = owner.process
            if self.event_channel is not None:
                self.event_channel.bind_journal(
                    monitor_id=monitor_id,
                    baseline_cursor=(
                        event.registration_semantic.initial_baseline_cursor
                    ),
                )
            if monitor_id in self._workers:
                return
            stop = Event()
            worker = Thread(
                target=self._monitor_loop,
                args=(monitor_id, stop),
                daemon=True,
                name=f"terminal-monitor-{monitor_id[-8:]}",
            )
            self._workers[monitor_id] = (worker, stop)
            worker.start()

    def recover_after_restart(self, *, deadline_monotonic: float) -> None:
        """Terminalize durable registrations that have no live physical owner.

        A RuntimeSession reconstruction deliberately starts with an empty
        process-owner map.  Confirmed completion authority can still produce an
        exact or typed-unavailable terminal observation; otherwise the monitor
        is deterministically interrupted.  Every candidate is stable across a
        repeated restart, so NONE can be retried by the next recovery owner.
        """

        for record in self.store.snapshots():
            if monotonic() >= deadline_monotonic:
                raise TerminalMonitorRecoveryBlocked(
                    "terminal monitor restart recovery deadline expired"
                )
            state = record.core_state
            if state.lifecycle_state in {
                "terminal_pending_delivery",
                "terminated",
                "reconciliation_required",
            }:
                continue
            if state.lifecycle_state == "active_pending_delivery":
                pending = record.pending_observation_event
                if pending is None:
                    raise TerminalMonitorContractError(
                        "restart pending-delivery monitor lacks its observation"
                    )
                with self._lock:
                    self._restart_pending_delivery[pending.id] = (
                        record.registration_event.registration_semantic.monitor_id
                    )
                continue
            monitor_id = record.registration_event.registration_semantic.monitor_id
            with self._lock:
                if monitor_id in self._processes or monitor_id in self._firing:
                    continue
            candidates = self._restart_recovery_candidates(record)
            owner = _FiringOwner(
                monitor_id=monitor_id,
                stable_candidates=candidates,
                source_state_fingerprint=state.core_state_fingerprint,
            )
            with self._lock:
                if monitor_id in self._firing:
                    continue
                self._firing[monitor_id] = owner
            self._commit_firing(owner, deadline_monotonic=deadline_monotonic)

    def _restart_recovery_candidates(
        self,
        record: TerminalMonitorRecord,
        *,
        interruption_cause: AgentEvent | None = None,
    ) -> tuple[AgentEvent, ...]:
        registration = record.registration_event
        stream = (
            registration.registration_semantic.initial_baseline_cursor.stream_identity
        )
        completion = self.runtime_session.terminal_notification_store.completion_event_for_stream(
            stream
        )
        if completion is None:
            cause = interruption_cause or registration
            return self._termination_candidates(
                record=record,
                terminal_reason="interrupted_by_host_restart",
                cause=cause,
                terminal_cursor=record.core_state.last_observation_cursor,
                observed_at=max(registration.created_at, cause.created_at),
            )

        if (
            completion.sequence is None
            or registration.sequence is None
            or completion.sequence <= registration.sequence
        ):
            return self._termination_candidates(
                record=record,
                terminal_reason="authority_untrusted",
                cause=registration,
                terminal_cursor=record.core_state.last_observation_cursor,
                observed_at=registration.created_at,
            )
        outcome = completion.lifecycle_outcome
        observed_at = max(registration.created_at, completion.created_at)
        if outcome.status == "killed" and outcome.kill_reason in {
            "user_tool_kill",
            "teardown",
        }:
            return self._termination_candidates(
                record=record,
                terminal_reason=(
                    "explicit_process_kill"
                    if outcome.kill_reason == "user_tool_kill"
                    else "process_completion_not_delivery_eligible"
                ),
                cause=completion,
                terminal_cursor=completion.terminal_output_cursor,
                observed_at=observed_at,
            )
        try:
            output = recover_terminal_output_delta(
                recovery_reference=completion.output_recovery_reference,
                requested_start_cursor=record.core_state.last_consumed_cursor,
                terminal_cursor=completion.terminal_output_cursor,
                max_chars=(
                    registration.registration_semantic.policy.delivery.max_output_chars
                ),
            )
        except (OSError, ValueError, _SpoolAuthorityConflict):
            return self._termination_candidates(
                record=record,
                terminal_reason="authority_untrusted",
                cause=completion,
                terminal_cursor=completion.terminal_output_cursor,
                observed_at=observed_at,
            )
        completion_semantic = build_frozen_fact(
            TerminalProcessCompletionSemanticFact,
            schema_version="terminal_process_completion_semantic.v1",
            terminal_output_cursor=completion.terminal_output_cursor,
            outcome=outcome,
        )
        observation = build_frozen_fact(
            TerminalProcessMonitorCompletionObservationSemanticFact,
            schema_version=(
                "terminal_process_monitor_completion_observation_semantic.v1"
            ),
            monitor_id=registration.registration_semantic.monitor_id,
            observation_kind="process_completed",
            observation_ordinal=record.core_state.last_committed_observation_ordinal
            + 1,
            completion_semantic=completion_semantic,
            output_authority=output,
        )
        attribution = (
            build_frozen_fact(
                TerminalOutputDeltaAttributionFact,
                schema_version="terminal_output_delta_attribution.v1",
                delta_semantic_fingerprint=output.delta_semantic_fingerprint,
                full_output_artifact_ref=None,
                retained_segment_first_index=None,
                retained_segment_last_index=None,
            )
            if isinstance(output, TerminalOutputDeltaSemanticFact)
            else None
        )
        return self._event_candidates(
            record=record,
            observation=observation,
            attribution=attribution,
            completion_reference=event_reference_from_stored(
                completion,
                runtime_session_id=self.runtime_session.runtime_session_id,
            ),
            observed_at=observed_at,
        )

    def _monitor_loop(self, monitor_id: str, stop: Event) -> None:
        while not stop.is_set():
            try:
                record = self.store.snapshot(monitor_id)
            except KeyError:
                return
            state = record.core_state
            if state.lifecycle_state in {
                "terminal_pending_delivery",
                "terminated",
                "reconciliation_required",
            }:
                return
            process = self._processes.get(monitor_id)
            if process is None:
                return
            if self.event_channel is not None:
                self.event_channel.publish_journal(process.output)
            try:
                self._try_freeze_and_commit(record, process)
            except Exception:
                self._mark_reconciliation_required(monitor_id)
                return
            revision = process.output.revision
            process.output.wait_for_revision(revision, _MONITOR_POLL_SECONDS)

    def _try_freeze_and_commit(
        self,
        record: TerminalMonitorRecord,
        process: TerminalProcessState,
    ) -> None:
        monitor_id = record.registration_event.registration_semantic.monitor_id
        with self._lock:
            if monitor_id in self._firing:
                return
        candidates = self._observation_candidates(record, process)
        if not candidates:
            return
        owner = _FiringOwner(
            monitor_id=monitor_id,
            stable_candidates=candidates,
            source_state_fingerprint=record.core_state.core_state_fingerprint,
        )
        with self._lock:
            if monitor_id in self._firing:
                return
            self._firing[monitor_id] = owner
        self._commit_firing(owner)

    def _commit_firing(
        self,
        owner: _FiringOwner,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        while True:
            if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
                raise TerminalMonitorRecoveryBlocked(
                    "terminal monitor recovery retained an unresolved write owner"
                )
            owner.attempts += 1
            try:
                if deadline_monotonic is None:
                    result = self.runtime_session.write_events_from_thread(
                        owner.stable_candidates
                    )
                else:
                    result = self.runtime_session.write_events_from_thread(
                        owner.stable_candidates,
                        deadline_monotonic=deadline_monotonic,
                    )
            except RuntimeEventWriteCancelled as cancelled:
                result = getattr(cancelled, "operation_result", None)
                if result is None:
                    if self._closed:
                        return
                    if (
                        deadline_monotonic is not None
                        and monotonic() + _MONITOR_RETRY_SECONDS >= deadline_monotonic
                    ):
                        raise TerminalMonitorRecoveryBlocked(
                            "terminal monitor recovery write remained unconfirmed"
                        )
                    sleep(_MONITOR_RETRY_SECONDS)
                    continue
            except Exception as exc:
                outcome = getattr(exc, "commit_outcome", None)
                if outcome == "none" or isinstance(exc, PendingRuntimeEventWriteError):
                    if self._closed:
                        return
                    if (
                        deadline_monotonic is not None
                        and monotonic() + _MONITOR_RETRY_SECONDS >= deadline_monotonic
                    ):
                        raise TerminalMonitorRecoveryBlocked(
                            "terminal monitor recovery write returned NONE"
                        ) from exc
                    sleep(_MONITOR_RETRY_SECONDS)
                    continue
                self._mark_reconciliation_required(owner.monitor_id)
                return
            if result.reconciliation_required:
                self._mark_reconciliation_required(owner.monitor_id)
                return
            # FULL is adopted through the committed reducer callback before the
            # physical call returns.  The explicit pop also supports test ports
            # that return a FULL result without a callback.
            with self._lock:
                self._firing.pop(owner.monitor_id, None)
            return

    def _mark_reconciliation_required(self, monitor_id: str) -> None:
        with self._lock:
            dormant = self._dormant.get(monitor_id)
            if dormant is not None:
                dormant.owner_state = "reconciliation_required"
        self.runtime_session.latch_event_commit_outcome_unknown()

    def _observation_candidates(
        self,
        record: TerminalMonitorRecord,
        process: TerminalProcessState,
    ) -> tuple[AgentEvent, ...]:
        before = record.core_state
        if before.lifecycle_state == "terminal_pending_delivery":
            return ()
        registration = record.registration_event
        policy = registration.registration_semantic.policy
        completion = process.completion_recorded_event
        observed_at = _canonical_now(None)
        expired = monitor_lifetime_expired(
            expires_at_utc=registration.registration_attribution.expires_at_utc,
            observed_at_utc=observed_at,
        )
        terminal = completion is not None
        if before.lifecycle_state == "active_pending_delivery" and not (
            terminal or expired
        ):
            return ()
        if before.lifecycle_state == "active_completion_only" and not (
            terminal or expired
        ):
            return ()
        if terminal:
            observed_at = max(
                registration.created_at,
                completion.created_at,
            )
            return self._completion_candidates(
                record=record,
                process=process,
                completion=completion,
                observed_at=observed_at,
            )
        if expired:
            return self._expiry_candidates(
                record=record,
                process=process,
                observed_at=observed_at,
            )
        output = policy.conditions.output
        end = process.output.end_cursor
        output_ready = (
            output is not None
            and end.sanitized_char_offset
            - before.last_observation_cursor.sanitized_char_offset
            >= output.min_new_output_chars
            and monotonic() - process.output.last_output_monotonic
            >= output.quiet_period_ms / 1000
        )
        heartbeat_ready = _heartbeat_ready(
            registration=registration,
            core=before,
            observed_at_utc=observed_at,
        )
        if not (output_ready or heartbeat_ready):
            return ()
        eligible_state, _next = progress_limiter_decision(
            previous=before.progress_limiter_state,
            policy=policy.delivery,
            observed_at_utc=observed_at,
        )
        if eligible_state is None:
            return ()
        return self._progress_candidates(
            record=record,
            process=process,
            observed_at=observed_at,
            kind="output_progress" if output_ready else "heartbeat",
        )

    def _progress_candidates(
        self,
        *,
        record: TerminalMonitorRecord,
        process: TerminalProcessState,
        observed_at: str,
        kind: Literal["output_progress", "heartbeat"],
    ) -> tuple[AgentEvent, ...]:
        before = record.core_state
        registration = record.registration_event
        delta, attribution = process.output.snapshot_since(
            before.last_consumed_cursor,
            max_chars=registration.registration_semantic.policy.delivery.max_output_chars,
        )
        observation = build_frozen_fact(
            TerminalProcessMonitorProgressObservationSemanticFact,
            schema_version=(
                "terminal_process_monitor_progress_observation_semantic.v1"
            ),
            monitor_id=registration.registration_semantic.monitor_id,
            observation_kind=kind,
            observation_ordinal=before.last_committed_observation_ordinal + 1,
            process_state=build_running_terminal_process_state(),
            output_authority=delta,
        )
        return self._event_candidates(
            record=record,
            observation=observation,
            attribution=attribution,
            completion_reference=None,
            observed_at=observed_at,
        )

    def _completion_candidates(
        self,
        *,
        record: TerminalMonitorRecord,
        process: TerminalProcessState,
        completion: TerminalProcessCompletedEvent,
        observed_at: str,
    ) -> tuple[AgentEvent, ...]:
        if (
            completion.lifecycle_outcome is None
            or completion.terminal_output_cursor is None
        ):
            raise TerminalMonitorContractError(
                "terminal monitor completion lacks typed authority"
            )
        before = record.core_state
        # user kill and teardown are lifecycle facts, but not autonomous model
        # delivery authority.
        if completion.lifecycle_outcome.status == "killed" and (
            completion.lifecycle_outcome.kill_reason in {"user_tool_kill", "teardown"}
        ):
            return self._termination_candidates(
                record=record,
                terminal_reason=(
                    "explicit_process_kill"
                    if completion.lifecycle_outcome.kill_reason == "user_tool_kill"
                    else "process_completion_not_delivery_eligible"
                ),
                cause=completion,
                terminal_cursor=completion.terminal_output_cursor,
                observed_at=observed_at,
            )
        delta, attribution = process.output.snapshot_since(
            before.last_consumed_cursor,
            max_chars=record.registration_event.registration_semantic.policy.delivery.max_output_chars,
        )
        completion_semantic = build_frozen_fact(
            TerminalProcessCompletionSemanticFact,
            schema_version="terminal_process_completion_semantic.v1",
            terminal_output_cursor=completion.terminal_output_cursor,
            outcome=completion.lifecycle_outcome,
        )
        observation = build_frozen_fact(
            TerminalProcessMonitorCompletionObservationSemanticFact,
            schema_version=(
                "terminal_process_monitor_completion_observation_semantic.v1"
            ),
            monitor_id=record.registration_event.registration_semantic.monitor_id,
            observation_kind="process_completed",
            observation_ordinal=before.last_committed_observation_ordinal + 1,
            completion_semantic=completion_semantic,
            output_authority=delta,
        )
        completion_reference = event_reference_from_stored(
            completion,
            runtime_session_id=self.runtime_session.runtime_session_id,
        )
        return self._event_candidates(
            record=record,
            observation=observation,
            attribution=attribution,
            completion_reference=completion_reference,
            observed_at=observed_at,
        )

    def _expiry_candidates(
        self,
        *,
        record: TerminalMonitorRecord,
        process: TerminalProcessState,
        observed_at: str,
    ) -> tuple[AgentEvent, ...]:
        before = record.core_state
        delta, attribution = process.output.snapshot_since(
            before.last_consumed_cursor,
            max_chars=record.registration_event.registration_semantic.policy.delivery.max_output_chars,
        )
        observation = build_frozen_fact(
            TerminalProcessMonitorExpiryObservationSemanticFact,
            schema_version=("terminal_process_monitor_expiry_observation_semantic.v1"),
            monitor_id=record.registration_event.registration_semantic.monitor_id,
            observation_kind="monitor_expired",
            observation_ordinal=before.last_committed_observation_ordinal + 1,
            process_state=build_running_terminal_process_state(),
            output_authority=delta,
        )
        return self._event_candidates(
            record=record,
            observation=observation,
            attribution=attribution,
            completion_reference=None,
            observed_at=observed_at,
        )

    def _event_candidates(
        self,
        *,
        record: TerminalMonitorRecord,
        observation: TerminalProcessMonitorObservationSemanticFact,
        attribution: TerminalOutputDeltaAttributionFact | None,
        completion_reference,
        observed_at: str,
    ) -> tuple[AgentEvent, ...]:
        before = record.core_state
        delivery = record.registration_event.registration_semantic.policy.delivery
        after = resulting_observation_core_state(
            before=before,
            observation=observation,
            observed_at_utc=observed_at,
            delivery_policy=delivery,
        )
        transition = build_frozen_fact(
            TerminalProcessMonitorStateTransitionFact,
            schema_version="terminal_process_monitor_state_transition.v1",
            source_revision=before.state_revision,
            result_revision=after.state_revision,
            before_core_state_fingerprint=before.core_state_fingerprint,
            after_core_state_fingerprint=after.core_state_fingerprint,
            observation_ordinal=observation.observation_ordinal,
        )
        registration = record.registration_event
        event = TerminalProcessMonitorObservationCommittedEvent(
            id=(
                f"terminal_monitor_observation:{observation.monitor_id}:"
                f"{observation.observation_ordinal}"
            ),
            created_at=observed_at,
            run_id=registration.run_id,
            turn_id=registration.turn_id,
            reply_id=registration.reply_id,
            registration_event_reference=record.registration_reference,
            observation=observation,
            monitor_state_transition=transition,
            output_delta_attribution=(
                attribution
                if isinstance(
                    observation.output_authority, TerminalOutputDeltaSemanticFact
                )
                else None
            ),
            completion_event_reference=completion_reference,
            owner_host_session_id=(
                registration.registration_attribution.owner_host_session_id
            ),
            wake_chain_id=(
                registration.registration_attribution.wake_chain.wake_chain_id
            ),
            observed_at_utc=observed_at,
            physical_reservation_id=f"terminal_monitor:{observation.monitor_id}",
            physical_reservation_fingerprint=context_fingerprint(
                "terminal-monitor-observation-physical-reservation:v1",
                {
                    "monitor_id": observation.monitor_id,
                    "policy_fingerprint": record.registration_event.registration_semantic.policy.policy_fingerprint,
                },
            ),
        )
        candidates: list[AgentEvent] = [event]
        pending = record.pending_observation_event
        if pending is not None and observation.observation_kind in {
            "process_completed",
            "monitor_expired",
        }:
            pending_ref = event_reference_from_stored(
                pending,
                runtime_session_id=self.runtime_session.runtime_session_id,
            )
            candidates.append(
                TerminalProcessObservationDeliveryDispositionEvent(
                    id=f"terminal_monitor_superseded:{pending.id}:{event.id}",
                    run_id=registration.run_id,
                    turn_id=registration.turn_id,
                    reply_id=registration.reply_id,
                    observation_source_references=(pending_ref,),
                    outcome="superseded_by_terminal_observation",
                )
            )
        if observation.observation_kind in {"process_completed", "monitor_expired"}:
            candidates.append(
                self.runtime_session.terminal_notification_account_coordinator.freeze_monitor_release(
                    monitor_id=observation.monitor_id,
                    cause_events=(event,),
                )
            )
        return tuple(candidates)

    def _termination_candidates(
        self,
        *,
        record: TerminalMonitorRecord,
        terminal_reason,
        cause: AgentEvent,
        terminal_cursor,
        observed_at: str,
    ) -> tuple[AgentEvent, ...]:
        before = record.core_state
        after = build_frozen_fact(
            TerminalProcessMonitorCoreStateFact,
            schema_version="terminal_process_monitor_core_state.v1",
            monitor_id=record.registration_event.registration_semantic.monitor_id,
            state_revision=before.state_revision + 1,
            lifecycle_state="terminated",
            last_observation_cursor=before.last_observation_cursor,
            last_consumed_cursor=before.last_consumed_cursor,
            last_committed_observation_ordinal=(
                before.last_committed_observation_ordinal
            ),
            committed_progress_observation_count=(
                before.committed_progress_observation_count
            ),
            progress_limiter_state=before.progress_limiter_state,
            pending_observation_semantic_fingerprint=None,
            terminal_reason=terminal_reason,
        )
        transition = build_frozen_fact(
            TerminalProcessMonitorStateTransitionFact,
            schema_version="terminal_process_monitor_state_transition.v1",
            source_revision=before.state_revision,
            result_revision=after.state_revision,
            before_core_state_fingerprint=before.core_state_fingerprint,
            after_core_state_fingerprint=after.core_state_fingerprint,
            observation_ordinal=None,
        )
        cause_ref = event_reference_from_stored(
            cause,
            runtime_session_id=self.runtime_session.runtime_session_id,
        )
        semantic = build_frozen_fact(
            TerminalProcessMonitorTerminationSemanticFact,
            schema_version="terminal_process_monitor_termination_semantic.v1",
            monitor_id=before.monitor_id,
            terminal_reason=terminal_reason,
            terminal_cursor=terminal_cursor,
            last_committed_observation_ordinal=(
                before.last_committed_observation_ordinal
            ),
        )
        termination = TerminalProcessMonitorTerminatedEvent(
            id=f"terminal_monitor_terminated:{before.monitor_id}:{terminal_reason}",
            created_at=observed_at,
            run_id=record.registration_event.run_id,
            turn_id=record.registration_event.turn_id,
            reply_id=record.registration_event.reply_id,
            registration_event_reference=record.registration_reference,
            termination_semantic=semantic,
            monitor_state_transition=transition,
            notification_reservation_id=(f"terminal_monitor_slot:{before.monitor_id}"),
            cause_event_references=(cause_ref,),
            terminated_at_utc=observed_at,
        )
        release = self.runtime_session.terminal_notification_account_coordinator.freeze_monitor_release(
            monitor_id=before.monitor_id,
            cause_events=(termination,),
        )
        return termination, release

    def _run_start(self, run_id: str) -> RunStartEvent:
        run_start = self.runtime_session.long_horizon_state_store.run_start(run_id)
        if run_start is None or run_start.sequence is None:
            raise TerminalMonitorContractError(
                "terminal monitor requires one committed Host RunStart"
            )
        return run_start

    def _registration_chain(
        self,
        *,
        run_start: RunStartEvent,
        run_reference,
    ) -> TerminalAutonomousDeliveryChainAttributionFact:
        ingress = run_start.host_run_ingress
        if not isinstance(ingress, RuntimeRequestRunIngressFact):
            return build_frozen_fact(
                TerminalAutonomousDeliveryChainAttributionFact,
                schema_version="terminal_autonomous_delivery_chain_attribution.v1",
                wake_chain_id=_stable_id("terminal_wake_chain", run_start.id),
                root_human_run_event_reference=run_reference,
                parent_monitor_id=None,
                parent_automatic_delivery_ordinal=None,
                resolved_policy=default_autonomy_chain_policy(),
            )
        delivery = ingress.autonomy_delivery
        snapshot = (
            self.runtime_session.terminal_notification_store.autonomy_chain_snapshot(
                delivery.wake_chain_id
            )
        )
        if (
            snapshot.state.last_automatic_delivery_ordinal
            != delivery.automatic_delivery_ordinal
            or snapshot.state.chain_policy_fingerprint
            != delivery.chain_policy_fingerprint
        ):
            raise TerminalMonitorContractError(
                "autonomous monitor registration chain state is stale"
            )
        source_monitor_ids: set[str] = set()
        for attachment in ingress.source_notifications:
            for reference in attachment.source_event_references:
                source = self.runtime_session.event_log.get_by_id(reference.event_id)
                if isinstance(source, TerminalProcessMonitorObservationCommittedEvent):
                    source_monitor_ids.add(source.observation.monitor_id)
        if not source_monitor_ids:
            raise TerminalMonitorContractError(
                "autonomous monitor registration lacks parent monitor authority"
            )
        return build_frozen_fact(
            TerminalAutonomousDeliveryChainAttributionFact,
            schema_version="terminal_autonomous_delivery_chain_attribution.v1",
            wake_chain_id=delivery.wake_chain_id,
            root_human_run_event_reference=(
                snapshot.attribution.root_human_run_event_reference
            ),
            parent_monitor_id=sorted(source_monitor_ids)[0],
            parent_automatic_delivery_ordinal=delivery.automatic_delivery_ordinal,
            resolved_policy=snapshot.attribution.resolved_policy,
        )


def initial_monitor_core_state(
    semantic: TerminalProcessMonitorRegistrationSemanticFact,
) -> TerminalProcessMonitorCoreStateFact:
    limiter = build_frozen_fact(
        TerminalProcessMonitorProgressLimiterStateFact,
        schema_version="terminal_process_monitor_progress_limiter_state.v1",
        retained_progress_observed_at_utc=(),
        last_committed_progress_observed_at_utc=None,
        delivery_policy_fingerprint=semantic.policy.delivery.delivery_policy_fingerprint,
    )
    return build_frozen_fact(
        TerminalProcessMonitorCoreStateFact,
        schema_version="terminal_process_monitor_core_state.v1",
        monitor_id=semantic.monitor_id,
        state_revision=0,
        lifecycle_state="active_ready",
        last_observation_cursor=semantic.initial_baseline_cursor,
        last_consumed_cursor=semantic.initial_baseline_cursor,
        last_committed_observation_ordinal=0,
        committed_progress_observation_count=0,
        progress_limiter_state=limiter,
        pending_observation_semantic_fingerprint=None,
        terminal_reason=None,
    )


def _terminated_core_state(
    before: TerminalProcessMonitorCoreStateFact,
    *,
    reason: Literal[
        "explicit_cancel",
        "session_closed",
        "interrupted_by_host_restart",
        "explicit_process_kill",
        "process_completion_not_delivery_eligible",
        "authority_untrusted",
    ],
) -> TerminalProcessMonitorCoreStateFact:
    return build_frozen_fact(
        TerminalProcessMonitorCoreStateFact,
        schema_version="terminal_process_monitor_core_state.v1",
        monitor_id=before.monitor_id,
        state_revision=before.state_revision + 1,
        lifecycle_state="terminated",
        last_observation_cursor=before.last_observation_cursor,
        last_consumed_cursor=before.last_consumed_cursor,
        last_committed_observation_ordinal=before.last_committed_observation_ordinal,
        committed_progress_observation_count=(
            before.committed_progress_observation_count
        ),
        progress_limiter_state=before.progress_limiter_state,
        pending_observation_semantic_fingerprint=None,
        terminal_reason=reason,
    )


def resulting_observation_core_state(
    *,
    before: TerminalProcessMonitorCoreStateFact,
    observation: TerminalProcessMonitorObservationSemanticFact,
    observed_at_utc: str,
    delivery_policy: TerminalProcessMonitorDeliveryPolicyFact,
) -> TerminalProcessMonitorCoreStateFact:
    output = observation.output_authority
    if isinstance(output, TerminalOutputDeltaSemanticFact):
        end_cursor = output.end_cursor
    elif isinstance(output, UnavailableRecoveredTerminalOutputDeltaFact):
        end_cursor = output.terminal_cursor
    else:  # pragma: no cover - discriminated union is closed
        raise TypeError(type(output))
    progress = observation.observation_kind in {"heartbeat", "output_progress"}
    if progress:
        limiter = advance_progress_limiter(
            previous=before.progress_limiter_state,
            policy=delivery_policy,
            observed_at_utc=observed_at_utc,
        )
        if limiter is None:
            raise TerminalMonitorContractError(
                "ineligible progress candidate reached state construction"
            )
        progress_count = before.committed_progress_observation_count + 1
        lifecycle_state = "active_pending_delivery"
        terminal_reason = None
    else:
        limiter = before.progress_limiter_state
        progress_count = before.committed_progress_observation_count
        lifecycle_state = "terminal_pending_delivery"
        terminal_reason = (
            "process_completed"
            if observation.observation_kind == "process_completed"
            else "monitor_expired"
        )
    return build_frozen_fact(
        TerminalProcessMonitorCoreStateFact,
        schema_version="terminal_process_monitor_core_state.v1",
        monitor_id=before.monitor_id,
        state_revision=before.state_revision + 1,
        lifecycle_state=lifecycle_state,
        last_observation_cursor=end_cursor,
        last_consumed_cursor=before.last_consumed_cursor,
        last_committed_observation_ordinal=observation.observation_ordinal,
        committed_progress_observation_count=progress_count,
        progress_limiter_state=limiter,
        pending_observation_semantic_fingerprint=(
            observation.observation_semantic_fingerprint
        ),
        terminal_reason=terminal_reason,
    )


def resulting_disposition_core_state(
    *,
    before: TerminalProcessMonitorCoreStateFact,
    observation: TerminalProcessMonitorObservationSemanticFact,
    delivery_policy: TerminalProcessMonitorDeliveryPolicyFact,
    consumed_through_cursor: TerminalOutputCursorFact | None = None,
) -> TerminalProcessMonitorCoreStateFact:
    output = observation.output_authority
    consumed = (
        output.end_cursor
        if isinstance(output, TerminalOutputDeltaSemanticFact)
        else output.terminal_cursor
    )
    consumed = _later_cursor(before.last_consumed_cursor, consumed)
    if consumed_through_cursor is not None:
        if consumed_through_cursor.stream_identity != consumed.stream_identity:
            raise TerminalMonitorContractError(
                "terminal receipt consumed cursor crosses output streams"
            )
        if (
            consumed_through_cursor.sanitized_char_offset
            >= consumed.sanitized_char_offset
            and consumed_through_cursor.sanitized_utf8_byte_offset
            >= consumed.sanitized_utf8_byte_offset
        ):
            consumed = consumed_through_cursor
    observed = _later_cursor(before.last_observation_cursor, consumed)
    terminal = before.lifecycle_state == "terminal_pending_delivery"
    progress_cap = (
        before.committed_progress_observation_count
        >= delivery_policy.maximum_committed_progress_observations
    )
    return build_frozen_fact(
        TerminalProcessMonitorCoreStateFact,
        schema_version="terminal_process_monitor_core_state.v1",
        monitor_id=before.monitor_id,
        state_revision=before.state_revision + 1,
        lifecycle_state=(
            "terminated"
            if terminal
            else "active_completion_only"
            if progress_cap
            else "active_ready"
        ),
        last_observation_cursor=observed,
        last_consumed_cursor=consumed,
        last_committed_observation_ordinal=before.last_committed_observation_ordinal,
        committed_progress_observation_count=(
            before.committed_progress_observation_count
        ),
        progress_limiter_state=before.progress_limiter_state,
        pending_observation_semantic_fingerprint=None,
        terminal_reason=before.terminal_reason if terminal else None,
    )


def resulting_receipt_core_state(
    *,
    before: TerminalProcessMonitorCoreStateFact,
    receipt: TerminalProcessObservationReceiptFact,
    pending: TerminalProcessMonitorObservationSemanticFact | None,
) -> TerminalProcessMonitorCoreStateFact:
    """Apply one exact ToolResult receipt without advancing observation ordinal."""

    if before.lifecycle_state in {"terminated", "reconciliation_required"}:
        raise TerminalMonitorContractError(
            "terminal receipt cannot advance a terminal monitor"
        )
    if pending is not None and not terminal_receipt_dominates_observation(
        receipt=receipt,
        pending=pending,
    ):
        raise TerminalMonitorContractError(
            "terminal receipt does not dominate the pending observation"
        )
    end = receipt.observation_semantic.observed_end_cursor
    observed = _later_cursor(before.last_observation_cursor, end)
    consumed = _later_cursor(before.last_consumed_cursor, end)
    return build_frozen_fact(
        TerminalProcessMonitorCoreStateFact,
        schema_version="terminal_process_monitor_core_state.v1",
        monitor_id=before.monitor_id,
        state_revision=before.state_revision + 1,
        lifecycle_state=before.lifecycle_state,
        last_observation_cursor=observed,
        last_consumed_cursor=consumed,
        last_committed_observation_ordinal=before.last_committed_observation_ordinal,
        committed_progress_observation_count=(
            before.committed_progress_observation_count
        ),
        progress_limiter_state=before.progress_limiter_state,
        pending_observation_semantic_fingerprint=(
            before.pending_observation_semantic_fingerprint
        ),
        terminal_reason=before.terminal_reason,
    )


def _later_cursor(
    first: TerminalOutputCursorFact,
    second: TerminalOutputCursorFact,
) -> TerminalOutputCursorFact:
    if first.stream_identity != second.stream_identity:
        raise TerminalMonitorContractError("terminal cursor crosses output streams")
    first_offsets = (
        first.sanitized_char_offset,
        first.sanitized_utf8_byte_offset,
    )
    second_offsets = (
        second.sanitized_char_offset,
        second.sanitized_utf8_byte_offset,
    )
    if all(
        left <= right for left, right in zip(first_offsets, second_offsets, strict=True)
    ):
        if first_offsets == second_offsets and first != second:
            raise TerminalMonitorContractError(
                "terminal cursor prefix identity drifted at equal offsets"
            )
        return second
    if all(
        left >= right for left, right in zip(first_offsets, second_offsets, strict=True)
    ):
        return first
    raise TerminalMonitorContractError("terminal cursor offsets are not monotonic")


def _cursor_equal_or_after(
    cursor: TerminalOutputCursorFact,
    other: TerminalOutputCursorFact,
) -> bool:
    return _later_cursor(cursor, other) == cursor


def default_monitor_conditions(
    *,
    min_new_output_chars: int | None,
    quiet_period_ms: int,
    heartbeat_interval_seconds: int | None,
) -> TerminalProcessMonitorConditionsFact:
    output = (
        None
        if min_new_output_chars is None
        else build_frozen_fact(
            TerminalProcessMonitorOutputConditionFact,
            schema_version="terminal_process_monitor_output_condition.v1",
            min_new_output_chars=min_new_output_chars,
            quiet_period_ms=quiet_period_ms,
        )
    )
    return build_frozen_fact(
        TerminalProcessMonitorConditionsFact,
        schema_version="terminal_process_monitor_conditions.v1",
        output=output,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )


def default_monitor_delivery_policy(
    *,
    max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS,
    minimum_progress_observation_interval_seconds: int = 5,
) -> TerminalProcessMonitorDeliveryPolicyFact:
    return build_frozen_fact(
        TerminalProcessMonitorDeliveryPolicyFact,
        schema_version="terminal_process_monitor_delivery_policy.v1",
        max_output_chars=max_output_chars,
        minimum_progress_observation_interval_seconds=(
            minimum_progress_observation_interval_seconds
        ),
        maximum_pending_progress_observations=1,
        maximum_committed_progress_observations=_DEFAULT_MAX_PROGRESS,
        progress_observation_rate_window_seconds=3_600,
        maximum_progress_observations_per_rate_window=_DEFAULT_MAX_PROGRESS,
    )


def default_monitor_lifetime(
    *,
    maximum_duration_seconds: int = MAXIMUM_TERMINAL_MONITOR_DURATION_SECONDS,
) -> TerminalProcessMonitorLifetimeFact:
    return build_frozen_fact(
        TerminalProcessMonitorLifetimeFact,
        schema_version="terminal_process_monitor_lifetime.v1",
        kind="process_lifetime",
        maximum_duration_seconds=maximum_duration_seconds,
    )


def default_autonomy_chain_policy() -> ResolvedTerminalAutonomyChainPolicyFact:
    return build_frozen_fact(
        ResolvedTerminalAutonomyChainPolicyFact,
        schema_version="resolved_terminal_autonomy_chain_policy.v1",
        policy_id="terminal-monitor-autonomy",
        policy_version=1,
        maximum_automatic_deliveries=12,
        minimum_automatic_delivery_interval_seconds=5,
        maximum_notifications_per_autonomous_ingress=8,
    )


def _heartbeat_ready(
    *,
    registration: TerminalProcessMonitorRegisteredEvent,
    core: TerminalProcessMonitorCoreStateFact,
    observed_at_utc: str,
) -> bool:
    interval = (
        registration.registration_semantic.policy.conditions.heartbeat_interval_seconds
    )
    if interval is None:
        return False
    start = (
        core.progress_limiter_state.last_committed_progress_observed_at_utc
        or registration.registration_attribution.registered_at_utc
    )
    return (
        datetime.fromisoformat(observed_at_utc.replace("Z", "+00:00"))
        - datetime.fromisoformat(start.replace("Z", "+00:00"))
    ).total_seconds() >= interval


def monitor_lifetime_expired(
    *,
    expires_at_utc: str,
    observed_at_utc: str,
) -> bool:
    """Return the deterministic UTC lifetime boundary decision."""

    expires = datetime.fromisoformat(expires_at_utc.replace("Z", "+00:00"))
    observed = datetime.fromisoformat(observed_at_utc.replace("Z", "+00:00"))
    return observed >= expires


def _stable_id(prefix: str, *parts: str) -> str:
    digest = sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _canonical_now(value: str | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _format_utc(parsed)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _add_seconds(value: str, seconds: int) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _format_utc(parsed + timedelta(seconds=seconds))


__all__ = [
    "PreparedTerminalProcessMonitorRegistration",
    "TerminalMonitorContractError",
    "TerminalMonitorCoordinator",
    "TerminalMonitorRecord",
    "TerminalMonitorStore",
    "default_monitor_conditions",
    "default_monitor_delivery_policy",
    "default_monitor_lifetime",
    "initial_monitor_core_state",
    "monitor_lifetime_expired",
    "resulting_disposition_core_state",
    "resulting_observation_core_state",
]
