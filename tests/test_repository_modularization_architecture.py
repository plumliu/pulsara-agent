"""Mechanical-equivalence gates for ConversationKernelRepository."""

from __future__ import annotations

import importlib.util
import ast
import inspect
import json
from pathlib import Path
import pickle
import typing

from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS


ROOT = Path(__file__).resolve().parents[1]
# Fork hard cut: these existing SQL consumers now explicitly route executed
# owners; public signatures remain unchanged. Behavioral rejection is covered
# by test_conversation_fork, rather than old implementation AST equality.
_FORK_CHANGED_METHODS = {
    "accept_explicit_subagent_result", "accept_inter_agent_mailbox_batch", "accept_subagent_task_batch",
    "confirm_inter_agent_mailbox_batch", "confirm_subagent_task_batch", "confirm_subagent_turn_admission", "confirm_explicit_subagent_result",
    "list_subagent_tasks", "query_subagent_task", "_accepted_entry", "_initial_context_binding_revision_matches",
    "_insert_entry", "_insert_initial_context_binding_revision", "_require_provider_safe_turn_in_transaction",
    "_resolve_event_turn_id", "_read_entry_public_body", "_read_memory_governance_terminal_suffix",
    "claim_memory_candidate_for_governance", "_require_compaction_target", "confirm_assistant_message_winner",
    "confirm_root_turn_intent", "confirm_terminal_observation_winner", "query_command", "accept_tool_attempt",
    "accept_tool_capability_decision", "accept_tool_interaction_decision", "accept_tool_result",
    "confirm_tool_result_winner", "confirm_prepared_prompt_head_consumption", "confirm_prepared_prompt_steer",
    "_accepted_completion_row", "accept_subagent_completion_into_root", "_accept_rejected_plan_tool_batch_in_transaction",
    "_confirm_plan_resolution_in_transaction", "_confirm_plan_tool_batch_in_transaction", "_eligible_plan_handoff",
    "accept_plan_tool_batch", "inspect_plan_continuation", "resolve_plan_question",
}
TOOL = ROOT / "tools/repository_modularization_inventory.py"
BASELINE = ROOT / "tests/fixtures/repository_modularization_baseline.json"
_INTERNAL_REPOSITORY_PACKAGE = "pulsara_agent.conversation_kernel._repository"
_FRONTEND_HARD_CUT_RETIRED_PYTEST_NODES = {
    (
        "tests/test_stage2_architecture.py::"
        "test_stage2_ordinary_host_and_terminal_binary_select_only_kernel_v3"
    ),
    (
        "tests/test_stage2_tui_cross_language.py::"
        "test_stage2_python_gateway_to_go_tui_fresh_snapshot_and_detach"
    ),
}
_ROUND5B_DURABLE_JOB_SUBTRACTION_RETIRED_PYTEST_NODES = {
    "tests/test_round5_long_horizon_execution_envelope.py::test_round5_job_transport_is_bounded_without_changing_foreground",
    "tests/test_stage2_architecture.py::test_stage2_registry_schema_and_job_catalog_are_exact",
    "tests/test_stage2_canonical_reader.py::test_job_result_acceptance_is_explicit_idempotent_and_safe_point_bound",
    "tests/test_stage2_conversation_kernel_postgres.py::test_stage2_expired_job_reaper_rebinds_normal_claim_append_guard",
    "tests/test_stage2_conversation_kernel_postgres.py::test_stage2_job_attempt_retry_and_terminal_event_are_finite",
    "tests/test_stage2_conversation_kernel_postgres.py::test_stage2_job_cancel_is_set_once_and_exact_claim_owner_terminalizes_it",
    "tests/test_stage2_conversation_kernel_postgres.py::test_stage2_job_claim_ack_unknown_and_host_takeover_keep_one_attempt_owner",
    "tests/test_stage2_conversation_kernel_postgres.py::test_stage2_job_claim_and_host_cancel_share_session_first_lock_order",
    "tests/test_stage2_conversation_kernel_postgres.py::test_stage2_memory_refresh_exhaustion_is_stable_and_query_is_unavailable",
    "tests/test_stage2_conversation_kernel_postgres.py::test_stage2_provider_request_bound_terminalizes_without_retry",
    "tests/test_stage2_conversation_kernel_postgres.py::test_stage2_tool_message_precedes_attempt_and_job_claim_mints_second_guard",
    "tests/test_stage2_job_executor.py::test_job_cancellation_settles_only_after_physical_thread_exits",
    "tests/test_stage2_job_executor.py::test_job_model_prepares_the_final_context_with_target_token_estimator",
    "tests/test_stage2_job_executor.py::test_job_provider_admission_is_installed_before_the_only_physical_call",
    "tests/test_stage2_job_executor.py::test_stage2_job_executor_close_joins_active_handler_and_settles_attempt",
    "tests/test_stage2_protocol_v3.py::test_stage2_host_exposes_job_result_acceptance_to_production_protocol",
}
_ASYNC_SUBAGENT_COMPLETION_RETIRED_PYTEST_NODES = {
    (
        "tests/test_stage2_canonical_reader.py::"
        "test_subagent_result_acceptance_linearizes_at_provider_safe_point"
    ),
    (
        "tests/test_stage2_protocol_v3.py::"
        "test_stage2_controller_can_accept_exact_durable_subagent_result"
    ),
    (
        "tests/test_stage2_protocol_v3.py::"
        "test_stage2_controller_can_accept_external_results_into_a_new_root"
    ),
}
_SUBAGENT_WAIT_REFINEMENT_RETIRED_PYTEST_NODES = {
    (
        "tests/test_stage2_conversation_runner.py::"
        "test_round3_1_expired_steer_planning_consumes_nothing_and_io_closes"
    ),
}
_MEMORY_TAXONOMY_HARD_CUT_RETIRED_PYTEST_NODES = {
    (
        "tests/test_host_identity.py::"
        "test_host_workspace_transient_resolution_uses_user_scope_only"
    ),
}
_PERMISSION_HOST_SCOPE_HARD_CUT_RETIRED_PYTEST_NODES = {
    (
        "tests/test_round2_terminal_output.py::"
        "test_round2_cwd_fallback_outside_rejection_and_probe_cleanup"
    ),
    (
        "tests/test_round3_structured_model_input_compiler.py::"
        "test_round3_runtime_path_is_fixed_escaped_and_cannot_leave_workspace"
    ),
}
_MODEL_UNIVERSE_HARD_CUT_RETIRED_PYTEST_NODES = {
    "tests/test_llm_retry.py::test_llm_config_reads_retry_and_sdk_retry_env",
    "tests/test_llm_retry.py::test_retry_config_from_env_and_validation",
    "tests/test_settings.py::test_env_file_does_not_override_existing_environment_by_default",
    "tests/test_settings.py::test_settings_can_load_env_file",
    "tests/test_settings.py::test_settings_chat_completions_defaults_to_thinking_profile",
    "tests/test_settings.py::test_settings_default_llm_api_is_openai_responses",
    "tests/test_settings.py::test_settings_loads_custom_provider_profile",
    "tests/test_settings.py::test_settings_redacted_llm_endpoint_never_exposes_userinfo_path_or_query",
    "tests/test_settings.py::test_storage_config_rejects_empty_postgres_dsn",
}
_MODEL_UNIVERSE_ADDED_OBSERVED_IMPORTS = {
    "PreparedRootTurnIntent",
    "build_prepared_root_turn_intent",
}
_MODEL_UNIVERSE_REMOVED_OBSERVED_IMPORTS = {
    "PreparedRootTurnAdmission",
    "build_prepared_root_turn_admission",
}
_MODEL_UNIVERSE_ADDED_ALL = {
    "AcceptedRootTurnAdmission",
    "PreparedRootTurnIntent",
    "build_prepared_root_turn_intent",
}
_MODEL_UNIVERSE_REMOVED_ALL = {
    "PreparedRootTurnAdmission",
    "build_prepared_root_turn_admission",
}
_MODEL_UNIVERSE_ADDED_TOP_LEVEL_CLASSES = {
    "AcceptedRootTurnAdmission",
    "PreparedRootTurnIntent",
}
_MODEL_UNIVERSE_ADDED_TOP_LEVEL_FUNCTIONS = {
    "build_prepared_root_turn_intent",
}
_MODEL_UNIVERSE_CHANGED_TOP_LEVEL_FUNCTIONS = {
    "_root_turn_admission_payload",
    "build_prepared_root_turn_admission",
}
_MODEL_UNIVERSE_ADDED_METHODS = {
    "accept_root_turn_intent",
    "confirm_root_turn_intent",
    "read_session_model_call_binding",
    "read_turn_model_call_binding",
    "reject_prepared_prompt_head_model_unavailable",
    "update_session_model_call_binding",
}
_PR03_ADDED_METHODS = {
    "_user_control_feedback_event",
    "accept_user_control_feedback",
    "confirm_user_control_feedback_winner",
    "list_subagent_task_activities",
    "list_subagent_task_groups",
}
_PR03_CHANGED_METHODS = {
    "interrupt_turn",
}
_MODEL_UNIVERSE_CHANGED_METHODS = {
    "confirm_prompt_ingress",
    "enqueue_prompt",
}
_MODEL_UNIVERSE_REMOVED_METHODS = {
    "accept_root_turn",
    "confirm_root_turn_admission",
    "start_root_turn",
}
_MODEL_UNIVERSE_RUNTIME_ADDED_DATACLASSES = {
    "AcceptedRootTurnAdmission",
    "PreparedRootTurnIntent",
}
_ASYNC_SUBAGENT_COMPLETION_ADDED_OBSERVED_IMPORTS = {
    "AcceptedSubagentCompletion",
    "SubagentCompletionDisposition",
}
_ASYNC_SUBAGENT_COMPLETION_ADDED_ALL = {
    "AcceptedSubagentCompletion",
    "SubagentCompletionDisposition",
}
_ASYNC_SUBAGENT_COMPLETION_ADDED_TOP_LEVEL_CLASSES = {
    "AcceptedSubagentCompletion",
    "SubagentCompletionDisposition",
}
_ASYNC_SUBAGENT_COMPLETION_ADDED_METHODS = {
    "_accepted_completion_row",
    "_bind_completion_command",
    "_completion_accepted_entry",
    "_completion_source_row",
    "_prepare_completion_target",
}
_SUBAGENT_WAIT_REFINEMENT_ADDED_METHODS = {
    "validate_host_writer",
}
_ASYNC_SUBAGENT_COMPLETION_REMOVED_METHODS = {
    "_prepare_external_result_target",
}
_ASYNC_SUBAGENT_COMPLETION_CHANGED_METHODS = {
    "accept_subagent_completion_into_root",
}
_ASYNC_SUBAGENT_COMPLETION_ADDED_RUNTIME_DATACLASSES = {
    "AcceptedSubagentCompletion",
}
_MEMORY_GOVERNANCE_HARD_CUT_ADDED_TOP_LEVEL_FUNCTIONS = {
    "_memory_governance_entry_is_human",
    "_memory_governance_entry_product_kind",
    "_memory_governance_incomplete_human_marker",
    "_memory_governance_source_role_and_label",
    "_memory_governance_terminal_fence",
    "_read_memory_governance_terminal_candidate",
}
_MEMORY_GOVERNANCE_HARD_CUT_REMOVED_TOP_LEVEL_FUNCTIONS = {
    "_governance_public_text",
}
_MEMORY_GOVERNANCE_HARD_CUT_ADDED_METHODS = {
    "_read_assistant_public_blocks",
    "_read_memory_governance_producer_cut",
    "_read_memory_governance_producer_output",
    "_read_memory_governance_source_item",
    "_read_memory_governance_terminal_suffix",
    "confirm_memory_governance_terminal_fence",
}
_MEMORY_GOVERNANCE_HARD_CUT_REMOVED_METHODS = {
    "_read_assistant_public_body",
    "_read_memory_governance_turn_projection",
}
_ROUND7_ADDED_TOP_LEVEL_FUNCTIONS = {"_plan_question_response"}
_ROUND7_CHANGED_TOP_LEVEL_FUNCTIONS = {
    "_prepared_tool_result_manifest",
    "build_prepared_tool_result_acceptance",
}
_FINGERPRINT_HARD_CUT_CHANGED_TOP_LEVEL_FUNCTIONS = {
    "_prompt_steer_row_matches_resource_rejection",
}
_ROUND7_ADDED_METHODS = {
    "_subagent_cancellation_drafts",
    "confirm_cancelled_subagent_turn_and_task",
    "read_turn_terminal_outcome",
    "settle_cancelled_subagent_turn_and_task",
}
_ROUND7_CHANGED_METHODS = {
    "_accept_rejected_plan_tool_batch_in_transaction",
    "_confirm_plan_resolution_in_transaction",
    "_confirm_plan_tool_batch_in_transaction",
    "accept_subagent_child",
    "accept_plan_tool_batch",
    "accept_tool_capability_decision",
    "accept_tool_interaction_decision",
    "accept_tool_result",
    "confirm_plan_question_winner",
    "confirm_tool_result_winner",
    "read_turn_status",
    "resolve_plan_question",
    "set_subagent_task_status",
}
_ROUND7_RUNTIME_CHANGED_METHODS = {
    "_confirm_plan_resolution_in_transaction",
    "accept_subagent_child",
    "set_subagent_task_status",
}
# Exact digest of the deliberately changed Round 7 repository slice.  The M0
# fixture remains immutable; all definitions and physical DB calls outside
# this closed allowlist must still equal that historical checkpoint exactly.
_ROUND7_REPOSITORY_DELTA_SHA256 = (
    "f11be8ab473f29aba733e1f094af393c8d745541da502675ac9f16e1579b76a9"
)

# Round 8 deliberately replaces the old memory projection/job surface with the
# advisory-memory candidate/fact/relation transactions.  Keep the M0 fixture
# immutable and describe that evolution as a closed delta: anything outside
# these sets must remain byte-for-byte equivalent to the modularization
# checkpoint.
_ROUND8_ADDED_ALL = {"AcceptedMemoryGovernance"}
_ROUND8_REMOVED_ALL = {"MemoryVectorFactSource", "MemoryVectorSource"}
_ROUND8_ADDED_TOP_LEVEL_CLASSES = {"_ObservedActiveMemoryDuplicate"}
_ROUND8_REMOVED_TOP_LEVEL_CLASSES = {
    "AcceptedMemoryCandidate",
    "MemoryVectorFactSource",
    "MemoryVectorSource",
}
_ROUND8_ADDED_TOP_LEVEL_FUNCTIONS = {
    "_decode_governance_projection",
    "_governance_public_text",
}
_ROUND8_ADDED_METHODS = {
    "_accept_memory_governance_once",
    "_active_semantic_winner",
    "_candidate_owns_no_memory_rows",
    "_confirm_existing_relation",
    "_confirm_processing_existing_source_settlement",
    "_expected_relation_tuple",
    "_fact_draft_row",
    "_find_exact_relation",
    "_freeze_existing_source_relation_settlement",
    "_governance_relations_match",
    "_insert_governance_relations",
    "_insert_memory_fact",
    "_insert_prepared_memory_candidate",
    "_insert_relation",
    "_lock_basis_targets",
    "_lock_governance_target",
    "_lock_processing_candidate",
    "_lock_response_preference_context",
    "_memory_fact_matches",
    "_memory_fact_settlement_identity",
    "_memory_settlement_identity_matches",
    "_prepare_memory_duplicate_outcome",
    "_prepared_governance_inputs_still_match",
    "_read_assistant_public_body",
    "_read_entry_public_body",
    "_read_governance_target_for_confirmation",
    "_read_memory_governance_tool_evidence",
    "_read_memory_governance_turn_projection",
    "_read_memory_public_facts",
    "_read_prepared_memory_candidate",
    "_relation_tuple",
    "_response_preference_capacity_allows",
    "_settle_existing_source_memory_relation_once",
    "abandon_memory_candidate",
    "claim_memory_candidate_for_governance",
    "confirm_memory_candidate_intake",
    "confirm_memory_governance_winner",
    "list_unembedded_memory_facts",
    "prepare_existing_source_memory_relation_settlement",
    "read_memory_governance_evidence",
    "settle_existing_source_memory_relation",
    "upsert_memory_embedding",
}
_ROUND8_REMOVED_METHODS = {
    "accept_extracted_memory_bundle",
    "accept_memory_candidate_and_governance_job",
    "apply_fts_memory_index",
    "apply_vector_memory_index",
    "read_memory_extraction_job_source",
    "snapshot_memory_vector_source",
}
_ROUND8_CHANGED_METHODS = {
    "_confirm_memory_proposal_side_branch",
    "_insert_event",
    "accept_compaction_job_result",
    "accept_memory_governance",
    "accept_tool_result",
    "acquire_host_writer",
    "confirm_tool_result_winner",
    "read_memory_candidate_for_governance",
    "renew_host_writer",
}
_ROUND8_RUNTIME_ADDED_EXCEPTIONS = {"_ObservedActiveMemoryDuplicate"}
_ROUND8_RUNTIME_REMOVED_DATACLASSES = {
    "AcceptedMemoryCandidate",
    "MemoryVectorFactSource",
    "MemoryVectorSource",
}
_ROUND8_RUNTIME_CHANGED_DATACLASSES = {
    "AcceptedMemoryGovernance",
    "PreparedMemoryProposalSideBranch",
    "PreparedToolResultAcceptance",
}
_ROUND9_2_RUNTIME_CHANGED_DATACLASSES = {
    # Process-local Hook carriers and the shared accepted ToolResult
    # settlement are fields on existing repository DTOs; no durable schema or
    # replay authority is introduced.
    "AcceptedCapabilityDecision",
    "AcceptedEntry",
    "AcceptedInteractionDecision",
    "AcceptedPlanResolution",
    "AcceptedPlanToolBatch",
    "PreparedPlanBatchCall",
    "PreparedRootTurnAdmission",
    "PreparedSubagentTurnAdmission",
}
_TODO_REFINEMENT_ADDED_METHODS = {
    "_queued_root_entry_matches_candidate",
    "_queued_root_revision_matches_candidate",
    "_queued_root_row_matches_candidate",
    "_queued_root_turn_matches_candidate",
    "confirm_prepared_prompt_head_consumption",
    "consume_prepared_prompt_head",
    "prepare_prompt_head_consumption",
}
_TODO_REFINEMENT_REMOVED_METHODS = {"consume_prompt_head"}
_ROUND5A2_CHANGED_METHODS = {
    "_insert_entry",
    "commit_assistant_message",
    "confirm_assistant_message_winner",
}
_ROUND5B_REMOVED_OBSERVED_IMPORTS = {
    "AcceptedJobAttempt",
    "JobAttemptTerminalized",
    "JobCancellationRequested",
    "StaleJobClaim",
}
_ROUND5B_REMOVED_ALL = {
    "AcceptedJobAttempt",
    "AcceptedJobSettlement",
    "JobAttemptTerminalized",
    "StaleJobClaim",
}
_ROUND5B_REMOVED_TOP_LEVEL_CLASSES = {
    "AcceptedJobAttempt",
    "AcceptedJobSettlement",
    "JobAttemptTerminalized",
    "JobCancellationRequested",
    "StaleJobClaim",
    # The public FQCN remains repository.ConversationKernelConflict; only the
    # implementation-package class definition moved to a neutral error leaf so
    # canonical readers do not bypass the repository facade boundary.
    "ConversationKernelConflict",
}
_ROUND5B_ADDED_TOP_LEVEL_FUNCTIONS = {"_manual_compaction_turn_matches"}
_ROUND5B_REMOVED_TOP_LEVEL_FUNCTIONS = {"_load_root_transcript_cut"}
_ROUND5B_ADDED_METHODS = {
    "_compaction_binding_row_matches",
    "_compaction_predecessor_row_matches",
    "_compaction_snapshot_row_matches",
    "_initial_context_binding_revision_matches",
    "_insert_initial_context_binding_revision",
    "_require_compaction_source_digest",
    "_require_compaction_target",
    "accept_manual_compaction_command",
    "confirm_context_snapshot_adoption",
    "confirm_manual_compaction_command",
    "prepare_compaction_input_cut",
    "read_latest_terminal_scope_turn_id",
}
_ROUND5B_REMOVED_METHODS = {
    "_job_transaction",
    "_require_job_claim",
    "accept_compaction_job_result",
    "accept_job_result_into_root",
    "claim_due_job",
    "confirm_active_job_claim",
    "enqueue_background_compaction",
    "enqueue_job",
    "mark_job_provider_call_started",
    "prepare_job_claim_candidate",
    "read_compaction_job_source",
    "request_job_cancel",
    "settle_job_attempt",
}
_ROUND5B_CHANGED_METHODS = {
    "_append_events",
    "_insert_plan_continuation_turn",
    "_insert_resolution_plan_continuation",
    "_prepare_external_result_target",
    "accept_terminal_observation",
    "adopt_context_snapshot",
    "confirm_root_turn_admission",
    "confirm_subagent_turn_admission",
    "confirm_terminal_observation_winner",
    "query_command",
    "start_root_turn",
    "start_subagent_turn",
}
_ROUND5B_RUNTIME_REMOVED_DATACLASSES = {
    "AcceptedJobAttempt",
    "AcceptedJobSettlement",
}
_ROUND5B_RUNTIME_REMOVED_EXCEPTIONS = {
    "ConversationKernelConflict",
    "JobAttemptTerminalized",
    "JobCancellationRequested",
    "StaleJobClaim",
}
_ROUND5B_RUNTIME_CHANGED_METHODS = {
    "_append_events",
    "adopt_context_snapshot",
}
_ROUND10_ADDED_TOP_LEVEL_FUNCTIONS = {
    "_explicit_result_arguments_match",
    "_subagent_batch_arguments_match",
    "_subagent_batch_subject_matches",
    "_subagent_context_arguments_match",
}
_ROUND9_2_ADDED_TOP_LEVEL_FUNCTIONS = {
    # Round 9.2 hard-cuts the compiler-private Plan storage conversion into
    # the one pure public ToolResult settlement/projection constructor shared
    # by the Plan owner, compiler, and Hook lifecycle seam.
    "_plan_tool_result_settlement",
}
_ROUND9_2_CHANGED_TOP_LEVEL_FUNCTIONS = {
    # The prompt admission candidate now freezes the exact permission snapshot
    # used by the shared direct/queued UserPromptSubmit Hook gate.
    "build_prepared_root_turn_admission",
    # SubagentStart uses the exact accepted task-start event and permission
    # carrier prepared by the repository admission owner.
    "_subagent_turn_admission_payload",
    "build_prepared_subagent_turn_admission",
}
_ROUND10_ADDED_METHODS = {
    "_explicit_result_events",
    "_subagent_batch_task_event",
    "_subagent_task_start_event",
    "_subagent_task_terminal_event",
    "accept_explicit_subagent_result",
    "accept_inter_agent_mailbox_batch",
    "accept_subagent_task_batch",
    "accept_subagent_task_start",
    "accept_subagent_task_terminal_settlement",
    "confirm_explicit_subagent_result",
    "confirm_inter_agent_mailbox_batch",
    "confirm_subagent_task_batch",
    "confirm_subagent_task_start",
    "confirm_subagent_task_terminal_settlement",
    "list_runnable_subagent_tasks",
    "read_subagent_dependencies",
    "read_subagent_task_board",
    "settle_subagent_dependency_frontier",
}
_ROUND10_CHANGED_METHODS = {
    "_interrupt_prior_generation",
    # Result acceptance now copies the durable public result summary rather
    # than the producer tool acknowledgement into the ROOT transcript.
    "accept_subagent_completion_into_root",
    "list_subagent_tasks",
    "query_subagent_task",
}
_ROUND9_2_ADDED_METHODS = {
    # Lifecycle owners freeze the exact permission carriers before any Hook
    # command runs; neither method creates a new durable authority.
    "prepare_root_permission_snapshot",
    "prepare_subagent_launch_permission",
}
_ROUND9_2_CHANGED_METHODS = {
    # Both canonical prompt admission owners now consume exact Hook-gated
    # candidates while retaining their distinct stable-ID formulas.
    "accept_subagent_turn",
    "enqueue_prompt",
    "start_subagent_turn",
}
_ROUND10_REMOVED_METHODS = {
    "accept_subagent_child",
    "accept_subagent_task",
    "set_subagent_task_status",
}


def _package_for_source(path: Path) -> str:
    relative = path.relative_to(ROOT / "src").with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    else:
        parts = parts[:-1]
    return ".".join(parts)


def _absolute_import_targets(
    tree: ast.AST,
    *,
    current_package: str,
) -> set[str]:
    """Resolve imported modules and imported aliases to absolute targets."""

    targets: set[str] = set()
    package_parts = current_package.split(".") if current_package else []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            retained = len(package_parts) - (node.level - 1)
            base_parts = package_parts[: max(0, retained)]
            if node.module:
                base_parts.extend(node.module.split("."))
            base = ".".join(base_parts)
        else:
            base = node.module or ""
        if base:
            targets.add(base)
        for alias in node.names:
            if alias.name == "*":
                continue
            targets.add(f"{base}.{alias.name}" if base else alias.name)
    return targets


def _is_internal_repository_target(target: str) -> bool:
    return target == _INTERNAL_REPOSITORY_PACKAGE or target.startswith(
        f"{_INTERNAL_REPOSITORY_PACKAGE}."
    )


def _inventory_module():
    spec = importlib.util.spec_from_file_location(
        "repository_modularization_inventory", TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _baseline() -> dict[str, object]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _without_source_modules(value: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {key: item for key, item in record.items() if key != "source_module"}
        for record in value
    ]


def _closed_owner_calls(calls: list[str], renames: dict[str, str]) -> list[str]:
    return sorted(renames.get(call, call) for call in calls)


def _round7_repository_delta(current: dict[str, object]) -> dict[str, object]:
    changed_functions = (
        _ROUND7_ADDED_TOP_LEVEL_FUNCTIONS | _ROUND7_CHANGED_TOP_LEVEL_FUNCTIONS
    )
    changed_methods = _ROUND7_ADDED_METHODS | _ROUND7_CHANGED_METHODS
    runtime = current["runtime"]
    assert isinstance(runtime, dict)
    dataclasses = runtime["dataclasses"]
    methods = runtime["methods"]
    assert isinstance(dataclasses, dict) and isinstance(methods, dict)
    return {
        "top_level_functions": {
            name: current["top_level_functions"][name]
            for name in sorted(changed_functions)
        },
        "methods": {
            name: current["methods"][name]
            for name in sorted(changed_methods)
            if name in current["methods"]
        },
        "runtime_dataclasses": {
            "PreparedToolResultAcceptance": dataclasses["PreparedToolResultAcceptance"]
        },
        "runtime_methods": {
            name: methods[name]
            for name in sorted(_ROUND7_ADDED_METHODS | _ROUND7_RUNTIME_CHANGED_METHODS)
        },
        "database_calls": _without_source_modules(
            [
                record
                for record in current["database_calls"]
                if record["owner"] in changed_methods
            ]
        ),
        "physical_checkouts": _without_source_modules(
            [
                record
                for record in current["physical_checkouts"]
                if record["owner"] in changed_methods
            ]
        ),
    }


def _round8_repository_delta(current: dict[str, object]) -> dict[str, object]:
    changed_functions = (
        _ROUND7_ADDED_TOP_LEVEL_FUNCTIONS
        | _ROUND7_CHANGED_TOP_LEVEL_FUNCTIONS
        | _ROUND8_ADDED_TOP_LEVEL_FUNCTIONS
        | _ROUND5B_ADDED_TOP_LEVEL_FUNCTIONS
        | _ROUND10_ADDED_TOP_LEVEL_FUNCTIONS
        | _ROUND9_2_ADDED_TOP_LEVEL_FUNCTIONS
        | _ROUND9_2_CHANGED_TOP_LEVEL_FUNCTIONS
        | _MODEL_UNIVERSE_CHANGED_TOP_LEVEL_FUNCTIONS
    )
    changed_methods = (
        _ROUND7_ADDED_METHODS
        | _ROUND7_CHANGED_METHODS
        | _ROUND8_ADDED_METHODS
        | _ROUND8_CHANGED_METHODS
        | _ROUND5B_ADDED_METHODS
        | _ROUND5B_CHANGED_METHODS
        | _ROUND10_ADDED_METHODS
        | _ROUND10_CHANGED_METHODS
        | _ROUND9_2_ADDED_METHODS
        | _ROUND9_2_CHANGED_METHODS
    )
    runtime = current["runtime"]
    assert isinstance(runtime, dict)
    return {
        "all": current["all"],
        "top_level_classes": current["top_level_classes"],
        "top_level_functions": {
            name: current["top_level_functions"][name]
            for name in sorted(changed_functions)
        },
        "methods": {
            name: current["methods"][name]
            for name in sorted(changed_methods)
            if name in current["methods"]
        },
        "round5b_removed_observed_imports": sorted(_ROUND5B_REMOVED_OBSERVED_IMPORTS),
        "round5b_removed_methods": sorted(_ROUND5B_REMOVED_METHODS),
        "runtime_exceptions": runtime["exceptions"],
        "runtime_dataclasses": runtime["dataclasses"],
        "runtime_methods": {
            name: runtime["methods"][name]
            for name in sorted(
                _ROUND7_ADDED_METHODS
                | _ROUND7_RUNTIME_CHANGED_METHODS
                | _ROUND8_ADDED_METHODS
                | _ROUND8_CHANGED_METHODS
                | _ROUND5B_ADDED_METHODS
                | _ROUND5B_RUNTIME_CHANGED_METHODS
                | _ROUND10_ADDED_METHODS
                | _ROUND9_2_ADDED_METHODS
            )
            if name in runtime["methods"]
        },
        "database_calls": _without_source_modules(
            [
                record
                for record in current["database_calls"]
                if record["owner"] in changed_methods
            ]
        ),
        "physical_checkouts": _without_source_modules(
            [
                record
                for record in current["physical_checkouts"]
                if record["owner"] in changed_methods
            ]
        ),
    }


def test_repository_modularization_baseline_is_exact_at_checkpoint() -> None:
    baseline = _baseline()
    assert baseline["checkpoint_head"] == "edbe7aea5518085028657aedc161d8fcbe88bb6b"
    assert baseline["repository_sha256"] == (
        "43669989c424012e84874d15d85ca3d6842f216d025fa2ae2be293166b2b915e"
    )
    assert len(baseline["methods"]) == 128
    assert len(baseline["top_level_functions"]) == 29
    assert len(baseline["all"]) == 34
    assert len(baseline["observed_imports"]) == 41
    assert len(baseline["runtime"]["owned_observed_symbols"]) == 39
    assert (
        len(set(baseline["pytest_node_ids"]) - set(baseline["m0_gate_node_ids"])) == 541
    )
    checkouts = baseline["physical_checkouts"]
    assert len(checkouts) == 39
    assert sum(item["classification"] == "direct_operation" for item in checkouts) == 34
    assert (
        sum(item["classification"] == "writer_bootstrap_or_renew" for item in checkouts)
        == 2
    )
    assert (
        sum(
            item["owner"]
            in {"_writer_transaction", "_job_transaction", "_event_transaction"}
            for item in checkouts
        )
        == 3
    )


def test_repository_modularization_current_contract_matches_baseline() -> None:
    module = _inventory_module()
    current = module.build_inventory(include_pytest_nodes=False)
    baseline = _baseline()
    assert set(current["observed_imports"]) == (
        set(baseline["observed_imports"])
        - _ROUND5B_REMOVED_OBSERVED_IMPORTS
        - _MODEL_UNIVERSE_REMOVED_OBSERVED_IMPORTS
    ) | _ASYNC_SUBAGENT_COMPLETION_ADDED_OBSERVED_IMPORTS | (
        _MODEL_UNIVERSE_ADDED_OBSERVED_IMPORTS
    )
    for key in ("closed_owner_renames", "override_seams"):
        assert current[key] == baseline[key], key
    assert (
        set(current["all"])
        == (
            set(baseline["all"])
            - _ROUND8_REMOVED_ALL
            - _ROUND5B_REMOVED_ALL
            - _MODEL_UNIVERSE_REMOVED_ALL
        )
        | _ROUND8_ADDED_ALL
        | _ASYNC_SUBAGENT_COMPLETION_ADDED_ALL
        | _MODEL_UNIVERSE_ADDED_ALL
    )
    assert (
        set(current["top_level_classes"])
        == (
            set(baseline["top_level_classes"])
            - _ROUND8_REMOVED_TOP_LEVEL_CLASSES
            - _ROUND5B_REMOVED_TOP_LEVEL_CLASSES
        )
        | _ROUND8_ADDED_TOP_LEVEL_CLASSES
        | _ASYNC_SUBAGENT_COMPLETION_ADDED_TOP_LEVEL_CLASSES
        | _MODEL_UNIVERSE_ADDED_TOP_LEVEL_CLASSES
        | {"FrozenMemoryDeletionPlan", "CanonicalForkCreation"}
    )
    for key, added, changed in (
        (
            "top_level_functions",
            (
                _ROUND7_ADDED_TOP_LEVEL_FUNCTIONS
                | _ROUND8_ADDED_TOP_LEVEL_FUNCTIONS
                | _ROUND5B_ADDED_TOP_LEVEL_FUNCTIONS
                | _ROUND10_ADDED_TOP_LEVEL_FUNCTIONS
                | _ROUND9_2_ADDED_TOP_LEVEL_FUNCTIONS
                | _MODEL_UNIVERSE_ADDED_TOP_LEVEL_FUNCTIONS
                | {"_freeze", "_frozen_rows", "_remaining", "_insert"}
            )
            - _MEMORY_GOVERNANCE_HARD_CUT_REMOVED_TOP_LEVEL_FUNCTIONS
            | _MEMORY_GOVERNANCE_HARD_CUT_ADDED_TOP_LEVEL_FUNCTIONS,
            (
                _ROUND7_CHANGED_TOP_LEVEL_FUNCTIONS
                | _FINGERPRINT_HARD_CUT_CHANGED_TOP_LEVEL_FUNCTIONS
                | _ROUND9_2_CHANGED_TOP_LEVEL_FUNCTIONS
                | _MODEL_UNIVERSE_CHANGED_TOP_LEVEL_FUNCTIONS
            ),
        ),
        (
            "methods",
            (
                _ROUND7_ADDED_METHODS
                | _ROUND8_ADDED_METHODS
                | _TODO_REFINEMENT_ADDED_METHODS
                | _ROUND5B_ADDED_METHODS
                | _ROUND10_ADDED_METHODS
                | _ROUND9_2_ADDED_METHODS
                | _ASYNC_SUBAGENT_COMPLETION_ADDED_METHODS
                | _SUBAGENT_WAIT_REFINEMENT_ADDED_METHODS
                | _MODEL_UNIVERSE_ADDED_METHODS
                | _PR03_ADDED_METHODS
                | {
                    "_management_connection",
                    "fork_conversation",
                    "_management_context",
                    "_management_labels",
                    "_management_relation",
                    "memory_management_projects",
                    "memory_management_catalog",
                    "memory_management_detail",
                    "memory_deletion_preview",
                    "_memory_deletion_plan",
                    "execute_memory_deletion",
                    "_memory_exact_delete",
                }
            )
            - _MEMORY_GOVERNANCE_HARD_CUT_REMOVED_METHODS
            | _MEMORY_GOVERNANCE_HARD_CUT_ADDED_METHODS,
            _ROUND7_CHANGED_METHODS
            | _ROUND8_CHANGED_METHODS
            | _ROUND5A2_CHANGED_METHODS
            | _ROUND5B_CHANGED_METHODS
            | _ROUND10_CHANGED_METHODS
            | _ROUND9_2_CHANGED_METHODS
            | _ASYNC_SUBAGENT_COMPLETION_CHANGED_METHODS
            | _PR03_CHANGED_METHODS
            | _MODEL_UNIVERSE_CHANGED_METHODS | _FORK_CHANGED_METHODS,
        ),
    ):
        removed = (
            _ROUND8_REMOVED_METHODS
            | _TODO_REFINEMENT_REMOVED_METHODS
            | _ROUND5B_REMOVED_METHODS
            | _ROUND10_REMOVED_METHODS
            | _ASYNC_SUBAGENT_COMPLETION_REMOVED_METHODS
            | _MEMORY_GOVERNANCE_HARD_CUT_REMOVED_METHODS
            | _MODEL_UNIVERSE_REMOVED_METHODS
            if key == "methods"
            else (
                _ROUND5B_REMOVED_TOP_LEVEL_FUNCTIONS
                | _MEMORY_GOVERNANCE_HARD_CUT_REMOVED_TOP_LEVEL_FUNCTIONS
            )
        )
        assert set(current[key]) == (set(baseline[key]) - removed) | added
        for name in set(baseline[key]) - changed:
            if name in removed:
                continue
            assert current[key][name] == baseline[key][name], (key, name)
    current_runtime = current["runtime"]
    baseline_runtime = baseline["runtime"]
    for key in set(baseline_runtime) - {
        "dataclasses",
        "exceptions",
        "methods",
        "observed_symbols",
        "owned_observed_symbols",
    }:
        assert current_runtime[key] == baseline_runtime[key], ("runtime", key)
    for key in ("observed_symbols", "owned_observed_symbols"):
        assert set(current_runtime[key]) == (
            set(baseline_runtime[key])
            - _ROUND5B_REMOVED_OBSERVED_IMPORTS
            - _MODEL_UNIVERSE_REMOVED_OBSERVED_IMPORTS
        ) | _ASYNC_SUBAGENT_COMPLETION_ADDED_OBSERVED_IMPORTS | (
            _MODEL_UNIVERSE_ADDED_OBSERVED_IMPORTS
        )
        if isinstance(current_runtime[key], dict):
            for name in (
                set(current_runtime[key])
                - _ASYNC_SUBAGENT_COMPLETION_ADDED_OBSERVED_IMPORTS
                - _MODEL_UNIVERSE_ADDED_OBSERVED_IMPORTS
            ):
                assert current_runtime[key][name] == baseline_runtime[key][name]
    assert (
        set(current_runtime["exceptions"])
        == (set(baseline_runtime["exceptions"]) - _ROUND5B_RUNTIME_REMOVED_EXCEPTIONS)
        | _ROUND8_RUNTIME_ADDED_EXCEPTIONS
    )
    for name in (
        set(baseline_runtime["exceptions"]) - _ROUND5B_RUNTIME_REMOVED_EXCEPTIONS
    ):
        assert (
            current_runtime["exceptions"][name] == baseline_runtime["exceptions"][name]
        )
    assert set(current_runtime["dataclasses"]) == (
        set(baseline_runtime["dataclasses"])
        - _ROUND8_RUNTIME_REMOVED_DATACLASSES
        - _ROUND5B_RUNTIME_REMOVED_DATACLASSES
    ) | _ASYNC_SUBAGENT_COMPLETION_ADDED_RUNTIME_DATACLASSES | (
        _MODEL_UNIVERSE_RUNTIME_ADDED_DATACLASSES | {"FrozenMemoryDeletionPlan", "CanonicalForkCreation"}
    )
    for name in (
        set(baseline_runtime["dataclasses"])
        - _ROUND8_RUNTIME_REMOVED_DATACLASSES
        - _ROUND5B_RUNTIME_REMOVED_DATACLASSES
        - _MODEL_UNIVERSE_REMOVED_ALL
        - _ROUND8_RUNTIME_CHANGED_DATACLASSES
        - _ROUND9_2_RUNTIME_CHANGED_DATACLASSES
    ):
        assert (
            current_runtime["dataclasses"][name]
            == baseline_runtime["dataclasses"][name]
        )
    assert (
        set(current_runtime["methods"])
        == (
            set(baseline_runtime["methods"])
            - _ROUND8_REMOVED_METHODS
            - _TODO_REFINEMENT_REMOVED_METHODS
            - _ROUND5B_REMOVED_METHODS
            - _ROUND10_REMOVED_METHODS
            - _ASYNC_SUBAGENT_COMPLETION_REMOVED_METHODS
            - _MEMORY_GOVERNANCE_HARD_CUT_REMOVED_METHODS
            - _MODEL_UNIVERSE_REMOVED_METHODS
        )
        | (
            _ROUND7_ADDED_METHODS
            | _ROUND8_ADDED_METHODS
            | _TODO_REFINEMENT_ADDED_METHODS
            | _ROUND5B_ADDED_METHODS
            | _ROUND10_ADDED_METHODS
            | _ROUND9_2_ADDED_METHODS
            | _ASYNC_SUBAGENT_COMPLETION_ADDED_METHODS
            | _SUBAGENT_WAIT_REFINEMENT_ADDED_METHODS
            | _MODEL_UNIVERSE_ADDED_METHODS
            | _PR03_ADDED_METHODS
            | {
                "_management_connection",
                "fork_conversation",
                "_management_context",
                "_management_labels",
                "_management_relation",
                "memory_management_projects",
                "memory_management_catalog",
                "memory_management_detail",
                "memory_deletion_preview",
                "_memory_deletion_plan",
                "execute_memory_deletion",
                "_memory_exact_delete",
            }
        )
        - _MEMORY_GOVERNANCE_HARD_CUT_REMOVED_METHODS
        - _MODEL_UNIVERSE_REMOVED_METHODS
        | _MEMORY_GOVERNANCE_HARD_CUT_ADDED_METHODS
    )
    for name in (
        set(baseline_runtime["methods"])
        - _ROUND7_RUNTIME_CHANGED_METHODS
        - _ROUND8_CHANGED_METHODS
        - _ROUND5A2_CHANGED_METHODS
        - _ROUND5B_RUNTIME_CHANGED_METHODS
        - _ROUND10_CHANGED_METHODS
        - _ROUND9_2_CHANGED_METHODS
        - _ASYNC_SUBAGENT_COMPLETION_CHANGED_METHODS
        - _ROUND8_REMOVED_METHODS
        - _TODO_REFINEMENT_REMOVED_METHODS
        - _ROUND5B_REMOVED_METHODS
        - _ROUND10_REMOVED_METHODS
        - _ASYNC_SUBAGENT_COMPLETION_REMOVED_METHODS
        - _MEMORY_GOVERNANCE_HARD_CUT_REMOVED_METHODS
        - _MODEL_UNIVERSE_REMOVED_METHODS
        - _MODEL_UNIVERSE_CHANGED_METHODS
        - _PR03_CHANGED_METHODS
    ):
        assert current_runtime["methods"][name] == baseline_runtime["methods"][name]
    assert set(
        _closed_owner_calls(
            current["class_qualified_calls"], baseline["closed_owner_renames"]
        )
    ) == set(
        _closed_owner_calls(
            baseline["class_qualified_calls"], baseline["closed_owner_renames"]
        )
    ) | {
        "_RepositoryKernel._initial_context_binding_revision_matches",
        "_RepositoryKernel._insert_initial_context_binding_revision",
    }
    changed_owners = (
        {
            "_management_connection",
            "_management_context",
            "_management_labels",
            "_management_relation",
            "memory_management_projects",
            "memory_management_catalog",
            "memory_management_detail",
            "memory_deletion_preview",
            "_memory_deletion_plan",
            "execute_memory_deletion",
            "_memory_exact_delete",
            "_remaining",
        }
        | _ROUND7_ADDED_METHODS
        | _ROUND7_CHANGED_METHODS
        | _ROUND8_ADDED_METHODS
        | _ROUND8_CHANGED_METHODS
        | _ROUND8_REMOVED_METHODS
        | _TODO_REFINEMENT_ADDED_METHODS
        | _TODO_REFINEMENT_REMOVED_METHODS
        | _ROUND5A2_CHANGED_METHODS
        | _ROUND5B_ADDED_METHODS
        | _ROUND5B_CHANGED_METHODS
        | _ROUND5B_REMOVED_METHODS
        | _ROUND10_ADDED_METHODS
        | _ROUND10_CHANGED_METHODS
        | _ASYNC_SUBAGENT_COMPLETION_ADDED_METHODS
        | _SUBAGENT_WAIT_REFINEMENT_ADDED_METHODS
        | _ASYNC_SUBAGENT_COMPLETION_CHANGED_METHODS
        | _ASYNC_SUBAGENT_COMPLETION_REMOVED_METHODS
        | _MEMORY_GOVERNANCE_HARD_CUT_ADDED_METHODS
        | _MEMORY_GOVERNANCE_HARD_CUT_REMOVED_METHODS
        | _MEMORY_GOVERNANCE_HARD_CUT_ADDED_TOP_LEVEL_FUNCTIONS
        | _MEMORY_GOVERNANCE_HARD_CUT_REMOVED_TOP_LEVEL_FUNCTIONS
        | _ROUND9_2_ADDED_METHODS
        | _ROUND9_2_CHANGED_METHODS
        | _MODEL_UNIVERSE_ADDED_METHODS
        | _PR03_ADDED_METHODS
        | _PR03_CHANGED_METHODS
        | _MODEL_UNIVERSE_CHANGED_METHODS
        | _MODEL_UNIVERSE_REMOVED_METHODS
        | _ROUND10_REMOVED_METHODS
        | _ROUND10_ADDED_TOP_LEVEL_FUNCTIONS
        | _ROUND9_2_ADDED_TOP_LEVEL_FUNCTIONS
        | _ROUND9_2_CHANGED_TOP_LEVEL_FUNCTIONS
        | _ROUND5B_ADDED_TOP_LEVEL_FUNCTIONS
        | _ROUND5B_REMOVED_TOP_LEVEL_FUNCTIONS
        | _FORK_CHANGED_METHODS
        | {"fork_conversation", "_insert"}
    )
    for key in ("database_calls", "physical_checkouts"):
        current_unchanged = _without_source_modules(
            [item for item in current[key] if item["owner"] not in changed_owners]
        )
        baseline_unchanged = _without_source_modules(
            [item for item in baseline[key] if item["owner"] not in changed_owners]
        )
        assert current_unchanged == baseline_unchanged, key
    import pulsara_agent.conversation_kernel.repository as repository

    for name in baseline["runtime"]["owned_observed_symbols"]:
        if name in (
            _ROUND5B_REMOVED_ALL
            | _ROUND5B_REMOVED_OBSERVED_IMPORTS
            | _MODEL_UNIVERSE_REMOVED_ALL
            | _MODEL_UNIVERSE_REMOVED_OBSERVED_IMPORTS
        ):
            continue
        value = getattr(repository, name)
        assert pickle.loads(pickle.dumps(value)) is value
    for name in repository.__all__:
        value = getattr(repository, name)
        if inspect.isclass(value) or inspect.isfunction(value):
            typing.get_type_hints(value)


def test_repository_modularization_preserves_every_existing_pytest_node() -> None:
    module = _inventory_module()
    baseline_nodes = set(_baseline()["pytest_node_ids"])
    current_nodes = set(module._pytest_node_ids())
    assert baseline_nodes - current_nodes == (
        _FRONTEND_HARD_CUT_RETIRED_PYTEST_NODES
        | _ROUND5B_DURABLE_JOB_SUBTRACTION_RETIRED_PYTEST_NODES
        | _ASYNC_SUBAGENT_COMPLETION_RETIRED_PYTEST_NODES
        | _SUBAGENT_WAIT_REFINEMENT_RETIRED_PYTEST_NODES
        | _MEMORY_TAXONOMY_HARD_CUT_RETIRED_PYTEST_NODES
        | _PERMISSION_HOST_SCOPE_HARD_CUT_RETIRED_PYTEST_NODES
        | _MODEL_UNIVERSE_HARD_CUT_RETIRED_PYTEST_NODES
        # SDK transport owns buffering. The capability hard-cut §6.3 explicitly
        # retires the old per-slot raw byte reservation, rather than keeping an
        # unused implementation merely to retain this historical unit test.
        | {"tests/test_round6_mcp_production.py::test_round6_slot_wire_budget_is_shared_and_released"}
    )
    assert (
        "tests/test_stage2_architecture.py::"
        "test_stage2_ordinary_host_and_renderer_neutral_protocol_select_kernel_v3"
    ) in current_nodes


def test_repository_modularization_facade_and_internal_owner_shape() -> None:
    facade = ROOT / "src/pulsara_agent/conversation_kernel/repository.py"
    implementation = ROOT / "src/pulsara_agent/conversation_kernel/_repository"
    assert facade.exists()
    if implementation.exists():
        forbidden = {"_monolith.py", "monolith.py", "legacy.py"}
        assert not {path.name for path in implementation.glob("*.py")} & forbidden
        for path in implementation.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports = _absolute_import_targets(
                tree,
                current_package=_package_for_source(path),
            )
            assert "pulsara_agent.conversation_kernel.repository" not in imports
        facade_source = facade.read_text(encoding="utf-8")
        assert len(facade_source.splitlines()) < 256
        assert "pulsara_v3." not in facade_source
        assert "._provider.connection" not in facade_source
        provider_owners = [
            path.name
            for path in implementation.glob("*.py")
            if "VerifiedPostgresConnectionProviderProtocol"
            in path.read_text(encoding="utf-8")
        ]
        assert provider_owners == ["kernel.py"]
        for pure_name in ("contracts.py", "matching.py"):
            pure = (implementation / pure_name).read_text(encoding="utf-8")
            assert "psycopg" not in pure
            assert "postgres_connection_provider" not in pure
        assert not (implementation / "jobs.py").exists()
        assert "def _require_compaction_source_digest(" in (
            implementation / "conversation.py"
        ).read_text(encoding="utf-8")
        assert "def _content_from_row(" in (implementation / "matching.py").read_text(
            encoding="utf-8"
        )

    assert ConversationKernelRepository.__module__ == (
        "pulsara_agent.conversation_kernel.repository"
    )
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 30
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 11
    assert len(APPEND_GUARDS) == 1
    assert len(CONVERSATION_KERNEL_RELATIONS) == 28


def test_repository_modularization_internal_package_is_not_a_second_public_api() -> (
    None
):
    implementation = ROOT / "src/pulsara_agent/conversation_kernel/_repository"
    production = ROOT / "src/pulsara_agent"
    relative_targets = _absolute_import_targets(
        ast.parse("from ._repository.contracts import FrozenContent"),
        current_package="pulsara_agent.conversation_kernel",
    )
    assert "pulsara_agent.conversation_kernel._repository.contracts" in (
        relative_targets
    )
    alias_targets = _absolute_import_targets(
        ast.parse("from pulsara_agent.conversation_kernel import _repository"),
        current_package="pulsara_agent.conversation_kernel",
    )
    assert _INTERNAL_REPOSITORY_PACKAGE in alias_targets
    for path in production.rglob("*.py"):
        if path == ROOT / "src/pulsara_agent/conversation_kernel/repository.py":
            continue
        if implementation in path.parents:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = _absolute_import_targets(
            tree,
            current_package=_package_for_source(path),
        )
        assert not any(_is_internal_repository_target(item) for item in imports), path
