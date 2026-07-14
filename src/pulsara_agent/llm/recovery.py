"""Deterministic recovery for durably started model streams.

The process-local execution handle disappears on restart.  This service uses
only the Start-frozen recovery plan and the canonical event prefix to close an
incomplete lifecycle without contacting the provider or inventing new model
identity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ProviderModelStreamErrorEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RolloutBudgetAccountOpenedEvent,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.primitives.model_call import ModelCallDiagnosticFact
from pulsara_agent.llm.accounting import (
    build_model_reservation_settlement_event,
)


class ModelStreamRecoveryError(RuntimeError):
    """Base class for model-stream recovery failures."""


class ModelStreamRecoveryStructuralError(ModelStreamRecoveryError):
    """The durable lifecycle cannot be repaired without guessing."""


@dataclass(frozen=True, slots=True)
class RecoveredModelStream:
    resolved_model_call_id: str
    lifecycle_kind: str
    terminal_outcome: str
    terminal_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelStreamRecoveryReport:
    through_sequence_before: int
    through_sequence_after: int
    repaired: tuple[RecoveredModelStream, ...]
    already_terminal_call_ids: tuple[str, ...]


class ModelStreamRecoveryService:
    """Recover Start-without-End lifecycles in one quiescent runtime ledger."""

    def __init__(self, *, event_log: EventLog) -> None:
        self._event_log = event_log

    def repair_incomplete_model_streams(
        self,
        *,
        through_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> ModelStreamRecoveryReport:
        snapshot = self._read_snapshot(
            through_sequence=through_sequence,
            deadline_monotonic=deadline_monotonic,
        )
        before = snapshot[0]
        repaired: list[RecoveredModelStream] = []
        already_terminal: list[str] = []

        while True:
            self._check_deadline(deadline_monotonic)
            high_water, events = self._read_snapshot(
                through_sequence=None,
                deadline_monotonic=deadline_monotonic,
            )
            starts = _unique_model_starts(events)
            incomplete = []
            for call_id, start in starts.items():
                ends = _matching_ends(events, call_id)
                if ends:
                    _validate_existing_terminal(events=events, start=start, ends=ends)
                    if call_id not in already_terminal:
                        already_terminal.append(call_id)
                    continue
                incomplete.append(start)
            if not incomplete:
                return ModelStreamRecoveryReport(
                    through_sequence_before=before,
                    through_sequence_after=high_water,
                    repaired=tuple(repaired),
                    already_terminal_call_ids=tuple(already_terminal),
                )

            start = min(incomplete, key=_required_sequence)
            batch, outcome = _build_recovery_terminal_batch(
                events=events,
                start=start,
            )
            try:
                stored = tuple(
                    self._event_log.extend(
                        batch,
                        expected_last_sequence=high_water,
                    )
                )
            except BaseException:
                confirmation = self._event_log.confirm_batch(batch)
                if confirmation.missing_event_ids:
                    if confirmation.committed_events:
                        raise ModelStreamRecoveryStructuralError(
                            "model recovery terminal batch committed partially"
                        )
                    raise
                stored = confirmation.committed_events
            expected_ids = tuple(event.id for event in batch)
            if tuple(event.id for event in stored) != expected_ids:
                raise ModelStreamRecoveryStructuralError(
                    "model recovery confirmation returned another terminal batch"
                )
            repaired.append(
                RecoveredModelStream(
                    resolved_model_call_id=(
                        start.resolved_call.resolved_model_call_id
                    ),
                    lifecycle_kind=start.recovery_plan.lifecycle_kind,
                    terminal_outcome=outcome,
                    terminal_event_ids=expected_ids,
                )
            )

    def _read_snapshot(
        self,
        *,
        through_sequence: int | None,
        deadline_monotonic: float | None,
    ) -> tuple[int, tuple[AgentEvent, ...]]:
        self._check_deadline(deadline_monotonic)
        high_water = (
            through_sequence
            if through_sequence is not None
            else self._event_log.next_sequence() - 1  # type: ignore[attr-defined]
        )
        if high_water == 0:
            return 0, ()
        raw = self._event_log.read_raw_range_snapshot(
            minimum_sequence=1,
            through_sequence=high_water,
            deadline_monotonic=deadline_monotonic,
        )
        events = tuple(
            envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            for envelope in raw.events
        )
        if tuple(event.sequence for event in events) != tuple(
            range(1, high_water + 1)
        ):
            raise ModelStreamRecoveryStructuralError(
                "model recovery input is not one contiguous ledger prefix"
            )
        return high_water, events

    @staticmethod
    def _check_deadline(deadline_monotonic: float | None) -> None:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise TimeoutError("model stream recovery exceeded its deadline")


def _unique_model_starts(events: tuple[AgentEvent, ...]) -> dict[str, ModelCallStartEvent]:
    starts: dict[str, ModelCallStartEvent] = {}
    for event in events:
        if not isinstance(event, ModelCallStartEvent):
            continue
        call_id = event.resolved_call.resolved_model_call_id
        if call_id in starts:
            raise ModelStreamRecoveryStructuralError(
                "model stream has duplicate Start identity"
            )
        starts[call_id] = event
    return starts


def _matching_ends(
    events: tuple[AgentEvent, ...], call_id: str
) -> tuple[ModelCallEndEvent, ...]:
    return tuple(
        event
        for event in events
        if isinstance(event, ModelCallEndEvent)
        and event.resolved_model_call_id == call_id
    )


def _validate_existing_terminal(
    *,
    events: tuple[AgentEvent, ...],
    start: ModelCallStartEvent,
    ends: tuple[ModelCallEndEvent, ...],
) -> None:
    if len(ends) != 1:
        raise ModelStreamRecoveryStructuralError(
            "model stream has duplicate terminal events"
        )
    end = ends[0]
    plan = start.recovery_plan
    if (
        end.id != plan.stable_model_call_end_event_id
        or end.target_fingerprint != start.resolved_call.target.target_fingerprint
        or _required_sequence(end) <= _required_sequence(start)
    ):
        raise ModelStreamRecoveryStructuralError(
            "model stream terminal identity does not match Start"
        )
    expected: list[AgentEvent] = [end]
    if plan.stable_settlement_event_id is not None:
        settlements = tuple(
            event
            for event in events
            if isinstance(event, RolloutBudgetReservationSettledEvent)
            and event.id == plan.stable_settlement_event_id
        )
        if len(settlements) != 1:
            raise ModelStreamRecoveryStructuralError(
                "model terminal lacks its atomic settlement"
            )
        settlement = settlements[0]
        if (
            settlement.reservation_id != plan.reservation_id
            or settlement.source_model_call_end_event_id != end.id
        ):
            raise ModelStreamRecoveryStructuralError(
                "model settlement attribution mismatch"
            )
        expected.append(settlement)
    if plan.stable_reply_end_event_id is not None:
        replies = tuple(
            event
            for event in events
            if isinstance(event, ReplyEndEvent)
            and event.id == plan.stable_reply_end_event_id
        )
        if len(replies) != 1 or replies[0].model_terminal_outcome != end.outcome:
            raise ModelStreamRecoveryStructuralError(
                "model terminal lacks its atomic ReplyEnd"
            )
        expected.append(replies[0])
    if tuple(_required_sequence(item) for item in expected) != tuple(
        range(_required_sequence(end), _required_sequence(end) + len(expected))
    ):
        raise ModelStreamRecoveryStructuralError(
            "model terminal batch is not contiguous"
        )


def _build_recovery_terminal_batch(
    *,
    events: tuple[AgentEvent, ...],
    start: ModelCallStartEvent,
) -> tuple[tuple[AgentEvent, ...], str]:
    _validate_start_bundle(events=events, start=start)
    plan = start.recovery_plan
    call_id = start.resolved_call.resolved_model_call_id
    semantic = tuple(
        event
        for event in events
        if getattr(event, "model_stream_attribution", None) is not None
        and event.model_stream_attribution.resolved_model_call_id == call_id  # type: ignore[union-attr]
        and event.model_stream_attribution.model_call_start_event_id == start.id  # type: ignore[union-attr]
    )
    indexes = tuple(
        event.model_stream_attribution.transport_sequence_index  # type: ignore[union-attr]
        for event in semantic
    )
    if indexes != tuple(range(len(semantic))):
        raise ModelStreamRecoveryStructuralError(
            "model recovery semantic prefix is not contiguous"
        )
    provider_errors = tuple(
        (index, event)
        for index, event in enumerate(semantic)
        if isinstance(event, ProviderModelStreamErrorEvent)
    )
    if len(provider_errors) > 1 or (
        provider_errors and provider_errors[0][0] != len(semantic) - 1
    ):
        raise ModelStreamRecoveryStructuralError(
            "model recovery provider-error winner is ambiguous"
        )
    outcome = "provider_error" if provider_errors else "runtime_error"
    event_context = EventContext(
        run_id=start.run_id,
        turn_id=start.turn_id,
        reply_id=start.reply_id,
    )
    model_end = ModelCallEndEvent(
        id=plan.stable_model_call_end_event_id,
        **event_context.event_fields(),
        created_at=start.created_at,
        resolved_model_call_id=call_id,
        target_fingerprint=start.resolved_call.target.target_fingerprint,
        reported_model_id=None,
        outcome=outcome,
        provider_dispatch_status="dispatched",
        usage_status="missing",
        usage=None,
        estimated_input_tokens=plan.pre_send_estimated_input_tokens,
        diagnostics=(
            ModelCallDiagnosticFact(
                code="process_restarted_before_model_terminal",
                message="Model stream was terminalized during session reopen.",
            ),
        ),
    )
    batch: list[AgentEvent] = [model_end]
    reservation = _matching_reservation(events=events, start=start)
    if reservation is not None:
        accounts = tuple(
            event.account
            for event in events
            if isinstance(event, RolloutBudgetAccountOpenedEvent)
            and event.account.account_id == reservation.reservation.account_id
        )
        if len(accounts) != 1:
            raise ModelStreamRecoveryStructuralError(
                "model recovery reservation lacks its rollout account"
            )
        settlement = build_model_reservation_settlement_event(
            event_context=event_context,
            account=accounts[0],
            reservation=reservation.reservation,
            model_end=model_end,
        ).model_copy(update={"created_at": start.created_at})
        if settlement.id != plan.stable_settlement_event_id:
            raise ModelStreamRecoveryStructuralError(
                "model recovery settlement stable identity mismatch"
            )
        batch.append(settlement)
    if plan.stable_reply_end_event_id is not None:
        batch.append(
            ReplyEndEvent(
                id=plan.stable_reply_end_event_id,
                **event_context.event_fields(),
                created_at=start.created_at,
                model_terminal_outcome=outcome,
            )
        )
    return tuple(batch), outcome


def _validate_start_bundle(
    *, events: tuple[AgentEvent, ...], start: ModelCallStartEvent
) -> None:
    plan = start.recovery_plan
    expected: list[AgentEvent] = []
    if plan.reply_start_event_id is not None:
        replies = tuple(
            event
            for event in events
            if isinstance(event, ReplyStartEvent)
            and event.id == plan.reply_start_event_id
        )
        if len(replies) != 1:
            raise ModelStreamRecoveryStructuralError(
                "model Start lacks its atomic ReplyStart"
            )
        expected.append(replies[0])
    reservation = _matching_reservation(events=events, start=start)
    if reservation is not None:
        expected.append(reservation)
    expected.append(start)
    first = _required_sequence(start) - len(expected) + 1
    if tuple(_required_sequence(item) for item in expected) != tuple(
        range(first, _required_sequence(start) + 1)
    ):
        raise ModelStreamRecoveryStructuralError(
            "model Start bundle is not contiguous"
        )


def _matching_reservation(
    *, events: tuple[AgentEvent, ...], start: ModelCallStartEvent
) -> RolloutBudgetReservationCreatedEvent | None:
    plan = start.recovery_plan
    if plan.reservation_id is None:
        return None
    matches = tuple(
        event
        for event in events
        if isinstance(event, RolloutBudgetReservationCreatedEvent)
        and event.reservation.reservation_id == plan.reservation_id
    )
    if len(matches) != 1:
        raise ModelStreamRecoveryStructuralError(
            "model Start lacks its exact rollout reservation"
        )
    event = matches[0]
    quote = event.reservation.model_call_reservation_quote
    if (
        event.reservation.owner_id
        != start.resolved_call.resolved_model_call_id
        or quote is None
        or quote.quote_fact_fingerprint != plan.reservation_quote_fingerprint
    ):
        raise ModelStreamRecoveryStructuralError(
            "model Start reservation quote attribution mismatch"
        )
    return event


def _required_sequence(event: AgentEvent) -> int:
    if event.sequence is None or event.sequence < 1:
        raise ModelStreamRecoveryStructuralError(
            "model stream recovery requires committed events"
        )
    return event.sequence


__all__ = [
    "ModelStreamRecoveryError",
    "ModelStreamRecoveryReport",
    "ModelStreamRecoveryService",
    "ModelStreamRecoveryStructuralError",
    "RecoveredModelStream",
]
