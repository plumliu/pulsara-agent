"""Materialize one pending interaction from its exact committed source fact."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import TypeVar

from pulsara_agent.event import (
    PlanExitRequestedEvent,
    PlanQuestionAskedEvent,
    RequireUserConfirmEvent,
    ToolExecutionSuspendedEvent,
)
from pulsara_agent.ports.run_execution import (
    PendingApprovalAuthority,
    PendingInteractionAuthority,
    PendingInteractionIdentity,
    PendingMcpInputRequiredAuthority,
    PendingPlanExitAuthority,
    PendingPlanQuestionAuthority,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.runtime.approval import pending_approval_from_state
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored
from pulsara_agent.runtime.plan import (
    pending_mcp_input_required_from_state,
    pending_plan_interaction_from_state,
)
from pulsara_agent.runtime.run_execution.owner import RunOwner
from pulsara_agent.runtime.run_execution.prepared import RunActivationStateCarrier
from pulsara_agent.runtime.run_execution.commit_gateway import (
    rebind_confirmed_event_candidate,
)
from pulsara_agent.runtime.run_execution.owner import (
    ApprovalSuspensionResources,
    McpSuspensionResources,
    PlanSuspensionResources,
    RunSuspensionResources,
)
from pulsara_agent.runtime.state import LoopStatus


_FactT = TypeVar("_FactT", bound=FrozenRuntimeStateBase)


def materialize_pending_interaction(
    *,
    owner: RunOwner,
    host_session_id: str,
) -> tuple[PendingInteractionAuthority, RunSuspensionResources]:
    segment = owner.active_segment
    if (
        segment is None
        or segment.state_carrier is None
        or segment.state_owner_token is None
    ):
        raise RuntimeError("pending interaction lacks its activation-state owner")
    state = segment.state_carrier.borrow(owner_token=segment.state_owner_token)
    if state.status is not LoopStatus.WAITING_USER:
        raise ValueError("pending interaction requires a waiting activation")
    if state.pending_interaction_kind == "mcp_input_required":
        pending = pending_mcp_input_required_from_state(state, host_session_id)
        source_ref = pending.source_suspension_event_reference
        source = _read_exact_source(
            state=state,
            owner=owner,
            source_ref=source_ref,
            event_type=ToolExecutionSuspendedEvent,
        )
        identity = _identity(
            owner=owner,
            interaction_kind="mcp_input_required",
            interaction_id=source.suspension.interaction.interaction_id,
            source_ref=source_ref,
        )
        authority = _build_runtime_fact(
            PendingMcpInputRequiredAuthority,
            domain="pending-mcp-input-authority:v1",
            fingerprint_field="authority_fingerprint",
            interaction_kind="mcp_input_required",
            identity=identity,
            suspension=source.suspension,
        )
        pending_handle = state.pending_interaction_payload.get("mcp_pending_handle")
        if pending_handle is None or not hasattr(pending_handle, "identity"):
            raise RuntimeError("MCP suspension lost its process-local pending handle")
        resources = _mcp_resources(
            owner=owner,
            authority=authority,
            public_view=pending,
            pending_handle=pending_handle,
            activation_payload=_freeze_activation_payload(
                state.pending_interaction_payload,
                omitted_keys=frozenset({"mcp_pending_handle"}),
            ),
        )
        return authority, resources

    if state.pending_interaction_kind == "plan":
        pending = pending_plan_interaction_from_state(state, host_session_id)
        source_ref = _pending_source_reference(state)
        if pending.kind == "question":
            source = _read_exact_source(
                state=state,
                owner=owner,
                source_ref=source_ref,
                event_type=PlanQuestionAskedEvent,
            )
            if source.question_id != pending.question_id:
                raise RuntimeError("plan-question source identity drifted")
            identity = _identity(
                owner=owner,
                interaction_kind="plan_question",
                interaction_id=source.question_id,
                source_ref=source_ref,
            )
            authority = _build_runtime_fact(
                PendingPlanQuestionAuthority,
                domain="pending-plan-question-authority:v1",
                fingerprint_field="authority_fingerprint",
                interaction_kind="plan_question",
                identity=identity,
            )
            resources = _plan_resources(
                owner=owner,
                authority=authority,
                public_view=pending,
                activation_payload=_freeze_activation_payload(
                    state.pending_interaction_payload
                ),
            )
            return authority, resources
        source = _read_exact_source(
            state=state,
            owner=owner,
            source_ref=source_ref,
            event_type=PlanExitRequestedEvent,
        )
        if source.exit_request_id != pending.exit_request_id:
            raise RuntimeError("plan-exit source identity drifted")
        identity = _identity(
            owner=owner,
            interaction_kind="plan_exit",
            interaction_id=source.exit_request_id,
            source_ref=source_ref,
        )
        authority = _build_runtime_fact(
            PendingPlanExitAuthority,
            domain="pending-plan-exit-authority:v1",
            fingerprint_field="authority_fingerprint",
            interaction_kind="plan_exit",
            identity=identity,
        )
        resources = _plan_resources(
            owner=owner,
            authority=authority,
            public_view=pending,
            activation_payload=_freeze_activation_payload(
                state.pending_interaction_payload
            ),
        )
        return authority, resources

    pending = pending_approval_from_state(state, host_session_id)
    pending_call_ids = tuple(call.id for call in pending.tool_calls)
    source_ref = _pending_source_reference(state)
    source = _read_exact_source(
        state=state,
        owner=owner,
        source_ref=source_ref,
        event_type=RequireUserConfirmEvent,
    )
    if tuple(call.id for call in source.tool_calls) != pending_call_ids:
        raise RuntimeError("approval source tool-call identity drifted")
    # Approval did not historically carry a durable ID. Derive it from the
    # immutable source event rather than generating a new identity on replay.
    pending.approval_id = source.id
    identity = _identity(
        owner=owner,
        interaction_kind="approval",
        interaction_id=pending.approval_id,
        source_ref=source_ref,
    )
    authority = _build_runtime_fact(
        PendingApprovalAuthority,
        domain="pending-approval-authority:v1",
        fingerprint_field="authority_fingerprint",
        interaction_kind="approval",
        identity=identity,
    )
    resources = _approval_resources(
        owner=owner,
        authority=authority,
        public_view=pending,
    )
    return authority, resources


def materialize_recovered_mcp_interaction(
    *,
    owner: RunOwner,
    host_session_id: str,
    state_carrier: RunActivationStateCarrier,
    state_owner_token: str,
    resource_generation: int,
) -> tuple[PendingMcpInputRequiredAuthority, McpSuspensionResources]:
    """Rebuild a suspension slot from its exact stored candidate on reopen."""

    if resource_generation < 1:
        raise ValueError("recovered MCP resource generation must be positive")
    state = state_carrier.borrow(owner_token=state_owner_token)
    if state.status is not LoopStatus.WAITING_USER:
        raise ValueError("recovered MCP interaction requires a waiting state")
    pending = pending_mcp_input_required_from_state(state, host_session_id)
    source_ref = pending.source_suspension_event_reference
    source = _read_exact_source(
        state=state,
        owner=owner,
        source_ref=source_ref,
        event_type=ToolExecutionSuspendedEvent,
    )
    identity = _identity(
        owner=owner,
        interaction_kind="mcp_input_required",
        interaction_id=source.suspension.interaction.interaction_id,
        source_ref=source_ref,
    )
    authority = _build_runtime_fact(
        PendingMcpInputRequiredAuthority,
        domain="pending-mcp-input-authority:v1",
        fingerprint_field="authority_fingerprint",
        interaction_kind="mcp_input_required",
        identity=identity,
        suspension=source.suspension,
    )
    pending_handle = state.pending_interaction_payload.get("mcp_pending_handle")
    if pending_handle is None or not hasattr(pending_handle, "identity"):
        raise RuntimeError("recovered MCP suspension lost its pending handle")
    resource_fingerprint = context_fingerprint(
        "run-suspension-resource:v1",
        {
            "owner_fingerprint": owner.identity.owner_fingerprint,
            "interaction_fingerprint": identity.interaction_fingerprint,
            "resource_kind": "mcp_input_required",
            "resource_generation": resource_generation,
        },
    )
    resources = McpSuspensionResources(
        resource_kind="mcp_input_required",
        resource_generation=resource_generation,
        pending_interaction_fingerprint=identity.interaction_fingerprint,
        resource_identity_fingerprint=resource_fingerprint,
        public_view=pending,
        pending_handle=pending_handle,
        activation_payload=_freeze_activation_payload(
            state.pending_interaction_payload,
            omitted_keys=frozenset({"mcp_pending_handle"}),
        ),
        state_carrier=state_carrier,
        state_owner_token=state_owner_token,
    )
    return authority, resources


def _resource_identity(*, owner: RunOwner, authority, kind: str) -> tuple[int, str]:
    segment = owner.active_segment
    if segment is None:
        raise RuntimeError("pending interaction has no active activation owner")
    generation = segment.segment_generation
    return generation, context_fingerprint(
        "run-suspension-resource:v1",
        {
            "owner_fingerprint": owner.identity.owner_fingerprint,
            "interaction_fingerprint": authority.identity.interaction_fingerprint,
            "resource_kind": kind,
            "resource_generation": generation,
        },
    )


def _approval_resources(*, owner: RunOwner, authority, public_view):
    generation, fingerprint = _resource_identity(
        owner=owner, authority=authority, kind="approval"
    )
    return ApprovalSuspensionResources(
        resource_kind="approval",
        resource_generation=generation,
        pending_interaction_fingerprint=authority.identity.interaction_fingerprint,
        resource_identity_fingerprint=fingerprint,
        public_view=public_view,
        state_carrier=_required_segment_state_carrier(owner),
        state_owner_token=_suspension_state_owner_token(owner, authority),
    )


def _plan_resources(*, owner: RunOwner, authority, public_view, activation_payload):
    kind = authority.interaction_kind
    generation, fingerprint = _resource_identity(
        owner=owner, authority=authority, kind=kind
    )
    return PlanSuspensionResources(
        resource_kind=kind,
        resource_generation=generation,
        pending_interaction_fingerprint=authority.identity.interaction_fingerprint,
        resource_identity_fingerprint=fingerprint,
        public_view=public_view,
        activation_payload=activation_payload,
        state_carrier=_required_segment_state_carrier(owner),
        state_owner_token=_suspension_state_owner_token(owner, authority),
    )


def _mcp_resources(
    *, owner: RunOwner, authority, public_view, pending_handle, activation_payload
):
    generation, fingerprint = _resource_identity(
        owner=owner, authority=authority, kind="mcp_input_required"
    )
    return McpSuspensionResources(
        resource_kind="mcp_input_required",
        resource_generation=generation,
        pending_interaction_fingerprint=authority.identity.interaction_fingerprint,
        resource_identity_fingerprint=fingerprint,
        public_view=public_view,
        pending_handle=pending_handle,
        activation_payload=activation_payload,
        state_carrier=_required_segment_state_carrier(owner),
        state_owner_token=_suspension_state_owner_token(owner, authority),
    )


def _freeze_activation_payload(
    payload: Mapping[str, object],
    *,
    omitted_keys: frozenset[str] = frozenset(),
) -> Mapping[str, object]:
    """Freeze branch data without retaining a second live-resource owner."""

    return MappingProxyType(
        {
            key: _freeze_activation_value(value)
            for key, value in payload.items()
            if key not in omitted_keys
        }
    )


def _freeze_activation_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_activation_value(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_activation_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze_activation_value(item) for item in value)
    return deepcopy(value)


def _required_segment_state_carrier(owner: RunOwner):
    segment = owner.active_segment
    if segment is None or segment.state_carrier is None:
        raise RuntimeError("suspension lost its activation-state carrier")
    return segment.state_carrier


def _suspension_state_owner_token(owner: RunOwner, authority) -> str:
    return (
        f"suspension:{owner.identity.owner_fingerprint}:"
        f"{authority.identity.interaction_fingerprint}"
    )


def _identity(
    *,
    owner: RunOwner,
    interaction_kind: str,
    interaction_id: str,
    source_ref,
) -> PendingInteractionIdentity:
    return _build_runtime_fact(
        PendingInteractionIdentity,
        domain="pending-interaction:v1",
        fingerprint_field="interaction_fingerprint",
        schema_version=1,
        owner_identity=owner.identity,
        interaction_kind=interaction_kind,
        interaction_id=interaction_id,
        source_interaction_event_reference=source_ref,
    )


def _pending_source_reference(state) -> ContextEventReferenceFact:
    source_ref = state.pending_interaction_source_event_reference
    if not isinstance(source_ref, ContextEventReferenceFact):
        raise RuntimeError("pending interaction lost its exact source reference")
    return source_ref


def _read_exact_source(*, state, owner, source_ref, event_type):
    if source_ref.runtime_session_id != owner.identity.runtime_session_id:
        raise RuntimeError("pending interaction source ledger drifted")
    candidate = state.pending_interaction_source_event_candidate
    source = rebind_confirmed_event_candidate(
        candidate,
        source_reference=source_ref,
    )
    if not isinstance(source, event_type) or source.run_id != owner.identity.run_id:
        raise RuntimeError("pending interaction source authority is not exact")
    rebound = event_reference_from_stored(
        source,
        runtime_session_id=owner.identity.runtime_session_id,
    )
    if rebound != source_ref:
        raise RuntimeError("pending interaction source reference drifted")
    return source


def _build_runtime_fact(
    fact_type: type[_FactT],
    *,
    domain: str,
    fingerprint_field: str,
    **payload,
) -> _FactT:
    provisional = fact_type.model_construct(
        **payload,
        **{fingerprint_field: "sha256:" + "0" * 64},
    )
    return fact_type(
        **payload,
        **{
            fingerprint_field: context_fingerprint(
                domain,
                provisional.model_dump(mode="json", exclude={fingerprint_field}),
            )
        },
    )


__all__ = [
    "materialize_pending_interaction",
    "materialize_recovered_mcp_interaction",
]
