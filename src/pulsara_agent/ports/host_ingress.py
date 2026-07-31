"""Process-local Host ingress carriers shared with run execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.host_ingress import (
    ActiveRunMonitorSafePointCommitGuardFact,
    HostActiveRunMonitorDeliveryFact,
    HostRuntimeNotificationAttachmentFact,
)
from pulsara_agent.primitives.terminal_observation import (
    TerminalAutonomousDeliveryFact,
)


class HostIngressAdmissionStale(RuntimeError):
    """The borrowed Host admission authority no longer matches precommit state."""


@dataclass(frozen=True, slots=True)
class ActiveRunMonitorSafePointLease:
    """Process-local owner retained until one ModelStart is confirmed FULL."""

    lease_id: str
    runtime_session_id: str
    run_id: str
    next_model_call_index: int
    source_events: tuple[Any, ...]
    attachments: tuple[HostRuntimeNotificationAttachmentFact, ...]
    selected_notification_head_fingerprints: tuple[str, ...]
    notification_state_fingerprint: str
    wake_chain_id: str
    expected_autonomy_chain_state_fingerprint: str
    proposed_automatic_delivery_ordinal: int
    chain_policy_fingerprint: str
    host_state_generation: int
    permission_policy_revision: int
    permission_policy_fingerprint: str
    close_intent_revision: int
    stop_intent_revision: int
    termination_intent_revision: int
    active_segment_id: str
    active_segment_generation: int
    llm_lifecycle_generation: int
    run_start_event_reference: Any
    previous_model_call_end_event_reference: Any
    prior_model_control_disposition_reference: Any
    pending_interaction_frontier_fingerprint: str
    open_tool_pair_frontier_fingerprint: str


def build_active_run_monitor_delivery(
    *,
    lease: ActiveRunMonitorSafePointLease,
    provider_input_start_bundle: Any,
) -> HostActiveRunMonitorDeliveryFact:
    """Bind one borrowed notification set to the exact prepared append."""

    from pulsara_agent.primitives.provider_input import (
        ExistingAppendCommitGuardFact,
        RolloverGenerationCommitGuardFact,
    )

    provider_guard = (
        provider_input_start_bundle.prepared_candidate.generation_commit_guard
    )
    if isinstance(provider_guard, ExistingAppendCommitGuardFact):
        generation_id = provider_guard.generation_id
        revision = provider_guard.expected_revision
        core_fingerprint = provider_guard.expected_committed_core_state_fingerprint
    elif isinstance(provider_guard, RolloverGenerationCommitGuardFact):
        generation_id = provider_guard.old_generation_id
        revision = provider_guard.expected_old_revision
        core_fingerprint = provider_guard.expected_old_core_state_fingerprint
    else:
        raise HostIngressAdmissionStale(
            "active-run monitor delivery requires an existing provider generation"
        )
    guard = build_frozen_fact(
        ActiveRunMonitorSafePointCommitGuardFact,
        schema_version="active_run_monitor_safe_point_commit_guard.v1",
        runtime_session_id=lease.runtime_session_id,
        run_start_event_reference=lease.run_start_event_reference,
        active_segment_id=lease.active_segment_id,
        active_segment_generation=lease.active_segment_generation,
        expected_host_state_generation=lease.host_state_generation,
        expected_next_model_call_index=lease.next_model_call_index,
        expected_llm_lifecycle_generation=lease.llm_lifecycle_generation,
        expected_termination_intent_revision=lease.termination_intent_revision,
        expected_stop_intent_revision=lease.stop_intent_revision,
        expected_close_intent_revision=lease.close_intent_revision,
        expected_permission_policy_revision=lease.permission_policy_revision,
        expected_permission_policy_fingerprint=(lease.permission_policy_fingerprint),
        prior_model_control_disposition_reference=(
            lease.prior_model_control_disposition_reference
        ),
        previous_model_call_end_event_reference=(
            lease.previous_model_call_end_event_reference
        ),
        expected_provider_input_generation_id=generation_id,
        expected_provider_input_generation_revision=revision,
        expected_provider_input_committed_state_fingerprint=core_fingerprint,
        expected_pending_interaction_frontier_fingerprint=(
            lease.pending_interaction_frontier_fingerprint
        ),
        expected_open_tool_pair_frontier_fingerprint=(
            lease.open_tool_pair_frontier_fingerprint
        ),
        expected_notification_state_fingerprint=(lease.notification_state_fingerprint),
        expected_selected_notification_head_fingerprints=(
            lease.selected_notification_head_fingerprints
        ),
        expected_autonomy_chain_state_fingerprint=(
            lease.expected_autonomy_chain_state_fingerprint
        ),
        prepared_provider_input_append_fingerprint=(
            provider_input_start_bundle.prepared_candidate.candidate_fingerprint
        ),
    )
    attachment_fingerprints = tuple(
        item.attachment_fingerprint for item in lease.attachments
    )
    autonomy = build_frozen_fact(
        TerminalAutonomousDeliveryFact,
        schema_version="terminal_autonomous_delivery.v1",
        wake_chain_id=lease.wake_chain_id,
        ordered_source_attachment_fingerprints=attachment_fingerprints,
        delivery_kind="active_run_safe_point",
        automatic_delivery_ordinal=lease.proposed_automatic_delivery_ordinal,
        chain_policy_fingerprint=lease.chain_policy_fingerprint,
    )
    return build_frozen_fact(
        HostActiveRunMonitorDeliveryFact,
        schema_version="host_active_run_monitor_delivery.v1",
        commit_guard=guard,
        ordered_attachment_fingerprints=attachment_fingerprints,
        autonomy_delivery=autonomy,
    )


__all__ = [
    "ActiveRunMonitorSafePointLease",
    "HostIngressAdmissionStale",
    "build_active_run_monitor_delivery",
]
