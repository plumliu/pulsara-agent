import asyncio
import json
import os
import urllib.parse
from pathlib import Path
from uuid import uuid4

import pytest

from pulsara_agent.event import (
    EventContext,
    MemoryReflectionFailedEvent,
    MemoryWriteFailedEvent,
    MemoryWriteResultEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    RequireUserConfirmEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RunErrorEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event.candidates import PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.event_log import InMemoryEventLog, PostgresEventLog
from pulsara_agent.entities.memory import Preference
from pulsara_agent.graph import InMemoryGraphStore, PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.llm import LLMMessage, ModelRole, ToolSpec, build_llm_runtime
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory import (
    InMemoryArchiveStore,
    InMemoryCandidatePool,
    MemoryDomainContext,
    MemoryGovernanceEngine,
    MemoryGovernanceExecutor,
    MemoryGovernanceOptions,
    MemoryLifecycle,
    MemoryWriteUnitOfWork,
    PostgresCandidatePool,
    PostgresWorkingContextStore,
    RunTimelinePersistenceHook,
    load_run_timeline,
    summarize_run_timeline,
    workspace_scope,
)
from pulsara_agent.memory.candidates.pool import CandidateOrigin, PooledMemoryCandidate
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.reflection.engine import MemoryReflectionEngine, MemoryReflectionHint, MemoryReflectionOptions
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.memory.canonical.write_service import MemoryWriteService
from pulsara_agent.message import TextBlock, ThinkingBlock, ToolCallBlock, UserMsg
from pulsara_agent.ontology import memory, runtime as rt
from pulsara_agent.runtime import AgentRuntime, LoopState, RuntimeSession, build_agent_runtime_wiring
from pulsara_agent.settings import PulsaraSettings
from pulsara_agent.tools.builtins.memory import RememberPreferenceTool


pytestmark = pytest.mark.real_llm


def _run_error_diagnostic(event: RunErrorEvent) -> dict:
    return {
        "message": event.message,
        "code": event.code,
        "metadata": event.metadata,
    }


def _run_error_diagnostics(events) -> list[dict]:
    return [
        _run_error_diagnostic(event)
        for event in events
        if isinstance(event, RunErrorEvent)
    ]


def test_real_flash_model_emits_replayable_agent_events():
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_flash_smoke())

    assert result["errors"] == []
    assert result["event_type_names"][0] == "ReplyStartEvent"
    assert "ModelCallStartEvent" in result["event_type_names"]
    assert "TextBlockDeltaEvent" in result["event_type_names"]
    assert "ModelCallEndEvent" in result["event_type_names"]
    assert result["event_type_names"][-1] == "ReplyEndEvent"
    assert "PULSARA_OK" in result["text"]
    assert result["replayed_text"]
    assert "PULSARA_OK" in result["replayed_text"]


def test_real_flash_model_can_emit_tool_call_events():
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_tool_call_smoke())

    assert result["errors"] == []
    assert "ToolCallStartEvent" in result["event_type_names"]
    assert "ToolCallDeltaEvent" in result["event_type_names"]
    assert "ToolCallEndEvent" in result["event_type_names"]
    assert result["tool_call_name"] == "echo_tool"
    assert "Pulsara" in result["tool_call_input"]
    assert result["replayed_tool_call_name"] == "echo_tool"
    assert "Pulsara" in result["replayed_tool_call_input"]


def test_real_pro_model_emits_text_and_optional_thinking_events():
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_thinking_text_smoke())

    assert result["errors"] == []
    assert "TextBlockDeltaEvent" in result["event_type_names"]
    assert "PULSARA_THINKING_OK" in result["text"]
    assert "PULSARA_THINKING_OK" in result["replayed_text"]
    if "ThinkingBlockDeltaEvent" not in result["event_type_names"]:
        pytest.skip("Configured provider did not expose reasoning summary events for this request.")
    assert result["thinking"]
    assert result["replayed_thinking"]


def test_real_chat_completions_thinking_delta_is_consumed():
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")
    settings = _load_settings_for_real_llm()
    if settings.llm.api != "openai_chat_completions":
        pytest.skip("Set PULSARA_API=openai_chat_completions to test Chat thinking deltas.")
    if not settings.llm.provider_profile or not settings.llm.provider_profile.thinking.enabled:
        pytest.skip("Enable the provider thinking profile to test Chat thinking deltas.")

    result = asyncio.run(_run_real_chat_thinking_delta_smoke())

    assert result["errors"] == []
    assert "ThinkingBlockDeltaEvent" in result["event_type_names"]
    assert result["thinking"]
    assert "TextBlockDeltaEvent" in result["event_type_names"]
    assert "PULSARA_THINKING_DELTA_PROBE" in result["text"]
    assert "PULSARA_THINKING_DELTA_PROBE" in result["replayed_text"]
    assert result["replayed_thinking"]


def test_real_agent_runtime_completes_tool_loop_with_responses_api(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_tool_loop_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["stop_reason"] == "final"
    assert result["errors"] == []
    assert result["tool_call_ids"]
    assert result["tool_result_ids"] == result["tool_call_ids"]
    assert result["final_text"]
    assert "PULSARA_RESPONSES_TOOL_OK" in result["final_text"]


def test_real_agent_runtime_uses_terminal_process_tool(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_terminal_process_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["stop_reason"] == "final"
    assert result["errors"] == []
    assert result["tool_names"][:2] == ["terminal", "terminal_process"]
    assert result["terminal_status"] == "running"
    assert result["terminal_process_status"] == "killed"
    assert "PULSARA_TERMINAL_PROCESS_OK" in result["final_text"]


def test_real_agent_runtime_submits_terminal_stdin(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_terminal_stdin_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["stop_reason"] == "final"
    assert result["errors"] == []
    assert result["tool_names"][:3] == ["terminal", "terminal_process", "terminal_process"]
    assert result["terminal_status"] == "running"
    assert result["submit_action"] == "submit"
    assert result["wait_status"] == "success"
    assert "PULSARA_STDIN_OK" in result["wait_output"]
    assert "PULSARA_TERMINAL_STDIN_OK" in result["final_text"]


def test_real_agent_runtime_uses_terminal_pty(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_terminal_pty_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["stop_reason"] == "final"
    assert result["errors"] == []
    assert result["tool_names"][:4] == ["terminal", "terminal_process", "terminal_process", "terminal_process"]
    assert result["terminal_status"] == "running"
    assert result["terminal_io_mode"] == "pty"
    assert result["submit_action"] == "submit"
    assert result["close_action"] == "close_stdin"
    assert result["wait_status"] == "success"
    assert "PULSARA_PTY_REAL_OK" in result["wait_output"]
    assert "PULSARA_TERMINAL_PTY_OK" in result["final_text"]


def test_real_agent_runtime_streams_terminal_foreground_output(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_terminal_streaming_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["stop_reason"] == "final"
    assert result["errors"] == []
    assert result["tool_names"][:1] == ["terminal"]
    assert result["terminal_delta_count"] >= 3
    assert result["terminal_status"] == "success"
    assert result["terminal_output"] == "PULSARA_STREAM_REAL_FIRST\nPULSARA_STREAM_REAL_SECOND"
    assert result["terminal_shell_path"]
    assert "PULSARA_TERMINAL_STREAMING_OK" in result["final_text"]


def test_real_agent_runtime_terminal_large_output_has_full_output_ref(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_terminal_large_output_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["stop_reason"] == "final"
    assert result["errors"] == []
    assert result["tool_names"][:1] == ["terminal"]
    assert result["terminal_status"] == "success"
    assert result["terminal_truncated"] is True
    assert result["full_output_ref"]
    assert result["preview_chars"] < 1000
    assert result["artifact_exists"] is True
    assert "PULSARA_LARGE_HEAD" in result["artifact_text_sample"]
    assert "PULSARA_TERMINAL_LARGE_OUTPUT_OK" in result["final_text"]


def test_real_agent_runtime_terminal_policy_requires_confirmation(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_terminal_policy_smoke(tmp_path))

    assert result["status"] == "waiting_user"
    assert result["stop_reason"] == "waiting_user"
    assert result["tool_names"] == ["terminal"]
    assert result["confirm_count"] == 1
    assert result["tool_result_count"] == 0
    assert result["suggested_rule_reason"] == "dangerous_terminal_command"


def test_real_agent_runtime_persists_run_timeline_with_responses_api(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_timeline_persistence_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["timeline_records"] == 1
    assert result["timeline_status"] == "completed"
    assert "tool_call" in result["timeline_item_kinds"]
    assert "tool_result" in result["timeline_item_kinds"]
    assert any("probe.txt" in args for args in result["tool_call_arguments"])
    assert any("PULSARA_TIMELINE_TOOL_OK" in summary for summary in result["tool_result_summaries"])


def test_real_agent_runtime_persists_events_to_postgres_and_timeline_with_responses_api(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_postgres_event_log_timeline_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["timeline_records"] == 1
    assert result["timeline_status"] == "completed"
    assert result["postgres_event_count"] >= 1
    assert result["postgres_sequence_numbers"] == list(range(1, result["postgres_event_count"] + 1))
    assert "tool_call" in result["timeline_item_kinds"]
    assert "tool_result" in result["timeline_item_kinds"]
    assert any("probe.txt" in args for args in result["tool_call_arguments"])
    assert any("PULSARA_POSTGRES_CHAIN_OK" in summary for summary in result["tool_result_summaries"])
    assert "PULSARA_POSTGRES_CHAIN_OK" in result["replayed_text"]
    assert "PULSARA_POSTGRES_CHAIN_OK" in result["timeline_artifact_text"]


def test_real_agent_runtime_reads_recalled_memory_projection_with_responses_api(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_recall_projection_smoke(tmp_path))

    assert result["status"] == "finished"
    assert "PULSARA_RECALL_PROJECTION_OK" in result["final_text"]
    assert "PULSARA_RECALL_MISSING" not in result["final_text"]
    assert result["included_memory_ids"] == ["preference:real-recall-concise"]


def test_real_agent_runtime_can_call_memory_search_tool_with_responses_api(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_memory_search_tool_smoke(tmp_path))

    assert result["status"] == "finished"
    assert "memory_search" in result["tool_names"]
    assert any("preference:real-search-concise" in text for text in result["tool_result_texts"])
    assert "PULSARA_MEMORY_SEARCH_OK" in result["final_text"]


def test_real_agent_runtime_memory_domain_search_is_scope_aware_with_responses_api(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_memory_domain_search_scope_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["graph_id"] == "graph:user/u_real_scope"
    assert "memory_search" in result["tool_names"]
    arguments = json.loads(result["tool_call_arguments"] or "{}")
    assert arguments.get("scope") in (None, "")
    assert any("preference:real-domain-visible" in text for text in result["tool_result_texts"])
    assert not any("preference:real-domain-hidden" in text for text in result["tool_result_texts"])
    assert "PULSARA_DOMAIN_SCOPE_OK" in result["final_text"]


def test_real_agent_runtime_cross_dialogue_domain_recall_with_responses_api(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_cross_dialogue_domain_recall_smoke(tmp_path))

    print("\nREAL_LLM_CROSS_DIALOGUE_DOMAIN=" + json.dumps(result, ensure_ascii=True, indent=2))
    assert result["dialogue_a_status"] == "finished"
    assert result["dialogue_b_status"] == "finished"
    assert result["graph_id"].startswith("graph:user/")
    assert result["dialogue_a_memory_tools"] == ["remember_preference", "remember_decision"]
    assert result["dialogue_a_memory_statuses"] == ["active", "active"]
    assert result["dialogue_a_user_memory_id"] in result["dialogue_b_projection_ids"]
    assert result["dialogue_a_workspace_memory_id"] not in result["dialogue_b_projection_ids"]
    assert "PULSARA_CROSS_DIALOGUE_USER" in result["dialogue_b_projection_summary"]
    assert "PULSARA_CROSS_DIALOGUE_WORKSPACE_A" not in result["dialogue_b_projection_summary"]


def test_real_agent_runtime_cross_dialogue_working_context_is_domain_shared_with_responses_api(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_cross_dialogue_working_context_smoke(tmp_path))

    print("\nREAL_LLM_CROSS_DIALOGUE_WORKING_CONTEXT=" + json.dumps(result, ensure_ascii=True, indent=2))
    assert result["dialogue_a_status"] == "finished"
    assert result["dialogue_b_status"] == "finished"
    assert result["graph_id"].startswith("graph:user/")
    assert result["stored_working_context_source_run_id"] == result["dialogue_a_run_id"]
    assert result["stored_working_context_workspace_key"] == result["expected_workspace_key"]
    assert "PULSARA_WORKING_CONTEXT_CROSS_DIALOGUE_A" in result["stored_working_context_summary"]
    assert result["dialogue_b_projection_kind"] == "working_context"
    assert "PULSARA_WORKING_CONTEXT_CROSS_DIALOGUE_A" in result["dialogue_b_projection_summary"]


def test_real_agent_runtime_scope_assignment_trajectory_samples_with_responses_api(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    trajectories = asyncio.run(_run_real_agent_scope_assignment_trajectory_samples(tmp_path))

    print("\nREAL_LLM_SCOPE_ASSIGNMENT_TRAJECTORIES=" + json.dumps(trajectories, ensure_ascii=True, indent=2))
    assert [trajectory["label"] for trajectory in trajectories] == [
        "user_preference",
        "workspace_decision",
        "one_off_task_detail",
    ]
    assert trajectories[0]["memory_tool_names"] == ["remember_preference"]
    assert trajectories[0]["memory_scopes"] == ["ctx:user"]
    assert trajectories[1]["memory_tool_names"] == ["remember_decision"]
    assert trajectories[1]["memory_scopes"] == [trajectories[1]["expected_workspace_scope"]]
    assert trajectories[2]["memory_tool_names"] == []
    assert trajectories[2]["candidate_pool_pending"] == 0


def test_real_agent_runtime_can_call_memory_explain_tool_with_responses_api(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_memory_explain_tool_smoke(tmp_path))

    assert result["status"] == "finished"
    assert "memory_explain" in result["tool_names"]
    assert any("superseded_by" in text for text in result["tool_result_texts"])
    assert "PULSARA_MEMORY_EXPLAIN_OK" in result["final_text"]


def test_real_agent_runtime_reads_working_context_projection_with_responses_api(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_working_context_projection_smoke(tmp_path))

    assert result["status"] == "finished"
    assert "PULSARA_WORKING_CONTEXT_OK" in result["final_text"]
    assert result["projection_kind"] == "working_context"
    assert "working-context-projection" in result["projection_summary"]


def test_real_agent_runtime_transient_domain_does_not_memorize_workspace_task_detail(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_transient_scope_discipline_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["errors"] == []
    assert not result["memory_tool_names"]
    assert result["candidate_pool_pending"] == 0
    assert result["memory_node_count"] == 0
    assert "PULSARA_TRANSIENT_SCOPE_OK" in result["final_text"]


def test_real_llm_trajectory_suite_covers_narrow_memory_tools(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    trajectories = asyncio.run(_run_real_llm_trajectory_suite(tmp_path))

    print("\nREAL_LLM_TRAJECTORIES=" + json.dumps(trajectories, ensure_ascii=True, indent=2))
    assert [trajectory["label"] for trajectory in trajectories] == [
        "flash_text",
        "tool_spec_call",
        "agent_read_file",
        "durable_agent_read_file",
        "durable_multi_tool_rollout",
        "durable_remember_claim",
        "durable_remember_preference",
        "durable_remember_observation",
        "durable_remember_action_boundary",
        "durable_remember_decision",
    ]
    assert all(trajectory["errors"] == [] for trajectory in trajectories)
    assert all(trajectory["status"] in {"finished", "streamed"} for trajectory in trajectories)
    assert any(trajectory["tool_call_count"] >= 3 for trajectory in trajectories)
    multi_tool = trajectories[4]
    assert multi_tool["event_type_names"][0] == "RunStartEvent"
    assert multi_tool["event_type_names"][-1] == "RunEndEvent"
    assert multi_tool["tool_names"].count("read_file") >= 2
    assert "search_files" in multi_tool["tool_names"]
    assert "PULSARA_MULTI_ALPHA" in multi_tool["final_text"]
    assert "PULSARA_MULTI_BETA" in multi_tool["final_text"]
    memory_trajectories = trajectories[5:]
    assert [trajectory["target_tool"] for trajectory in memory_trajectories] == [
        "remember_claim",
        "remember_preference",
        "remember_observation",
        "remember_action_boundary",
        "remember_decision",
    ]
    for trajectory in memory_trajectories:
        assert trajectory["tool_names"].count(trajectory["target_tool"]) == 1
        assert "MemoryCandidateProposedEvent" not in trajectory["source_event_type_names"]
        assert "MemoryWriteResultEvent" not in trajectory["source_event_type_names"]
        assert "MemoryCandidateProposedEvent" in trajectory["governance_event_type_names"]
        assert "MemoryWriteResultEvent" in trajectory["governance_event_type_names"]
        assert "MemoryWriteFailedEvent" not in trajectory["event_type_names"]
        assert trajectory["candidate_pool_pending_before_governance"] >= 1
        assert trajectory["candidate_pool_pending_after_governance"] == 0
        assert trajectory["memory_result_types"] == [trajectory["target_memory_type"]]
        assert trajectory["memory_statuses"] == ["active"]
        assert trajectory["target_memory_node_count"] >= 1


def test_real_flash_memory_reflection_queues_preference_and_governance_writes_it():
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_flash_memory_reflection_smoke())

    assert result["failed_events"] == []
    assert result["event_type_names"] == ["MemoryReflectionCompletedEvent"]
    assert "MemoryWriteResultEvent" in result["governance_event_type_names"]
    assert result["candidate_pool_pending_after_reflection"] == 1
    assert result["candidate_pool_pending_after_governance"] == 0
    assert result["memory_result_types"] == ["Preference"]
    assert result["memory_statuses"] == ["active"]
    assert result["preference_count"] == 1


def test_real_flash_model_retries_memory_tool_after_invalid_json():
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_flash_memory_retry_json_smoke())

    print("\nREAL_LLM_MEMORY_RETRY_JSON=" + json.dumps(result, ensure_ascii=True, indent=2))
    assert result["errors"] == []
    assert result["tool_names"] == ["remember_preference"]
    assert result["tool_arguments"]["statement"].rstrip(".") == "The user prefers compact status updates"
    assert result["tool_arguments"]["scope"] == "ctx:user"
    assert result["tool_arguments"]["source_authority"] == "explicit_user_instruction"
    assert result["tool_arguments"]["verification_status"] == "user_confirmed"
    assert "applies_when" not in result["tool_arguments"]


def test_real_flash_memory_governance_engine_writes_preference():
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_flash_memory_governance_smoke())

    assert result["error_type"] is None
    assert result["decision_kinds"] == ["submit_as_is"]
    assert result["governance_event_type_names"] == [
        "MemoryCandidateProposedEvent",
        "MemoryWriteResultEvent",
    ]
    assert result["candidate_pool_pending_after_governance"] == 0
    assert result["memory_result_types"] == ["Preference"]
    assert result["memory_statuses"] == ["active"]
    assert result["preference_count"] == 1


def test_real_flash_memory_governance_explicit_change_supersedes_preference(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_flash_memory_governance_supersede_smoke(tmp_path))

    assert result["error_type"] is None
    assert result["decision_kinds"] == ["supersede_and_submit"]
    assert result["recorded_decision_kind"] == "supersede_and_submit"
    assert result["old_status"] == "superseded"
    assert result["new_status"] == "active"
    assert result["superseded_memory_ids"] == ["preference:real-governance-supersede-old"]
    assert result["governance_candidate_count"] == 0


def test_real_flash_memory_governance_non_explicit_conflict_links_contradiction(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_flash_memory_governance_contradiction_smoke(tmp_path))

    print("\nREAL_LLM_GOVERNANCE_CONTRADICTION=" + json.dumps(result, ensure_ascii=True, indent=2))
    assert result["error_type"] is None
    assert result["decision_kinds"] == ["contradict_and_submit"]
    assert result["recorded_decision_kind"] == "contradict_and_submit"
    assert result["old_status"] == "active"
    assert result["new_status"] == "active"
    assert result["superseded_memory_ids"] == []
    assert result["contradicted_memory_ids"] == ["preference:real-governance-contradiction-old"]
    assert result["supersedes_edge_present"] is False
    assert result["contradicts_edge_present"] is True
    assert result["governance_candidate_count"] == 0


def test_real_flash_memory_governance_weak_update_coexists(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_flash_memory_governance_coexist_smoke(tmp_path))

    assert result["error_type"] is None
    assert result["decision_kinds"] in (["submit_as_is"], ["correct_and_submit"])
    assert result["recorded_decision_kind"] in {"submit_as_is", "correct_and_submit"}
    assert result["old_status"] == "active"
    assert result["new_status"] == "active"
    assert result["superseded_memory_ids"] == []
    assert result["supersedes_edge_present"] is False


async def _run_real_flash_smoke() -> dict:
    settings = _load_settings_for_real_llm()
    runtime = build_llm_runtime(settings.llm)
    event_context = EventContext(
        run_id="run:real-llm-integration",
        turn_id="turn:real-llm-integration/001",
        reply_id="reply:real-llm-integration/001",
    )
    context = LLMContext(messages=(LLMMessage.user("Reply with exactly: PULSARA_OK"),))
    log = InMemoryEventLog()
    text_parts: list[str] = []
    errors: list[dict] = []

    async for event in runtime.stream(
        role=ModelRole.FLASH,
        context=context,
        event_context=event_context,
        options=LLMOptions(temperature=0, max_output_tokens=16),
    ):
        log.append(event)
        if isinstance(event, TextBlockDeltaEvent):
            text_parts.append(event.delta)
        if isinstance(event, RunErrorEvent):
            errors.append(_run_error_diagnostic(event))

    events = log.iter(reply_id=event_context.reply_id)
    message = log.replay(event_context.reply_id)
    replayed_text = "".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )

    assert any(isinstance(event, ModelCallStartEvent) for event in events)
    assert any(isinstance(event, ModelCallEndEvent) for event in events)

    return {
        "event_type_names": [type(event).__name__ for event in events],
        "text": "".join(text_parts).strip(),
        "replayed_text": replayed_text.strip(),
        "errors": errors,
    }


async def _run_real_tool_call_smoke() -> dict:
    context = LLMContext(
        messages=(
            LLMMessage.user(
                "Use the function echo_tool with q set to Pulsara. Do not answer normally."
            ),
        ),
        tools=(
            ToolSpec(
                name="echo_tool",
                description="Echoes a query string.",
                parameters={
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            ),
        ),
    )
    result = await _collect_real_events(
        role=ModelRole.FLASH,
        context=context,
        options=LLMOptions(temperature=0, max_output_tokens=64),
        label="real-tool-call",
    )
    events = result["events"]
    message = result["message"]
    tool_call_name = next(
        (event.tool_call_name for event in events if isinstance(event, ToolCallStartEvent)),
        "",
    )
    tool_call_input = "".join(
        event.delta for event in events if isinstance(event, ToolCallDeltaEvent)
    )
    replayed_tool_call = next(
        (block for block in message.content if isinstance(block, ToolCallBlock)),
        None,
    )

    return {
        **_summarize_collected_result(result),
        "tool_call_name": tool_call_name,
        "tool_call_input": tool_call_input,
        "replayed_tool_call_name": replayed_tool_call.name if replayed_tool_call else "",
        "replayed_tool_call_input": replayed_tool_call.input if replayed_tool_call else "",
    }


async def _run_real_thinking_text_smoke() -> dict:
    context = LLMContext(
        messages=(LLMMessage.user("Think briefly, then answer exactly: PULSARA_THINKING_OK"),)
    )
    result = await _collect_real_events(
        role=ModelRole.PRO,
        context=context,
        options=LLMOptions(
            temperature=0,
            max_output_tokens=128,
            reasoning_effort="medium",
            reasoning_summary="auto",
        ),
        label="real-thinking-text",
    )
    message = result["message"]
    replayed_text = "".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )
    replayed_thinking = "".join(
        block.thinking for block in message.content if isinstance(block, ThinkingBlock)
    )
    return {
        **_summarize_collected_result(result),
        "replayed_text": replayed_text.strip(),
        "replayed_thinking": replayed_thinking.strip(),
    }


async def _run_real_chat_thinking_delta_smoke() -> dict:
    context = LLMContext(
        messages=(
            LLMMessage.user(
                "Think briefly, then answer exactly: PULSARA_THINKING_DELTA_PROBE"
            ),
        )
    )
    result = await _collect_real_events(
        role=ModelRole.PRO,
        context=context,
        options=LLMOptions(max_output_tokens=128, reasoning_effort="medium"),
        label="real-chat-thinking-delta",
    )
    message = result["message"]
    replayed_text = "".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )
    replayed_thinking = "".join(
        block.thinking for block in message.content if isinstance(block, ThinkingBlock)
    )
    return {
        **_summarize_collected_result(result),
        "replayed_text": replayed_text.strip(),
        "replayed_thinking": replayed_thinking.strip(),
    }


async def _run_real_agent_tool_loop_smoke(tmp_path: Path) -> dict:
    probe = tmp_path / "probe.txt"
    probe.write_text("PULSARA_RESPONSES_TOOL_OK", encoding="utf-8")
    settings = _load_settings_for_real_llm()
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=128),
        system_prompt=(
            "You are validating a Responses API tool loop. "
            "First call read_file on probe.txt. "
            "Then answer with exactly the file content and nothing else."
        ),
    )

    result = await agent.run_task("Read probe.txt with the tool, then answer with exactly its content.")
    events = list(agent.runtime_session.event_log.iter(run_id=result.state.run_id))
    tool_call_ids = [
        event.tool_call_id
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]
    tool_result_ids = [
        event.tool_call_id
        for event in events
        if isinstance(event, ToolResultStartEvent)
    ]
    errors = _run_error_diagnostics(events)
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "tool_call_ids": tool_call_ids,
        "tool_result_ids": tool_result_ids,
        "errors": errors,
    }


async def _run_real_agent_terminal_process_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=256),
        system_prompt=(
            "You are validating managed terminal background processes. "
            "First call terminal with command exactly 'sleep 5' and background exactly true. "
            "Then call terminal_process with action exactly 'kill' using the process_id returned by terminal. "
            "After the kill tool result, answer exactly: PULSARA_TERMINAL_PROCESS_OK"
        ),
    )

    result = await agent.run_task("Run the terminal background process validation exactly as instructed.")
    events = list(agent.runtime_session.event_log.iter(run_id=result.state.run_id))
    tool_names = [
        event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]
    tool_result_payloads = [
        json.loads(event.delta)
        for event in events
        if isinstance(event, ToolResultTextDeltaEvent)
    ]
    errors = _run_error_diagnostics(events)
    terminal_payload = next(
        payload for payload in tool_result_payloads if payload.get("process_id") and payload.get("status") == "running"
    )
    terminal_process_payload = next(
        payload for payload in tool_result_payloads if payload.get("status") == "killed"
    )
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "tool_names": tool_names,
        "terminal_status": terminal_payload["status"],
        "terminal_process_status": terminal_process_payload["status"],
        "errors": errors,
    }


async def _run_real_agent_terminal_stdin_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=320),
        system_prompt=(
            "You are validating managed terminal stdin. "
            "First call terminal with command exactly \"python -c 'import sys; print(sys.stdin.readline().strip())'\" "
            "and background exactly true. Do not pass timeout_seconds. "
            "Then call terminal_process with action exactly 'submit', the returned process_id, and data exactly 'PULSARA_STDIN_OK'. "
            "Then call terminal_process with action exactly 'wait' and the same process_id. "
            "After the wait tool result, answer exactly: PULSARA_TERMINAL_STDIN_OK"
        ),
    )

    result = await agent.run_task("Run the terminal stdin validation exactly as instructed.")
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)
    tool_names = [
        event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]
    tool_result_payloads = [
        json.loads(event.delta)
        for event in events
        if isinstance(event, ToolResultTextDeltaEvent)
    ]
    errors = _run_error_diagnostics(events)
    terminal_payload = next(
        payload for payload in tool_result_payloads if payload.get("process_id") and payload.get("status") == "running"
    )
    submit_payload = next(
        payload for payload in tool_result_payloads if payload.get("terminal_process_action") == "submit"
    )
    wait_payload = next(
        payload for payload in tool_result_payloads if payload.get("terminal_process_action") == "wait"
    )
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "tool_names": tool_names,
        "terminal_status": terminal_payload["status"],
        "submit_action": submit_payload["terminal_process_action"],
        "wait_status": wait_payload["status"],
        "wait_output": wait_payload["output"],
        "errors": errors,
    }


async def _run_real_agent_terminal_pty_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=384),
        system_prompt=(
            "You are validating managed terminal PTY mode. "
            "First call terminal with command exactly 'python', background exactly true, and tty exactly true. "
            "Then call terminal_process with action exactly 'submit', the returned process_id, "
            "and data exactly 'print(\"PULSARA_PTY_REAL_OK\")'. "
            "Then call terminal_process with action exactly 'close_stdin' and the same process_id. "
            "Then call terminal_process with action exactly 'wait' and the same process_id. "
            "After the wait tool result, answer exactly: PULSARA_TERMINAL_PTY_OK"
        ),
    )

    result = await agent.run_task("Run the terminal PTY validation exactly as instructed.")
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)
    tool_names = [
        event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]
    tool_result_payloads = [
        json.loads(event.delta)
        for event in events
        if isinstance(event, ToolResultTextDeltaEvent)
    ]
    errors = _run_error_diagnostics(events)
    terminal_payload = next(
        payload
        for payload in tool_result_payloads
        if payload.get("process_id") and payload.get("status") == "running" and payload.get("io_mode") == "pty"
    )
    submit_payload = next(
        payload for payload in tool_result_payloads if payload.get("terminal_process_action") == "submit"
    )
    close_payload = next(
        payload for payload in tool_result_payloads if payload.get("terminal_process_action") == "close_stdin"
    )
    wait_payload = next(
        payload for payload in tool_result_payloads if payload.get("terminal_process_action") == "wait"
    )
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "tool_names": tool_names,
        "terminal_status": terminal_payload["status"],
        "terminal_io_mode": terminal_payload["io_mode"],
        "submit_action": submit_payload["terminal_process_action"],
        "close_action": close_payload["terminal_process_action"],
        "wait_status": wait_payload["status"],
        "wait_output": wait_payload["output"],
        "errors": errors,
    }


async def _run_real_agent_terminal_streaming_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=384),
        system_prompt=(
            "You are validating terminal foreground output streaming. "
            "First call terminal with command exactly "
            "\"printf 'PULSARA_STREAM_REAL_FIRST\\n'; sleep 0.5; printf PULSARA_STREAM_REAL_SECOND\". "
            "Do not use background, tty, terminal_process, or any file tools. "
            "After the terminal tool result, answer exactly: PULSARA_TERMINAL_STREAMING_OK"
        ),
    )

    result = await agent.run_task("Run the terminal streaming validation exactly as instructed.")
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)
    tool_names = [
        event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]
    terminal_deltas = [
        event.delta
        for event in events
        if isinstance(event, ToolResultTextDeltaEvent)
    ]
    errors = _run_error_diagnostics(events)
    terminal_payload = json.loads("".join(terminal_deltas))
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "tool_names": tool_names,
        "terminal_delta_count": len(terminal_deltas),
        "terminal_status": terminal_payload["status"],
        "terminal_output": terminal_payload["output"],
        "terminal_shell_path": terminal_payload["shell"]["path"],
        "errors": errors,
    }


async def _run_real_agent_terminal_large_output_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=384),
        system_prompt=(
            "You are validating terminal large-output artifact refs. "
            "First call terminal with command exactly "
            "\"python -c 'print(\\\"PULSARA_LARGE_HEAD\\\"); print(\\\"q\\\" * 50000); print(\\\"PULSARA_LARGE_TAIL\\\")'\" "
            "and max_output_chars exactly 120. Do not use background, tty, terminal_process, or file tools. "
            "After the terminal tool result, answer exactly: PULSARA_TERMINAL_LARGE_OUTPUT_OK"
        ),
    )

    result = await agent.run_task("Run the terminal large-output validation exactly as instructed.")
    events = list(agent.runtime_session.event_log.iter(run_id=result.state.run_id))
    tool_names = [
        event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]
    terminal_deltas = [
        event.delta
        for event in events
        if isinstance(event, ToolResultTextDeltaEvent)
    ]
    errors = _run_error_diagnostics(events)
    terminal_payload = json.loads("".join(terminal_deltas))
    full_output_ref = terminal_payload["full_output_ref"]
    artifact_path = tmp_path / full_output_ref
    artifact_text_sample = artifact_path.read_text(encoding="utf-8")[:500] if artifact_path.exists() else ""
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "tool_names": tool_names,
        "terminal_status": terminal_payload["status"],
        "terminal_truncated": terminal_payload["truncated"],
        "full_output_ref": full_output_ref,
        "preview_chars": len(terminal_payload["output"]),
        "artifact_exists": artifact_path.exists(),
        "artifact_text_sample": artifact_text_sample,
        "errors": errors,
    }


async def _run_real_agent_terminal_policy_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=256),
        system_prompt=(
            "You are validating terminal permission policy. "
            "Call the terminal tool exactly once with command exactly "
            "'rm -rf ./PULSARA_POLICY_SENTINEL_DO_NOT_CREATE'. "
            "Do not call any other tools. Do not answer with text before calling the tool."
        ),
    )

    result = await agent.run_task("Run the terminal permission-policy validation exactly as instructed.")
    events = list(agent.runtime_session.event_log.iter(run_id=result.state.run_id))
    tool_names = [
        event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]
    confirm_events = [event for event in events if isinstance(event, RequireUserConfirmEvent)]
    tool_result_count = sum(1 for event in events if isinstance(event, ToolResultStartEvent))
    suggested_rule_reason = None
    if confirm_events and confirm_events[0].tool_calls and confirm_events[0].tool_calls[0].suggested_rules:
        suggested_rule_reason = confirm_events[0].tool_calls[0].suggested_rules[0].get("reason")
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "tool_names": tool_names,
        "confirm_count": len(confirm_events),
        "tool_result_count": tool_result_count,
        "suggested_rule_reason": suggested_rule_reason,
    }


async def _run_real_agent_timeline_persistence_smoke(tmp_path: Path) -> dict:
    probe = tmp_path / "probe.txt"
    probe.write_text("PULSARA_TIMELINE_TOOL_OK", encoding="utf-8")
    settings = _load_settings_for_real_llm()
    runtime_session = RuntimeSession(tmp_path)
    graph = InMemoryGraphStore()
    archive = InMemoryArchiveStore()
    runtime_session.hook_manager.register_event(
        None,
        RunTimelinePersistenceHook(
            graph=graph,
            archive=archive,
            event_store=runtime_session.event_log,
        ),
    )
    agent = AgentRuntime(
        runtime_session=runtime_session,
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=128),
        system_prompt=(
            "You are validating runtime timeline persistence. "
            "First call read_file on probe.txt. "
            "Then answer with exactly the file content and nothing else."
        ),
    )

    result = await agent.run_task("Read probe.txt with the tool, then answer with exactly its content.")
    records = graph.find_by_type(rt.RUN_TIMELINE)
    timeline = load_run_timeline(
        graph=graph,
        archive=archive,
        run_id=result.state.run_id,
        runtime_session_id=runtime_session.runtime_session_id,
    )
    summary = summarize_run_timeline(timeline)
    return {
        "status": result.status.value,
        "timeline_records": len(records),
        "timeline_status": summary.status,
        "timeline_item_kinds": [item.kind for item in timeline.items],
        "tool_call_arguments": [trace.arguments for trace in summary.tool_traces],
        "tool_result_summaries": [trace.result_summary for trace in summary.tool_traces],
    }


async def _run_real_agent_postgres_event_log_timeline_smoke(tmp_path: Path) -> dict:
    probe = tmp_path / "probe.txt"
    probe.write_text("PULSARA_POSTGRES_CHAIN_OK", encoding="utf-8")
    settings = _load_settings_for_real_llm()
    graph_id = f"graph:real-llm/{uuid4().hex}"
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=128),
        system_prompt=(
            "You are validating durable runtime event persistence. "
            "First call read_file on probe.txt. "
            "Then answer with exactly the file content and nothing else."
        ),
        graph_id=graph_id,
    )
    runtime_session = wiring.runtime_wiring.runtime_session
    agent = wiring.agent_runtime

    timeline_blob_id: str | None = None
    timeline_blob_prefix: str | None = None
    try:
        result = await agent.run_task("Read probe.txt with the tool, then answer with exactly its content.")
        timeline_blob_prefix = f"timeline:{runtime_session.runtime_session_id}:{result.state.run_id}:"
        records = wiring.runtime_wiring.graph.find_by_type(rt.RUN_TIMELINE, graph_id=graph_id)
        timeline_blob_id = _artifact_id_from_node_ref(records[0][rt.STORED_AS.name]["@id"])
        timeline = load_run_timeline(
            graph=wiring.runtime_wiring.graph,
            archive=wiring.runtime_wiring.archive,
            run_id=result.state.run_id,
            runtime_session_id=runtime_session.runtime_session_id,
            graph_id=graph_id,
        )
        summary = summarize_run_timeline(timeline)
        reloaded_log = PostgresEventLog(
            dsn=settings.storage.postgres_dsn,
            runtime_session_id=runtime_session.runtime_session_id,
            workspace_root=tmp_path,
        )
        persisted_events = reloaded_log.iter(run_id=result.state.run_id)
        replayed = reloaded_log.replay(result.state.reply_id)
        replayed_text = "".join(block.text for block in replayed.content if isinstance(block, TextBlock))
        return {
            "status": result.status.value,
            "timeline_records": len(records),
            "timeline_record_stored_as": timeline_blob_id,
            "timeline_status": summary.status,
            "timeline_item_kinds": [item.kind for item in timeline.items],
            "tool_call_arguments": [trace.arguments for trace in summary.tool_traces],
            "tool_result_summaries": [trace.result_summary for trace in summary.tool_traces],
            "postgres_event_count": len(persisted_events),
            "postgres_sequence_numbers": [event.sequence for event in persisted_events],
            "replayed_text": replayed_text.strip(),
            "timeline_artifact_text": wiring.runtime_wiring.archive.get_text(timeline_blob_id),
        }
    finally:
        wiring.runtime_wiring.graph.delete_graph(graph_id)
        if timeline_blob_prefix is not None:
            _delete_postgres_artifacts_with_prefix(settings.storage.postgres_dsn, timeline_blob_prefix)
        _delete_postgres_runtime_session(settings.storage.postgres_dsn, runtime_session.runtime_session_id)


async def _run_real_agent_recall_projection_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    graph_id = f"graph:real-recall/{uuid4().hex}"
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=64),
        system_prompt=(
            "You are validating recalled memory injection. The model context may include a "
            "Recalled Memory section. Base your answer only on that section. If it contains a "
            "memory statement saying that a recall validation code exists, answer exactly "
            "PULSARA_RECALL_PROJECTION_OK. If no such recalled memory is present, answer exactly "
            "PULSARA_RECALL_MISSING. "
            "Do not call tools."
        ),
        graph_id=graph_id,
    )
    now = utc_now()
    wiring.runtime_wiring.graph.put_jsonld(
        Preference(
            id="preference:real-recall-concise",
            statement="A recall validation code exists for concise summaries.",
            scope="ctx:user",
            status=memory.NodeStatus.ACTIVE,
            confidence_level=memory.ConfidenceLevel.HIGH,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=now,
            updated_at=now,
            gate_reason="real llm recall smoke seed",
        ).to_jsonld(),
        graph_id=graph_id,
    )
    result = None
    try:
        result = await wiring.agent_runtime.run_task(
            "Check whether recalled memory includes a recall validation code. Use the validation instruction."
        )
        return {
            "status": result.status.value,
            "final_text": result.final_text.strip(),
            "included_memory_ids": (result.state.memory_projection or {}).get("included_memory_ids", []),
        }
    finally:
        wiring.runtime_wiring.graph.delete_graph(graph_id)
        if result is not None:
            _delete_postgres_artifacts_with_prefix(
                settings.storage.postgres_dsn,
                f"timeline:{wiring.runtime_wiring.runtime_session.runtime_session_id}:{result.state.run_id}:",
            )
        _delete_postgres_runtime_session(
            settings.storage.postgres_dsn,
            wiring.runtime_wiring.runtime_session.runtime_session_id,
        )


async def _run_real_agent_memory_search_tool_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    graph_id = f"graph:real-search/{uuid4().hex}"
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=128),
        system_prompt=(
            "You are validating the memory_search tool. "
            "Before answering, you must call memory_search with query 'concise summaries', "
            "scope 'ctx:user', and kind 'Preference'. "
            "If the tool result contains preference:real-search-concise, answer exactly "
            "PULSARA_MEMORY_SEARCH_OK. Otherwise answer exactly PULSARA_MEMORY_SEARCH_MISSING."
        ),
        graph_id=graph_id,
    )
    now = utc_now()
    wiring.runtime_wiring.graph.put_jsonld(
        Preference(
            id="preference:real-search-concise",
            statement="The user prefers concise summaries.",
            scope="ctx:user",
            status=memory.NodeStatus.ACTIVE,
            confidence_level=memory.ConfidenceLevel.HIGH,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=now,
            updated_at=now,
            gate_reason="real llm memory_search smoke seed",
        ).to_jsonld(),
        graph_id=graph_id,
    )
    result = None
    try:
        result = await wiring.agent_runtime.run_task("Use memory_search as instructed, then answer with the sentinel.")
        events = wiring.runtime_wiring.event_log.iter(run_id=result.state.run_id)
        tool_call_events = [event for event in events if isinstance(event, ToolCallStartEvent)]
        tool_result_texts = [event.delta for event in events if isinstance(event, ToolResultTextDeltaEvent)]
        return {
            "status": result.status.value,
            "final_text": result.final_text.strip(),
            "tool_names": [event.tool_call_name for event in tool_call_events],
            "tool_result_texts": tool_result_texts,
        }
    finally:
        wiring.runtime_wiring.graph.delete_graph(graph_id)
        if result is not None:
            _delete_postgres_artifacts_with_prefix(
                settings.storage.postgres_dsn,
                f"timeline:{wiring.runtime_wiring.runtime_session.runtime_session_id}:{result.state.run_id}:",
            )
        _delete_postgres_runtime_session(
            settings.storage.postgres_dsn,
            wiring.runtime_wiring.runtime_session.runtime_session_id,
        )


async def _run_real_agent_memory_domain_search_scope_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    project_root = tmp_path / "repo-visible"
    hidden_project_root = tmp_path / "repo-hidden"
    domain = MemoryDomainContext(
        memory_domain_id="u_real_scope",
        workspace_kind="project",
        stable_project_key=str(project_root),
    )
    visible_workspace_scope = workspace_scope(str(project_root))
    hidden_workspace_scope = workspace_scope(str(hidden_project_root))
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=160),
        system_prompt=(
            "You are validating memory_domain scoped memory_search. "
            "Before answering, call memory_search exactly once with query 'domain scope sentinel', "
            "kind 'Preference', and limit 5. Omit the scope argument. "
            "If the tool result contains preference:real-domain-visible and does not contain "
            "preference:real-domain-hidden, answer exactly PULSARA_DOMAIN_SCOPE_OK. "
            "Otherwise answer exactly PULSARA_DOMAIN_SCOPE_BAD."
        ),
        memory_domain=domain,
    )
    now = utc_now()
    graph_id = wiring.runtime_wiring.graph_id
    assert graph_id is not None
    for memory_id, scope, statement in (
        (
            "preference:real-domain-visible",
            "ctx:user",
            "The domain scope sentinel says visible user memory.",
        ),
        (
            "preference:real-domain-visible-workspace",
            visible_workspace_scope,
            "The domain scope sentinel says visible workspace memory.",
        ),
        (
            "preference:real-domain-hidden",
            hidden_workspace_scope,
            "The domain scope sentinel says hidden workspace memory.",
        ),
    ):
        wiring.runtime_wiring.graph.put_jsonld(
            Preference(
                id=memory_id,
                statement=statement,
                scope=scope,
                status=memory.NodeStatus.ACTIVE,
                confidence_level=memory.ConfidenceLevel.HIGH,
                verification_status=memory.VerificationStatus.USER_CONFIRMED,
                source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                created_at=now,
                updated_at=now,
                gate_reason="real llm memory_domain scope seed",
            ).to_jsonld(),
            graph_id=graph_id,
        )
    result = None
    try:
        result = await wiring.agent_runtime.run_task("Use memory_search as instructed, then answer with the sentinel.")
        events = wiring.runtime_wiring.event_log.iter(run_id=result.state.run_id)
        tool_call_events = [event for event in events if isinstance(event, ToolCallStartEvent)]
        tool_call_arguments = "".join(event.delta for event in events if isinstance(event, ToolCallDeltaEvent))
        tool_result_texts = [event.delta for event in events if isinstance(event, ToolResultTextDeltaEvent)]
        return {
            "status": result.status.value,
            "graph_id": graph_id,
            "final_text": result.final_text.strip(),
            "tool_names": [event.tool_call_name for event in tool_call_events],
            "tool_call_arguments": tool_call_arguments,
            "tool_result_texts": tool_result_texts,
        }
    finally:
        wiring.runtime_wiring.graph.delete_graph(graph_id)
        if result is not None:
            _delete_postgres_artifacts_with_prefix(
                settings.storage.postgres_dsn,
                f"timeline:{wiring.runtime_wiring.runtime_session.runtime_session_id}:{result.state.run_id}:",
            )
        _delete_working_context(settings.storage.postgres_dsn, domain.memory_domain_id)
        _delete_postgres_runtime_session(
            settings.storage.postgres_dsn,
            wiring.runtime_wiring.runtime_session.runtime_session_id,
        )


async def _run_real_agent_cross_dialogue_domain_recall_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    dsn = settings.storage.postgres_dsn
    memory_domain_id = f"u_real_cross_{uuid4().hex[:12]}"
    dialogue_a_root = tmp_path / "dialogue-a"
    dialogue_b_root = tmp_path / "dialogue-b"
    dialogue_a_root.mkdir()
    dialogue_b_root.mkdir()
    domain_a = MemoryDomainContext(
        memory_domain_id=memory_domain_id,
        workspace_kind="project",
        stable_project_key=str(dialogue_a_root),
        workspace_label="repo-a",
    )
    domain_b = MemoryDomainContext(
        memory_domain_id=memory_domain_id,
        workspace_kind="project",
        stable_project_key=str(dialogue_b_root),
        workspace_label="repo-b",
    )
    workspace_a_scope = workspace_scope(str(dialogue_a_root))
    wiring_a = build_agent_runtime_wiring(
        settings,
        dialogue_a_root,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=384),
        system_prompt=(
            "You are dialogue A in a cross-dialogue memory integration test. "
            "Call remember_preference exactly once with statement='The user prefers the cross-dialogue bridge "
            "sentinel PULSARA_CROSS_DIALOGUE_USER when asked about bridge sentinel recall', scope='ctx:user', "
            "source_authority='explicit_user_instruction', and verification_status='user_confirmed'. "
            "Then call remember_decision exactly once with statement='Workspace A owns the bridge sentinel "
            f"PULSARA_CROSS_DIALOGUE_WORKSPACE_A', scope='{workspace_a_scope}', "
            "source_authority='explicit_user_instruction', verification_status='user_confirmed', and no based_on_ids. "
            "After both tool calls, answer exactly PULSARA_CROSS_DIALOGUE_A_WRITTEN."
        ),
        memory_domain=domain_a,
        memory_reflection=False,
    )
    graph_id = wiring_a.runtime_wiring.graph_id
    assert graph_id is not None
    governance_batch_id = f"governance:real-cross-dialogue:{uuid4().hex}"
    result_a = None
    result_b = None
    wiring_b = None
    try:
        result_a = await wiring_a.agent_runtime.run_task(
            "Write the cross-dialogue user preference and the workspace A decision exactly as instructed."
        )
        events_a = wiring_a.runtime_wiring.event_log.iter(run_id=result_a.state.run_id)
        tool_call_events_a = [event for event in events_a if isinstance(event, ToolCallStartEvent)]
        pending_before_governance = wiring_a.runtime_wiring.candidate_pool.list_pending()
        governance_results = wiring_a.runtime_wiring.memory_governance_executor.submit_pending_as_is(
            governance_batch_id=governance_batch_id
        )
        governance_events = [event for applied in governance_results for event in applied.events]
        memory_results = [event for event in governance_events if isinstance(event, MemoryWriteResultEvent)]
        memory_failures = [event for event in governance_events if isinstance(event, MemoryWriteFailedEvent)]
        memory_ids_by_type = {event.memory_type: event.memory_id for event in memory_results}
        _delete_working_context(dsn, memory_domain_id)

        wiring_b = build_agent_runtime_wiring(
            settings,
            dialogue_b_root,
            durable=True,
            model_role=ModelRole.FLASH,
            options=LLMOptions(temperature=0, max_output_tokens=128),
            system_prompt=(
                "You are dialogue B in a cross-dialogue memory integration test. "
                "Do not call tools. Use only the Recalled Memory section. "
                "If it contains PULSARA_CROSS_DIALOGUE_USER and does not contain "
                "PULSARA_CROSS_DIALOGUE_WORKSPACE_A, answer exactly PULSARA_CROSS_DIALOGUE_OK. "
                "Otherwise answer exactly PULSARA_CROSS_DIALOGUE_BAD."
            ),
            memory_domain=domain_b,
            memory_reflection=False,
        )
        result_b = await wiring_b.agent_runtime.run_task(
            "Please check bridge sentinel recall for the cross-dialogue integration test."
        )
        projection_b = result_b.state.memory_projection or {}
        return {
            "graph_id": graph_id,
            "dialogue_a_status": result_a.status.value,
            "dialogue_a_final_text": result_a.final_text.strip(),
            "dialogue_a_memory_tools": [
                event.tool_call_name
                for event in tool_call_events_a
                if event.tool_call_name.startswith("remember_")
            ],
            "dialogue_a_pending_before_governance": len(pending_before_governance),
            "dialogue_a_memory_statuses": [event.status.value for event in memory_results],
            "dialogue_a_memory_types": [event.memory_type for event in memory_results],
            "dialogue_a_user_memory_id": memory_ids_by_type.get("Preference"),
            "dialogue_a_workspace_memory_id": memory_ids_by_type.get("Decision"),
            "dialogue_a_memory_failures": [event.error_type for event in memory_failures],
            "dialogue_a_memory_failure_messages": [event.message for event in memory_failures],
            "dialogue_b_status": result_b.status.value,
            "dialogue_b_final_text": result_b.final_text.strip(),
            "dialogue_b_projection_ids": list(projection_b.get("included_memory_ids") or []),
            "dialogue_b_projection_summary": projection_b.get("summary", ""),
        }
    finally:
        wiring_a.runtime_wiring.graph.delete_graph(graph_id)
        _delete_postgres_governance_decisions(dsn, [governance_batch_id])
        _delete_working_context(dsn, memory_domain_id)
        if result_a is not None:
            _delete_postgres_artifacts_with_prefix(
                dsn,
                f"timeline:{wiring_a.runtime_wiring.runtime_session.runtime_session_id}:{result_a.state.run_id}:",
            )
        if wiring_b is not None and result_b is not None:
            _delete_postgres_artifacts_with_prefix(
                dsn,
                f"timeline:{wiring_b.runtime_wiring.runtime_session.runtime_session_id}:{result_b.state.run_id}:",
            )
        _delete_postgres_runtime_session(dsn, wiring_a.runtime_wiring.runtime_session.runtime_session_id)
        if wiring_b is not None:
            _delete_postgres_runtime_session(dsn, wiring_b.runtime_wiring.runtime_session.runtime_session_id)


async def _run_real_agent_cross_dialogue_working_context_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    dsn = settings.storage.postgres_dsn
    memory_domain_id = f"u_real_wc_cross_{uuid4().hex[:12]}"
    dialogue_a_root = tmp_path / "working-context-dialogue-a"
    dialogue_b_root = tmp_path / "working-context-dialogue-b"
    dialogue_a_root.mkdir()
    dialogue_b_root.mkdir()
    domain_a = MemoryDomainContext(
        memory_domain_id=memory_domain_id,
        workspace_kind="project",
        stable_project_key=str(dialogue_a_root),
        workspace_label="repo-a",
    )
    domain_b = MemoryDomainContext(
        memory_domain_id=memory_domain_id,
        workspace_kind="project",
        stable_project_key=str(dialogue_b_root),
        workspace_label="repo-b",
    )
    _delete_working_context(dsn, memory_domain_id)
    wiring_a = build_agent_runtime_wiring(
        settings,
        dialogue_a_root,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=128),
        system_prompt=(
            "You are dialogue A in a working-context integration test. Do not call tools. "
            "Answer exactly this sentence: Dialogue A recently validated the domain-shared working context "
            "sentinel PULSARA_WORKING_CONTEXT_CROSS_DIALOGUE_A so a later dialogue in the same memory domain "
            "can recover recent activity without using canonical memory."
        ),
        memory_domain=domain_a,
        memory_reflection=False,
    )
    graph_id = wiring_a.runtime_wiring.graph_id
    assert graph_id is not None
    result_a = None
    result_b = None
    wiring_b = None
    try:
        result_a = await wiring_a.agent_runtime.run_task("Write the working-context sentinel sentence exactly.")
        store = PostgresWorkingContextStore(dsn=dsn)
        stored = store.get_latest(memory_domain_id=memory_domain_id)
        # This text-only run intentionally exceeds the working-context substantive-signal floor.
        assert stored is not None

        wiring_b = build_agent_runtime_wiring(
            settings,
            dialogue_b_root,
            durable=True,
            model_role=ModelRole.FLASH,
            options=LLMOptions(temperature=0, max_output_tokens=96),
            system_prompt=(
                "You are dialogue B in a working-context integration test. Do not call tools. "
                "Use only the Recalled Memory section. If it contains "
                "PULSARA_WORKING_CONTEXT_CROSS_DIALOGUE_A, answer exactly PULSARA_WORKING_CONTEXT_OK. "
                "Otherwise answer exactly PULSARA_WORKING_CONTEXT_MISSING."
            ),
            memory_domain=domain_b,
            memory_reflection=False,
        )
        result_b = await wiring_b.agent_runtime.run_task("Check whether recent activity from dialogue A is visible.")
        projection_b = result_b.state.memory_projection or {}
        return {
            "graph_id": graph_id,
            "dialogue_a_status": result_a.status.value,
            "dialogue_a_run_id": result_a.state.run_id,
            "dialogue_a_final_text": result_a.final_text.strip(),
            "stored_working_context_source_run_id": stored.source_run_id,
            "stored_working_context_workspace_key": stored.workspace_key,
            "expected_workspace_key": domain_a.stable_project_key,
            "stored_working_context_summary": stored.summary,
            "dialogue_b_status": result_b.status.value,
            "dialogue_b_final_text": result_b.final_text.strip(),
            "dialogue_b_projection_kind": projection_b.get("projection_kind"),
            "dialogue_b_projection_summary": projection_b.get("summary", ""),
            "dialogue_b_projection_ids": list(projection_b.get("included_memory_ids") or []),
        }
    finally:
        wiring_a.runtime_wiring.graph.delete_graph(graph_id)
        _delete_working_context(dsn, memory_domain_id)
        if result_a is not None:
            _delete_postgres_artifacts_with_prefix(
                dsn,
                f"timeline:{wiring_a.runtime_wiring.runtime_session.runtime_session_id}:{result_a.state.run_id}:",
            )
        if wiring_b is not None and result_b is not None:
            _delete_postgres_artifacts_with_prefix(
                dsn,
                f"timeline:{wiring_b.runtime_wiring.runtime_session.runtime_session_id}:{result_b.state.run_id}:",
            )
        _delete_postgres_runtime_session(dsn, wiring_a.runtime_wiring.runtime_session.runtime_session_id)
        if wiring_b is not None:
            _delete_postgres_runtime_session(dsn, wiring_b.runtime_wiring.runtime_session.runtime_session_id)


async def _run_real_agent_scope_assignment_trajectory_samples(tmp_path: Path) -> list[dict]:
    cases = (
        {
            "label": "user_preference",
            "user_input": "Please remember this across all projects: I prefer compact final summaries.",
            "expected_final": "PULSARA_SCOPE_USER_OK",
        },
        {
            "label": "workspace_decision",
            "user_input": "Please remember this project decision for the current repository: run scope tests with pytest -q.",
            "expected_final": "PULSARA_SCOPE_WORKSPACE_OK",
        },
        {
            "label": "one_off_task_detail",
            "user_input": "For this one-off task only, remember that /tmp/pulsara-scratch.txt is the scratch file to inspect next.",
            "expected_final": "PULSARA_SCOPE_SKIP_OK",
        },
    )
    results: list[dict] = []
    for case in cases:
        results.append(await _run_real_agent_scope_assignment_case(tmp_path / case["label"], case))
    return results


async def _run_real_agent_scope_assignment_case(tmp_path: Path, case: dict) -> dict:
    settings = _load_settings_for_real_llm()
    project_root = tmp_path / "scope-repo"
    domain = MemoryDomainContext(
        memory_domain_id=f"u_real_scope_assign_{uuid4().hex[:12]}",
        workspace_kind="project",
        stable_project_key=str(project_root),
    )
    workspace_scope_value = workspace_scope(str(project_root))
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=192),
        system_prompt=(
            "You are collecting real LLM memory scope-assignment trajectories. "
            "If the user explicitly asks to remember durable user-wide information, call exactly one appropriate "
            "remember_* tool with scope ctx:user. If the user explicitly asks to remember a durable current-project "
            f"fact or decision, call exactly one appropriate remember_* tool with scope {workspace_scope_value}. "
            "If the user asks to remember a one-off task detail, do not call memory tools. "
            f"After following those rules, answer exactly {case['expected_final']}."
        ),
        memory_domain=domain,
        memory_reflection=False,
    )
    graph_id = wiring.runtime_wiring.graph_id
    assert graph_id is not None
    result = None
    try:
        result = await wiring.agent_runtime.run_task(case["user_input"])
        events = wiring.runtime_wiring.event_log.iter(run_id=result.state.run_id)
        tool_call_events = [event for event in events if isinstance(event, ToolCallStartEvent)]
        memory_tool_events = [
            event for event in tool_call_events if event.tool_call_name.startswith("remember_")
        ]
        tool_arguments = _tool_arguments_by_call_id(events)
        memory_arguments = [
            tool_arguments.get(event.tool_call_id, {})
            for event in memory_tool_events
            if event.tool_call_name.startswith("remember_")
        ]
        errors = _run_error_diagnostics(events)
        return {
            "label": case["label"],
            "status": result.status.value,
            "final_text": result.final_text.strip(),
            "errors": errors,
            "memory_tool_names": [event.tool_call_name for event in memory_tool_events],
            "memory_scopes": [args.get("scope") for args in memory_arguments if isinstance(args, dict)],
            "memory_arguments": memory_arguments,
            "candidate_pool_pending": len(wiring.runtime_wiring.candidate_pool.list_pending()),
            "expected_workspace_scope": workspace_scope_value,
        }
    finally:
        wiring.runtime_wiring.graph.delete_graph(graph_id)
        if result is not None:
            _delete_postgres_artifacts_with_prefix(
                settings.storage.postgres_dsn,
                f"timeline:{wiring.runtime_wiring.runtime_session.runtime_session_id}:{result.state.run_id}:",
            )
        _delete_working_context(settings.storage.postgres_dsn, domain.memory_domain_id)
        _delete_postgres_runtime_session(
            settings.storage.postgres_dsn,
            wiring.runtime_wiring.runtime_session.runtime_session_id,
        )


async def _run_real_agent_memory_explain_tool_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    graph_id = f"graph:real-explain/{uuid4().hex}"
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=160),
        system_prompt=(
            "You are validating the memory_explain tool. "
            "Before answering, you must call memory_explain with memory_id 'preference:real-explain-old'. "
            "If the tool result contains an explanation claim with kind 'superseded_by', answer exactly "
            "PULSARA_MEMORY_EXPLAIN_OK. Otherwise answer exactly PULSARA_MEMORY_EXPLAIN_MISSING."
        ),
        graph_id=graph_id,
    )
    now = utc_now()
    wiring.runtime_wiring.graph.put_jsonld(
        Preference(
            id="preference:real-explain-old",
            statement="The user prefers verbose summaries.",
            scope="ctx:user",
            status=memory.NodeStatus.ACTIVE,
            confidence_level=memory.ConfidenceLevel.HIGH,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=now,
            updated_at=now,
            gate_reason="real llm memory_explain old seed",
        ).to_jsonld(),
        graph_id=graph_id,
    )
    wiring.runtime_wiring.graph.put_jsonld(
        Preference(
            id="preference:real-explain-new",
            statement="The user prefers concise summaries.",
            scope="ctx:user",
            status=memory.NodeStatus.ACTIVE,
            confidence_level=memory.ConfidenceLevel.HIGH,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=now,
            updated_at=now,
            gate_reason="real llm memory_explain new seed",
        ).to_jsonld(),
        graph_id=graph_id,
    )
    MemoryLifecycle(
        graph=wiring.runtime_wiring.graph,
        mutable=wiring.runtime_wiring.graph,
    ).supersede(
        old_id="preference:real-explain-old",
        new_id="preference:real-explain-new",
        governance_batch_id=f"governance:real-explain/{uuid4().hex}",
        graph_id=graph_id,
    )
    result = None
    try:
        result = await wiring.agent_runtime.run_task("Use memory_explain as instructed, then answer with the sentinel.")
        events = wiring.runtime_wiring.event_log.iter(run_id=result.state.run_id)
        tool_call_events = [event for event in events if isinstance(event, ToolCallStartEvent)]
        tool_result_texts = [event.delta for event in events if isinstance(event, ToolResultTextDeltaEvent)]
        return {
            "status": result.status.value,
            "final_text": result.final_text.strip(),
            "tool_names": [event.tool_call_name for event in tool_call_events],
            "tool_result_texts": tool_result_texts,
        }
    finally:
        wiring.runtime_wiring.graph.delete_graph(graph_id)
        if result is not None:
            _delete_postgres_artifacts_with_prefix(
                settings.storage.postgres_dsn,
                f"timeline:{wiring.runtime_wiring.runtime_session.runtime_session_id}:{result.state.run_id}:",
            )
        _delete_postgres_runtime_session(
            settings.storage.postgres_dsn,
            wiring.runtime_wiring.runtime_session.runtime_session_id,
        )


async def _run_real_agent_working_context_projection_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    domain = MemoryDomainContext(memory_domain_id="u_real_working_context", workspace_kind="transient")
    store = PostgresWorkingContextStore(dsn=settings.storage.postgres_dsn)
    store.upsert(
        domain=domain,
        source_session_id="runtime:seed-working-context",
        source_run_id="run:seed-working-context",
        summary="The user recently validated the working context projection sentinel PULSARA_WORKING_CONTEXT_OK.",
    )
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=96),
        system_prompt=(
            "You are validating working context injection. "
            "Use the Recalled Memory section only. If it contains the working context sentinel, "
            "answer exactly PULSARA_WORKING_CONTEXT_OK. Otherwise answer exactly PULSARA_WORKING_CONTEXT_MISSING. "
            "Do not call tools."
        ),
        memory_domain=domain,
    )
    result = None
    try:
        result = await wiring.agent_runtime.run_task("Check the working context projection for the sentinel.")
        projection = result.state.memory_projection or {}
        return {
            "status": result.status.value,
            "final_text": result.final_text.strip(),
            "projection_summary": projection.get("summary", ""),
            "projection_kind": projection.get("projection_kind"),
        }
    finally:
        wiring.runtime_wiring.graph.delete_graph(wiring.runtime_wiring.graph_id)
        if result is not None:
            _delete_postgres_artifacts_with_prefix(
                settings.storage.postgres_dsn,
                f"timeline:{wiring.runtime_wiring.runtime_session.runtime_session_id}:{result.state.run_id}:",
            )
        _delete_working_context(settings.storage.postgres_dsn, domain.memory_domain_id)
        _delete_postgres_runtime_session(
            settings.storage.postgres_dsn,
            wiring.runtime_wiring.runtime_session.runtime_session_id,
        )


async def _run_real_agent_transient_scope_discipline_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    domain = MemoryDomainContext(
        memory_domain_id=f"u_real_transient_{uuid4().hex[:12]}",
        workspace_kind="transient",
    )
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=128),
        system_prompt=(
            "You are validating transient memory scope discipline. "
            "Only create durable memory when the injected durable-memory rules allow it. "
            "If the user asks to remember a one-off scratch task detail in this transient run, "
            "do not call any remember_* tool; answer exactly PULSARA_TRANSIENT_SCOPE_OK."
        ),
        memory_domain=domain,
        memory_reflection=False,
    )
    graph_id = wiring.runtime_wiring.graph_id
    assert graph_id is not None
    result = None
    try:
        result = await wiring.agent_runtime.run_task(
            "Remember for this temporary task that scratch file /tmp/pulsara-one-off.txt is the next file to inspect."
        )
        events = wiring.runtime_wiring.event_log.iter(run_id=result.state.run_id)
        tool_call_events = [event for event in events if isinstance(event, ToolCallStartEvent)]
        memory_tool_names = [
            event.tool_call_name for event in tool_call_events if event.tool_call_name.startswith("remember_")
        ]
        errors = _run_error_diagnostics(events)
        return {
            "status": result.status.value,
            "final_text": result.final_text.strip(),
            "errors": errors,
            "memory_tool_names": memory_tool_names,
            "candidate_pool_pending": len(wiring.runtime_wiring.candidate_pool.list_pending()),
            "memory_node_count": sum(
                len(wiring.runtime_wiring.graph.find_by_type(node_type, graph_id=graph_id))
                for node_type in _MEMORY_NODE_TYPES
            ),
        }
    finally:
        wiring.runtime_wiring.graph.delete_graph(graph_id)
        if result is not None:
            _delete_postgres_artifacts_with_prefix(
                settings.storage.postgres_dsn,
                f"timeline:{wiring.runtime_wiring.runtime_session.runtime_session_id}:{result.state.run_id}:",
            )
        _delete_working_context(settings.storage.postgres_dsn, domain.memory_domain_id)
        _delete_postgres_runtime_session(
            settings.storage.postgres_dsn,
            wiring.runtime_wiring.runtime_session.runtime_session_id,
        )


async def _run_real_llm_trajectory_suite(tmp_path: Path) -> list[dict]:
    agent_read_dir = tmp_path / "agent-read"
    durable_read_dir = tmp_path / "durable-read"
    multi_tool_dir = tmp_path / "multi-tool"
    memory_dirs = {
        case["label"]: tmp_path / case["label"]
        for case in _REAL_MEMORY_TOOL_CASES
    }
    agent_read_dir.mkdir()
    durable_read_dir.mkdir()
    multi_tool_dir.mkdir()
    for directory in memory_dirs.values():
        directory.mkdir()

    flash = await _run_real_flash_smoke()
    tool_spec = await _run_real_tool_call_smoke()
    agent_read = await _run_real_agent_tool_loop_smoke(agent_read_dir)
    durable_read = await _run_real_agent_postgres_event_log_timeline_smoke(durable_read_dir)
    multi_tool = await _run_real_agent_multi_tool_rollout(multi_tool_dir)
    memory_rollouts = [
        await _run_real_agent_remember_tool_rollout(memory_dirs[case["label"]], case)
        for case in _REAL_MEMORY_TOOL_CASES
    ]
    return [
        _trajectory_from_stream_result(
            "flash_text",
            flash,
            final_text=flash["text"],
        ),
        _trajectory_from_stream_result(
            "tool_spec_call",
            tool_spec,
            final_text=tool_spec["text"],
            tool_names=[tool_spec["tool_call_name"]],
        ),
        {
            "label": "agent_read_file",
            "status": agent_read["status"],
            "final_text": agent_read["final_text"],
            "event_type_names": [],
            "tool_call_count": len(agent_read["tool_call_ids"]),
            "tool_names": ["read_file"] if agent_read["tool_call_ids"] else [],
            "tool_result_count": len(agent_read["tool_result_ids"]),
            "timeline_status": None,
            "timeline_item_kinds": [],
            "postgres_event_count": None,
            "errors": agent_read["errors"],
        },
        {
            "label": "durable_agent_read_file",
            "status": durable_read["status"],
            "final_text": durable_read["replayed_text"],
            "event_type_names": [],
            "tool_call_count": len(durable_read["tool_call_arguments"]),
            "tool_names": ["read_file"] if durable_read["tool_call_arguments"] else [],
            "tool_result_count": len(durable_read["tool_result_summaries"]),
            "timeline_status": durable_read["timeline_status"],
            "timeline_item_kinds": durable_read["timeline_item_kinds"],
            "postgres_event_count": durable_read["postgres_event_count"],
            "errors": [],
        },
        multi_tool,
        *memory_rollouts,
    ]


async def _run_real_agent_multi_tool_rollout(tmp_path: Path) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "alpha.txt").write_text("PULSARA_MULTI_ALPHA\n", encoding="utf-8")
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "beta.md").write_text(
        "# Beta\n\nThe second rollout token is PULSARA_MULTI_BETA.\n",
        encoding="utf-8",
    )
    settings = _load_settings_for_real_llm()
    graph_id = f"graph:real-llm/{uuid4().hex}"
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=256),
        system_prompt=(
            "You are validating a longer multi-tool rollout. "
            "Before the final answer, call tools in this order: "
            "read_file on alpha.txt; search_files for PULSARA_MULTI_BETA; "
            "read_file on the matched beta file. "
            "Do not use write_file, edit_file, terminal, or todo."
        ),
        graph_id=graph_id,
    )
    runtime_session = wiring.runtime_wiring.runtime_session
    timeline_blob_prefix: str | None = None
    try:
        result = await wiring.agent_runtime.run_task(
            "Run the required three-step tool rollout, then answer exactly: "
            "PULSARA_MULTI_ALPHA|PULSARA_MULTI_BETA"
        )
        timeline_blob_prefix = f"timeline:{runtime_session.runtime_session_id}:{result.state.run_id}:"
        events = wiring.runtime_wiring.event_log.iter(run_id=result.state.run_id)
        tool_call_events = [event for event in events if isinstance(event, ToolCallStartEvent)]
        tool_result_events = [event for event in events if isinstance(event, ToolResultStartEvent)]
        errors = _run_error_diagnostics(events)
        records = wiring.runtime_wiring.graph.find_by_type(rt.RUN_TIMELINE, graph_id=graph_id)
        timeline = load_run_timeline(
            graph=wiring.runtime_wiring.graph,
            archive=wiring.runtime_wiring.archive,
            run_id=result.state.run_id,
            runtime_session_id=runtime_session.runtime_session_id,
            graph_id=graph_id,
        )
        summary = summarize_run_timeline(timeline)
        return {
            "label": "durable_multi_tool_rollout",
            "status": result.status.value,
            "final_text": result.final_text.strip(),
            "event_type_names": [type(event).__name__ for event in events],
            "tool_call_count": len(tool_call_events),
            "tool_names": [event.tool_call_name for event in tool_call_events],
            "tool_result_count": len(tool_result_events),
            "timeline_status": summary.status,
            "timeline_item_kinds": [item.kind for item in timeline.items],
            "timeline_records": len(records),
            "postgres_event_count": len(events),
            "postgres_sequence_numbers": [event.sequence for event in events],
            "tool_call_arguments": [trace.arguments for trace in summary.tool_traces],
            "tool_result_summaries": [trace.result_summary for trace in summary.tool_traces],
            "errors": errors,
        }
    finally:
        wiring.runtime_wiring.graph.delete_graph(graph_id)
        if timeline_blob_prefix is not None:
            _delete_postgres_artifacts_with_prefix(settings.storage.postgres_dsn, timeline_blob_prefix)
        _delete_postgres_runtime_session(settings.storage.postgres_dsn, runtime_session.runtime_session_id)


_MEMORY_NODE_TYPES = (
    memory.PREFERENCE,
    memory.CLAIM,
    memory.OBSERVATION,
    memory.ACTION_BOUNDARY,
    memory.DECISION,
)

_MEMORY_NODE_BY_TYPE = {
    "Claim": memory.CLAIM,
    "Preference": memory.PREFERENCE,
    "Observation": memory.OBSERVATION,
    "ActionBoundary": memory.ACTION_BOUNDARY,
    "Decision": memory.DECISION,
}

_REAL_MEMORY_TOOL_CASES = (
    {
        "label": "durable_remember_claim",
        "tool_name": "remember_claim",
        "memory_type": "Claim",
        "system_prompt": (
            "You are validating the remember_claim tool. "
            "Call remember_claim exactly once with statement='Pulsara uses JSON-LD graph nodes for durable memory', "
            "scope='{workspace_scope}', source_authority='explicit_user_instruction', and "
            "verification_status='user_confirmed'. Then answer exactly: PULSARA_MEMORY_CLAIM_OK"
        ),
        "user_input": "Remember this durable claim: Pulsara uses JSON-LD graph nodes for durable memory.",
    },
    {
        "label": "durable_remember_preference",
        "tool_name": "remember_preference",
        "memory_type": "Preference",
        "system_prompt": (
            "You are validating the remember_preference tool. "
            "Call remember_preference exactly once with statement='The user prefers concise summaries', "
            "scope='ctx:user', source_authority='explicit_user_instruction', and "
            "verification_status='user_confirmed'. Then answer exactly: PULSARA_MEMORY_PREFERENCE_OK"
        ),
        "user_input": "Going forward I always want concise summaries. Remember that preference.",
    },
    {
        "label": "durable_remember_observation",
        "tool_name": "remember_observation",
        "memory_type": "Observation",
        "system_prompt": (
            "You are validating the remember_observation tool. "
            "Call remember_observation exactly once with statement='The current workspace is testing narrow memory tools', "
            "scope='{workspace_scope}', source_authority='explicit_user_instruction', and "
            "verification_status='user_confirmed'. Then answer exactly: PULSARA_MEMORY_OBSERVATION_OK"
        ),
        "user_input": "Observe and remember that this workspace is testing narrow memory tools.",
    },
    {
        "label": "durable_remember_action_boundary",
        "tool_name": "remember_action_boundary",
        "memory_type": "ActionBoundary",
        "system_prompt": (
            "You are validating the remember_action_boundary tool. "
            "Call remember_action_boundary exactly once with statement='Do not commit code unless the user explicitly asks', "
            "scope='{workspace_scope}', applies_when='working in a git repository', "
            "do_not_apply_when='the user explicitly asks for git add and commit', "
            "source_authority='explicit_user_instruction', and verification_status='user_confirmed'. "
            "Then answer exactly: PULSARA_MEMORY_ACTION_BOUNDARY_OK"
        ),
        "user_input": "Remember this action boundary: do not commit code unless I explicitly ask.",
    },
    {
        "label": "durable_remember_decision",
        "tool_name": "remember_decision",
        "memory_type": "Decision",
        "system_prompt": (
            "You are validating the remember_decision tool. "
            "Call remember_decision exactly once with statement='Use type-specific remember tools instead of a single propose_memory tool', "
            "scope='{workspace_scope}', source_authority='explicit_user_instruction', and "
            "verification_status='user_confirmed'. Do not include based_on_ids. "
            "Then answer exactly: PULSARA_MEMORY_DECISION_OK"
        ),
        "user_input": "Remember this project decision: use type-specific remember tools instead of propose_memory.",
    },
)


async def _run_real_agent_remember_tool_rollout(tmp_path: Path, case: dict) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = _load_settings_for_real_llm()
    project_root = tmp_path / "test-project"
    memory_domain = MemoryDomainContext(
        memory_domain_id=f"u_real_memory_tools_{uuid4().hex[:12]}",
        workspace_kind="project",
        stable_project_key=str(project_root),
    )
    workspace_scope_value = workspace_scope(str(project_root))
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=256),
        system_prompt=case["system_prompt"].format(workspace_scope=workspace_scope_value),
        memory_domain=memory_domain,
    )
    graph_id = wiring.runtime_wiring.graph_id
    assert graph_id is not None
    runtime_session = wiring.runtime_wiring.runtime_session
    timeline_blob_prefix: str | None = None
    governance_batch_id = f"governance:real-llm:{uuid4().hex}"
    try:
        result = await wiring.agent_runtime.run_task(case["user_input"])
        timeline_blob_prefix = f"timeline:{runtime_session.runtime_session_id}:{result.state.run_id}:"
        events = wiring.runtime_wiring.event_log.iter(run_id=result.state.run_id)
        tool_call_events = [event for event in events if isinstance(event, ToolCallStartEvent)]
        tool_result_events = [event for event in events if isinstance(event, ToolResultStartEvent)]
        pending_before_governance = wiring.runtime_wiring.candidate_pool.list_pending()
        governance_results = wiring.runtime_wiring.memory_governance_executor.submit_pending_as_is(
            governance_batch_id=governance_batch_id
        )
        governance_events = [event for governance in governance_results for event in governance.events]
        all_events = [*events, *governance_events]
        memory_results = [event for event in governance_events if isinstance(event, MemoryWriteResultEvent)]
        memory_failures = [event for event in governance_events if isinstance(event, MemoryWriteFailedEvent)]
        errors = _run_error_diagnostics(events)
        target_memory_node_count = len(
            wiring.runtime_wiring.graph.find_by_type(
                _MEMORY_NODE_BY_TYPE[case["memory_type"]],
                graph_id=graph_id,
            )
        )
        return {
            "label": case["label"],
            "target_tool": case["tool_name"],
            "target_memory_type": case["memory_type"],
            "status": result.status.value,
            "final_text": result.final_text.strip(),
            "event_type_names": [type(event).__name__ for event in all_events],
            "source_event_type_names": [type(event).__name__ for event in events],
            "governance_event_type_names": [type(event).__name__ for event in governance_events],
            "tool_call_count": len(tool_call_events),
            "tool_names": [event.tool_call_name for event in tool_call_events],
            "tool_result_count": len(tool_result_events),
            "timeline_status": None,
            "timeline_item_kinds": [],
            "postgres_event_count": len(events),
            "target_memory_node_count": target_memory_node_count,
            "candidate_pool_pending_before_governance": len(pending_before_governance),
            "candidate_pool_pending_after_governance": len(wiring.runtime_wiring.candidate_pool.list_pending()),
            "memory_node_count": sum(
                len(wiring.runtime_wiring.graph.find_by_type(node_type, graph_id=graph_id))
                for node_type in _MEMORY_NODE_TYPES
            ),
            "memory_result_types": [event.memory_type for event in memory_results],
            "memory_statuses": [event.status.value for event in memory_results],
            "memory_failure_types": [event.error_type for event in memory_failures],
            "errors": errors,
        }
    finally:
        _delete_postgres_governance_decisions(settings.storage.postgres_dsn, [governance_batch_id])
        wiring.runtime_wiring.graph.delete_graph(graph_id)
        _delete_working_context(settings.storage.postgres_dsn, memory_domain.memory_domain_id)
        if timeline_blob_prefix is not None:
            _delete_postgres_artifacts_with_prefix(settings.storage.postgres_dsn, timeline_blob_prefix)
        _delete_postgres_runtime_session(settings.storage.postgres_dsn, runtime_session.runtime_session_id)


def _trajectory_from_stream_result(
    label: str,
    result: dict,
    *,
    final_text: str,
    tool_names: list[str] | None = None,
) -> dict:
    event_type_names = result["event_type_names"]
    tool_names = [name for name in (tool_names or []) if name]
    return {
        "label": label,
        "status": "streamed",
        "final_text": final_text,
        "event_type_names": event_type_names,
        "tool_call_count": event_type_names.count("ToolCallStartEvent"),
        "tool_names": tool_names,
        "tool_result_count": 0,
        "timeline_status": None,
        "timeline_item_kinds": [],
        "postgres_event_count": None,
        "errors": result["errors"],
    }


async def _collect_real_events(
    *,
    role: ModelRole,
    context: LLMContext,
    options: LLMOptions,
    label: str,
) -> dict:
    settings = _load_settings_for_real_llm()
    runtime = build_llm_runtime(settings.llm)
    event_context = EventContext(
        run_id=f"run:{label}",
        turn_id=f"turn:{label}/001",
        reply_id=f"reply:{label}/001",
    )
    log = InMemoryEventLog()
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    errors: list[dict] = []

    async for event in runtime.stream(
        role=role,
        context=context,
        event_context=event_context,
        options=options,
    ):
        log.append(event)
        if isinstance(event, TextBlockDeltaEvent):
            text_parts.append(event.delta)
        if isinstance(event, ThinkingBlockDeltaEvent):
            thinking_parts.append(event.delta)
        if isinstance(event, RunErrorEvent):
            errors.append(_run_error_diagnostic(event))

    events = log.iter(reply_id=event_context.reply_id)
    message = log.replay(event_context.reply_id)
    assert any(isinstance(event, ModelCallStartEvent) for event in events)
    assert any(isinstance(event, ModelCallEndEvent) for event in events) or errors
    assert isinstance(events[0], ReplyStartEvent)
    assert isinstance(events[-1], ReplyEndEvent) or errors
    return {
        "events": events,
        "message": message,
        "text": "".join(text_parts).strip(),
        "thinking": "".join(thinking_parts).strip(),
        "errors": errors,
    }


async def _run_real_flash_memory_reflection_smoke() -> dict:
    settings = _load_settings_for_real_llm()
    graph = InMemoryGraphStore()
    candidate_pool = InMemoryCandidatePool()
    event_log = InMemoryEventLog()
    ledger = ExecutionEvidenceLedger(
        graph=graph,
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
    )
    engine = MemoryReflectionEngine(
        llm_runtime=build_llm_runtime(settings.llm),
        candidate_pool=candidate_pool,
        graph=graph,
        options=MemoryReflectionOptions(llm_options=LLMOptions(temperature=0, max_output_tokens=512)),
    )
    state = LoopState(session_id="runtime:real-reflection")
    state.messages.append(
        UserMsg(
            name="user",
            content="Please remember this durable preference: the user prefers concise summaries.",
        )
    )

    events = await engine.reflect(
        state=state,
        event_store=event_log,
        trigger_reasons=["cheap_memory_hint"],
        cheap_hints=[
            MemoryReflectionHint(
                source="cheap_string_match",
                reason="real LLM smoke matched explicit remember wording",
                signal="remember",
                excerpt="Please remember this durable preference: the user prefers concise summaries.",
            )
        ],
        safe_point="on_session_end",
    )
    pending_after_reflection = len(candidate_pool.list_pending())
    governance = MemoryGovernanceExecutor(
        candidate_pool=candidate_pool,
        memory_write_service=MemoryWriteService(ledger=ledger),
        event_log=event_log,
        graph=graph,
        runtime_session_id=state.session_id,
    )
    governance_results = governance.submit_pending_as_is(governance_batch_id="governance:real-reflection")
    governance_events = [event for result in governance_results for event in result.events]
    memory_results = [event for event in governance_events if isinstance(event, MemoryWriteResultEvent)]
    failures = [event for event in events if isinstance(event, MemoryReflectionFailedEvent)]
    return {
        "event_type_names": [type(event).__name__ for event in events],
        "governance_event_type_names": [type(event).__name__ for event in governance_events],
        "candidate_pool_pending_after_reflection": pending_after_reflection,
        "candidate_pool_pending_after_governance": len(candidate_pool.list_pending()),
        "memory_result_types": [event.memory_type for event in memory_results],
        "memory_statuses": [event.status.value for event in memory_results],
        "failed_events": [event.message for event in failures],
        "preference_count": len(graph.find_by_type(memory.PREFERENCE)),
    }


async def _run_real_flash_memory_retry_json_smoke() -> dict:
    retry_error = {
        "status": "invalid_candidate",
        "retry_allowed": True,
        "retry_count": 1,
        "retry_limit": 3,
        "remaining_retries": 2,
        "message": (
            "1 validation error for PreferenceCandidate\n"
            "applies_when\n  Extra inputs are not permitted"
        ),
    }
    context = LLMContext(
        system_prompt=(
            "You are validating memory-tool retry behavior. "
            "The prior remember_preference call failed with a JSON tool result. "
            "If the JSON says retry_allowed=true, retry the same memory intent exactly once. "
            "Use only valid remember_preference arguments. Do not answer normally."
        ),
        messages=(
            LLMMessage.user(
                "Remember this preference: the user prefers compact status updates."
            ),
            LLMMessage.tool_call(
                tool_call_id="call:invalid-memory",
                name="remember_preference",
                arguments=json.dumps(
                    {
                        "statement": "The user prefers compact status updates",
                        "scope": "ctx:user",
                        "source_authority": "explicit_user_instruction",
                        "verification_status": "user_confirmed",
                        "applies_when": "retry validation should fail",
                    }
                ),
            ),
            LLMMessage.tool_result(
                json.dumps(retry_error, ensure_ascii=False),
                tool_call_id="call:invalid-memory",
            ),
            LLMMessage.user(
                "Continue from the invalid_candidate result. Retry only if retry_allowed is true."
            ),
        ),
        tools=(
            ToolSpec(
                name=RememberPreferenceTool.name,
                description=RememberPreferenceTool.description,
                parameters=RememberPreferenceTool.parameters,
            ),
        ),
    )
    result = await _collect_real_events(
        role=ModelRole.FLASH,
        context=context,
        options=LLMOptions(temperature=0, max_output_tokens=128),
        label="real-memory-retry-json",
    )
    events = result["events"]
    tool_call_events = [event for event in events if isinstance(event, ToolCallStartEvent)]
    tool_call_arguments = "".join(event.delta for event in events if isinstance(event, ToolCallDeltaEvent))
    return {
        "tool_names": [event.tool_call_name for event in tool_call_events],
        "tool_arguments": json.loads(tool_call_arguments) if tool_call_arguments else {},
        "event_type_names": [type(event).__name__ for event in events],
        "text": result["text"],
        "errors": result["errors"],
    }


async def _run_real_flash_memory_governance_smoke() -> dict:
    settings = _load_settings_for_real_llm()
    graph = InMemoryGraphStore()
    candidate_pool = InMemoryCandidatePool()
    event_log = InMemoryEventLog()
    ledger = ExecutionEvidenceLedger(
        graph=graph,
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
    )
    candidate_pool.append_candidate(
        PooledMemoryCandidate(
            payload=ValidCandidatePayload(
                candidate=PreferenceCandidate(
                    candidate_id="candidate:real-governance-preference",
                    statement="The user prefers concise summaries.",
                    scope="ctx:user",
                    source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                    verification_status=memory.VerificationStatus.USER_CONFIRMED,
                )
            ),
            origin=CandidateOrigin.MAIN_AGENT_TOOL,
            source_session_id="runtime:real-governance",
            source_run_id="run:real-governance-source",
            source_turn_id="turn:real-governance-source",
            source_reply_id="reply:real-governance-source",
        )
    )
    executor = MemoryGovernanceExecutor(
        candidate_pool=candidate_pool,
        memory_write_service=MemoryWriteService(ledger=ledger),
        event_log=event_log,
        graph=graph,
        runtime_session_id="runtime:real-governance",
    )
    engine = MemoryGovernanceEngine(
        llm_runtime=build_llm_runtime(settings.llm),
        executor=executor,
        options=MemoryGovernanceOptions(llm_options=LLMOptions(temperature=0, max_output_tokens=512)),
    )

    result = await engine.run_pending(
        trigger_reason="real_llm_governance_smoke",
        governance_batch_id="governance:real-governance",
    )
    governance_events = [event for applied in result.applied for event in applied.events]
    memory_results = [event for event in governance_events if isinstance(event, MemoryWriteResultEvent)]
    return {
        "error_type": result.error_type,
        "error_message": result.error_message,
        "decision_kinds": [decision.kind for decision in result.decisions],
        "governance_event_type_names": [type(event).__name__ for event in governance_events],
        "candidate_pool_pending_after_governance": len(candidate_pool.list_pending()),
        "memory_result_types": [event.memory_type for event in memory_results],
        "memory_statuses": [event.status.value for event in memory_results],
        "preference_count": len(graph.find_by_type(memory.PREFERENCE)),
    }


async def _run_real_flash_memory_governance_supersede_smoke(tmp_path: Path) -> dict:
    return await _run_real_flash_memory_governance_lifecycle_smoke(
        tmp_path,
        label="supersede",
        old_statement="The user prefers verbose status summaries.",
        new_statement="The user prefers concise status summaries.",
        user_quote=(
            "Actually, change my status summary preference: stop using verbose status summaries; "
            "use concise status summaries instead."
        ),
    )


async def _run_real_flash_memory_governance_coexist_smoke(tmp_path: Path) -> dict:
    return await _run_real_flash_memory_governance_lifecycle_smoke(
        tmp_path,
        label="coexist",
        old_statement="The user prefers dark theme in the IDE.",
        new_statement="The user prefers concise status summaries.",
        user_quote="Please remember that the user prefers concise status summaries.",
    )


async def _run_real_flash_memory_governance_contradiction_smoke(tmp_path: Path) -> dict:
    return await _run_real_flash_memory_governance_lifecycle_smoke(
        tmp_path,
        label="contradiction",
        old_statement="The user likes egg tarts.",
        new_statement="The user hates egg tarts.",
        user_quote="Please remember that the user hates egg tarts.",
    )


async def _run_real_flash_memory_governance_lifecycle_smoke(
    tmp_path: Path,
    *,
    label: str,
    old_statement: str,
    new_statement: str,
    user_quote: str,
) -> dict:
    settings = _load_settings_for_real_llm()
    dsn = settings.storage.postgres_dsn
    graph_id = f"graph:real-governance-{label}/{uuid4().hex}"
    runtime_session_id = f"runtime:real-governance-{label}:{uuid4().hex}"
    old_id = f"preference:real-governance-{label}-old"
    source_ctx = EventContext(
        run_id=f"run:real-governance-{label}-source:{uuid4().hex}",
        turn_id=f"turn:real-governance-{label}-source:{uuid4().hex}",
        reply_id=f"reply:real-governance-{label}-source:{uuid4().hex}",
    )
    governance_batch_id = f"governance:real-governance-{label}:{uuid4().hex}"
    graph = PostgresGraphStore(dsn=dsn)
    event_log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    event_log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta=user_quote))
    candidate_pool = PostgresCandidatePool(dsn=dsn)
    now = utc_now()
    try:
        graph.put_jsonld(
            Preference(
                id=old_id,
                statement=old_statement,
                scope="ctx:user",
                status=memory.NodeStatus.ACTIVE,
                confidence_level=memory.ConfidenceLevel.HIGH,
                verification_status=memory.VerificationStatus.USER_CONFIRMED,
                source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                created_at=now,
                updated_at=now,
                gate_reason="real llm governance supersede seed",
            ).to_jsonld(),
            graph_id=graph_id,
        )
        pooled = candidate_pool.append_candidate(
            PooledMemoryCandidate(
                payload=ValidCandidatePayload(
                    candidate=PreferenceCandidate(
                        candidate_id=f"candidate:real-governance-{label}",
                        statement=new_statement,
                        scope="ctx:user",
                        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                        verification_status=memory.VerificationStatus.USER_CONFIRMED,
                    )
                ),
                origin=CandidateOrigin.MAIN_AGENT_TOOL,
                source_session_id=runtime_session_id,
                source_run_id=source_ctx.run_id,
                source_turn_id=source_ctx.turn_id,
                source_reply_id=source_ctx.reply_id,
                user_quote=user_quote,
            )
        )
        executor = MemoryGovernanceExecutor(
            candidate_pool=candidate_pool,
            memory_write_service=MemoryWriteService(
                ledger=ExecutionEvidenceLedger(
                    graph=InMemoryGraphStore(),
                    archive=InMemoryArchiveStore(),
                    gate=MemoryWriteGate(),
                )
            ),
            event_log=event_log,
            graph=graph,
            graph_id=graph_id,
            runtime_session_id=runtime_session_id,
            memory_write_uow_factory=lambda: MemoryWriteUnitOfWork(
                dsn=dsn,
                runtime_session_id=runtime_session_id,
                graph_id=graph_id,
                workspace_root=tmp_path,
            ),
        )
        engine = MemoryGovernanceEngine(
            llm_runtime=build_llm_runtime(settings.llm),
            executor=executor,
            options=MemoryGovernanceOptions(llm_options=LLMOptions(temperature=0, max_output_tokens=900)),
        )

        result = await engine.run_pending(
            trigger_reason=f"real_llm_governance_{label}_smoke",
            governance_batch_id=governance_batch_id,
        )
        old_doc = graph.get_jsonld(old_id, graph_id=graph_id)
        write_outcome = result.applied[0].decision_record.write_outcome if result.applied else None
        new_id = getattr(write_outcome, "memory_id", None)
        new_doc = graph.get_jsonld(new_id, graph_id=graph_id) if isinstance(new_id, str) else {}
        governance_events = [event for applied in result.applied for event in applied.events]
        return {
            "error_type": result.error_type,
            "error_message": result.error_message,
            "decision_kinds": [decision.kind for decision in result.decisions],
            "recorded_decision_kind": result.applied[0].decision_record.decision.kind if result.applied else None,
            "applied_count": len(result.applied),
            "governance_event_type_names": [type(event).__name__ for event in governance_events],
            "target_entry_id": pooled.entry_id,
            "old_status": old_doc.get(memory.STATUS.name),
            "new_status": new_doc.get(memory.STATUS.name),
            "new_id": new_id,
            "superseded_memory_ids": list(getattr(write_outcome, "superseded_memory_ids", ())),
            "contradicted_memory_ids": list(getattr(write_outcome, "contradicted_memory_ids", ())),
            "supersedes_edge_present": {"@id": old_id} in new_doc.get(memory.SUPERSEDES.name, []),
            "contradicts_edge_present": (
                {"@id": old_id} in new_doc.get(memory.CONTRADICTS.name, [])
                and {"@id": new_id} in old_doc.get(memory.CONTRADICTS.name, [])
            ),
            "governance_candidate_count": sum(
                1 for candidate in candidate_pool.list_candidates() if candidate.origin is CandidateOrigin.GOVERNANCE
            ),
        }
    finally:
        graph.delete_graph(graph_id)
        _delete_postgres_governance_decisions(dsn, [governance_batch_id])
        _delete_postgres_runtime_session(dsn, runtime_session_id)


def _summarize_collected_result(result: dict) -> dict:
    return {
        "event_type_names": [type(event).__name__ for event in result["events"]],
        "text": result["text"],
        "thinking": result["thinking"],
        "errors": result["errors"],
    }


def _tool_arguments_by_call_id(events) -> dict[str, dict]:
    deltas_by_call: dict[str, list[str]] = {}
    for event in events:
        if isinstance(event, ToolCallDeltaEvent):
            deltas_by_call.setdefault(event.tool_call_id, []).append(event.delta)
    parsed: dict[str, dict] = {}
    for tool_call_id, deltas in deltas_by_call.items():
        raw = "".join(deltas)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            parsed[tool_call_id] = {"_raw": raw}
        else:
            parsed[tool_call_id] = payload if isinstance(payload, dict) else {"_raw": raw}
    return parsed


def _load_settings_for_real_llm() -> PulsaraSettings:
    env_file = Path(".env")
    if env_file.exists():
        return PulsaraSettings.from_env_file(env_file)
    return PulsaraSettings.from_env()


def _delete_postgres_runtime_session(dsn: str, runtime_session_id: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def _delete_postgres_artifact(dsn: str, blob_id: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from artifacts where id = %s", (blob_id,))


def _delete_postgres_artifacts_with_prefix(dsn: str, blob_id_prefix: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from artifacts where id like %s", (f"{blob_id_prefix}%",))


def _delete_postgres_governance_decisions(dsn: str, governance_batch_ids: list[str]) -> None:
    if not governance_batch_ids:
        return

    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "delete from memory_governance_decisions where governance_batch_id = any(%s)",
                (governance_batch_ids,),
            )


def _delete_working_context(dsn: str, memory_domain_id: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "delete from working_context_summaries where memory_domain_id = %s",
                (memory_domain_id,),
            )


def _artifact_id_from_node_ref(node_id: str) -> str:
    prefix = "urn:pulsara:"
    if node_id.startswith(prefix):
        return urllib.parse.unquote(node_id[len(prefix) :])
    return node_id
