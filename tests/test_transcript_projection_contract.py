from __future__ import annotations

from hashlib import sha256
from time import monotonic
from typing import Any, TypeVar

import pytest
from pydantic import ValidationError

from pulsara_agent.primitives.authority_materialization import (
    LedgerMaterializationConsumerHorizonFact,
    LedgerMaterializationConsumerKind,
    TranscriptProjectionStableSemanticStateFact,
)
from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    TranscriptProjectionWindowFact,
)
from pulsara_agent.primitives.frozen import DURABLE_FACT_FINGERPRINT_REGISTRY
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.transcript_checkpoint import (
    CheckpointDiagnosticSanitizationContractFact,
    CheckpointDiagnosticSanitizerBinding,
    CheckpointDiagnosticSanitizerRegistry,
    sanitize_checkpoint_diagnostic_detail,
)
from pulsara_agent.primitives.transcript_projection import (
    EmptyTranscriptProjectionRootManifestFact,
    InlineNormalizedMessageContentFact,
    RunTranscriptSeedSemanticFact,
    TranscriptInlineBlockAttributionFact,
    TranscriptInlineBlockFact,
    TranscriptMessageAttributionFact,
    TranscriptMessageLeafEntryFact,
    TranscriptMessageLeafSemanticFact,
    TranscriptMessageProviderPlacementSemanticFact,
    TranscriptMessageProviderSemanticFact,
    TranscriptProjectionOrdinalFact,
    TranscriptProjectionSemanticSourceFact,
    TranscriptProjectionTreeContractFact,
    TranscriptProviderTextBlockSemanticFact,
)
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.runtime.authority_materialization import (
    build_default_authority_materialization_contract_bundle,
    build_default_transcript_projection_materialization_contracts,
    build_account_state,
    build_generation,
    canonical_empty_account,
    hydrate_run_transcript_seed,
    persist_prepared_run_transcript_seed,
    prepare_authority_artifact_write_reservation,
    prepare_run_transcript_seed,
    prepare_transcript_checkpoint_candidate,
    prepare_transcript_projection_materialization,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
    TranscriptProjectionDocumentRegistry,
)
from pulsara_agent.runtime.context_input.stable_transcript import (
    project_stable_context_transcript,
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
        candidate.model_dump(mode="json", exclude={spec.own_fingerprint_field}),
    )
    return model(**payload)


def _sanitization_contract(
    *,
    key_tokens: tuple[str, ...] = ("authorization",),
    marker_tokens: tuple[str, ...] = ("secret",),
) -> CheckpointDiagnosticSanitizationContractFact:
    return _fact(
        CheckpointDiagnosticSanitizationContractFact,
        schema_version="checkpoint_diagnostic_sanitization_contract.v2",
        contract_id="checkpoint-diagnostic",
        contract_version="2",
        unicode_normalization="NFC",
        secret_key_normalization="casefold_strip_non_alnum",
        secret_key_tokens=key_tokens,
        secret_marker_tokens=marker_tokens,
        max_token_characters=64,
        max_token_utf8_bytes=256,
        max_secret_key_tokens_total_utf8_bytes=4096,
        max_secret_marker_tokens_total_utf8_bytes=4096,
        max_all_secret_tokens_total_utf8_bytes=4096,
        url_userinfo_policy="remove",
        url_query_policy="remove",
        url_fragment_policy="remove",
        header_policy="remove_all",
        cookie_policy="remove_all",
        control_character_policy="replace_with_space",
        redaction_token="[redacted]",
        max_sanitization_passes=4,
        fixed_point_required=True,
        secret_safe_validation_required=True,
        max_output_characters=256,
        max_output_utf8_bytes=1024,
    )


class _StepSanitizer:
    def __init__(self, outputs: tuple[str, ...], *, safe: bool = True) -> None:
        self.outputs = outputs
        self.safe = safe
        self.calls = 0

    def sanitize_once(self, current_detail: str) -> str | None:
        result = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return result

    def is_secret_safe(self, sanitized_detail: str | None) -> bool:
        return self.safe


def _registry(
    contract: CheckpointDiagnosticSanitizationContractFact,
    sanitizer: _StepSanitizer,
) -> CheckpointDiagnosticSanitizerRegistry:
    registry = CheckpointDiagnosticSanitizerRegistry()
    registry.register(
        CheckpointDiagnosticSanitizerBinding(
            contract_id=contract.contract_id,
            contract_version=contract.contract_version,
            contract_fingerprint=contract.contract_fingerprint,
            implementation_build_fingerprint="build:test",
            sanitizer=sanitizer,
        )
    )
    return registry


def test_checkpoint_diagnostic_sanitizer_uses_exact_four_pass_algorithm() -> None:
    contract = _sanitization_contract()
    sanitizer = _StepSanitizer(("pass-1", "pass-2", "pass-3", "pass-3"))

    result = sanitize_checkpoint_diagnostic_detail(
        "raw",
        contract=contract,
        registry=_registry(contract, sanitizer),
    )

    assert result == "pass-3"
    assert sanitizer.calls == 4


def test_checkpoint_diagnostic_sanitizer_does_not_fail_on_second_change() -> None:
    contract = _sanitization_contract()
    sanitizer = _StepSanitizer(("pass-1", "pass-2", "pass-2"))

    assert sanitize_checkpoint_diagnostic_detail(
        "raw",
        contract=contract,
        registry=_registry(contract, sanitizer),
    ) == "pass-2"
    assert sanitizer.calls == 3


def test_checkpoint_diagnostic_sanitizer_never_runs_after_fourth_pass() -> None:
    contract = _sanitization_contract()
    sanitizer = _StepSanitizer(("one", "two", "three", "four", "five"))

    assert (
        sanitize_checkpoint_diagnostic_detail(
            "raw",
            contract=contract,
            registry=_registry(contract, sanitizer),
        )
        is None
    )
    assert sanitizer.calls == 4


def test_checkpoint_diagnostic_sanitizer_unsafe_output_has_no_fallback() -> None:
    contract = _sanitization_contract()
    sanitizer = _StepSanitizer(("same", "same"), safe=False)

    assert (
        sanitize_checkpoint_diagnostic_detail(
            "same",
            contract=contract,
            registry=_registry(contract, sanitizer),
        )
        is None
    )


def test_checkpoint_diagnostic_tokens_enforce_aggregate_utf8_bound() -> None:
    tokens = tuple(f"{index:02d}" + "\U0001f600" * 62 for index in range(64))
    with pytest.raises(ValidationError, match="aggregate UTF-8 byte bound"):
        _sanitization_contract(key_tokens=tokens)


def test_checkpoint_diagnostic_tokens_enforce_combined_utf8_bound() -> None:
    key_tokens = tuple(f"k{index:02d}-" + "a" * 60 for index in range(64))
    marker_tokens = tuple(f"m{index:02d}-" + "b" * 60 for index in range(64))

    with pytest.raises(ValidationError, match="combined UTF-8 byte bound"):
        _sanitization_contract(
            key_tokens=key_tokens,
            marker_tokens=marker_tokens,
        )


def test_tree_contract_cross_proves_height_and_capacity() -> None:
    with pytest.raises(ValidationError, match="height/fanout proof"):
        _fact(
            TranscriptProjectionTreeContractFact,
            schema_version="transcript_projection_tree_contract.v1",
            tree_contract_id="transcript-tree",
            tree_contract_version="1",
            max_internal_fanout=4,
            max_leaf_entries=8,
            max_inline_entry_bytes=4_096,
            max_node_bytes=65_536,
            max_tree_height=2,
            maximum_representable_entries=33,
            ordinal_contract_fingerprint=_fingerprint("ordinal"),
            node_canonicalization_contract_fingerprint=_fingerprint("node"),
            ordering_contract_fingerprint=_fingerprint("ordering"),
        )


def test_empty_root_has_no_fake_ordinal_or_node_reference() -> None:
    root = _fact(
        EmptyTranscriptProjectionRootManifestFact,
        schema_version="empty_transcript_projection_root.v2",
        root_kind="empty",
        root_manifest_contract_fingerprint=_fingerprint("root-contract"),
        tree_contract_fingerprint=_fingerprint("tree-contract"),
        total_entry_count=0,
        normalized_transcript_fingerprint=_fingerprint("empty-transcript"),
    )

    assert "root_node_ref" not in type(root).model_fields
    assert "first_ordinal" not in type(root).model_fields


def test_empty_run_seed_is_content_addressed_and_has_no_fake_tree_node() -> None:
    authority = build_default_authority_materialization_contract_bundle()
    contracts = build_default_transcript_projection_materialization_contracts(
        authority.limits
    )
    stable = build_frozen_fact(
        TranscriptProjectionStableSemanticStateFact,
        schema_version="transcript_projection_stable_semantic_state.v1",
        semantic_source_event_count=0,
        semantic_source_accumulator=_fingerprint("empty-semantic-prefix"),
        normalized_transcript_fingerprint=context_fingerprint(
            "normalized-transcript-semantic:v1", ()
        ),
    )

    prepared = prepare_run_transcript_seed(
        runtime_session_id="runtime:test",
        stable_state=stable,
        stable_entries=(),
        ledger_through_sequence=0,
        ledger_continuity_accumulator=_fingerprint("empty-ledger-prefix"),
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            authority.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=contracts,
    )

    assert prepared.root_manifest.root_kind == "empty"
    assert prepared.root_reference.root_kind == "empty"
    assert prepared.seed_reference.seed_semantic_fingerprint == (
        prepared.seed_semantic.seed_semantic_fingerprint
    )
    assert prepared.seed_artifact.root_manifest == prepared.root_manifest
    assert prepared.artifacts[-1].artifact_id == prepared.seed_reference.seed_artifact_id


def test_empty_run_seed_round_trips_through_strict_hydrator() -> None:
    authority = build_default_authority_materialization_contract_bundle()
    contracts = build_default_transcript_projection_materialization_contracts(
        authority.limits
    )
    stable = build_frozen_fact(
        TranscriptProjectionStableSemanticStateFact,
        schema_version="transcript_projection_stable_semantic_state.v1",
        semantic_source_event_count=0,
        semantic_source_accumulator=_fingerprint("hydrate-empty-semantic-prefix"),
        normalized_transcript_fingerprint=context_fingerprint(
            "normalized-transcript-semantic:v1", ()
        ),
    )
    prepared = prepare_run_transcript_seed(
        runtime_session_id="runtime:hydrate",
        stable_state=stable,
        stable_entries=(),
        ledger_through_sequence=0,
        ledger_continuity_accumulator=_fingerprint("hydrate-empty-ledger-prefix"),
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            authority.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=contracts,
    )
    archive = InMemoryArchiveStore()
    deadline = monotonic() + 2
    persist_prepared_run_transcript_seed(
        prepared,
        write_reservation=prepare_authority_artifact_write_reservation(
            operation_id="run-seed:hydrate",
            owner_kind="run_seed_materialization",
            artifacts=prepared.artifacts,
            limits=authority.limits,
            absolute_deadline_monotonic=deadline,
        ),
        limits=authority.limits,
        archive=archive,
        runtime_session_id="runtime:hydrate",
        deadline_monotonic=deadline,
    )

    hydrated = hydrate_run_transcript_seed(
        archive=archive,
        runtime_session_id="runtime:hydrate",
        seed_semantic=prepared.seed_semantic,
        seed_reference=prepared.seed_reference,
        contracts=contracts,
        deadline_monotonic=deadline,
    )

    assert hydrated.seed_artifact == prepared.seed_artifact
    assert hydrated.root_manifest == prepared.root_manifest
    assert hydrated.entries == ()


def test_identical_seed_semantics_use_session_scoped_artifact_placement() -> None:
    authority = build_default_authority_materialization_contract_bundle()
    contracts = build_default_transcript_projection_materialization_contracts(
        authority.limits
    )
    stable = build_frozen_fact(
        TranscriptProjectionStableSemanticStateFact,
        schema_version="transcript_projection_stable_semantic_state.v1",
        semantic_source_event_count=0,
        semantic_source_accumulator=_fingerprint("namespace-empty-semantic-prefix"),
        normalized_transcript_fingerprint=context_fingerprint(
            "normalized-transcript-semantic:v1", ()
        ),
    )
    payload = dict(
        stable_state=stable,
        stable_entries=(),
        ledger_through_sequence=0,
        ledger_continuity_accumulator=_fingerprint("namespace-empty-ledger-prefix"),
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            authority.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=contracts,
    )

    parent = prepare_run_transcript_seed(
        runtime_session_id="runtime:parent",
        **payload,
    )
    child = prepare_run_transcript_seed(
        runtime_session_id="runtime:child",
        **payload,
    )

    assert parent.seed_semantic == child.seed_semantic
    assert parent.seed_reference.seed_artifact_id != child.seed_reference.seed_artifact_id


def test_run_seed_closes_source_stable_state_and_transcript_identity() -> None:
    stable = _fact(
        TranscriptProjectionStableSemanticStateFact,
        schema_version="transcript_projection_stable_semantic_state.v1",
        semantic_source_event_count=3,
        semantic_source_accumulator=_fingerprint("source"),
        normalized_transcript_fingerprint=_fingerprint("transcript"),
    )
    source = _fact(
        TranscriptProjectionSemanticSourceFact,
        schema_version="transcript_projection_semantic_source.v1",
        reducer_id="transcript",
        reducer_version="1",
        reducer_contract_fingerprint=_fingerprint("reducer"),
        transcript_semantic_domain_contract_fingerprint=_fingerprint("domain"),
        semantic_source_event_count=3,
        semantic_source_accumulator=_fingerprint("source"),
        resulting_state_fingerprint=stable.state_semantic_fingerprint,
    )
    seed = _fact(
        RunTranscriptSeedSemanticFact,
        schema_version="run_transcript_seed_semantic.v2",
        prior_semantic_source=source,
        prior_stable_semantic_state=stable,
        normalized_prior_transcript_fingerprint=stable.normalized_transcript_fingerprint,
    )

    assert seed.prior_stable_semantic_state == stable


def test_message_provider_semantic_is_physically_separate_from_attribution() -> None:
    placement = _fact(
        TranscriptMessageProviderPlacementSemanticFact,
        schema_version="transcript_message_provider_placement_semantic.v2",
        normalized_lane="prior_history",
        lowering_scope="transcript_prior",
        timing_overlay_kind="historical_replay",
        timing_policy_semantic_fingerprint=_fingerprint("timing-policy"),
        placement_contract_id="placement",
        placement_contract_version="2",
        placement_contract_fingerprint=_fingerprint("placement"),
    )
    semantic = _fact(
        TranscriptMessageProviderSemanticFact,
        schema_version="transcript_message_provider_semantic.v3",
        role="user",
        name=None,
        placement_semantic=placement,
        ordered_block_semantic_fingerprints=(),
    )
    attribution = _fact(
        TranscriptMessageAttributionFact,
        schema_version="transcript_message_attribution.v2",
        message_id="message:1",
        run_id="run:1",
        turn_id="turn:1",
        reply_id="reply:1",
        created_at_utc="2026-07-15T00:00:00.000000Z",
        finished_at_utc=None,
        segment="prior_history",
    )

    assert "message_id" not in type(semantic).model_fields
    assert "compiled_at_utc" not in type(semantic).model_fields
    assert attribution.message_id == "message:1"


def test_stable_projection_lowers_current_user_without_raw_stream_events() -> None:
    source_ref = ContextEventReferenceFact(
        runtime_session_id="runtime:stable-projector",
        event_id="run-start:1",
        sequence=1,
        event_type="RUN_START",
        payload_fingerprint=_fingerprint("run-start:1"),
    )
    block_semantic = _fact(
        TranscriptProviderTextBlockSemanticFact,
        schema_version="transcript_provider_text_block_semantic.v1",
        block_kind="text",
        text="hello",
    )
    block = _fact(
        TranscriptInlineBlockFact,
        schema_version="transcript_inline_block.v1",
        provider_semantic_identity=block_semantic,
        attribution=_fact(
            TranscriptInlineBlockAttributionFact,
            schema_version="transcript_inline_block_attribution.v1",
            block_id="text:user:1",
            block_index=0,
            source_projection_order=None,
        ),
    )
    placement = _fact(
        TranscriptMessageProviderPlacementSemanticFact,
        schema_version="transcript_message_provider_placement_semantic.v2",
        normalized_lane="current_user",
        lowering_scope="leading_user",
        timing_overlay_kind="current_user",
        timing_policy_semantic_fingerprint=_fingerprint("current-user-timing"),
        placement_contract_id="pulsara.transcript-message-placement",
        placement_contract_version="2",
        placement_contract_fingerprint=_fingerprint("placement-contract"),
    )
    provider = _fact(
        TranscriptMessageProviderSemanticFact,
        schema_version="transcript_message_provider_semantic.v3",
        role="user",
        name="user",
        placement_semantic=placement,
        ordered_block_semantic_fingerprints=(block_semantic.semantic_fingerprint,),
    )
    content = _fact(
        InlineNormalizedMessageContentFact,
        schema_version="inline_normalized_message_content.v3",
        content_kind="inline_normalized_message",
        provider_semantic_identity=provider,
        blocks=(block,),
    )
    leaf_semantic = _fact(
        TranscriptMessageLeafSemanticFact,
        schema_version="transcript_message_leaf_semantic.v2",
        semantic_kind="message",
        message_provider_semantic_identity=provider,
    )
    entry = _fact(
        TranscriptMessageLeafEntryFact,
        schema_version="transcript_message_leaf_entry.v3",
        entry_kind="message",
        ordinal=TranscriptProjectionOrdinalFact(
            schema_version="transcript_projection_ordinal.v1",
            encoding="u64_be_hex16",
            value_hex="0000000000000000",
        ),
        semantic_identity=leaf_semantic,
        attribution=_fact(
            TranscriptMessageAttributionFact,
            schema_version="transcript_message_attribution.v2",
            message_id="user:1",
            run_id="run:1",
            turn_id="turn:1",
            reply_id="reply:1",
            created_at_utc="2026-07-15T00:00:00.000000Z",
            finished_at_utc="2026-07-15T00:00:00.000000Z",
            segment="current_user",
        ),
        content=content,
        source_event_refs=(source_ref,),
    )
    window_payload = {
        "window_kind": "uncompacted",
        "compaction_terminal_ref": None,
        "compaction_summary_artifact_id": None,
        "compacted_through_sequence": None,
        "keep_after_sequence": None,
        "window_compaction_started_ref": None,
        "window_compaction_source_document_artifact_id": None,
        "window_compaction_source_document_fingerprint": None,
        "summarized_message_ids": (),
        "retained_message_ids": (),
        "retained_history_from_sequence": None,
        "retained_history_through_sequence": None,
        "protected_run_start_sequence": 1,
        "protected_run_through_sequence": 1,
    }
    window = TranscriptProjectionWindowFact(
        **window_payload,
        window_fingerprint=context_fingerprint(
            "transcript-projection-window:v1", window_payload
        ),
    )

    normalized = project_stable_context_transcript(
        runtime_session_id="runtime:stable-projector",
        through_sequence=1,
        current_user_anchor="user:1",
        projection_window=window,
        stable_entries=(entry,),
        documents=TranscriptProjectionDocumentRegistry(),
    )

    assert normalized.transcript.current_user_anchor == "user:1"
    assert len(normalized.transcript.messages) == 1
    message = normalized.transcript.messages[0]
    assert message.message_id == "user:1"
    assert message.blocks[0].text == "hello"
    assert message.blocks[0].source_events == (source_ref,)
    assert normalized.tool_result_units == ()


def test_oversized_non_terminal_message_uses_typed_content_artifact() -> None:
    source_ref = ContextEventReferenceFact(
        runtime_session_id="runtime:oversized-message",
        event_id="run-start:oversized-message",
        sequence=1,
        event_type="RUN_START",
        payload_fingerprint=_fingerprint("run-start:oversized-message"),
    )
    block_semantic = _fact(
        TranscriptProviderTextBlockSemanticFact,
        schema_version="transcript_provider_text_block_semantic.v1",
        block_kind="text",
        text="x" * (70 * 1024),
    )
    block = _fact(
        TranscriptInlineBlockFact,
        schema_version="transcript_inline_block.v1",
        provider_semantic_identity=block_semantic,
        attribution=_fact(
            TranscriptInlineBlockAttributionFact,
            schema_version="transcript_inline_block_attribution.v1",
            block_id="text:oversized-message",
            block_index=0,
            source_projection_order=None,
        ),
    )
    placement = _fact(
        TranscriptMessageProviderPlacementSemanticFact,
        schema_version="transcript_message_provider_placement_semantic.v2",
        normalized_lane="current_user",
        lowering_scope="leading_user",
        timing_overlay_kind="current_user",
        timing_policy_semantic_fingerprint=_fingerprint("current-user-timing"),
        placement_contract_id="pulsara.transcript-message-placement",
        placement_contract_version="2",
        placement_contract_fingerprint=_fingerprint("placement-contract"),
    )
    provider = _fact(
        TranscriptMessageProviderSemanticFact,
        schema_version="transcript_message_provider_semantic.v3",
        role="user",
        name=None,
        placement_semantic=placement,
        ordered_block_semantic_fingerprints=(block_semantic.semantic_fingerprint,),
    )
    entry = _fact(
        TranscriptMessageLeafEntryFact,
        schema_version="transcript_message_leaf_entry.v3",
        entry_kind="message",
        ordinal=TranscriptProjectionOrdinalFact(
            schema_version="transcript_projection_ordinal.v1",
            encoding="u64_be_hex16",
            value_hex="0000000000000000",
        ),
        semantic_identity=_fact(
            TranscriptMessageLeafSemanticFact,
            schema_version="transcript_message_leaf_semantic.v2",
            semantic_kind="message",
            message_provider_semantic_identity=provider,
        ),
        attribution=_fact(
            TranscriptMessageAttributionFact,
            schema_version="transcript_message_attribution.v2",
            message_id="message:oversized",
            run_id="run:oversized",
            turn_id="turn:oversized",
            reply_id="reply:oversized",
            created_at_utc="2026-07-15T00:00:00.000000Z",
            finished_at_utc="2026-07-15T00:00:00.000000Z",
            segment="current_user",
        ),
        content=_fact(
            InlineNormalizedMessageContentFact,
            schema_version="inline_normalized_message_content.v3",
            content_kind="inline_normalized_message",
            provider_semantic_identity=provider,
            blocks=(block,),
        ),
        source_event_refs=(source_ref,),
    )
    authority = build_default_authority_materialization_contract_bundle()
    contracts = build_default_transcript_projection_materialization_contracts(
        authority.limits
    )
    materialization = prepare_transcript_projection_materialization(
        runtime_session_id="runtime:oversized-message",
        stable_entries=(entry,),
        normalized_transcript_fingerprint=context_fingerprint(
            "normalized-transcript-semantic:v1",
            (entry.semantic_identity.semantic_fingerprint,),
        ),
        contracts=contracts,
    )

    artifact_kinds = tuple(
        artifact.semantic_metadata["artifact_kind"]
        for artifact in materialization.artifacts
    )
    assert "normalized_message_content" in artifact_kinds
    assert materialization.root_reference.root_byte_count <= (
        authority.limits.max_checkpoint_root_bytes
    )


def test_checkpoint_candidate_freezes_semantics_before_cow_artifact_io() -> None:
    from pulsara_agent.primitives.transcript_projection import (
        TranscriptProjectionScopeFact,
    )
    from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
        TranscriptProjectionDocumentRegistry,
        TranscriptProjectionStateStore,
    )

    runtime_session_id = "runtime:checkpoint"
    authority = build_default_authority_materialization_contract_bundle()
    contracts = build_default_transcript_projection_materialization_contracts(
        authority.limits
    )
    store = TranscriptProjectionStateStore(
        runtime_session_id=runtime_session_id,
        documents=TranscriptProjectionDocumentRegistry(),
    )
    stable = store.snapshot().stable_semantic_state
    seed = prepare_run_transcript_seed(
        runtime_session_id=runtime_session_id,
        stable_state=stable,
        stable_entries=(),
        ledger_through_sequence=0,
        ledger_continuity_accumulator=store.snapshot().ledger_continuity_accumulator,
        reducer_id="pulsara.transcript-projection",
        reducer_version="1",
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transcript_semantic_domain_contract_fingerprint=(
            authority.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=contracts,
    )
    empty = canonical_empty_account(
        runtime_session_id=runtime_session_id,
        charge_contract_fingerprint=authority.charge_contract.contract_fingerprint,
    )
    horizon = build_frozen_fact(
        LedgerMaterializationConsumerHorizonFact,
        schema_version="ledger_materialization_consumer_horizon.v1",
        runtime_session_id=runtime_session_id,
        consumer_kind=LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW,
        consumer_id="transcript:run:window:0",
        business_run_id="run:checkpoint",
        business_window_id="window:checkpoint",
        business_window_generation=0,
        through_sequence=0,
        ledger_event_count_through=0,
        ledger_charged_payload_bytes_through=0,
        ledger_continuity_accumulator=store.snapshot().ledger_continuity_accumulator,
        consumer_contract_fingerprint=_fingerprint("consumer-contract"),
    )
    generation = build_generation(
        source=empty.generation,
        consumer_horizons=(horizon,),
        consumer_horizon_revision=1,
    )
    account = build_account_state(
        runtime_session_id=runtime_session_id,
        generation=generation,
        ledger_through_sequence=0,
        ledger_charged_payload_bytes_through=0,
        active_reservations=(),
        active_checkpoint_barrier=None,
        latest_transition_event_ids=(),
        reconciliation_required=False,
        reconciliation_reason_code=None,
    )
    prepared = prepare_transcript_checkpoint_candidate(
        checkpoint_id="checkpoint:1",
        scope=TranscriptProjectionScopeFact(
            schema_version="transcript_projection_scope.v1",
            runtime_session_id=runtime_session_id,
            run_id="run:checkpoint",
            window_id="window:checkpoint",
            window_generation=0,
        ),
        run_seed_semantic=seed.seed_semantic,
        run_seed_reference=seed.seed_reference,
        materialization_consumer=horizon,
        account_state=account,
        transcript_store=store,
        transcript_semantic_domain_contract_fingerprint=(
            authority.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=contracts,
        limits=authority.limits,
    )

    assert prepared.candidate.stable_semantic_state == stable
    assert prepared.candidate.materialization.root_kind == "empty"
    repeated = prepare_transcript_projection_materialization(
        runtime_session_id=runtime_session_id,
        stable_entries=(),
        normalized_transcript_fingerprint=stable.normalized_transcript_fingerprint,
        contracts=contracts,
        previously_reachable_artifact_ids=frozenset(
            item.artifact_id for item in prepared.materialization.artifacts
        ),
    )
    assert repeated.root_reference == prepared.materialization.root_reference
    assert repeated.artifacts == ()
