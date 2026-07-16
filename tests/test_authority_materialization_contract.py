from __future__ import annotations

import asyncio
from hashlib import sha256
from time import monotonic
from typing import Any, TypeVar

import pytest
from pydantic import TypeAdapter, ValidationError
from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.primitives.context import ToolArgumentsParseErrorCode
from pulsara_agent.event import (
    CustomEvent,
    EventContext,
    LedgerMaterializationConsumerHorizonAdvancedEvent,
    PlanExitResolvedEvent,
    TerminalProcessCompletedEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
)
from pulsara_agent.event_log import (
    InMemoryEventLog,
    MaterializationAccountStateConflict,
)
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    canonical_event_payload_bytes,
)
from pulsara_agent.llm.drafts import ProviderErrorDraft, ProviderTransportTerminalDraft
from pulsara_agent.llm.sanitizing_transport import SanitizingProviderTransportExecution
from pulsara_agent.primitives import (
    DURABLE_FACT_FINGERPRINT_REGISTRY,
    ActivePhysicalReservationStateFact,
    AuthorityMaterializationLimits,
    FixedBatchBurstContractFact,
    LedgerMaterializationAccountStateFact,
    LedgerMaterializationConsumerHorizonFact,
    LedgerMaterializationConsumerKind,
    LedgerMaterializationGenerationFact,
    ModelCallSemanticSourceFact,
    ModelProjectionItemFact,
    ModelTerminalProjectionPayloadFact,
    ModelTerminalProjectionSemanticFact,
    ModelTextBlockSemanticFact,
    ModelToolCallBlockSemanticFact,
    PhysicalBurstContractFact,
    PhysicalOperationKind,
    PhysicalOperationReservationFact,
    TranscriptEventDomainRegistryContractFact,
    TranscriptDomainSparseReadProofFact,
    TranscriptProjectionLiveAssemblyState,
    TranscriptProjectionStableSemanticStateFact,
    TranscriptSemanticEventContractFact,
    TransportFragmentedBurstContractFact,
    StableEventIdentityFact,
    TerminalContentSemanticFact,
    TerminalInlineContentFact,
    TerminalProjectionDocumentFact,
    context_fingerprint,
)
from pulsara_agent.runtime.authority_materialization import (
    AuthorityMaterializationContractDoctor,
    AuthorityMaterializationContractBundle,
    CheckpointDispatchBarrierActive,
    account_with_committed_usage,
    build_default_authority_materialization_contract_bundle,
    canonical_empty_account,
    commit_checkpoint_success,
    commit_checkpoint_cancellation,
    commit_checkpoint_failure,
    commit_checkpoint_recovered_interrupted,
    LedgerMaterializationAccountStore,
    LedgerMaterializationCoordinator,
    PhysicalBurstContractBinding,
    PhysicalBurstContractRegistry,
    build_default_checkpoint_terminal_contract,
    install_checkpoint_barrier,
    prepare_run_transcript_seed,
    prepare_transcript_checkpoint_candidate,
    materialize_transcript_sparse_read_proof,
    persist_prepared_transcript_projection_materialization,
    prepare_authority_artifact_write_reservation,
    restore_transcript_projection,
)
from pulsara_agent.runtime.long_horizon.checkpoint import (
    prepare_subagent_graph_checkpoint,
)
from pulsara_agent.runtime.long_horizon.reducer_contract import (
    build_default_subagent_graph_reducer_binding,
)


T = TypeVar("T")


def _fingerprint(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def _fact(model: type[T], /, **payload: Any) -> T:
    spec = DURABLE_FACT_FINGERPRINT_REGISTRY.resolve(payload["schema_version"])
    assert spec.own_fingerprint_field is not None
    candidate = model.model_construct(
        **payload,
        **{spec.own_fingerprint_field: "pending"},
    )
    payload[spec.own_fingerprint_field] = context_fingerprint(
        spec.domain_separator,
        candidate.model_dump(
            mode="json",
            exclude={spec.own_fingerprint_field},
        ),
    )
    return model(**payload)


def _transport_payload() -> dict[str, Any]:
    return {
        "schema_version": "transport_fragmented_burst_contract.v1",
        "burst_shape": "transport_fragmented",
        "contract_id": "model-stream",
        "contract_version": "1",
        "operation_kind": PhysicalOperationKind.MODEL_CALL,
        "max_commit_batches": 4,
        "max_structural_tail_events": 3,
        "max_structural_tail_payload_bytes": 300,
        "max_terminal_recovery_events": 2,
        "max_terminal_recovery_payload_bytes": 200,
        "terminal_tail_reserved_events": 2,
        "terminal_tail_reserved_payload_bytes": 200,
        "max_total_reserved_events": 15,
        "max_total_reserved_payload_bytes": 1_500,
        "event_domain_registry_contract_fingerprint": _fingerprint("domain"),
        "canonical_event_serialization_contract_fingerprint": _fingerprint(
            "serialization"
        ),
        "physical_charge_contract_fingerprint": _fingerprint("charge"),
        "fragmentation_mode": "one_event_per_sanitized_source_item",
        "max_source_items": 10,
        "max_sanitized_source_payload_bytes": 800,
        "max_durable_events_per_source_item": 1,
        "max_canonical_wrapper_payload_bytes_per_source_item": 20,
        "sanitization_contract_fingerprint": _fingerprint("sanitizer"),
    }


def _stable_state() -> TranscriptProjectionStableSemanticStateFact:
    return _fact(
        TranscriptProjectionStableSemanticStateFact,
        schema_version="transcript_projection_stable_semantic_state.v1",
        semantic_source_event_count=0,
        semantic_source_accumulator=_fingerprint("empty-semantic"),
        normalized_transcript_fingerprint=_fingerprint("empty-transcript"),
    )


def _stable_event(event_id: str) -> StableEventIdentityFact:
    return _fact(
        StableEventIdentityFact,
        schema_version="stable_event_identity.v2",
        runtime_session_id="runtime:1",
        event_id=event_id,
        event_type="MODEL_CALL_START",
        event_schema_version="model-call-start.v1",
        event_schema_fingerprint=_fingerprint("model-call-start-schema"),
        payload_fingerprint=_fingerprint(f"payload:{event_id}"),
    )


def _model_document(*, completion_status: str = "completed") -> object:
    text = "hello"
    content_semantic = _fact(
        TerminalContentSemanticFact,
        schema_version="terminal_content_semantic.v2",
        canonical_content_sha256=_fingerprint(text),
        utf8_bytes=len(text.encode("utf-8")),
        media_type="text/plain; charset=utf-8",
        content_canonicalization_contract_fingerprint=_fingerprint("content-contract"),
    )
    content = _fact(
        TerminalInlineContentFact,
        schema_version="terminal_inline_content.v2",
        storage_kind="inline",
        semantic_identity=content_semantic,
        text=text,
    )
    semantic = _fact(
        ModelTextBlockSemanticFact,
        schema_version="model_text_block_semantic.v1",
        block_kind="text",
        block_id="block:1",
        block_index=0,
        projection_order=0,
        completion_status=completion_status,
        content_semantic_identity=content_semantic,
    )
    item = _fact(
        ModelProjectionItemFact,
        schema_version="model_projection_item.v2",
        semantic_identity=semantic,
        content=content,
        source_start_sequence=2,
        source_end_sequence=3 if completion_status == "completed" else None,
        provider_error=None,
    )
    model_semantic = _fact(
        ModelTerminalProjectionSemanticFact,
        schema_version="model_terminal_projection_semantic.v1",
        projection_kind="model_call",
        terminal_outcome="completed",
        ordered_item_semantic_fingerprints=(semantic.semantic_fingerprint,),
    )
    payload = ModelTerminalProjectionPayloadFact(
        schema_version="model_terminal_projection_payload.v2",
        projection_kind="model_call",
        items=(item,),
    )
    source = _fact(
        ModelCallSemanticSourceFact,
        schema_version="model_call_semantic_source.v1",
        resolved_model_call_id="call:1",
        model_call_start_event_identity=_stable_event("event:start"),
        source_semantic_item_count=1,
        source_first_transport_index=0,
        source_last_transport_index=0,
        source_semantic_accumulator=_fingerprint("model-source"),
        model_stream_semantic_domain_contract_fingerprint=_fingerprint("model-domain"),
        reducer_contract_fingerprint=_fingerprint("model-reducer"),
    )
    return _fact(
        TerminalProjectionDocumentFact,
        schema_version="terminal_projection_document.v2",
        document_contract_fingerprint=_fingerprint("document-contract"),
        semantic_identity=model_semantic,
        payload=payload,
        source_fact=source,
        usage_status="missing",
        usage=None,
        reported_model_id=None,
        tool_result_artifact_refs=(),
    )


def _generation(
    horizons: tuple[LedgerMaterializationConsumerHorizonFact, ...] = (),
) -> LedgerMaterializationGenerationFact:
    minimum = min((item.through_sequence for item in horizons), default=0)
    return _fact(
        LedgerMaterializationGenerationFact,
        schema_version="ledger_materialization_generation.v1",
        runtime_session_id="runtime:1",
        ledger_materialization_generation=0,
        consumer_horizon_revision=0,
        consumer_horizons=horizons,
        active_consumer_set_fingerprint=_fingerprint("consumer-set"),
        reclaimable_through_sequence=minimum,
        reclaimable_event_count_through=minimum,
        reclaimable_charged_payload_bytes_through=minimum * 10,
        ledger_continuity_accumulator_through_reclaimable=_fingerprint(
            f"ledger:{minimum}"
        ),
        physical_charge_contract_fingerprint=_fingerprint("charge"),
    )


def test_transport_burst_contract_proves_finite_worst_case() -> None:
    contract = _fact(TransportFragmentedBurstContractFact, **_transport_payload())

    assert contract.max_total_reserved_events == 15
    parsed = TypeAdapter(PhysicalBurstContractFact).validate_python(
        contract.model_dump(mode="json")
    )
    assert isinstance(parsed, TransportFragmentedBurstContractFact)


def test_transport_burst_contract_rejects_underestimated_tail() -> None:
    payload = _transport_payload()
    payload["max_total_reserved_events"] = 14

    with pytest.raises(ValidationError, match="event reservation is underestimated"):
        _fact(TransportFragmentedBurstContractFact, **payload)


def test_burst_max_commit_batches_covers_charge_applied_transitions() -> None:
    bundle = build_default_authority_materialization_contract_bundle()
    model_binding = bundle.burst_registry.unique_binding_for_operation(
        PhysicalOperationKind.MODEL_CALL
    )
    contract = model_binding.contract
    minimum_bytes = (
        bundle.charge_contract.reservation_bookkeeping_charge_bytes
        + contract.max_commit_batches
        * bundle.charge_contract.charge_applied_bookkeeping_charge_bytes
        + bundle.charge_contract.suspension_bookkeeping_charge_bytes
        + bundle.charge_contract.settlement_bookkeeping_charge_bytes
    )
    assert contract.max_structural_tail_payload_bytes >= minimum_bytes

    payload = contract.model_dump(mode="python", exclude={"contract_fingerprint"})
    payload["max_structural_tail_payload_bytes"] = minimum_bytes - 1
    payload["max_total_reserved_payload_bytes"] = max(
        payload["max_total_reserved_payload_bytes"],
        payload["max_sanitized_source_payload_bytes"]
        + payload["max_source_items"]
        * payload["max_canonical_wrapper_payload_bytes_per_source_item"]
        + payload["max_structural_tail_payload_bytes"]
        + payload["max_terminal_recovery_payload_bytes"],
    )
    drifted = _fact(TransportFragmentedBurstContractFact, **payload)
    registry = PhysicalBurstContractRegistry()
    for binding in bundle.burst_registry.bindings():
        registry.register(
            PhysicalBurstContractBinding(
                contract=(
                    drifted
                    if binding.contract.operation_kind
                    is PhysicalOperationKind.MODEL_CALL
                    else binding.contract
                ),
                implementation_build_fingerprint=(
                    binding.implementation_build_fingerprint
                ),
            )
        )
    drifted_bundle = AuthorityMaterializationContractBundle(
        event_domain=bundle.event_domain,
        charge_contract=bundle.charge_contract,
        limits=bundle.limits,
        burst_registry=registry,
    )
    with pytest.raises(
        ValueError,
        match="commit batches exceed structural byte quote",
    ):
        AuthorityMaterializationContractDoctor().verify(drifted_bundle)


def test_charge_applied_bound_covers_max_identity_semantic_batch() -> None:
    bundle = build_default_authority_materialization_contract_bundle()
    runtime_session_id = "r" * 128
    event_log = InMemoryEventLog(runtime_session_id=runtime_session_id)
    store = LedgerMaterializationAccountStore(
        state=None,
        charge_contract=bundle.charge_contract,
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id=runtime_session_id,
        event_log=event_log,
        store=store,
        charge_contract=bundle.charge_contract,
        limits=bundle.limits,
    )
    context = EventContext(
        run_id="u" * 128,
        turn_id="t" * 128,
        reply_id="p" * 128,
    )
    coordinator.bootstrap_genesis(
        context=context,
        business_events=(
            CustomEvent(
                id="g" * 128,
                **context.event_fields(),
                name="genesis",
            ),
        ),
        genesis_profile="host_first_run",
        genesis_burst_contract=bundle.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.LEDGER_GENESIS
        ).contract,
        register_transcript_consumer=True,
    )
    admitted = coordinator.reserve_and_commit_dispatch(
        context=context,
        business_events=(
            CustomEvent(
                id="s" * 128,
                **context.event_fields(),
                name="dispatch",
            ),
        ),
        reservation_id="v" * 128,
        owner_id="o" * 128,
        burst_contract=bundle.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.MODEL_CALL
        ).contract,
    )
    charged = coordinator.commit_reserved_charge(
        context=context,
        reservation=admitted.reservation,
        business_events=tuple(
            CustomEvent(
                id=f"{index:04d}" + "e" * 124,
                **context.event_fields(),
                name="n" * 128,
            )
            for index in range(16)
        ),
    )
    actual_stored_charge = len(
        canonical_event_payload_bytes(charged.charge_event)
    ) + (
        bundle.charge_contract.fixed_sequence_wrapper_charge_bytes_per_event
        + bundle.charge_contract.fixed_schema_wrapper_charge_bytes_per_event
    )
    assert actual_stored_charge <= (
        bundle.charge_contract.charge_applied_bookkeeping_charge_bytes
    )


def test_fixed_batch_genesis_cannot_be_used_as_ordinary_reservation() -> None:
    burst = _fact(
        FixedBatchBurstContractFact,
        schema_version="fixed_batch_burst_contract.v1",
        burst_shape="fixed_batch",
        contract_id="genesis",
        contract_version="1",
        operation_kind=PhysicalOperationKind.LEDGER_GENESIS,
        max_commit_batches=1,
        max_structural_tail_events=0,
        max_structural_tail_payload_bytes=0,
        max_terminal_recovery_events=0,
        max_terminal_recovery_payload_bytes=0,
        terminal_tail_reserved_events=0,
        terminal_tail_reserved_payload_bytes=0,
        max_total_reserved_events=2,
        max_total_reserved_payload_bytes=2_048,
        event_domain_registry_contract_fingerprint=_fingerprint("domain"),
        canonical_event_serialization_contract_fingerprint=_fingerprint(
            "serialization"
        ),
        physical_charge_contract_fingerprint=_fingerprint("charge"),
        max_business_events=2,
        max_business_candidate_payload_bytes=2_048,
        batch_event_contracts=(),
        batch_contract_fingerprint=_fingerprint("genesis-batch"),
    )
    assert burst.operation_kind is PhysicalOperationKind.LEDGER_GENESIS

    reservation = {
        "schema_version": "physical_operation_reservation.v2",
        "reservation_id": "reservation:1",
        "runtime_session_id": "runtime:1",
        "owner_kind": "ledger_genesis",
        "owner_id": "genesis:1",
        "ledger_materialization_generation": 0,
        "consumer_horizon_revision": 0,
        "source_ledger_through_sequence": 0,
        "burst_contract_id": "genesis",
        "burst_contract_version": "1",
        "burst_contract_fingerprint": burst.contract_fingerprint,
        "physical_charge_contract_fingerprint": _fingerprint("charge"),
        "reserved_events": 2,
        "reserved_payload_bytes": 2_048,
        "terminal_tail_reserved_events": 0,
        "terminal_tail_reserved_payload_bytes": 0,
    }
    with pytest.raises(ValidationError):
        _fact(PhysicalOperationReservationFact, **reservation)


def test_transcript_domain_registry_is_discriminated_and_canonical() -> None:
    semantic = _fact(
        TranscriptSemanticEventContractFact,
        schema_version="transcript_semantic_event_contract.v1",
        event_type="MODEL_CALL_TERMINAL_PROJECTION_COMMITTED",
        event_schema_version="1",
        event_schema_fingerprint=_fingerprint("event-schema"),
        event_domain="transcript_semantic",
        event_domain_contract_fingerprint=_fingerprint("event-domain"),
        semantic_projection_contract_fingerprint=_fingerprint("projection"),
    )
    registry = _fact(
        TranscriptEventDomainRegistryContractFact,
        schema_version="transcript_event_domain_registry.v1",
        registry_id="transcript-domain",
        registry_version="1",
        supported_events=(semantic,),
        event_classification_contract_fingerprint=_fingerprint("classification"),
        transcript_semantic_domain_contract_fingerprint=_fingerprint("semantic"),
        transcript_prefix_accumulator_contract_fingerprint=_fingerprint("prefix"),
        ledger_continuity_accumulator_contract_fingerprint=_fingerprint("ledger"),
    )

    assert registry.supported_events == (semantic,)
    bad = semantic.model_dump(mode="json") | {
        "deterministic_noop_contract_fingerprint": _fingerprint("noop")
    }
    with pytest.raises(ValidationError):
        TypeAdapter(PhysicalBurstContractFact).validate_python(bad)


def test_checkpointable_live_assembly_rejects_pending_interaction() -> None:
    with pytest.raises(ValidationError, match="cannot contain pending state"):
        TranscriptProjectionLiveAssemblyState(
            schema_version="transcript_projection_live_assembly.v1",
            stable_semantic_state=_stable_state(),
            pending_model_projection_ids=("projection:1",),
            pending_model_disposition_call_ids=(),
            pending_assistant_tool_call_ids=(),
            pending_tool_result_projection_ids=(),
            pending_tool_pair_ids=(),
            suspended_tool_call_ids=(),
            pending_external_requirement_ids=(),
            ledger_through_sequence=1,
            ledger_continuity_accumulator=_fingerprint("ledger:1"),
            transcript_semantic_event_count=1,
            transcript_semantic_accumulator=_fingerprint("semantic:1"),
            checkpointable=True,
            assembly_fingerprint=_fingerprint("assembly"),
        )


def test_generation_uses_minimum_consumer_horizon() -> None:
    transcript = _fact(
        LedgerMaterializationConsumerHorizonFact,
        schema_version="ledger_materialization_consumer_horizon.v1",
        runtime_session_id="runtime:1",
        consumer_kind=LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW,
        consumer_id="transcript:run:window:0",
        business_run_id="run:1",
        business_window_id="window:1",
        business_window_generation=0,
        through_sequence=500,
        ledger_event_count_through=500,
        ledger_charged_payload_bytes_through=5_000,
        ledger_continuity_accumulator=_fingerprint("ledger:500"),
        consumer_contract_fingerprint=_fingerprint("transcript-consumer"),
    )
    graph = _fact(
        LedgerMaterializationConsumerHorizonFact,
        schema_version="ledger_materialization_consumer_horizon.v1",
        runtime_session_id="runtime:1",
        consumer_kind=LedgerMaterializationConsumerKind.SUBAGENT_GRAPH,
        consumer_id="subagent-graph:runtime:1",
        through_sequence=100,
        ledger_event_count_through=100,
        ledger_charged_payload_bytes_through=1_000,
        ledger_continuity_accumulator=_fingerprint("ledger:100"),
        consumer_contract_fingerprint=_fingerprint("graph-consumer"),
    )
    horizons = tuple(sorted((transcript, graph), key=lambda item: item.consumer_id))
    generation = _generation(horizons)

    assert generation.reclaimable_through_sequence == 100


def test_account_used_amount_is_derived_from_reclaimable_prefix() -> None:
    reservation = _fact(
        ActivePhysicalReservationStateFact,
        schema_version="active_physical_reservation_state.v1",
        reservation_id="reservation:1",
        owner_kind=PhysicalOperationKind.MODEL_CALL,
        owner_id="call:1",
        lifecycle_status="active",
        reservation_fingerprint=_fingerprint("reservation"),
        suspension_fingerprint=None,
        reserved_events_total=10,
        reserved_payload_bytes_total=100,
        charged_candidate_events_lifetime=1,
        charged_candidate_payload_bytes_lifetime=8,
        charged_wrapper_bytes_lifetime=2,
        charged_bookkeeping_events_lifetime=1,
        charged_bookkeeping_bytes_lifetime=10,
        charged_events_lifetime=2,
        charged_payload_bytes_lifetime=20,
        remaining_events=8,
        remaining_payload_bytes=80,
        latest_reservation_event_id="event:reservation",
        latest_lifecycle_event_id="event:reservation",
        latest_charge_applied_event_id=None,
    )
    generation = _generation()
    account = _fact(
        LedgerMaterializationAccountStateFact,
        schema_version="ledger_materialization_account_state.v1",
        runtime_session_id="runtime:1",
        generation=generation,
        ledger_through_sequence=2,
        ledger_event_count_through=2,
        ledger_charged_payload_bytes_through=20,
        used_since_reclaimable_events=2,
        used_since_reclaimable_payload_bytes=20,
        active_reservations=(reservation,),
        active_checkpoint_barrier=None,
        latest_transition_event_ids=("event:reservation",),
        reconciliation_required=False,
        reconciliation_reason_code=None,
    )
    assert account.used_since_reclaimable_events == 2


def test_authority_limits_keep_soft_pressure_below_hard_cap() -> None:
    payload = {
        "schema_version": "authority_materialization_limits.v2",
        "max_unreclaimable_ledger_events": 100,
        "max_unreclaimable_charged_payload_bytes": 100_000,
        "max_active_materialization_consumers": 8,
        "max_active_physical_reservations": 8,
        "soft_reclaim_pressure_events": 100,
        "soft_reclaim_pressure_payload_bytes": 80_000,
        "maintenance_reserved_events": 10,
        "maintenance_reserved_payload_bytes": 10_000,
        "max_active_projection_entries": 1_000,
        "max_checkpoint_root_bytes": 4_096,
        "max_checkpoint_node_bytes": 4_096,
        "max_checkpoint_changed_leaves_per_operation": 16,
        "max_checkpoint_changed_nodes_per_operation": 64,
        "max_checkpoint_nodes_per_artifact_batch": 16,
        "max_normalized_message_content_artifact_bytes": 1_000_000,
        "max_changed_message_content_artifacts_per_operation": 16,
        "max_checkpoint_total_artifact_bytes_per_operation": 1_000_000,
        "max_checkpoint_artifact_batches_per_operation": 8,
        "checkpoint_operation_timeout_seconds": 30.0,
        "max_named_authority_events": 1_000,
        "max_named_authority_payload_bytes": 1_000_000,
        "max_model_stream_source_items_per_call": 1_000,
        "max_model_stream_recovery_events": 1_100,
        "max_model_stream_recovery_payload_bytes": 1_000_000,
        "operation_timeout_seconds": 30.0,
        "reservation_wait_timeout_seconds": 5.0,
    }
    with pytest.raises(ValidationError, match="soft pressure"):
        _fact(AuthorityMaterializationLimits, **payload)


def test_terminal_inline_content_recomputes_sha_and_utf8_bytes() -> None:
    semantic = _fact(
        TerminalContentSemanticFact,
        schema_version="terminal_content_semantic.v2",
        canonical_content_sha256=_fingerprint("different"),
        utf8_bytes=5,
        media_type="text/plain; charset=utf-8",
        content_canonicalization_contract_fingerprint=_fingerprint("contract"),
    )

    with pytest.raises(ValidationError, match="SHA mismatch"):
        _fact(
            TerminalInlineContentFact,
            schema_version="terminal_inline_content.v2",
            storage_kind="inline",
            semantic_identity=semantic,
            text="hello",
        )


def test_completed_model_projection_rejects_interrupted_block() -> None:
    with pytest.raises(ValidationError, match="incomplete items"):
        _model_document(completion_status="interrupted")


def test_model_tool_arguments_keep_typed_parse_failure_state() -> None:
    with pytest.raises(ValidationError, match="parse error code mismatch"):
        _fact(
            ModelToolCallBlockSemanticFact,
            schema_version="model_tool_call_block_semantic.v1",
            block_kind="tool_call",
            block_id="block:tool",
            block_index=0,
            projection_order=0,
            tool_call_id="tool-call:1",
            tool_name="search",
            completion_status="completed",
            arguments_status="invalid_json",
            parsed_arguments=None,
            parse_error_code=ToolArgumentsParseErrorCode.JSON_ROOT_NOT_OBJECT,
            raw_arguments_json="{",
        )


def test_completed_model_projection_document_is_lossless_and_typed() -> None:
    document = _model_document()

    assert isinstance(document, TerminalProjectionDocumentFact)
    assert document.semantic_identity.terminal_outcome == "completed"
    assert document.usage_status == "missing"


def test_default_event_domain_registry_covers_every_current_schema() -> None:
    bundle = build_default_authority_materialization_contract_bundle()
    covered = {
        (item.event_type, item.event_schema_version)
        for item in bundle.event_domain.contract.supported_events
    }
    current = {
        (item.event_type, item.event_schema_version)
        for item in DEFAULT_EVENT_SCHEMA_REGISTRY.contracts()
    }

    assert covered == current


def test_default_authority_materialization_doctor_proves_all_operation_kinds() -> None:
    bundle = build_default_authority_materialization_contract_bundle()
    report = AuthorityMaterializationContractDoctor().verify(bundle)

    assert set(report.checked_operation_kinds) == {
        item.value for item in PhysicalOperationKind
    }
    assert report.checked_binding_count == 9


def test_transcript_sparse_prefix_proves_semantic_delta_without_full_ledger() -> None:
    event_log = InMemoryEventLog(runtime_session_id="runtime:sparse-proof")
    context = EventContext(run_id="run:1", turn_id="turn:1", reply_id="reply:1")
    event_log.append(TextBlockStartEvent(**context.event_fields(), block_id="block:1"))
    event_log.append(
        PlanExitResolvedEvent(
            **context.event_fields(),
            exit_request_id="plan-exit:1",
            tool_call_id="tool-call:1",
            decision="approve",
            user_feedback="",
        )
    )
    binding = build_default_authority_materialization_contract_bundle().event_domain
    raw = event_log.read_transcript_domain_delta(
        after_sequence=0,
        through_sequence=2,
        registry_contract_fingerprint=(binding.contract.registry_contract_fingerprint),
    )

    proof = materialize_transcript_sparse_read_proof(raw, binding=binding)

    assert isinstance(proof, TranscriptDomainSparseReadProofFact)
    assert proof.selected_transcript_semantic_event_count == 1
    assert raw.semantic_events[0].event_type == "PLAN_EXIT_RESOLVED"
    assert proof.prefix_through.ledger_event_count == 2


def test_transcript_sparse_prefix_has_canonical_empty_range() -> None:
    event_log = InMemoryEventLog(runtime_session_id="runtime:sparse-empty")
    context = EventContext(run_id="run:1", turn_id="turn:1", reply_id="reply:1")
    event_log.append(TextBlockStartEvent(**context.event_fields(), block_id="block:1"))
    binding = build_default_authority_materialization_contract_bundle().event_domain

    raw = event_log.read_transcript_domain_delta(
        after_sequence=1,
        through_sequence=1,
        registry_contract_fingerprint=(binding.contract.registry_contract_fingerprint),
    )
    proof = materialize_transcript_sparse_read_proof(raw, binding=binding)

    assert proof.range_kind == "empty"
    assert proof.from_sequence == 2
    assert proof.through_sequence == 1
    assert proof.prefix_before == proof.prefix_through


def test_sanitizing_transport_stops_at_source_item_circuit_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.llm.sanitizing_transport as transport_module

    monkeypatch.setattr(
        transport_module, "MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL", 1
    )
    context = EventContext(run_id="raw:run", turn_id="raw:turn", reply_id="raw:reply")

    async def raw_stream():
        yield TextBlockStartEvent(**context.event_fields(), block_id="block:1")
        yield TextBlockEndEvent(**context.event_fields(), block_id="block:1")

    execution = SanitizingProviderTransportExecution(
        raw_stream=raw_stream(), resolved_model_call_id="call:1"
    )

    async def collect():
        return (
            await execution.read_next(),
            await execution.read_next(),
            await execution.read_next(),
        )

    first, error, terminal = asyncio.run(collect())

    assert first is not None
    assert isinstance(error, ProviderErrorDraft)
    assert error.error.code == "transport_source_item_limit_exceeded"
    assert isinstance(terminal, ProviderTransportTerminalDraft)
    assert terminal.outcome == "provider_error"


def test_sanitizing_transport_stops_at_sanitized_byte_circuit_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.llm.sanitizing_transport as transport_module

    monkeypatch.setattr(
        transport_module, "MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL", 1
    )
    context = EventContext(run_id="raw:run", turn_id="raw:turn", reply_id="raw:reply")

    async def raw_stream():
        yield TextBlockStartEvent(**context.event_fields(), block_id="block:1")

    execution = SanitizingProviderTransportExecution(
        raw_stream=raw_stream(), resolved_model_call_id="call:1"
    )

    async def collect():
        return await execution.read_next(), await execution.read_next()

    error, terminal = asyncio.run(collect())

    assert isinstance(error, ProviderErrorDraft)
    assert error.error.code == "transport_source_payload_limit_exceeded"
    assert isinstance(terminal, ProviderTransportTerminalDraft)
    assert terminal.outcome == "provider_error"


def test_event_batch_and_materialization_account_commit_atomically() -> None:
    contracts = build_default_authority_materialization_contract_bundle()
    event_log = InMemoryEventLog(runtime_session_id="runtime:account")
    source = canonical_empty_account(
        runtime_session_id="runtime:account",
        charge_contract_fingerprint=contracts.charge_contract.contract_fingerprint,
    )
    event = CustomEvent(
        id="event:account-business",
        run_id="run:account",
        turn_id="turn:account",
        reply_id="reply:account",
        name="account-business",
    )
    resulting = account_with_committed_usage(
        source,
        events=(event,),
        charge_contract=contracts.charge_contract,
    )

    stored = event_log.extend_with_materialization_state(
        (event,),
        expected_account_state_fingerprint=None,
        resulting_account_state=resulting,
        physical_charge_contract=contracts.charge_contract,
        expected_last_sequence=0,
    )

    assert stored[0].sequence == 1
    assert event_log.read_materialization_account_state() == resulting

    with pytest.raises(MaterializationAccountStateConflict):
        event_log.extend_with_materialization_state(
            (
                event.model_copy(
                    update={"id": "event:stale-account-write", "sequence": None}
                ),
            ),
            expected_account_state_fingerprint=None,
            resulting_account_state=resulting,
            physical_charge_contract=contracts.charge_contract,
            expected_last_sequence=1,
        )


def test_genesis_and_dispatch_reservation_share_one_account_cas() -> None:
    contracts = build_default_authority_materialization_contract_bundle()
    event_log = InMemoryEventLog(runtime_session_id="runtime:coordinator")
    store = LedgerMaterializationAccountStore(
        state=None,
        charge_contract=contracts.charge_contract,
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id="runtime:coordinator",
        event_log=event_log,
        store=store,
        charge_contract=contracts.charge_contract,
        limits=contracts.limits,
    )
    context = EventContext(
        run_id="run:coordinator",
        turn_id="turn:coordinator",
        reply_id="reply:coordinator",
    )
    first = CustomEvent(
        id="event:first-business",
        **context.event_fields(),
        name="first-business",
    )
    genesis = coordinator.bootstrap_genesis(
        context=context,
        business_events=(first,),
        genesis_profile="host_first_run",
        genesis_burst_contract=(
            contracts.burst_registry.unique_binding_for_operation(
                PhysicalOperationKind.LEDGER_GENESIS
            ).contract
        ),
        register_transcript_consumer=True,
    )

    assert genesis.stored_events[-1].type == "LEDGER_MATERIALIZATION_ACCOUNT_GENESIS"
    assert len(genesis.resulting_account_state.generation.consumer_horizons) == 2

    dispatch = CustomEvent(
        id="event:dispatch-proof",
        **context.event_fields(),
        name="dispatch-proof",
    )
    committed = coordinator.reserve_and_commit_dispatch(
        context=context,
        business_events=(dispatch,),
        reservation_id="reservation:dispatch",
        owner_id="owner:dispatch",
        burst_contract=(
            contracts.burst_registry.unique_binding_for_operation(
                PhysicalOperationKind.HOST_RUN_BOUNDARY
            ).contract
        ),
        business_run_id="run:coordinator",
        business_window_id="window:coordinator",
        business_window_generation=0,
    )

    assert committed.stored_events[-1].type == "PHYSICAL_OPERATION_RESERVATION_CREATED"
    assert len(committed.resulting_account_state.active_reservations) == 1
    assert event_log.read_materialization_account_state() == (
        committed.resulting_account_state
    )


def test_one_shot_operation_advances_account_without_leaving_reservation() -> None:
    contracts = build_default_authority_materialization_contract_bundle()
    event_log = InMemoryEventLog(runtime_session_id="runtime:one-shot")
    store = LedgerMaterializationAccountStore(
        state=None,
        charge_contract=contracts.charge_contract,
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id="runtime:one-shot",
        event_log=event_log,
        store=store,
        charge_contract=contracts.charge_contract,
        limits=contracts.limits,
    )
    context = EventContext(
        run_id="run:one-shot",
        turn_id="turn:one-shot",
        reply_id="reply:one-shot",
    )
    coordinator.bootstrap_genesis(
        context=context,
        business_events=(
            CustomEvent(
                id="event:one-shot-genesis",
                **context.event_fields(),
                name="one-shot-genesis",
            ),
        ),
        genesis_profile="host_first_run",
        genesis_burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.LEDGER_GENESIS
        ).contract,
        register_transcript_consumer=True,
    )

    committed = coordinator.commit_one_shot_operation(
        context=context,
        business_events=(
            CustomEvent(
                id="event:one-shot-business",
                **context.event_fields(),
                name="one-shot-business",
            ),
        ),
        reservation_id="reservation:one-shot",
        owner_id="owner:one-shot",
        burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.RUNTIME_INTERNAL_WRITE
        ).contract,
        business_run_id="run:one-shot",
        business_window_id="window:one-shot",
        business_window_generation=0,
    )

    assert tuple(event.type for event in committed.stored_events) == (
        "CUSTOM",
        "PHYSICAL_OPERATION_RESERVATION_CREATED",
        "PHYSICAL_OPERATION_RESERVATION_SETTLED",
    )
    assert committed.reservation.reserved_events == len(committed.stored_events)
    assert (
        committed.settlement_event.settlement.total_charged_events
        == committed.reservation.reserved_events
    )
    assert committed.settlement_event.settlement.released_on_settlement_events == 0
    assert committed.resulting_account_state.active_reservations == ()
    assert committed.resulting_account_state.ledger_through_sequence == len(
        tuple(event_log.iter())
    )
    assert event_log.read_materialization_account_state() == (
        committed.resulting_account_state
    )


def test_retained_reservation_charges_and_settles_exact_predecessor() -> None:
    contracts = build_default_authority_materialization_contract_bundle()
    event_log = InMemoryEventLog(runtime_session_id="runtime:retained")
    store = LedgerMaterializationAccountStore(
        state=None,
        charge_contract=contracts.charge_contract,
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id="runtime:retained",
        event_log=event_log,
        store=store,
        charge_contract=contracts.charge_contract,
        limits=contracts.limits,
    )
    context = EventContext(
        run_id="run:retained",
        turn_id="turn:retained",
        reply_id="reply:retained",
    )
    coordinator.bootstrap_genesis(
        context=context,
        business_events=(
            CustomEvent(
                id="event:retained-genesis",
                **context.event_fields(),
                name="retained-genesis",
            ),
        ),
        genesis_profile="host_first_run",
        genesis_burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.LEDGER_GENESIS
        ).contract,
        register_transcript_consumer=True,
    )
    admitted = coordinator.reserve_and_commit_dispatch(
        context=context,
        business_events=(
            CustomEvent(
                id="event:retained-dispatch",
                **context.event_fields(),
                name="retained-dispatch",
            ),
        ),
        reservation_id="reservation:retained",
        owner_id="owner:retained",
        burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.MODEL_CALL
        ).contract,
    )
    charged = coordinator.commit_reserved_charge(
        context=context,
        reservation=admitted.reservation,
        business_events=(
            CustomEvent(
                id="event:retained-delta",
                **context.event_fields(),
                name="retained-delta",
            ),
        ),
    )
    assert charged.resulting_reservation_state.remaining_events < (
        admitted.resulting_account_state.active_reservations[0].remaining_events
    )
    settled = coordinator.commit_reserved_settlement(
        context=context,
        reservation=admitted.reservation,
        business_events=(
            CustomEvent(
                id="event:retained-terminal",
                **context.event_fields(),
                name="retained-terminal",
            ),
        ),
        terminal_outcome="completed",
    )

    assert settled.resulting_account_state.active_reservations == ()
    assert (
        settled.settlement_event.settlement.predecessor_reservation_state_fingerprint
        == (charged.resulting_reservation_state.state_fingerprint)
    )
    assert settled.settlement_event.settlement.total_charged_events == (
        settled.settlement_event.settlement.charged_candidate_events
        + settled.settlement_event.settlement.charged_bookkeeping_events
    )


def test_materialization_account_commit_then_raise_confirms_exact_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contracts = build_default_authority_materialization_contract_bundle()
    event_log = InMemoryEventLog(runtime_session_id="runtime:account-confirm")
    original = InMemoryEventLog.extend_with_materialization_state
    calls = 0

    def commit_then_raise(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        original(self, *args, **kwargs)
        raise RuntimeError("connection outcome unknown")

    monkeypatch.setattr(
        InMemoryEventLog,
        "extend_with_materialization_state",
        commit_then_raise,
    )
    store = LedgerMaterializationAccountStore(
        state=None, charge_contract=contracts.charge_contract
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id="runtime:account-confirm",
        event_log=event_log,
        store=store,
        charge_contract=contracts.charge_contract,
        limits=contracts.limits,
    )
    context = EventContext(
        run_id="run:account-confirm",
        turn_id="turn:account-confirm",
        reply_id="reply:account-confirm",
    )

    committed = coordinator.bootstrap_genesis(
        context=context,
        business_events=(
            CustomEvent(
                id="event:account-confirm-first",
                **context.event_fields(),
                name="account-confirm-first",
            ),
        ),
        genesis_profile="host_first_run",
        genesis_burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.LEDGER_GENESIS
        ).contract,
        register_transcript_consumer=True,
    )

    assert calls == 1
    assert store.snapshot() == committed.resulting_account_state
    assert event_log.next_sequence() - 1 == len(committed.stored_events)


def test_graph_checkpoint_atomically_advances_only_graph_consumer() -> None:
    contracts = build_default_authority_materialization_contract_bundle()
    event_log = InMemoryEventLog(runtime_session_id="runtime:graph-horizon")
    store = LedgerMaterializationAccountStore(
        state=None,
        charge_contract=contracts.charge_contract,
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id="runtime:graph-horizon",
        event_log=event_log,
        store=store,
        charge_contract=contracts.charge_contract,
        limits=contracts.limits,
    )
    context = EventContext(
        run_id="run:graph-horizon",
        turn_id="turn:graph-horizon",
        reply_id="reply:graph-horizon",
    )
    genesis = coordinator.bootstrap_genesis(
        context=context,
        business_events=(
            CustomEvent(
                id="event:graph-horizon-genesis",
                **context.event_fields(),
                name="graph-horizon-genesis",
            ),
        ),
        genesis_profile="host_first_run",
        genesis_burst_contract=(
            contracts.burst_registry.unique_binding_for_operation(
                PhysicalOperationKind.LEDGER_GENESIS
            ).contract
        ),
        register_transcript_consumer=True,
    )
    source = genesis.resulting_account_state
    raw = event_log.read_raw_range_snapshot(
        minimum_sequence=1,
        through_sequence=source.ledger_through_sequence,
    )
    prepared = prepare_subagent_graph_checkpoint(
        runtime_session_id="runtime:graph-horizon",
        prefix_events=raw.events,
        reducer_binding=build_default_subagent_graph_reducer_binding(),
    )
    prefix = event_log.read_transcript_domain_delta(
        after_sequence=source.ledger_through_sequence,
        through_sequence=source.ledger_through_sequence,
        max_events=1,
        max_payload_bytes=1,
        registry_contract_fingerprint=(
            contracts.event_domain.contract.registry_contract_fingerprint
        ),
    ).after

    committed = coordinator.commit_graph_checkpoint_consumer_advance(
        checkpoint_event=prepared.event,
        ledger_charged_payload_bytes_through_checkpoint=(
            source.ledger_charged_payload_bytes_through
        ),
        ledger_continuity_accumulator_through_checkpoint=(
            prefix.ledger_continuity_accumulator
        ),
    )

    horizons = {
        item.consumer_kind: item
        for item in committed.resulting_account_state.generation.consumer_horizons
    }
    assert horizons[
        LedgerMaterializationConsumerKind.SUBAGENT_GRAPH
    ].through_sequence == source.ledger_through_sequence
    assert horizons[
        LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW
    ].through_sequence == 0
    assert committed.resulting_account_state.generation.reclaimable_through_sequence == 0
    assert committed.generation_event is None
    assert tuple(event.id for event in committed.stored_events) == (
        prepared.event.id,
        committed.horizon_event.id,
    )


def test_graph_checkpoint_service_commits_horizon_in_same_account_batch(
    tmp_path,
) -> None:
    runtime_session_id = "runtime:graph-service-horizon"
    event_log = InMemoryEventLog(runtime_session_id=runtime_session_id)
    context = EventContext(
        run_id="run:graph-service-horizon",
        turn_id="turn:graph-service-horizon",
        reply_id="reply:graph-service-horizon",
    )
    event_log.append(
        CustomEvent(
            id="event:graph-service-prefix",
            **context.event_fields(),
            name="graph-service-prefix",
        )
    )
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=runtime_session_id,
        event_log=event_log,
    )
    try:
        runtime._adopt_unbootstrapped_in_memory_account_for_test(
            incoming_run_id=context.run_id
        )
        source = runtime.materialization_account_store.snapshot()
        assert source is not None
        raw = event_log.read_raw_range_snapshot(
            minimum_sequence=1,
            through_sequence=source.ledger_through_sequence,
        )
        prepared = prepare_subagent_graph_checkpoint(
            runtime_session_id=runtime_session_id,
            prefix_events=raw.events,
            reducer_binding=build_default_subagent_graph_reducer_binding(),
        )

        asyncio.run(
            runtime.subagent_graph_checkpoint_service._write_prepared_checkpoint(
                prepared,
                deadline_monotonic=monotonic() + 5.0,
            )
        )

        account = runtime.materialization_account_store.snapshot()
        assert account is not None
        graph = next(
            item
            for item in account.generation.consumer_horizons
            if item.consumer_kind is LedgerMaterializationConsumerKind.SUBAGENT_GRAPH
        )
        assert graph.through_sequence == source.ledger_through_sequence
        assert event_log.get_by_id(prepared.event.id) is not None
        assert any(
            isinstance(event, LedgerMaterializationConsumerHorizonAdvancedEvent)
            and event.cause.checkpoint_id == prepared.checkpoint.checkpoint_id
            for event in event_log.iter()
        )
    finally:
        runtime.close()


def test_materialization_account_precommit_failure_remains_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contracts = build_default_authority_materialization_contract_bundle()
    event_log = InMemoryEventLog(runtime_session_id="runtime:account-none")

    def fail_before_commit(self, *args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        InMemoryEventLog,
        "extend_with_materialization_state",
        fail_before_commit,
    )
    store = LedgerMaterializationAccountStore(
        state=None, charge_contract=contracts.charge_contract
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id="runtime:account-none",
        event_log=event_log,
        store=store,
        charge_contract=contracts.charge_contract,
        limits=contracts.limits,
    )
    context = EventContext(
        run_id="run:account-none",
        turn_id="turn:account-none",
        reply_id="reply:account-none",
    )

    with pytest.raises(RuntimeError, match="was not committed"):
        coordinator.bootstrap_genesis(
            context=context,
            business_events=(
                CustomEvent(
                    id="event:account-none-first",
                    **context.event_fields(),
                    name="account-none-first",
                ),
            ),
            genesis_profile="host_first_run",
            genesis_burst_contract=(
                contracts.burst_registry.unique_binding_for_operation(
                    PhysicalOperationKind.LEDGER_GENESIS
                ).contract
            ),
            register_transcript_consumer=True,
        )

    assert event_log.next_sequence() == 1
    assert store.snapshot() is None


def test_checkpoint_barrier_closes_new_producer_admission() -> None:
    from pulsara_agent.primitives.transcript_projection import (
        TranscriptProjectionScopeFact,
    )
    from pulsara_agent.runtime.authority_materialization import (
        build_default_transcript_projection_materialization_contracts,
    )
    from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
        TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
        TranscriptProjectionDocumentRegistry,
        TranscriptProjectionStateStore,
    )

    contracts = build_default_authority_materialization_contract_bundle()
    event_log = InMemoryEventLog(runtime_session_id="runtime:barrier")
    store = LedgerMaterializationAccountStore(
        state=None, charge_contract=contracts.charge_contract
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id="runtime:barrier",
        event_log=event_log,
        store=store,
        charge_contract=contracts.charge_contract,
        limits=contracts.limits,
    )
    context = EventContext(
        run_id="run:barrier", turn_id="turn:barrier", reply_id="reply:barrier"
    )
    genesis = coordinator.bootstrap_genesis(
        context=context,
        business_events=(
            CustomEvent(
                id="event:barrier-first",
                **context.event_fields(),
                name="barrier-first",
            ),
        ),
        genesis_profile="host_first_run",
        genesis_burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.LEDGER_GENESIS
        ).contract,
        register_transcript_consumer=True,
    )
    transcript_store = TranscriptProjectionStateStore(
        runtime_session_id="runtime:barrier",
        documents=TranscriptProjectionDocumentRegistry(),
    )
    transcript_store.apply_committed(genesis.stored_events)
    stable = transcript_store.snapshot().stable_semantic_state
    tree_contracts = build_default_transcript_projection_materialization_contracts(
        contracts.limits
    )
    seed = prepare_run_transcript_seed(
        runtime_session_id="runtime:barrier",
        stable_state=_fact(
            TranscriptProjectionStableSemanticStateFact,
            schema_version="transcript_projection_stable_semantic_state.v1",
            semantic_source_event_count=0,
            semantic_source_accumulator=_fingerprint("barrier-seed-source"),
            normalized_transcript_fingerprint=context_fingerprint(
                "normalized-transcript-semantic:v1", ()
            ),
        ),
        stable_entries=(),
        ledger_through_sequence=0,
        ledger_continuity_accumulator=_fingerprint("barrier-seed-ledger"),
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            contracts.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=tree_contracts,
    )
    account = store.snapshot()
    assert account is not None
    consumer = next(
        item
        for item in account.generation.consumer_horizons
        if item.consumer_kind is LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW
    )
    prepared = prepare_transcript_checkpoint_candidate(
        checkpoint_id="checkpoint:barrier",
        scope=TranscriptProjectionScopeFact(
            schema_version="transcript_projection_scope.v1",
            runtime_session_id="runtime:barrier",
            run_id="run:barrier",
            window_id="seed",
            window_generation=0,
        ),
        run_seed_semantic=seed.seed_semantic,
        run_seed_reference=seed.seed_reference,
        materialization_consumer=consumer,
        account_state=account,
        transcript_store=transcript_store,
        transcript_semantic_domain_contract_fingerprint=(
            contracts.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=tree_contracts,
        limits=contracts.limits,
    )
    installed = install_checkpoint_barrier(
        coordinator=coordinator,
        context=context,
        prepared=prepared,
        checkpoint_burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.CHECKPOINT_COMMIT
        ).contract,
        terminal_contract=build_default_checkpoint_terminal_contract(),
    )

    assert installed.resulting_account_state.active_checkpoint_barrier is not None
    with pytest.raises(CheckpointDispatchBarrierActive, match="checkpoint barrier"):
        coordinator.reserve_and_commit_dispatch(
            context=context,
            business_events=(
                CustomEvent(
                    id="event:blocked-behind-barrier",
                    **context.event_fields(),
                    name="blocked",
                ),
            ),
            reservation_id="reservation:blocked",
            owner_id="owner:blocked",
            burst_contract=contracts.burst_registry.unique_binding_for_operation(
                PhysicalOperationKind.HOST_RUN_BOUNDARY
            ).contract,
            business_run_id="run:barrier",
            business_window_id="seed",
            business_window_generation=0,
        )
    assert stable.normalized_transcript_fingerprint == (
        prepared.candidate.stable_semantic_state.normalized_transcript_fingerprint
    )


def test_checkpoint_success_advances_horizon_and_reopens_admission() -> None:
    from pulsara_agent.primitives.transcript_projection import (
        TranscriptProjectionScopeFact,
    )
    from pulsara_agent.runtime.authority_materialization import (
        build_default_transcript_projection_materialization_contracts,
    )
    from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
        TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
        TranscriptProjectionDocumentRegistry,
        TranscriptProjectionStateStore,
    )

    contracts = build_default_authority_materialization_contract_bundle()
    event_log = InMemoryEventLog(runtime_session_id="runtime:checkpoint-success")
    store = LedgerMaterializationAccountStore(
        state=None, charge_contract=contracts.charge_contract
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id="runtime:checkpoint-success",
        event_log=event_log,
        store=store,
        charge_contract=contracts.charge_contract,
        limits=contracts.limits,
    )
    context = EventContext(
        run_id="run:checkpoint-success",
        turn_id="turn:checkpoint-success",
        reply_id="reply:checkpoint-success",
    )
    genesis = coordinator.bootstrap_genesis(
        context=context,
        business_events=(
            CustomEvent(
                id="event:checkpoint-success-first",
                **context.event_fields(),
                name="checkpoint-success-first",
            ),
        ),
        genesis_profile="host_first_run",
        genesis_burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.LEDGER_GENESIS
        ).contract,
        register_transcript_consumer=True,
    )
    transcript_store = TranscriptProjectionStateStore(
        runtime_session_id="runtime:checkpoint-success",
        documents=TranscriptProjectionDocumentRegistry(),
    )
    transcript_store.apply_committed(genesis.stored_events)
    tree_contracts = build_default_transcript_projection_materialization_contracts(
        contracts.limits
    )
    seed = prepare_run_transcript_seed(
        runtime_session_id="runtime:checkpoint-success",
        stable_state=_fact(
            TranscriptProjectionStableSemanticStateFact,
            schema_version="transcript_projection_stable_semantic_state.v1",
            semantic_source_event_count=0,
            semantic_source_accumulator=_fingerprint("checkpoint-success-seed-source"),
            normalized_transcript_fingerprint=context_fingerprint(
                "normalized-transcript-semantic:v1", ()
            ),
        ),
        stable_entries=(),
        ledger_through_sequence=0,
        ledger_continuity_accumulator=_fingerprint("checkpoint-success-seed-ledger"),
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            contracts.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=tree_contracts,
    )
    account = store.snapshot()
    assert account is not None
    consumer = next(
        item
        for item in account.generation.consumer_horizons
        if item.consumer_kind is LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW
    )
    prepared = prepare_transcript_checkpoint_candidate(
        checkpoint_id="checkpoint:success",
        scope=TranscriptProjectionScopeFact(
            schema_version="transcript_projection_scope.v1",
            runtime_session_id="runtime:checkpoint-success",
            run_id="run:checkpoint-success",
            window_id="seed",
            window_generation=0,
        ),
        run_seed_semantic=seed.seed_semantic,
        run_seed_reference=seed.seed_reference,
        materialization_consumer=consumer,
        account_state=account,
        transcript_store=transcript_store,
        transcript_semantic_domain_contract_fingerprint=(
            contracts.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=tree_contracts,
        limits=contracts.limits,
    )
    terminal_contract = build_default_checkpoint_terminal_contract()
    installed = install_checkpoint_barrier(
        coordinator=coordinator,
        context=context,
        prepared=prepared,
        checkpoint_burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.CHECKPOINT_COMMIT
        ).contract,
        terminal_contract=terminal_contract,
    )

    committed = commit_checkpoint_success(
        coordinator=coordinator,
        context=context,
        installed=installed,
        terminal_contract=terminal_contract,
    )

    assert installed.intent_event.sequence is not None
    assert installed.reservation_event.sequence is not None
    assert installed.barrier_event.sequence is not None
    assert committed.committed_event.sequence is not None
    assert committed.horizon_event.sequence is not None
    assert committed.settlement_event.sequence is not None
    assert committed.barrier_release_event.sequence is not None
    assert committed.resulting_account_state.active_checkpoint_barrier is None
    assert committed.resulting_account_state.active_reservations == ()
    resulting_consumer = next(
        item
        for item in committed.resulting_account_state.generation.consumer_horizons
        if item.consumer_id == consumer.consumer_id
    )
    assert resulting_consumer.through_sequence == (
        prepared.candidate.candidate_ledger_through_sequence
    )
    admitted = coordinator.reserve_and_commit_dispatch(
        context=context,
        business_events=(
            CustomEvent(
                id="event:after-checkpoint",
                **context.event_fields(),
                name="after-checkpoint",
            ),
        ),
        reservation_id="reservation:after-checkpoint",
        owner_id="owner:after-checkpoint",
        burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.HOST_RUN_BOUNDARY
        ).contract,
        business_run_id="run:checkpoint-success",
        business_window_id="seed",
        business_window_generation=0,
    )
    assert admitted.reservation.reservation_id == "reservation:after-checkpoint"


def test_missing_checkpoint_artifact_uses_previous_compatible_generation() -> None:
    from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
    from pulsara_agent.primitives.transcript_projection import (
        TranscriptProjectionScopeFact,
    )
    from pulsara_agent.runtime.authority_materialization import (
        build_default_transcript_projection_materialization_contracts,
    )
    from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
        TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
        TranscriptProjectionDocumentRegistry,
        TranscriptProjectionStateStore,
    )

    runtime_session_id = "runtime:checkpoint-fallback"
    context = EventContext(
        run_id="run:checkpoint-fallback",
        turn_id="turn:checkpoint-fallback",
        reply_id="reply:checkpoint-fallback",
    )
    contracts = build_default_authority_materialization_contract_bundle()
    tree_contracts = build_default_transcript_projection_materialization_contracts(
        contracts.limits
    )
    event_log = InMemoryEventLog(runtime_session_id=runtime_session_id)
    account_store = LedgerMaterializationAccountStore(
        state=None,
        charge_contract=contracts.charge_contract,
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id=runtime_session_id,
        event_log=event_log,
        store=account_store,
        charge_contract=contracts.charge_contract,
        limits=contracts.limits,
    )
    transcript_store = TranscriptProjectionStateStore(
        runtime_session_id=runtime_session_id,
        documents=TranscriptProjectionDocumentRegistry(),
    )
    archive = InMemoryArchiveStore()
    genesis = coordinator.bootstrap_genesis(
        context=context,
        business_events=(
            CustomEvent(
                id="event:checkpoint-fallback-genesis",
                **context.event_fields(),
                name="checkpoint-fallback-genesis",
            ),
        ),
        genesis_profile="host_first_run",
        genesis_burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.LEDGER_GENESIS
        ).contract,
        register_transcript_consumer=True,
    )
    transcript_store.apply_committed(genesis.stored_events)
    initial = transcript_store.snapshot()
    seed = prepare_run_transcript_seed(
        runtime_session_id=runtime_session_id,
        stable_state=initial.stable_semantic_state,
        stable_entries=(),
        ledger_through_sequence=initial.ledger_through_sequence,
        ledger_continuity_accumulator=initial.ledger_continuity_accumulator,
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            contracts.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=tree_contracts,
    )
    terminal_contract = build_default_checkpoint_terminal_contract()

    def commit_checkpoint_generation(
        *,
        checkpoint_id: str,
        previous_checkpoint_id: str | None,
        previously_reachable_artifact_ids: frozenset[str],
    ):
        account = account_store.snapshot()
        assert account is not None
        consumer = next(
            item
            for item in account.generation.consumer_horizons
            if item.consumer_kind
            is LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW
        )
        prepared = prepare_transcript_checkpoint_candidate(
            checkpoint_id=checkpoint_id,
            scope=TranscriptProjectionScopeFact(
                schema_version="transcript_projection_scope.v1",
                runtime_session_id=runtime_session_id,
                run_id=context.run_id,
                window_id="seed",
                window_generation=0,
            ),
            run_seed_semantic=seed.seed_semantic,
            run_seed_reference=seed.seed_reference,
            materialization_consumer=consumer,
            account_state=account,
            transcript_store=transcript_store,
            transcript_semantic_domain_contract_fingerprint=(
                contracts.event_domain.contract.registry_contract_fingerprint
            ),
            contracts=tree_contracts,
            limits=contracts.limits,
            previous_checkpoint_id=previous_checkpoint_id,
            previously_reachable_artifact_ids=previously_reachable_artifact_ids,
        )
        installed = install_checkpoint_barrier(
            coordinator=coordinator,
            context=context,
            prepared=prepared,
            checkpoint_burst_contract=contracts.burst_registry.unique_binding_for_operation(
                PhysicalOperationKind.CHECKPOINT_COMMIT
            ).contract,
            terminal_contract=terminal_contract,
        )
        transcript_store.apply_committed(installed.stored_events)
        deadline = monotonic() + 2
        persist_prepared_transcript_projection_materialization(
            prepared.materialization,
            write_reservation=prepare_authority_artifact_write_reservation(
                operation_id=checkpoint_id,
                owner_kind="checkpoint_materialization",
                artifacts=prepared.materialization.artifacts,
                limits=contracts.limits,
                absolute_deadline_monotonic=deadline,
            ),
            limits=contracts.limits,
            archive=archive,
            runtime_session_id=runtime_session_id,
            run_id=context.run_id,
            deadline_monotonic=deadline,
        )
        committed = commit_checkpoint_success(
            coordinator=coordinator,
            context=context,
            installed=installed,
            terminal_contract=terminal_contract,
        )
        transcript_store.apply_committed(committed.stored_events)
        return committed

    first = commit_checkpoint_generation(
        checkpoint_id="checkpoint:fallback:first",
        previous_checkpoint_id=None,
        previously_reachable_artifact_ids=frozenset(),
    )
    lifecycle = coordinator.commit_one_shot_operation(
        context=context,
        business_events=(
            TerminalProcessCompletedEvent(
                id="terminal_process_completed:checkpoint-fallback",
                **context.event_fields(),
                process_id="process:checkpoint-fallback",
                terminal_session_id="terminal:checkpoint-fallback",
                command="pytest -q",
                status="success",
                exit_code=0,
                cwd="/workspace",
                duration_seconds=1.0,
            ),
        ),
        reservation_id="reservation:checkpoint-fallback-lifecycle",
        owner_id="owner:checkpoint-fallback-lifecycle",
        burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.RUNTIME_INTERNAL_WRITE
        ).contract,
        business_run_id=context.run_id,
        business_window_id="seed",
        business_window_generation=0,
    )
    transcript_store.apply_committed(lifecycle.stored_events)
    first_artifacts = frozenset(
        item.artifact_id
        for item in first.installed.prepared.materialization.artifacts
    )
    second = commit_checkpoint_generation(
        checkpoint_id="checkpoint:fallback:second",
        previous_checkpoint_id=first.installed.checkpoint_id,
        previously_reachable_artifact_ids=first_artifacts,
    )
    first_root = (
        first.installed.prepared.materialization.root_reference.root_artifact_id
    )
    second_root = (
        second.installed.prepared.materialization.root_reference.root_artifact_id
    )
    assert first_root != second_root
    second_info = archive.get_info(second_root, session_id=runtime_session_id)
    assert archive.delete_if_identity(
        second_root,
        session_id=runtime_session_id,
        digest=second_info.digest,
        media_type=second_info.media_type,
        semantic_metadata_fingerprint=str(
            second_info.metadata["semantic_metadata_fingerprint"]
        ),
    )

    restored = restore_transcript_projection(
        event_log=event_log,
        archive=archive,
        runtime_session_id=runtime_session_id,
        requested_through_sequence=event_log.next_sequence() - 1,
        event_domain_binding=contracts.event_domain,
        materialization_contracts=tree_contracts,
        limits=contracts.limits,
    )

    assert restored.base_kind == "checkpoint"
    assert restored.base_id == first.installed.checkpoint_id
    assert restored.state_store.snapshot().stable_semantic_state == (
        transcript_store.snapshot().stable_semantic_state
    )

    later = coordinator.commit_one_shot_operation(
        context=context,
        business_events=(
            TerminalProcessCompletedEvent(
                id="terminal_process_completed:checkpoint-fallback-later",
                **context.event_fields(),
                process_id="process:checkpoint-fallback-later",
                terminal_session_id="terminal:checkpoint-fallback",
                command="pytest tests/test_authority_materialization_contract.py -q",
                status="success",
                exit_code=0,
                cwd="/workspace",
                duration_seconds=2.0,
            ),
        ),
        reservation_id="reservation:checkpoint-fallback-later",
        owner_id="owner:checkpoint-fallback-later",
        burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.RUNTIME_INTERNAL_WRITE
        ).contract,
        business_run_id=context.run_id,
        business_window_id="seed",
        business_window_generation=0,
    )
    transcript_store.apply_committed(later.stored_events)
    third = commit_checkpoint_generation(
        checkpoint_id="checkpoint:fallback:third",
        previous_checkpoint_id=first.installed.checkpoint_id,
        previously_reachable_artifact_ids=first_artifacts,
    )

    from pulsara_agent.runtime.authority_materialization import (
        garbage_collect_transcript_projection_artifacts,
    )
    from pulsara_agent.event import TranscriptProjectionCheckpointCommittedEvent
    from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
        InMemoryCheckpointMaintenanceAuthority,
    )

    report = garbage_collect_transcript_projection_artifacts(
        runtime_session_id=runtime_session_id,
        event_log=event_log,
        archive=archive,
        maintenance_authority=InMemoryCheckpointMaintenanceAuthority(
            is_quiescent=lambda _runtime_session_id: True
        ),
        materialization_contracts=tree_contracts,
        retained_checkpoint_min_count=1,
    )
    assert report.verified_fallback_checkpoint_ids == (
        third.installed.checkpoint_id,
    )
    assert report.unavailable_checkpoint_ids == (
        second.installed.checkpoint_id,
    )
    assert first_root not in archive.blobs
    assert sum(
        isinstance(event, TranscriptProjectionCheckpointCommittedEvent)
        for event in event_log.iter()
    ) == 3


def test_transcript_restore_uses_shared_maintenance_read_lease() -> None:
    from contextlib import contextmanager

    from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
    from pulsara_agent.runtime.authority_materialization import (
        build_default_transcript_projection_materialization_contracts,
    )
    from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
        InMemoryCheckpointMaintenanceAuthority,
    )

    runtime_session_id = "runtime:transcript-read-lease"
    contracts = build_default_authority_materialization_contract_bundle()
    tree_contracts = build_default_transcript_projection_materialization_contracts(
        contracts.limits
    )
    delegate = InMemoryCheckpointMaintenanceAuthority(
        is_quiescent=lambda _runtime_session_id: True
    )

    class CountingAuthority:
        shared_acquisitions = 0

        @contextmanager
        def acquire_shared(self, requested_runtime_session_id):
            self.shared_acquisitions += 1
            with delegate.acquire_shared(requested_runtime_session_id) as permit:
                yield permit

        def acquire_exclusive(self, requested_runtime_session_id):
            return delegate.acquire_exclusive(requested_runtime_session_id)

    authority = CountingAuthority()
    restored = restore_transcript_projection(
        event_log=InMemoryEventLog(runtime_session_id=runtime_session_id),
        archive=InMemoryArchiveStore(),
        runtime_session_id=runtime_session_id,
        requested_through_sequence=0,
        event_domain_binding=contracts.event_domain,
        materialization_contracts=tree_contracts,
        limits=contracts.limits,
        maintenance_authority=authority,
    )

    assert restored.base_kind == "empty"
    assert authority.shared_acquisitions == 1


def test_transcript_checkpoint_gc_is_quiescent_and_empty_catalog_safe() -> None:
    from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
    from pulsara_agent.runtime.authority_materialization import (
        build_default_transcript_projection_materialization_contracts,
        garbage_collect_transcript_projection_artifacts,
    )
    from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
        CheckpointMaintenanceLockUnavailable,
        InMemoryCheckpointMaintenanceAuthority,
    )

    runtime_session_id = "runtime:transcript-empty-gc"
    contracts = build_default_authority_materialization_contract_bundle()
    authority = InMemoryCheckpointMaintenanceAuthority(
        is_quiescent=lambda _runtime_session_id: True
    )
    event_log = InMemoryEventLog(runtime_session_id=runtime_session_id)
    archive = InMemoryArchiveStore()
    with authority.acquire_shared(runtime_session_id):
        with pytest.raises(CheckpointMaintenanceLockUnavailable):
            garbage_collect_transcript_projection_artifacts(
                runtime_session_id=runtime_session_id,
                event_log=event_log,
                archive=archive,
                maintenance_authority=authority,
                materialization_contracts=(
                    build_default_transcript_projection_materialization_contracts(
                        contracts.limits
                    )
                ),
            )

    report = garbage_collect_transcript_projection_artifacts(
        runtime_session_id=runtime_session_id,
        event_log=event_log,
        archive=archive,
        maintenance_authority=authority,
        materialization_contracts=(
            build_default_transcript_projection_materialization_contracts(
                contracts.limits
            )
        ),
    )
    assert report.catalog_checkpoint_count == 0
    assert report.deleted_artifact_ids == ()


def _installed_checkpoint_for_terminal_test(
    *,
    suffix: str,
) -> tuple[
    LedgerMaterializationCoordinator,
    EventContext,
    object,
    object,
]:
    from pulsara_agent.primitives.transcript_projection import (
        TranscriptProjectionScopeFact,
    )
    from pulsara_agent.runtime.authority_materialization import (
        build_default_transcript_projection_materialization_contracts,
    )
    from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
        TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
        TranscriptProjectionDocumentRegistry,
        TranscriptProjectionStateStore,
    )

    runtime_session_id = f"runtime:checkpoint-{suffix}"
    contracts = build_default_authority_materialization_contract_bundle()
    event_log = InMemoryEventLog(runtime_session_id=runtime_session_id)
    store = LedgerMaterializationAccountStore(
        state=None, charge_contract=contracts.charge_contract
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id=runtime_session_id,
        event_log=event_log,
        store=store,
        charge_contract=contracts.charge_contract,
        limits=contracts.limits,
    )
    context = EventContext(
        run_id=f"run:checkpoint-{suffix}",
        turn_id=f"turn:checkpoint-{suffix}",
        reply_id=f"reply:checkpoint-{suffix}",
    )
    genesis = coordinator.bootstrap_genesis(
        context=context,
        business_events=(
            CustomEvent(
                id=f"event:checkpoint-{suffix}-first",
                **context.event_fields(),
                name="checkpoint-terminal-first",
            ),
        ),
        genesis_profile="host_first_run",
        genesis_burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.LEDGER_GENESIS
        ).contract,
        register_transcript_consumer=True,
    )
    transcript_store = TranscriptProjectionStateStore(
        runtime_session_id=runtime_session_id,
        documents=TranscriptProjectionDocumentRegistry(),
    )
    transcript_store.apply_committed(genesis.stored_events)
    tree_contracts = build_default_transcript_projection_materialization_contracts(
        contracts.limits
    )
    seed = prepare_run_transcript_seed(
        runtime_session_id=runtime_session_id,
        stable_state=_fact(
            TranscriptProjectionStableSemanticStateFact,
            schema_version="transcript_projection_stable_semantic_state.v1",
            semantic_source_event_count=0,
            semantic_source_accumulator=_fingerprint(
                f"checkpoint-{suffix}-seed-source"
            ),
            normalized_transcript_fingerprint=context_fingerprint(
                "normalized-transcript-semantic:v1", ()
            ),
        ),
        stable_entries=(),
        ledger_through_sequence=0,
        ledger_continuity_accumulator=_fingerprint(f"checkpoint-{suffix}-seed-ledger"),
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            contracts.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=tree_contracts,
    )
    account = store.snapshot()
    assert account is not None
    consumer = next(
        item
        for item in account.generation.consumer_horizons
        if item.consumer_kind is LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW
    )
    prepared = prepare_transcript_checkpoint_candidate(
        checkpoint_id=f"checkpoint:{suffix}",
        scope=TranscriptProjectionScopeFact(
            schema_version="transcript_projection_scope.v1",
            runtime_session_id=runtime_session_id,
            run_id=context.run_id,
            window_id="seed",
            window_generation=0,
        ),
        run_seed_semantic=seed.seed_semantic,
        run_seed_reference=seed.seed_reference,
        materialization_consumer=consumer,
        account_state=account,
        transcript_store=transcript_store,
        transcript_semantic_domain_contract_fingerprint=(
            contracts.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=tree_contracts,
        limits=contracts.limits,
    )
    terminal_contract = build_default_checkpoint_terminal_contract()
    installed = install_checkpoint_barrier(
        coordinator=coordinator,
        context=context,
        prepared=prepared,
        checkpoint_burst_contract=contracts.burst_registry.unique_binding_for_operation(
            PhysicalOperationKind.CHECKPOINT_COMMIT
        ).contract,
        terminal_contract=terminal_contract,
    )
    return coordinator, context, installed, terminal_contract


def test_checkpoint_failure_releases_exact_barrier_and_reservation() -> None:
    from pulsara_agent.primitives.transcript_checkpoint import (
        CheckpointFailureReasonCode,
        CheckpointTerminalDiagnosticCode,
        CheckpointTerminalDiagnosticFact,
    )

    coordinator, context, installed, terminal_contract = (
        _installed_checkpoint_for_terminal_test(suffix="failure")
    )
    diagnostic = _fact(
        CheckpointTerminalDiagnosticFact,
        schema_version="checkpoint_terminal_diagnostic.v2",
        diagnostic_code=CheckpointTerminalDiagnosticCode.ARTIFACT_IO_FAILURE,
        failure_stage="root_write",
        sanitized_detail=None,
    )

    terminated = commit_checkpoint_failure(
        coordinator=coordinator,
        context=context,
        installed=installed,
        terminal_contract=terminal_contract,
        reason_code=CheckpointFailureReasonCode.ARTIFACT_WRITE_FAILED,
        diagnostics=(diagnostic,),
    )

    assert terminated.terminal_event.sequence is not None
    assert terminated.settlement_event.sequence is not None
    assert terminated.barrier_release_event.sequence is not None
    assert terminated.terminal_event.type == "TRANSCRIPT_PROJECTION_CHECKPOINT_FAILED"
    assert terminated.resulting_account_state.active_checkpoint_barrier is None
    assert terminated.resulting_account_state.active_reservations == ()


def test_checkpoint_release_none_keeps_barrier_and_retries_stable_terminal_batch(
    monkeypatch,
) -> None:
    from pulsara_agent.primitives.transcript_checkpoint import (
        CheckpointFailureReasonCode,
    )

    coordinator, context, installed, terminal_contract = (
        _installed_checkpoint_for_terminal_test(suffix="stable-terminal-retry")
    )
    original = coordinator.commit_transition_batch
    candidates: list[tuple[dict[str, Any], ...]] = []

    def fail_once(**kwargs):
        events = tuple(kwargs["events"])
        candidates.append(tuple(event.model_dump(mode="json") for event in events))
        if len(candidates) == 1:
            raise RuntimeError("terminal precommit unavailable")
        return original(**kwargs)

    monkeypatch.setattr(coordinator, "commit_transition_batch", fail_once)

    with pytest.raises(RuntimeError, match="precommit unavailable"):
        commit_checkpoint_failure(
            coordinator=coordinator,
            context=context,
            installed=installed,
            terminal_contract=terminal_contract,
            reason_code=CheckpointFailureReasonCode.ARTIFACT_WRITE_FAILED,
        )
    source = coordinator.store.snapshot()
    assert source is not None
    assert source.active_checkpoint_barrier == installed.barrier

    terminated = commit_checkpoint_failure(
        coordinator=coordinator,
        context=context,
        installed=installed,
        terminal_contract=terminal_contract,
        reason_code=CheckpointFailureReasonCode.ARTIFACT_WRITE_FAILED,
    )

    assert candidates[0] == candidates[1]
    assert terminated.resulting_account_state.active_checkpoint_barrier is None


def test_checkpoint_cancel_and_recovery_use_typed_terminal_winners() -> None:
    from pulsara_agent.primitives.transcript_checkpoint import (
        CheckpointCancellationReasonCode,
    )

    coordinator, context, installed, terminal_contract = (
        _installed_checkpoint_for_terminal_test(suffix="cancel")
    )
    cancelled = commit_checkpoint_cancellation(
        coordinator=coordinator,
        context=context,
        installed=installed,
        terminal_contract=terminal_contract,
        cancellation_source="host_close",
        reason_code=CheckpointCancellationReasonCode.HOST_CLOSE,
    )
    assert cancelled.terminal_event.type == (
        "TRANSCRIPT_PROJECTION_CHECKPOINT_CANCELLED"
    )

    recovery_coordinator, recovery_context, recovery_installed, recovery_contract = (
        _installed_checkpoint_for_terminal_test(suffix="recovered")
    )
    recovery_source = recovery_coordinator.store.snapshot()
    assert recovery_source is not None
    recovered = commit_checkpoint_recovered_interrupted(
        coordinator=recovery_coordinator,
        context=recovery_context,
        installed=recovery_installed,
        terminal_contract=recovery_contract,
        reopen_ledger_high_water=recovery_source.ledger_through_sequence,
    )
    assert recovered.terminal_event.type == (
        "TRANSCRIPT_PROJECTION_CHECKPOINT_RECOVERED_INTERRUPTED"
    )
    assert recovered.resulting_account_state.active_checkpoint_barrier is None


def test_checkpoint_intent_full_without_terminal_recovers_interrupted_before_reopen(
    tmp_path,
) -> None:
    coordinator, _, installed, _ = _installed_checkpoint_for_terminal_test(
        suffix="runtime-reopen"
    )

    runtime = in_memory_runtime_session(
        tmp_path,
        event_log=coordinator.event_log,
        runtime_session_id=coordinator.runtime_session_id,
    )

    account = runtime.materialization_account_store.snapshot()
    assert account is not None
    assert account.active_checkpoint_barrier is None
    assert account.active_reservations == ()
    recovered = coordinator.event_log.get_by_id(
        f"checkpoint_recovered_interrupted:{installed.checkpoint_id}"
    )
    assert recovered is not None
    assert recovered.type == ("TRANSCRIPT_PROJECTION_CHECKPOINT_RECOVERED_INTERRUPTED")
    assert runtime.transcript_projection_checkpoint_service.pending_count == 0
    assert (
        runtime.physical_reservation_for_owner(
            operation_kind=PhysicalOperationKind.CHECKPOINT_COMMIT,
            owner_id=installed.checkpoint_id,
        )
        is None
    )
    runtime.close()


def test_restart_recovers_active_checkpoint_barrier_before_producer_admission(
    tmp_path,
    monkeypatch,
) -> None:
    coordinator, _, installed, _ = _installed_checkpoint_for_terminal_test(
        suffix="reopen-retry"
    )
    event_log = coordinator.event_log
    original_extend = InMemoryEventLog.extend_with_materialization_state
    terminal_candidates: list[tuple[dict[str, Any], ...]] = []
    recovery_write_blocked = True

    def fail_first_recovery(self, events, **kwargs):
        nonlocal recovery_write_blocked
        event_tuple = tuple(events)
        if self is event_log and any(
            str(event.type) == "TRANSCRIPT_PROJECTION_CHECKPOINT_RECOVERED_INTERRUPTED"
            for event in event_tuple
        ):
            terminal_candidates.append(
                tuple(event.model_dump(mode="json") for event in event_tuple)
            )
            if recovery_write_blocked:
                raise RuntimeError("recovery ledger unavailable")
        return original_extend(self, event_tuple, **kwargs)

    monkeypatch.setattr(
        InMemoryEventLog,
        "extend_with_materialization_state",
        fail_first_recovery,
    )
    with pytest.raises(
        RuntimeError,
        match="checkpoint recovered-interrupted terminalization failed",
    ):
        in_memory_runtime_session(
            tmp_path,
            event_log=event_log,
            runtime_session_id=coordinator.runtime_session_id,
        )
    recovery_write_blocked = False
    blocked = event_log.read_materialization_account_state()
    assert blocked is not None
    assert blocked.active_checkpoint_barrier == installed.barrier

    runtime = in_memory_runtime_session(
        tmp_path,
        event_log=event_log,
        runtime_session_id=coordinator.runtime_session_id,
    )

    assert terminal_candidates[0] == terminal_candidates[1]
    assert runtime.materialization_account_store.snapshot() is not None
    assert (
        runtime.materialization_account_store.snapshot().active_checkpoint_barrier
        is None
    )
    runtime.close()


def test_host_close_cancels_and_drains_checkpoint_barrier_owner(
    tmp_path,
    monkeypatch,
) -> None:
    import asyncio
    import threading
    from time import monotonic

    from pulsara_agent.runtime.authority_materialization import checkpoint_service
    from pulsara_agent.runtime.authority_materialization.checkpoint_service import (
        TranscriptProjectionCheckpointService,
        _CheckpointOwner,
    )
    from pulsara_agent.runtime.context_input.io_service import (
        PendingContextInputIoError,
    )

    coordinator, context, installed, _ = _installed_checkpoint_for_terminal_test(
        suffix="host-close-drain"
    )
    monkeypatch.setattr(
        TranscriptProjectionCheckpointService,
        "__post_init__",
        lambda self: None,
    )
    runtime = in_memory_runtime_session(
        tmp_path,
        event_log=coordinator.event_log,
        runtime_session_id=coordinator.runtime_session_id,
    )
    service = runtime.transcript_projection_checkpoint_service
    entered = threading.Event()
    release = threading.Event()
    original_persist = (
        checkpoint_service.persist_prepared_transcript_projection_materialization
    )

    def blocking_persist(*args, **kwargs):
        entered.set()
        release.wait()
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(
        checkpoint_service,
        "persist_prepared_transcript_projection_materialization",
        blocking_persist,
    )

    async def scenario() -> None:
        owner = _CheckpointOwner(
            checkpoint_id=installed.checkpoint_id,
            context=context,
            prepared=installed.prepared,
            installed=installed,
        )
        service._owners[installed.checkpoint_id] = owner
        service._start_owner_task(owner)
        assert await asyncio.to_thread(entered.wait, 1)

        await service.request_close_cancellation()
        with pytest.raises(
            PendingContextInputIoError,
            match="drain deadline exceeded",
        ):
            await runtime.context_input_io_service.drain_pending(
                deadline_monotonic=monotonic() + 0.02
            )
        blocked = runtime.materialization_account_store.snapshot()
        assert blocked is not None
        assert blocked.active_checkpoint_barrier == installed.barrier
        assert (
            coordinator.event_log.get_by_id(
                f"checkpoint_cancelled:{installed.checkpoint_id}"
            )
            is None
        )
        assert service.pending_count == 1

        release.set()
        await runtime.context_input_io_service.drain_pending(
            deadline_monotonic=monotonic() + 1
        )
        await service.drain_pending(deadline_monotonic=monotonic() + 1)

        terminal = coordinator.event_log.get_by_id(
            f"checkpoint_cancelled:{installed.checkpoint_id}"
        )
        assert terminal is not None
        assert terminal.type == "TRANSCRIPT_PROJECTION_CHECKPOINT_CANCELLED"
        settled = runtime.materialization_account_store.snapshot()
        assert settled is not None
        assert settled.active_checkpoint_barrier is None
        assert settled.active_reservations == ()
        assert service.pending_count == 0

    asyncio.run(scenario())
    runtime.close()


def test_checkpoint_worker_cancel_preserves_retryable_candidate(
    tmp_path,
    monkeypatch,
) -> None:
    import asyncio
    import threading
    from time import monotonic

    from pulsara_agent.runtime.authority_materialization import checkpoint_service
    from pulsara_agent.runtime.authority_materialization.checkpoint_service import (
        TranscriptProjectionCheckpointService,
        _CheckpointOwner,
    )

    coordinator, context, installed, _ = _installed_checkpoint_for_terminal_test(
        suffix="worker-cancel-retry"
    )
    monkeypatch.setattr(
        TranscriptProjectionCheckpointService,
        "__post_init__",
        lambda self: None,
    )
    runtime = in_memory_runtime_session(
        tmp_path,
        event_log=coordinator.event_log,
        runtime_session_id=coordinator.runtime_session_id,
    )
    service = runtime.transcript_projection_checkpoint_service
    entered = threading.Event()
    release = threading.Event()
    original_persist = (
        checkpoint_service.persist_prepared_transcript_projection_materialization
    )

    def blocking_persist(*args, **kwargs):
        entered.set()
        release.wait()
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(
        checkpoint_service,
        "persist_prepared_transcript_projection_materialization",
        blocking_persist,
    )

    async def scenario() -> None:
        owner = _CheckpointOwner(
            checkpoint_id=installed.checkpoint_id,
            context=context,
            prepared=installed.prepared,
            installed=installed,
        )
        service._owners[installed.checkpoint_id] = owner
        service._start_owner_task(owner)
        assert await asyncio.to_thread(entered.wait, 1)
        task = owner.task
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        blocked = runtime.materialization_account_store.snapshot()
        assert blocked is not None
        assert blocked.active_checkpoint_barrier == installed.barrier
        assert owner.artifact_operation is not None
        assert not owner.artifact_operation.physically_complete
        assert (
            coordinator.event_log.get_by_id(
                f"checkpoint_committed:{installed.checkpoint_id}"
            )
            is None
        )

        release.set()
        await runtime.context_input_io_service.drain_pending(
            deadline_monotonic=monotonic() + 1
        )
        await service.drain_pending(deadline_monotonic=monotonic() + 1)

        committed = coordinator.event_log.get_by_id(
            f"checkpoint_committed:{installed.checkpoint_id}"
        )
        assert committed is not None
        assert committed.type == "TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED"
        settled = runtime.materialization_account_store.snapshot()
        assert settled is not None
        assert settled.active_checkpoint_barrier is None
        assert service.pending_count == 0

    asyncio.run(scenario())
    runtime.close()


def test_checkpoint_publication_failure_does_not_revoke_committed_checkpoint(
    tmp_path,
    monkeypatch,
) -> None:
    import asyncio
    from time import monotonic

    from pulsara_agent.runtime.authority_materialization.checkpoint_service import (
        TranscriptProjectionCheckpointService,
        _CheckpointOwner,
    )

    coordinator, context, installed, _ = _installed_checkpoint_for_terminal_test(
        suffix="publication-handoff"
    )
    monkeypatch.setattr(
        TranscriptProjectionCheckpointService,
        "__post_init__",
        lambda self: None,
    )
    runtime = in_memory_runtime_session(
        tmp_path,
        event_log=coordinator.event_log,
        runtime_session_id=coordinator.runtime_session_id,
    )
    service = runtime.transcript_projection_checkpoint_service
    original_accept = type(runtime).accept_authority_materialization_transition
    reject_once = True

    def fail_first_terminal_handoff(self, events):
        nonlocal reject_once
        if self is runtime and reject_once and any(
            str(event.type) == "TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED"
            for event in events
        ):
            reject_once = False
            raise RuntimeError("publication handoff unavailable")
        return original_accept(self, events)

    monkeypatch.setattr(
        type(runtime),
        "accept_authority_materialization_transition",
        fail_first_terminal_handoff,
    )

    async def scenario() -> None:
        owner = _CheckpointOwner(
            checkpoint_id=installed.checkpoint_id,
            context=context,
            prepared=installed.prepared,
            installed=installed,
        )
        service._owners[installed.checkpoint_id] = owner
        service._start_owner_task(owner)
        task = owner.task
        assert task is not None
        with pytest.raises(RuntimeError, match="publication handoff unavailable"):
            await task

        durable_high_water = coordinator.event_log.next_sequence() - 1
        committed = coordinator.event_log.get_by_id(
            f"checkpoint_committed:{installed.checkpoint_id}"
        )
        assert committed is not None
        assert owner.committed_terminal is not None
        durable = runtime.materialization_account_store.snapshot()
        assert durable is not None
        assert durable.active_checkpoint_barrier is None
        assert durable.active_reservations == ()

        await service.drain_pending(deadline_monotonic=monotonic() + 2)

        assert coordinator.event_log.next_sequence() - 1 == durable_high_water
        assert coordinator.event_log.get_by_id(committed.id) == committed
        assert service.pending_count == 0

    asyncio.run(scenario())
    runtime.close()


def test_projection_evidence_restores_requested_high_water_when_live_store_is_ahead(
    tmp_path,
) -> None:
    from tests.conftest import emit_test_accepted_model_reply

    from pulsara_agent.event import EventContext, RunStartEvent

    runtime = in_memory_runtime_session(tmp_path)
    context = EventContext(
        run_id="run:projection-evidence-rewind",
        turn_id="turn:projection-evidence-rewind",
        reply_id="reply:projection-evidence-rewind",
    )

    async def scenario() -> None:
        await emit_test_accepted_model_reply(
            runtime,
            event_context=context,
            assistant_text="later accepted reply",
        )
        run_start = next(
            event
            for event in runtime.event_log.iter(run_id=context.run_id)
            if isinstance(event, RunStartEvent)
        )
        assert run_start.sequence is not None
        live = runtime.transcript_projection_state_store.snapshot()

        evidence = await (
            runtime.transcript_projection_checkpoint_service.prepare_projection_evidence(
                requested_through_sequence=run_start.sequence
            )
        )

        assert evidence.semantic_source.semantic_source_event_count < (
            live.stable_semantic_state.semantic_source_event_count
        )
        assert evidence.semantic_source.semantic_source_accumulator != (
            live.stable_semantic_state.semantic_source_accumulator
        )
        assert evidence.domain_completeness_proof.through_sequence == (
            run_start.sequence
        )
        assert any(
            getattr(entry, "attribution", None) is not None
            and entry.attribution.message_id
            == run_start.current_user_message.message_id
            for entry in evidence.stable_entries
        )
        assert evidence.projection_base.common.stable_semantic_state == (
            evidence.projection_base.common.run_seed_semantic.prior_stable_semantic_state
        )

    asyncio.run(scenario())
    runtime.close()


def test_run_seed_restore_hydrates_terminal_documents_from_stable_entries(
    tmp_path,
) -> None:
    from tests.conftest import (
        emit_test_accepted_model_reply,
        persist_test_run_transcript_seed,
    )

    from pulsara_agent.event import EventContext
    from pulsara_agent.primitives.frozen import build_frozen_fact
    from pulsara_agent.primitives.transcript_projection import (
        ProjectionBaseCommonFact,
        ProjectionBaseSemanticIdentityFact,
        RunSeedProjectionBaseFact,
    )
    from pulsara_agent.runtime.authority_materialization import (
        restore_transcript_projection_from_base,
    )
    from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
        stable_entry_projection_references,
    )

    runtime = in_memory_runtime_session(tmp_path)
    context = EventContext(
        run_id="run:seed-terminal-documents",
        turn_id="turn:seed-terminal-documents",
        reply_id="reply:seed-terminal-documents",
    )

    async def scenario() -> None:
        await emit_test_accepted_model_reply(
            runtime,
            event_context=context,
            assistant_text="seeded accepted reply",
        )

    asyncio.run(scenario())
    seed = persist_test_run_transcript_seed(
        runtime,
        run_id="run:next-seed-terminal-documents",
    )
    stable_state = seed.seed_semantic.prior_stable_semantic_state
    semantic_identity = build_frozen_fact(
        ProjectionBaseSemanticIdentityFact,
        schema_version="projection_base_semantic_identity.v2",
        run_seed_semantic_fingerprint=(
            seed.seed_semantic.seed_semantic_fingerprint
        ),
        stable_state_semantic_fingerprint=stable_state.state_semantic_fingerprint,
    )
    projection_base = build_frozen_fact(
        RunSeedProjectionBaseFact,
        schema_version="run_seed_projection_base.v2",
        base_kind="run_seed",
        common=build_frozen_fact(
            ProjectionBaseCommonFact,
            schema_version="projection_base_common.v2",
            run_seed_semantic=seed.seed_semantic,
            run_seed_reference=seed.seed_reference,
            stable_semantic_state=stable_state,
            semantic_identity=semantic_identity,
        ),
    )
    restored = restore_transcript_projection_from_base(
        event_log=runtime.event_log,
        archive=runtime.archive,
        runtime_session_id=runtime.runtime_session_id,
        requested_through_sequence=(
            seed.seed_reference.source_ledger_through_sequence
        ),
        projection_base=projection_base,
        event_domain_binding=runtime.authority_materialization_contracts.event_domain,
        materialization_contracts=(
            runtime.transcript_projection_materialization_contracts
        ),
        limits=runtime.authority_materialization_contracts.limits,
    )
    references = stable_entry_projection_references(restored.stable_entries)
    assert references
    assert all(restored.document_registry.contains(item) for item in references)
    runtime.close()


def test_full_source_doctor_rebuilds_checkpoint(tmp_path) -> None:
    from tests.conftest import emit_test_accepted_model_reply

    from pulsara_agent.event import EventContext
    from pulsara_agent.runtime.authority_materialization import (
        TranscriptProjectionDoctorOutcome,
        build_default_transcript_projection_materialization_contracts,
        verify_or_rebuild_transcript_projection_checkpoint,
    )
    from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
        InMemoryCheckpointMaintenanceAuthority,
    )

    runtime = in_memory_runtime_session(tmp_path)
    context = EventContext(
        run_id="run:transcript-doctor",
        turn_id="turn:transcript-doctor",
        reply_id="reply:transcript-doctor",
    )
    asyncio.run(
        emit_test_accepted_model_reply(
            runtime,
            event_context=context,
            assistant_text="doctor verified reply",
        )
    )
    event_log = runtime.event_log
    archive = runtime.archive
    runtime.close()
    authority = InMemoryCheckpointMaintenanceAuthority(
        is_quiescent=lambda _runtime_session_id: True
    )
    contracts = build_default_authority_materialization_contract_bundle()
    materialization = build_default_transcript_projection_materialization_contracts(
        contracts.limits
    )

    before = verify_or_rebuild_transcript_projection_checkpoint(
        runtime_session_id=event_log.runtime_session_id,
        mode="verify",
        event_log=event_log,
        archive=archive,
        maintenance_authority=authority,
        authority_contracts=contracts,
        materialization_contracts=materialization,
    )
    assert before.outcome is TranscriptProjectionDoctorOutcome.CHECKPOINT_MISSING

    rebuilt = verify_or_rebuild_transcript_projection_checkpoint(
        runtime_session_id=event_log.runtime_session_id,
        mode="rebuild",
        event_log=event_log,
        archive=archive,
        maintenance_authority=authority,
        authority_contracts=contracts,
        materialization_contracts=materialization,
    )
    assert rebuilt.outcome is TranscriptProjectionDoctorOutcome.REBUILT
    assert rebuilt.rebuilt_checkpoint_id is not None
    assert rebuilt.account_state_fingerprint_after != (
        rebuilt.account_state_fingerprint_before
    )

    after = verify_or_rebuild_transcript_projection_checkpoint(
        runtime_session_id=event_log.runtime_session_id,
        mode="verify",
        event_log=event_log,
        archive=archive,
        maintenance_authority=authority,
        authority_contracts=contracts,
        materialization_contracts=materialization,
    )
    assert after.outcome is TranscriptProjectionDoctorOutcome.VERIFIED
    assert rebuilt.rebuilt_checkpoint_id in after.verified_checkpoint_ids

    committed_checkpoint = event_log.get_by_id(
        f"checkpoint_committed:{rebuilt.rebuilt_checkpoint_id}"
    )
    assert committed_checkpoint is not None
    reopened = in_memory_runtime_session(
        tmp_path,
        event_log=event_log,
        archive=archive,
        runtime_session_id=event_log.runtime_session_id,
    )
    assert asyncio.run(
        reopened.transcript_projection_checkpoint_service
        .projection_delta_minimum_sequence()
    ) == (
        committed_checkpoint.checkpoint.candidate_ledger_through_sequence + 1
    )
    reopened.close()

    from pulsara_agent.inspector.service import (
        _authority_materialization_projection,
    )

    class _ArchiveInspectorStore:
        def artifact(self, artifact_id: str) -> dict[str, str] | None:
            try:
                return {
                    "text_body": archive.get_text(
                        artifact_id,
                        session_id=event_log.runtime_session_id,
                    )
                }
            except KeyError:
                return None

    inspector_projection = _authority_materialization_projection(
        tuple(event_log.iter()),
        account=event_log.read_materialization_account_state(),
        store=_ArchiveInspectorStore(),  # type: ignore[arg-type]
    )
    assert inspector_projection["transcript_projection"]["source_kind"] == (
        "checkpoint"
    )
    assert inspector_projection["terminal_projections"][0]["restore_outcome"] == (
        "exact"
    )
    checkpoint_projection = inspector_projection["checkpoint_accelerations"][0]
    assert checkpoint_projection["checkpoint_id"] == rebuilt.rebuilt_checkpoint_id
    assert checkpoint_projection["restore_outcome"] == "not_hydrated"
    assert inspector_projection["ledger_materialization_account"] is not None
    assert inspector_projection["authority_pressure"] == {
        **inspector_projection["authority_pressure"],
        "historical_hard_limits": None,
        "current_config_not_recomputed": True,
    }
    assert inspector_projection["diagnostics"] == []


def test_full_source_doctor_never_bootstraps_missing_account() -> None:
    from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
    from pulsara_agent.runtime.authority_materialization import (
        build_default_transcript_projection_materialization_contracts,
        verify_or_rebuild_transcript_projection_checkpoint,
    )
    from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
        InMemoryCheckpointMaintenanceAuthority,
    )

    event_log = InMemoryEventLog(runtime_session_id="runtime:legacy-doctor")
    event_log.append(
        CustomEvent(
            id="event:legacy-doctor",
            run_id="run:legacy-doctor",
            turn_id="turn:legacy-doctor",
            reply_id="reply:legacy-doctor",
            name="unsupported-old-ledger",
        )
    )
    contracts = build_default_authority_materialization_contract_bundle()
    with pytest.raises(ValueError, match="reset the database"):
        verify_or_rebuild_transcript_projection_checkpoint(
            runtime_session_id=event_log.runtime_session_id,
            mode="rebuild",
            event_log=event_log,
            archive=InMemoryArchiveStore(),
            maintenance_authority=InMemoryCheckpointMaintenanceAuthority(
                is_quiescent=lambda _runtime_session_id: True
            ),
            authority_contracts=contracts,
            materialization_contracts=(
                build_default_transcript_projection_materialization_contracts(
                    contracts.limits
                )
            ),
        )
    assert event_log.read_materialization_account_state() is None


def test_compile_authority_uses_bounded_delta_beyond_legacy_slice_limits() -> None:
    """Historical ledger size must not become a second context window."""

    from types import SimpleNamespace

    from tests.conftest import run_start_permission_fields

    from pulsara_agent.event import RunStartEvent
    from pulsara_agent.runtime.context_input.event_slice import (
        InMemoryContextAuthoritySliceCache,
    )
    from pulsara_agent.runtime.context_input.live import (
        _read_live_primary_event_slice,
    )

    runtime_session_id = "runtime:authority-history-stress"
    event_log = InMemoryEventLog(runtime_session_id=runtime_session_id)
    historical_event_count = 16_385
    historical_value = "x" * 1_025
    event_log.extend(
        tuple(
            CustomEvent(
                id=f"event:history:{index}",
                run_id="run:historical",
                turn_id="turn:historical",
                reply_id="reply:historical",
                name="historical-physical-pressure",
                value={"payload": historical_value},
            )
            for index in range(historical_event_count)
        )
    )
    assert historical_event_count * len(historical_value.encode("utf-8")) > (
        16 * 1024 * 1024
    )

    run_id = "run:authority-history-stress"
    turn_id = "turn:authority-history-stress"
    reply_id = "reply:authority-history-stress"
    run_start_id = f"run_start:test:{run_id}"
    start = event_log.append(
        RunStartEvent(
            id=run_start_id,
            run_id=run_id,
            turn_id=turn_id,
            reply_id=reply_id,
            **run_start_permission_fields(
                run_id,
                user_input="bounded delta",
                turn_id=turn_id,
                reply_id=reply_id,
                mcp_installation_owner_runtime_session_id=runtime_session_id,
                transcript_source_through_sequence=historical_event_count,
                transcript_source_event_count=historical_event_count,
            ),
            user_input_chars=len("bounded delta"),
        )
    )
    tail = event_log.append(
        CustomEvent(
            id="event:authority-history-stress-tail",
            run_id=run_id,
            turn_id=turn_id,
            reply_id=reply_id,
            name="current-delta",
        )
    )
    assert isinstance(start, RunStartEvent)
    assert start.sequence == historical_event_count + 1
    assert tail.sequence == historical_event_count + 2

    class _CheckpointBase:
        async def projection_delta_minimum_sequence(self) -> int:
            return historical_event_count + 1

    class _LongHorizonStore:
        def run_start_by_event_id(self, event_id: str):
            return start if event_id == start.id else None

        def window_state(self, _run_id: str):
            return None

    class _InlineIoService:
        async def execute(self, *, operation_name, operation, deadline_monotonic):
            del operation_name, deadline_monotonic
            return operation()

    runtime_session = SimpleNamespace(
        reconciliation_required=False,
        runtime_session_id=runtime_session_id,
        transcript_projection_checkpoint_service=_CheckpointBase(),
        long_horizon_state_store=_LongHorizonStore(),
        event_log=event_log,
        context_authority_slice_cache=InMemoryContextAuthoritySliceCache(),
        context_input_io_service=_InlineIoService(),
    )
    working_set = SimpleNamespace(
        run_start_event_id=start.id,
        run_start_sequence=start.sequence,
        effective_exposure_event_ref=None,
        plan_snapshot=SimpleNamespace(entered_event_id=None),
        latest_committed_resume_boundary_ref=None,
    )

    authority = asyncio.run(
        _read_live_primary_event_slice(
            runtime_session=runtime_session,
            working_set=working_set,
        )
    )

    assert authority.primary_slice.from_sequence == historical_event_count + 1
    assert authority.primary_slice.through_sequence == tail.sequence
    assert tuple(event.event_id for event in authority.primary_slice.events) == (
        start.id,
        tail.id,
    )


def test_every_concrete_frozen_schema_has_one_unique_fingerprint_contract() -> None:
    from typing import Literal, get_args, get_origin

    from pulsara_agent.primitives.frozen import FrozenFactBase

    def descendants(parent: type[FrozenFactBase]) -> tuple[type[FrozenFactBase], ...]:
        direct = tuple(parent.__subclasses__())
        return direct + tuple(
            nested for child in direct for nested in descendants(child)
        )

    schemas: dict[str, type[FrozenFactBase]] = {}
    for fact_type in descendants(FrozenFactBase):
        field = fact_type.model_fields.get("schema_version")
        if field is None or get_origin(field.annotation) is not Literal:
            continue
        values = get_args(field.annotation)
        if len(values) != 1 or not isinstance(values[0], str):
            continue
        schema_version = values[0]
        assert schema_version not in schemas
        spec = DURABLE_FACT_FINGERPRINT_REGISTRY.resolve(schema_version)
        if spec.own_fingerprint_field is not None:
            assert spec.own_fingerprint_field in fact_type.model_fields
        schemas[schema_version] = fact_type

    registered = {
        item.schema_version
        for item in DURABLE_FACT_FINGERPRINT_REGISTRY.snapshot()
    }
    assert set(schemas) == registered
