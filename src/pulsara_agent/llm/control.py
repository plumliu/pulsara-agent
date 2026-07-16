"""Live model-call control disposition linearization.

The durable disposition records whether one completed main model call was
accepted by the run control plane.  Process-local segment identity never enters
that event; it is carried only by the ephemeral permit returned to the active
AgentRuntime segment.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Literal, Mapping

from pulsara_agent.event import (
    EventContext,
    ModelCallControlDispositionResolvedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
)
from pulsara_agent.primitives.model_call import (
    CommittedModelCallResult,
    ModelCallControlDisposition,
    ModelCallResultControlDisposition,
    RunTerminationIntentAttributionFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.run_boundary import RunExecutionActivationFact
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession
    from pulsara_agent.runtime.state import LoopState


class ModelCallControlResolutionError(RuntimeError):
    """A completed call could not acquire a valid control disposition."""


_CONTROL_DISPOSITION_COMMIT_ATTEMPTS = 3


def model_call_control_disposition_event_id(
    *, run_id: str, resolved_model_call_id: str, model_call_index: int
) -> str:
    return (
        f"model_call_control_disposition:{run_id}:"
        f"{resolved_model_call_id}:{model_call_index}"
    )


def build_model_call_control_disposition_event(
    *,
    result: CommittedModelCallResult,
    model_call_index: int,
    event_context: EventContext,
    activation: RunExecutionActivationFact,
    disposition: ModelCallControlDisposition,
    termination_intent: RunTerminationIntentAttributionFact | None,
    recovery_reason_code: str | None,
    created_at: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> ModelCallControlDispositionResolvedEvent:
    fields = {
        "id": model_call_control_disposition_event_id(
            run_id=event_context.run_id,
            resolved_model_call_id=result.resolved_model_call_id,
            model_call_index=model_call_index,
        ),
        **event_context.event_fields(),
        "resolved_model_call_id": result.resolved_model_call_id,
        "model_call_start_event_id": result.model_call_start_event_id,
        "model_call_end_event_id": result.model_call_end_event_id,
        "model_call_index": model_call_index,
        "source_result_fingerprint": result.result_fingerprint,
        "run_execution_activation": activation,
        "disposition": disposition,
        "termination_intent": termination_intent,
        "recovery_reason_code": recovery_reason_code,
        "metadata": dict(metadata or {}),
    }
    if created_at is not None:
        fields["created_at"] = created_at
    provisional = ModelCallControlDispositionResolvedEvent.model_construct(
        **fields,
        event_fingerprint="pending",
    )
    canonical = provisional.model_dump(
        mode="json", exclude={"event_fingerprint", "sequence"}
    )
    return ModelCallControlDispositionResolvedEvent(
        **canonical,
        event_fingerprint=sha256_fingerprint(
            "model-call-control-disposition-event:v1", canonical
        ),
    )


@dataclass(frozen=True, slots=True)
class ModelCallControlPermit:
    disposition_event_id: str
    resolved_model_call_id: str
    source_result_fingerprint: str
    run_execution_activation_fingerprint: str
    segment_id: str
    segment_generation: int
    permit_fingerprint: str


@dataclass(frozen=True, slots=True)
class ModelCallControlResolutionResult:
    disposition_event: ModelCallControlDispositionResolvedEvent
    accepted_permit: ModelCallControlPermit | None
    publication: "ModelCallControlDispositionPublicationResult"


@dataclass(frozen=True, slots=True)
class _ConfirmedControlDispositionBatch:
    disposition_event: ModelCallControlDispositionResolvedEvent
    reducer_high_waters: Mapping[str, int]
    reconciliation_required: bool
    reducer_error_count: int
    caller_cancellation: BaseException | None = None


@dataclass(frozen=True, slots=True)
class ModelCallControlDispositionFoldResult:
    disposition_event: ModelCallControlDispositionResolvedEvent
    disposition_event_id: str
    disposition_sequence: int
    committed_payload_fingerprint: str
    reducer_state_fingerprint: str
    fold_fingerprint: str


@dataclass(frozen=True, slots=True)
class ModelCallControlDispositionPublicationResult:
    status: Literal["published", "observer_failed", "pending_retry"]
    disposition_event_id: str
    disposition_sequence: int
    diagnostic_code: str | None


class SessionModelCallControlDispositionOwner:
    """RuntimeSession owner for stable candidates and durable winners."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._pending: dict[str, ModelCallControlDispositionResolvedEvent] = {}
        self._winners: dict[str, ModelCallControlResolutionResult] = {}

    def get_or_register_candidate(
        self,
        candidate: ModelCallControlDispositionResolvedEvent,
    ) -> ModelCallControlDispositionResolvedEvent:
        call_id = candidate.resolved_model_call_id
        with self._lock:
            winner = self._winners.get(call_id)
            if winner is not None:
                if not _same_disposition_candidate(
                    winner.disposition_event,
                    candidate,
                ):
                    raise ModelCallControlResolutionError(
                        "session disposition winner conflicts with candidate"
                    )
                return winner.disposition_event
            current = self._pending.get(call_id)
            if current is not None:
                if not _same_disposition_candidate(current, candidate):
                    raise ModelCallControlResolutionError(
                        "session disposition candidate identity drifted"
                    )
                return current
            self._pending[call_id] = candidate
            return candidate

    def winner_for(
        self,
        resolved_model_call_id: str,
    ) -> ModelCallControlResolutionResult | None:
        with self._lock:
            return self._winners.get(resolved_model_call_id)

    def adopt_winner(self, winner: ModelCallControlResolutionResult) -> None:
        call_id = winner.disposition_event.resolved_model_call_id
        with self._lock:
            current = self._winners.get(call_id)
            if current is not None and not _same_disposition_candidate(
                current.disposition_event,
                winner.disposition_event,
            ):
                raise ModelCallControlResolutionError(
                    "session disposition winner identity drifted"
                )
            pending = self._pending.get(call_id)
            if pending is not None and not _same_disposition_candidate(
                pending,
                winner.disposition_event,
            ):
                raise ModelCallControlResolutionError(
                    "session disposition winner differs from pending candidate"
                )
            self._winners[call_id] = winner
            self._pending.pop(call_id, None)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
            self._winners.clear()

    @property
    def pending_candidate_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def pending_candidate_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._pending))

    @property
    def winner_count(self) -> int:
        with self._lock:
            return len(self._winners)

    @property
    def winner_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._winners))


class RuntimeSessionModelCallControlDispositionCommitPort:
    """Three-phase control writer: commit/confirm, fold, then publish."""

    def __init__(
        self,
        *,
        runtime_session: RuntimeSession,
        state: LoopState,
    ) -> None:
        self._runtime_session = runtime_session
        self._state = state

    async def commit_and_confirm_resolution(
        self,
        *,
        candidate: ModelCallControlDispositionResolvedEvent,
        deadline_monotonic: float,
    ) -> _ConfirmedControlDispositionBatch:
        def commit_or_confirm():
            try:
                return self._runtime_session.commit_reduce_events_from_thread(
                    (candidate,),
                    state=self._state,
                )
            except BaseException as original:
                try:
                    return self._runtime_session.confirm_and_reduce_event_batch(
                        (candidate,),
                        state=self._state,
                    )
                except BaseException as confirmation_error:
                    from pulsara_agent.runtime.session import EventCommitError

                    if isinstance(confirmation_error, EventCommitError):
                        raise original
                    self._runtime_session.latch_event_commit_outcome_unknown()
                    raise ModelCallControlResolutionError(
                        "model disposition commit outcome is structurally untrusted"
                    ) from confirmation_error

        caller_cancellation: BaseException | None = None
        try:
            write = await self._runtime_session.event_write_service.execute(
                commit_or_confirm,
                deadline_monotonic=deadline_monotonic,
            )
        except BaseException as cancelled:
            from pulsara_agent.runtime.event_write_service import (
                RuntimeEventWriteCancelled,
            )
            from pulsara_agent.runtime.session import EventCommitError, EventWriteResult

            if not isinstance(cancelled, RuntimeEventWriteCancelled):
                raise
            if isinstance(cancelled.operation_result, EventWriteResult):
                write = cancelled.operation_result
                caller_cancellation = cancelled
            else:
                operation_error = cancelled.operation_error
                if not (
                    isinstance(operation_error, EventCommitError)
                    and operation_error.commit_outcome == "none"
                ):
                    self._runtime_session.latch_event_commit_outcome_unknown()
                raise
        stored = write.committed_events
        if len(stored) != 1 or not isinstance(
            stored[0], ModelCallControlDispositionResolvedEvent
        ):
            raise ModelCallControlResolutionError(
                "model disposition commit did not return its canonical event"
            )
        return _ConfirmedControlDispositionBatch(
            disposition_event=stored[0],
            reducer_high_waters=write.reducer_high_waters,
            reconciliation_required=write.reconciliation_required,
            reducer_error_count=len(write.reducer_errors),
            caller_cancellation=caller_cancellation,
        )

    def fold_confirmed_resolution(
        self,
        *,
        confirmed: _ConfirmedControlDispositionBatch,
    ) -> ModelCallControlDispositionFoldResult:
        event = confirmed.disposition_event
        if (
            confirmed.reconciliation_required
            or confirmed.reducer_error_count
            or event.sequence is None
        ):
            raise ModelCallControlResolutionError(
                "model disposition committed but reducer state is untrusted"
            )
        reducer_payload = tuple(sorted(confirmed.reducer_high_waters.items()))
        reducer_fingerprint = sha256_fingerprint(
            "model-call-control-reducer-state:v1",
            reducer_payload,
        )
        payload = {
            "disposition_event_id": event.id,
            "disposition_sequence": event.sequence,
            "committed_payload_fingerprint": event.event_fingerprint,
            "reducer_state_fingerprint": reducer_fingerprint,
        }
        return ModelCallControlDispositionFoldResult(
            disposition_event=event,
            **payload,
            fold_fingerprint=sha256_fingerprint(
                "model-call-control-fold:v1",
                payload,
            ),
        )

    async def publish_folded_resolution(
        self,
        *,
        folded: ModelCallControlDispositionFoldResult,
    ) -> ModelCallControlDispositionPublicationResult:
        try:
            status = self._runtime_session.publish_committed_through_from_thread(
                through_sequence=folded.disposition_sequence,
                state=self._state,
            )
        except BaseException:
            status = "unavailable"
        return ModelCallControlDispositionPublicationResult(
            status="published" if status != "unavailable" else "pending_retry",
            disposition_event_id=folded.disposition_event.id,
            disposition_sequence=folded.disposition_sequence,
            diagnostic_code=(
                None
                if status != "unavailable"
                else "model_control_publication_pending_retry"
            ),
        )


class RunModelCallControlOwner:
    """Exact live-segment owner shared by Host stop and Agent control gates."""

    def __init__(
        self,
        *,
        run_id: str,
        activation: RunExecutionActivationFact,
        segment_id: str,
        segment_generation: int,
    ) -> None:
        if activation.segment_generation != segment_generation:
            raise ValueError("model control owner activation generation mismatch")
        self.run_id = run_id
        self.activation = activation
        self.segment_id = segment_id
        self.segment_generation = segment_generation
        self._lock = asyncio.Lock()
        self._termination_intent: RunTerminationIntentAttributionFact | None = None
        self._pending_candidates: dict[
            str, ModelCallControlDispositionResolvedEvent
        ] = {}
        self._winners: dict[str, ModelCallControlResolutionResult] = {}
        self._active = True

    async def install_termination_intent(
        self,
        intent: RunTerminationIntentAttributionFact,
    ) -> Literal["installed", "joined"]:
        async with self._lock:
            if (
                intent.target_run_execution_activation_fingerprint
                != self.activation.activation_fingerprint
            ):
                raise ValueError("termination intent targets another activation")
            current = self._termination_intent
            if current is not None:
                if current != intent:
                    raise ModelCallControlResolutionError(
                        "model control owner already has another termination intent"
                    )
                return "joined"
            self._termination_intent = intent
            return "installed"

    async def resolve_completed_call(
        self,
        *,
        result: CommittedModelCallResult,
        model_call_index: int,
        event_context: EventContext,
        runtime_session: RuntimeSession,
        state: LoopState,
    ) -> ModelCallControlResolutionResult:
        if result.control_disposition is not ModelCallResultControlDisposition.SUCCESS_ELIGIBLE:
            raise ModelCallControlResolutionError(
                "only a success-eligible model result has a live disposition"
            )
        if result.terminal_outcome != "completed":
            raise ModelCallControlResolutionError(
                "only a completed model result has a live disposition"
            )
        if model_call_index < 1:
            raise ValueError("model call index must be positive")

        # Bind the publisher loop before the control lock.  The lock protects
        # only durable commit, reducer fold, and permit installation; ordered
        # publication is deliberately deferred until after the lock is released.
        runtime_session.publisher.bind_running_loop()
        attribution_deadline = monotonic() + 30.0
        raw_attribution = await runtime_session.context_input_io_service.execute(
            operation_name="model-control-attribution-read",
            operation=lambda: runtime_session.event_log.read_raw_events_by_id(
                (
                    result.model_call_start_event_id,
                    result.model_call_end_event_id,
                ),
                deadline_monotonic=attribution_deadline,
            ),
            deadline_monotonic=attribution_deadline,
        )
        attribution_events = tuple(
            item.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            for item in raw_attribution
        )
        starts = tuple(
            event
            for event in attribution_events
            if isinstance(event, ModelCallStartEvent)
        )
        ends = tuple(
            event
            for event in attribution_events
            if isinstance(event, ModelCallEndEvent)
        )
        commit_port = RuntimeSessionModelCallControlDispositionCommitPort(
            runtime_session=runtime_session,
            state=state,
        )
        pending_publication: ModelCallControlResolutionResult | None = None
        folded: ModelCallControlDispositionFoldResult | None = None
        async with self._lock:
            if not self._active:
                raise ModelCallControlResolutionError(
                    "model control owner is no longer active"
                )
            self._validate_durable_result_attribution(
                result=result,
                model_call_index=model_call_index,
                event_context=event_context,
                starts=starts,
                ends=ends,
            )
            winner = self._winners.get(result.resolved_model_call_id)
            if winner is not None:
                if (
                    winner.disposition_event.source_result_fingerprint
                    != result.result_fingerprint
                ):
                    raise ModelCallControlResolutionError(
                        "existing model disposition belongs to another result"
                    )
                return winner

            candidate = self._pending_candidates.get(result.resolved_model_call_id)
            if candidate is None:
                candidate = self._build_candidate(
                    result=result,
                    model_call_index=model_call_index,
                    event_context=event_context,
                    metadata=runtime_session.default_event_metadata,
                )
                self._pending_candidates[result.resolved_model_call_id] = candidate
            candidate = (
                runtime_session.model_call_control_disposition_owner
                .get_or_register_candidate(candidate)
            )

            commit_deadline = monotonic() + 30.0
            confirmed = None
            last_commit_error: Exception | None = None
            for _attempt in range(_CONTROL_DISPOSITION_COMMIT_ATTEMPTS):
                try:
                    confirmed = await commit_port.commit_and_confirm_resolution(
                        candidate=candidate,
                        deadline_monotonic=commit_deadline,
                    )
                    break
                except Exception as exc:
                    if (
                        getattr(exc, "commit_outcome", None) != "none"
                        or runtime_session.reconciliation_required
                        or monotonic() >= commit_deadline
                    ):
                        raise
                    last_commit_error = exc
            if confirmed is None:
                raise ModelCallControlResolutionError(
                    "stable model disposition remained uncommitted after bounded retry"
                ) from last_commit_error
            folded = commit_port.fold_confirmed_resolution(confirmed=confirmed)
            durable = folded.disposition_event
            permit = (
                self._build_permit(durable)
                if durable.disposition is ModelCallControlDisposition.ACCEPTED
                else None
            )
            pending_publication = ModelCallControlResolutionResult(
                disposition_event=durable,
                accepted_permit=permit,
                publication=ModelCallControlDispositionPublicationResult(
                    status="pending_retry",
                    disposition_event_id=durable.id,
                    disposition_sequence=folded.disposition_sequence,
                    diagnostic_code="model_control_publication_not_started",
                ),
            )
            runtime_session.model_call_control_disposition_owner.adopt_winner(
                pending_publication
            )
            self._winners[result.resolved_model_call_id] = pending_publication
            self._pending_candidates.pop(result.resolved_model_call_id, None)

        assert pending_publication is not None and folded is not None
        publication = await commit_port.publish_folded_resolution(folded=folded)
        resolved = replace(pending_publication, publication=publication)
        async with self._lock:
            current = self._winners.get(result.resolved_model_call_id)
            if current == pending_publication:
                self._winners[result.resolved_model_call_id] = resolved
            else:
                resolved = current or resolved
        runtime_session.model_call_control_disposition_owner.adopt_winner(resolved)
        if confirmed.caller_cancellation is not None:
            raise confirmed.caller_cancellation
        return resolved

    def _validate_durable_result_attribution(
        self,
        *,
        result: CommittedModelCallResult,
        model_call_index: int,
        event_context: EventContext,
        starts: tuple[ModelCallStartEvent, ...],
        ends: tuple[ModelCallEndEvent, ...],
    ) -> None:
        if event_context.run_id != self.run_id:
            raise ModelCallControlResolutionError(
                "model disposition event context belongs to another run"
            )
        if (
            len(starts) != 1
            or len(ends) != 1
            or starts[0].id != result.model_call_start_event_id
            or ends[0].id != result.model_call_end_event_id
        ):
            raise ModelCallControlResolutionError(
                "model result lacks its exact durable Start/End pair"
            )
        start = starts[0]
        end = ends[0]
        if (
            start.turn_id != event_context.turn_id
            or start.reply_id != event_context.reply_id
            or end.run_id != event_context.run_id
            or end.turn_id != event_context.turn_id
            or end.reply_id != event_context.reply_id
            or start.model_call_index != model_call_index
            or start.resolved_call.resolved_model_call_id
            != result.resolved_model_call_id
            or end.resolved_model_call_id != result.resolved_model_call_id
            or start.sequence != result.model_call_start_sequence
            or end.sequence != result.model_call_end_sequence
            or end.outcome != "completed"
            or result.terminal_outcome != "completed"
            or start.recovery_plan.lifecycle_kind != "main_assistant_reply"
            or start.recovery_plan.run_execution_activation != self.activation
        ):
            raise ModelCallControlResolutionError(
                "model disposition result/start/end/activation attribution mismatch"
            )

    async def permit_is_active(self, permit: ModelCallControlPermit) -> bool:
        async with self._lock:
            if not self._active or self._termination_intent is not None:
                return False
            if (
                permit.segment_id != self.segment_id
                or permit.segment_generation != self.segment_generation
                or permit.run_execution_activation_fingerprint
                != self.activation.activation_fingerprint
            ):
                return False
            winner = self._winners.get(permit.resolved_model_call_id)
            return (
                winner is not None
                and winner.accepted_permit == permit
                and winner.disposition_event.disposition
                is ModelCallControlDisposition.ACCEPTED
            )

    async def retire(self) -> None:
        async with self._lock:
            self._active = False

    def _build_candidate(
        self,
        *,
        result: CommittedModelCallResult,
        model_call_index: int,
        event_context: EventContext,
        metadata: Mapping[str, object],
    ) -> ModelCallControlDispositionResolvedEvent:
        intent = self._termination_intent
        disposition = (
            ModelCallControlDisposition.ACCEPTED
            if intent is None
            else ModelCallControlDisposition.SUPPRESSED_BY_TERMINATION
        )
        return build_model_call_control_disposition_event(
            result=result,
            model_call_index=model_call_index,
            event_context=event_context,
            activation=self.activation,
            disposition=disposition,
            termination_intent=intent,
            recovery_reason_code=None,
            metadata=metadata,
        )

    def _build_permit(
        self,
        event: ModelCallControlDispositionResolvedEvent,
    ) -> ModelCallControlPermit:
        payload = {
            "disposition_event_id": event.id,
            "resolved_model_call_id": event.resolved_model_call_id,
            "source_result_fingerprint": event.source_result_fingerprint,
            "run_execution_activation_fingerprint": (
                event.run_execution_activation.activation_fingerprint
            ),
            "segment_id": self.segment_id,
            "segment_generation": self.segment_generation,
        }
        return ModelCallControlPermit(
            **payload,
            permit_fingerprint=sha256_fingerprint(
                "model-call-control-permit:v1", payload
            ),
        )


__all__ = [
    "ModelCallControlDispositionFoldResult",
    "ModelCallControlDispositionPublicationResult",
    "ModelCallControlPermit",
    "ModelCallControlResolutionError",
    "ModelCallControlResolutionResult",
    "RunModelCallControlOwner",
    "RuntimeSessionModelCallControlDispositionCommitPort",
    "SessionModelCallControlDispositionOwner",
    "build_model_call_control_disposition_event",
    "model_call_control_disposition_event_id",
]


def _same_disposition_candidate(
    left: ModelCallControlDispositionResolvedEvent,
    right: ModelCallControlDispositionResolvedEvent,
) -> bool:
    return left.model_copy(update={"sequence": None}) == right.model_copy(
        update={"sequence": None}
    )
