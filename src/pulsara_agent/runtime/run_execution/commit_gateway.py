"""Run-owned exact confirmation adapter over the RuntimeSession ledger.

This module is deliberately the only run-execution adapter that understands
generic event candidates.  Coordinators and owners consume only the closed
reconciliation confirmations returned here.
"""

from __future__ import annotations

from collections.abc import Sequence

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log import EventLog
from pulsara_agent.event_log.serialization import (
    decode_event_write_candidate,
    freeze_event_write_candidate,
)
from pulsara_agent.ports.event_write import FrozenEventWriteCandidate
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.ports.run_execution import (
    LedgerHorizonFact,
    ReconciliationConflictConfirmation,
    ReconciliationFullConfirmation,
    ReconciliationNoneConfirmation,
    ReconciliationUnresolvedConfirmation,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.context import ContextEventReferenceFact


def event_batch_candidate_identity(
    candidates: Sequence[AgentEvent],
) -> tuple[str, str]:
    """Return one stable ID/fingerprint for an ordered unsequenced batch."""

    if not candidates:
        raise ValueError("reconciliation event batch cannot be empty")
    frozen = tuple(
        freeze_event_write_candidate(candidate.model_copy(update={"sequence": None}))
        for candidate in candidates
    )
    event_ids = tuple(item.event_id for item in frozen)
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("reconciliation event batch contains duplicate IDs")
    fingerprint = context_fingerprint(
        "run-reconciliation-event-batch:v1",
        tuple(item.fingerprint_payload() for item in frozen),
    )
    return frozen[-1].event_id, fingerprint


def event_candidate_identity(candidate: AgentEvent) -> tuple[str, str]:
    """Return the stable ID and payload fingerprint without leaking a writer DTO."""

    frozen = freeze_event_write_candidate(
        candidate.model_copy(update={"sequence": None})
    )
    return frozen.event_id, frozen.payload_fingerprint


def rebind_confirmed_event_candidate(
    candidate: object,
    *,
    source_reference: ContextEventReferenceFact,
) -> AgentEvent:
    """Decode one confirmed process-local candidate behind the closed gateway."""

    if not isinstance(candidate, FrozenEventWriteCandidate):
        raise RuntimeError("pending interaction lost its confirmed source payload")
    source = decode_event_write_candidate(candidate).model_copy(
        update={"sequence": source_reference.sequence}
    )
    return source


def read_ledger_horizon(
    event_log: EventLog,
    *,
    through_sequence: int | None = None,
    deadline_monotonic: float | None = None,
) -> LedgerHorizonFact:
    prefix = event_log.read_raw_ledger_prefix(
        through_sequence=through_sequence,
        deadline_monotonic=deadline_monotonic,
    )
    payload = {
        "through_sequence": prefix.through_sequence,
        "continuity_accumulator": prefix.ledger_continuity_accumulator,
    }
    return LedgerHorizonFact(
        **payload,
        horizon_fingerprint=context_fingerprint(
            "run-ledger-horizon:v1",
            payload,
        ),
    )


def confirm_event_batch(
    event_log: EventLog,
    *,
    runtime_session_id: str,
    candidates: Sequence[AgentEvent],
    deadline_monotonic: float,
):
    """Classify one stable batch using a single ID-selection high-water."""

    candidate_id, candidate_fingerprint = event_batch_candidate_identity(candidates)
    frozen = tuple(
        freeze_event_write_candidate(candidate.model_copy(update={"sequence": None}))
        for candidate in candidates
    )
    event_ids = tuple(item.event_id for item in frozen)
    try:
        selected = event_log.read_raw_events_by_id_snapshot(
            event_ids,
            deadline_monotonic=deadline_monotonic,
        )
        horizon = read_ledger_horizon(
            event_log,
            through_sequence=selected.through_sequence,
            deadline_monotonic=deadline_monotonic,
        )
    except Exception:
        payload = {
            "disposition": "unresolved",
            "diagnostic_code": "ledger_confirmation_unavailable",
        }
        return ReconciliationUnresolvedConfirmation(
            **payload,
            confirmation_fingerprint=context_fingerprint(
                "run-reconciliation-confirmation:unresolved:v1",
                payload,
            ),
        )

    observed_by_id = {item.event_id: item for item in selected.events}
    if not observed_by_id:
        payload = {
            "disposition": "none",
            "observed_ledger_horizon": horizon,
        }
        return ReconciliationNoneConfirmation(
            **payload,
            confirmation_fingerprint=context_fingerprint(
                "run-reconciliation-confirmation:none:v1",
                payload,
            ),
        )

    references = tuple(
        _reference_from_raw(observed_by_id[event_id], runtime_session_id)
        for event_id in event_ids
        if event_id in observed_by_id
    )
    exact = len(observed_by_id) == len(frozen) and all(
        _stored_matches_candidate(observed_by_id[item.event_id], item)
        for item in frozen
    )
    if not exact:
        payload = {
            "disposition": "conflict",
            "conflicting_authority_references": references,
            "diagnostic_code": "stored_candidate_conflict",
        }
        return ReconciliationConflictConfirmation(
            **payload,
            confirmation_fingerprint=context_fingerprint(
                "run-reconciliation-confirmation:conflict:v1",
                payload,
            ),
        )

    payload = {
        "disposition": "full",
        "stored_candidate_id": candidate_id,
        "stored_candidate_fingerprint": candidate_fingerprint,
        "exact_event_references": references,
        "observed_ledger_horizon": horizon,
    }
    return ReconciliationFullConfirmation(
        **payload,
        confirmation_fingerprint=context_fingerprint(
            "run-reconciliation-confirmation:full:v1",
            payload,
        ),
    )


def _reference_from_raw(raw, runtime_session_id: str) -> ContextEventReferenceFact:
    if raw.runtime_session_id != runtime_session_id:
        raise RuntimeError("reconciliation selected a cross-ledger event")
    return ContextEventReferenceFact(
        runtime_session_id=runtime_session_id,
        event_id=raw.event_id,
        sequence=raw.sequence,
        event_type=raw.event_type,
        payload_fingerprint=raw.payload_fingerprint,
    )


def _stored_matches_candidate(raw, candidate) -> bool:
    if (
        raw.event_type != candidate.event_type
        or raw.event_schema_version != candidate.event_schema_version
        or raw.event_schema_fingerprint != candidate.event_schema_fingerprint
        or raw.event_domain_contract_fingerprint
        != candidate.event_domain_contract_fingerprint
    ):
        return False
    stored = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    unsequenced = freeze_event_write_candidate(
        stored.model_copy(update={"sequence": None})
    )
    return unsequenced == candidate


__all__ = [
    "confirm_event_batch",
    "event_batch_candidate_identity",
    "event_candidate_identity",
    "read_ledger_horizon",
    "rebind_confirmed_event_candidate",
]
