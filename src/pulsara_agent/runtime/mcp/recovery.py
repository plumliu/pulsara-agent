"""Typed recovery owner for an MCP suspension whose process lease is gone."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    McpInputRequiredInteractionClosedEvent,
    RolloutBudgetReservationSettledEvent,
    ToolExecutionSuspendedEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
)
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.message import ToolResultState
from pulsara_agent.primitives.authority_materialization import (
    LedgerWriteAdmissionClass,
    PhysicalOperationKind,
)
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.runtime_event_vocabulary import (
    McpInputRequiredTerminalSourceFact,
    stable_runtime_event_id,
)
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.tool_loop import build_tool_result_error_events


@dataclass(frozen=True, slots=True)
class RecoveredMcpInputRequiredClosure:
    interaction_id: str
    run_id: str
    closure_event_reference: ContextEventReferenceFact
    committed_events: tuple[AgentEvent, ...]


async def terminalize_reopened_mcp_input_required(
    runtime_session: RuntimeSession,
    *,
    run_id: str,
    closure_reason: str,
    deadline_monotonic: float,
) -> RecoveredMcpInputRequiredClosure:
    """Close one active MCP lifecycle without recreating its provider lease."""

    if closure_reason not in {
        "session_reopen_lease_unavailable",
        "child_pending_unsupported",
    }:
        raise ValueError("reopen MCP recovery received an invalid closure reason")
    records = runtime_session.mcp_input_required_lifecycle_store.active_for_run(
        run_id
    )
    if len(records) != 1:
        raise RuntimeError(
            "MCP lifecycle recovery requires one exact active interaction"
        )
    record = records[0]
    source_event = runtime_session.event_log.get_by_id(
        record.source_suspension_event_reference.event_id
    )
    if not isinstance(source_event, ToolExecutionSuspendedEvent):
        raise RuntimeError("MCP lifecycle recovery lost its typed suspension")
    if (
        event_reference_from_stored(
            source_event,
            runtime_session_id=runtime_session.runtime_session_id,
        )
        != record.source_suspension_event_reference
    ):
        raise RuntimeError("MCP lifecycle recovery suspension reference drifted")
    interaction = source_event.suspension.interaction
    if (
        source_event.run_id != run_id
        or interaction.interaction_id != record.interaction_id
        or interaction.tool_call_id != record.tool_call_id
        or interaction.tool_name != record.tool_name
    ):
        raise RuntimeError("MCP lifecycle recovery source identity drifted")

    source = build_frozen_fact(
        McpInputRequiredTerminalSourceFact,
        schema_version="mcp_input_required_terminal_source.v1",
        source_suspension_event_reference=(
            record.source_suspension_event_reference
        ),
        source_resolution_submitted_event_reference=(
            record.latest_resolution_submitted_event_reference
        ),
    )
    existing_starts = tuple(
        event
        for event in runtime_session.event_log.iter(run_id=run_id)
        if isinstance(event, ToolResultStartEvent)
        and event.tool_call_id == record.tool_call_id
    )
    if len(existing_starts) > 1:
        raise RuntimeError("MCP lifecycle recovery found duplicate ToolResult starts")
    candidates = tuple(
        build_tool_result_error_events(
            EventContext(
                run_id=record.run_id,
                turn_id=record.turn_id,
                reply_id=record.reply_id,
            ),
            tool_call_id=record.tool_call_id,
            tool_call_name=record.tool_name,
            message=(
                "MCP input-required interaction was interrupted because its "
                "original process lease is no longer available."
            ),
            state=ToolResultState.INTERRUPTED,
            existing_start=existing_starts[0] if existing_starts else None,
            mcp_input_required_terminal_source=source,
        )
    )
    prepared = (
        await runtime_session.tool_terminal_projection_service.prepare_batch(
            candidates,
            deadline_monotonic=deadline_monotonic,
        )
    )
    terminal = next(
        event for event in prepared if isinstance(event, ToolResultEndEvent)
    )
    resume_failed_reference = (
        record.latest_resume_failed_event_reference
        if closure_reason == "session_reopen_lease_unavailable"
        else None
    )
    closure = McpInputRequiredInteractionClosedEvent(
        id=stable_runtime_event_id(
            "mcp-input-required-interaction-closed-event:v1",
            record.source_suspension_event_reference.event_id,
            (
                record.latest_resolution_submitted_event_reference.event_id
                if record.latest_resolution_submitted_event_reference is not None
                else None
            ),
            (
                resume_failed_reference.event_id
                if resume_failed_reference is not None
                else None
            ),
            closure_reason,
            terminal.id,
        ),
        run_id=record.run_id,
        turn_id=record.turn_id,
        reply_id=record.reply_id,
        source_suspension_event_reference=(
            record.source_suspension_event_reference
        ),
        source_resolution_submitted_event_reference=(
            record.latest_resolution_submitted_event_reference
        ),
        source_resume_failed_event_reference=resume_failed_reference,
        closure_reason=closure_reason,
        terminal_tool_result_event_identity=stable_event_identity(
            terminal,
            runtime_session_id=runtime_session.runtime_session_id,
        ),
    )
    ordered = list(prepared)
    ordered.insert(ordered.index(terminal) + 1, closure)

    rollout_reservation = _rollout_reservation(
        runtime_session,
        reservation_id=source_event.suspension.rollout_reservation_id,
        reservation_fingerprint=(
            source_event.suspension.rollout_reservation_fingerprint
        ),
        tool_call_id=record.tool_call_id,
    )
    rollout_settlement = RolloutBudgetReservationSettledEvent(
        id=(
            "rollout_budget_reservation_settled:"
            f"{rollout_reservation.reservation_id}"
        ),
        run_id=record.run_id,
        turn_id=record.turn_id,
        reply_id=record.reply_id,
        reservation_id=rollout_reservation.reservation_id,
        charged_milliunits=rollout_reservation.reserved_milliunits,
        usage_status="tool_terminal",
        usage_charge=None,
        source_model_call_end_event_id=None,
        source_tool_result_event_id=terminal.id,
        child_usage_handoff=None,
    )
    ordered.append(rollout_settlement)
    physical_reservation = runtime_session.physical_reservation_for_owner(
        operation_kind=PhysicalOperationKind.TOOL_CALL,
        owner_id=record.tool_call_id,
    )
    if physical_reservation is None:
        raise RuntimeError(
            "MCP lifecycle recovery lacks its physical tool reservation"
        )
    result = await runtime_session.event_write_service.execute(
        lambda: runtime_session.settle_physical_operation_from_thread(
            tuple(ordered),
            reservation=physical_reservation,
            terminal_outcome="interrupted",
            state=None,
        ),
        deadline_monotonic=deadline_monotonic,
        admission_class=LedgerWriteAdmissionClass.OPERATION_CONTINUATION,
        operation_owner_id=(
            runtime_session.physical_operation_admission_owner_id(
                operation_kind=PhysicalOperationKind.TOOL_CALL,
                owner_id=record.tool_call_id,
            )
        ),
    )
    stored_closure = next(
        (
            event
            for event in result.committed_events
            if isinstance(event, McpInputRequiredInteractionClosedEvent)
        ),
        None,
    )
    if stored_closure is None:
        raise RuntimeError("MCP lifecycle recovery did not commit its closure")
    return RecoveredMcpInputRequiredClosure(
        interaction_id=record.interaction_id,
        run_id=run_id,
        closure_event_reference=event_reference_from_stored(
            stored_closure,
            runtime_session_id=runtime_session.runtime_session_id,
        ),
        committed_events=tuple(result.committed_events),
    )


def _rollout_reservation(
    runtime_session: RuntimeSession,
    *,
    reservation_id: str,
    reservation_fingerprint: str,
    tool_call_id: str,
):
    matches = tuple(
        reservation
        for state in runtime_session.long_horizon_state_store.rollout_states()
        for reservation in state.active_reservations
        if reservation.reservation_id == reservation_id
    )
    if len(matches) != 1:
        raise RuntimeError(
            "MCP lifecycle recovery requires one rollout reservation"
        )
    reservation = matches[0]
    if (
        reservation.semantic_fingerprint != reservation_fingerprint
        or reservation.owner_kind != "tool_call"
        or reservation.owner_id != tool_call_id
    ):
        raise RuntimeError("MCP lifecycle recovery rollout reservation drifted")
    return reservation


__all__ = [
    "RecoveredMcpInputRequiredClosure",
    "terminalize_reopened_mcp_input_required",
]
