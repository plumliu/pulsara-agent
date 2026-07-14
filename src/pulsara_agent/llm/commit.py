"""Typed commit boundary for model-stream lifecycle phases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import json

from pulsara_agent.event import (
    AgentEvent,
    ContextWindowCompactionStartedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RolloutBudgetReservationCreatedEvent,
    RolloutBudgetReservationSettledEvent,
)
from pulsara_agent.event_log.protocol import RawStoredEventEnvelope
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    FrozenEventWriteCandidate,
    decode_event_write_candidate,
    freeze_event_write_candidate,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint

if TYPE_CHECKING:
    from pulsara_agent.llm.execution import ModelStreamLiveSemanticCursor
    from pulsara_agent.llm.lifecycle import (
        ModelLifecycleStartCommitBundle,
        ModelLifecycleKind,
        RolloutAccountingMode,
    )
    from pulsara_agent.runtime.session import EventWriteResult, RuntimeSession
    from pulsara_agent.runtime.state import LoopState


class ModelStreamCommitContractError(RuntimeError):
    pass


class ModelStreamCommitNotCommitted(RuntimeError):
    """A stable model-stream batch is confirmed absent from the ledger."""


@dataclass(frozen=True, slots=True)
class ConfirmedCommittedBatch:
    committed_events: tuple[RawStoredEventEnvelope, ...]
    committed_through_sequence: int
    batch_fingerprint: str

    def decode_owned(self) -> tuple[AgentEvent, ...]:
        return tuple(
            event.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            for event in self.committed_events
        )


@dataclass(frozen=True, slots=True)
class ModelStreamStartCommitGuard:
    resolved_model_call_id: str
    stable_model_call_start_event_id: str
    lifecycle_kind: ModelLifecycleKind
    recovery_plan_fingerprint: str
    rollout_accounting_mode: RolloutAccountingMode
    expected_rollout_account_state_fingerprint: str | None
    reservation_id: str | None
    reservation_quote_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class ModelStreamSemanticCommitGuard:
    resolved_model_call_id: str
    model_call_start_event_id: str
    first_transport_sequence_index: int
    semantic_item_count: int
    expected_previous_semantic_event_id: str | None


@dataclass(frozen=True, slots=True)
class ModelStreamTerminalCommitGuard:
    resolved_model_call_id: str
    model_call_start_event_id: str
    stable_model_call_end_event_id: str
    lifecycle_kind: ModelLifecycleKind
    stable_reply_end_event_id: str | None
    stable_settlement_event_id: str | None
    expected_last_semantic_event_id: str | None
    semantic_item_count: int
    rollout_accounting_mode: RolloutAccountingMode
    reservation_id: str | None
    reservation_quote_fingerprint: str | None


def build_model_stream_start_commit_guard(
    bundle: ModelLifecycleStartCommitBundle,
) -> ModelStreamStartCommitGuard:
    reservation = bundle.reservation
    return ModelStreamStartCommitGuard(
        resolved_model_call_id=bundle.resolved_model_call_id,
        stable_model_call_start_event_id=(
            bundle.recovery_plan.model_call_start_event_id
        ),
        lifecycle_kind=bundle.lifecycle_kind,
        recovery_plan_fingerprint=(
            bundle.recovery_plan.recovery_plan_fingerprint
        ),
        rollout_accounting_mode=bundle.rollout_accounting_mode,
        expected_rollout_account_state_fingerprint=(
            bundle.expected_rollout_account_state_fingerprint
        ),
        reservation_id=(reservation.reservation_id if reservation else None),
        reservation_quote_fingerprint=(
            bundle.reservation_quote.quote_fact_fingerprint
            if bundle.reservation_quote is not None
            else None
        ),
    )


def build_model_stream_terminal_commit_guard(
    bundle: ModelLifecycleStartCommitBundle,
    *,
    expected_last_semantic_event_id: str | None,
    semantic_item_count: int,
) -> ModelStreamTerminalCommitGuard:
    reservation = bundle.reservation
    return ModelStreamTerminalCommitGuard(
        resolved_model_call_id=bundle.resolved_model_call_id,
        model_call_start_event_id=bundle.recovery_plan.model_call_start_event_id,
        stable_model_call_end_event_id=(
            bundle.recovery_plan.stable_model_call_end_event_id
        ),
        lifecycle_kind=bundle.lifecycle_kind,
        stable_reply_end_event_id=bundle.stable_reply_end_event_id,
        stable_settlement_event_id=(
            bundle.recovery_plan.stable_settlement_event_id
        ),
        expected_last_semantic_event_id=expected_last_semantic_event_id,
        semantic_item_count=semantic_item_count,
        rollout_accounting_mode=bundle.rollout_accounting_mode,
        reservation_id=reservation.reservation_id if reservation else None,
        reservation_quote_fingerprint=(
            bundle.reservation_quote.quote_fact_fingerprint
            if bundle.reservation_quote is not None
            else None
        ),
    )


class RuntimeSessionModelStreamEventCommitPort:
    """Commit one model phase through RuntimeSession's canonical writer."""

    def __init__(
        self,
        *,
        runtime_session: RuntimeSession,
        state: LoopState | None,
    ) -> None:
        self._runtime_session = runtime_session
        self._state = state

    @property
    def runtime_session(self) -> RuntimeSession:
        return self._runtime_session

    @property
    def state(self) -> LoopState | None:
        return self._state

    async def commit_start(
        self,
        candidates: tuple[FrozenEventWriteCandidate, ...],
        *,
        guard: ModelStreamStartCommitGuard,
    ) -> ConfirmedCommittedBatch:
        events = self._decode_candidates(candidates)
        starts = tuple(event for event in events if isinstance(event, ModelCallStartEvent))
        if len(starts) != 1 or events[-1] is not starts[0]:
            raise ModelStreamCommitContractError(
                "model start batch requires exactly one final ModelCallStartEvent"
            )
        allowed = (
            ContextWindowCompactionStartedEvent,
            ModelCallStartEvent,
            ReplyStartEvent,
            RolloutBudgetReservationCreatedEvent,
        )
        if any(not isinstance(event, allowed) for event in events):
            raise ModelStreamCommitContractError(
                "model start batch contains a non-start fact"
            )
        start = starts[0]
        if (
            start.id != guard.stable_model_call_start_event_id
            or start.resolved_call.resolved_model_call_id
            != guard.resolved_model_call_id
            or start.recovery_plan.lifecycle_kind != guard.lifecycle_kind
            or start.recovery_plan.recovery_plan_fingerprint
            != guard.recovery_plan_fingerprint
        ):
            raise ModelStreamCommitContractError(
                "model start batch does not match its frozen commit guard"
            )
        reservations = tuple(
            event.reservation
            for event in events
            if isinstance(event, RolloutBudgetReservationCreatedEvent)
        )
        accounted = guard.rollout_accounting_mode != "not_rollout_accounted"
        if accounted != (len(reservations) == 1):
            raise ModelStreamCommitContractError(
                "model start accounting mode does not match reservation batch"
            )
        reservation = reservations[0] if reservations else None
        if accounted:
            assert reservation is not None
            quote = reservation.model_call_reservation_quote
            if (
                guard.expected_rollout_account_state_fingerprint is None
                or guard.reservation_id != reservation.reservation_id
                or quote is None
                or guard.reservation_quote_fingerprint
                != quote.quote_fact_fingerprint
                or reservation.owner_id != guard.resolved_model_call_id
            ):
                raise ModelStreamCommitContractError(
                    "model start reservation does not match its frozen guard"
                )
        elif any(
            value is not None
            for value in (
                guard.expected_rollout_account_state_fingerprint,
                guard.reservation_id,
                guard.reservation_quote_fingerprint,
            )
        ):
            raise ModelStreamCommitContractError(
                "unaccounted model start cannot carry rollout guard fields"
            )

        self._runtime_session.publisher.bind_running_loop()

        def commit_start_in_writer() -> ConfirmedCommittedBatch:
            # The same reentrant session lock covers state comparison and the
            # append, so an unrelated settlement cannot slip between them.
            with self._runtime_session.write_coordinator.lock:
                if reservation is not None:
                    self._require_expected_rollout_state(
                        run_id=start.run_id,
                        account_id=reservation.account_id,
                        accounting_mode=guard.rollout_accounting_mode,
                        expected_fingerprint=(
                            guard.expected_rollout_account_state_fingerprint
                        ),
                    )
                return self._commit_in_writer(candidates, events=events)

        return await self._runtime_session.event_write_service.execute(
            commit_start_in_writer
        )

    async def commit_semantic(
        self,
        candidates: tuple[FrozenEventWriteCandidate, ...],
        *,
        guard: ModelStreamSemanticCommitGuard,
        live_cursor: ModelStreamLiveSemanticCursor,
    ) -> ConfirmedCommittedBatch:
        events = self._decode_candidates(candidates)
        if not events or len(events) != guard.semantic_item_count:
            raise ModelStreamCommitContractError(
                "model semantic commit candidate count drifted"
            )
        for offset, event in enumerate(events):
            attribution = getattr(event, "model_stream_attribution", None)
            if attribution is None or isinstance(
                event,
                (
                    ModelCallStartEvent,
                    ModelCallEndEvent,
                    ReplyStartEvent,
                    ReplyEndEvent,
                ),
            ):
                raise ModelStreamCommitContractError(
                    "model semantic commit requires attributed semantic events"
                )
            if (
                attribution.resolved_model_call_id != guard.resolved_model_call_id
                or attribution.model_call_start_event_id
                != guard.model_call_start_event_id
                or attribution.transport_sequence_index
                != guard.first_transport_sequence_index + offset
            ):
                raise ModelStreamCommitContractError(
                    "model semantic event does not match its frozen commit guard"
                )
        self._runtime_session.publisher.bind_running_loop()

        def commit_semantic_in_writer() -> ConfirmedCommittedBatch:
            with self._runtime_session.write_coordinator.lock:
                self._require_live_semantic_cursor(live_cursor, guard)
                result = self._commit_in_writer(candidates, events=events)
                live_cursor.advance_semantic(result.decode_owned())
                return result

        return await self._runtime_session.event_write_service.execute(
            commit_semantic_in_writer
        )

    async def commit_terminal(
        self,
        candidates: tuple[FrozenEventWriteCandidate, ...],
        *,
        guard: ModelStreamTerminalCommitGuard,
        live_cursor: ModelStreamLiveSemanticCursor,
    ) -> ConfirmedCommittedBatch:
        events = self._decode_candidates(candidates)
        if not events or not isinstance(events[0], ModelCallEndEvent):
            raise ModelStreamCommitContractError(
                "model terminal batch must begin with ModelCallEndEvent"
            )
        if sum(isinstance(event, ModelCallEndEvent) for event in events) != 1:
            raise ModelStreamCommitContractError(
                "model terminal batch requires exactly one ModelCallEndEvent"
            )
        allowed = (
            ModelCallEndEvent,
            RolloutBudgetReservationSettledEvent,
            ReplyEndEvent,
        )
        if any(not isinstance(event, allowed) for event in events):
            raise ModelStreamCommitContractError(
                "model terminal batch contains a non-terminal fact"
            )
        if any(isinstance(event, ReplyEndEvent) for event in events[:-1]):
            raise ModelStreamCommitContractError(
                "ReplyEndEvent must be the final terminal fact"
            )
        model_end = events[0]
        assert isinstance(model_end, ModelCallEndEvent)
        if (
            model_end.id != guard.stable_model_call_end_event_id
            or model_end.resolved_model_call_id != guard.resolved_model_call_id
        ):
            raise ModelStreamCommitContractError(
                "model terminal end does not match its frozen commit guard"
            )
        settlements = tuple(
            event
            for event in events
            if isinstance(event, RolloutBudgetReservationSettledEvent)
        )
        reply_ends = tuple(
            event for event in events if isinstance(event, ReplyEndEvent)
        )
        accounted = guard.rollout_accounting_mode != "not_rollout_accounted"
        if accounted != (len(settlements) == 1):
            raise ModelStreamCommitContractError(
                "model terminal accounting mode does not match settlement batch"
            )
        if guard.lifecycle_kind == "main_assistant_reply":
            if (
                len(reply_ends) != 1
                or reply_ends[0].id != guard.stable_reply_end_event_id
                or reply_ends[0].model_terminal_outcome != model_end.outcome
            ):
                raise ModelStreamCommitContractError(
                    "main model terminal requires its matching ReplyEndEvent"
                )
        elif reply_ends or guard.stable_reply_end_event_id is not None:
            raise ModelStreamCommitContractError(
                "direct/window terminal cannot carry reply lifecycle"
            )
        if accounted:
            settlement = settlements[0]
            if (
                settlement.id != guard.stable_settlement_event_id
                or settlement.reservation_id != guard.reservation_id
                or settlement.source_model_call_end_event_id != model_end.id
                or guard.reservation_quote_fingerprint is None
            ):
                raise ModelStreamCommitContractError(
                    "model terminal settlement does not match its frozen guard"
                )
        elif any(
            value is not None
            for value in (
                guard.stable_settlement_event_id,
                guard.reservation_id,
                guard.reservation_quote_fingerprint,
            )
        ):
            raise ModelStreamCommitContractError(
                "unaccounted model terminal cannot carry rollout guard fields"
            )

        self._runtime_session.publisher.bind_running_loop()

        def commit_terminal_in_writer() -> ConfirmedCommittedBatch:
            with self._runtime_session.write_coordinator.lock:
                self._require_live_terminal_cursor(live_cursor, guard)
                if accounted:
                    self._require_active_reservation(guard, run_id=model_end.run_id)
                try:
                    result = self._commit_in_writer(candidates, events=events)
                    committed_end = next(
                        event
                        for event in result.decode_owned()
                        if isinstance(event, ModelCallEndEvent)
                    )
                    live_cursor.mark_terminal(committed_end)
                    return result
                except BaseException as exc:
                    from pulsara_agent.runtime.session import EventCommitError

                    if isinstance(exc, EventCommitError):
                        raise ModelStreamCommitNotCommitted(
                            "stable model-stream terminal batch was not committed"
                        ) from exc
                    raise

        return await self._runtime_session.event_write_service.execute(
            commit_terminal_in_writer
        )

    def _commit_in_writer(
        self,
        candidates: tuple[FrozenEventWriteCandidate, ...],
        *,
        events: tuple[AgentEvent, ...],
    ) -> ConfirmedCommittedBatch:
        """Commit/reduce/enqueue without awaiting an observer callback.

        A model worker owns durable progress. Publication acknowledgement is not
        part of that progress and cannot cancel it. If a lower writer raises a
        BaseException after commit, stable IDs recover the exact committed batch
        and restore reducer/publisher handoff before returning.
        """

        try:
            result = self._runtime_session.write_events_from_thread(
                events,
                state=self._state,
            )
        except BaseException as original:
            try:
                result = self._runtime_session.confirm_and_handoff_event_batch(
                    events,
                    state=self._state,
                )
            except Exception as confirmation_error:
                from pulsara_agent.runtime.session import EventCommitError

                if isinstance(confirmation_error, EventCommitError):
                    raise original
                self._runtime_session.latch_event_commit_outcome_unknown()
                raise
        return self._confirmed_batch(candidates, result=result)

    @staticmethod
    def _decode_candidates(
        candidates: tuple[FrozenEventWriteCandidate, ...],
    ) -> tuple[AgentEvent, ...]:
        if not candidates:
            raise ModelStreamCommitContractError(
                "model stream commit batch cannot be empty"
            )
        ids = tuple(candidate.event_id for candidate in candidates)
        if len(ids) != len(set(ids)):
            raise ModelStreamCommitContractError(
                "model stream commit candidate ids must be unique"
            )
        return tuple(
            decode_event_write_candidate(candidate) for candidate in candidates
        )

    def _confirmed_batch(
        self,
        candidates: tuple[FrozenEventWriteCandidate, ...],
        *,
        result: EventWriteResult,
    ) -> ConfirmedCommittedBatch:
        ids = tuple(candidate.event_id for candidate in candidates)
        committed_events = result.committed_events
        if len(committed_events) != len(candidates):
            raise ModelStreamCommitContractError(
                "model stream commit confirmation is missing a candidate"
            )
        raw = tuple(
            RawStoredEventEnvelope.from_stored_event(
                event=event,
                runtime_session_id=self._runtime_session.runtime_session_id,
                schema_registry=DEFAULT_EVENT_SCHEMA_REGISTRY,
            )
            for event in committed_events
        )
        for candidate, stored in zip(candidates, raw, strict=True):
            expected_candidate = freeze_event_write_candidate(
                self._runtime_session.prepare_event_for_write(
                    decode_event_write_candidate(candidate)
                )
            )
            if (
                expected_candidate.event_id != stored.event_id
                or expected_candidate.event_type != stored.event_type
                or expected_candidate.event_schema_version
                != stored.event_schema_version
                or expected_candidate.event_schema_fingerprint
                != stored.event_schema_fingerprint
                or expected_candidate.event_domain_contract_fingerprint
                != stored.event_domain_contract_fingerprint
                or not _stored_payload_matches_candidate(
                    stored, expected_candidate
                )
            ):
                raise ModelStreamCommitContractError(
                    "model stream committed event drifted from its frozen candidate"
                )
        sequences = tuple(event.sequence for event in raw)
        if sequences != tuple(range(sequences[0], sequences[-1] + 1)):
            raise ModelStreamCommitContractError(
                "model stream committed batch is not contiguous"
            )
        if tuple(event.id for event in result.committed_events) != ids:
            raise ModelStreamCommitContractError(
                "model stream writer acknowledgement order drifted"
            )
        payload = {
            "committed_through_sequence": sequences[-1],
            "envelope_fingerprints": tuple(
                event.envelope_fingerprint for event in raw
            ),
        }
        return ConfirmedCommittedBatch(
            committed_events=raw,
            committed_through_sequence=sequences[-1],
            batch_fingerprint=context_fingerprint(
                "confirmed-model-stream-batch:v1", payload
            ),
        )

    def _require_expected_rollout_state(
        self,
        *,
        run_id: str,
        account_id: str,
        accounting_mode: RolloutAccountingMode,
        expected_fingerprint: str | None,
    ) -> None:
        from pulsara_agent.runtime.long_horizon.accounting import (
            resolve_run_rollout_binding,
        )

        if expected_fingerprint is None:
            raise ModelStreamCommitContractError(
                "accounted model start requires an expected rollout state"
            )
        if accounting_mode == "root_account":
            state = self._runtime_session.long_horizon_state_store.rollout_state(
                account_id
            )
            if state is None:
                raise ModelStreamCommitContractError(
                    "root-account start lacks its rollout reducer state"
                )
            actual = state.state_fingerprint
        elif accounting_mode == "child_subaccount":
            binding = resolve_run_rollout_binding(
                self._runtime_session,
                run_id=run_id,
            )
            if binding.child_state is None:
                raise ModelStreamCommitContractError(
                    "child-account start lacks a child rollout ledger"
                )
            actual = binding.child_state.state_fingerprint
        else:
            raise ModelStreamCommitContractError(
                "unaccounted model start cannot validate rollout state"
            )
        if actual != expected_fingerprint:
            raise ModelStreamCommitContractError(
                "model start rollout account state changed after preparation"
            )

    @staticmethod
    def _require_live_semantic_cursor(
        live_cursor: ModelStreamLiveSemanticCursor,
        guard: ModelStreamSemanticCommitGuard,
    ) -> None:
        try:
            live_cursor.require_open(
                resolved_model_call_id=guard.resolved_model_call_id,
                model_call_start_event_id=guard.model_call_start_event_id,
                expected_semantic_item_count=guard.first_transport_sequence_index,
                expected_previous_semantic_event_id=(
                    guard.expected_previous_semantic_event_id
                ),
            )
        except RuntimeError as exc:
            raise ModelStreamCommitContractError(str(exc)) from exc

    @staticmethod
    def _require_live_terminal_cursor(
        live_cursor: ModelStreamLiveSemanticCursor,
        guard: ModelStreamTerminalCommitGuard,
    ) -> None:
        try:
            live_cursor.require_open(
                resolved_model_call_id=guard.resolved_model_call_id,
                model_call_start_event_id=guard.model_call_start_event_id,
                expected_semantic_item_count=guard.semantic_item_count,
                expected_previous_semantic_event_id=(
                    guard.expected_last_semantic_event_id
                ),
            )
        except RuntimeError as exc:
            raise ModelStreamCommitContractError(str(exc)) from exc

    def _require_active_reservation(
        self,
        guard: ModelStreamTerminalCommitGuard,
        *,
        run_id: str,
    ) -> None:
        from pulsara_agent.runtime.long_horizon.accounting import (
            resolve_run_rollout_binding,
        )

        if guard.rollout_accounting_mode == "root_account":
            matching_states = tuple(
                state
                for state in self._runtime_session.long_horizon_state_store.rollout_states()
                if any(
                    item.reservation_id == guard.reservation_id
                    for item in state.active_reservations
                )
            )
            if len(matching_states) != 1:
                raise ModelStreamCommitContractError(
                    "model terminal root reservation is missing or ambiguous"
                )
            active = matching_states[0].active_reservations
        elif guard.rollout_accounting_mode == "child_subaccount":
            binding = resolve_run_rollout_binding(
                self._runtime_session,
                run_id=run_id,
            )
            if binding.child_state is None:
                raise ModelStreamCommitContractError(
                    "child model terminal lacks its local rollout ledger"
                )
            active = binding.child_state.active_reservations
        else:
            raise ModelStreamCommitContractError(
                "unaccounted terminal cannot validate a reservation"
            )
        matching = tuple(
            item for item in active if item.reservation_id == guard.reservation_id
        )
        if len(matching) != 1:
            raise ModelStreamCommitContractError(
                "model terminal reservation is missing or ambiguous"
            )
        quote = matching[0].model_call_reservation_quote
        if (
            matching[0].owner_id != guard.resolved_model_call_id
            or quote is None
            or quote.quote_fact_fingerprint
            != guard.reservation_quote_fingerprint
        ):
            raise ModelStreamCommitContractError(
                "model terminal reservation quote identity drifted"
            )

def _stored_payload_matches_candidate(
    stored: RawStoredEventEnvelope,
    candidate: FrozenEventWriteCandidate,
) -> bool:
    payload = json.loads(stored.canonical_payload_bytes.decode("utf-8"))
    if not isinstance(payload, dict):
        return False
    payload["sequence"] = None
    return canonical_json_bytes(payload) == candidate.canonical_payload_bytes


__all__ = [
    "ConfirmedCommittedBatch",
    "ModelStreamSemanticCommitGuard",
    "ModelStreamStartCommitGuard",
    "ModelStreamTerminalCommitGuard",
    "ModelStreamCommitContractError",
    "ModelStreamCommitNotCommitted",
    "RuntimeSessionModelStreamEventCommitPort",
    "build_model_stream_start_commit_guard",
    "build_model_stream_terminal_commit_guard",
]
