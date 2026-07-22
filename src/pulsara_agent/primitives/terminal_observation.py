"""Durable terminal output, monitor, and notification authority contracts."""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    canonical_utc_timestamp,
)
from pulsara_agent.primitives.context_source import ContextArtifactReferenceFact
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    StableEventIdentityFact,
    build_frozen_fact,
    register_durable_fact,
)


Fingerprint = str
MAXIMUM_TERMINAL_MONITOR_DURATION_SECONDS = 10 * 60 * 60


def _fact(
    schema_version: str,
    own_fingerprint_field: str,
    domain_separator: str,
):
    def decorate(cls):
        register_durable_fact(
            schema_version=schema_version,
            own_fingerprint_field=own_fingerprint_field,
            domain_separator=domain_separator,
        )
        return cls

    return decorate


def _sha256_text(value: str) -> str:
    return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"


def _same_stream(*cursors: "TerminalOutputCursorFact") -> bool:
    return (
        len({item.stream_identity.stream_identity_fingerprint for item in cursors}) == 1
    )


def _cursor_le(
    left: "TerminalOutputCursorFact", right: "TerminalOutputCursorFact"
) -> bool:
    if not _same_stream(left, right):
        return False
    return (
        left.sanitized_char_offset <= right.sanitized_char_offset
        and left.sanitized_utf8_byte_offset <= right.sanitized_utf8_byte_offset
    )


@_fact(
    "terminal_output_sanitization_contract.v1",
    "contract_fingerprint",
    "terminal-output-sanitization-contract:v1",
)
class TerminalOutputSanitizationContractFact(FrozenFactBase):
    schema_version: Literal["terminal_output_sanitization_contract.v1"] = (
        "terminal_output_sanitization_contract.v1"
    )
    contract_id: str = Field(min_length=1)
    contract_version: int = Field(ge=1)
    utf8_error_policy: Literal["replace"] = "replace"
    ansi_normalization_contract_fingerprint: Fingerprint
    control_character_policy: Literal["preserve_newline_normalize_cr"] = (
        "preserve_newline_normalize_cr"
    )
    secret_redaction_contract_fingerprint: Fingerprint
    maximum_sanitizer_carry_utf8_bytes: int = Field(gt=0)
    oversized_sensitive_token_policy: Literal["redact_entire_token"] = (
        "redact_entire_token"
    )
    partial_line_policy_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint


@_fact(
    "terminal_output_stream_identity.v1",
    "stream_identity_fingerprint",
    "terminal-output-stream-identity:v1",
)
class TerminalOutputStreamIdentityFact(FrozenFactBase):
    schema_version: Literal["terminal_output_stream_identity.v1"] = (
        "terminal_output_stream_identity.v1"
    )
    process_id: str = Field(min_length=1)
    journal_instance_id: str = Field(min_length=1)
    stream_identity_fingerprint: Fingerprint


@_fact(
    "terminal_output_cursor.v1",
    "cursor_fingerprint",
    "terminal-output-cursor:v1",
)
class TerminalOutputCursorFact(FrozenFactBase):
    schema_version: Literal["terminal_output_cursor.v1"] = "terminal_output_cursor.v1"
    stream_identity: TerminalOutputStreamIdentityFact
    sanitized_char_offset: int = Field(ge=0)
    sanitized_utf8_byte_offset: int = Field(ge=0)
    canonical_prefix_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sanitizer_contract_fingerprint: Fingerprint
    cursor_fingerprint: Fingerprint


@_fact(
    "terminal_output_delta_semantic.v1",
    "delta_semantic_fingerprint",
    "terminal-output-delta-semantic:v1",
)
class TerminalOutputDeltaSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_output_delta_semantic.v1"] = (
        "terminal_output_delta_semantic.v1"
    )
    availability: Literal["available"] = "available"
    requested_start_cursor: TerminalOutputCursorFact
    available_start_cursor: TerminalOutputCursorFact
    end_cursor: TerminalOutputCursorFact
    output_preview: str
    delta_content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    truncated: bool
    delta_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _range(self) -> "TerminalOutputDeltaSemanticFact":
        if not _same_stream(
            self.requested_start_cursor, self.available_start_cursor, self.end_cursor
        ):
            raise ValueError("terminal output delta stream identity mismatch")
        if not _cursor_le(self.requested_start_cursor, self.available_start_cursor):
            raise ValueError("terminal output available start precedes request")
        if not _cursor_le(self.available_start_cursor, self.end_cursor):
            raise ValueError("terminal output delta range is reversed")
        expected_chars = (
            self.end_cursor.sanitized_char_offset
            - self.available_start_cursor.sanitized_char_offset
        )
        expected_bytes = (
            self.end_cursor.sanitized_utf8_byte_offset
            - self.available_start_cursor.sanitized_utf8_byte_offset
        )
        if len(self.output_preview) > expected_chars:
            raise ValueError(
                "terminal output preview exceeds available character range"
            )
        if len(self.output_preview.encode("utf-8")) > expected_bytes:
            raise ValueError("terminal output preview exceeds available byte range")
        if self.delta_content_sha256 != _sha256_text(self.output_preview):
            raise ValueError("terminal output delta preview hash mismatch")
        omitted = self.available_start_cursor != self.requested_start_cursor
        clipped = (
            len(self.output_preview) != expected_chars
            or len(self.output_preview.encode("utf-8")) != expected_bytes
        )
        if self.truncated != (omitted or clipped):
            raise ValueError("terminal output delta truncation matrix mismatch")
        return self


@_fact(
    "unavailable_recovered_terminal_output_delta.v1",
    "delta_semantic_fingerprint",
    "unavailable-recovered-terminal-output-delta:v1",
)
class UnavailableRecoveredTerminalOutputDeltaFact(FrozenFactBase):
    schema_version: Literal["unavailable_recovered_terminal_output_delta.v1"] = (
        "unavailable_recovered_terminal_output_delta.v1"
    )
    availability: Literal["unavailable_recovered"] = "unavailable_recovered"
    requested_start_cursor: TerminalOutputCursorFact
    terminal_cursor: TerminalOutputCursorFact
    recovery_reason: Literal[
        "spool_range_evicted",
        "spool_write_failed",
        "spool_writer_queue_overflow",
        "spool_fsync_timeout",
        "spool_terminal_drain_timeout",
        "artifact_gc_confirmed",
    ]
    delta_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _range(self) -> "UnavailableRecoveredTerminalOutputDeltaFact":
        if not _cursor_le(self.requested_start_cursor, self.terminal_cursor):
            raise ValueError("unavailable terminal output range is invalid")
        return self


TerminalMonitorObservationOutputFact: TypeAlias = Annotated[
    TerminalOutputDeltaSemanticFact | UnavailableRecoveredTerminalOutputDeltaFact,
    Field(discriminator="availability"),
]


@_fact(
    "terminal_output_delta_attribution.v1",
    "attribution_fingerprint",
    "terminal-output-delta-attribution:v1",
)
class TerminalOutputDeltaAttributionFact(FrozenFactBase):
    schema_version: Literal["terminal_output_delta_attribution.v1"] = (
        "terminal_output_delta_attribution.v1"
    )
    delta_semantic_fingerprint: Fingerprint
    full_output_artifact_ref: ContextArtifactReferenceFact | None
    retained_segment_first_index: int | None = Field(default=None, ge=0)
    retained_segment_last_index: int | None = Field(default=None, ge=0)
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _segment_range(self) -> "TerminalOutputDeltaAttributionFact":
        first = self.retained_segment_first_index
        last = self.retained_segment_last_index
        if (first is None) != (last is None):
            raise ValueError("terminal delta retained segment range is all-or-none")
        if first is not None and last is not None and first > last:
            raise ValueError("terminal delta retained segment range is reversed")
        return self


@_fact(
    "terminal_output_spool_policy.v1",
    "policy_fingerprint",
    "terminal-output-spool-policy:v1",
)
class TerminalOutputSpoolPolicyFact(FrozenFactBase):
    schema_version: Literal["terminal_output_spool_policy.v1"] = (
        "terminal_output_spool_policy.v1"
    )
    maximum_spool_utf8_bytes: int = Field(gt=0)
    page_utf8_bytes: int = Field(gt=0)
    maximum_pending_spool_utf8_bytes: int = Field(gt=0)
    page_fsync_timeout_ms: int = Field(gt=0)
    terminal_drain_timeout_ms: int = Field(gt=0)
    overflow_policy: Literal["evict_oldest_complete_page_with_gap"] = (
        "evict_oldest_complete_page_with_gap"
    )
    file_permission_mode: Literal["0600"] = "0600"
    page_commit_contract_fingerprint: Fingerprint
    retention_horizon_policy_fingerprint: Fingerprint
    policy_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bounds(self) -> "TerminalOutputSpoolPolicyFact":
        if self.page_utf8_bytes > self.maximum_spool_utf8_bytes:
            raise ValueError("terminal spool page exceeds total quota")
        if self.maximum_pending_spool_utf8_bytes < self.page_utf8_bytes:
            raise ValueError("terminal spool queue cannot hold one page")
        return self


@_fact(
    "terminal_output_spool_gap.v1",
    "gap_fingerprint",
    "terminal-output-spool-gap:v1",
)
class TerminalOutputSpoolGapFact(FrozenFactBase):
    schema_version: Literal["terminal_output_spool_gap.v1"] = (
        "terminal_output_spool_gap.v1"
    )
    start_cursor: TerminalOutputCursorFact
    end_cursor: TerminalOutputCursorFact
    reason: Literal[
        "quota_evicted",
        "write_enospc",
        "write_permission_denied",
        "write_io_error",
        "writer_queue_overflow",
        "fsync_timeout",
        "terminal_drain_timeout",
    ]
    gap_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _range(self) -> "TerminalOutputSpoolGapFact":
        if not _cursor_le(self.start_cursor, self.end_cursor):
            raise ValueError("terminal spool gap range is invalid")
        return self


@_fact(
    "terminal_output_spool_writer_state.v1",
    "state_fingerprint",
    "terminal-output-spool-writer-state:v1",
)
class TerminalOutputSpoolWriterStateFact(FrozenFactBase):
    schema_version: Literal["terminal_output_spool_writer_state.v1"] = (
        "terminal_output_spool_writer_state.v1"
    )
    stream_identity: TerminalOutputStreamIdentityFact
    journal_end_cursor: TerminalOutputCursorFact
    successfully_spooled_cursor: TerminalOutputCursorFact
    retained_start_cursor: TerminalOutputCursorFact
    writer_state: Literal["active", "degraded", "closed", "authority_untrusted"]
    latest_gap: TerminalOutputSpoolGapFact | None
    spool_policy_fingerprint: Fingerprint
    state_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _cursors(self) -> "TerminalOutputSpoolWriterStateFact":
        cursors = (
            self.journal_end_cursor,
            self.successfully_spooled_cursor,
            self.retained_start_cursor,
        )
        if any(item.stream_identity != self.stream_identity for item in cursors):
            raise ValueError("terminal spool state stream mismatch")
        if not _cursor_le(self.successfully_spooled_cursor, self.journal_end_cursor):
            raise ValueError("spooled cursor exceeds journal cursor")
        if not _cursor_le(self.retained_start_cursor, self.successfully_spooled_cursor):
            raise ValueError("spool retained start exceeds spooled cursor")
        if self.writer_state == "active" and self.latest_gap is not None:
            raise ValueError("active spool cannot carry a gap")
        if self.writer_state == "degraded" and self.latest_gap is None:
            raise ValueError("degraded spool requires a gap")
        return self


@_fact(
    "terminal_output_recovery_reference.v1",
    "recovery_reference_fingerprint",
    "terminal-output-recovery-reference:v1",
)
class TerminalOutputRecoveryReferenceFact(FrozenFactBase):
    schema_version: Literal["terminal_output_recovery_reference.v1"] = (
        "terminal_output_recovery_reference.v1"
    )
    spool_locator_id: str = Field(min_length=1)
    spool_writer_state: TerminalOutputSpoolWriterStateFact
    spool_manifest_reference: ContextArtifactReferenceFact | None
    spool_policy_fingerprint: Fingerprint
    recovery_reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _policy(self) -> "TerminalOutputRecoveryReferenceFact":
        if (
            self.spool_policy_fingerprint
            != self.spool_writer_state.spool_policy_fingerprint
        ):
            raise ValueError("terminal recovery spool policy mismatch")
        return self


@_fact(
    "running_terminal_process_state.v1",
    "state_fingerprint",
    "running-terminal-process-state:v1",
)
class RunningTerminalProcessStateFact(FrozenFactBase):
    schema_version: Literal["running_terminal_process_state.v1"] = (
        "running_terminal_process_state.v1"
    )
    status: Literal["running"] = "running"
    state_fingerprint: Fingerprint


@_fact(
    "terminal_process_lifecycle_outcome.v1",
    "outcome_fingerprint",
    "terminal-process-lifecycle-outcome:v1",
)
class TerminalProcessLifecycleOutcomeFact(FrozenFactBase):
    schema_version: Literal["terminal_process_lifecycle_outcome.v1"] = (
        "terminal_process_lifecycle_outcome.v1"
    )
    status: Literal["success", "error", "timeout", "killed"]
    exit_code: int
    kill_reason: Literal["user_tool_kill", "teardown", "lifetime_watchdog"] | None
    outcome_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _matrix(self) -> "TerminalProcessLifecycleOutcomeFact":
        valid = (
            self.status == "success"
            and self.exit_code == 0
            and self.kill_reason is None
            or self.status == "error"
            and self.exit_code > 0
            and self.exit_code != 124
            and self.kill_reason is None
            or self.status == "timeout"
            and self.exit_code == 124
            and self.kill_reason is None
            or self.status == "killed"
            and self.exit_code < 0
            and self.kill_reason is not None
        )
        if not valid:
            raise ValueError("terminal lifecycle outcome matrix mismatch")
        return self


TerminalProcessObservedStateFact: TypeAlias = Annotated[
    RunningTerminalProcessStateFact | TerminalProcessLifecycleOutcomeFact,
    Field(discriminator="status"),
]


@_fact(
    "inline_terminal_observation_coverage.v1",
    "coverage_fingerprint",
    "inline-terminal-observation-coverage:v1",
)
class InlineTerminalObservationCoverageFact(FrozenFactBase):
    schema_version: Literal["inline_terminal_observation_coverage.v1"] = (
        "inline_terminal_observation_coverage.v1"
    )
    coverage_kind: Literal["inline"] = "inline"
    covered_start_cursor: TerminalOutputCursorFact
    covered_end_cursor: TerminalOutputCursorFact
    visible_content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    coverage_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _range(self) -> "InlineTerminalObservationCoverageFact":
        if not _cursor_le(self.covered_start_cursor, self.covered_end_cursor):
            raise ValueError("inline terminal observation coverage is invalid")
        return self


@_fact(
    "artifact_terminal_observation_coverage.v1",
    "coverage_fingerprint",
    "artifact-terminal-observation-coverage:v1",
)
class ArtifactTerminalObservationCoverageFact(FrozenFactBase):
    schema_version: Literal["artifact_terminal_observation_coverage.v1"] = (
        "artifact_terminal_observation_coverage.v1"
    )
    coverage_kind: Literal["artifact"] = "artifact"
    covered_start_cursor: TerminalOutputCursorFact
    covered_end_cursor: TerminalOutputCursorFact
    artifact_reference: ContextArtifactReferenceFact
    covered_range_content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifact_codec_contract_fingerprint: Fingerprint
    coverage_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _range(self) -> "ArtifactTerminalObservationCoverageFact":
        if not _cursor_le(self.covered_start_cursor, self.covered_end_cursor):
            raise ValueError("artifact terminal observation coverage is invalid")
        return self


@_fact(
    "unavailable_terminal_observation_coverage.v1",
    "coverage_fingerprint",
    "unavailable-terminal-observation-coverage:v1",
)
class UnavailableTerminalObservationCoverageFact(FrozenFactBase):
    schema_version: Literal["unavailable_terminal_observation_coverage.v1"] = (
        "unavailable_terminal_observation_coverage.v1"
    )
    coverage_kind: Literal["unavailable_recovered"] = "unavailable_recovered"
    unavailable_delta_semantic_fingerprint: Fingerprint
    coverage_fingerprint: Fingerprint


@_fact(
    "bounded_preview_terminal_observation_coverage.v1",
    "coverage_fingerprint",
    "bounded-preview-terminal-observation-coverage:v1",
)
class BoundedPreviewTerminalObservationCoverageFact(FrozenFactBase):
    """A visible preview that is not a contiguous proof of the whole range."""

    schema_version: Literal["bounded_preview_terminal_observation_coverage.v1"] = (
        "bounded_preview_terminal_observation_coverage.v1"
    )
    coverage_kind: Literal["bounded_preview"] = "bounded_preview"
    output_delta_semantic_fingerprint: Fingerprint
    visible_content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    coverage_fingerprint: Fingerprint


TerminalProcessObservationCoverageFact: TypeAlias = Annotated[
    InlineTerminalObservationCoverageFact
    | ArtifactTerminalObservationCoverageFact
    | BoundedPreviewTerminalObservationCoverageFact
    | UnavailableTerminalObservationCoverageFact,
    Field(discriminator="coverage_kind"),
]


@_fact(
    "terminal_process_observation_semantic.v1",
    "observation_semantic_fingerprint",
    "terminal-process-observation-semantic:v1",
)
class TerminalProcessObservationSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_observation_semantic.v1"] = (
        "terminal_process_observation_semantic.v1"
    )
    requested_start_cursor: TerminalOutputCursorFact | None
    observed_start_cursor: TerminalOutputCursorFact
    observed_end_cursor: TerminalOutputCursorFact
    output_coverage: TerminalProcessObservationCoverageFact
    observed_state: TerminalProcessObservedStateFact
    observation_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _join(self) -> "TerminalProcessObservationSemanticFact":
        if not _cursor_le(self.observed_start_cursor, self.observed_end_cursor):
            raise ValueError("terminal process observation range is invalid")
        if self.requested_start_cursor is not None and not _cursor_le(
            self.requested_start_cursor, self.observed_start_cursor
        ):
            raise ValueError("terminal observation starts before requested cursor")
        coverage = self.output_coverage
        if isinstance(
            coverage,
            (
                InlineTerminalObservationCoverageFact,
                ArtifactTerminalObservationCoverageFact,
            ),
        ) and (
            coverage.covered_start_cursor != self.observed_start_cursor
            or coverage.covered_end_cursor != self.observed_end_cursor
        ):
            raise ValueError("terminal observation coverage range mismatch")
        return self


@_fact(
    "terminal_process_observation_receipt.v1",
    "receipt_fingerprint",
    "terminal-process-observation-receipt:v1",
)
class TerminalProcessObservationReceiptFact(FrozenFactBase):
    schema_version: Literal["terminal_process_observation_receipt.v1"] = (
        "terminal_process_observation_receipt.v1"
    )
    observation_semantic: TerminalProcessObservationSemanticFact
    action_kind: Literal["poll", "log", "wait", "kill"]
    origin_tool_call_id: str = Field(min_length=1)
    completion_event_reference: ContextEventReferenceFact | None
    receipt_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _completion(self) -> "TerminalProcessObservationReceiptFact":
        terminal = isinstance(
            self.observation_semantic.observed_state,
            TerminalProcessLifecycleOutcomeFact,
        )
        if terminal != (self.completion_event_reference is not None):
            raise ValueError(
                "terminal observation receipt completion reference mismatch"
            )
        return self


@_fact(
    "terminal_process_monitor_output_condition.v1",
    "condition_fingerprint",
    "terminal-process-monitor-output-condition:v1",
)
class TerminalProcessMonitorOutputConditionFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_output_condition.v1"] = (
        "terminal_process_monitor_output_condition.v1"
    )
    min_new_output_chars: int = Field(gt=0)
    quiet_period_ms: int = Field(ge=0, le=60_000)
    condition_fingerprint: Fingerprint


@_fact(
    "terminal_process_monitor_conditions.v1",
    "conditions_fingerprint",
    "terminal-process-monitor-conditions:v1",
)
class TerminalProcessMonitorConditionsFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_conditions.v1"] = (
        "terminal_process_monitor_conditions.v1"
    )
    output: TerminalProcessMonitorOutputConditionFact | None
    heartbeat_interval_seconds: int | None = Field(default=None, ge=5, le=1800)
    conditions_fingerprint: Fingerprint


@_fact(
    "terminal_process_monitor_delivery_policy.v1",
    "delivery_policy_fingerprint",
    "terminal-process-monitor-delivery-policy:v1",
)
class TerminalProcessMonitorDeliveryPolicyFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_delivery_policy.v1"] = (
        "terminal_process_monitor_delivery_policy.v1"
    )
    max_output_chars: int = Field(gt=0)
    minimum_progress_observation_interval_seconds: int = Field(ge=5)
    maximum_pending_progress_observations: Literal[1] = 1
    maximum_committed_progress_observations: int = Field(ge=1, le=119)
    progress_observation_rate_window_seconds: int = Field(gt=0)
    maximum_progress_observations_per_rate_window: int = Field(ge=1)
    delivery_policy_fingerprint: Fingerprint


@_fact(
    "terminal_process_monitor_progress_limiter_state.v1",
    "limiter_state_fingerprint",
    "terminal-process-monitor-progress-limiter-state:v1",
)
class TerminalProcessMonitorProgressLimiterStateFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_progress_limiter_state.v1"] = (
        "terminal_process_monitor_progress_limiter_state.v1"
    )
    retained_progress_observed_at_utc: tuple[str, ...]
    last_committed_progress_observed_at_utc: str | None
    delivery_policy_fingerprint: Fingerprint
    limiter_state_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _times(self) -> "TerminalProcessMonitorProgressLimiterStateFact":
        times = tuple(
            canonical_utc_timestamp(item)
            for item in self.retained_progress_observed_at_utc
        )
        if times != self.retained_progress_observed_at_utc or times != tuple(
            sorted(times)
        ):
            raise ValueError(
                "terminal progress limiter times are not canonical/ordered"
            )
        if bool(times) != (self.last_committed_progress_observed_at_utc is not None):
            raise ValueError("terminal progress limiter last-time matrix mismatch")
        if times and times[-1] != canonical_utc_timestamp(
            self.last_committed_progress_observed_at_utc or ""
        ):
            raise ValueError("terminal progress limiter last-time mismatch")
        return self


@_fact(
    "terminal_process_monitor_lifetime.v1",
    "lifetime_fingerprint",
    "terminal-process-monitor-lifetime:v1",
)
class TerminalProcessMonitorLifetimeFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_lifetime.v1"] = (
        "terminal_process_monitor_lifetime.v1"
    )
    kind: Literal["bounded", "process_lifetime"]
    maximum_duration_seconds: int = Field(
        gt=0,
        le=MAXIMUM_TERMINAL_MONITOR_DURATION_SECONDS,
    )
    lifetime_fingerprint: Fingerprint


@_fact(
    "terminal_process_monitor_policy.v1",
    "policy_fingerprint",
    "terminal-process-monitor-policy:v1",
)
class TerminalProcessMonitorPolicyFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_policy.v1"] = (
        "terminal_process_monitor_policy.v1"
    )
    conditions: TerminalProcessMonitorConditionsFact
    delivery: TerminalProcessMonitorDeliveryPolicyFact
    lifetime: TerminalProcessMonitorLifetimeFact
    policy_fingerprint: Fingerprint


@_fact(
    "resolved_terminal_autonomy_chain_policy.v1",
    "policy_fingerprint",
    "resolved-terminal-autonomy-chain-policy:v1",
)
class ResolvedTerminalAutonomyChainPolicyFact(FrozenFactBase):
    schema_version: Literal["resolved_terminal_autonomy_chain_policy.v1"] = (
        "resolved_terminal_autonomy_chain_policy.v1"
    )
    policy_id: str = Field(min_length=1)
    policy_version: int = Field(ge=1)
    maximum_automatic_deliveries: int = Field(ge=0, le=12)
    minimum_automatic_delivery_interval_seconds: int = Field(ge=0)
    maximum_notifications_per_autonomous_ingress: int = Field(ge=1, le=8)
    policy_fingerprint: Fingerprint


@_fact(
    "terminal_autonomous_delivery_chain_attribution.v1",
    "attribution_fingerprint",
    "terminal-autonomous-delivery-chain-attribution:v1",
)
class TerminalAutonomousDeliveryChainAttributionFact(FrozenFactBase):
    schema_version: Literal["terminal_autonomous_delivery_chain_attribution.v1"] = (
        "terminal_autonomous_delivery_chain_attribution.v1"
    )
    wake_chain_id: str = Field(min_length=1)
    root_human_run_event_reference: ContextEventReferenceFact
    parent_monitor_id: str | None
    parent_automatic_delivery_ordinal: int | None = Field(default=None, ge=1)
    resolved_policy: ResolvedTerminalAutonomyChainPolicyFact
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _parent(self) -> "TerminalAutonomousDeliveryChainAttributionFact":
        if (self.parent_monitor_id is None) != (
            self.parent_automatic_delivery_ordinal is None
        ):
            raise ValueError("terminal autonomy chain parent is all-or-none")
        return self


@_fact(
    "terminal_autonomy_permission_authority.v1",
    "authority_fingerprint",
    "terminal-autonomy-permission-authority:v1",
)
class TerminalAutonomyPermissionAuthorityFact(FrozenFactBase):
    schema_version: Literal["terminal_autonomy_permission_authority.v1"] = (
        "terminal_autonomy_permission_authority.v1"
    )
    registration_permission_snapshot_id: str = Field(min_length=1)
    registration_permission_mode: str = Field(min_length=1)
    registration_permission_policy_fingerprint: Fingerprint
    scheduling_policy_id: str = Field(min_length=1)
    scheduling_policy_version: int = Field(ge=1)
    scheduling_policy_fingerprint: Fingerprint
    caller_owner_kind: Literal["host_main_run"] = "host_main_run"
    authority_fingerprint: Fingerprint


@_fact(
    "terminal_process_monitor_registration_semantic.v1",
    "registration_semantic_fingerprint",
    "terminal-process-monitor-registration-semantic:v1",
)
class TerminalProcessMonitorRegistrationSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_registration_semantic.v1"] = (
        "terminal_process_monitor_registration_semantic.v1"
    )
    monitor_id: str = Field(min_length=1)
    initial_baseline_cursor: TerminalOutputCursorFact
    policy: TerminalProcessMonitorPolicyFact
    registration_semantic_fingerprint: Fingerprint


@_fact(
    "terminal_process_monitor_registration_attribution.v1",
    "attribution_fingerprint",
    "terminal-process-monitor-registration-attribution:v1",
)
class TerminalProcessMonitorRegistrationAttributionFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_registration_attribution.v1"] = (
        "terminal_process_monitor_registration_attribution.v1"
    )
    owner_host_session_id: str = Field(min_length=1)
    owner_conversation_id: str | None
    origin_runtime_session_id: str = Field(min_length=1)
    process_origin_runtime_session_id: str = Field(min_length=1)
    process_origin_run_entry_kind: Literal["host_main_run"] = "host_main_run"
    origin_run_event_reference: ContextEventReferenceFact
    origin_tool_call_id: str = Field(min_length=1)
    registered_at_utc: str
    expires_at_utc: str
    permission_authority: TerminalAutonomyPermissionAuthorityFact
    wake_chain: TerminalAutonomousDeliveryChainAttributionFact
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _authority(self) -> "TerminalProcessMonitorRegistrationAttributionFact":
        registered = datetime.fromisoformat(
            canonical_utc_timestamp(self.registered_at_utc).replace("Z", "+00:00")
        )
        expires = datetime.fromisoformat(
            canonical_utc_timestamp(self.expires_at_utc).replace("Z", "+00:00")
        )
        if expires <= registered:
            raise ValueError("terminal monitor expiry must follow registration")
        runtime_ids = {
            self.origin_runtime_session_id,
            self.process_origin_runtime_session_id,
            self.origin_run_event_reference.runtime_session_id,
            self.wake_chain.root_human_run_event_reference.runtime_session_id,
        }
        if len(runtime_ids) != 1:
            raise ValueError("terminal monitor registration crosses runtime ledgers")
        return self


@_fact(
    "terminal_process_monitor_core_state.v1",
    "core_state_fingerprint",
    "terminal-process-monitor-core-state:v1",
)
class TerminalProcessMonitorCoreStateFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_core_state.v1"] = (
        "terminal_process_monitor_core_state.v1"
    )
    monitor_id: str = Field(min_length=1)
    state_revision: int = Field(ge=0)
    lifecycle_state: Literal[
        "active_ready",
        "active_pending_delivery",
        "active_completion_only",
        "terminal_pending_delivery",
        "terminated",
        "reconciliation_required",
    ]
    last_observation_cursor: TerminalOutputCursorFact
    last_consumed_cursor: TerminalOutputCursorFact
    last_committed_observation_ordinal: int = Field(ge=0)
    committed_progress_observation_count: int = Field(ge=0, le=119)
    progress_limiter_state: TerminalProcessMonitorProgressLimiterStateFact
    pending_observation_semantic_fingerprint: str | None
    terminal_reason: (
        Literal[
            "process_completed",
            "monitor_expired",
            "explicit_cancel",
            "session_closed",
            "interrupted_by_host_restart",
            "explicit_process_kill",
            "process_completion_not_delivery_eligible",
            "authority_untrusted",
        ]
        | None
    )
    core_state_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _matrix(self) -> "TerminalProcessMonitorCoreStateFact":
        if not _cursor_le(self.last_consumed_cursor, self.last_observation_cursor):
            raise ValueError(
                "terminal monitor consumed cursor exceeds observation cursor"
            )
        pending = self.lifecycle_state in {
            "active_pending_delivery",
            "terminal_pending_delivery",
        }
        if pending != (self.pending_observation_semantic_fingerprint is not None):
            raise ValueError("terminal monitor pending observation matrix mismatch")
        terminal = self.lifecycle_state in {"terminal_pending_delivery", "terminated"}
        if terminal != (self.terminal_reason is not None):
            raise ValueError("terminal monitor terminal reason matrix mismatch")
        if (
            self.committed_progress_observation_count
            > self.last_committed_observation_ordinal
        ):
            raise ValueError(
                "terminal monitor progress count exceeds observation ordinal"
            )
        return self


@_fact(
    "terminal_process_monitor_state_transition.v1",
    "transition_fingerprint",
    "terminal-process-monitor-state-transition:v1",
)
class TerminalProcessMonitorStateTransitionFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_state_transition.v1"] = (
        "terminal_process_monitor_state_transition.v1"
    )
    source_revision: int = Field(ge=0)
    result_revision: int = Field(ge=0)
    before_core_state_fingerprint: Fingerprint
    after_core_state_fingerprint: Fingerprint
    observation_ordinal: int | None = Field(default=None, ge=1)
    transition_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _revision(self) -> "TerminalProcessMonitorStateTransitionFact":
        if self.result_revision != self.source_revision + 1:
            raise ValueError("terminal monitor transition must advance one revision")
        return self


@_fact(
    "terminal_process_completion_semantic.v1",
    "completion_semantic_fingerprint",
    "terminal-process-completion-semantic:v1",
)
class TerminalProcessCompletionSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_completion_semantic.v1"] = (
        "terminal_process_completion_semantic.v1"
    )
    terminal_output_cursor: TerminalOutputCursorFact
    outcome: TerminalProcessLifecycleOutcomeFact
    completion_semantic_fingerprint: Fingerprint


@_fact(
    "terminal_process_monitor_progress_observation_semantic.v1",
    "observation_semantic_fingerprint",
    "terminal-process-monitor-progress-observation-semantic:v1",
)
class TerminalProcessMonitorProgressObservationSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "terminal_process_monitor_progress_observation_semantic.v1"
    ] = "terminal_process_monitor_progress_observation_semantic.v1"
    monitor_id: str = Field(min_length=1)
    observation_kind: Literal["heartbeat", "output_progress"]
    observation_ordinal: int = Field(ge=1)
    process_state: RunningTerminalProcessStateFact
    output_authority: TerminalOutputDeltaSemanticFact
    observation_semantic_fingerprint: Fingerprint


@_fact(
    "terminal_process_monitor_completion_observation_semantic.v1",
    "observation_semantic_fingerprint",
    "terminal-process-monitor-completion-observation-semantic:v1",
)
class TerminalProcessMonitorCompletionObservationSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "terminal_process_monitor_completion_observation_semantic.v1"
    ] = "terminal_process_monitor_completion_observation_semantic.v1"
    monitor_id: str = Field(min_length=1)
    observation_kind: Literal["process_completed"] = "process_completed"
    observation_ordinal: int = Field(ge=1)
    completion_semantic: TerminalProcessCompletionSemanticFact
    output_authority: TerminalMonitorObservationOutputFact
    observation_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _stream(self) -> "TerminalProcessMonitorCompletionObservationSemanticFact":
        output_cursor = (
            self.output_authority.end_cursor
            if isinstance(self.output_authority, TerminalOutputDeltaSemanticFact)
            else self.output_authority.terminal_cursor
        )
        if output_cursor != self.completion_semantic.terminal_output_cursor:
            raise ValueError("terminal completion observation output cursor mismatch")
        return self


@_fact(
    "terminal_process_monitor_expiry_observation_semantic.v1",
    "observation_semantic_fingerprint",
    "terminal-process-monitor-expiry-observation-semantic:v1",
)
class TerminalProcessMonitorExpiryObservationSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "terminal_process_monitor_expiry_observation_semantic.v1"
    ] = "terminal_process_monitor_expiry_observation_semantic.v1"
    monitor_id: str = Field(min_length=1)
    observation_kind: Literal["monitor_expired"] = "monitor_expired"
    observation_ordinal: int = Field(ge=1)
    process_state: RunningTerminalProcessStateFact
    output_authority: TerminalOutputDeltaSemanticFact
    observation_semantic_fingerprint: Fingerprint


TerminalProcessMonitorObservationSemanticFact: TypeAlias = Annotated[
    TerminalProcessMonitorProgressObservationSemanticFact
    | TerminalProcessMonitorCompletionObservationSemanticFact
    | TerminalProcessMonitorExpiryObservationSemanticFact,
    Field(discriminator="observation_kind"),
]


@_fact(
    "terminal_process_monitor_termination_semantic.v1",
    "termination_semantic_fingerprint",
    "terminal-process-monitor-termination-semantic:v1",
)
class TerminalProcessMonitorTerminationSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_termination_semantic.v1"] = (
        "terminal_process_monitor_termination_semantic.v1"
    )
    monitor_id: str = Field(min_length=1)
    terminal_reason: Literal[
        "explicit_cancel",
        "session_closed",
        "interrupted_by_host_restart",
        "explicit_process_kill",
        "process_completion_not_delivery_eligible",
        "authority_untrusted",
    ]
    terminal_cursor: TerminalOutputCursorFact
    last_committed_observation_ordinal: int = Field(ge=0)
    termination_semantic_fingerprint: Fingerprint


@_fact(
    "terminal_process_monitor_cancel_intent.v1",
    "intent_fingerprint",
    "terminal-process-monitor-cancel-intent:v1",
)
class TerminalProcessMonitorCancelIntentFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_cancel_intent.v1"] = (
        "terminal_process_monitor_cancel_intent.v1"
    )
    monitor_id: str = Field(min_length=1)
    origin_cancel_tool_call_id: str = Field(min_length=1)
    monitor_termination_event_id: str = Field(min_length=1)
    tool_result_end_event_id: str = Field(min_length=1)
    intent_fingerprint: Fingerprint


@_fact(
    "terminal_process_monitor_cancellation_semantic.v1",
    "cancellation_semantic_fingerprint",
    "terminal-process-monitor-cancellation-semantic:v1",
)
class TerminalProcessMonitorCancellationSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_cancellation_semantic.v1"] = (
        "terminal_process_monitor_cancellation_semantic.v1"
    )
    cancel_intent: TerminalProcessMonitorCancelIntentFact
    expected_monitor_state_revision: int = Field(ge=0)
    expected_monitor_core_state_fingerprint: Fingerprint
    cancellation_semantic_fingerprint: Fingerprint


@_fact(
    "terminal_autonomy_chain_state.v1",
    "state_fingerprint",
    "terminal-autonomy-chain-state:v1",
)
class TerminalAutonomyChainStateFact(FrozenFactBase):
    schema_version: Literal["terminal_autonomy_chain_state.v1"] = (
        "terminal_autonomy_chain_state.v1"
    )
    wake_chain_id: str = Field(min_length=1)
    state_revision: int = Field(ge=0)
    last_automatic_delivery_ordinal: int = Field(ge=0)
    last_automatic_delivery_at_utc: str | None
    chain_policy_fingerprint: Fingerprint
    state_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _time(self) -> "TerminalAutonomyChainStateFact":
        if self.last_automatic_delivery_at_utc is not None:
            canonical_utc_timestamp(self.last_automatic_delivery_at_utc)
        if (self.last_automatic_delivery_ordinal == 0) != (
            self.last_automatic_delivery_at_utc is None
        ):
            raise ValueError("terminal autonomy chain ordinal/time matrix mismatch")
        return self


@_fact(
    "terminal_autonomous_delivery.v1",
    "delivery_fingerprint",
    "terminal-autonomous-delivery:v1",
)
class TerminalAutonomousDeliveryFact(FrozenFactBase):
    schema_version: Literal["terminal_autonomous_delivery.v1"] = (
        "terminal_autonomous_delivery.v1"
    )
    wake_chain_id: str = Field(min_length=1)
    ordered_source_attachment_fingerprints: tuple[Fingerprint, ...]
    delivery_kind: Literal["active_run_safe_point", "autonomous_run_start"]
    automatic_delivery_ordinal: int = Field(ge=1)
    chain_policy_fingerprint: Fingerprint
    delivery_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _attachments(self) -> "TerminalAutonomousDeliveryFact":
        items = self.ordered_source_attachment_fingerprints
        if not items or len(items) != len(set(items)) or len(items) > 8:
            raise ValueError("terminal automatic delivery attachments are invalid")
        return self


@_fact(
    "terminal_notification_reservation.v1",
    "reservation_fingerprint",
    "terminal-notification-reservation:v1",
)
class TerminalNotificationReservationFact(FrozenFactBase):
    schema_version: Literal["terminal_notification_reservation.v1"] = (
        "terminal_notification_reservation.v1"
    )
    reservation_id: str = Field(min_length=1)
    reservation_kind: Literal["completion_process_head", "monitor_lifecycle"]
    stream_identity: TerminalOutputStreamIdentityFact
    monitor_id: str | None
    created_by_event_id: str = Field(min_length=1)
    reservation_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _monitor(self) -> "TerminalNotificationReservationFact":
        if (self.reservation_kind == "monitor_lifecycle") != (
            self.monitor_id is not None
        ):
            raise ValueError(
                "terminal notification reservation monitor matrix mismatch"
            )
        return self


@_fact(
    "terminal_notification_reservation_account_state.v1",
    "state_fingerprint",
    "terminal-notification-reservation-account-state:v1",
)
class TerminalNotificationReservationAccountStateFact(FrozenFactBase):
    schema_version: Literal["terminal_notification_reservation_account_state.v1"] = (
        "terminal_notification_reservation_account_state.v1"
    )
    ledger_runtime_session_id: str = Field(min_length=1)
    account_revision: int = Field(ge=0)
    maximum_completion_process_heads: int = Field(gt=0)
    maximum_active_monitor_slots: int = Field(gt=0)
    active_completion_reservations: tuple[TerminalNotificationReservationFact, ...]
    active_monitor_reservations: tuple[TerminalNotificationReservationFact, ...]
    latest_transition_event_id: str | None
    state_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _reservations(self) -> "TerminalNotificationReservationAccountStateFact":
        for items, kind, maximum in (
            (
                self.active_completion_reservations,
                "completion_process_head",
                self.maximum_completion_process_heads,
            ),
            (
                self.active_monitor_reservations,
                "monitor_lifecycle",
                self.maximum_active_monitor_slots,
            ),
        ):
            ids = tuple(item.reservation_id for item in items)
            if ids != tuple(sorted(set(ids))) or len(items) > maximum:
                raise ValueError("terminal notification reservations are invalid")
            if any(item.reservation_kind != kind for item in items):
                raise ValueError("terminal notification reservation kind mismatch")
        return self


@_fact(
    "terminal_notification_account_transition.v1",
    "transition_fingerprint",
    "terminal-notification-account-transition:v1",
)
class TerminalNotificationAccountTransitionFact(FrozenFactBase):
    schema_version: Literal["terminal_notification_account_transition.v1"] = (
        "terminal_notification_account_transition.v1"
    )
    source_revision: int = Field(ge=0)
    result_revision: int = Field(ge=0)
    before_state_fingerprint: Fingerprint
    after_state_fingerprint: Fingerprint
    reservation: TerminalNotificationReservationFact
    cause_event_identities: tuple[StableEventIdentityFact, ...]
    transition_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _revision(self) -> "TerminalNotificationAccountTransitionFact":
        if self.result_revision != self.source_revision + 1:
            raise ValueError("terminal notification account revision must advance once")
        identities = tuple(
            item.identity_fingerprint for item in self.cause_event_identities
        )
        if not identities or identities != tuple(sorted(set(identities))):
            raise ValueError("terminal notification account causes are invalid")
        return self


@_fact(
    "terminal_monitor_notification_head.v1",
    "head_fingerprint",
    "terminal-monitor-notification-head:v1",
)
class TerminalMonitorNotificationHeadFact(FrozenFactBase):
    schema_version: Literal["terminal_monitor_notification_head.v1"] = (
        "terminal_monitor_notification_head.v1"
    )
    monitor_id: str = Field(min_length=1)
    registration_event_identity: StableEventIdentityFact
    monitor_core_state_fingerprint: Fingerprint
    last_committed_observation_ordinal: int = Field(ge=0)
    last_observation_cursor_fingerprint: Fingerprint
    last_consumed_cursor_fingerprint: Fingerprint
    pending_observation_event_reference: ContextEventReferenceFact | None
    latest_delivery_event_reference: ContextEventReferenceFact | None
    head_fingerprint: Fingerprint


@_fact(
    "terminal_notification_process_head.v1",
    "head_fingerprint",
    "terminal-notification-process-head:v1",
)
class TerminalNotificationProcessHeadFact(FrozenFactBase):
    schema_version: Literal["terminal_notification_process_head.v1"] = (
        "terminal_notification_process_head.v1"
    )
    stream_identity: TerminalOutputStreamIdentityFact
    latest_completion_event_reference: ContextEventReferenceFact | None
    monitor_heads: tuple[TerminalMonitorNotificationHeadFact, ...]
    latest_dominant_receipt_reference: ContextEventReferenceFact | None
    pending_completion_without_monitor_reference: ContextEventReferenceFact | None
    head_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _monitors(self) -> "TerminalNotificationProcessHeadFact":
        ids = tuple(item.monitor_id for item in self.monitor_heads)
        if ids != tuple(sorted(set(ids))) or len(ids) > 1:
            raise ValueError("terminal process monitor heads are invalid")
        return self


@_fact(
    "host_ingress_notification_projection_state.v1",
    "state_fingerprint",
    "host-ingress-notification-projection-state:v1",
)
class HostIngressNotificationProjectionStateFact(FrozenFactBase):
    schema_version: Literal["host_ingress_notification_projection_state.v1"] = (
        "host_ingress_notification_projection_state.v1"
    )
    ledger_runtime_session_id: str = Field(min_length=1)
    source_through_sequence: int = Field(ge=0)
    process_heads: tuple[TerminalNotificationProcessHeadFact, ...]
    reservation_account_revision: int = Field(ge=0)
    reservation_account_state_fingerprint: Fingerprint
    reducer_contract_fingerprint: Fingerprint
    state_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _heads(self) -> "HostIngressNotificationProjectionStateFact":
        ids = tuple(
            item.stream_identity.stream_identity_fingerprint
            for item in self.process_heads
        )
        if ids != tuple(sorted(set(ids))):
            raise ValueError(
                "terminal notification process heads are not ordered/unique"
            )
        return self


def build_terminal_lifecycle_outcome(
    *,
    status: Literal["success", "error", "timeout", "killed"],
    exit_code: int,
    kill_reason: Literal["user_tool_kill", "teardown", "lifetime_watchdog"] | None,
) -> TerminalProcessLifecycleOutcomeFact:
    return build_frozen_fact(
        TerminalProcessLifecycleOutcomeFact,
        schema_version="terminal_process_lifecycle_outcome.v1",
        status=status,
        exit_code=exit_code,
        kill_reason=kill_reason,
    )


def build_running_terminal_process_state() -> RunningTerminalProcessStateFact:
    return build_frozen_fact(
        RunningTerminalProcessStateFact,
        schema_version="running_terminal_process_state.v1",
        status="running",
    )


def terminal_receipt_dominates_observation(
    *,
    receipt: TerminalProcessObservationReceiptFact,
    pending: TerminalProcessMonitorObservationSemanticFact,
) -> bool:
    """Return whether an explicit tool observation fully consumed a notification."""

    semantic = receipt.observation_semantic
    coverage = semantic.output_coverage
    if not isinstance(
        coverage,
        (
            InlineTerminalObservationCoverageFact,
            ArtifactTerminalObservationCoverageFact,
        ),
    ):
        return False
    pending_output = pending.output_authority
    pending_start = (
        pending_output.available_start_cursor
        if isinstance(pending_output, TerminalOutputDeltaSemanticFact)
        else pending_output.requested_start_cursor
    )
    pending_end = (
        pending_output.end_cursor
        if isinstance(pending_output, TerminalOutputDeltaSemanticFact)
        else pending_output.terminal_cursor
    )
    if (
        coverage.covered_start_cursor.stream_identity != pending_end.stream_identity
        or coverage.covered_start_cursor.sanitized_char_offset
        > pending_start.sanitized_char_offset
        or coverage.covered_start_cursor.sanitized_utf8_byte_offset
        > pending_start.sanitized_utf8_byte_offset
        or coverage.covered_end_cursor.sanitized_char_offset
        < pending_end.sanitized_char_offset
        or coverage.covered_end_cursor.sanitized_utf8_byte_offset
        < pending_end.sanitized_utf8_byte_offset
    ):
        return False
    if isinstance(pending, TerminalProcessMonitorCompletionObservationSemanticFact):
        return (
            isinstance(semantic.observed_state, TerminalProcessLifecycleOutcomeFact)
            and semantic.observed_state == pending.completion_semantic.outcome
            and receipt.completion_event_reference is not None
        )
    return isinstance(semantic.observed_state, RunningTerminalProcessStateFact)


def normalized_progress_candidate_time(
    *, sampled_at_utc: str, previous: TerminalProcessMonitorProgressLimiterStateFact
) -> str:
    sampled = canonical_utc_timestamp(sampled_at_utc)
    if previous.last_committed_progress_observed_at_utc is None:
        return sampled
    return max(sampled, previous.last_committed_progress_observed_at_utc)


def advance_progress_limiter(
    *,
    previous: TerminalProcessMonitorProgressLimiterStateFact,
    policy: TerminalProcessMonitorDeliveryPolicyFact,
    observed_at_utc: str,
) -> TerminalProcessMonitorProgressLimiterStateFact | None:
    if previous.delivery_policy_fingerprint != policy.delivery_policy_fingerprint:
        raise ValueError("terminal progress limiter policy mismatch")
    normalized = normalized_progress_candidate_time(
        sampled_at_utc=observed_at_utc, previous=previous
    )
    current = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    window = policy.progress_observation_rate_window_seconds
    retained = tuple(
        item
        for item in previous.retained_progress_observed_at_utc
        if (
            current - datetime.fromisoformat(item.replace("Z", "+00:00"))
        ).total_seconds()
        < window
    )
    if len(retained) >= policy.maximum_progress_observations_per_rate_window:
        return None
    if previous.last_committed_progress_observed_at_utc is not None:
        last = datetime.fromisoformat(
            previous.last_committed_progress_observed_at_utc.replace("Z", "+00:00")
        )
        if (
            current - last
        ).total_seconds() < policy.minimum_progress_observation_interval_seconds:
            return None
    return build_frozen_fact(
        TerminalProcessMonitorProgressLimiterStateFact,
        schema_version="terminal_process_monitor_progress_limiter_state.v1",
        retained_progress_observed_at_utc=(*retained, normalized),
        last_committed_progress_observed_at_utc=normalized,
        delivery_policy_fingerprint=policy.delivery_policy_fingerprint,
    )


def progress_limiter_decision(
    *,
    previous: TerminalProcessMonitorProgressLimiterStateFact,
    policy: TerminalProcessMonitorDeliveryPolicyFact,
    observed_at_utc: str,
) -> tuple[TerminalProcessMonitorProgressLimiterStateFact | None, str | None]:
    """Return the sole sliding-window eligibility decision and next retry time."""

    advanced = advance_progress_limiter(
        previous=previous,
        policy=policy,
        observed_at_utc=observed_at_utc,
    )
    if advanced is not None:
        return advanced, None
    normalized = normalized_progress_candidate_time(
        sampled_at_utc=observed_at_utc,
        previous=previous,
    )
    current = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    window = policy.progress_observation_rate_window_seconds
    retained = tuple(
        datetime.fromisoformat(item.replace("Z", "+00:00"))
        for item in previous.retained_progress_observed_at_utc
        if (
            current - datetime.fromisoformat(item.replace("Z", "+00:00"))
        ).total_seconds()
        < window
    )
    next_times: list[datetime] = []
    if previous.last_committed_progress_observed_at_utc is not None:
        last = datetime.fromisoformat(
            previous.last_committed_progress_observed_at_utc.replace("Z", "+00:00")
        )
        interval_end = last + timedelta(
            seconds=policy.minimum_progress_observation_interval_seconds
        )
        if interval_end > current:
            next_times.append(interval_end)
    if len(retained) >= policy.maximum_progress_observations_per_rate_window:
        next_times.append(retained[0] + timedelta(seconds=window))
    if not next_times:
        raise AssertionError("ineligible progress limiter decision lacks retry time")
    return None, canonical_utc_timestamp(max(next_times).isoformat())


__all__ = [
    name
    for name in globals()
    if name.startswith("Terminal") or name.startswith("HostIngress")
]
__all__.extend(
    [
        "ArtifactTerminalObservationCoverageFact",
        "InlineTerminalObservationCoverageFact",
        "MAXIMUM_TERMINAL_MONITOR_DURATION_SECONDS",
        "ResolvedTerminalAutonomyChainPolicyFact",
        "RunningTerminalProcessStateFact",
        "UnavailableRecoveredTerminalOutputDeltaFact",
        "UnavailableTerminalObservationCoverageFact",
        "advance_progress_limiter",
        "build_running_terminal_process_state",
        "build_terminal_lifecycle_outcome",
        "normalized_progress_candidate_time",
        "progress_limiter_decision",
    ]
)
