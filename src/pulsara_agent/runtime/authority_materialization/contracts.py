"""Composition-root bindings and AP0 static feasibility checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from pulsara_agent.event import EventType
from pulsara_agent.event_log.transcript_prefix import (
    TRANSCRIPT_ACCELERATION_EVENT_TYPES,
    TRANSCRIPT_SEMANTIC_EVENT_TYPES,
)
from pulsara_agent.event_log.protocol import RawStoredEventEnvelope
from pulsara_agent.event_log.protocol import RawTranscriptDomainDeltaSnapshot
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    EventSchemaDomainRegistry,
)
from pulsara_agent.primitives.authority_materialization import (
    MAX_MODEL_STREAM_STRUCTURAL_TAIL_EVENTS,
    MAX_MODEL_STREAM_STRUCTURAL_TAIL_PAYLOAD_BYTES,
    MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL,
    MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL,
    AuthorityMaterializationLimits,
    FixedBatchBurstContractFact,
    FixedBatchEventContractFact,
    NonTranscriptEventContractFact,
    PhysicalBookkeepingEventBoundFact,
    PhysicalBurstContractFact,
    PhysicalChargeContractFact,
    PhysicalOperationKind,
    StoredEnvelopeIdentityBoundsFact,
    ToolDeltaBurstContractFact,
    TranscriptAccelerationEventContractFact,
    TranscriptEventDomainRegistryContractFact,
    TranscriptDomainPrefixFact,
    TranscriptDomainSparseReadProofFact,
    TranscriptSemanticEventContractFact,
    TransportSegmentedBurstContractFact,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.model_call import (
    DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT,
)


_CANONICAL_EVENT_SERIALIZATION_CONTRACT_FINGERPRINT = context_fingerprint(
    "canonical-event-serialization-contract:v1",
    {
        "candidate": "pydantic-model-dump-json+canonical-json:v1",
        "stored_sequence": "database-assigned-positive-integer:v1",
        "historical_envelope": "stored-agent-event:v1",
    },
)


def _own_fingerprint(domain: str, payload: dict[str, object]) -> str:
    return context_fingerprint(domain, payload)


@dataclass(frozen=True, slots=True)
class TranscriptEventDomainRegistryBinding:
    contract: TranscriptEventDomainRegistryContractFact
    implementation_build_fingerprint: str

    def resolve_envelope(self, envelope: RawStoredEventEnvelope):
        entries = tuple(
            entry
            for entry in self.contract.supported_events
            if entry.event_type == envelope.event_type
            and entry.event_schema_version == envelope.event_schema_version
        )
        if len(entries) != 1:
            raise ValueError("stored event has no unique transcript-domain binding")
        entry = entries[0]
        if (
            entry.event_schema_fingerprint != envelope.event_schema_fingerprint
            or entry.event_domain_contract_fingerprint
            != envelope.event_domain_contract_fingerprint
        ):
            raise ValueError("stored event transcript-domain schema binding drifted")
        return entry


def materialize_transcript_sparse_read_proof(
    snapshot: RawTranscriptDomainDeltaSnapshot,
    *,
    binding: TranscriptEventDomainRegistryBinding,
) -> TranscriptDomainSparseReadProofFact:
    contract = binding.contract
    if snapshot.registry_contract_fingerprint != contract.registry_contract_fingerprint:
        raise ValueError("transcript sparse snapshot registry binding mismatch")
    accumulator = snapshot.before.semantic_accumulator
    for envelope in snapshot.semantic_events:
        entry = binding.resolve_envelope(envelope)
        if entry.event_domain != "transcript_semantic":
            raise ValueError("sparse transcript snapshot contains non-semantic event")
        event = envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        from pulsara_agent.event_log.transcript_prefix import (
            advance_transcript_semantic_accumulator,
        )

        accumulator = advance_transcript_semantic_accumulator(
            accumulator,
            event=event,
            event_schema_version=envelope.event_schema_version,
            event_schema_fingerprint=envelope.event_schema_fingerprint,
        )
    if accumulator != snapshot.after.semantic_accumulator:
        raise ValueError("transcript sparse snapshot accumulator proof failed")
    before_payload = {
        "schema_version": "transcript_domain_prefix.v1",
        "runtime_session_id": snapshot.runtime_session_id,
        "ledger_through_sequence": snapshot.before.through_sequence,
        "ledger_event_count": snapshot.before.through_sequence,
        "ledger_continuity_accumulator": (
            snapshot.before.ledger_continuity_accumulator
        ),
        "transcript_semantic_event_count": snapshot.before.semantic_event_count,
        "transcript_semantic_accumulator": snapshot.before.semantic_accumulator,
        "event_domain_registry_contract_fingerprint": (
            contract.registry_contract_fingerprint
        ),
    }
    prefix_before = TranscriptDomainPrefixFact(
        **before_payload,
        prefix_fingerprint=_own_fingerprint(
            "transcript-domain-prefix:v1", before_payload
        ),
    )
    after_payload = {
        **before_payload,
        "ledger_through_sequence": snapshot.after.through_sequence,
        "ledger_event_count": snapshot.after.through_sequence,
        "ledger_continuity_accumulator": snapshot.after.ledger_continuity_accumulator,
        "transcript_semantic_event_count": snapshot.after.semantic_event_count,
        "transcript_semantic_accumulator": snapshot.after.semantic_accumulator,
    }
    prefix_through = TranscriptDomainPrefixFact(
        **after_payload,
        prefix_fingerprint=_own_fingerprint(
            "transcript-domain-prefix:v1", after_payload
        ),
    )
    selected_ids_fingerprint = context_fingerprint(
        "transcript-sparse-selected-event-ids:v1",
        tuple(item.event_id for item in snapshot.semantic_events),
    )
    proof_payload = {
        "schema_version": "transcript_domain_sparse_read_proof.v1",
        "range_kind": (
            "empty"
            if snapshot.before.through_sequence == snapshot.after.through_sequence
            else "non_empty"
        ),
        "from_sequence": snapshot.before.through_sequence + 1,
        "through_sequence": snapshot.after.through_sequence,
        "prefix_before": prefix_before,
        "prefix_through": prefix_through,
        "selected_transcript_semantic_event_count": len(
            snapshot.semantic_events
        ),
        "selected_transcript_semantic_accumulator": accumulator,
        "selected_event_ids_fingerprint": selected_ids_fingerprint,
    }
    return TranscriptDomainSparseReadProofFact(
        **proof_payload,
        completeness_fingerprint=_own_fingerprint(
            "transcript-domain-sparse-read-proof:v1", proof_payload
        ),
    )


def build_default_transcript_event_domain_registry_binding(
    schema_registry: EventSchemaDomainRegistry = DEFAULT_EVENT_SCHEMA_REGISTRY,
) -> TranscriptEventDomainRegistryBinding:
    entries = []
    for schema in schema_registry.contracts():
        common: dict[str, object] = {
            "event_type": schema.event_type,
            "event_schema_version": schema.event_schema_version,
            "event_schema_fingerprint": schema.event_schema_fingerprint,
            "event_domain_contract_fingerprint": schema.domain_contract_fingerprint,
        }
        if schema.event_type in TRANSCRIPT_SEMANTIC_EVENT_TYPES:
            payload = {
                "schema_version": "transcript_semantic_event_contract.v1",
                **common,
                "event_domain": "transcript_semantic",
                "semantic_projection_contract_fingerprint": context_fingerprint(
                    "transcript-event-semantic-projection-contract:v1",
                    {
                        **common,
                        "projection": "typed-lossless-transcript-reducer:v2",
                        "storage_fields_excluded": ("sequence",),
                    },
                ),
            }
            entries.append(
                TranscriptSemanticEventContractFact(
                    **payload,
                    supported_event_fingerprint=_own_fingerprint(
                        "transcript-semantic-event-contract:v1", payload
                    ),
                )
            )
        elif schema.event_type in TRANSCRIPT_ACCELERATION_EVENT_TYPES:
            payload = {
                "schema_version": "transcript_acceleration_event_contract.v1",
                **common,
                "event_domain": "transcript_acceleration",
                "deterministic_noop_contract_fingerprint": context_fingerprint(
                    "transcript-acceleration-deterministic-noop:v1", common
                ),
            }
            entries.append(
                TranscriptAccelerationEventContractFact(
                    **payload,
                    supported_event_fingerprint=_own_fingerprint(
                        "transcript-acceleration-event-contract:v1", payload
                    ),
                )
            )
        else:
            payload = {
                "schema_version": "non_transcript_event_contract.v1",
                **common,
                "event_domain": "non_transcript",
                "exclusion_contract_fingerprint": context_fingerprint(
                    "transcript-event-explicit-exclusion:v1", common
                ),
            }
            entries.append(
                NonTranscriptEventContractFact(
                    **payload,
                    supported_event_fingerprint=_own_fingerprint(
                        "non-transcript-event-contract:v1", payload
                    ),
                )
            )
    entries = sorted(
        entries, key=lambda item: (item.event_type, item.event_schema_version)
    )
    schema_keys = {
        (item.event_type, item.event_schema_version)
        for item in schema_registry.contracts()
    }
    entry_keys = {(item.event_type, item.event_schema_version) for item in entries}
    if schema_keys != entry_keys:
        raise ValueError("transcript event-domain registry does not cover all schemas")
    semantic_entries = tuple(
        item.supported_event_fingerprint
        for item in entries
        if item.event_domain != "non_transcript"
    )
    payload = {
        "schema_version": "transcript_event_domain_registry.v1",
        "registry_id": "pulsara.transcript_event_domain",
        "registry_version": "1",
        "supported_events": tuple(entries),
        "event_classification_contract_fingerprint": context_fingerprint(
            "transcript-event-classification:v1",
            {
                "semantic": tuple(sorted(TRANSCRIPT_SEMANTIC_EVENT_TYPES)),
                "acceleration": tuple(sorted(TRANSCRIPT_ACCELERATION_EVENT_TYPES)),
                "default": "explicit_non_transcript",
            },
        ),
        "transcript_semantic_domain_contract_fingerprint": context_fingerprint(
            "transcript-semantic-domain:v1", semantic_entries
        ),
        "transcript_prefix_accumulator_contract_fingerprint": context_fingerprint(
            "transcript-prefix-accumulator:v1",
            "sha256-chain(previous,event-schema,event-semantic-payload)",
        ),
        "ledger_continuity_accumulator_contract_fingerprint": context_fingerprint(
            "ledger-continuity-accumulator:v1",
            "sha256-chain(previous,stored-envelope-fingerprint)",
        ),
    }
    contract = TranscriptEventDomainRegistryContractFact(
        **payload,
        registry_contract_fingerprint=_own_fingerprint(
            "transcript-event-domain-registry:v1", payload
        ),
    )
    return TranscriptEventDomainRegistryBinding(
        contract=contract,
        implementation_build_fingerprint="builtin-transcript-domain-registry:ap0-v1",
    )


@dataclass(frozen=True, slots=True)
class PhysicalBurstContractBinding:
    contract: PhysicalBurstContractFact
    implementation_build_fingerprint: str


class PhysicalBurstContractRegistry:
    def __init__(self) -> None:
        self._bindings: dict[tuple[str, str], PhysicalBurstContractBinding] = {}

    def register(self, binding: PhysicalBurstContractBinding) -> None:
        if type(binding.contract).__name__ == "PhysicalBurstContractBase":
            raise TypeError("abstract physical burst base cannot be registered")
        key = (binding.contract.contract_id, binding.contract.contract_version)
        existing = self._bindings.get(key)
        if existing is not None and (
            existing.contract.contract_fingerprint
            != binding.contract.contract_fingerprint
        ):
            raise ValueError("physical burst contract identity conflict")
        self._bindings[key] = binding

    def resolve_binding(
        self,
        *,
        contract_id: str,
        contract_version: str,
        contract_fingerprint: str,
    ) -> PhysicalBurstContractBinding:
        try:
            binding = self._bindings[(contract_id, contract_version)]
        except KeyError as exc:
            raise ValueError("physical burst contract binding is unavailable") from exc
        if binding.contract.contract_fingerprint != contract_fingerprint:
            raise ValueError("physical burst contract fingerprint mismatch")
        return binding

    def bindings(self) -> tuple[PhysicalBurstContractBinding, ...]:
        return tuple(self._bindings[key] for key in sorted(self._bindings))

    def unique_binding_for_operation(
        self, operation_kind: PhysicalOperationKind
    ) -> PhysicalBurstContractBinding:
        matching = tuple(
            item
            for item in self.bindings()
            if item.contract.operation_kind is operation_kind
        )
        if len(matching) != 1:
            raise ValueError(
                "physical operation does not have one unique default binding"
            )
        return matching[0]


@dataclass(frozen=True, slots=True)
class AuthorityMaterializationContractBundle:
    event_domain: TranscriptEventDomainRegistryBinding
    charge_contract: PhysicalChargeContractFact
    limits: AuthorityMaterializationLimits
    burst_registry: PhysicalBurstContractRegistry


@dataclass(frozen=True, slots=True)
class AuthorityMaterializationDoctorReport:
    checked_binding_count: int
    checked_operation_kinds: tuple[str, ...]
    maximum_reserved_events: int
    maximum_reserved_payload_bytes: int
    report_fingerprint: str


class AuthorityMaterializationContractDoctor:
    def verify(
        self, bundle: AuthorityMaterializationContractBundle
    ) -> AuthorityMaterializationDoctorReport:
        bindings = bundle.burst_registry.bindings()
        if not bindings:
            raise ValueError("authority materialization has no burst bindings")
        kinds = tuple(sorted({item.contract.operation_kind.value for item in bindings}))
        required = {item.value for item in PhysicalOperationKind}
        if set(kinds) != required:
            missing = sorted(required.difference(kinds))
            raise ValueError("physical burst registry is incomplete: " + ", ".join(missing))
        normal_event_limit = (
            bundle.limits.max_unreclaimable_ledger_events
            - bundle.limits.maintenance_reserved_events
        )
        normal_byte_limit = (
            bundle.limits.max_unreclaimable_charged_payload_bytes
            - bundle.limits.maintenance_reserved_payload_bytes
        )
        for binding in bindings:
            contract = binding.contract
            if (
                contract.event_domain_registry_contract_fingerprint
                != bundle.event_domain.contract.registry_contract_fingerprint
                or contract.physical_charge_contract_fingerprint
                != bundle.charge_contract.contract_fingerprint
                or contract.canonical_event_serialization_contract_fingerprint
                != _CANONICAL_EVENT_SERIALIZATION_CONTRACT_FINGERPRINT
            ):
                raise ValueError("physical burst binding references another contract bundle")
            if contract.operation_kind is PhysicalOperationKind.CHECKPOINT_COMMIT:
                event_limit = bundle.limits.maintenance_reserved_events
                byte_limit = bundle.limits.maintenance_reserved_payload_bytes
            elif contract.operation_kind is PhysicalOperationKind.LEDGER_GENESIS:
                event_limit = bundle.limits.max_unreclaimable_ledger_events
                byte_limit = bundle.limits.max_unreclaimable_charged_payload_bytes
            else:
                event_limit = normal_event_limit
                byte_limit = normal_byte_limit
            if contract.max_total_reserved_events > event_limit:
                raise ValueError("physical burst event quote is infeasible")
            if contract.max_total_reserved_payload_bytes > byte_limit:
                raise ValueError("physical burst byte quote is infeasible")
            if isinstance(contract, ToolDeltaBurstContractFact):
                minimum_structural_events = (
                    bundle.charge_contract.reservation_bookkeeping_charge_events
                    + contract.max_commit_batches
                    * bundle.charge_contract.charge_applied_bookkeeping_charge_events
                    + bundle.charge_contract.suspension_bookkeeping_charge_events
                    + bundle.charge_contract.settlement_bookkeeping_charge_events
                )
                minimum_structural_bytes = (
                    bundle.charge_contract.reservation_bookkeeping_charge_bytes
                    + contract.max_commit_batches
                    * bundle.charge_contract.charge_applied_bookkeeping_base_charge_bytes
                    + contract.max_result_delta_items
                    * contract.max_durable_events_per_delta_item
                    * bundle.charge_contract.charge_applied_bookkeeping_per_business_event_charge_bytes
                    + bundle.charge_contract.suspension_bookkeeping_charge_bytes
                    + bundle.charge_contract.settlement_bookkeeping_charge_bytes
                )
                if contract.max_structural_tail_events < minimum_structural_events:
                    raise ValueError(
                        "physical burst commit batches exceed structural event quote"
                    )
                if (
                    contract.max_structural_tail_payload_bytes
                    < minimum_structural_bytes
                ):
                    raise ValueError(
                        "physical burst commit batches exceed structural byte quote"
                    )
                wrapper_floor = (
                    bundle.charge_contract.fixed_sequence_wrapper_charge_bytes_per_event
                    + bundle.charge_contract.fixed_schema_wrapper_charge_bytes_per_event
                )
                wrapper_bound = (
                    contract.max_canonical_wrapper_payload_bytes_per_delta_item
                )
                if wrapper_bound < wrapper_floor:
                    raise ValueError(
                        "physical burst per-item wrapper quote is underestimated"
                    )
            if isinstance(contract, TransportSegmentedBurstContractFact):
                if contract.max_bookkeeping_events_per_commit < (
                    bundle.charge_contract.charge_applied_bookkeeping_charge_events
                ):
                    raise ValueError(
                        "segmented burst bookkeeping event quote is underestimated"
                    )
                if contract.max_bookkeeping_base_payload_bytes_per_commit < (
                    bundle.charge_contract.charge_applied_bookkeeping_base_charge_bytes
                ):
                    raise ValueError(
                        "segmented burst bookkeeping base quote is underestimated"
                    )
                if contract.max_bookkeeping_payload_bytes_per_business_event < (
                    bundle.charge_contract.charge_applied_bookkeeping_per_business_event_charge_bytes
                ):
                    raise ValueError(
                        "segmented burst bookkeeping per-event quote is underestimated"
                    )
                wrapper_floor = (
                    bundle.charge_contract.fixed_sequence_wrapper_charge_bytes_per_event
                    + bundle.charge_contract.fixed_schema_wrapper_charge_bytes_per_event
                )
                if contract.max_durable_event_wrapper_overhead_bytes < wrapper_floor:
                    raise ValueError(
                        "segmented burst durable wrapper quote is underestimated"
                    )
            if isinstance(contract, FixedBatchBurstContractFact):
                minimum_structural_bytes = (
                    bundle.charge_contract.reservation_bookkeeping_charge_bytes
                    + contract.max_commit_batches
                    * bundle.charge_contract.charge_applied_bookkeeping_base_charge_bytes
                    + contract.max_business_events
                    * bundle.charge_contract.charge_applied_bookkeeping_per_business_event_charge_bytes
                    + bundle.charge_contract.suspension_bookkeeping_charge_bytes
                    + bundle.charge_contract.settlement_bookkeeping_charge_bytes
                )
                if contract.max_structural_tail_payload_bytes < minimum_structural_bytes:
                    raise ValueError(
                        "fixed batch commit quote exceeds structural byte quote"
                    )
                self._verify_fixed_batch(contract)
        report_payload = {
            "checked_binding_count": len(bindings),
            "checked_operation_kinds": kinds,
            "maximum_reserved_events": max(
                item.contract.max_total_reserved_events for item in bindings
            ),
            "maximum_reserved_payload_bytes": max(
                item.contract.max_total_reserved_payload_bytes for item in bindings
            ),
        }
        return AuthorityMaterializationDoctorReport(
            **report_payload,
            report_fingerprint=context_fingerprint(
                "authority-materialization-doctor-report:v1", report_payload
            ),
        )

    @staticmethod
    def _verify_fixed_batch(contract: FixedBatchBurstContractFact) -> None:
        by_key = {
            (item.event_type, item.event_schema_version): item
            for item in contract.batch_event_contracts
        }
        for key, item in by_key.items():
            schema = DEFAULT_EVENT_SCHEMA_REGISTRY.latest_contract_for_type(key[0])
            if (
                schema.event_schema_version != key[1]
                or schema.event_schema_fingerprint != item.event_schema_fingerprint
            ):
                raise ValueError("fixed batch event schema binding drifted")


def _fixed_event_contract(
    event_type: EventType,
    *,
    maximum_occurrences: int,
    maximum_payload_bytes: int,
) -> FixedBatchEventContractFact:
    schema = DEFAULT_EVENT_SCHEMA_REGISTRY.latest_contract_for_type(event_type.value)
    payload = {
        "schema_version": "fixed_batch_event_contract.v1",
        "event_type": event_type.value,
        "event_schema_version": schema.event_schema_version,
        "event_schema_fingerprint": schema.event_schema_fingerprint,
        "minimum_occurrences": 0,
        "maximum_occurrences": maximum_occurrences,
        "max_candidate_payload_bytes_per_occurrence": maximum_payload_bytes,
    }
    return FixedBatchEventContractFact(
        **payload,
        event_contract_fingerprint=_own_fingerprint(
            "fixed-batch-event-contract:v1", payload
        ),
    )


def _build_charge_contract() -> PhysicalChargeContractFact:
    bookkeeping_envelope_bound = 256 * 1024
    charge_applied_envelope_bound = 256 * 1024
    charge_applied_base_charge = 7_680
    charge_applied_per_business_event_charge = 2_048
    bounds_payload = {
        "schema_version": "stored_envelope_identity_bounds.v1",
        "maximum_ledger_sequence": 9_999_999_999,
        "sequence_encoding": "unsigned_decimal",
        "max_sequence_encoded_bytes": 10,
        "max_event_id_utf8_bytes": 256,
        "max_runtime_session_id_utf8_bytes": 256,
        "max_run_id_utf8_bytes": 256,
        "max_turn_id_utf8_bytes": 256,
        "max_context_id_utf8_bytes": 256,
        "max_event_type_utf8_bytes": 128,
        "max_event_schema_version_utf8_bytes": 128,
        "max_created_at_utc_utf8_bytes": 64,
        "max_wrapper_metadata_canonical_bytes": 4_096,
    }
    bounds = StoredEnvelopeIdentityBoundsFact(
        **bounds_payload,
        bounds_contract_fingerprint=_own_fingerprint(
            "stored-envelope-identity-bounds:v1", bounds_payload
        ),
    )
    bookkeeping_types = tuple(
        sorted(
            (
                EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_INTENT,
                EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED,
                EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_FAILED,
                EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_CANCELLED,
                EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_RECOVERED_INTERRUPTED,
                EventType.LEDGER_MATERIALIZATION_ACCOUNT_GENESIS,
                EventType.LEDGER_MATERIALIZATION_CONSUMER_REGISTERED,
                EventType.LEDGER_MATERIALIZATION_CONSUMER_HORIZON_ADVANCED,
                EventType.LEDGER_MATERIALIZATION_CONSUMER_RETIRED,
                EventType.LEDGER_MATERIALIZATION_GENERATION_ADVANCED,
                EventType.PHYSICAL_OPERATION_RESERVATION_CREATED,
                EventType.PHYSICAL_OPERATION_CHARGE_APPLIED,
                EventType.PHYSICAL_OPERATION_RESERVATION_SUSPENDED,
                EventType.PHYSICAL_OPERATION_RESERVATION_SETTLED,
                EventType.CHECKPOINT_DISPATCH_BARRIER_INSTALLED,
                EventType.CHECKPOINT_DISPATCH_BARRIER_RELEASED,
            ),
            key=lambda item: item.value,
        )
    )
    bookkeeping_bounds = []
    for event_type in bookkeeping_types:
        schema = DEFAULT_EVENT_SCHEMA_REGISTRY.latest_contract_for_type(
            event_type.value
        )
        envelope_bound = (
            charge_applied_envelope_bound
            if event_type is EventType.PHYSICAL_OPERATION_CHARGE_APPLIED
            else bookkeeping_envelope_bound
        )
        bound_payload = {
            "schema_version": "physical_bookkeeping_event_bound.v1",
            "event_type": event_type.value,
            "event_schema_version": schema.event_schema_version,
            "event_schema_fingerprint": schema.event_schema_fingerprint,
            "max_payload_canonical_bytes": 1024 * 1024,
            "max_stored_envelope_bytes": envelope_bound,
        }
        bookkeeping_bounds.append(
            PhysicalBookkeepingEventBoundFact(
                **bound_payload,
                bound_fingerprint=_own_fingerprint(
                    "physical-bookkeeping-event-bound:v1", bound_payload
                ),
            )
        )
    payload = {
        "schema_version": "physical_charge_contract.v1",
        "contract_id": "pulsara.physical_charge",
        "contract_version": "1",
        "candidate_payload_canonicalization_fingerprint": (
            _CANONICAL_EVENT_SERIALIZATION_CONTRACT_FINGERPRINT
        ),
        "stored_envelope_identity_bounds": bounds,
        "bookkeeping_event_bounds": tuple(bookkeeping_bounds),
        "stored_envelope_bounds_contract_fingerprint": bounds.bounds_contract_fingerprint,
        "fixed_sequence_wrapper_charge_bytes_per_event": 1_024,
        "fixed_schema_wrapper_charge_bytes_per_event": 1_024,
        "reservation_bookkeeping_charge_events": 1,
        "reservation_bookkeeping_charge_bytes": bookkeeping_envelope_bound,
        "charge_applied_bookkeeping_charge_events": 1,
        "charge_applied_bookkeeping_base_charge_bytes": (
            charge_applied_base_charge
        ),
        "charge_applied_bookkeeping_per_business_event_charge_bytes": (
            charge_applied_per_business_event_charge
        ),
        "suspension_bookkeeping_charge_events": 1,
        "suspension_bookkeeping_charge_bytes": bookkeeping_envelope_bound,
        "settlement_bookkeeping_charge_events": 1,
        "settlement_bookkeeping_charge_bytes": bookkeeping_envelope_bound,
        "operational_observation_excluded_from_settlement": True,
    }
    return PhysicalChargeContractFact(
        **payload,
        contract_fingerprint=_own_fingerprint("physical-charge-contract:v1", payload),
    )


def _build_limits() -> AuthorityMaterializationLimits:
    payload = {
        "schema_version": "authority_materialization_limits.v2",
        "max_unreclaimable_ledger_events": 65_536,
        "max_unreclaimable_charged_payload_bytes": 256 * 1024 * 1024,
        "max_active_materialization_consumers": 64,
        "max_active_physical_reservations": 64,
        "soft_reclaim_pressure_events": 32_768,
        "soft_reclaim_pressure_payload_bytes": 128 * 1024 * 1024,
        "maintenance_reserved_events": 20_000,
        "maintenance_reserved_payload_bytes": 40 * 1024 * 1024,
        "max_active_projection_entries": 1_000_000,
        "max_checkpoint_root_bytes": 256 * 1024,
        "max_checkpoint_node_bytes": 512 * 1024,
        "max_checkpoint_changed_leaves_per_operation": 64,
        "max_checkpoint_changed_nodes_per_operation": 256,
        "max_checkpoint_nodes_per_artifact_batch": 32,
        "max_normalized_message_content_artifact_bytes": 16 * 1024 * 1024,
        "max_changed_message_content_artifacts_per_operation": 64,
        "max_checkpoint_total_artifact_bytes_per_operation": 32 * 1024 * 1024,
        "max_checkpoint_artifact_batches_per_operation": 16,
        "checkpoint_operation_timeout_seconds": 30.0,
        "max_named_authority_events": 4_096,
        "max_named_authority_payload_bytes": 16 * 1024 * 1024,
        "max_model_stream_source_items_per_call": (
            MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL
        ),
        "max_model_stream_recovery_events": (
            MAX_MODEL_STREAM_STRUCTURAL_TAIL_EVENTS
        ),
        "max_model_stream_recovery_payload_bytes": (
            MAX_MODEL_STREAM_STRUCTURAL_TAIL_PAYLOAD_BYTES
        ),
        "operation_timeout_seconds": 30.0,
        "reservation_wait_timeout_seconds": 30.0,
    }
    return AuthorityMaterializationLimits(
        **payload,
        limits_contract_fingerprint=_own_fingerprint(
            "authority-materialization-limits:v2", payload
        ),
    )


def _register_binding(
    registry: PhysicalBurstContractRegistry,
    contract: PhysicalBurstContractFact,
) -> None:
    registry.register(
        PhysicalBurstContractBinding(
            contract=contract,
            implementation_build_fingerprint="builtin-physical-burst:ap0-v1",
        )
    )


def _transport_contract(
    *,
    event_domain_fingerprint: str,
    charge_fingerprint: str,
) -> TransportSegmentedBurstContractFact:
    max_source_items = MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL
    max_durable_events = max_source_items + 1
    max_commit_batches = 1 + max_durable_events + 1 + 2
    bookkeeping_base_bytes = 7_680
    bookkeeping_per_business_event_bytes = 2_048
    payload = {
        "schema_version": "transport_segmented_burst_contract.v1",
        "burst_shape": "transport_segmented",
        "contract_id": "pulsara.model_stream.default",
        "contract_version": "2",
        "operation_kind": PhysicalOperationKind.MODEL_CALL,
        "max_commit_batches": max_commit_batches,
        "max_structural_tail_events": 128,
        "max_structural_tail_payload_bytes": 9 * 1024 * 1024,
        "max_terminal_recovery_events": MAX_MODEL_STREAM_STRUCTURAL_TAIL_EVENTS,
        "max_terminal_recovery_payload_bytes": (
            MAX_MODEL_STREAM_STRUCTURAL_TAIL_PAYLOAD_BYTES
        ),
        "terminal_tail_reserved_events": 128,
        "terminal_tail_reserved_payload_bytes": 9 * 1024 * 1024,
        "max_total_reserved_events": (
            max_durable_events
            + max_commit_batches
            + 128
            + MAX_MODEL_STREAM_STRUCTURAL_TAIL_EVENTS
        ),
        "max_total_reserved_payload_bytes": (
            MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL
            + 64 * 1024
            + max_durable_events * 2_048
            + max_commit_batches * bookkeeping_base_bytes
            + max_durable_events * bookkeeping_per_business_event_bytes
            + 9 * 1024 * 1024
            + MAX_MODEL_STREAM_STRUCTURAL_TAIL_PAYLOAD_BYTES
        ),
        "event_domain_registry_contract_fingerprint": event_domain_fingerprint,
        "canonical_event_serialization_contract_fingerprint": (
            _CANONICAL_EVENT_SERIALIZATION_CONTRACT_FINGERPRINT
        ),
        "physical_charge_contract_fingerprint": charge_fingerprint,
        "segmentation_mode": "contiguous_model_delta_segment_v1",
        "max_source_items": MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL,
        "max_source_payload_bytes": (
            MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL
        ),
        "max_single_source_item_canonical_bytes": (
            DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT
            .max_single_source_item_canonical_bytes
        ),
        "max_segment_source_items": (
            DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT.max_segment_source_items
        ),
        "max_segment_content_utf8_bytes": (
            DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT.max_content_utf8_bytes
        ),
        "max_segment_canonical_event_bytes": (
            DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT.max_canonical_event_bytes
        ),
        "max_durable_event_wrapper_overhead_bytes": 2_048,
        "max_unconfirmed_age_millis": (
            DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT
            .max_unconfirmed_age_millis
        ),
        "max_durable_events_per_source_item": 1,
        "max_synthetic_semantic_tail_events": 1,
        "max_synthetic_semantic_tail_payload_bytes": 64 * 1024,
        "max_start_commit_batches": 1,
        "max_terminal_commit_batches": 1,
        "max_recovery_commit_batches": 2,
        "max_bookkeeping_events_per_commit": 1,
        "max_bookkeeping_base_payload_bytes_per_commit": bookkeeping_base_bytes,
        "max_bookkeeping_payload_bytes_per_business_event": (
            bookkeeping_per_business_event_bytes
        ),
        "segment_policy_contract_fingerprint": (
            DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT.contract_fingerprint
        ),
        "sanitization_contract_fingerprint": context_fingerprint(
            "provider-semantic-source-sanitization:v1",
            "SanitizingLLMTransport+finite-source-circuit-breaker",
        ),
    }
    return TransportSegmentedBurstContractFact(
        **payload,
        contract_fingerprint=_own_fingerprint(
            "transport-segmented-burst-contract:v1", payload
        ),
    )


def _tool_contract(
    *, event_domain_fingerprint: str, charge_fingerprint: str
) -> ToolDeltaBurstContractFact:
    payload = {
        "schema_version": "tool_delta_burst_contract.v1",
        "burst_shape": "tool_delta",
        "contract_id": "pulsara.tool_result.default",
        "contract_version": "1",
        "operation_kind": PhysicalOperationKind.TOOL_CALL,
        "max_commit_batches": 64,
        "max_structural_tail_events": 136,
        "max_structural_tail_payload_bytes": 10 * 1024 * 1024,
        "max_terminal_recovery_events": 8,
        "max_terminal_recovery_payload_bytes": 256 * 1024,
        "terminal_tail_reserved_events": 32,
        "terminal_tail_reserved_payload_bytes": 2 * 1024 * 1024,
        "max_total_reserved_events": 4_240,
        "max_total_reserved_payload_bytes": 36 * 1024 * 1024,
        "event_domain_registry_contract_fingerprint": event_domain_fingerprint,
        "canonical_event_serialization_contract_fingerprint": (
            _CANONICAL_EVENT_SERIALIZATION_CONTRACT_FINGERPRINT
        ),
        "physical_charge_contract_fingerprint": charge_fingerprint,
        "max_result_delta_items": 4_096,
        "max_result_delta_payload_bytes": 16 * 1024 * 1024,
        "max_durable_events_per_delta_item": 1,
        "max_canonical_wrapper_payload_bytes_per_delta_item": 2_048,
        "result_capture_contract_fingerprint": context_fingerprint(
            "tool-result-capture-contract:v1", "typed-tool-result-capture"
        ),
        "artifact_fallback_contract_fingerprint": context_fingerprint(
            "tool-result-artifact-fallback:v1", "content-addressed-artifact"
        ),
    }
    return ToolDeltaBurstContractFact(
        **payload,
        contract_fingerprint=_own_fingerprint(
            "tool-delta-burst-contract:v1", payload
        ),
    )


def _fixed_contract(
    *,
    operation_kind: PhysicalOperationKind,
    event_types: Iterable[EventType],
    event_domain_fingerprint: str,
    charge_fingerprint: str,
    max_business_events: int = 64,
    max_business_bytes: int = 4 * 1024 * 1024,
) -> FixedBatchBurstContractFact:
    event_types = tuple(sorted(set(event_types), key=lambda item: item.value))
    per_type_max = max(1, max_business_events // max(1, len(event_types)))
    contracts = tuple(
        _fixed_event_contract(
            event_type,
            maximum_occurrences=per_type_max,
            maximum_payload_bytes=max_business_bytes,
        )
        for event_type in event_types
    )
    batch_fp = context_fingerprint(
        "fixed-batch-matrix:v1",
        tuple(item.event_contract_fingerprint for item in contracts),
    )
    max_commit_batches = 4
    structural_tail_bytes = max(
        2 * 1024 * 1024,
        3 * 256 * 1024
        + max_commit_batches * 7_680
        + max_business_events * 2_048,
    )
    payload = {
        "schema_version": "fixed_batch_burst_contract.v1",
        "burst_shape": "fixed_batch",
        "contract_id": f"pulsara.fixed.{operation_kind.value}",
        "contract_version": "1",
        "operation_kind": operation_kind,
        "max_commit_batches": max_commit_batches,
        "max_structural_tail_events": 12,
        "max_structural_tail_payload_bytes": structural_tail_bytes,
        "max_terminal_recovery_events": 4,
        "max_terminal_recovery_payload_bytes": 256 * 1024,
        "terminal_tail_reserved_events": (
            0 if operation_kind is PhysicalOperationKind.LEDGER_GENESIS else 8
        ),
        "terminal_tail_reserved_payload_bytes": (
            0
            if operation_kind is PhysicalOperationKind.LEDGER_GENESIS
            else 512 * 1024
        ),
        "max_total_reserved_events": max_business_events + 16,
        "max_total_reserved_payload_bytes": (
            max_business_bytes + structural_tail_bytes + 256 * 1024
        ),
        "event_domain_registry_contract_fingerprint": event_domain_fingerprint,
        "canonical_event_serialization_contract_fingerprint": (
            _CANONICAL_EVENT_SERIALIZATION_CONTRACT_FINGERPRINT
        ),
        "physical_charge_contract_fingerprint": charge_fingerprint,
        "max_business_events": max_business_events,
        "max_business_candidate_payload_bytes": max_business_bytes,
        "batch_event_contracts": contracts,
        "batch_contract_fingerprint": batch_fp,
    }
    return FixedBatchBurstContractFact(
        **payload,
        contract_fingerprint=_own_fingerprint(
            "fixed-batch-burst-contract:v1", payload
        ),
    )


def build_default_authority_materialization_contract_bundle(
) -> AuthorityMaterializationContractBundle:
    domain = build_default_transcript_event_domain_registry_binding()
    charge = _build_charge_contract()
    limits = _build_limits()
    registry = PhysicalBurstContractRegistry()
    _register_binding(
        registry,
        _transport_contract(
            event_domain_fingerprint=domain.contract.registry_contract_fingerprint,
            charge_fingerprint=charge.contract_fingerprint,
        ),
    )
    _register_binding(
        registry,
        _tool_contract(
            event_domain_fingerprint=domain.contract.registry_contract_fingerprint,
            charge_fingerprint=charge.contract_fingerprint,
        ),
    )
    fixed_profiles = {
        PhysicalOperationKind.LEDGER_GENESIS: (EventType.RUN_START,),
        PhysicalOperationKind.EXTERNAL_EXECUTION: (
            EventType.REQUIRE_EXTERNAL_EXECUTION,
            EventType.TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED,
            EventType.EXTERNAL_EXECUTION_RESULT,
        ),
        PhysicalOperationKind.MCP_RESUME: (
            EventType.TOOL_EXECUTION_SUSPENDED,
            EventType.TOOL_RESULT_END,
        ),
        PhysicalOperationKind.CHILD_PARENT_GRAPH_WRITE: (
            EventType.SUBAGENT_RUN_STARTED,
            EventType.SUBAGENT_MESSAGE_SENT,
            EventType.SUBAGENT_RUN_SUSPENDED,
            EventType.SUBAGENT_RUN_COMPLETED,
            EventType.SUBAGENT_RUN_FAILED,
            EventType.SUBAGENT_RUN_CANCELLED,
            EventType.SUBAGENT_EDGE_RECORDED,
            EventType.SUBAGENT_RESULT_DELIVERED,
            EventType.SUBAGENT_TASK_CREATED,
            EventType.SUBAGENT_TASK_SCHEDULED,
            EventType.SUBAGENT_TASK_STARTED,
            EventType.SUBAGENT_TASK_BLOCKED,
            EventType.SUBAGENT_TASK_COMPLETED,
            EventType.SUBAGENT_TASK_FAILED,
            EventType.SUBAGENT_TASK_CANCELLED,
            EventType.SUBAGENT_PHASE_REPORTED,
            EventType.SUBAGENT_RESULT_SUBMITTED,
            EventType.SUBAGENT_RESULT_CONSUMED,
            EventType.SUBAGENT_ROLLOUT_BUDGET_RESOLVED,
            EventType.ROLLOUT_BUDGET_RESERVATION_CREATED,
            EventType.ROLLOUT_BUDGET_RESERVATION_SETTLED,
            EventType.CHILD_ROLLOUT_SUBACCOUNT_CLOSED,
        ),
        PhysicalOperationKind.HOST_RUN_BOUNDARY: (
            EventType.RUN_START,
            EventType.RUN_END,
            EventType.RUN_INTERACTION_RESUME_BOUNDARY,
            EventType.CAPABILITY_EXPOSURE_RESOLVED,
            EventType.MCP_CAPABILITY_SNAPSHOT_INSTALLED,
            EventType.CONTEXT_WINDOW_OPENED,
            EventType.CONTEXT_WINDOW_CLOSED,
            EventType.ROLLOUT_BUDGET_ACCOUNT_OPENED,
            EventType.ROLLOUT_BUDGET_ACCOUNT_CLOSED,
        ),
        PhysicalOperationKind.CHECKPOINT_COMMIT: (
            EventType.SUBAGENT_GRAPH_CHECKPOINT_COMMITTED,
            EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_INTENT,
            EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED,
            EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_FAILED,
            EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_CANCELLED,
            EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_RECOVERED_INTERRUPTED,
            EventType.LEDGER_MATERIALIZATION_CONSUMER_HORIZON_ADVANCED,
            EventType.LEDGER_MATERIALIZATION_GENERATION_ADVANCED,
            EventType.PHYSICAL_OPERATION_RESERVATION_CREATED,
            EventType.PHYSICAL_OPERATION_RESERVATION_SETTLED,
            EventType.CHECKPOINT_DISPATCH_BARRIER_INSTALLED,
            EventType.CHECKPOINT_DISPATCH_BARRIER_RELEASED,
        ),
        PhysicalOperationKind.RUNTIME_INTERNAL_WRITE: tuple(
            event_type
            for event_type in EventType
            if event_type
            not in {
                EventType.LEDGER_MATERIALIZATION_ACCOUNT_GENESIS,
                EventType.LEDGER_MATERIALIZATION_CONSUMER_REGISTERED,
                EventType.LEDGER_MATERIALIZATION_CONSUMER_HORIZON_ADVANCED,
                EventType.LEDGER_MATERIALIZATION_CONSUMER_RETIRED,
                EventType.LEDGER_MATERIALIZATION_GENERATION_ADVANCED,
                EventType.PHYSICAL_OPERATION_RESERVATION_CREATED,
                EventType.PHYSICAL_OPERATION_CHARGE_APPLIED,
                EventType.PHYSICAL_OPERATION_RESERVATION_SUSPENDED,
                EventType.PHYSICAL_OPERATION_RESERVATION_SETTLED,
                EventType.CHECKPOINT_DISPATCH_BARRIER_INSTALLED,
                EventType.CHECKPOINT_DISPATCH_BARRIER_RELEASED,
            }
        ),
    }
    for kind, event_types in fixed_profiles.items():
        if kind is PhysicalOperationKind.CHECKPOINT_COMMIT:
            fixed_contract_options = {
                "max_business_events": 16,
                "max_business_bytes": 20 * 1024 * 1024,
            }
        elif kind is PhysicalOperationKind.RUNTIME_INTERNAL_WRITE:
            # The fallback contract explicitly enumerates every ordinary event
            # schema. Keep one full semantic batch per schema so the transitional
            # one-shot path remains finite without rejecting a homogeneous
            # 16-item model/tool batch before AP4 gives it a retained owner.
            fixed_contract_options = {
                "max_business_events": len(event_types) * 16,
            }
        else:
            fixed_contract_options = {}
        _register_binding(
            registry,
            _fixed_contract(
                operation_kind=kind,
                event_types=event_types,
                event_domain_fingerprint=domain.contract.registry_contract_fingerprint,
                charge_fingerprint=charge.contract_fingerprint,
                **fixed_contract_options,
            ),
        )
    bundle = AuthorityMaterializationContractBundle(
        event_domain=domain,
        charge_contract=charge,
        limits=limits,
        burst_registry=registry,
    )
    AuthorityMaterializationContractDoctor().verify(bundle)
    return bundle


__all__ = [
    "AuthorityMaterializationContractBundle",
    "AuthorityMaterializationContractDoctor",
    "AuthorityMaterializationDoctorReport",
    "PhysicalBurstContractBinding",
    "PhysicalBurstContractRegistry",
    "TranscriptEventDomainRegistryBinding",
    "build_default_authority_materialization_contract_bundle",
    "build_default_transcript_event_domain_registry_binding",
    "materialize_transcript_sparse_read_proof",
]
