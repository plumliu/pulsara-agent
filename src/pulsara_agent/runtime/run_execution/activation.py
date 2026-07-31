"""Exact activation attribution for one process-local run segment."""

from __future__ import annotations

from pulsara_agent.event import AgentEvent, RunInteractionResumeBoundaryEvent, RunStartEvent
from pulsara_agent.event_log import EventLog
from pulsara_agent.ports.run_authority import (
    HostRunGenesisEntry,
    SubagentRunGenesisEntry,
)
from pulsara_agent.ports.run_execution import (
    HostResumeBoundaryActivationSource,
    HostRunBoundaryActivationSource,
    RunActivationIdentity,
    SubagentRunStartActivationSource,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.run_boundary import RunExecutionActivationFact
from pulsara_agent.primitives.model_call import sha256_fingerprint
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored
from pulsara_agent.runtime.run_execution.owner import RunOwner


def materialize_activation_identity(
    *,
    owner: RunOwner,
    segment_generation: int,
    event_log: EventLog,
) -> RunActivationIdentity:
    if segment_generation < 1:
        raise ValueError("activation generation must be positive")
    durable_payload = {
        "schema_version": "run_execution_activation.v1",
        "activation_owner_kind": owner.latest_activation_owner_kind,
        "activation_owner_id": owner.latest_activation_owner_id,
        "segment_generation": segment_generation,
    }
    durable = RunExecutionActivationFact(
        **durable_payload,
        activation_fingerprint=sha256_fingerprint(
            "run-execution-activation:v1", durable_payload
        ),
    )

    return materialize_activation_identity_from_fact(
        owner=owner,
        durable_activation=durable,
        event_log=event_log,
    )


def materialize_activation_identity_from_fact(
    *,
    owner: RunOwner,
    durable_activation: RunExecutionActivationFact,
    event_log: EventLog,
    stored_source_event: AgentEvent | None = None,
) -> RunActivationIdentity:
    """Rebind an already committed segment attribution during process reopen."""

    if durable_activation.activation_owner_kind == "host_resume_boundary":
        source_event = stored_source_event or event_log.get_by_id(
            durable_activation.activation_owner_id
        )
        if (
            not isinstance(source_event, RunInteractionResumeBoundaryEvent)
            or source_event.id != durable_activation.activation_owner_id
            or source_event.sequence is None
            or source_event.run_id != owner.identity.run_id
        ):
            raise RuntimeError("resume activation lacks exact durable boundary")
        source = _runtime_fact(
            HostResumeBoundaryActivationSource,
            domain="run-activation-source:host-resume-boundary:v1",
            fingerprint_field="source_fingerprint",
            source_kind="host_resume_boundary",
            source_resume_boundary_event_reference=event_reference_from_stored(
                source_event,
                runtime_session_id=owner.identity.runtime_session_id,
            ),
            source_resume_boundary=source_event.boundary,
        )
    else:
        source_event = stored_source_event or event_log.get_by_id(
            owner.identity.run_start_event_id
        )
        if (
            not isinstance(source_event, RunStartEvent)
            or source_event.id != owner.identity.run_start_event_id
            or source_event.sequence != owner.identity.run_start_sequence
        ):
            raise RuntimeError("initial activation lacks exact durable RunStart")
        source_reference = event_reference_from_stored(
            source_event,
            runtime_session_id=owner.identity.runtime_session_id,
        )
        if durable_activation.activation_owner_kind == "host_run_boundary":
            if not isinstance(owner.genesis.entry, HostRunGenesisEntry):
                raise RuntimeError("Host activation has non-Host genesis")
            source = _runtime_fact(
                HostRunBoundaryActivationSource,
                domain="run-activation-source:host-run-boundary:v1",
                fingerprint_field="source_fingerprint",
                source_kind="host_run_boundary",
                source_run_start_event_reference=source_reference,
                source_boundary=owner.genesis.entry.new_run_boundary,
            )
        elif durable_activation.activation_owner_kind == "subagent_run_start":
            if not isinstance(owner.genesis.entry, SubagentRunGenesisEntry):
                raise RuntimeError("child activation has non-child genesis")
            source = _runtime_fact(
                SubagentRunStartActivationSource,
                domain="run-activation-source:subagent-run-start:v1",
                fingerprint_field="source_fingerprint",
                source_kind="subagent_run_start",
                source_run_start_event_reference=source_reference,
                source_entry=owner.genesis.entry.subagent_run_entry,
            )
        else:
            raise RuntimeError("unsupported recovered activation owner kind")

    payload = {
        "schema_version": 1,
        "owner_identity": owner.identity,
        "durable_activation": durable_activation,
        "source": source,
    }
    provisional = RunActivationIdentity.model_construct(
        **payload,
        activation_fingerprint="pending",
    )
    return RunActivationIdentity(
        **payload,
        activation_fingerprint=context_fingerprint(
            "run-activation:v1",
            provisional.model_dump(mode="json", exclude={"activation_fingerprint"}),
        ),
    )


def _runtime_fact(fact_type, *, domain: str, fingerprint_field: str, **payload):
    provisional = fact_type.model_construct(
        **payload,
        **{fingerprint_field: "pending"},
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
    "materialize_activation_identity",
    "materialize_activation_identity_from_fact",
]
