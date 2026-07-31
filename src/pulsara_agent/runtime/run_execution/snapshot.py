"""Immutable run-owner snapshots assembled under the registry critical section."""

from __future__ import annotations

from typing import Any

from pulsara_agent.ports.run_execution import (
    ActiveRunActivationSlotIdentity,
    ActiveRunSuspensionSlotIdentity,
    ClosedNeverBoundRunResourceSlotIdentity,
    HandleBackedRunResourceSlotIdentity,
    NoRunActivationSlotIdentity,
    NoRunSuspensionSlotIdentity,
    RunFinalizationSlotIdentity,
    RunOwnerStateIdentity,
    RunProgressSnapshot,
    UnboundRunResourceSlotIdentity,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.model_call import ModelTokenUsageFact
from pulsara_agent.runtime.run_execution.owner import (
    ActiveRunActivation,
    ActiveRunSuspension,
    BoundRunResources,
    ClosedBoundRunResources,
    ClosedNeverBoundRunResources,
    RetiringRunResources,
    RunFinalizationOwner,
    RunOwner,
    UnboundRunResources,
)
from pulsara_agent.runtime.run_execution.commit_gateway import event_candidate_identity


def build_progress_snapshot(owner: RunOwner) -> RunProgressSnapshot:
    """Project all mutable slots in one synchronous registry read."""

    state_identity = build_owner_state_identity(owner)
    usage = owner.progress.accumulated_usage
    latest_context = owner.progress.latest_context_reference
    if latest_context is not None and not isinstance(
        latest_context, ContextEventReferenceFact
    ):
        raise RuntimeError("run progress contains an untyped context reference")
    payload = {
        "state_identity": state_identity,
        "turn_index": owner.progress.turn_index,
        "reply_index": owner.progress.reply_index,
        "model_call_index": owner.progress.model_call_index,
        "accumulated_usage": ModelTokenUsageFact(
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_output_tokens=usage.reasoning_output_tokens,
            total_tokens=usage.input_tokens + usage.output_tokens,
        ),
        "latest_context_reference": latest_context,
    }
    return _fact(
        RunProgressSnapshot,
        domain="run-progress-snapshot:v1",
        fingerprint_field="snapshot_fingerprint",
        **payload,
    )


def build_owner_state_identity(owner: RunOwner) -> RunOwnerStateIdentity:
    resource_slot = _resource_slot_identity(owner)
    retiring = tuple(
        sorted(
            (
                _handle_identity(handles, slot_kind="retiring")
                for handles in owner.retiring_resources.handles_by_id.values()
            ),
            key=lambda item: (item.handle_generation, item.handle_id),
        )
    )
    activation_slot = _activation_slot_identity(owner)
    suspension_slot = _suspension_slot_identity(owner)
    finalization_slot = _finalization_slot_identity(owner)
    payload = {
        "schema_version": 1,
        "owner_identity": owner.identity,
        "lifecycle": owner.lifecycle,
        "authority_head_fingerprint": owner.authority_head.head_fingerprint,
        "resource_slot": resource_slot,
        "retiring_resource_identities": retiring,
        "retiring_resource_accumulator": context_fingerprint(
            "run-retiring-resource-set:v1",
            tuple(item.identity_fingerprint for item in retiring),
        ),
        "activation_slot": activation_slot,
        "suspension_slot": suspension_slot,
        "finalization_slot": finalization_slot,
        "progress_generation": owner.progress.progress_generation,
        "termination_revision": owner.termination_revision,
    }
    return _fact(
        RunOwnerStateIdentity,
        domain="run-owner-state:v1",
        fingerprint_field="state_fingerprint",
        **payload,
    )


def _resource_slot_identity(owner: RunOwner):
    slot = owner.resource_slot
    if isinstance(slot, UnboundRunResources):
        return _fact(
            UnboundRunResourceSlotIdentity,
            domain="run-resource-slot:unbound:v1",
            fingerprint_field="identity_fingerprint",
            slot_kind="unbound",
            reason=slot.reason,
        )
    if isinstance(slot, BoundRunResources):
        return _handle_identity(slot.handle_set, slot_kind="bound")
    if isinstance(slot, RetiringRunResources):
        return _handle_identity(slot.handle_set, slot_kind="retiring")
    if isinstance(slot, ClosedBoundRunResources):
        return _fact(
            HandleBackedRunResourceSlotIdentity,
            domain="run-resource-slot:handle-backed:v1",
            fingerprint_field="identity_fingerprint",
            slot_kind="closed_bound",
            handle_id=slot.closed_handle_id,
            handle_generation=slot.closed_handle_generation,
            handle_owner_fingerprint=owner.identity.owner_fingerprint,
        )
    if isinstance(slot, ClosedNeverBoundRunResources):
        return _fact(
            ClosedNeverBoundRunResourceSlotIdentity,
            domain="run-resource-slot:closed-never-bound:v1",
            fingerprint_field="identity_fingerprint",
            slot_kind="closed_never_bound",
        )
    raise TypeError(f"unsupported run resource slot: {type(slot).__name__}")


def _handle_identity(handles, *, slot_kind: str):
    handle_owner = handles.owner
    owner_fingerprint = getattr(
        handle_owner,
        "owner_fingerprint",
        getattr(handle_owner, "reservation_key_fingerprint", None),
    )
    if not isinstance(owner_fingerprint, str):
        raise RuntimeError("execution handle lacks a typed owner fingerprint")
    return _fact(
        HandleBackedRunResourceSlotIdentity,
        domain="run-resource-slot:handle-backed:v1",
        fingerprint_field="identity_fingerprint",
        slot_kind=slot_kind,
        handle_id=handles.handle_id,
        handle_generation=handles.handle_generation,
        handle_owner_fingerprint=owner_fingerprint,
    )


def _activation_slot_identity(owner: RunOwner):
    slot = owner.activation_slot
    if not isinstance(slot, ActiveRunActivation):
        return _fact(
            NoRunActivationSlotIdentity,
            domain="run-activation-slot:none:v1",
            fingerprint_field="identity_fingerprint",
            slot_kind="none",
        )
    activation = slot.coordinator
    if activation.activation_identity is None:
        raise RuntimeError("active production activation lacks typed identity")
    return _fact(
        ActiveRunActivationSlotIdentity,
        domain="run-activation-slot:active:v1",
        fingerprint_field="identity_fingerprint",
        slot_kind="active",
        activation_identity=activation.activation_identity,
        activation_phase=activation.phase,
        driver_generation=activation.segment_generation,
    )


def _suspension_slot_identity(owner: RunOwner):
    slot = owner.suspension_slot
    if not isinstance(slot, ActiveRunSuspension):
        return _fact(
            NoRunSuspensionSlotIdentity,
            domain="run-suspension-slot:none:v1",
            fingerprint_field="identity_fingerprint",
            slot_kind="none",
        )
    resources = slot.resources
    return _fact(
        ActiveRunSuspensionSlotIdentity,
        domain="run-suspension-slot:active:v1",
        fingerprint_field="identity_fingerprint",
        slot_kind="active",
        pending_interaction_identity=slot.authority.identity,
        authority_fingerprint=slot.authority.authority_fingerprint,
        resource_kind=resources.resource_kind,
        resource_generation=resources.resource_generation,
        resource_identity_fingerprint=resources.resource_identity_fingerprint,
    )


def _finalization_slot_identity(owner: RunOwner):
    slot = owner.finalization_slot
    finalization = slot.owner
    if slot.state == "completed":
        receipt = slot.receipt
        if receipt is None:
            raise RuntimeError("completed finalization slot lacks receipt")
        return _fact(
            RunFinalizationSlotIdentity,
            domain="run-finalization-slot:v1",
            fingerprint_field="identity_fingerprint",
            slot_state="completed",
            owner_or_receipt_id=f"terminal-receipt:{receipt.run_end_event_reference.event_id}",
            owner_or_receipt_fingerprint=receipt.receipt_fingerprint,
            stable_candidate_id=None,
            stable_candidate_fingerprint=None,
        )
    if not isinstance(finalization, RunFinalizationOwner):
        return _empty_finalization_identity()
    candidate = finalization.run_end_candidate
    confirmed_reference = finalization.confirmed_run_end_event_reference
    if candidate is not None:
        candidate_id, candidate_fingerprint = event_candidate_identity(candidate)
    elif confirmed_reference is not None:
        candidate_id = confirmed_reference.event_id
        candidate_fingerprint = confirmed_reference.payload_fingerprint
    else:
        if slot.state != "empty" or finalization.state != "idle":
            raise RuntimeError("non-empty finalization owner lacks terminal authority")
        return _empty_finalization_identity()
    owner_fingerprint = context_fingerprint(
        "run-finalization-owner:v1",
        (
            owner.identity.owner_fingerprint,
            finalization.terminal_event_id,
            candidate_fingerprint,
        ),
    )
    projected_state = slot.state
    if finalization.state == "reconciliation_required":
        projected_state = "reconciliation_required"
    elif finalization.commit_state == "confirmed":
        projected_state = "run_end_full_pending_output"
    else:
        projected_state = "active"
    return _fact(
        RunFinalizationSlotIdentity,
        domain="run-finalization-slot:v1",
        fingerprint_field="identity_fingerprint",
        slot_state=projected_state,
        owner_or_receipt_id=f"run-finalization:{finalization.terminal_event_id}",
        owner_or_receipt_fingerprint=owner_fingerprint,
        stable_candidate_id=candidate_id,
        stable_candidate_fingerprint=candidate_fingerprint,
    )


def _empty_finalization_identity():
    return _fact(
        RunFinalizationSlotIdentity,
        domain="run-finalization-slot:v1",
        fingerprint_field="identity_fingerprint",
        slot_state="empty",
        owner_or_receipt_id=None,
        owner_or_receipt_fingerprint=None,
        stable_candidate_id=None,
        stable_candidate_fingerprint=None,
    )


def _fact(model_type, *, domain: str, fingerprint_field: str, **payload: Any):
    provisional = model_type.model_construct(
        **payload,
        **{fingerprint_field: "sha256:" + "0" * 64},
    )
    return model_type(
        **payload,
        **{
            fingerprint_field: context_fingerprint(
                domain,
                provisional.model_dump(mode="json", exclude={fingerprint_field}),
            )
        },
    )


__all__ = ["build_owner_state_identity", "build_progress_snapshot"]
