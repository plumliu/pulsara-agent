"""Finite activation limits for the Stage 2 production kernel."""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True, slots=True)
class Stage2RuntimeLimits:
    committed_observation_default_events: int = 128
    committed_observation_hard_events: int = 256
    committed_observation_default_bytes: int = 2 << 20
    committed_observation_hard_bytes: int = 4 << 20
    committed_observation_default_wait_ms: int = 1_000
    committed_observation_hard_wait_ms: int = 30_000
    committed_payload_hard_bytes: int = 64 << 10
    audit_page_default_concurrency: int = 2
    audit_page_hard_concurrency: int = 8
    audit_query_default_concurrency: int = 2
    audit_query_hard_concurrency: int = 8
    snapshot_default_entries: int = 200
    snapshot_hard_entries: int = 256
    snapshot_default_control_items: int = 64
    snapshot_hard_control_items: int = 128
    snapshot_hard_bytes: int = 7 << 20
    history_page_default_entries: int = 128
    history_page_hard_entries: int = 256
    history_page_hard_bytes: int = 7 << 20
    pending_prompt_hard_items: int = 128
    nonterminal_subagent_hard_items: int = 4
    subagent_objective_hard_bytes: int = 64 << 10
    nonterminal_job_hard_items: int = 128
    active_tool_control_hard_items: int = 128
    live_observer_default_count: int = 16
    live_observer_hard_count: int = 64
    live_ring_hard_events: int = 512
    live_ring_hard_bytes: int = 1 << 20
    live_snapshot_default_events: int = 128
    live_snapshot_hard_events: int = 256
    live_snapshot_default_bytes: int = 512 << 10
    live_snapshot_hard_bytes: int = 1 << 20
    live_control_hard_events: int = 64
    live_control_hard_bytes: int = 256 << 10
    level_read_debounce_ms: int = 50
    diagnostic_sample_every: int = 1_000
    tool_argument_display_hard_bytes: int = 32 << 10
    hook_callback_timeout_ms: int = 2_000
    host_close_hard_ms: int = 5_000
    content_chunk_hard_bytes: int = 1 << 20
    content_hydrate_default_concurrency: int = 4
    content_hydrate_hard_concurrency: int = 8
    content_hydrate_timeout_ms: int = 12_000
    foreground_io_hard_concurrency: int = 8
    foreground_io_timeout_ms: int = 30_000
    memory_governance_sla_ms: int = 30_000
    memory_governance_batch_hard_items: int = 32
    job_claim_lease_ms: int = 60_000
    job_worker_default_concurrency: int = 4
    job_worker_hard_concurrency: int = 8
    memory_index_lag_warning_generations: int = 2
    memory_index_lag_error_generations: int = 10
    model_calls_per_turn_hard: int = 24
    provider_input_tokens_per_call_hard: int = 128_000
    provider_output_tokens_per_call_hard: int = 16_384
    prompt_hard_bytes: int = 1 << 20
    tool_result_hard_bytes: int = 4 << 20
    canonical_blob_hard_bytes: int = 16 << 20
    inline_content_hard_bytes: int = 64 << 10
    blob_orphan_grace_seconds: int = 24 * 60 * 60
    blob_gc_batch_hard_items: int = 128
    blob_gc_interval_ms: int = 5 * 60 * 1_000

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(
                    f"Stage 2 limit {field.name} must be finite and positive"
                )
        pairs = (
            (
                "committed_observation_default_events",
                "committed_observation_hard_events",
            ),
            ("committed_observation_default_bytes", "committed_observation_hard_bytes"),
            (
                "committed_observation_default_wait_ms",
                "committed_observation_hard_wait_ms",
            ),
            ("audit_page_default_concurrency", "audit_page_hard_concurrency"),
            ("audit_query_default_concurrency", "audit_query_hard_concurrency"),
            ("snapshot_default_entries", "snapshot_hard_entries"),
            ("snapshot_default_control_items", "snapshot_hard_control_items"),
            ("history_page_default_entries", "history_page_hard_entries"),
            ("live_observer_default_count", "live_observer_hard_count"),
            ("live_snapshot_default_events", "live_snapshot_hard_events"),
            ("live_snapshot_default_bytes", "live_snapshot_hard_bytes"),
            ("content_hydrate_default_concurrency", "content_hydrate_hard_concurrency"),
            ("job_worker_default_concurrency", "job_worker_hard_concurrency"),
        )
        for default_name, hard_name in pairs:
            if getattr(self, default_name) > getattr(self, hard_name):
                raise ValueError(f"Stage 2 {default_name} exceeds {hard_name}")


@dataclass(frozen=True, slots=True)
class Stage2StructuralBudgets:
    text_turn_canonical_transactions: int = 2
    text_turn_committed_events: int = 3
    one_tool_canonical_transactions: int = 5
    one_tool_committed_events: int = 7
    one_tool_remote_identity_transactions: int = 6
    one_tool_remote_identity_committed_events: int = 8
    text_turn_owner_families_hard: int = 3
    one_tool_owner_families_hard: int = 5
    host_close_logical_bands: int = 3
    host_close_awaits_hard: int = 12
    host_close_reducer_barriers: int = 0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"Stage 2 structural budget {field.name} is invalid")
        if any(
            getattr(self, field.name) == 0
            for field in fields(self)
            if field.name != "host_close_reducer_barriers"
        ):
            raise ValueError("only the reducer-barrier budget may be zero")


STAGE2_LIMITS = Stage2RuntimeLimits()
STAGE2_STRUCTURAL_BUDGETS = Stage2StructuralBudgets()


__all__ = [
    "STAGE2_LIMITS",
    "STAGE2_STRUCTURAL_BUDGETS",
    "Stage2RuntimeLimits",
    "Stage2StructuralBudgets",
]
