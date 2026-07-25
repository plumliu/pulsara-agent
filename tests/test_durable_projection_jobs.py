from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from pulsara_agent.primitives._context_base import (
    FrozenJsonObjectFact,
    context_fingerprint,
)
from pulsara_agent.runtime.projection_jobs.contracts import (
    DurableProjectionCommitConfirmation,
    DurableProjectionFailureKind,
    DurableProjectionJobCandidateFact,
    DurableProjectionJobSemanticFact,
    DurableProjectionJobStatus,
    DurableProjectionKind,
    DurableProjectionLedgerHorizonFact,
    DurableProjectionResultDocumentReferenceFact,
    DurableProjectionResultSemanticFact,
    DurableProjectionSeedCommitCandidateFact,
    DurableProjectionSeedStateFact,
    DurableProjectionSourceEventReferenceFact,
    PreparedDurableProjectionResultFact,
    ProjectionJobResultOwnerFact,
    build_projection_fact,
    durable_projection_job_id,
    projection_target_key,
)
from pulsara_agent.runtime.projection_jobs.registry import (
    DURABLE_PROJECTION_TRIGGER_REGISTRY,
    validate_projection_registry_completeness,
)
from pulsara_agent.runtime.projection_jobs.projection_handlers import (
    _strict_tool_arguments_evidence,
)
from pulsara_agent.runtime.projection_jobs.repository import (
    InMemoryDurableProjectionRepository,
)


def _fp(label: str) -> str:
    return context_fingerprint("test-durable-projection:v1", label)


def _source(
    *,
    sequence: int,
    event_id: str,
    event_type: str,
    run_id: str = "run:test",
) -> tuple[
    DurableProjectionSourceEventReferenceFact,
    DurableProjectionLedgerHorizonFact,
]:
    reference = cast(
        DurableProjectionSourceEventReferenceFact,
        build_projection_fact(
            DurableProjectionSourceEventReferenceFact,
            schema_version="durable_projection_source_event_reference.v1",
            runtime_session_id="runtime:test",
            run_id=run_id,
            turn_id="turn:test",
            reply_id="reply:test",
            event_id=event_id,
            sequence=sequence,
            event_type=event_type,
            event_schema_version="test.v1",
            event_schema_fingerprint=_fp(f"schema:{event_type}"),
            event_domain_contract_fingerprint=_fp(f"domain:{event_type}"),
            payload_fingerprint=_fp(f"payload:{event_id}"),
            stored_envelope_fingerprint=_fp(f"envelope:{event_id}"),
        ),
    )
    horizon = cast(
        DurableProjectionLedgerHorizonFact,
        build_projection_fact(
            DurableProjectionLedgerHorizonFact,
            schema_version="durable_projection_ledger_horizon.v1",
            runtime_session_id="runtime:test",
            through_sequence=sequence,
            ledger_continuity_accumulator=_fp(f"ledger:{sequence}"),
            ledger_payload_prefix_bytes=sequence * 10,
            transcript_semantic_prefix_count=sequence,
            transcript_semantic_prefix_accumulator=_fp(
                f"transcript:{sequence}"
            ),
        ),
    )
    return reference, horizon


def _candidate(
    *,
    kind: DurableProjectionKind,
    sequence: int,
    event_id: str,
    target_key: str,
) -> DurableProjectionJobCandidateFact:
    seed = DURABLE_PROJECTION_TRIGGER_REGISTRY.resolve(kind)
    source, horizon = _source(
        sequence=sequence,
        event_id=event_id,
        event_type=seed.ordered_trigger_bindings[0].trigger_event_type,
    )
    job_id = durable_projection_job_id(
        projection_kind=kind,
        source_event_reference=source,
        target_key=target_key,
        handler_contract_fingerprint=seed.handler_contract.contract_fingerprint,
    )
    semantic = cast(
        DurableProjectionJobSemanticFact,
        build_projection_fact(
            DurableProjectionJobSemanticFact,
            schema_version="durable_projection_job_semantic.v1",
            job_id=job_id,
            projection_kind=kind,
            target_key=target_key,
            source_event_reference=source,
            trigger_horizon=horizon,
            handler_contract=seed.handler_contract,
        ),
    )
    return cast(
        DurableProjectionJobCandidateFact,
        build_projection_fact(
            DurableProjectionJobCandidateFact,
            schema_version="durable_projection_job_candidate.v1",
            job_semantic=semantic,
            activation_fingerprint=_fp(f"activation:{kind.value}"),
            seed_contract_fingerprint=seed.seed_contract_fingerprint,
            delivery_policy=seed.delivery_policy,
            canonical_mutation_surface_plan=(
                seed.canonical_mutation_surface_plan
            ),
        ),
    )


def _seed(
    repository: InMemoryDurableProjectionRepository,
    candidates: tuple[DurableProjectionJobCandidateFact, ...],
) -> None:
    kind = candidates[0].job_semantic.projection_kind
    sequence = max(
        item.job_semantic.source_event_reference.sequence
        for item in candidates
    )
    _, scan_horizon = _source(
        sequence=sequence,
        event_id=f"scan:{sequence}",
        event_type="SCAN",
    )
    initial = cast(
        DurableProjectionSeedStateFact,
        build_projection_fact(
            DurableProjectionSeedStateFact,
            schema_version="durable_projection_seed_state.v1",
            runtime_session_id="runtime:test",
            projection_kind=kind,
            cutover_fingerprint=_fp(f"cutover:{kind.value}"),
            through_sequence=0,
            ledger_continuity_accumulator=_fp("ledger:0"),
            ledger_payload_prefix_bytes=0,
            transcript_semantic_prefix_count=0,
            transcript_semantic_prefix_accumulator=_fp("transcript:0"),
            admitted_job_candidate_count=0,
            admitted_job_candidate_accumulator=_fp("jobs:0"),
            seed_contract_fingerprint=(
                DURABLE_PROJECTION_TRIGGER_REGISTRY.resolve(
                    kind
                ).seed_contract_fingerprint
            ),
        ),
    )
    job_accumulator = initial.admitted_job_candidate_accumulator
    for candidate in candidates:
        job_accumulator = context_fingerprint(
            "durable-projection-admitted-job-candidate-accumulator:v1",
            {
                "previous_accumulator": job_accumulator,
                "job_candidate_fingerprint": candidate.candidate_fingerprint,
            },
        )
    resulting = cast(
        DurableProjectionSeedStateFact,
        build_projection_fact(
            DurableProjectionSeedStateFact,
            schema_version="durable_projection_seed_state.v1",
            runtime_session_id="runtime:test",
            projection_kind=kind,
            cutover_fingerprint=initial.cutover_fingerprint,
            through_sequence=sequence,
            ledger_continuity_accumulator=(
                scan_horizon.ledger_continuity_accumulator
            ),
            ledger_payload_prefix_bytes=scan_horizon.ledger_payload_prefix_bytes,
            transcript_semantic_prefix_count=(
                scan_horizon.transcript_semantic_prefix_count
            ),
            transcript_semantic_prefix_accumulator=(
                scan_horizon.transcript_semantic_prefix_accumulator
            ),
            admitted_job_candidate_count=len(candidates),
            admitted_job_candidate_accumulator=job_accumulator,
            seed_contract_fingerprint=initial.seed_contract_fingerprint,
        ),
    )
    repository.install_seed_state(initial)
    commit = cast(
        DurableProjectionSeedCommitCandidateFact,
        build_projection_fact(
            DurableProjectionSeedCommitCandidateFact,
            schema_version="durable_projection_seed_commit_candidate.v1",
            runtime_session_id=initial.runtime_session_id,
            projection_kind=kind,
            expected_seed_state=initial,
            resulting_seed_state=resulting,
            scan_horizon=scan_horizon,
            repaired_seed_failure_fingerprint=None,
            seed_repair_action_fingerprint=None,
            ordered_job_candidates=candidates,
            source_event_count=sequence,
            source_payload_bytes=scan_horizon.ledger_payload_prefix_bytes,
        ),
    )
    assert (
        repository.commit_seed(commit).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )


def _prepared(candidate: DurableProjectionJobCandidateFact) -> PreparedDurableProjectionResultFact:
    owner = cast(
        ProjectionJobResultOwnerFact,
        build_projection_fact(
            ProjectionJobResultOwnerFact,
            schema_version="projection_job_result_owner.v1",
            owner_kind="durable_projection_job",
            job_id=candidate.job_semantic.job_id,
            job_semantic_fingerprint=(
                candidate.job_semantic.job_semantic_fingerprint
            ),
            job_candidate_fingerprint=candidate.candidate_fingerprint,
            source_event_reference_fingerprint=(
                candidate.job_semantic.source_event_reference.reference_fingerprint
            ),
        ),
    )
    semantic = cast(
        DurableProjectionResultSemanticFact,
        build_projection_fact(
            DurableProjectionResultSemanticFact,
            schema_version="durable_projection_result_semantic.v1",
            projection_kind=candidate.job_semantic.projection_kind,
            source_projection_fingerprint=_fp(
                f"result:{candidate.job_semantic.job_id}"
            ),
            ordered_document_semantic_fingerprints=(),
            ordered_canonical_mutation_semantic_fingerprints=(),
        ),
    )
    return cast(
        PreparedDurableProjectionResultFact,
        build_projection_fact(
            PreparedDurableProjectionResultFact,
            schema_version="prepared_durable_projection_result.v1",
            result_owner=owner,
            result_semantic=semantic,
            ordered_documents=(),
            canonical_mutation_candidates=(),
        ),
    )


def test_projection_registries_cover_closed_kind_set() -> None:
    validate_projection_registry_completeness()


def test_evidence_target_excludes_result_identity() -> None:
    first = projection_target_key(
        projection_kind=DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE,
        runtime_session_id="runtime:test",
        run_id="run:test",
        tool_call_id="call:1",
    )
    second = projection_target_key(
        projection_kind=DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE,
        runtime_session_id="runtime:test",
        run_id="run:test",
        tool_call_id="call:1",
    )
    assert first == second


def test_evidence_is_single_assignment_and_second_terminal_conflicts() -> None:
    target = projection_target_key(
        projection_kind=DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE,
        runtime_session_id="runtime:test",
        run_id="run:test",
        tool_call_id="call:1",
    )
    first = _candidate(
        kind=DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE,
        sequence=1,
        event_id="event:first",
        target_key=target,
    )
    second = _candidate(
        kind=DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE,
        sequence=2,
        event_id="event:second",
        target_key=target,
    )
    repository = InMemoryDurableProjectionRepository()
    _seed(repository, (first, second))
    first_lease = repository.claim_due(owner_id="worker:1", limit=1)[0]
    assert first_lease.job.job_id == first.job_semantic.job_id
    applied = repository.settle_success(
        lease=first_lease,
        prepared=_prepared(first),
    )
    assert applied.resulting_status is DurableProjectionJobStatus.SUCCEEDED
    assert repository.claim_due(owner_id="worker:1", limit=1) == ()
    second_record = repository.read_job(second.job_semantic.job_id)
    assert second_record is not None
    assert second_record.state.status is DurableProjectionJobStatus.DEAD_LETTER
    assert len(repository.conflicts()) == 1


def test_result_document_union_rejects_unknown_and_cross_branch_fields() -> None:
    adapter = TypeAdapter(DurableProjectionResultDocumentReferenceFact)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "schema_version": "unknown.v1",
                "document_kind": "projection_receipt",
            }
        )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "schema_version": (
                    "durable_projection_graph_relation_reference.v1"
                ),
                "document_kind": "graph_relation",
                "relation_id": "relation:1",
                "graph_id": "graph:default",
                "source_document_id": "turn:1",
                "predicate_iri": "https://pulsara.dev/runtime#produced",
                "target_document_id": "tool-result:1",
                "relation_semantic_fingerprint": _fp("relation"),
                "lowering_contract_fingerprint": _fp("lowering"),
                "reference_fingerprint": _fp("forged"),
                "artifact_reference": {},
            }
        )


def test_retry_delay_is_deterministic_and_bounded() -> None:
    target = projection_target_key(
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
        runtime_session_id="runtime:test",
        run_id="run:test",
    )
    candidate = _candidate(
        kind=DurableProjectionKind.RUN_TIMELINE,
        sequence=1,
        event_id="event:timeline",
        target_key=target,
    )
    repository = InMemoryDurableProjectionRepository()
    _seed(repository, (candidate,))
    lease = repository.claim_due(owner_id="worker:1", limit=1)[0]
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    outcome = repository.settle_failure(
        lease=lease,
        # Retryable source lag must keep the durable owner.
        failure_kind=DurableProjectionFailureKind.SOURCE_NOT_READY,
        error=RuntimeError("Bearer secret-token"),
        now=now,
    )
    assert outcome.resulting_status is DurableProjectionJobStatus.RETRY_WAIT
    state = repository.read_job(candidate.job_semantic.job_id).state
    assert state.next_attempt_at is not None
    assert (state.next_attempt_at - now).total_seconds() == 1
    assert state.last_failure is not None
    assert "secret-token" not in state.last_failure.redacted_message


@pytest.mark.parametrize(
    ("raw", "error_code"),
    (
        ('{"value": NaN}', "non_finite_number"),
        ('{"value": Infinity}', "non_finite_number"),
        ('{"value": 1, "value": 2}', "duplicate_object_key"),
    ),
)
def test_tool_argument_evidence_treats_noncanonical_json_as_typed_malformed(
    raw: str,
    error_code: str,
) -> None:
    disposition, parsed, observed_error, canonical, _summary = (
        _strict_tool_arguments_evidence(raw)
    )
    assert disposition == "invalid_json"
    assert parsed is None
    assert observed_error == error_code
    assert canonical is None


def test_tool_argument_evidence_freezes_valid_object_recursively() -> None:
    disposition, parsed, error, canonical, _summary = (
        _strict_tool_arguments_evidence(
            '{"nested": {"items": [1, {"ok": true}]}}'
        )
    )
    assert disposition == "valid_object"
    assert error is None
    assert canonical is not None
    assert isinstance(parsed, FrozenJsonObjectFact)
    with pytest.raises(ValidationError):
        parsed.entries[0].value = None  # type: ignore[misc]
