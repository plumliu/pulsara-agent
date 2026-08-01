from __future__ import annotations

from dataclasses import dataclass
import asyncio

import pytest

from pulsara_agent.event import EventContext
from pulsara_agent.ports.mcp import McpInvocationOwner, McpPendingHandleState
from pulsara_agent.ports.tool_registry import (
    McpToolBindingContract,
    ToolBindingOrigin,
    build_tool_binding_contract,
)
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.mcp import McpBindingIdentityFact
from pulsara_agent.primitives.runtime_event_vocabulary import (
    McpInputRequiredSuspensionFact,
)
from pulsara_agent.runtime.mcp.tool_execution_port import (
    RuntimeMcpToolExecutionPort,
)
from tests.support.mcp import prepare_test_mcp_input_required_suspension


@dataclass(slots=True)
class _RecoverySupervisor:
    protocol_semantic_fingerprint: str
    endpoint_attribution_fingerprint: str
    auth_attribution_fingerprint: str
    recovered_reservation_id: str | None = None
    aborted_reservation_id: str | None = None
    physical_resume_count: int = 0

    def recovery_rebind_authority(self, binding):
        return (
            "2026-07-28",
            self.protocol_semantic_fingerprint,
            self.endpoint_attribution_fingerprint,
            self.auth_attribution_fingerprint,
            "sdk-generation:reopened",
            "snapshot-semantic:reopened",
        )

    def recover_confirmed_pending_lease(
        self,
        *,
        interaction_id: str,
        reservation_id: str,
        binding_identity,
    ) -> None:
        assert interaction_id == "interaction:restart"
        assert binding_identity.server_id == "docs"
        self.recovered_reservation_id = reservation_id

    def abort_pending_lease(self, interaction_id: str, reservation_id: str) -> None:
        assert interaction_id == "interaction:restart"
        self.aborted_reservation_id = reservation_id


def _binding_fact() -> McpBindingIdentityFact:
    return McpBindingIdentityFact(
        server_id="docs",
        slot_id="mcp_slot:reopened",
        snapshot_id="mcp_snapshot:reopened",
        discovery_generation=2,
    )


def _binding_contract() -> McpToolBindingContract:
    binding = _binding_fact()
    contract = build_tool_binding_contract(
        tool_name="mcp__docs__lookup",
        origin=ToolBindingOrigin.MCP,
        contract_id="pulsara.mcp.docs.lookup",
        contract_version="v2",
        binding_attributes=binding.model_dump(mode="json"),
        mcp_binding_identity=binding,
        original_tool_name="lookup",
    )
    assert isinstance(contract, McpToolBindingContract)
    return contract


def _prepared_source():
    source_binding = McpBindingIdentityFact(
        server_id="docs",
        slot_id="mcp_slot:original",
        snapshot_id="mcp_snapshot:reopened",
        discovery_generation=2,
    )
    prepared = prepare_test_mcp_input_required_suspension(
        interaction_id="interaction:restart",
        runtime_session_id="runtime:restart",
        run_id="run:restart",
        turn_id="turn:restart",
        reply_id="reply:restart",
        tool_call_id="call:restart",
        tool_name="mcp__docs__lookup",
        server_id="docs",
        binding_identity=source_binding,
        pending_lease_reservation_id="pending-reservation:restart",
    )
    raw = prepared.prepared
    durable = raw.continuation.durable_fact
    prepared.repository._records[durable.continuation_carrier_id] = (
        raw.continuation.stored_record
    )
    suspension = build_frozen_fact(
        McpInputRequiredSuspensionFact,
        schema_version="mcp_input_required_suspension.v2",
        interaction=raw.interaction,
        binding_identity=raw.binding_identity,
        pending_lease_reservation=raw.pending_lease_reservation,
        request_envelope=raw.request_envelope,
        durable_continuation=durable,
        rollout_reservation_id="rollout-reservation:restart",
        rollout_reservation_fingerprint="sha256:" + "4" * 64,
        source_mcp_installation_id="mcp-installation:original",
        predecessor_resolution_submitted_event_reference=None,
    )
    source_reference = ContextEventReferenceFact(
        runtime_session_id="runtime:restart",
        event_id=raw.suspension_event_id,
        sequence=41,
        event_type="TOOL_EXECUTION_SUSPENDED",
        payload_fingerprint="sha256:" + "5" * 64,
    )
    return prepared, suspension, source_reference


def _owner() -> McpInvocationOwner:
    return McpInvocationOwner(
        runtime_session_id="runtime:restart",
        run_id="run:restart",
        tool_call_id="call:restart",
        event_context=EventContext(
            run_id="run:restart",
            turn_id="turn:restart",
            reply_id="reply:restart",
        ),
    )


def test_stateless_awaiting_continuation_rebinds_without_physical_replay() -> None:
    prepared, suspension, source_reference = _prepared_source()
    durable = suspension.durable_continuation
    supervisor = _RecoverySupervisor(
        protocol_semantic_fingerprint=durable.protocol_semantic_fingerprint,
        endpoint_attribution_fingerprint=durable.endpoint_attribution_fingerprint,
        auth_attribution_fingerprint=durable.auth_attribution_fingerprint,
    )
    port = RuntimeMcpToolExecutionPort(
        supervisor,  # type: ignore[arg-type]
        continuation_codec=prepared.codec,
        continuation_repository=prepared.repository,
    )

    recovered = port.recover_committed_continuation(
        owner=_owner(),
        binding=_binding_contract(),
        source_suspension_event_reference=source_reference,
        source_suspension=suspension,
    )

    assert recovered.recovery_state == "awaiting_client_input"
    assert recovered.prepared_resolution is None
    assert recovered.pending_handle.state is McpPendingHandleState.PENDING_CONFIRMED
    assert recovered.pending_handle.recovery_rebind_receipt is not None
    assert supervisor.recovered_reservation_id == "pending-reservation:restart"
    assert supervisor.physical_resume_count == 0
    assert (
        port.handle_for_interaction("interaction:restart") is recovered.pending_handle
    )

    port.discard_recovered_continuation(recovered)
    assert supervisor.aborted_reservation_id == "pending-reservation:restart"
    assert port.handle_for_interaction("interaction:restart") is None
    assert prepared.repository.read(durable.continuation_carrier_id) is not None


def test_restart_authority_mismatch_fails_before_lease_or_replay() -> None:
    prepared, suspension, source_reference = _prepared_source()
    durable = suspension.durable_continuation
    supervisor = _RecoverySupervisor(
        protocol_semantic_fingerprint=durable.protocol_semantic_fingerprint,
        endpoint_attribution_fingerprint=durable.endpoint_attribution_fingerprint,
        auth_attribution_fingerprint="auth-attribution:changed",
    )
    port = RuntimeMcpToolExecutionPort(
        supervisor,  # type: ignore[arg-type]
        continuation_codec=prepared.codec,
        continuation_repository=prepared.repository,
    )

    with pytest.raises(RuntimeError, match="target authority changed"):
        port.recover_committed_continuation(
            owner=_owner(),
            binding=_binding_contract(),
            source_suspension_event_reference=source_reference,
            source_suspension=suspension,
        )

    assert supervisor.recovered_reservation_id is None
    assert supervisor.physical_resume_count == 0
    assert port.handle_for_interaction("interaction:restart") is None


def test_reopened_terminalization_is_owned_by_port_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, _suspension, _source_reference = _prepared_source()
    supervisor = _RecoverySupervisor(
        protocol_semantic_fingerprint="sha256:protocol",
        endpoint_attribution_fingerprint="sha256:endpoint",
        auth_attribution_fingerprint="sha256:auth",
    )
    port = RuntimeMcpToolExecutionPort(
        supervisor,  # type: ignore[arg-type]
        continuation_codec=prepared.codec,
        continuation_repository=prepared.repository,
    )
    observed = {}
    sentinel = object()

    async def terminalize(_runtime_session, **kwargs):
        observed.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        "pulsara_agent.runtime.mcp.recovery.terminalize_reopened_mcp_input_required",
        terminalize,
    )
    result = asyncio.run(
        port.terminalize_reopened_input_required(
            object(),  # type: ignore[arg-type]
            run_id="run:restart",
            closure_reason="child_pending_unsupported",
            deadline_monotonic=1.0,
        )
    )

    assert result is sentinel
    assert observed["continuation_repository"] is prepared.repository
    assert observed["run_id"] == "run:restart"
