"""Restart recovery for completed main model calls without a disposition."""

from __future__ import annotations

import time
from dataclasses import dataclass

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ModelCallControlDispositionResolvedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
)
from pulsara_agent.event_log import (
    EventIdConflict,
    EventLog,
    EventLogWriteConflict,
    InMemoryEventLog,
)
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.llm.control import build_model_call_control_disposition_event
from pulsara_agent.llm.control_contract import (
    MODEL_CALL_CONTROL_DOWNSTREAM_BINDINGS,
    ModelCallControlDownstreamContractError,
)
from pulsara_agent.llm.materialize import (
    ModelStreamMaterializationError,
    materialize_committed_model_call_result_from_terminal_projection,
)
from pulsara_agent.llm.terminal_projection import hydrate_terminal_projection_text
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives.model_call import ModelCallControlDisposition
from pulsara_agent.runtime.authority_materialization import (
    MaterializationAccountCommitFailed,
    MaterializationAccountReconciliationRequired,
    commit_quiescent_accounted_batch,
)


class ModelCallControlRecoveryError(RuntimeError):
    """Base class for restart control-resolution failures."""


class ModelCallControlRecoveryStructuralError(ModelCallControlRecoveryError):
    """The durable control history cannot be repaired without guessing."""


@dataclass(frozen=True, slots=True)
class RecoveredModelCallControlDisposition:
    resolved_model_call_id: str
    disposition_event_id: str
    disposition: ModelCallControlDisposition
    source: str


@dataclass(frozen=True, slots=True)
class ModelCallControlRecoveryReport:
    through_sequence_before: int
    through_sequence_after: int
    recovered: tuple[RecoveredModelCallControlDisposition, ...]


class ModelCallControlDispositionRecoveryService:
    """Close completed main-call control gaps during a quiescent reopen."""

    def __init__(
        self,
        *,
        event_log: EventLog,
        archive: ArtifactStore,
        allow_unbootstrapped_test_events: bool = False,
    ) -> None:
        if allow_unbootstrapped_test_events and not isinstance(
            event_log, InMemoryEventLog
        ):
            raise ValueError(
                "unbootstrapped control recovery is restricted to the in-memory "
                "pytest event log"
            )
        self._event_log = event_log
        self._archive = archive
        self._allow_unbootstrapped_test_events = allow_unbootstrapped_test_events

    def repair_missing_dispositions(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> ModelCallControlRecoveryReport:
        initial_high_water, _ = self._read_snapshot(deadline_monotonic)
        recovered: list[RecoveredModelCallControlDisposition] = []

        while True:
            self._check_deadline(deadline_monotonic)
            high_water, events = self._read_snapshot(deadline_monotonic)
            candidates = _completed_main_starts(events)
            unresolved = False
            for start in candidates:
                result = _materialize(
                    events=events,
                    start=start,
                    event_log=self._event_log,
                    archive=self._archive,
                    deadline_monotonic=deadline_monotonic,
                )
                dispositions = _matching_dispositions(events=events, start=start)
                downstream = _matching_downstream(
                    events=events,
                    start=start,
                    result=result,
                )
                if dispositions:
                    winner = _validate_existing_winner(
                        start=start,
                        result=result,
                        dispositions=dispositions,
                        downstream=downstream,
                    )
                    if not any(
                        item.resolved_model_call_id
                        == result.resolved_model_call_id
                        for item in recovered
                    ):
                        recovered.append(
                            RecoveredModelCallControlDisposition(
                                resolved_model_call_id=(
                                    result.resolved_model_call_id
                                ),
                                disposition_event_id=winner.id,
                                disposition=winner.disposition,
                                source="existing_winner",
                            )
                        )
                    continue
                if downstream[1]:
                    raise ModelCallControlRecoveryStructuralError(
                        "completed model call has downstream control facts "
                        "without a prior disposition"
                    )

                unresolved = True
                candidate = build_model_call_control_disposition_event(
                    result=result,
                    model_call_index=_model_call_index(start),
                    event_context=EventContext(
                        run_id=start.run_id,
                        turn_id=start.turn_id,
                        reply_id=start.reply_id,
                    ),
                    activation=_activation(start),
                    disposition=(
                        ModelCallControlDisposition.SUPPRESSED_BY_RECOVERY
                    ),
                    termination_intent=None,
                    recovery_reason_code=(
                        "process_restarted_before_control_resolution"
                    ),
                    created_at=_model_end(events=events, start=start).created_at,
                    metadata=start.metadata,
                )
                try:
                    stored = self._commit_candidate(
                        candidate=candidate,
                        high_water=high_water,
                        deadline_monotonic=deadline_monotonic,
                    )
                except (EventLogWriteConflict, MaterializationAccountCommitFailed):
                    break
                except EventIdConflict as exc:
                    raise ModelCallControlRecoveryStructuralError(
                        "model control recovery event identity conflicts"
                    ) from exc
                if not stored or stored[0].id != candidate.id:
                    raise ModelCallControlRecoveryStructuralError(
                        "model control recovery confirmed another winner"
                    )
                recovered.append(
                    RecoveredModelCallControlDisposition(
                        resolved_model_call_id=result.resolved_model_call_id,
                        disposition_event_id=candidate.id,
                        disposition=candidate.disposition,
                        source="recovered_suppression",
                    )
                )
                break
            else:
                if not unresolved:
                    final_high_water, _ = self._read_snapshot(deadline_monotonic)
                    return ModelCallControlRecoveryReport(
                        through_sequence_before=initial_high_water,
                        through_sequence_after=final_high_water,
                        recovered=tuple(recovered),
                    )
            # A CAS-stale snapshot or one newly committed recovery winner is
            # always re-read before another decision.
            continue

    def _commit_candidate(
        self,
        *,
        candidate: ModelCallControlDispositionResolvedEvent,
        high_water: int,
        deadline_monotonic: float | None,
    ) -> tuple[AgentEvent, ...]:
        account = self._event_log.read_materialization_account_state(
            deadline_monotonic=deadline_monotonic
        )
        if account is not None:
            try:
                return commit_quiescent_accounted_batch(
                    event_log=self._event_log,
                    business_events=(candidate,),
                    owner_scope="model-control-recovery",
                    deadline_monotonic=deadline_monotonic,
                )
            except MaterializationAccountReconciliationRequired as exc:
                raise ModelCallControlRecoveryStructuralError(
                    "model control recovery account outcome is unknown"
                ) from exc
        if not self._allow_unbootstrapped_test_events:
            raise ModelCallControlRecoveryStructuralError(
                "model control recovery ledger is missing its materialization account"
            )
        try:
            return tuple(
                self._event_log.extend(
                    (candidate,),
                    expected_last_sequence=high_water,
                )
            )
        except BaseException:
            confirmation = self._event_log.confirm_batch((candidate,))
            if confirmation.missing_event_ids:
                if confirmation.committed_events:
                    raise ModelCallControlRecoveryStructuralError(
                        "model control recovery committed partially"
                    )
                raise
            return confirmation.committed_events

    def _read_snapshot(
        self, deadline_monotonic: float | None
    ) -> tuple[int, tuple[AgentEvent, ...]]:
        self._check_deadline(deadline_monotonic)
        high_water = self._event_log.next_sequence() - 1  # type: ignore[attr-defined]
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
            raise ModelCallControlRecoveryStructuralError(
                "model control recovery requires one contiguous ledger prefix"
            )
        return high_water, events

    @staticmethod
    def _check_deadline(deadline_monotonic: float | None) -> None:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise TimeoutError("model control recovery exceeded its deadline")


def _completed_main_starts(
    events: tuple[AgentEvent, ...],
) -> tuple[ModelCallStartEvent, ...]:
    starts = tuple(
        event
        for event in events
        if isinstance(event, ModelCallStartEvent)
        and event.recovery_plan.lifecycle_kind == "main_assistant_reply"
    )
    identities = tuple(
        event.resolved_call.resolved_model_call_id for event in starts
    )
    if len(identities) != len(set(identities)):
        raise ModelCallControlRecoveryStructuralError(
            "model control recovery found duplicate Start identity"
        )
    output = []
    for start in starts:
        end = _model_end(events=events, start=start)
        if end.outcome == "completed":
            output.append(start)
    return tuple(sorted(output, key=_sequence))


def _model_end(
    *, events: tuple[AgentEvent, ...], start: ModelCallStartEvent
) -> ModelCallEndEvent:
    call_id = start.resolved_call.resolved_model_call_id
    ends = tuple(
        event
        for event in events
        if isinstance(event, ModelCallEndEvent)
        and event.resolved_model_call_id == call_id
    )
    if len(ends) != 1:
        raise ModelCallControlRecoveryStructuralError(
            "model control recovery requires one terminal ModelEnd"
        )
    end = ends[0]
    if end.id != start.recovery_plan.stable_model_call_end_event_id:
        raise ModelCallControlRecoveryStructuralError(
            "model control recovery ModelEnd identity mismatch"
        )
    return end


def _materialize(
    *,
    events: tuple[AgentEvent, ...],
    start: ModelCallStartEvent,
    event_log: EventLog,
    archive: ArtifactStore,
    deadline_monotonic: float | None,
):
    try:
        end = _model_end(events=events, start=start)
        reference = end.terminal_projection.projection_reference
        document_text = archive.get_text(
            reference.document_artifact_id,
            session_id=event_log.runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        document = hydrate_terminal_projection_text(reference, document_text)
        return materialize_committed_model_call_result_from_terminal_projection(
            events,
            resolved_model_call_id=start.resolved_call.resolved_model_call_id,
            runtime_session_id=event_log.runtime_session_id,
            document=document,
        )
    except ModelStreamMaterializationError as exc:
        raise ModelCallControlRecoveryStructuralError(
            "model control recovery could not materialize the committed result"
        ) from exc


def _matching_dispositions(
    *, events: tuple[AgentEvent, ...], start: ModelCallStartEvent
) -> tuple[ModelCallControlDispositionResolvedEvent, ...]:
    call_id = start.resolved_call.resolved_model_call_id
    return tuple(
        event
        for event in events
        if isinstance(event, ModelCallControlDispositionResolvedEvent)
        and event.resolved_model_call_id == call_id
    )


def _matching_downstream(*, events, start, result):
    contract = start.recovery_plan.control_downstream_predicate_contract
    if contract is None:
        raise ModelCallControlRecoveryStructuralError(
            "main model Start lacks its downstream predicate contract"
        )
    try:
        binding = MODEL_CALL_CONTROL_DOWNSTREAM_BINDINGS.resolve(contract)
    except ModelCallControlDownstreamContractError as exc:
        raise ModelCallControlRecoveryStructuralError(str(exc)) from exc
    end_sequence = result.model_call_end_sequence
    tool_call_ids = frozenset(item.tool_call_id for item in result.tool_calls)
    matched = []
    for event in events:
        sequence = event.sequence or 0
        if event.run_id != start.run_id or sequence <= end_sequence:
            continue
        predicate = binding.match(
            event,
            result_tool_call_ids=tool_call_ids,
        )
        if predicate is not None:
            matched.append((event, predicate))
    return binding, tuple(matched)


def _validate_existing_winner(*, start, result, dispositions, downstream):
    if len(dispositions) != 1:
        raise ModelCallControlRecoveryStructuralError(
            "model call has multiple durable control dispositions"
        )
    winner = dispositions[0]
    if (
        winner.run_id != start.run_id
        or winner.turn_id != start.turn_id
        or winner.reply_id != start.reply_id
        or winner.model_call_start_event_id != start.id
        or winner.model_call_end_event_id != result.model_call_end_event_id
        or winner.model_call_index != _model_call_index(start)
        or winner.source_result_fingerprint != result.result_fingerprint
        or winner.run_execution_activation != _activation(start)
        or _sequence(winner) <= result.model_call_end_sequence
    ):
        raise ModelCallControlRecoveryStructuralError(
            "model control disposition attribution mismatch"
        )
    binding, matched = downstream
    for event, predicate in matched:
        if _sequence(winner) >= _sequence(event):
            raise ModelCallControlRecoveryStructuralError(
                "model control downstream fact precedes its disposition"
            )
        allowed = binding.allowed_dispositions(predicate)
        if winner.disposition not in allowed:
            raise ModelCallControlRecoveryStructuralError(
                "model control downstream fact contradicts its disposition"
            )
    return winner


def _activation(start: ModelCallStartEvent):
    activation = start.recovery_plan.run_execution_activation
    if activation is None:
        raise ModelCallControlRecoveryStructuralError(
            "main model Start lacks execution activation"
        )
    return activation


def _model_call_index(start: ModelCallStartEvent) -> int:
    if start.model_call_index is None or start.model_call_index < 1:
        raise ModelCallControlRecoveryStructuralError(
            "main model Start lacks a positive model_call_index"
        )
    return start.model_call_index


def _sequence(event: AgentEvent) -> int:
    if event.sequence is None or event.sequence < 1:
        raise ModelCallControlRecoveryStructuralError(
            "model control recovery requires committed event sequence"
        )
    return event.sequence


__all__ = [
    "ModelCallControlDispositionRecoveryService",
    "ModelCallControlRecoveryError",
    "ModelCallControlRecoveryReport",
    "ModelCallControlRecoveryStructuralError",
    "RecoveredModelCallControlDisposition",
]
