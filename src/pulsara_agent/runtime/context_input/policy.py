"""Resolve mutable runtime budget configuration into immutable compile policy."""

from __future__ import annotations

from pulsara_agent.primitives.context import (
    ContextAllocationPolicyFact,
    ContextCandidateCollectionPolicyFact,
    ContextCompilePolicyFact,
    ToolResultEnvelopeRenderPolicyFact,
    ToolResultRenderPolicyBasisFact,
    context_fingerprint,
)
from pulsara_agent.runtime.state import LoopBudget


def resolve_context_compile_policy(budget: LoopBudget) -> ContextCompilePolicyFact:
    """Resolve every optional LoopBudget source value exactly once.

    The derivation mirrors ``_ToolResultRenderAllocator.from_loop_budget``.
    Compiler and renderer code consume only the returned non-null facts.
    """

    total = max(0, budget.tool_result_context_chars)
    body_total = budget.tool_result_body_context_chars
    configured_envelope = max(0, budget.tool_result_envelope_context_chars)
    if body_total is None:
        envelope_total = min(configured_envelope, max(0, total // 3))
        body_total = max(0, total - envelope_total)
    else:
        body_total = max(0, min(body_total, total))
        envelope_total = min(configured_envelope, max(0, total - body_total))

    prior = budget.prior_tool_result_context_chars
    current = budget.current_tail_tool_result_context_chars
    legacy = budget.legacy_tool_result_context_chars
    if prior is None or current is None:
        derived_prior = min(12_000, body_total // 3)
        derived_current = max(0, body_total - derived_prior)
        prior = derived_prior if prior is None else prior
        current = derived_current if current is None else current
    prior = max(0, prior)
    current = max(0, current)
    legacy = max(0, body_total if legacy is None else legacy)

    latest_reserved = max(0, budget.latest_tool_result_reserved_chars)
    per_tool = budget.tool_result_per_tool_cap_chars
    if per_tool is None:
        per_tool = max(latest_reserved, min(12_000, max(current, legacy, prior)))
    per_message = budget.tool_result_per_message_cap_chars
    if per_message is None:
        per_message = max(
            latest_reserved,
            min(20_000, max(current, legacy, prior)),
        )
    per_envelope = max(256, budget.tool_result_per_envelope_cap_chars)

    envelope_payload = {
        "envelope_renderer_version": "tool-result-envelope:v1",
        "truncation_marker_version": "clipped-marker:v1",
        "artifact_envelope_version": "artifact-envelope:v1",
        "timing_header_version": "tool-timing:v1",
        "full_string_cap_chars": 240,
        "compact_string_cap_chars": 96,
        "minimal_string_cap_chars": 72,
        "ultra_minimal_string_cap_chars": 32,
        "max_process_summaries": 8,
        "compact_process_summaries": 3,
        "process_summary_string_cap_chars": 160,
    }
    envelope = ToolResultEnvelopeRenderPolicyFact(
        **envelope_payload,
        policy_fingerprint=context_fingerprint(
            "tool-result-envelope-render-policy:v1", envelope_payload
        ),
    )

    basis_payload = {
        "policy_version": "tool-result-render-policy:v1",
        "total_context_chars": total,
        "body_context_chars": body_total,
        "envelope_context_chars": envelope_total,
        "prior_history_context_chars": prior,
        "current_run_tail_context_chars": current,
        "current_user_context_chars": current,
        "legacy_history_context_chars": legacy,
        "per_tool_cap_chars": max(0, per_tool),
        "per_message_cap_chars": max(0, per_message),
        "per_envelope_cap_chars": per_envelope,
        "latest_result_reserved_chars_per_unit": latest_reserved,
        "max_tool_results_per_context": max(0, budget.max_tool_results_per_context),
        "minimum_essential_envelope_chars": max(
            1, budget.minimum_essential_envelope_chars
        ),
        "max_artifact_refs_per_unit": 64,
        "max_data_placeholder_chars": 512,
        "envelope_render": envelope,
    }
    basis = ToolResultRenderPolicyBasisFact(
        **basis_payload,
        basis_fingerprint=context_fingerprint(
            "tool-result-render-policy-basis:v1", basis_payload
        ),
    )

    candidate_payload = {
        "policy_version": "context-candidate-collection:v1",
        "projection_token_budget": max(0, budget.projection_token_budget),
        "max_subagent_results_per_parent_compile": max(
            0, budget.max_subagent_results_per_parent_compile
        ),
        "max_inline_candidate_chars": 64_000,
        "max_aggregate_candidate_chars": 256_000,
        "max_candidate_source_refs": 1_024,
        "max_candidate_artifact_refs": 512,
        "max_input_manifest_chars": 1_048_576,
    }
    candidate = ContextCandidateCollectionPolicyFact(
        **candidate_payload,
        policy_fingerprint=context_fingerprint(
            "context-candidate-collection-policy:v1", candidate_payload
        ),
    )

    allocation_payload = {
        "section_policy_version": "context-section-allocation:v1",
        "required_section_ids": ("base_system_instruction",),
        "optional_section_priority_order": (
            "runtime_context",
            "capability_catalog",
            "capability_active_skill",
            "plan",
            "memory_projection",
            "recovery",
            "subagent_results",
        ),
        "lifecycle_policy_version": "context-lifecycle:v1",
        "timing_header_policy_version": "context-timing-header:v1",
    }
    allocation = ContextAllocationPolicyFact(
        **allocation_payload,
        fingerprint=context_fingerprint(
            "context-allocation-policy:v1", allocation_payload
        ),
    )
    compile_payload = {
        "compiler_contract_version": "context-compiler:v1",
        "tool_result_basis": basis,
        "candidate_collection": candidate,
        "allocation": allocation,
    }
    return ContextCompilePolicyFact(
        **compile_payload,
        fingerprint=context_fingerprint("context-compile-policy:v1", compile_payload),
    )


__all__ = ["resolve_context_compile_policy"]
