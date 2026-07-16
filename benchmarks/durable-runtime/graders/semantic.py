"""Versioned executable semantic assertion contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class SemanticGradeError(ValueError):
    """A benchmark sample is semantically invalid and cannot be accepted."""


@dataclass(frozen=True, slots=True)
class SemanticGrade:
    grader_id: str
    grader_version: str
    passed_assertion_ids: tuple[str, ...]


GRADER_ASSERTION_REGISTRY: dict[tuple[str, str], tuple[str, ...]] = {
    (
        "pulsara.writer.model-semantic-equivalence",
        "1",
    ): (
        "ordered_semantic_content_equal",
        "terminal_projection_equal",
        "physical_settlement_valid",
        "accounted_writer_path_only",
    ),
    (
        "pulsara.writer.structural-semantic-equivalence",
        "1",
    ): (
        "transport_order_equal",
        "start_precedes_delta",
        "end_follows_delta",
        "tool_call_control_safe",
        "committed_model_result_equal",
    ),
    (
        "pulsara.writer.multi-session-isolation",
        "1",
    ): (
        "per_session_sequence_contiguous",
        "cross_session_order_isolated",
        "resource_capacity_bounded",
        "session_close_drained",
    ),
    (
        "pulsara.writer.stable-confirmation",
        "1",
    ): (
        "stable_candidate_reused",
        "exactly_once_storage",
        "full_cancellation_adopted",
        "unknown_outcome_latched",
        "predecessor_order_preserved",
    ),
    (
        "pulsara.writer.mixed-runtime-accounting",
        "1",
    ): (
        "account_high_water_matches",
        "reservations_explained",
        "checkpoint_barriers_closed",
        "terminal_causality_valid",
        "session_close_drained",
    ),
    (
        "pulsara.context.long-plan-authority",
        "1",
    ): (
        "projection_base_stable",
        "semantic_delta_total_exact",
        "authority_manifest_fingerprints_valid",
        "normalized_transcript_equal",
    ),
    (
        "pulsara.context.incremental-authority",
        "1",
    ): (
        "high_water_monotonic",
        "delta_increment_exact",
        "provider_transcript_deterministic",
        "durable_delta_references_complete",
    ),
    (
        "pulsara.context.compaction-authority",
        "1",
    ): (
        "window_base_correct",
        "post_compaction_authority_bounded",
        "summary_source_verified",
        "cold_warm_transcript_equal",
    ),
    (
        "pulsara.context.subagent-authority",
        "1",
    ): (
        "ledger_identity_isolated",
        "child_terminal_reference_exact",
        "dependency_order_valid",
        "checkpoint_restore_semantics_equal",
        "child_result_selected_once",
    ),
    (
        "pulsara.context.artifact-hydration",
        "1",
    ): (
        "artifact_identity_verified",
        "cache_semantics_equal",
        "checkpoint_root_excludes_large_content",
        "authority_fingerprint_stable",
    ),
    (
        "pulsara.context.checkpoint-rebase",
        "1",
    ): (
        "checkpoint_is_acceleration_only",
        "rebase_selection_deterministic",
        "cold_reopen_semantics_equal",
        "delta_read_bounded",
        "provider_semantic_identity_stable",
    ),
}


def validate_grader_contract(
    *,
    grader_id: str,
    grader_version: str,
    assertion_ids: tuple[str, ...],
) -> None:
    expected = GRADER_ASSERTION_REGISTRY.get((grader_id, grader_version))
    if expected is None:
        raise SemanticGradeError(
            f"unsupported semantic grader binding: {grader_id}@{grader_version}"
        )
    if assertion_ids != expected:
        raise SemanticGradeError(
            f"semantic grader assertion contract drifted: {grader_id}"
        )


def grade_semantic_assertions(
    *,
    grader_id: str,
    grader_version: str,
    required_assertion_ids: tuple[str, ...],
    observed_assertions: Mapping[str, bool],
) -> SemanticGrade:
    validate_grader_contract(
        grader_id=grader_id,
        grader_version=grader_version,
        assertion_ids=required_assertion_ids,
    )
    missing = tuple(
        assertion_id
        for assertion_id in required_assertion_ids
        if assertion_id not in observed_assertions
    )
    if missing:
        raise SemanticGradeError(
            f"{grader_id} is missing assertions: {', '.join(missing)}"
        )
    failed = tuple(
        assertion_id
        for assertion_id in required_assertion_ids
        if observed_assertions[assertion_id] is not True
    )
    if failed:
        raise SemanticGradeError(
            f"{grader_id} failed assertions: {', '.join(failed)}"
        )
    unexpected = tuple(
        sorted(set(observed_assertions) - set(required_assertion_ids))
    )
    if unexpected:
        raise SemanticGradeError(
            f"{grader_id} reported undeclared assertions: {', '.join(unexpected)}"
        )
    return SemanticGrade(
        grader_id=grader_id,
        grader_version=grader_version,
        passed_assertion_ids=required_assertion_ids,
    )
