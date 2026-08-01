"""Pure materializers for committed run authority."""

from __future__ import annotations

from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope

from pulsara_agent.event import (
    CapabilityExposureResolvedEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
)
from pulsara_agent.primitives.stored_event import RawStoredEventEnvelope
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.capability import CapabilityResolveBasisFact
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored
from pulsara_agent.runtime.permission_snapshot import snapshot_from_run_start_event
from pulsara_agent.ports.run_authority import (
    AwaitingInitialRevision,
    ContinuationRunAuthorityRevision,
    HostRunGenesisEntry,
    InitialRunAuthorityRevision,
    InstalledRunAuthorityRevision,
    RunGenesisAuthority,
    SubagentRunGenesisEntry,
)
from pulsara_agent.ports.run_execution import (
    RunOwnerIdentity,
    build_prepared_run_owner_reservation_key,
    build_run_owner_identity,
)


def owner_identity_from_stored_run_start(
    event: RunStartEvent,
    *,
    stored_envelope: RawStoredEventEnvelope,
) -> RunOwnerIdentity:
    if event.sequence is None:
        raise ValueError("RunOwnerIdentity requires a stored RunStart")
    decoded = decode_raw_stored_event_envelope(
        stored_envelope, DEFAULT_EVENT_SCHEMA_REGISTRY
    )
    if not isinstance(decoded, RunStartEvent) or decoded != event:
        raise ValueError("RunOwnerIdentity requires the exact stored RunStart envelope")
    reservation = build_prepared_run_owner_reservation_key(
        runtime_session_id=stored_envelope.runtime_session_id,
        run_id=event.run_id,
        run_start_event_id=event.id,
    )
    return build_run_owner_identity(
        reservation_key=reservation,
        run_start_sequence=event.sequence,
    )


def materialize_run_genesis(
    event: RunStartEvent,
    *,
    stored_envelope: RawStoredEventEnvelope,
) -> RunGenesisAuthority:
    identity = owner_identity_from_stored_run_start(
        event, stored_envelope=stored_envelope
    )
    reference = ContextEventReferenceFact(
        runtime_session_id=stored_envelope.runtime_session_id,
        event_id=stored_envelope.event_id,
        sequence=stored_envelope.sequence,
        event_type=stored_envelope.event_type,
        payload_fingerprint=stored_envelope.payload_fingerprint,
    )
    if event.new_run_boundary is not None:
        if event.host_run_ingress is None or event.host_ingress_admission_proof is None:
            raise ValueError("Host RunStart lost ingress authority")
        entry = HostRunGenesisEntry(
            new_run_boundary=event.new_run_boundary,
            host_run_ingress=event.host_run_ingress,
            host_ingress_admission_proof=event.host_ingress_admission_proof,
        )
    else:
        if event.subagent_run_entry is None or event.child_rollout_subaccount is None:
            raise ValueError("child RunStart lost entry authority")
        entry = SubagentRunGenesisEntry(
            subagent_run_entry=event.subagent_run_entry,
            child_rollout_subaccount=event.child_rollout_subaccount,
        )
    basis = (
        entry.new_run_boundary.capability_basis
        if isinstance(entry, HostRunGenesisEntry)
        else entry.subagent_run_entry.capability_basis
    )
    if basis.owner.runtime_session_id != stored_envelope.runtime_session_id:
        raise ValueError("RunStart capability basis belongs to another ledger")
    permission = snapshot_from_run_start_event(
        event, runtime_session_id=stored_envelope.runtime_session_id
    ).to_context_fact()
    payload = {
        "schema_version": 1,
        "owner_identity": identity,
        "run_start_event_reference": reference,
        "run_start_payload_fingerprint": reference.payload_fingerprint,
        "entry": entry,
        "current_user_message": event.current_user_message,
        "run_model_target": event.model_target,
        "permission_snapshot": permission,
        "subagent_graph_reducer_contract": event.subagent_graph_reducer_contract,
        "long_horizon": event.long_horizon,
        "mcp_installation_id": event.mcp_installation_id,
        "mcp_installation_owner_runtime_session_id": (
            event.mcp_installation_owner_runtime_session_id
        ),
        "transcript_seed_semantic": event.run_transcript_seed_semantic,
        "transcript_seed_reference": event.run_transcript_seed_reference,
        "terminal_run_end_event_id": event.terminal_run_end_event_id,
    }
    provisional = RunGenesisAuthority.model_construct(
        **payload, genesis_fingerprint="pending"
    )
    return RunGenesisAuthority(
        **payload,
        genesis_fingerprint=context_fingerprint(
            "run-genesis-authority:v1",
            provisional.model_dump(mode="json", exclude={"genesis_fingerprint"}),
        ),
    )


def awaiting_initial_revision(
    genesis: RunGenesisAuthority,
) -> AwaitingInitialRevision:
    if isinstance(genesis.entry, HostRunGenesisEntry):
        basis = genesis.entry.new_run_boundary.capability_basis
    else:
        basis = genesis.entry.subagent_run_entry.capability_basis
    payload = {
        "owner_identity": genesis.owner_identity,
        "source_run_start_event_reference": genesis.run_start_event_reference,
        "capability_basis": basis,
    }
    provisional = AwaitingInitialRevision.model_construct(
        head_kind="awaiting_initial_revision",
        **payload,
        head_fingerprint="pending",
    )
    return AwaitingInitialRevision(
        **payload,
        head_fingerprint=context_fingerprint(
            "run-authority-head:awaiting:v1",
            provisional.model_dump(mode="json", exclude={"head_fingerprint"}),
        ),
    )


def materialize_initial_revision(
    *,
    genesis: RunGenesisAuthority,
    stored_exposure: CapabilityExposureResolvedEvent,
) -> InitialRunAuthorityRevision:
    if stored_exposure.sequence is None:
        raise ValueError("initial authority requires a stored exposure")
    if stored_exposure.exposure_revision != 1:
        raise ValueError("initial authority requires exposure revision 1")
    if isinstance(genesis.entry, HostRunGenesisEntry):
        expected_basis = genesis.entry.new_run_boundary.capability_basis
    else:
        expected_basis = genesis.entry.subagent_run_entry.capability_basis
    exposure = stored_exposure.exposure
    if (
        stored_exposure.run_id != genesis.owner_identity.run_id
        or exposure.owner != expected_basis.owner
        or exposure.resolve_basis != expected_basis
        or exposure.semantic.execution_surface
        != expected_basis.execution_surface_identity
        or exposure.semantic.execution_surface.mcp_installation_id
        != genesis.mcp_installation_id
    ):
        raise ValueError("initial capability exposure does not match RunStart basis")
    reference = event_reference_from_stored(
        stored_exposure,
        runtime_session_id=genesis.owner_identity.runtime_session_id,
    )
    payload = {
        "schema_version": 1,
        "owner_identity": genesis.owner_identity,
        "revision_kind": "initial",
        "revision": 1,
        "source_exposure_event_reference": reference,
        "source_exposure": stored_exposure.exposure,
        "effective_model_target": genesis.run_model_target,
        "effective_permission": genesis.permission_snapshot,
        "execution_surface_identity": stored_exposure.exposure.semantic.execution_surface,
    }
    provisional = InitialRunAuthorityRevision.model_construct(
        **payload, authority_fingerprint="pending"
    )
    return InitialRunAuthorityRevision(
        **payload,
        authority_fingerprint=context_fingerprint(
            "run-authority:initial:v1",
            provisional.model_dump(mode="json", exclude={"authority_fingerprint"}),
        ),
    )


def installed_authority_head(
    revision: InitialRunAuthorityRevision | ContinuationRunAuthorityRevision,
) -> InstalledRunAuthorityRevision:
    payload = {"head_kind": "installed_revision", "revision": revision}
    provisional = InstalledRunAuthorityRevision.model_construct(
        **payload,
        head_fingerprint="pending",
    )
    return InstalledRunAuthorityRevision(
        **payload,
        head_fingerprint=context_fingerprint(
            "run-authority-head:installed:v1",
            provisional.model_dump(mode="json", exclude={"head_fingerprint"}),
        ),
    )


def materialize_continuation_revision(
    *,
    predecessor: InitialRunAuthorityRevision | ContinuationRunAuthorityRevision,
    stored_boundary: RunInteractionResumeBoundaryEvent,
    stored_exposure: CapabilityExposureResolvedEvent,
    effective_model_target,
    effective_permission,
    runtime_session_id: str,
) -> ContinuationRunAuthorityRevision:
    """Build one immutable continuation revision from exact stored sources."""

    if stored_boundary.sequence is None or stored_exposure.sequence is None:
        raise ValueError("continuation authority requires stored source events")
    owner = predecessor.owner_identity
    boundary = stored_boundary.boundary
    exposure = stored_exposure.exposure
    if (
        stored_boundary.run_id != owner.run_id
        or stored_exposure.run_id != owner.run_id
        or boundary.original_run_start_event_id != owner.run_start_event_id
        or boundary.original_run_start_sequence != owner.run_start_sequence
    ):
        raise ValueError("continuation authority source run mismatch")
    if stored_exposure.exposure_revision != predecessor.revision + 1:
        raise ValueError("continuation authority revision is not consecutive")
    if exposure.owner.owner_id != boundary.identity.boundary_id:
        raise ValueError("continuation exposure owner mismatch")
    if (
        boundary.effective_exposure_id != exposure.exposure_id
        or boundary.effective_exposure_fact_fingerprint
        != exposure.exposure_fact_fingerprint
        or boundary.effective_exposure_semantic_fingerprint
        != exposure.exposure_semantic_fingerprint
        or boundary.model_target_fingerprint
        != effective_model_target.target_fingerprint
        or boundary.permission_snapshot_id != effective_permission.snapshot_id
    ):
        raise ValueError("continuation boundary authority join mismatch")
    payload = {
        "schema_version": 1,
        "owner_identity": owner,
        "revision_kind": "continuation",
        "revision": stored_exposure.exposure_revision,
        "predecessor_revision": predecessor.revision,
        "predecessor_fingerprint": predecessor.authority_fingerprint,
        "source_resume_boundary_event_reference": event_reference_from_stored(
            stored_boundary,
            runtime_session_id=runtime_session_id,
        ),
        "source_resume_boundary": boundary,
        "source_exposure_event_reference": event_reference_from_stored(
            stored_exposure,
            runtime_session_id=runtime_session_id,
        ),
        "source_exposure": exposure,
        "effective_model_target": effective_model_target,
        "effective_permission": effective_permission,
        "execution_surface_identity": exposure.semantic.execution_surface,
    }
    provisional = ContinuationRunAuthorityRevision.model_construct(
        **payload,
        authority_fingerprint="pending",
    )
    return ContinuationRunAuthorityRevision(
        **payload,
        authority_fingerprint=context_fingerprint(
            "run-authority:continuation:v1",
            provisional.model_dump(mode="json", exclude={"authority_fingerprint"}),
        ),
    )


def capability_basis_from_genesis(
    genesis: RunGenesisAuthority,
) -> CapabilityResolveBasisFact:
    if isinstance(genesis.entry, HostRunGenesisEntry):
        return genesis.entry.new_run_boundary.capability_basis
    return genesis.entry.subagent_run_entry.capability_basis


__all__ = [
    "awaiting_initial_revision",
    "capability_basis_from_genesis",
    "materialize_initial_revision",
    "materialize_continuation_revision",
    "installed_authority_head",
    "materialize_run_genesis",
    "owner_identity_from_stored_run_start",
]
