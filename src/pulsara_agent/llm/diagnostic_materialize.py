"""Diagnostic-only reconstruction of model results from raw stream events.

Production control, transcript, and recovery paths must consume the durable
terminal projection. Raw reconstruction is retained only for doctor probes and
contract comparison tests.
"""

from __future__ import annotations

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log import EventLog
from pulsara_agent.llm.materialize import (
    ModelStreamMaterializationError,
    _materialize_committed_model_call_result_from_raw_event_log,
    _materialize_committed_model_call_result_from_raw_events,
)
from pulsara_agent.primitives.model_call import CommittedModelCallResult


def materialize_committed_model_call_result(
    event_log: EventLog,
    *,
    resolved_model_call_id: str,
    deadline_monotonic: float | None = None,
) -> CommittedModelCallResult:
    return _materialize_committed_model_call_result_from_raw_event_log(
        event_log,
        resolved_model_call_id=resolved_model_call_id,
        deadline_monotonic=deadline_monotonic,
    )


def materialize_committed_model_call_result_from_events(
    events: tuple[AgentEvent, ...],
    *,
    resolved_model_call_id: str,
) -> CommittedModelCallResult:
    return _materialize_committed_model_call_result_from_raw_events(
        events,
        resolved_model_call_id=resolved_model_call_id,
    )


__all__ = [
    "ModelStreamMaterializationError",
    "materialize_committed_model_call_result",
    "materialize_committed_model_call_result_from_events",
]
