from __future__ import annotations

import asyncio
from dataclasses import asdict
from time import monotonic

import pytest

from pulsara_agent.event import EventContext
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.mcp import (
    McpDispatchReservationCommitGuard,
    McpInvocationOwner,
    McpPendingHandleState,
    McpToolCompletedOutcome,
    McpToolRejectCode,
    McpToolRejectedOutcome,
    McpToolSuspendedOutcome,
    build_mcp_tool_execution_request,
    build_mcp_tool_resume_request,
)
from pulsara_agent.ports.mcp_secret import McpElicitationAction
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
)
from pulsara_agent.runtime.mcp.continuation_store import (
    InMemoryMcpContinuationSecretStore,
    McpContinuationKeyProvider,
    McpContinuationSecretCodec,
)
from pulsara_agent.runtime.mcp.supervisor import McpDrainError
from pulsara_agent.runtime.mcp.tool_execution_port import RuntimeMcpToolExecutionPort
from pulsara_agent.runtime.mcp.types import (
    McpBindingIdentity,
    McpContentArtifact,
    McpManagerLease,
    McpPendingLeaseReservation,
    McpToolResult,
)
from tests.support.mcp import make_mcp_client_input_required


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
        return self.resume_result or McpToolResult(output="resumed")


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


def _codec() -> McpContinuationSecretCodec:
    return McpContinuationSecretCodec(
        McpContinuationKeyProvider.from_master_key(
            key_id="test-mcp-port-key",
            master_key=b"test-mcp-port-master-key" * 2,
        )
    )


def _port(supervisor: _Supervisor) -> RuntimeMcpToolExecutionPort:
    return RuntimeMcpToolExecutionPort(
        supervisor,  # type: ignore[arg-type]
        continuation_codec=_codec(),
        continuation_repository=InMemoryMcpContinuationSecretStore(),
    )


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
        contract_version="v2",
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
    event_id = handle.suspension_commit_view.suspension_event_id
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
        "ordered_candidate_event_ids": (event_id,),
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


def _suspension_and_reference(handle):
    view = handle.suspension_commit_view
    suspension = build_frozen_fact(
        McpInputRequiredSuspensionFact,
        schema_version="mcp_input_required_suspension.v2",
        interaction=view.interaction,
        binding_identity=view.binding_identity,
        pending_lease_reservation=view.pending_lease_reservation,
        request_envelope=view.request_envelope,
        durable_continuation=view.durable_continuation,
        rollout_reservation_id="rollout_reservation:mcp-port",
        rollout_reservation_fingerprint="sha256:" + "4" * 64,
        source_mcp_installation_id="mcp_installation:mcp-port",
        predecessor_resolution_submitted_event_reference=None,
    )
    reference = ContextEventReferenceFact(
        runtime_session_id="runtime:mcp-port",
        event_id=view.suspension_event_id,
        sequence=1,
        event_type="tool_execution_suspended",
        payload_fingerprint="sha256:" + "6" * 64,
    )
    return suspension, reference


def _prepare_resume(port, handle):
    handle.elicitation_batch_owner.submit_form(
        request_key="answer",
        action=McpElicitationAction.ACCEPT,
        content_present=True,
        content={"value": "secret"},
    )
    suspension, source_reference = _suspension_and_reference(handle)
    resolution = port.prepare_resolution(
        pending_handle=handle,
        source_suspension_event_reference=source_reference,
        source_suspension=suspension,
        attempt_ordinal=1,
        submitted_at_utc="2026-07-31T00:00:01Z",
    )
    port.confirm_resolution_commit(
        prepared_resolution=resolution,
        outcome="full",
    )
    resolution_ref = ContextEventReferenceFact(
        runtime_session_id="runtime:mcp-port",
        event_id=resolution.resolution_carrier.resolution_event_id,
        sequence=2,
        event_type="mcp_input_required_resolution_submitted",
        payload_fingerprint="sha256:" + "7" * 64,
    )
    physical_ref = ContextEventReferenceFact(
        runtime_session_id="runtime:mcp-port",
        event_id="physical_operation_reserved:mcp-port",
        sequence=3,
        event_type="physical_operation_reserved",
        payload_fingerprint="sha256:" + "8" * 64,
    )
    guard_payload = {
        "runtime_session_id": "runtime:mcp-port",
        "interaction_id": handle.identity.interaction_id,
        "tool_call_id": "call:mcp-port",
        "physical_operation_id": "physical:mcp-port",
        "physical_reservation_event_reference": physical_ref.model_dump(mode="json"),
        "physical_reservation_fingerprint": "sha256:" + "9" * 64,
        "guard_generation": 1,
    }
    guard = McpDispatchReservationCommitGuard(
        runtime_session_id="runtime:mcp-port",
        interaction_id=handle.identity.interaction_id,
        tool_call_id="call:mcp-port",
        physical_operation_id="physical:mcp-port",
        physical_reservation_event_reference=physical_ref,
        physical_reservation_fingerprint="sha256:" + "9" * 64,
        guard_generation=1,
        guard_fingerprint=context_fingerprint(
            "mcp-dispatch-reservation-commit-guard:v1", guard_payload
        ),
    )
    dispatch = port.prepare_dispatch(
        pending_handle=handle,
        prepared_resolution=resolution,
        source_resolution_event_reference=resolution_ref,
        commit_guard=guard,
    )
    receipt = port.confirm_dispatch_commit(
        pending_handle=handle,
        prepared_dispatch=dispatch,
        outcome="full",
    )
    assert receipt is not None
    return build_mcp_tool_resume_request(
        owner=_owner(),
        pending_handle=handle,
        binding=_binding_contract(),
        source_suspension_event_reference=source_reference,
        source_suspension=suspension,
        prepared_resolution=resolution,
        dispatch_receipt=receipt,
        timeout_ms=5_000,
    )


def _input_required(*, round_ordinal: int = 1):
    return make_mcp_client_input_required(
        interaction_id="mcp_input_required:test",
        round_ordinal=round_ordinal,
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
    outcome = asyncio.run(_port(supervisor).execute(_request()))

    assert isinstance(outcome, McpToolCompletedOutcome)
    assert outcome.result_state is ToolResultState.ERROR
    assert outcome.normalized_is_error is True
    assert supervisor.release_count == 1
    source_metadata["nested"]["items"].append(3)
    assert thaw_mcp_json_value(outcome.normalized_metadata)["nested"] == {
        "items": [1, 2]
    }


def test_execute_cancellation_releases_ordinary_lease_and_drains_port() -> None:
    async def scenario() -> None:
        manager = _BlockingCallManager()
        supervisor = _Supervisor(manager, _runtime_binding())
        port = _port(supervisor)
        task = asyncio.create_task(port.execute(_request()))
        await asyncio.wait_for(manager.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert supervisor.release_count == 1
        await port.stop_admission_and_drain(deadline_monotonic=monotonic() + 1)

    asyncio.run(scenario())


def test_resolution_none_rearms_the_exact_sealed_candidate() -> None:
    async def scenario() -> None:
        supervisor = _Supervisor(_Manager(_input_required()), _runtime_binding())
        port = _port(supervisor)
        outcome = await port.execute(_request())
        assert isinstance(outcome, McpToolSuspendedOutcome)
        handle = outcome.pending_handle
        _confirm_pending(port, handle)
        handle.elicitation_batch_owner.submit_form(
            request_key="answer",
            action=McpElicitationAction.ACCEPT,
            content_present=True,
            content={"value": "secret"},
        )
        suspension, source_reference = _suspension_and_reference(handle)
        first = port.prepare_resolution(
            pending_handle=handle,
            source_suspension_event_reference=source_reference,
            source_suspension=suspension,
            attempt_ordinal=1,
            submitted_at_utc="2026-07-31T00:00:01Z",
        )
        port.confirm_resolution_commit(
            prepared_resolution=first,
            outcome="none",
        )
        assert handle.state is McpPendingHandleState.PENDING_CONFIRMED

        second = port.prepare_resolution(
            pending_handle=handle,
            source_suspension_event_reference=source_reference,
            source_suspension=suspension,
            attempt_ordinal=1,
            submitted_at_utc="2026-07-31T00:00:09Z",
        )
        assert second is first
        assert second.sealed_responses is first.sealed_responses
        assert handle.state is McpPendingHandleState.RESOLUTION_COMMIT_IN_FLIGHT
        port.confirm_resolution_commit(
            prepared_resolution=second,
            outcome="full",
        )
        assert handle.state is McpPendingHandleState.REPLAY_READY

    asyncio.run(scenario())


def test_resume_requires_dispatch_full_and_returns_borrow_on_cancellation() -> None:
    async def scenario() -> None:
        manager = _BlockingResumeManager(_input_required())
        supervisor = _Supervisor(manager, _runtime_binding())
        port = _port(supervisor)
        initial = await port.execute(_request())
        assert isinstance(initial, McpToolSuspendedOutcome)
        handle = initial.pending_handle
        _confirm_pending(port, handle)
        request = _prepare_resume(port, handle)
        task = asyncio.create_task(port.resume(request))
        await asyncio.wait_for(manager.resume_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert supervisor.return_borrow_count == 1
        assert handle.state is McpPendingHandleState.RECONCILIATION_REQUIRED
        with pytest.raises(McpDrainError):
            await port.stop_admission_and_drain(deadline_monotonic=monotonic() + 0.01)

    asyncio.run(scenario())


def test_terminal_lowering_failure_never_reexecutes_physical_resume() -> None:
    manager = _Manager(
        _input_required(),
        resume_result=McpToolResult(
            output="provider completed",
            metadata={"bad": object()},
        ),
    )
    supervisor = _Supervisor(manager, _runtime_binding())
    port = _port(supervisor)
    initial = asyncio.run(port.execute(_request()))
    assert isinstance(initial, McpToolSuspendedOutcome)
    handle = initial.pending_handle
    _confirm_pending(port, handle)
    request = _prepare_resume(port, handle)

    first = asyncio.run(port.resume(request))
    second = asyncio.run(port.resume(request))

    assert isinstance(first, McpToolRejectedOutcome)
    assert first.error_code is McpToolRejectCode.RESULT_LOWERING_FAILED
    assert second is first
    assert manager.resume_count == 1
    assert supervisor.return_borrow_count == 1
    assert handle.state is McpPendingHandleState.TERMINAL_RESULT_FROZEN


def test_successor_result_is_frozen_before_atomic_handle_replacement() -> None:
    manager = _Manager(
        _input_required(),
        resume_result=_input_required(round_ordinal=2),
    )
    supervisor = _Supervisor(manager, _runtime_binding())
    port = _port(supervisor)
    initial = asyncio.run(port.execute(_request()))
    assert isinstance(initial, McpToolSuspendedOutcome)
    predecessor = initial.pending_handle
    _confirm_pending(port, predecessor)

    outcome = asyncio.run(port.resume(_prepare_resume(port, predecessor)))

    assert isinstance(outcome, McpToolSuspendedOutcome)
    successor = outcome.pending_handle
    assert manager.resume_count == 1
    assert predecessor.state is McpPendingHandleState.COMPLETED
    assert successor.state is McpPendingHandleState.PREPARED_SUSPENSION
    assert successor.identity.predecessor_handle_id == predecessor.identity.handle_id
    assert successor.identity.handle_generation == 2
    assert port.handle_for_interaction("mcp_input_required:test") is successor
