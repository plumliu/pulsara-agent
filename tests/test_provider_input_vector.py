from __future__ import annotations

from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.context_source import LedgerAuthorityHorizonFact
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.provider_input import (
    ProviderInputReplayBindingIdentityFact,
)
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.runtime.provider_input.vector import (
    load_ledger_horizon_set,
    load_replay_binding_set,
    prepare_ledger_horizon_set,
    prepare_replay_binding_set,
)


def _horizon(index: int) -> LedgerAuthorityHorizonFact:
    return build_frozen_fact(
        LedgerAuthorityHorizonFact,
        schema_version="ledger_authority_horizon.v1",
        runtime_session_id=f"runtime:{index:04d}",
        through_sequence=10,
        ledger_event_count_through=10,
        ledger_continuity_accumulator_through=f"sha256:{index:064x}",
    )


def _binding(index: int) -> ProviderInputReplayBindingIdentityFact:
    return build_frozen_fact(
        ProviderInputReplayBindingIdentityFact,
        schema_version="provider_input_replay_binding_identity.v1",
        binding_kind="context_source",
        contract_id=f"source:{index:04d}",
        contract_version="1",
        schema_or_contract_fingerprint=f"sha256:{index:064x}",
    )


def test_large_ledger_horizon_set_has_a_bounded_persistent_root() -> None:
    prepared = prepare_ledger_horizon_set(_horizon(index) for index in range(1000))

    assert prepared.reference.horizon_count == 1000
    assert prepared.reference.root_node_ref is not None
    assert prepared.reference.root_node_ref.node_kind == "internal"
    assert prepared.reference.root_node_ref.subtree_horizon_count == 1000
    assert len(prepared.artifacts) == 17
    assert len(canonical_json_bytes(prepared.reference.model_dump(mode="json"))) < 2048


def test_replay_binding_set_uses_a_bounded_content_addressed_root() -> None:
    prepared = prepare_replay_binding_set(_binding(index) for index in range(1000))
    repeated = prepare_replay_binding_set(_binding(index) for index in range(1000))

    assert prepared.reference.binding_count == 1000
    assert prepared.reference.root_artifact_ref is not None
    assert len(prepared.artifacts) == 17
    assert prepared.reference == repeated.reference
    assert len(canonical_json_bytes(prepared.reference.model_dump(mode="json"))) < 1024


def test_persistent_horizon_and_replay_sets_round_trip_exactly() -> None:
    archive = InMemoryArchiveStore()
    runtime_session_id = "runtime:provider-vector"
    horizons = tuple(_horizon(index) for index in range(130))
    bindings = tuple(_binding(index) for index in range(130))
    prepared_horizons = prepare_ledger_horizon_set(horizons)
    prepared_bindings = prepare_replay_binding_set(bindings)
    for artifact in (*prepared_horizons.artifacts, *prepared_bindings.artifacts):
        reference = artifact.artifact_reference
        archive.put_text_if_absent_or_confirm_identical(
            reference.artifact_id,
            artifact.canonical_text,
            session_id=runtime_session_id,
            run_id=None,
            media_type=reference.media_type,
            semantic_metadata=artifact.semantic_metadata,
        )

    restored_horizons, horizon_artifacts = load_ledger_horizon_set(
        archive=archive,
        runtime_session_id=runtime_session_id,
        reference=prepared_horizons.reference,
        deadline_monotonic=10**9,
    )
    restored_bindings, binding_artifacts = load_replay_binding_set(
        archive=archive,
        runtime_session_id=runtime_session_id,
        reference=prepared_bindings.reference,
        deadline_monotonic=10**9,
    )

    assert restored_horizons == horizons
    assert restored_bindings == tuple(
        sorted(bindings, key=lambda item: item.identity_fingerprint)
    )
    assert len(horizon_artifacts) == len(prepared_horizons.artifacts)
    assert len(binding_artifacts) == len(prepared_bindings.artifacts)
