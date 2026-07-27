from __future__ import annotations

import asyncio
from dataclasses import asdict
from time import monotonic

import pytest

from pulsara_agent.event import EventContext
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.mcp import (
    McpInvocationOwner,
    McpPendingHandleState,
    McpToolCompletedOutcome,
    McpToolRejectCode,
    McpToolRejectedOutcome,
    McpToolSuspendedOutcome,
    build_mcp_tool_execution_request,
    build_mcp_tool_resume_request,
)
from pulsara_agent.ports.tool_execution import (
    ToolExecutionCandidateConfirmationKind,
    ToolExecutionNonePolicy,
    ToolExecutionStableCandidateCommitReceipt,
    ToolExecutionStableCandidateKind,
    ToolExecutionStableCandidateOwnerIdentity,
)
from pulsara_agent.ports.tool_registry import (
    McpToolBindingContract,
    ToolBindingOrigin,
    build_tool_binding_contract,
)
from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    context_fingerprint,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.mcp import (
    McpBindingIdentityFact,
    thaw_mcp_json_value,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    McpInputRequiredSuspensionFact,
    prepare_mcp_input_required_resolution,
)
from pulsara_agent.runtime.mcp.tool_execution_port import RuntimeMcpToolExecutionPort
from pulsara_agent.runtime.mcp.supervisor import McpDrainError
from pulsara_agent.runtime.mcp.types import (
    McpBindingIdentity,
    McpContentArtifact,
    McpInputRequestDTO,
    McpInputRequired,
    McpManagerLease,
    McpOriginalRequest,
    McpPendingLeaseReservation,
    McpRequestSourceMethod,
    McpToolResult,
)


CTX = EventContext(
    run_id="run:mcp-port",
    turn_id="turn:mcp-port",
    reply_id="reply:mcp-port",
)


class _Manager:
    def __init__(
        self,
        call_result,
        *,
        resume_error: Exception | None = None,
        resume_result=None,
    ) -> None:
        self.call_result = call_result
        self.resume_error = resume_error
        self.resume_result = resume_result
        self.resume_count = 0

    async def call_tool(self, *_args, **_kwargs):
        return self.call_result

    async def resume_suspended_request(self, **_kwargs):
        self.resume_count += 1
        if self.resume_error is not None:
            raise self.resume_error
        return (
            self.resume_result
            if self.resume_result is not None
            else McpToolResult(output="resumed")
        )


class _BlockingCallManager(_Manager):
    def __init__(self) -> None:
        super().__init__(McpToolResult(output="unused"))
        self.started = asyncio.Event()

    async def call_tool(self, *_args, **_kwargs):
        self.started.set()
        await asyncio.Event().wait()


class _BlockingResumeManager(_Manager):
    def __init__(self, call_result) -> None:
        super().__init__(call_result)
        self.resume_started = asyncio.Event()

    async def resume_suspended_request(self, **_kwargs):
        self.resume_count += 1
        self.resume_started.set()
        await asyncio.Event().wait()


class _Supervisor:
    def __init__(self, manager: _Manager, binding: McpBindingIdentity) -> None:
        self.manager = manager
        self.binding = binding
        self.lease = McpManagerLease(
            lease_id="mcp_lease:test",
            slot_id=binding.slot_id,
            binding_identity=binding,
        )
        self.release_count = 0
        self.return_borrow_count = 0
        self.confirmed = False
        self.completed = False

    def acquire_binding_lease(self, identity):
        assert identity == self.binding
        return self.lease

    def manager_for_lease(self, lease):
        assert lease == self.lease
        return self.manager

    def release_lease(self, lease):
        assert lease == self.lease
        self.release_count += 1

    def promote_lease_to_pending(self, lease, interaction_id):
        assert lease == self.lease
        return McpPendingLeaseReservation(
            reservation_id="mcp_pending_lease:test",
            interaction_id=interaction_id,
            binding_identity=self.binding,
        )

    def confirm_pending_lease(self, interaction_id, reservation_id):
        assert interaction_id == "mcp_input_required:test"
        assert reservation_id == "mcp_pending_lease:test"
        self.confirmed = True

    def abort_pending_lease(self, interaction_id, reservation_id):
        del interaction_id, reservation_id
        self.release_count += 1

    def borrow_pending_lease(self, interaction_id, identity):
        assert self.confirmed
        assert interaction_id == "mcp_input_required:test"
        assert identity == self.binding
        return self.lease

    def return_pending_borrow(self, interaction_id):
        assert interaction_id == "mcp_input_required:test"
        self.return_borrow_count += 1

    def complete_pending_lease(self, interaction_id):
        assert interaction_id == "mcp_input_required:test"
        self.completed = True


def _binding_fact() -> McpBindingIdentityFact:
    return McpBindingIdentityFact(
        server_id="docs",
        slot_id="mcp_slot:docs",
        snapshot_id="mcp_snapshot:docs",
        discovery_generation=1,
    )


def _runtime_binding() -> McpBindingIdentity:
    value = _binding_fact()
    return McpBindingIdentity(
        server_id=value.server_id,
        slot_id=value.slot_id,
        snapshot_id=value.snapshot_id,
        discovery_generation=value.discovery_generation,
    )


def _binding_contract() -> McpToolBindingContract:
    value = build_tool_binding_contract(
        tool_name="mcp__docs__lookup",
        origin=ToolBindingOrigin.MCP,
        contract_id="pulsara.mcp.docs.lookup",
        contract_version="v1",
        binding_attributes=_binding_fact().model_dump(mode="json"),
        mcp_binding_identity=_binding_fact(),
        original_tool_name="lookup",
    )
    assert isinstance(value, McpToolBindingContract)
    return value


def _owner() -> McpInvocationOwner:
    return McpInvocationOwner(
        runtime_session_id="runtime:mcp-port",
        run_id=CTX.run_id,
        tool_call_id="call:mcp-port",
        event_context=CTX,
    )


def _request():
    return build_mcp_tool_execution_request(
        owner=_owner(),
        exposed_tool_name="mcp__docs__lookup",
        original_tool_name="lookup",
        binding=_binding_contract(),
        arguments={"query": {"terms": ["one"]}},
        timeout_ms=5_000,
    )


def _candidate_owner(handle) -> ToolExecutionStableCandidateOwnerIdentity:
    payload = {
        "registry_instance_id": "tool_execution_registry:mcp-port",
        "owner_id": "tool_terminal_owner:mcp-port",
        "owner_generation": 1,
        "runtime_session_id": "runtime:mcp-port",
        "run_id": CTX.run_id,
        "tool_call_id": "call:mcp-port",
        "rollout_reservation_id": "rollout_reservation:mcp-port",
        "rollout_reservation_fingerprint": "sha256:" + "4" * 64,
        "candidate_kind": ToolExecutionStableCandidateKind.SUSPENSION,
        "none_policy": ToolExecutionNonePolicy.ABANDON_ON_NONE,
        "ordered_candidate_event_ids": ("tool_execution_suspended:mcp-port",),
        "candidate_batch_fingerprint": "sha256:" + "5" * 64,
        "physical_owner_kind": "mcp_pending",
        "physical_owner_identity_fingerprint": handle.identity.identity_fingerprint,
    }
    return ToolExecutionStableCandidateOwnerIdentity(
        **payload,
        identity_fingerprint=context_fingerprint(
            "tool-execution-stable-candidate-owner:v1", payload
        ),
    )


def _full_receipt(owner) -> ToolExecutionStableCandidateCommitReceipt:
    payload = {
        "owner_identity": asdict(owner),
        "confirmation_kind": ToolExecutionCandidateConfirmationKind.FULL.value,
        "write_attempt_generation": 1,
        "committed_event_references": (),
        "publication_summary": "completed",
        "retry_scheduled": False,
        "reconciliation_required": False,
    }
    return ToolExecutionStableCandidateCommitReceipt(
        owner_identity=owner,
        confirmation_kind=ToolExecutionCandidateConfirmationKind.FULL,
        write_attempt_generation=1,
        committed_event_references=(),
        publication_summary="completed",
        retry_scheduled=False,
        reconciliation_required=False,
        receipt_fingerprint=context_fingerprint(
            "tool-execution-stable-candidate-commit-receipt:v1", payload
        ),
    )


def _input_required() -> McpInputRequired:
    return McpInputRequired(
        interaction_id="mcp_input_required:test",
        server_id="docs",
        protocol_version="2026-07-26",
        request_state="opaque-state",
        input_requests=(
            McpInputRequestDTO(
                key="token",
                method="elicitation/create",
                params={"message": "token"},
            ),
        ),
        original_request=McpOriginalRequest(
            source_method=McpRequestSourceMethod.TOOL_CALL,
            tool_name="lookup",
            arguments={"query": "x"},
        ),
    )


def _confirm_pending(port, handle) -> None:
    candidate_owner = _candidate_owner(handle)
    port.bind_suspension_candidate(
        pending_handle=handle,
        candidate_owner_identity=candidate_owner,
    )
    port.confirm_suspension_commit(
        pending_handle=handle,
        commit_receipt=_full_receipt(candidate_owner),
    )


def _resume_request(handle, *, responses=None):
    view = handle.suspension_commit_view
    suspension = build_frozen_fact(
        McpInputRequiredSuspensionFact,
        schema_version="mcp_input_required_suspension.v1",
        interaction=view.interaction,
        binding_identity=view.binding_identity,
        pending_lease_reservation=view.pending_lease_reservation,
        request_envelope=view.request_envelope,
        rollout_reservation_id="rollout_reservation:mcp-port",
        rollout_reservation_fingerprint="sha256:" + "4" * 64,
        source_mcp_installation_id="mcp_installation:mcp-port",
        durable_deadline_utc=None,
        deadline_policy_fingerprint=context_fingerprint(
            "mcp-deadline-policy:v1", "test"
        ),
        predecessor_resolution_submitted_event_reference=None,
    )
    source_reference = ContextEventReferenceFact(
        runtime_session_id="runtime:mcp-port",
        event_id="tool_execution_suspended:mcp-port",
        sequence=1,
        event_type="tool_execution_suspended",
        payload_fingerprint="sha256:" + "6" * 64,
    )
    resolution = prepare_mcp_input_required_resolution(
        source_suspension_event_reference=source_reference,
        source_suspension_fact_fingerprint=suspension.suspension_fact_fingerprint,
        interaction_id="mcp_input_required:test",
        responses=(responses or {"token": {"value": "secret"}}),
        cancelled=False,
    )
    return build_mcp_tool_resume_request(
        owner=_owner(),
        pending_handle=handle,
        binding=_binding_contract(),
        source_suspension_event_reference=source_reference,
        source_suspension=suspension,
        prepared_resolution=resolution,
        timeout_ms=5_000,
    )


def test_completed_application_error_preserves_metadata_and_deep_freezes_it() -> None:
    source_metadata = {"nested": {"items": [1, 2]}}
    result = McpToolResult(
        output="application error",
        is_error=True,
        metadata=source_metadata,
        artifacts=(
            McpContentArtifact(
                role="evidence",
                media_type="application/json",
                text="{}",
                metadata={"labels": ["a"]},
            ),
        ),
    )
    supervisor = _Supervisor(_Manager(result), _runtime_binding())
    port = RuntimeMcpToolExecutionPort(supervisor)  # type: ignore[arg-type]

    outcome = asyncio.run(port.execute(_request()))

    assert isinstance(outcome, McpToolCompletedOutcome)
    assert outcome.result_state is ToolResultState.ERROR
    assert outcome.normalized_is_error is True
    assert outcome.normalized_output == "application error"
    assert supervisor.release_count == 1
    source_metadata["nested"]["items"].append(3)
    assert thaw_mcp_json_value(outcome.normalized_metadata)["nested"] == {
        "items": [1, 2]
    }
    assert thaw_mcp_json_value(outcome.artifact_candidates[0].metadata) == {
        "labels": ["a"]
    }


def test_resume_returns_active_borrow_in_finally_and_retains_pending_owner() -> None:
    input_required = McpInputRequired(
        interaction_id="mcp_input_required:test",
        server_id="docs",
        protocol_version="2026-07-26",
        request_state="opaque-state",
        input_requests=(
            McpInputRequestDTO(
                key="token",
                method="elicitation/create",
                params={"message": "token"},
            ),
        ),
        original_request=McpOriginalRequest(
            source_method=McpRequestSourceMethod.TOOL_CALL,
            tool_name="lookup",
            arguments={"query": "x"},
        ),
    )
    supervisor = _Supervisor(
        _Manager(input_required, resume_error=RuntimeError("resume failed")),
        _runtime_binding(),
    )
    port = RuntimeMcpToolExecutionPort(supervisor)  # type: ignore[arg-type]
    initial = asyncio.run(port.execute(_request()))
    assert isinstance(initial, McpToolSuspendedOutcome)
    handle = initial.pending_handle
    candidate_owner = _candidate_owner(handle)
    port.bind_suspension_candidate(
        pending_handle=handle,
        candidate_owner_identity=candidate_owner,
    )
    port.confirm_suspension_commit(
        pending_handle=handle,
        commit_receipt=_full_receipt(candidate_owner),
    )
    assert handle.state is McpPendingHandleState.PENDING_CONFIRMED

    view = handle.suspension_commit_view
    suspension = build_frozen_fact(
        McpInputRequiredSuspensionFact,
        schema_version="mcp_input_required_suspension.v1",
        interaction=view.interaction,
        binding_identity=view.binding_identity,
        pending_lease_reservation=view.pending_lease_reservation,
        request_envelope=view.request_envelope,
        rollout_reservation_id="rollout_reservation:mcp-port",
        rollout_reservation_fingerprint="sha256:" + "4" * 64,
        source_mcp_installation_id="mcp_installation:mcp-port",
        durable_deadline_utc=None,
        deadline_policy_fingerprint=context_fingerprint(
            "mcp-deadline-policy:v1", "test"
        ),
        predecessor_resolution_submitted_event_reference=None,
    )
    source_reference = ContextEventReferenceFact(
        runtime_session_id="runtime:mcp-port",
        event_id="tool_execution_suspended:mcp-port",
        sequence=1,
        event_type="tool_execution_suspended",
        payload_fingerprint="sha256:" + "6" * 64,
    )
    resolution = prepare_mcp_input_required_resolution(
        source_suspension_event_reference=source_reference,
        source_suspension_fact_fingerprint=suspension.suspension_fact_fingerprint,
        interaction_id="mcp_input_required:test",
        responses={"token": {"value": "secret"}},
        cancelled=False,
    )
    resume_request = build_mcp_tool_resume_request(
        owner=_owner(),
        pending_handle=handle,
        binding=_binding_contract(),
        source_suspension_event_reference=source_reference,
        source_suspension=suspension,
        prepared_resolution=resolution,
        timeout_ms=5_000,
    )

    outcome = asyncio.run(port.resume(resume_request))

    assert isinstance(outcome, McpToolRejectedOutcome)
    assert supervisor.return_borrow_count == 1
    assert handle.state is McpPendingHandleState.PENDING_CONFIRMED


def test_execute_cancellation_releases_ordinary_lease_and_drains_port() -> None:
    async def scenario() -> None:
        manager = _BlockingCallManager()
        supervisor = _Supervisor(manager, _runtime_binding())
        port = RuntimeMcpToolExecutionPort(supervisor)  # type: ignore[arg-type]
        task = asyncio.create_task(port.execute(_request()))
        await asyncio.wait_for(manager.started.wait(), timeout=1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert supervisor.release_count == 1
        await port.stop_admission_and_drain(deadline_monotonic=monotonic() + 1)
        with pytest.raises(RuntimeError, match="admission is closed"):
            await port.execute(_request())

    asyncio.run(scenario())


def test_resume_cancellation_returns_borrow_and_restores_pending_handle() -> None:
    async def scenario() -> None:
        manager = _BlockingResumeManager(_input_required())
        supervisor = _Supervisor(manager, _runtime_binding())
        port = RuntimeMcpToolExecutionPort(supervisor)  # type: ignore[arg-type]
        initial = await port.execute(_request())
        assert isinstance(initial, McpToolSuspendedOutcome)
        handle = initial.pending_handle
        _confirm_pending(port, handle)

        task = asyncio.create_task(port.resume(_resume_request(handle)))
        await asyncio.wait_for(manager.resume_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert supervisor.return_borrow_count == 1
        assert handle.state is McpPendingHandleState.PENDING_CONFIRMED
        with pytest.raises(McpDrainError):
            await port.stop_admission_and_drain(deadline_monotonic=monotonic() + 0.01)

    asyncio.run(scenario())


def test_terminal_lowering_failure_freezes_non_retryable_result_without_reexecution() -> (
    None
):
    manager = _Manager(
        _input_required(),
        resume_result=McpToolResult(
            output="provider completed",
            metadata={"bad": object()},
        ),
    )
    supervisor = _Supervisor(manager, _runtime_binding())
    port = RuntimeMcpToolExecutionPort(supervisor)  # type: ignore[arg-type]
    initial = asyncio.run(port.execute(_request()))
    assert isinstance(initial, McpToolSuspendedOutcome)
    handle = initial.pending_handle
    _confirm_pending(port, handle)
    request = _resume_request(handle)

    first = asyncio.run(port.resume(request))
    second = asyncio.run(port.resume(request))
    mismatched = asyncio.run(
        port.resume(
            _resume_request(handle, responses={"token": {"value": "different"}})
        )
    )

    assert isinstance(first, McpToolRejectedOutcome)
    assert first.error_code is McpToolRejectCode.RESULT_LOWERING_FAILED
    assert first.retryable_in_same_live_owner is False
    assert second is first
    assert isinstance(mismatched, McpToolRejectedOutcome)
    assert mismatched.error_code is McpToolRejectCode.RESOLUTION_IDENTITY_MISMATCH
    assert manager.resume_count == 1
    assert supervisor.return_borrow_count == 1
    assert handle.state is McpPendingHandleState.TERMINAL_RESULT_FROZEN


def test_successor_lowering_failure_terminalizes_predecessor_without_reexecution() -> (
    None
):
    malformed_successor = McpInputRequired(
        interaction_id="mcp_input_required:test",
        server_id="docs",
        protocol_version="2026-07-26",
        request_state="opaque-state-2",
        input_requests=(
            McpInputRequestDTO(
                key="confirmation",
                method="elicitation/create",
                params={"bad": object()},
            ),
        ),
        original_request=McpOriginalRequest(
            source_method=McpRequestSourceMethod.TOOL_CALL,
            tool_name="lookup",
            arguments={"query": "x"},
        ),
        round_count=2,
    )
    manager = _Manager(_input_required(), resume_result=malformed_successor)
    supervisor = _Supervisor(manager, _runtime_binding())
    port = RuntimeMcpToolExecutionPort(supervisor)  # type: ignore[arg-type]
    initial = asyncio.run(port.execute(_request()))
    assert isinstance(initial, McpToolSuspendedOutcome)
    handle = initial.pending_handle
    _confirm_pending(port, handle)
    request = _resume_request(handle)

    first = asyncio.run(port.resume(request))
    second = asyncio.run(port.resume(request))

    assert isinstance(first, McpToolRejectedOutcome)
    assert first.error_code is McpToolRejectCode.RESULT_LOWERING_FAILED
    assert second is first
    assert manager.resume_count == 1
    assert supervisor.return_borrow_count == 1
    assert handle.state is McpPendingHandleState.TERMINAL_RESULT_FROZEN
    assert port.handle_for_interaction("mcp_input_required:test") is handle


def test_successor_result_is_frozen_before_atomic_handle_replacement() -> None:
    successor_result = McpInputRequired(
        interaction_id="mcp_input_required:test",
        server_id="docs",
        protocol_version="2026-07-26",
        request_state="opaque-state-2",
        input_requests=(
            McpInputRequestDTO(
                key="confirmation",
                method="elicitation/create",
                params={"message": "continue"},
            ),
        ),
        original_request=McpOriginalRequest(
            source_method=McpRequestSourceMethod.TOOL_CALL,
            tool_name="lookup",
            arguments={"query": "x"},
        ),
        round_count=2,
    )
    manager = _Manager(_input_required(), resume_result=successor_result)
    supervisor = _Supervisor(manager, _runtime_binding())
    port = RuntimeMcpToolExecutionPort(supervisor)  # type: ignore[arg-type]
    initial = asyncio.run(port.execute(_request()))
    assert isinstance(initial, McpToolSuspendedOutcome)
    predecessor = initial.pending_handle
    _confirm_pending(port, predecessor)

    outcome = asyncio.run(port.resume(_resume_request(predecessor)))

    assert isinstance(outcome, McpToolSuspendedOutcome)
    successor = outcome.pending_handle
    assert manager.resume_count == 1
    assert supervisor.return_borrow_count == 1
    assert predecessor.state is McpPendingHandleState.COMPLETED
    assert successor.state is McpPendingHandleState.PENDING_CONFIRMED
    assert successor.identity.predecessor_handle_id == predecessor.identity.handle_id
    assert successor.identity.handle_generation == 2
    assert port.handle_for_interaction("mcp_input_required:test") is successor
