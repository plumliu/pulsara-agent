"""Source-derived execution policy for compaction-memory extraction jobs."""

from __future__ import annotations

from typing import cast

from pulsara_agent.primitives.compaction import (
    DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
    EXTRACTION_INPUT_BUDGET_SELECTION_CONTRACT_FINGERPRINT,
    CompactionMemoryExtractionPolicyFact,
)
from pulsara_agent.primitives.model_call import ResolvedModelTargetFact
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionDeliveryPolicyFact,
    DurableProjectionPhysicalPolicyFact,
    DurableProjectionRetryPolicyFact,
    build_projection_fact,
)


DEFAULT_COMPACTION_MEMORY_MAXIMUM_ATTEMPTS = 3
DEFAULT_COMPACTION_MEMORY_PROVIDER_TIMEOUT_SECONDS = 120
DEFAULT_COMPACTION_MEMORY_LEASE_DURATION_SECONDS = 180
_RETRY_BASE_DELAY_MILLISECONDS = 1_000
_RETRY_MAXIMUM_DELAY_MILLISECONDS = 30_000
_CLAIM_BATCH_SIZE = 4


def compaction_memory_delivery_policy_from_request(
    policy: CompactionMemoryExtractionPolicyFact,
) -> DurableProjectionDeliveryPolicyFact:
    """Derive the complete D3 delivery policy from one stored Request policy."""

    delivery = _build_delivery_policy(
        maximum_attempts=policy.maximum_attempts,
        provider_timeout_seconds=policy.provider_timeout_seconds,
        lease_duration_seconds=policy.lease_duration_seconds,
    )
    retry = delivery.retry_policy
    if policy.retry_policy_fingerprint != retry.policy_fingerprint:
        raise ValueError("extraction Request retry policy binding drifted")
    if (
        policy.input_budget_policy_fingerprint
        != EXTRACTION_INPUT_BUDGET_SELECTION_CONTRACT_FINGERPRINT
    ):
        raise ValueError("extraction Request input-budget policy binding drifted")
    if (
        policy.background_work_budget_policy_fingerprint
        != DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY.policy_fingerprint
    ):
        raise ValueError("extraction Request background-budget binding drifted")
    return delivery


def _build_delivery_policy(
    *,
    maximum_attempts: int,
    provider_timeout_seconds: int,
    lease_duration_seconds: int,
) -> DurableProjectionDeliveryPolicyFact:
    retry = cast(
        DurableProjectionRetryPolicyFact,
        build_projection_fact(
            DurableProjectionRetryPolicyFact,
            schema_version="durable_projection_retry_policy.v1",
            maximum_attempts=maximum_attempts,
            base_delay_milliseconds=_RETRY_BASE_DELAY_MILLISECONDS,
            maximum_delay_milliseconds=_RETRY_MAXIMUM_DELAY_MILLISECONDS,
            lease_duration_seconds=lease_duration_seconds,
            claim_batch_size=_CLAIM_BATCH_SIZE,
        ),
    )
    physical = cast(
        DurableProjectionPhysicalPolicyFact,
        build_projection_fact(
            DurableProjectionPhysicalPolicyFact,
            schema_version="durable_projection_physical_policy.v1",
            database_operation_timeout_seconds=10,
            source_hydration_timeout_seconds=20,
            handler_compute_timeout_seconds=provider_timeout_seconds,
            result_commit_timeout_seconds=20,
            external_surface_attempt_timeout_seconds=60,
            maximum_physical_attempt_seconds=provider_timeout_seconds,
        ),
    )
    delivery = cast(
        DurableProjectionDeliveryPolicyFact,
        build_projection_fact(
            DurableProjectionDeliveryPolicyFact,
            schema_version="durable_projection_delivery_policy.v1",
            retry_policy=retry,
            physical_policy=physical,
        ),
    )
    return delivery


def build_default_compaction_memory_extraction_policy(
    *,
    model_target: ResolvedModelTargetFact,
) -> CompactionMemoryExtractionPolicyFact:
    """Freeze the production Request policy from the same execution contracts."""

    delivery = _build_delivery_policy(
        maximum_attempts=DEFAULT_COMPACTION_MEMORY_MAXIMUM_ATTEMPTS,
        provider_timeout_seconds=(DEFAULT_COMPACTION_MEMORY_PROVIDER_TIMEOUT_SECONDS),
        lease_duration_seconds=DEFAULT_COMPACTION_MEMORY_LEASE_DURATION_SECONDS,
    )
    return build_frozen_fact(
        CompactionMemoryExtractionPolicyFact,
        schema_version="compaction_memory_extraction_policy.v1",
        enabled=True,
        allowed_triggers=("auto", "manual"),
        allowed_phases=("manual", "mid_turn", "pre_run", "window_maintenance"),
        model_target=model_target,
        maximum_attempts=DEFAULT_COMPACTION_MEMORY_MAXIMUM_ATTEMPTS,
        provider_timeout_seconds=(DEFAULT_COMPACTION_MEMORY_PROVIDER_TIMEOUT_SECONDS),
        lease_duration_seconds=DEFAULT_COMPACTION_MEMORY_LEASE_DURATION_SECONDS,
        retry_policy_fingerprint=delivery.retry_policy.policy_fingerprint,
        input_budget_policy_fingerprint=(
            EXTRACTION_INPUT_BUDGET_SELECTION_CONTRACT_FINGERPRINT
        ),
        background_work_budget_policy_fingerprint=(
            DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY.policy_fingerprint
        ),
    )


def default_compaction_memory_delivery_policy() -> DurableProjectionDeliveryPolicyFact:
    """Return the active V1 seed policy without requiring a model target."""

    return _build_delivery_policy(
        maximum_attempts=DEFAULT_COMPACTION_MEMORY_MAXIMUM_ATTEMPTS,
        provider_timeout_seconds=(DEFAULT_COMPACTION_MEMORY_PROVIDER_TIMEOUT_SECONDS),
        lease_duration_seconds=DEFAULT_COMPACTION_MEMORY_LEASE_DURATION_SECONDS,
    )


def compaction_memory_retry_delay_seconds(
    delivery_policy: DurableProjectionDeliveryPolicyFact,
    *,
    dispatch_attempt_ordinal: int,
) -> float:
    """Resolve the closed V1 1/2/4-second model retry schedule without jitter."""

    if dispatch_attempt_ordinal <= 0:
        raise ValueError("dispatch attempt ordinal must be positive")
    retry = delivery_policy.retry_policy
    delay_milliseconds = min(
        retry.maximum_delay_milliseconds,
        retry.base_delay_milliseconds * (2 ** (dispatch_attempt_ordinal - 1)),
    )
    return delay_milliseconds / 1000.0


__all__ = [
    "DEFAULT_COMPACTION_MEMORY_MAXIMUM_ATTEMPTS",
    "build_default_compaction_memory_extraction_policy",
    "compaction_memory_delivery_policy_from_request",
    "compaction_memory_retry_delay_seconds",
    "default_compaction_memory_delivery_policy",
]
