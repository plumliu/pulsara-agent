from __future__ import annotations

from dataclasses import asdict
from time import monotonic

import pytest

from tests.conftest import tool_result_end_contract_fields
from tests.support.runtime_session import in_memory_runtime_session
from tests.support.mcp import prepare_test_mcp_input_required_suspension

from pulsara_agent.event import (
    EventContext,
    ToolExecutionSuspendedEvent,
    ToolResultEndEvent,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import (
    ToolExecutionCandidateConfirmationKind,
    ToolExecutionNonePolicy,
    ToolExecutionPhysicalOwnerHandoffReceipt,
    ToolExecutionStableCandidateOwnerState,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.long_horizon import (
    RolloutBudgetBucket,
    RolloutPhase,
    RolloutReservationFact,
)
from pulsara_agent.primitives.mcp import McpBindingIdentityFact
from pulsara_agent.primitives.runtime_event_vocabulary import (
    McpInputRequiredSuspensionFact,
)
from pulsara_agent.runtime.session import EventBatchCommitOutcome, EventWriteResult
from pulsara_agent.runtime.tool_execution import ToolExecutionCommitContractError


CTX = EventContext(
    run_id="run:stable-candidate",
    turn_id="turn:stable-candidate",
    reply_id="reply:stable-candidate",
)


def _reservation(tool_call_id: str) -> RolloutReservationFact:
    payload = {
        "reservation_id": f"reservation:{tool_call_id}",
        "account_id": "rollout:stable-candidate",
        "owner_kind": "tool_call",
        "owner_id": tool_call_id,
        "phase_at_reservation": RolloutPhase.EXPLORATION,
        "budget_bucket": RolloutBudgetBucket.EXPLORATION,
        "reserved_milliunits": 1,
        "model_call_reservation_quote": None,
        "source_sequence": 0,
    }
    return RolloutReservationFact(
        **payload,
        semantic_fingerprint=context_fingerprint(
            "rollout-reservation:v1",
            payload,
        ),
    )


def _terminal(
    tool_call_id: str,
    *,
    event_id: str,
    state: ToolResultState = ToolResultState.SUCCESS,
) -> ToolResultEndEvent:
    return ToolResultEndEvent(
        id=event_id,
        created_at="2026-07-26T00:00:00Z",
        **CTX.event_fields(),
        tool_call_id=tool_call_id,
        state=state,
        **tool_result_end_contract_fields(
            tool_call_id,
            tool_name="probe",
            state=state,
            observed_at_utc="2026-07-26T00:00:00Z",
        ),
    )


def _suspension(
    tool_call_id: str,
    *,
    event_id: str,
    interaction_id: str,
    rollout_reservation: RolloutReservationFact,
) -> ToolExecutionSuspendedEvent:
    binding = McpBindingIdentityFact(
        server_id="docs",
        slot_id="slot:docs",
        snapshot_id="snapshot:docs",
        discovery_generation=1,
    )
    prepared = prepare_test_mcp_input_required_suspension(
        interaction_id=interaction_id,
        runtime_session_id="runtime:stable-candidate",
        run_id=CTX.run_id,
        turn_id=CTX.turn_id,
        reply_id=CTX.reply_id,
        tool_call_id=tool_call_id,
        tool_name="mcp__docs__lookup",
        server_id="docs",
        binding_identity=binding,
        pending_lease_reservation_id=f"lease:{interaction_id}",
        protocol_revision="2026-07-28",
        request_state=None,
    )
    suspension = build_frozen_fact(
        McpInputRequiredSuspensionFact,
        schema_version="mcp_input_required_suspension.v2",
        interaction=prepared.interaction,
        binding_identity=prepared.binding_identity,
        pending_lease_reservation=prepared.pending_lease_reservation,
        request_envelope=prepared.request_envelope,
        durable_continuation=prepared.continuation.durable_fact,
        rollout_reservation_id=rollout_reservation.reservation_id,
        rollout_reservation_fingerprint=(rollout_reservation.semantic_fingerprint),
        source_mcp_installation_id="mcp_installation:test",
        predecessor_resolution_submitted_event_reference=None,
    )
    return ToolExecutionSuspendedEvent(
        id=event_id,
        created_at="2026-07-26T00:00:00Z",
        **CTX.event_fields(),
        interaction_kind="mcp_input_required",
        tool_call_id=tool_call_id,
        tool_name="mcp__docs__lookup",
        suspension=suspension,
    )


def _full_outcome(event) -> EventBatchCommitOutcome:
    committed = event.model_copy(update={"sequence": 1})
    return EventBatchCommitOutcome(
        status="full",
        deadline_monotonic=monotonic() + 1.0,
        result=EventWriteResult(
            committed_events=(committed,),
            commit_status="committed",
            reducer_high_waters={},
            reconciliation_required=False,
            reducer_errors=(),
            publication_status="completed",
            publisher_enqueued_through_sequence=1,
        ),
    )


def _handoff(
    owner,
    receipt,
    *,
    physical_owner_fingerprint: str,
    disposition: str,
    exact_retry_required: bool = False,
    source_receipt_fingerprint: str | None = None,
) -> ToolExecutionPhysicalOwnerHandoffReceipt:
    source = source_receipt_fingerprint or receipt.receipt_fingerprint
    payload = {
        "candidate_owner_identity": asdict(owner),
        "source_commit_receipt_fingerprint": source,
        "physical_owner_kind": "mcp_pending",
        "physical_owner_identity_fingerprint": physical_owner_fingerprint,
        "handoff_generation": receipt.write_attempt_generation,
        "physical_disposition": disposition,
        "exact_retry_required": exact_retry_required,
        "reconciliation_required": False,
    }
    return ToolExecutionPhysicalOwnerHandoffReceipt(
        candidate_owner_identity=owner,
        source_commit_receipt_fingerprint=source,
        physical_owner_kind="mcp_pending",
        physical_owner_identity_fingerprint=physical_owner_fingerprint,
        handoff_generation=receipt.write_attempt_generation,
        physical_disposition=disposition,
        exact_retry_required=exact_retry_required,
        reconciliation_required=False,
        receipt_fingerprint=context_fingerprint(
            "tool-execution-physical-owner-handoff:v1",
            payload,
        ),
    )


def test_stable_candidate_rejects_same_id_with_different_committed_payload(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    registry = runtime.tool_execution_terminal_registry
    reservation = _reservation("call:payload")
    registry.install_admitted_batch(run_id=CTX.run_id, reservations=(reservation,))
    candidate = _terminal(
        reservation.owner_id,
        event_id="tool_result_end:payload",
    )
    owner = registry.freeze_terminal(
        run_id=CTX.run_id,
        reservation=reservation,
        candidates=(candidate,),
    )
    conflicting = _terminal(
        reservation.owner_id,
        event_id=candidate.id,
        state=ToolResultState.ERROR,
    )

    with pytest.raises(
        ToolExecutionCommitContractError,
        match="committed payload",
    ):
        registry.confirm_stable_candidate_write(
            owner_identity=owner,
            outcome=_full_outcome(conflicting),
        )


def test_stable_candidate_rejects_process_local_payload_replacement(tmp_path) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    registry = runtime.tool_execution_terminal_registry
    reservation = _reservation("call:mutation")
    installed = registry.install_admitted_batch(
        run_id=CTX.run_id,
        reservations=(reservation,),
    )[0]
    candidate = _terminal(
        reservation.owner_id,
        event_id="tool_result_end:mutation",
    )
    owner = registry.freeze_terminal(
        run_id=CTX.run_id,
        reservation=reservation,
        candidates=(candidate,),
    )
    installed.stable_candidates = (
        _terminal(
            reservation.owner_id,
            event_id=candidate.id,
            state=ToolResultState.ERROR,
        ),
    )

    with pytest.raises(
        ToolExecutionCommitContractError,
        match="payload changed after freeze",
    ):
        registry.confirm_stable_candidate_write(
            owner_identity=owner,
            outcome=EventBatchCommitOutcome(
                status="none",
                deadline_monotonic=monotonic() + 1.0,
            ),
        )


def test_successor_suspension_retries_same_candidate_and_handoff_exact_joins(
    tmp_path,
) -> None:
    runtime = in_memory_runtime_session(tmp_path)
    registry = runtime.tool_execution_terminal_registry
    reservation = _reservation("call:suspension")
    installed = registry.install_admitted_batch(
        run_id=CTX.run_id,
        reservations=(reservation,),
    )[0]
    initial = _suspension(
        reservation.owner_id,
        event_id="tool_execution_suspended:initial",
        interaction_id="interaction:initial",
        rollout_reservation=reservation,
    )
    initial_owner = registry.freeze_suspension(
        run_id=CTX.run_id,
        reservation=reservation,
        candidates=(initial,),
        physical_owner_identity_fingerprint="handle:initial",
    )
    assert initial_owner.none_policy is ToolExecutionNonePolicy.ABANDON_ON_NONE
    initial_receipt = registry.confirm_stable_candidate_write(
        owner_identity=initial_owner,
        outcome=_full_outcome(initial),
    )
    registry.accept_physical_owner_handoff(
        _handoff(
            initial_owner,
            initial_receipt,
            physical_owner_fingerprint="handle:initial",
            disposition="confirmed",
        )
    )
    assert installed.state is ToolExecutionStableCandidateOwnerState.SUSPENDED

    successor = _suspension(
        reservation.owner_id,
        event_id="tool_execution_suspended:successor",
        interaction_id="interaction:successor",
        rollout_reservation=reservation,
    )
    successor_owner = registry.freeze_suspension(
        run_id=CTX.run_id,
        reservation=reservation,
        candidates=(successor,),
        physical_owner_identity_fingerprint="handle:successor",
    )
    assert successor_owner.owner_generation == initial_owner.owner_generation + 1
    assert successor_owner.none_policy is ToolExecutionNonePolicy.RETRY_SAME_CANDIDATE
    retry_receipt = registry.confirm_stable_candidate_write(
        owner_identity=successor_owner,
        outcome=EventBatchCommitOutcome(
            status="none",
            deadline_monotonic=monotonic() + 1.0,
        ),
    )
    assert (
        retry_receipt.confirmation_kind is ToolExecutionCandidateConfirmationKind.NONE
    )
    assert retry_receipt.retry_scheduled is True

    forged_source = context_fingerprint("forged-receipt:v1", "wrong")
    with pytest.raises(
        ToolExecutionCommitContractError,
        match="handoff identity mismatch",
    ):
        registry.accept_physical_owner_handoff(
            _handoff(
                successor_owner,
                retry_receipt,
                physical_owner_fingerprint="handle:successor",
                disposition="retained",
                exact_retry_required=True,
                source_receipt_fingerprint=forged_source,
            )
        )

    registry.accept_physical_owner_handoff(
        _handoff(
            successor_owner,
            retry_receipt,
            physical_owner_fingerprint="handle:successor",
            disposition="retained",
            exact_retry_required=True,
        )
    )
    assert installed.state is ToolExecutionStableCandidateOwnerState.RETRY_WAIT
    assert installed.stable_candidates == (successor,)
