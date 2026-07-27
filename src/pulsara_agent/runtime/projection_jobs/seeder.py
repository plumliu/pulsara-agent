"""Pure factories for durable EventLog-to-projection admission."""

from __future__ import annotations

from hashlib import sha256
from typing import cast

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.runtime_event_vocabulary import (
    build_bounded_runtime_failure_diagnostic,
)
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionJobCandidateFact,
    DurableProjectionLedgerHorizonFact,
    DurableProjectionSeedRepairActionFact,
    DurableProjectionSeedCommitCandidateFact,
    DurableProjectionSeedFailureCommitCandidateFact,
    DurableProjectionSeedFailureFact,
    DurableProjectionSeedFailureResolutionFact,
    DurableProjectionSeedStateFact,
    DurableProjectionSessionCutoverFact,
    DurableRepairAuthorityReferenceFact,
    build_projection_fact,
)


def seed_candidate_accumulator_genesis(
    *,
    runtime_session_id: str,
    projection_kind: str,
    cutover_fingerprint: str,
    seed_contract_fingerprint: str,
) -> str:
    """Return the canonical empty candidate accumulator for one cutover."""

    return context_fingerprint(
        "durable-projection-admitted-job-candidate-accumulator-genesis:v1",
        {
            "runtime_session_id": runtime_session_id,
            "projection_kind": projection_kind,
            "cutover_fingerprint": cutover_fingerprint,
            "seed_contract_fingerprint": seed_contract_fingerprint,
        },
    )


def advance_seed_candidate_accumulator(
    previous_accumulator: str,
    candidate: DurableProjectionJobCandidateFact,
) -> str:
    return context_fingerprint(
        "durable-projection-admitted-job-candidate-accumulator:v1",
        {
            "previous_accumulator": previous_accumulator,
            "job_candidate_fingerprint": candidate.candidate_fingerprint,
        },
    )


def canonical_seed_state(
    cutover: DurableProjectionSessionCutoverFact,
) -> DurableProjectionSeedStateFact:
    """Build the only legal checkpoint genesis for an active cutover."""

    return cast(
        DurableProjectionSeedStateFact,
        build_projection_fact(
            DurableProjectionSeedStateFact,
            schema_version="durable_projection_seed_state.v1",
            runtime_session_id=cutover.runtime_session_id,
            projection_kind=cutover.projection_kind,
            cutover_fingerprint=cutover.cutover_fingerprint,
            through_sequence=cutover.cutover_through_sequence,
            ledger_continuity_accumulator=(
                cutover.cutover_ledger_continuity_accumulator
            ),
            ledger_payload_prefix_bytes=(cutover.cutover_ledger_payload_prefix_bytes),
            transcript_semantic_prefix_count=(
                cutover.cutover_transcript_semantic_prefix_count
            ),
            transcript_semantic_prefix_accumulator=(
                cutover.cutover_transcript_semantic_prefix_accumulator
            ),
            admitted_job_candidate_count=0,
            admitted_job_candidate_accumulator=(
                seed_candidate_accumulator_genesis(
                    runtime_session_id=cutover.runtime_session_id,
                    projection_kind=cutover.projection_kind.value,
                    cutover_fingerprint=cutover.cutover_fingerprint,
                    seed_contract_fingerprint=cutover.seed_contract_fingerprint,
                )
            ),
            seed_contract_fingerprint=cutover.seed_contract_fingerprint,
        ),
    )


def build_seed_commit_candidate(
    *,
    expected_state: DurableProjectionSeedStateFact,
    scan_horizon: DurableProjectionLedgerHorizonFact,
    ordered_job_candidates: tuple[DurableProjectionJobCandidateFact, ...],
    source_event_count: int,
    source_payload_bytes: int,
    repaired_seed_failure_fingerprint: str | None = None,
    seed_repair_action_fingerprint: str | None = None,
) -> DurableProjectionSeedCommitCandidateFact:
    """Build one bounded, page-independent seed transaction candidate."""

    if scan_horizon.runtime_session_id != expected_state.runtime_session_id:
        raise ValueError("seed scan horizon belongs to another runtime")
    if scan_horizon.through_sequence < expected_state.through_sequence:
        raise ValueError("seed scan horizon cannot move backwards")
    ordered_identity = tuple(
        (
            item.job_semantic.source_event_reference.sequence,
            item.job_semantic.source_event_reference.event_id,
            item.job_semantic.job_id,
        )
        for item in ordered_job_candidates
    )
    if ordered_identity != tuple(sorted(ordered_identity)):
        raise ValueError("seed jobs must follow source/event/job order")
    accumulator = expected_state.admitted_job_candidate_accumulator
    for item in ordered_job_candidates:
        semantic = item.job_semantic
        if (
            semantic.projection_kind is not expected_state.projection_kind
            or semantic.source_event_reference.runtime_session_id
            != expected_state.runtime_session_id
            or semantic.source_event_reference.sequence
            <= expected_state.through_sequence
            or semantic.source_event_reference.sequence > scan_horizon.through_sequence
            or semantic.trigger_horizon.through_sequence
            != semantic.source_event_reference.sequence
            or item.seed_contract_fingerprint
            != expected_state.seed_contract_fingerprint
        ):
            raise ValueError("seed job candidate authority mismatch")
        accumulator = advance_seed_candidate_accumulator(accumulator, item)
    resulting_state = cast(
        DurableProjectionSeedStateFact,
        build_projection_fact(
            DurableProjectionSeedStateFact,
            schema_version="durable_projection_seed_state.v1",
            runtime_session_id=expected_state.runtime_session_id,
            projection_kind=expected_state.projection_kind,
            cutover_fingerprint=expected_state.cutover_fingerprint,
            through_sequence=scan_horizon.through_sequence,
            ledger_continuity_accumulator=(scan_horizon.ledger_continuity_accumulator),
            ledger_payload_prefix_bytes=(scan_horizon.ledger_payload_prefix_bytes),
            transcript_semantic_prefix_count=(
                scan_horizon.transcript_semantic_prefix_count
            ),
            transcript_semantic_prefix_accumulator=(
                scan_horizon.transcript_semantic_prefix_accumulator
            ),
            admitted_job_candidate_count=(
                expected_state.admitted_job_candidate_count
                + len(ordered_job_candidates)
            ),
            admitted_job_candidate_accumulator=accumulator,
            seed_contract_fingerprint=expected_state.seed_contract_fingerprint,
        ),
    )
    return cast(
        DurableProjectionSeedCommitCandidateFact,
        build_projection_fact(
            DurableProjectionSeedCommitCandidateFact,
            schema_version="durable_projection_seed_commit_candidate.v1",
            runtime_session_id=expected_state.runtime_session_id,
            projection_kind=expected_state.projection_kind,
            expected_seed_state=expected_state,
            resulting_seed_state=resulting_state,
            scan_horizon=scan_horizon,
            repaired_seed_failure_fingerprint=(repaired_seed_failure_fingerprint),
            seed_repair_action_fingerprint=seed_repair_action_fingerprint,
            ordered_job_candidates=ordered_job_candidates,
            source_event_count=source_event_count,
            source_payload_bytes=source_payload_bytes,
        ),
    )


def build_seed_failure_commit_candidate(
    *,
    cutover: DurableProjectionSessionCutoverFact,
    activation_fingerprint: str,
    expected_state: DurableProjectionSeedStateFact,
    failure_kind: str,
    error: BaseException,
    observed_scan_horizon: DurableProjectionLedgerHorizonFact | None = None,
    conflicting_source_event_reference_fingerprint: str | None = None,
) -> DurableProjectionSeedFailureCommitCandidateFact:
    """Freeze one deterministic per-authority seed failure."""

    allowed_failure_kinds = {
        "active_cutover_missing",
        "ledger_account_missing",
        "ledger_account_prefix_conflict",
        "source_authority_conflict",
        "historical_decoder_unavailable",
        "trigger_contract_mismatch",
        "job_identity_conflict",
    }
    if failure_kind not in allowed_failure_kinds:
        raise ValueError("unknown durable projection seed failure kind")
    if (
        expected_state.runtime_session_id != cutover.runtime_session_id
        or expected_state.projection_kind is not cutover.projection_kind
        or expected_state.cutover_fingerprint != cutover.cutover_fingerprint
    ):
        raise ValueError("seed failure state/cutover authority mismatch")
    blocked_from = expected_state.through_sequence + 1
    blocked_through = max(
        blocked_from,
        (
            observed_scan_horizon.through_sequence
            if observed_scan_horizon is not None
            else blocked_from
        ),
    )
    identity = {
        "runtime_session_id": cutover.runtime_session_id,
        "projection_kind": cutover.projection_kind.value,
        "activation_fingerprint": activation_fingerprint,
        "expected_seed_state_fingerprint": expected_state.state_fingerprint,
        "blocked_from_sequence": blocked_from,
        "blocked_through_sequence": blocked_through,
        "observed_scan_horizon_fingerprint": (
            observed_scan_horizon.horizon_fingerprint
            if observed_scan_horizon is not None
            else None
        ),
        "failure_kind": failure_kind,
        "conflicting_source_event_reference_fingerprint": (
            conflicting_source_event_reference_fingerprint
        ),
        "seed_contract_fingerprint": cutover.seed_contract_fingerprint,
    }
    failure_id = (
        "projection-seed-failure:"
        + sha256(
            context_fingerprint(
                "durable-projection-seed-failure-id:v1",
                identity,
            ).encode("ascii")
        ).hexdigest()
    )
    failure = cast(
        DurableProjectionSeedFailureFact,
        build_projection_fact(
            DurableProjectionSeedFailureFact,
            schema_version="durable_projection_seed_failure.v1",
            failure_id=failure_id,
            runtime_session_id=cutover.runtime_session_id,
            projection_kind=cutover.projection_kind,
            activation_fingerprint=activation_fingerprint,
            expected_seed_state_fingerprint=expected_state.state_fingerprint,
            blocked_from_sequence=blocked_from,
            blocked_through_sequence=blocked_through,
            observed_scan_horizon=observed_scan_horizon,
            failure_kind=failure_kind,
            conflicting_source_event_reference_fingerprint=(
                conflicting_source_event_reference_fingerprint
            ),
            diagnostic=build_bounded_runtime_failure_diagnostic(
                error=error,
                redaction_profile_id="durable_projection_seed_error.v1",
            ),
            seed_contract_fingerprint=cutover.seed_contract_fingerprint,
        ),
    )
    return cast(
        DurableProjectionSeedFailureCommitCandidateFact,
        build_projection_fact(
            DurableProjectionSeedFailureCommitCandidateFact,
            schema_version=("durable_projection_seed_failure_commit_candidate.v1"),
            runtime_session_id=cutover.runtime_session_id,
            projection_kind=cutover.projection_kind,
            activation_fingerprint=activation_fingerprint,
            expected_seed_state_fingerprint=expected_state.state_fingerprint,
            failure=failure,
        ),
    )


def build_seed_repair_action(
    *,
    failure: DurableProjectionSeedFailureFact,
    expected_state: DurableProjectionSeedStateFact,
    action: str,
    operator_authority_id: str,
    repair_generation: int,
    predecessor_repair_action_fingerprint: str | None,
) -> DurableProjectionSeedRepairActionFact:
    """Freeze the only typed authority that may release one seed latch."""

    if action not in {
        "retry_after_authority_repair",
        "reverify_after_schema_repair",
    }:
        raise ValueError("unknown durable projection seed repair action")
    if not operator_authority_id.strip():
        raise ValueError("seed repair operator authority must be non-empty")
    if (
        failure.runtime_session_id != expected_state.runtime_session_id
        or failure.projection_kind is not expected_state.projection_kind
        or failure.expected_seed_state_fingerprint != expected_state.state_fingerprint
    ):
        raise ValueError("seed repair state/failure authority mismatch")
    authority_semantic_fingerprint = context_fingerprint(
        "durable-projection-seed-repair-operator-authority:v1",
        {
            "failure_fingerprint": failure.failure_fingerprint,
            "expected_seed_state_fingerprint": expected_state.state_fingerprint,
            "action": action,
            "operator_authority_id": operator_authority_id,
            "repair_generation": repair_generation,
        },
    )
    authority = cast(
        DurableRepairAuthorityReferenceFact,
        build_projection_fact(
            DurableRepairAuthorityReferenceFact,
            schema_version="durable_repair_authority_reference.v1",
            authority_kind="source_authority_repair",
            authority_id=operator_authority_id,
            authority_semantic_fingerprint=authority_semantic_fingerprint,
        ),
    )
    repair_action_id = (
        "projection-seed-repair:"
        + sha256(
            context_fingerprint(
                "durable-projection-seed-repair-action-id:v1",
                {
                    "failure_fingerprint": failure.failure_fingerprint,
                    "expected_seed_state_fingerprint": expected_state.state_fingerprint,
                    "action": action,
                    "operator_authority_id": operator_authority_id,
                    "repair_generation": repair_generation,
                    "predecessor_repair_action_fingerprint": (
                        predecessor_repair_action_fingerprint
                    ),
                },
            ).encode("ascii")
        ).hexdigest()
    )
    return cast(
        DurableProjectionSeedRepairActionFact,
        build_projection_fact(
            DurableProjectionSeedRepairActionFact,
            schema_version="durable_projection_seed_repair_action.v1",
            repair_action_id=repair_action_id,
            runtime_session_id=failure.runtime_session_id,
            projection_kind=failure.projection_kind,
            expected_seed_failure_fingerprint=failure.failure_fingerprint,
            expected_seed_state_fingerprint=expected_state.state_fingerprint,
            action=action,
            authority_references=(authority,),
            repair_generation=repair_generation,
            predecessor_repair_action_fingerprint=(
                predecessor_repair_action_fingerprint
            ),
        ),
    )


def build_seed_failure_resolution(
    *,
    failure: DurableProjectionSeedFailureFact,
    repair_action: DurableProjectionSeedRepairActionFact,
    resulting_state: DurableProjectionSeedStateFact,
) -> DurableProjectionSeedFailureResolutionFact:
    """Bind one repaired latch to the exact checkpoint that crossed it."""

    if (
        repair_action.runtime_session_id != failure.runtime_session_id
        or repair_action.projection_kind is not failure.projection_kind
        or repair_action.expected_seed_failure_fingerprint
        != failure.failure_fingerprint
        or resulting_state.runtime_session_id != failure.runtime_session_id
        or resulting_state.projection_kind is not failure.projection_kind
        or resulting_state.through_sequence < failure.blocked_from_sequence
    ):
        raise ValueError("seed failure resolution authority mismatch")
    return cast(
        DurableProjectionSeedFailureResolutionFact,
        build_projection_fact(
            DurableProjectionSeedFailureResolutionFact,
            schema_version=("durable_projection_seed_failure_resolution.v1"),
            seed_failure_fingerprint=failure.failure_fingerprint,
            repair_action_fingerprint=repair_action.action_fingerprint,
            resulting_seed_state_fingerprint=resulting_state.state_fingerprint,
            resolved_through_sequence=resulting_state.through_sequence,
        ),
    )


__all__ = [
    "advance_seed_candidate_accumulator",
    "build_seed_commit_candidate",
    "build_seed_failure_resolution",
    "build_seed_failure_commit_candidate",
    "build_seed_repair_action",
    "canonical_seed_state",
    "seed_candidate_accumulator_genesis",
]
