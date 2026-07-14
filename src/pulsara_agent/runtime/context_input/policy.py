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
    """Freeze per-observation safety limits and context collection policy.

    Aggregate tool-result allocation is intentionally absent.  The resolved
    model input budget and long-horizon projection planner own every cross-unit
    token decision.
    """

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
        "policy_version": "tool-result-render-policy:v2",
        "per_tool_cap_chars": budget.tool_result_per_tool_cap_chars,
        "per_message_cap_chars": budget.tool_result_per_message_cap_chars,
        "per_envelope_cap_chars": budget.tool_result_per_envelope_cap_chars,
        "minimum_essential_envelope_chars": budget.minimum_essential_envelope_chars,
        "max_artifact_refs_per_unit": 64,
        "max_data_placeholder_chars": 512,
        "envelope_render": envelope,
    }
    basis = ToolResultRenderPolicyBasisFact(
        **basis_payload,
        basis_fingerprint=context_fingerprint(
            "tool-result-render-policy-basis:v2", basis_payload
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
