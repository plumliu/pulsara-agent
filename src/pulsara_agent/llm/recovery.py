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
    ModelCallTerminalProjectionCommittedEvent,
    PhysicalOperationReservationCreatedEvent,
    ProviderInputAppendCommittedEvent,
    ProviderInputGenerationClosedEvent,
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
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.provider_input import (
    CommittedProviderInputGenerationCoreStateFact,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.terminal_projection import (
    TERMINAL_PROJECTION_MEDIA_TYPE,
    ModelTerminalProjectionReducer,
    build_default_terminal_projection_contract_bundle,
    hydrate_terminal_projection_text,
    rebuild_model_stream_semantic_commit_measurements,
)
from pulsara_agent.llm.segment import MODEL_STREAM_SEGMENT_POLICY
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.runtime.authority_materialization import (
    LedgerMaterializationAccountStore,
    LedgerMaterializationCoordinator,
    build_default_authority_materialization_contract_bundle,
)
from pulsara_agent.primitives.authority_materialization import (
    PhysicalOperationKind,
    PhysicalOperationReservationFact,
)
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

    def __init__(
        self,
        *,
        event_log: EventLog,
        archive: ArtifactStore,
    ) -> None:
        self._event_log = event_log
        self._archive = archive
        self._terminal_contracts = build_default_terminal_projection_contract_bundle()
        self._materialization_contracts = (
            build_default_authority_materialization_contract_bundle()
        )
        self._domain_fingerprint = self._materialization_contracts.event_domain.contract.transcript_semantic_domain_contract_fingerprint

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
            batch, outcome = self._build_recovery_terminal_batch(
                events=events,
                start=start,
            )
            stored = self._commit_recovery_terminal_batch(
                batch=batch,
                start=start,
                high_water=high_water,
                terminal_outcome=outcome,
                deadline_monotonic=deadline_monotonic,
            )
            expected_ids = tuple(event.id for event in batch)
            if tuple(event.id for event in stored[: len(batch)]) != expected_ids:
                raise ModelStreamRecoveryStructuralError(
                    "model recovery confirmation returned another terminal batch"
                )
            repaired.append(
                RecoveredModelStream(
                    resolved_model_call_id=(start.resolved_call.resolved_model_call_id),
                    lifecycle_kind=start.recovery_plan.lifecycle_kind,
                    terminal_outcome=outcome,
                    terminal_event_ids=expected_ids,
                )
            )

    def _commit_recovery_terminal_batch(
        self,
        *,
        batch: tuple[AgentEvent, ...],
        start: ModelCallStartEvent,
        high_water: int,
        terminal_outcome: str,
        deadline_monotonic: float | None,
    ) -> tuple[AgentEvent, ...]:
        account = self._event_log.read_materialization_account_state(
            deadline_monotonic=deadline_monotonic
        )
        if account is None:
            raise ModelStreamRecoveryStructuralError(
                "non-empty model recovery ledger is missing its required "
                "materialization account"
            )
        if account.ledger_through_sequence != high_water:
            raise ModelStreamRecoveryStructuralError(
                "model recovery account high-water does not match the ledger"
            )
        active = tuple(
            item
            for item in account.active_reservations
            if item.owner_kind is PhysicalOperationKind.MODEL_CALL
            and item.owner_id == start.resolved_call.resolved_model_call_id
        )
        if len(active) != 1:
            raise ModelStreamRecoveryStructuralError(
                "incomplete model stream does not have one exact physical reservation"
            )
        raw = self._event_log.read_raw_events_by_id(
            (active[0].latest_reservation_event_id,),
            deadline_monotonic=deadline_monotonic,
        )
        if len(raw) != 1:
            raise ModelStreamRecoveryStructuralError(
                "model recovery physical reservation creation event is missing"
            )
        reservation_event = raw[0].decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        from pulsara_agent.event import PhysicalOperationReservationCreatedEvent

        if not isinstance(reservation_event, PhysicalOperationReservationCreatedEvent):
            raise ModelStreamRecoveryStructuralError(
                "model recovery reservation reference is not a creation event"
            )
        reservation: PhysicalOperationReservationFact = reservation_event.reservation
        if (
            reservation.reservation_id != active[0].reservation_id
            or reservation.reservation_fingerprint != active[0].reservation_fingerprint
            or reservation.owner_kind is not PhysicalOperationKind.MODEL_CALL
            or reservation.owner_id != start.resolved_call.resolved_model_call_id
        ):
            raise ModelStreamRecoveryStructuralError(
                "model recovery physical reservation identity drifted"
            )
        store = LedgerMaterializationAccountStore(
            state=account,
            charge_contract=self._materialization_contracts.charge_contract,
        )
        coordinator = LedgerMaterializationCoordinator(
            runtime_session_id=self._event_log.runtime_session_id,
            event_log=self._event_log,
            store=store,
            charge_contract=self._materialization_contracts.charge_contract,
            limits=self._materialization_contracts.limits,
        )
        projection_event = next(
            (
                event
                for event in batch
                if isinstance(event, ModelCallTerminalProjectionCommittedEvent)
            ),
            None,
        )
        if projection_event is None:
            raise ModelStreamRecoveryStructuralError(
                "model recovery terminal batch lacks projection"
            )
        projection_text = self._archive.get_text(
            projection_event.projection_reference.document_artifact_id,
            session_id=self._event_log.runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        projection_document = hydrate_terminal_projection_text(
            projection_event.projection_reference,
            projection_text,
        )
        try:
            committed = coordinator.commit_reserved_settlement(
                context=EventContext(
                    run_id=start.run_id,
                    turn_id=start.turn_id,
                    reply_id=start.reply_id,
                ),
                reservation=reservation,
                business_events=batch,
                terminal_outcome=terminal_outcome,
                model_stream_measurement_fingerprint=(
                    projection_document.source_fact.stream_settlement_measurement.measurement_fingerprint
                ),
                deadline_monotonic=deadline_monotonic,
            )
        except BaseException as exc:
            raise ModelStreamRecoveryStructuralError(
                "model recovery terminal settlement could not be confirmed"
            ) from exc
        return committed.stored_events

    def _build_recovery_terminal_batch(
        self,
        *,
        events: tuple[AgentEvent, ...],
        start: ModelCallStartEvent,
    ) -> tuple[tuple[AgentEvent, ...], str]:
        outcome = _recovery_terminal_outcome(events=events, start=start)
        semantic = tuple(
            event
            for event in events
            if getattr(event, "model_stream_attribution", None) is not None
            and event.model_stream_attribution.resolved_model_call_id  # type: ignore[union-attr]
            == start.resolved_call.resolved_model_call_id
            and event.model_stream_attribution.model_call_start_event_id  # type: ignore[union-attr]
            == start.id
        )
        reducer = ModelTerminalProjectionReducer(
            runtime_session_id=self._event_log.runtime_session_id,
            start_event=start,
            contracts=self._terminal_contracts,
            model_stream_semantic_domain_contract_fingerprint=(
                self._domain_fingerprint
            ),
            segment_policy_contract_fingerprint=(
                MODEL_STREAM_SEGMENT_POLICY.contract_fingerprint
            ),
        )
        reducer.apply_committed(semantic)
        prepared = reducer.prepare_terminal(
            event_context=EventContext(
                run_id=start.run_id,
                turn_id=start.turn_id,
                reply_id=start.reply_id,
            ),
            terminal_outcome=outcome,  # type: ignore[arg-type]
            usage_report=TransportUsageReport(
                usage_status="missing",
                usage=None,
            ),
            semantic_commit_measurements=(
                rebuild_model_stream_semantic_commit_measurements(
                    runtime_session_id=self._event_log.runtime_session_id,
                    resolved_model_call_id=(start.resolved_call.resolved_model_call_id),
                    semantic_events=semantic,
                    ledger_events=events,
                )
                if any(
                    isinstance(event, PhysicalOperationReservationCreatedEvent)
                    and event.reservation.owner_kind is PhysicalOperationKind.MODEL_CALL
                    and event.reservation.owner_id
                    == start.resolved_call.resolved_model_call_id
                    for event in events
                )
                else ()
            ),
            physical_accounting_mode=(
                "accounted"
                if any(
                    isinstance(event, PhysicalOperationReservationCreatedEvent)
                    and event.reservation.owner_kind is PhysicalOperationKind.MODEL_CALL
                    and event.reservation.owner_id
                    == start.resolved_call.resolved_model_call_id
                    for event in events
                )
                else "unbootstrapped_test"
            ),
        )
        reference = prepared.projection_reference
        confirmation = self._archive.put_text_if_absent_or_confirm_identical(
            reference.document_artifact_id,
            prepared.canonical_document_bytes.decode("utf-8"),
            session_id=self._event_log.runtime_session_id,
            run_id=start.run_id,
            media_type=TERMINAL_PROJECTION_MEDIA_TYPE,
            semantic_metadata={
                "projection_kind": "model_call",
                "document_fact_fingerprint": reference.document_fact_fingerprint,
                "document_contract_fingerprint": (
                    reference.document_contract_fingerprint
                ),
            },
        )
        if (
            confirmation.result.id != reference.document_artifact_id
            or confirmation.result.digest != reference.document_sha256
            or confirmation.result.size_bytes != reference.document_byte_count
        ):
            raise ModelStreamRecoveryStructuralError(
                "model recovery projection artifact confirmation drifted"
            )
        terminal_tail, checked_outcome = _build_recovery_terminal_batch(
            events=events,
            start=start,
            terminal_projection=prepared.end_reference,
        )
        if checked_outcome != outcome:
            raise AssertionError("model recovery terminal outcome drifted")
        return (
            (prepared.committed_event, *terminal_tail),
            outcome,
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
        if tuple(event.sequence for event in events) != tuple(range(1, high_water + 1)):
            raise ModelStreamRecoveryStructuralError(
                "model recovery input is not one contiguous ledger prefix"
            )
        return high_water, events

    @staticmethod
    def _check_deadline(deadline_monotonic: float | None) -> None:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise TimeoutError("model stream recovery exceeded its deadline")


def _unique_model_starts(
    events: tuple[AgentEvent, ...],
) -> dict[str, ModelCallStartEvent]:
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
    projections = tuple(
        event
        for event in events
        if isinstance(event, ModelCallTerminalProjectionCommittedEvent)
        and event.resolved_model_call_id == start.resolved_call.resolved_model_call_id
    )
    if (
        len(projections) != 1
        or end.terminal_projection is None
        or end.terminal_projection.projection_reference
        != projections[0].projection_reference
        or _required_sequence(projections[0]) + 1 != _required_sequence(end)
    ):
        raise ModelStreamRecoveryStructuralError(
            "model terminal lacks its atomic projection"
        )
    expected: list[AgentEvent] = [end]
    close = _required_one_shot_close(events=events, start=start)
    if close is not None:
        expected.append(close)
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
    terminal_projection,
) -> tuple[tuple[AgentEvent, ...], str]:
    _validate_start_bundle(events=events, start=start)
    plan = start.recovery_plan
    call_id = start.resolved_call.resolved_model_call_id
    outcome = _recovery_terminal_outcome(events=events, start=start)
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
        terminal_projection=terminal_projection,
    )
    batch: list[AgentEvent] = [model_end]
    one_shot_close = _build_recovered_one_shot_close_event(
        events=events,
        start=start,
        event_context=event_context,
    )
    if one_shot_close is not None:
        batch.append(one_shot_close)
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


def _provider_input_append_for_start(
    *,
    events: tuple[AgentEvent, ...],
    start: ModelCallStartEvent,
) -> ProviderInputAppendCommittedEvent | None:
    reference = start.provider_input_reference
    if reference is None:
        return None
    matches = tuple(
        event
        for event in events
        if isinstance(event, ProviderInputAppendCommittedEvent)
        and event.id == reference.append_committed_event_identity.event_id
    )
    if len(matches) != 1:
        raise ModelStreamRecoveryStructuralError(
            "model provider-input reference lacks one exact append event"
        )
    append = matches[0]
    if (
        append.generation_id != reference.generation_id
        or append.resulting_core_state_fingerprint
        != reference.resulting_generation_core_state_fingerprint
    ):
        raise ModelStreamRecoveryStructuralError(
            "model provider-input append/reference identity drifted"
        )
    compiled = append.append_kind == "compiled_manifest"
    if compiled != (reference.reference_kind == "compiled_manifest"):
        raise ModelStreamRecoveryStructuralError(
            "model provider-input append/reference kind drifted"
        )
    if compiled and (
        append.manifest_projection_reference is None
        or append.causal_validation is None
        or reference.manifest_projection_reference_fingerprint
        != append.manifest_projection_reference.reference_fingerprint
        or reference.causal_validation_fingerprint
        != append.causal_validation.result_fingerprint
        or reference.transcript_frontier_fingerprint
        != append.resulting_core_state.transcript_frontier.provider_semantic_frontier_fingerprint
    ):
        raise ModelStreamRecoveryStructuralError(
            "model provider-input manifest proof drifted"
        )
    return append


def _required_one_shot_close(
    *,
    events: tuple[AgentEvent, ...],
    start: ModelCallStartEvent,
) -> ProviderInputGenerationClosedEvent | None:
    append = _provider_input_append_for_start(events=events, start=start)
    if (
        append is None
        or append.resulting_core_state.generation.scope.scope_kind != "one_shot"
    ):
        return None
    matches = tuple(
        event
        for event in events
        if isinstance(event, ProviderInputGenerationClosedEvent)
        and event.generation_id == append.generation_id
        and event.close_reason == "one_shot_terminal"
    )
    if len(matches) != 1:
        raise ModelStreamRecoveryStructuralError(
            "one-shot model terminal lacks one exact generation close"
        )
    return matches[0]


def _build_recovered_one_shot_close_event(
    *,
    events: tuple[AgentEvent, ...],
    start: ModelCallStartEvent,
    event_context: EventContext,
) -> ProviderInputGenerationClosedEvent | None:
    append = _provider_input_append_for_start(events=events, start=start)
    if (
        append is None
        or append.resulting_core_state.generation.scope.scope_kind != "one_shot"
    ):
        return None
    core = append.resulting_core_state
    payload = {
        name: getattr(core, name)
        for name in core.__class__.model_fields
        if name not in {"schema_version", "core_state_fingerprint"}
    }
    payload.update(status="closed", reconciliation_reason=None)
    closed = build_frozen_fact(
        CommittedProviderInputGenerationCoreStateFact,
        schema_version="committed_provider_input_generation_core_state.v3",
        **payload,
    )
    return ProviderInputGenerationClosedEvent(
        id=f"provider_input_generation_closed:{core.generation.generation_id}",
        **event_context.event_fields(),
        created_at=start.created_at,
        generation_id=core.generation.generation_id,
        generation_fingerprint=core.generation.generation_fingerprint,
        final_revision=core.revision,
        final_prefix_fingerprint=core.committed_prefix_fingerprint,
        final_vector_root=core.unit_vector_root,
        close_reason="one_shot_terminal",
        successor_generation_id=None,
        unconsumed_continuation_fingerprint=None,
        predecessor_core_state_fingerprint=core.core_state_fingerprint,
        resulting_closed_core_state=closed,
    )


def _recovery_terminal_outcome(
    *,
    events: tuple[AgentEvent, ...],
    start: ModelCallStartEvent,
) -> str:
    call_id = start.resolved_call.resolved_model_call_id
    semantic = tuple(
        event
        for event in events
        if getattr(event, "model_stream_attribution", None) is not None
        and event.model_stream_attribution.resolved_model_call_id == call_id  # type: ignore[union-attr]
        and event.model_stream_attribution.model_call_start_event_id == start.id  # type: ignore[union-attr]
    )
    indexes = tuple(
        event.model_stream_attribution.durable_semantic_event_index  # type: ignore[union-attr]
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
    return "provider_error" if provider_errors else "runtime_error"


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
        raise ModelStreamRecoveryStructuralError("model Start bundle is not contiguous")


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
        event.reservation.owner_id != start.resolved_call.resolved_model_call_id
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
