"""Dormant RunOwner reconstruction for process reopen and terminal repair."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    RunEndEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
)
from pulsara_agent.event_log.protocol import RawStoredEventEnvelope, same_event_payload
from pulsara_agent.runtime.run_entry import (
    CommittedHostRunEntry,
    CommittedRunEntry,
    CommittedSubagentRunEntry,
)
from pulsara_agent.runtime.run_execution.authority import (
    awaiting_initial_revision,
    installed_authority_head,
    materialize_continuation_revision,
    materialize_initial_revision,
    materialize_run_genesis,
)
from pulsara_agent.runtime.run_execution.owner import (
    NoActiveActivation,
    NoActiveSuspension,
    RunFinalizationOwner,
    RunFinalizationSlot,
    RunObserverRegistry,
    RunOwner,
    RunProgressState,
    RunRetiringResourceSet,
    UnboundRunResources,
)
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored


def materialize_dormant_run_owner(
    *,
    events: Sequence[AgentEvent],
    run_start_envelope: RawStoredEventEnvelope,
) -> RunOwner:
    """Fold one non-terminal run without reviving old physical resources."""

    ordered = tuple(sorted(events, key=_stored_sequence))
    starts = tuple(event for event in ordered if isinstance(event, RunStartEvent))
    if len(starts) != 1:
        raise ValueError("recovered run requires one exact RunStart")
    if any(isinstance(event, RunEndEvent) for event in ordered):
        raise ValueError("terminal run cannot be rebuilt as a dormant owner")
    run_start = starts[0]
    if any(event.run_id != run_start.run_id for event in ordered):
        raise ValueError("recovered run event slice crosses run identity")
    genesis = materialize_run_genesis(
        run_start,
        stored_envelope=run_start_envelope,
    )
    authority_head = awaiting_initial_revision(genesis)
    latest_resume: RunInteractionResumeBoundaryEvent | None = None
    boundaries = tuple(
        event
        for event in ordered
        if isinstance(event, RunInteractionResumeBoundaryEvent)
    )
    exposures = tuple(
        event for event in ordered if isinstance(event, CapabilityExposureResolvedEvent)
    )
    if exposures:
        revisions = tuple(event.exposure_revision for event in exposures)
        if revisions != tuple(range(1, len(exposures) + 1)):
            raise ValueError("recovered authority revisions are not contiguous")
        revision = materialize_initial_revision(
            genesis=genesis,
            stored_exposure=exposures[0],
        )
        authority_head = installed_authority_head(revision)
        for exposure in exposures[1:]:
            matches = tuple(
                boundary
                for boundary in boundaries
                if boundary.sequence is not None
                and boundary.sequence == _stored_sequence(exposure) + 1
                and boundary.boundary.effective_exposure_id
                == exposure.exposure.exposure_id
            )
            if len(matches) != 1:
                raise ValueError(
                    "continuation exposure lacks one exact resume boundary: "
                    f"exposure={exposure.exposure.exposure_id!r}, "
                    "boundaries="
                    f"{tuple(item.boundary.effective_exposure_id for item in boundaries)!r}"
                )
            latest_resume = matches[0]
            revision = materialize_continuation_revision(
                predecessor=revision,
                stored_boundary=latest_resume,
                stored_exposure=exposure,
                effective_model_target=genesis.run_model_target,
                effective_permission=genesis.permission_snapshot,
                runtime_session_id=genesis.owner_identity.runtime_session_id,
            )
            authority_head = installed_authority_head(revision)

    committed = _committed_entry(run_start, ordered)
    reason = (
        "reopen_continuation_rebind_pending"
        if latest_resume is not None
        else "reopen_initial_rebind_pending"
    )
    latest_kind = (
        "host_resume_boundary"
        if latest_resume is not None
        else (
            "host_run_boundary"
            if run_start.new_run_boundary is not None
            else "subagent_run_start"
        )
    )
    latest_owner_id = (
        latest_resume.id
        if latest_resume is not None
        else (
            run_start.new_run_boundary.identity.boundary_id
            if run_start.new_run_boundary is not None
            else run_start.id
        )
    )
    max_generation = max(
        (
            activation.segment_generation
            for event in ordered
            if (activation := getattr(event, "run_execution_activation", None))
            is not None
        ),
        default=0,
    )
    finalization = RunFinalizationOwner(
        owner_identity=genesis.owner_identity,
        terminal_event_id=run_start.terminal_run_end_event_id,
    )
    return RunOwner(
        identity=genesis.owner_identity,
        genesis=genesis,
        authority_head=authority_head,
        progress=RunProgressState(owner_identity=genesis.owner_identity),
        lifecycle="initializing",
        resource_slot=UnboundRunResources(reason=reason),
        retiring_resources=RunRetiringResourceSet(
            owner_identity=genesis.owner_identity
        ),
        activation_slot=NoActiveActivation(),
        suspension_slot=NoActiveSuspension(),
        finalization_slot=RunFinalizationSlot(
            state="empty",
            owner=finalization,
        ),
        observer_registry=RunObserverRegistry(),
        activation_completion_history={},
        run_completion=asyncio.get_running_loop().create_future(),
        entry=committed,
        termination_intent=None,
        next_segment_generation=max_generation,
        latest_activation_owner_kind=latest_kind,
        latest_activation_owner_id=latest_owner_id,
    )


def freeze_recovered_terminal_batch(
    owner: RunOwner,
    candidates: Sequence[AgentEvent],
) -> None:
    """Install the exact recovered terminal candidate on its stable owner."""

    finalization = owner.finalization_slot.owner
    if not isinstance(finalization, RunFinalizationOwner):
        raise RuntimeError("recovered run lacks a finalization owner")
    run_ends = tuple(event for event in candidates if isinstance(event, RunEndEvent))
    if (
        len(run_ends) != 1
        or run_ends[0].id != finalization.terminal_event_id
        or run_ends[0].run_id != owner.identity.run_id
    ):
        raise ValueError("recovered terminal batch has an invalid RunEnd")
    finalization.candidate_generation += 1
    finalization.terminal_candidates = tuple(candidates)
    finalization.run_end_candidate = run_ends[0]
    finalization.state = "candidate_frozen"
    owner.finalization_slot.state = "active"
    finalization.run_end_candidate = run_ends[0]
    finalization.commit_state = "candidate_frozen"
    owner.lifecycle = "terminalizing"


def confirm_recovered_terminal_batch(
    owner: RunOwner,
    stored_events: Sequence[AgentEvent],
) -> RunEndEvent:
    """Advance the stable owner only after exact FULL terminal confirmation."""

    finalization = owner.finalization_slot.owner
    if not isinstance(finalization, RunFinalizationOwner):
        raise RuntimeError("recovered run lacks a finalization owner")
    candidates = finalization.terminal_candidates
    selected = tuple(
        event
        for event in stored_events
        if event.id in {candidate.id for candidate in candidates}
    )
    if len(selected) != len(candidates) or any(
        not same_event_payload(candidate, stored)
        for candidate, stored in zip(candidates, selected, strict=True)
    ):
        finalization.state = "reconciliation_required"
        owner.finalization_slot.state = "reconciliation_required"
        owner.lifecycle = "reconciliation_required"
        raise RuntimeError("recovered terminal batch exact confirmation failed")
    run_end = next(event for event in selected if isinstance(event, RunEndEvent))
    finalization.confirmed_run_end_event_reference = event_reference_from_stored(
        run_end,
        runtime_session_id=owner.identity.runtime_session_id,
    )
    finalization.commit_state = "confirmed"
    finalization.state = "full_output_pending"
    finalization.run_end_candidate = None
    finalization.terminal_candidates = ()
    owner.finalization_slot.state = "run_end_full_pending_output"
    owner.lifecycle = "terminal"
    return run_end


def _committed_entry(
    run_start: RunStartEvent,
    events: Sequence[AgentEvent],
) -> CommittedRunEntry:
    committed_through = max(_stored_sequence(event) for event in events)
    if run_start.new_run_boundary is not None:
        return CommittedHostRunEntry(
            run_start_event=run_start,
            run_start_sequence=_stored_sequence(run_start),
            committed_through_sequence=committed_through,
            publication_status="completed",
            boundary_id=run_start.new_run_boundary.identity.boundary_id,
            committed_audit_event_ids=(),
        )
    if run_start.subagent_run_entry is None:
        raise ValueError("recovered RunStart has no entry authority")
    return CommittedSubagentRunEntry(
        run_start_event=run_start,
        run_start_sequence=_stored_sequence(run_start),
        committed_through_sequence=committed_through,
        publication_status="completed",
        subagent_run_id=run_start.subagent_run_entry.subagent_run_id,
    )


def _stored_sequence(event: AgentEvent) -> int:
    if event.sequence is None:
        raise ValueError("run recovery requires stored events")
    return event.sequence


__all__ = [
    "confirm_recovered_terminal_batch",
    "freeze_recovered_terminal_batch",
    "materialize_dormant_run_owner",
]
