"""Exact EventLog source binding and deterministic job candidates."""

from __future__ import annotations

from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope

from dataclasses import dataclass
from time import monotonic
from typing import cast

from pulsara_agent.event import (
    ContextCompactionMemoryExtractionRequestedEvent,
    ToolResultEndEvent,
)
from pulsara_agent.primitives.stored_event import (
    RawStoredEventEnvelope,
    RawTranscriptDomainPrefixFact,
)

from pulsara_agent.event_log.protocol import EventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionJobCandidateFact,
    DurableProjectionJobSemanticFact,
    DurableProjectionKind,
    DurableProjectionLedgerHorizonFact,
    DurableProjectionSourceEventReferenceFact,
    DurableProjectionStoredEventFact,
    build_projection_fact,
    durable_projection_job_id,
    projection_target_key,
)
from pulsara_agent.projection_jobs.compaction_memory_policy import (
    compaction_memory_delivery_policy_from_request,
)
from pulsara_agent.runtime.projection_jobs.registry import (
    DurableProjectionTriggerRegistry,
)


@dataclass(frozen=True, slots=True)
class BoundDurableProjectionStoredEvent:
    envelope: RawStoredEventEnvelope
    stored_event: DurableProjectionStoredEventFact
    source_reference: DurableProjectionSourceEventReferenceFact
    trigger_horizon: DurableProjectionLedgerHorizonFact


def source_event_reference(
    envelope: RawStoredEventEnvelope,
) -> DurableProjectionSourceEventReferenceFact:
    return cast(
        DurableProjectionSourceEventReferenceFact,
        build_projection_fact(
            DurableProjectionSourceEventReferenceFact,
            schema_version="durable_projection_source_event_reference.v1",
            runtime_session_id=envelope.runtime_session_id,
            run_id=envelope.run_id,
            turn_id=envelope.turn_id,
            reply_id=envelope.reply_id,
            event_id=envelope.event_id,
            sequence=envelope.sequence,
            event_type=envelope.event_type,
            event_schema_version=envelope.event_schema_version,
            event_schema_fingerprint=envelope.event_schema_fingerprint,
            event_domain_contract_fingerprint=(
                envelope.event_domain_contract_fingerprint
            ),
            payload_fingerprint=envelope.payload_fingerprint,
            stored_envelope_fingerprint=envelope.envelope_fingerprint,
        ),
    )


def ledger_horizon(
    *,
    runtime_session_id: str,
    prefix: RawTranscriptDomainPrefixFact,
) -> DurableProjectionLedgerHorizonFact:
    return cast(
        DurableProjectionLedgerHorizonFact,
        build_projection_fact(
            DurableProjectionLedgerHorizonFact,
            schema_version="durable_projection_ledger_horizon.v1",
            runtime_session_id=runtime_session_id,
            through_sequence=prefix.through_sequence,
            ledger_continuity_accumulator=prefix.ledger_continuity_accumulator,
            ledger_payload_prefix_bytes=prefix.ledger_payload_bytes,
            transcript_semantic_prefix_count=prefix.semantic_event_count,
            transcript_semantic_prefix_accumulator=prefix.semantic_accumulator,
        ),
    )


def exact_stored_event(
    *,
    event_log: EventLog,
    event_id: str,
    deadline_monotonic: float | None = None,
) -> BoundDurableProjectionStoredEvent:
    deadline = deadline_monotonic or monotonic() + 20.0
    selected = event_log.read_raw_events_by_id((event_id,), deadline_monotonic=deadline)
    if len(selected) != 1 or selected[0].event_id != event_id:
        raise ValueError("projection source event is unavailable")
    envelope = selected[0]
    prefix = event_log.read_raw_ledger_prefix(
        through_sequence=envelope.sequence,
        deadline_monotonic=deadline,
    )
    source_reference = source_event_reference(envelope)
    canonical_payload = envelope.canonical_payload_bytes.decode("utf-8")
    stored_event = cast(
        DurableProjectionStoredEventFact,
        build_projection_fact(
            DurableProjectionStoredEventFact,
            schema_version="durable_projection_stored_event.v1",
            event_reference=source_reference,
            canonical_payload_json_utf8=canonical_payload,
            canonical_payload_utf8_bytes=len(canonical_payload.encode("utf-8")),
            canonical_payload_sha256=envelope.payload_fingerprint,
        ),
    )
    return BoundDurableProjectionStoredEvent(
        envelope=envelope,
        stored_event=stored_event,
        source_reference=source_reference,
        trigger_horizon=ledger_horizon(
            runtime_session_id=envelope.runtime_session_id,
            prefix=prefix,
        ),
    )


def exact_rebind_source_reference(
    *,
    event_log: EventLog,
    reference: DurableProjectionSourceEventReferenceFact,
    deadline_monotonic: float | None = None,
) -> BoundDurableProjectionStoredEvent:
    stored = exact_stored_event(
        event_log=event_log,
        event_id=reference.event_id,
        deadline_monotonic=deadline_monotonic,
    )
    if stored.source_reference != reference:
        raise ValueError("projection source event exact rebind failed")
    return stored


def build_job_candidate(
    *,
    stored: BoundDurableProjectionStoredEvent,
    projection_kind: DurableProjectionKind,
    activation_fingerprint: str,
    trigger_registry: DurableProjectionTriggerRegistry,
) -> DurableProjectionJobCandidateFact:
    seed_contract = trigger_registry.resolve(projection_kind)
    matching = tuple(
        binding
        for binding in seed_contract.ordered_trigger_bindings
        if binding.trigger_event_type == stored.envelope.event_type
    )
    if len(matching) != 1:
        raise ValueError("event type is not a unique trigger for projection kind")
    if (
        stored.envelope.event_schema_fingerprint
        not in matching[0].accepted_event_schema_fingerprints
    ):
        raise ValueError("projection trigger event schema is unsupported")
    event = decode_raw_stored_event_envelope(
        stored.envelope, DEFAULT_EVENT_SCHEMA_REGISTRY
    )
    tool_call_id = event.tool_call_id if isinstance(event, ToolResultEndEvent) else None
    source_event_id = (
        event.id
        if isinstance(event, ContextCompactionMemoryExtractionRequestedEvent)
        else None
    )
    delivery_policy = seed_contract.delivery_policy
    if isinstance(event, ContextCompactionMemoryExtractionRequestedEvent):
        delivery_policy = compaction_memory_delivery_policy_from_request(
            event.extraction_policy
        )
        if delivery_policy != seed_contract.delivery_policy:
            raise ValueError(
                "extraction Request delivery policy is outside the active seed contract"
            )
    target_key = projection_target_key(
        projection_kind=projection_kind,
        runtime_session_id=stored.source_reference.runtime_session_id,
        run_id=stored.source_reference.run_id,
        tool_call_id=tool_call_id,
        source_event_id=source_event_id,
    )
    job_id = durable_projection_job_id(
        projection_kind=projection_kind,
        source_event_reference=stored.source_reference,
        target_key=target_key,
        handler_contract_fingerprint=(
            seed_contract.handler_contract.contract_fingerprint
        ),
    )
    semantic = cast(
        DurableProjectionJobSemanticFact,
        build_projection_fact(
            DurableProjectionJobSemanticFact,
            schema_version="durable_projection_job_semantic.v1",
            job_id=job_id,
            projection_kind=projection_kind,
            target_key=target_key,
            source_event_reference=stored.source_reference,
            trigger_horizon=stored.trigger_horizon,
            handler_contract=seed_contract.handler_contract,
        ),
    )
    return cast(
        DurableProjectionJobCandidateFact,
        build_projection_fact(
            DurableProjectionJobCandidateFact,
            schema_version="durable_projection_job_candidate.v1",
            job_semantic=semantic,
            activation_fingerprint=activation_fingerprint,
            seed_contract_fingerprint=seed_contract.seed_contract_fingerprint,
            delivery_policy=delivery_policy,
            canonical_mutation_surface_plan=(
                seed_contract.canonical_mutation_surface_plan
            ),
        ),
    )


def verify_job_source(
    *,
    event_log: EventLog,
    candidate: DurableProjectionJobCandidateFact,
    deadline_monotonic: float | None = None,
) -> None:
    stored = exact_rebind_source_reference(
        event_log=event_log,
        reference=candidate.job_semantic.source_event_reference,
        deadline_monotonic=deadline_monotonic,
    )
    if stored.trigger_horizon != candidate.job_semantic.trigger_horizon:
        raise ValueError("projection job trigger horizon exact rebind failed")


__all__ = [
    "BoundDurableProjectionStoredEvent",
    "build_job_candidate",
    "exact_rebind_source_reference",
    "exact_stored_event",
    "ledger_horizon",
    "source_event_reference",
    "verify_job_source",
]
