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
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    RequireUserConfirmEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RunEndEvent,
    RunErrorEvent,
    RunStartEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
    TerminalProcessCompletedEvent,
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.capability import LocalSkillResolver, sync_bundled_skills
from pulsara_agent.event.candidates import PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.event_log import InMemoryEventLog, PostgresEventLog
from pulsara_agent.entities.memory import Preference
from pulsara_agent.graph import InMemoryGraphStore, PostgresGraphStore
from pulsara_agent.host import HostCore, HostWorkspaceInput
from pulsara_agent.host.transcript import INTERRUPTED_NOTE_TEXT, rebuild_prior_messages
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
from pulsara_agent.runtime import (
    AgentRuntime,
    ApprovalResolution,
    LoopBudget,
    LoopState,
    PlanExitResolution,
    RuntimeSession,
    ToolApprovalDecision,
    build_agent_runtime_wiring,
)
from pulsara_agent.runtime.context import msg_to_llm_messages
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionMode,
    PermissionProfile,
    TerminalAccess,
    preset_to_policy,
)
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


def _trusted_terminal_policy() -> EffectivePermissionPolicy:
    return EffectivePermissionPolicy(
        profile=PermissionProfile.TRUSTED_HOST,
        approval=ApprovalPolicy.RISKY_ONLY,
        terminal=TerminalAccess.ALLOW,
    )


def _trusted_terminal_ask_policy() -> EffectivePermissionPolicy:
    return EffectivePermissionPolicy(
        profile=PermissionProfile.TRUSTED_HOST,
        approval=ApprovalPolicy.RISKY_ONLY,
        terminal=TerminalAccess.ASK,
    )


def _workspace_on_request_policy() -> EffectivePermissionPolicy:
    return EffectivePermissionPolicy(
        profile=PermissionProfile.WORKSPACE_GUARDED,
        approval=ApprovalPolicy.ON_REQUEST,
        terminal=TerminalAccess.OFF,
    )


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


def test_real_flash_model_accepts_message_level_system_item():
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_message_level_system_smoke())

    assert result["errors"] == []
    assert result["event_type_names"][0] == "ReplyStartEvent"
    assert "ModelCallStartEvent" in result["event_type_names"]
    assert "ModelCallEndEvent" in result["event_type_names"]
    assert result["event_type_names"][-1] == "ReplyEndEvent"
    assert "PULSARA_SYSTEM_MSG_OK" in result["text"]
    assert "PULSARA_SYSTEM_MSG_OK" in result["replayed_text"]


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


def test_real_agent_runtime_read_only_policy_keeps_tools_visible_but_blocks_them(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_read_only_permission_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["stop_reason"] == "final"
    assert result["errors"] == []
    # Visible-but-blocked: write/terminal tools stay registered under read-only
    # (gate denies them at call time); they are NOT hidden from the registry.
    assert {"edit_file", "write_file", "terminal", "terminal_process"}.issubset(
        set(result["registry_names"])
    )
    assert result["tool_names"] == ["read_file"]
    assert "PULSARA_PERMISSION_READ_ONLY_OK" in result["final_text"]


def test_real_agent_runtime_trusted_host_allows_terminal_tool(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_trusted_terminal_permission_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["stop_reason"] == "final"
    assert result["errors"] == []
    assert result["tool_names"][:1] == ["terminal"]
    assert result["terminal_status"] == "success"
    assert result["terminal_output"] == "PULSARA_PERMISSION_TERMINAL_OK"
    assert "PULSARA_PERMISSION_TERMINAL_OK" in result["final_text"]


def test_real_host_core_terminal_access_ask_approval_completes(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(asyncio.wait_for(_run_real_host_core_terminal_ask_approval_smoke(tmp_path), timeout=180))

    assert result["first_status"] == "waiting_user"
    assert result["resolved_status"] == "finished"
    assert result["errors"] == []
    assert result["pending_tool_names"] == ["terminal"]
    assert result["tool_names"][:1] == ["terminal"]
    assert result["terminal_status"] == "success"
    assert "PULSARA_TERMINAL_ASK_OK" in result["terminal_output"]
    assert "PULSARA_TERMINAL_ASK_OK" in result["final_text"]
    assert result["model_end_count"] >= 2


def test_real_host_core_on_request_write_approval_completes(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(asyncio.wait_for(_run_real_host_core_on_request_write_approval_smoke(tmp_path), timeout=180))

    assert result["first_status"] == "waiting_user"
    assert result["resolved_status"] == "finished"
    assert result["errors"] == []
    assert result["pending_tool_names"] in (["write_file"], ["edit_file"])
    assert result["file_text"] == "PULSARA_ON_REQUEST_WRITE_OK\n"
    assert "PULSARA_ON_REQUEST_WRITE_OK" in result["final_text"]
    # Visible-but-blocked: terminal tools stay registered even with terminal=off.
    assert "terminal" in result["registry_names"]
    assert "terminal_process" in result["registry_names"]
    assert result["model_end_count"] >= 2


def test_real_host_core_plan_mode_exit_plan_round_trip(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(asyncio.wait_for(_run_real_host_core_plan_mode_smoke(tmp_path), timeout=180))

    assert result["first_status"] == "waiting_user"
    assert result["resolved_status"] == "finished"
    assert result["pending_kind"] == "exit"
    assert result["tool_names"] == ["exit_plan"]
    assert {"enter_plan", "ask_plan_question", "exit_plan"}.issubset(set(result["registry_names"]))
    assert result["plan_entered_sources"] == ["user"]
    assert result["exit_decisions"] == ["approve"]
    assert result["plan_exited_sources"] == ["approved_exit_plan"]
    assert result["accepted_plan_artifact_id"]
    assert result["plan_active_after"] is False
    assert result["mode_after_approval"] == PermissionMode.BYPASS_PERMISSIONS.value
    assert result["final_text"] == "PULSARA_PLAN_MODE_OK"
    assert result["errors"] == []


def test_real_agent_runtime_uses_active_workspace_skill(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_active_skill_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["stop_reason"] == "final"
    assert result["errors"] == []
    assert result["tool_names"] == []
    assert "PULSARA_SKILL_ACTIVE_OK" in result["final_text"]


def test_real_agent_runtime_uses_synced_bundled_skill(tmp_path, monkeypatch):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    pulsara_home = tmp_path / "pulsara-home"
    monkeypatch.setenv("PULSARA_HOME", str(pulsara_home))
    sync_result = sync_bundled_skills()

    assert any(item.name == "pulsara-skill-creator" and item.action == "installed" for item in sync_result.items)
    result = asyncio.run(_run_real_agent_synced_bundled_skill_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["stop_reason"] == "final"
    assert result["errors"] == []
    assert result["tool_names"] == []
    assert "PULSARA_BUNDLED_SKILL_ACTIVE_OK" in result["final_text"]


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


def test_real_host_core_terminal_process_survives_after_real_turn(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(asyncio.wait_for(_run_real_host_core_terminal_continuity_smoke(tmp_path), timeout=120))

    assert result["status"] == "finished"
    assert result["terminal_status_after_run"] == "running"
    assert result["terminal_status_after_kill"] == "killed"
    assert result["replay_count"] > 0
    assert "PULSARA_HOST_TERMINAL_TURN_OK" in result["final_text"]


def test_real_host_core_terminal_completion_note_drives_list_and_log(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(asyncio.wait_for(_run_real_host_core_terminal_completion_note_smoke(tmp_path), timeout=180))

    assert result["first_status"] == "finished"
    assert result["second_status"] == "finished"
    assert result["completion_event_count"] == 1
    assert result["completion_output_preview"] == "PULSARA_COMPLETION_EVENT_OUTPUT"
    assert "terminal_process" in result["second_tool_names"]
    assert result["second_terminal_process_actions"][:2] == ["list", "log"]
    assert result["log_output"] == "PULSARA_COMPLETION_EVENT_OUTPUT"
    assert "PULSARA_TERMINAL_COMPLETION_NOTE_OK" in result["second_final_text"]


def test_real_agent_runtime_terminal_yield_survives_wait_timeout(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_terminal_yield_survival_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["stop_reason"] == "final"
    assert result["errors"] == []
    assert result["tool_names"][:4] == ["terminal", "terminal_process", "terminal_process", "terminal_process"]
    assert result["terminal_status"] == "running"
    assert result["first_wait_status"] == "running"
    assert result["submit_action"] == "submit"
    assert result["second_wait_status"] == "success"
    assert "PULSARA_YIELD_SURVIVED" in result["second_wait_output"]
    assert "PULSARA_TERMINAL_YIELD_SURVIVAL_OK" in result["final_text"]


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


def test_real_agent_runtime_terminal_large_output_has_tool_result_artifact(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_agent_terminal_large_output_smoke(tmp_path))

    assert result["status"] == "finished"
    assert result["stop_reason"] == "final"
    assert result["errors"] == []
    assert result["tool_names"][:2] == ["terminal", "artifact_read"]
    assert result["terminal_status"] == "success"
    assert result["terminal_truncated"] is True
    assert result["artifact_id"]
    assert result["artifact_read_status"] == "success"
    assert result["preview_chars"] < 1000
    assert "PULSARA_LARGE_HEAD" in result["artifact_text_sample"]
    assert "PULSARA_LARGE_TAIL" in result["artifact_text_sample"]
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


def test_real_host_core_active_stop_injects_interrupted_note(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(asyncio.wait_for(_run_real_host_core_active_stop_smoke(tmp_path), timeout=180))

    assert result["stop_result_status"] == "aborted"
    assert result["first_result_status"] == "aborted"
    assert result["second_status"] == "finished"
    assert "PULSARA_ACTIVE_STOP_NOTE_OK" in result["second_final_text"]
    assert result["aborted_run_end_count"] == 1
    assert result["run_errors"] == []


def test_real_host_core_pending_approval_stop_injects_interrupted_note(tmp_path):
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(asyncio.wait_for(_run_real_host_core_pending_stop_smoke(tmp_path), timeout=180))

    assert result["first_status"] == "waiting_user"
    assert result["stop_result_status"] == "aborted"
    assert result["second_status"] == "finished"
    assert result["tool_names"] == ["terminal"]
    assert result["pending_after_stop"] is None
    assert result["interrupted_note_present"] is True
    assert "PULSARA_PENDING_STOP_NOTE_OK" in result["second_final_text"]
    assert result["aborted_run_end_count"] == 1
    assert result["run_errors"] == []


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
    tool_result_text = "\n".join(multi_tool["tool_result_summaries"])
    assert "PULSARA_MULTI_ALPHA" in tool_result_text
    assert "PULSARA_MULTI_BETA" in tool_result_text
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


def test_real_flash_accepts_aborted_unfinished_tool_recovery_context():
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    result = asyncio.run(_run_real_aborted_unfinished_tool_recovery_context_smoke())

    assert result["errors"] == []
    assert "PULSARA_ABORTED_UNFINISHED_NOTE_OK" in result["final_text"]
    assert result["note_present"] is True
    assert result["tool_name_present"] is True
    assert result["pending_not_executed_present"] is True
    assert result["dangerous_args_present"] is False
    assert result["tool_call_count"] == 0


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


async def _run_real_aborted_unfinished_tool_recovery_context_smoke() -> dict:
    ctx = EventContext(
        run_id="run:real-aborted-unfinished",
        turn_id="turn:real-aborted-unfinished/001",
        reply_id="reply:real-aborted-unfinished/001",
    )
    log = InMemoryEventLog()
    log.extend(
        [
            RunStartEvent(
                **ctx.event_fields(),
                user_input_chars=20,
                metadata={"user_input": "remove generated files"},
            ),
            ToolCallStartEvent(**ctx.event_fields(), tool_call_id="call:danger", tool_call_name="terminal"),
            ToolCallDeltaEvent(
                **ctx.event_fields(),
                tool_call_id="call:danger",
                delta='{"command": "rm -rf ./PULSARA_DANGEROUS_DO_NOT_RUN"}',
            ),
            RequireUserConfirmEvent(
                **ctx.event_fields(),
                tool_calls=[
                    ToolCallBlock(
                        id="call:danger",
                        name="terminal",
                        input='{"command": "rm -rf ./PULSARA_DANGEROUS_DO_NOT_RUN"}',
                    )
                ],
            ),
            ReplyEndEvent(**ctx.event_fields()),
            RunEndEvent(**ctx.event_fields(), status="aborted", stop_reason="aborted"),
        ]
    )
    prior_messages = rebuild_prior_messages(log)
    llm_messages = list(msg_to_llm_messages(prior_messages, LoopBudget()))
    llm_messages.append(
        LLMMessage.user(
            "If the prior context contains the Pulsara interrupted note with unfinished terminal "
            "pending approval guidance, answer exactly PULSARA_ABORTED_UNFINISHED_NOTE_OK. "
            "Do not call tools."
        )
    )
    rendered_context = "\n".join("\n".join(message.content) for message in llm_messages)
    result = await _collect_real_events(
        role=ModelRole.FLASH,
        context=LLMContext(
            messages=tuple(llm_messages),
            system_prompt="You are validating provider replay for Pulsara recovery notes. Do not call tools.",
        ),
        options=LLMOptions(temperature=0, max_output_tokens=512),
        label="real-aborted-unfinished-recovery",
    )
    return {
        **_summarize_collected_result(result),
        "note_present": INTERRUPTED_NOTE_TEXT in rendered_context,
        "tool_name_present": "terminal" in rendered_context,
        "pending_not_executed_present": "pending approval and did not execute" in rendered_context,
        "dangerous_args_present": "PULSARA_DANGEROUS_DO_NOT_RUN" in rendered_context,
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


async def _run_real_message_level_system_smoke() -> dict:
    context = LLMContext(
        messages=(
            LLMMessage.user("This is the original preserved user input."),
            LLMMessage.system(
                "Pulsara note: the previous turn did not complete. "
                "Acknowledge this by replying with exactly: PULSARA_SYSTEM_MSG_OK"
            ),
            LLMMessage.user("Continue now by following the Pulsara note exactly."),
        )
    )
    result = await _collect_real_events(
        role=ModelRole.FLASH,
        context=context,
        options=LLMOptions(temperature=0, max_output_tokens=512),
        label="real-message-level-system",
    )
    return _summarize_collected_result(result)


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


async def _run_real_agent_read_only_permission_smoke(tmp_path: Path) -> dict:
    probe = tmp_path / "probe.txt"
    probe.write_text("PULSARA_PERMISSION_READ_ONLY_OK", encoding="utf-8")
    settings = _load_settings_for_real_llm()
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=256),
        system_prompt=(
            "You are validating Pulsara read-only permissions. "
            "First call read_file on probe.txt. "
            "Do not attempt write_file, edit_file, terminal, or terminal_process. "
            "After the tool result, answer exactly with the file content and nothing else."
        ),
        permission_policy=EffectivePermissionPolicy(
            profile=PermissionProfile.READ_ONLY,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.OFF,
        ),
    )

    registry_names = agent.tool_executor.registry.names()
    result = await agent.run_task("Read probe.txt with the available tool, then answer exactly with its content.")
    events = list(agent.runtime_session.event_log.iter(run_id=result.state.run_id))
    tool_names = [
        event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "registry_names": registry_names,
        "tool_names": tool_names,
        "errors": _run_error_diagnostics(events),
    }


async def _run_real_agent_trusted_terminal_permission_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=256),
        system_prompt=(
            "You are validating Pulsara trusted-host terminal permissions. "
            "Call terminal exactly once with command exactly 'printf PULSARA_PERMISSION_TERMINAL_OK'. "
            "Do not use terminal_process or file tools. "
            "After the terminal result, answer exactly: PULSARA_PERMISSION_TERMINAL_OK"
        ),
        permission_policy=_trusted_terminal_policy(),
    )

    result = await agent.run_task("Run the trusted terminal permission validation exactly as instructed.")
    events = list(agent.runtime_session.event_log.iter(run_id=result.state.run_id))
    tool_names = [
        event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]
    tool_result_payloads = list(_tool_result_payloads_by_call_id(events).values())
    terminal_payload = next(payload for payload in tool_result_payloads if payload.get("status") == "success")
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "tool_names": tool_names,
        "terminal_status": terminal_payload["status"],
        "terminal_output": terminal_payload["output"],
        "errors": _run_error_diagnostics(events),
    }


async def _run_real_host_core_terminal_ask_approval_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    core = HostCore(settings=settings, durable=False, use_workspace_supervisor=False)
    session = await core.open_session(
        HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=tmp_path,
            memory_domain_id=f"u_real_terminal_ask_{uuid4().hex[:12]}",
        ),
        host_session_id=f"host:real-terminal-ask:{uuid4().hex[:12]}",
        conversation_id=f"conversation:real-terminal-ask:{uuid4().hex[:12]}",
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=512),
        memory_reflection=False,
        system_prompt=(
            "You are validating Pulsara terminal_access=ask approval. "
            "For the first validation request, call terminal exactly once with command exactly "
            "'printf PULSARA_TERMINAL_ASK_OK'. Do not use terminal_process or file tools. "
            "After the approved terminal tool result, answer exactly: PULSARA_TERMINAL_ASK_OK"
        ),
        permission_policy=_trusted_terminal_ask_policy(),
    )
    try:
        first = await session.run_turn("Run the terminal_access=ask validation exactly as instructed.")
        pending = session.get_pending_approval()
        pending_tool_names = [call.name for call in pending.tool_calls] if pending is not None else []
        if pending is None:
            events = session.replay_events()
            return {
                "first_status": first.status.value,
                "resolved_status": None,
                "final_text": first.final_text.strip(),
                "pending_tool_names": pending_tool_names,
                "tool_names": [
                    event.tool_call_name for event in events if isinstance(event, ToolCallStartEvent)
                ],
                "terminal_status": None,
                "terminal_output": "",
                "model_end_count": sum(isinstance(event, ModelCallEndEvent) for event in events),
                "model_end_metadata": [event.metadata for event in events if isinstance(event, ModelCallEndEvent)],
                "errors": _run_error_diagnostics(events),
            }
        resolved = await session.resolve_approval(
            ApprovalResolution(
                approval_id=pending.approval_id,
                decisions=tuple(ToolApprovalDecision(tool_call_id=call.id, confirmed=True) for call in pending.tool_calls),
            )
        )
        events = session.replay_events()
        first_run_events = [event for event in events if event.run_id == first.state.run_id]
        tool_names = [
            event.tool_call_name for event in first_run_events if isinstance(event, ToolCallStartEvent)
        ]
        payloads = _tool_result_payloads_by_call_id(first_run_events)
        terminal_payload = next((payload for payload in payloads.values() if payload.get("status") == "success"), {})
        return {
            "first_status": first.status.value,
            "resolved_status": resolved.status.value,
            "final_text": resolved.final_text.strip(),
            "pending_tool_names": pending_tool_names,
            "tool_names": tool_names,
            "terminal_status": terminal_payload.get("status"),
            "terminal_output": terminal_payload.get("output", ""),
            "model_end_count": sum(isinstance(event, ModelCallEndEvent) for event in first_run_events),
            "model_end_metadata": [event.metadata for event in first_run_events if isinstance(event, ModelCallEndEvent)],
            "errors": _run_error_diagnostics(first_run_events),
        }
    finally:
        await core.close_session(session.host_session_id)


async def _run_real_host_core_on_request_write_approval_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    target = tmp_path / "on_request.txt"
    core = HostCore(settings=settings, durable=False, use_workspace_supervisor=False)
    session = await core.open_session(
        HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=tmp_path,
            memory_domain_id=f"u_real_on_request_{uuid4().hex[:12]}",
        ),
        host_session_id=f"host:real-on-request:{uuid4().hex[:12]}",
        conversation_id=f"conversation:real-on-request:{uuid4().hex[:12]}",
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=512),
        memory_reflection=False,
        system_prompt=(
            "You are validating Pulsara approval_policy=on_request for write tools. "
            "For the first validation request, call write_file exactly once with path exactly "
            "'on_request.txt' and content exactly 'PULSARA_ON_REQUEST_WRITE_OK\\n'. "
            "Do not use edit_file, terminal, terminal_process, or read_file. "
            "After the approved write_file tool result, answer exactly: PULSARA_ON_REQUEST_WRITE_OK"
        ),
        permission_policy=_workspace_on_request_policy(),
    )
    try:
        registry_names = session.wiring.agent_runtime.tool_executor.registry.names()
        first = await session.run_turn("Run the approval_policy=on_request write validation exactly as instructed.")
        pending = session.get_pending_approval()
        pending_tool_names = [call.name for call in pending.tool_calls] if pending is not None else []
        if pending is None:
            events = session.replay_events()
            return {
                "first_status": first.status.value,
                "resolved_status": None,
                "final_text": first.final_text.strip(),
                "pending_tool_names": pending_tool_names,
                "registry_names": registry_names,
                "file_text": target.read_text(encoding="utf-8") if target.exists() else None,
                "model_end_count": sum(isinstance(event, ModelCallEndEvent) for event in events),
                "model_end_metadata": [event.metadata for event in events if isinstance(event, ModelCallEndEvent)],
                "errors": _run_error_diagnostics(events),
            }
        resolved = await session.resolve_approval(
            ApprovalResolution(
                approval_id=pending.approval_id,
                decisions=tuple(ToolApprovalDecision(tool_call_id=call.id, confirmed=True) for call in pending.tool_calls),
            )
        )
        events = session.replay_events()
        first_run_events = [event for event in events if event.run_id == first.state.run_id]
        return {
            "first_status": first.status.value,
            "resolved_status": resolved.status.value,
            "final_text": resolved.final_text.strip(),
            "pending_tool_names": pending_tool_names,
            "registry_names": registry_names,
            "file_text": target.read_text(encoding="utf-8") if target.exists() else None,
            "model_end_count": sum(isinstance(event, ModelCallEndEvent) for event in first_run_events),
            "model_end_metadata": [event.metadata for event in first_run_events if isinstance(event, ModelCallEndEvent)],
            "errors": _run_error_diagnostics(first_run_events),
        }
    finally:
        await core.close_session(session.host_session_id)


async def _run_real_host_core_plan_mode_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    core = HostCore(settings=settings, durable=False, use_workspace_supervisor=False)
    session = await core.open_session(
        HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=tmp_path,
            memory_domain_id=f"u_real_plan_{uuid4().hex[:12]}",
        ),
        host_session_id=f"host:real-plan:{uuid4().hex[:12]}",
        conversation_id=f"conversation:real-plan:{uuid4().hex[:12]}",
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=512),
        memory_reflection=False,
        system_prompt=(
            "You are validating Pulsara Plan workflow. The host will already have entered Plan mode. "
            "For the first validation request, call exit_plan exactly once. The exit_plan plan must be exactly "
            "'Return PULSARA_PLAN_MODE_OK after approval.' and summary exactly 'sentinel plan'. "
            "Do not call enter_plan, ask_plan_question, write_file, edit_file, terminal, terminal_process, or read_file. "
            "After the approved exit_plan tool result, answer exactly: PULSARA_PLAN_MODE_OK"
        ),
        permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
    )
    try:
        registry_names = session.wiring.agent_runtime.tool_executor.registry.names()
        session.enter_plan(reason="real llm plan smoke")
        first = await session.run_turn("Submit the validation plan with exit_plan exactly as instructed.")
        pending = session.get_pending_interaction()
        pending_kind = pending.kind if pending is not None else None
        if pending is None:
            events = session.replay_events()
            return {
                "first_status": first.status.value,
                "resolved_status": None,
                "final_text": first.final_text.strip(),
                "pending_kind": pending_kind,
                "tool_names": [event.tool_call_name for event in events if isinstance(event, ToolCallStartEvent)],
                "registry_names": registry_names,
                "plan_entered_sources": [
                    event.source for event in events if isinstance(event, PlanModeEnteredEvent)
                ],
                "exit_decisions": [event.decision for event in events if isinstance(event, PlanExitResolvedEvent)],
                "plan_exited_sources": [event.source for event in events if isinstance(event, PlanModeExitedEvent)],
                "accepted_plan_artifact_id": next(
                    (
                        event.accepted_plan_artifact_id
                        for event in events
                        if isinstance(event, PlanModeExitedEvent) and event.accepted_plan_artifact_id
                    ),
                    None,
                ),
                "plan_active_after": session.plan_state.active,
                "mode_after_approval": (
                    session.current_permission_mode.value if session.current_permission_mode is not None else None
                ),
                "model_end_count": sum(isinstance(event, ModelCallEndEvent) for event in events),
                "errors": _run_error_diagnostics(events),
            }
        resolved = await session.resolve_plan_interaction(
            PlanExitResolution(
                interaction_id=pending.interaction_id,
                decision="approve",
                user_feedback="approved",
            )
        )
        events = session.replay_events()
        first_run_events = [event for event in events if event.run_id == first.state.run_id]
        return {
            "first_status": first.status.value,
            "resolved_status": resolved.status.value,
            "final_text": resolved.final_text.strip(),
            "pending_kind": pending_kind,
            "tool_names": [event.tool_call_name for event in first_run_events if isinstance(event, ToolCallStartEvent)],
            "registry_names": registry_names,
            "plan_entered_sources": [
                event.source for event in first_run_events if isinstance(event, PlanModeEnteredEvent)
            ],
            "exit_decisions": [
                event.decision for event in first_run_events if isinstance(event, PlanExitResolvedEvent)
            ],
            "plan_exited_sources": [
                event.source for event in first_run_events if isinstance(event, PlanModeExitedEvent)
            ],
            "accepted_plan_artifact_id": next(
                (
                    event.accepted_plan_artifact_id
                    for event in first_run_events
                    if isinstance(event, PlanModeExitedEvent) and event.accepted_plan_artifact_id
                ),
                None,
            ),
            "plan_active_after": session.plan_state.active,
            "mode_after_approval": (
                session.current_permission_mode.value if session.current_permission_mode is not None else None
            ),
            "model_end_count": sum(isinstance(event, ModelCallEndEvent) for event in first_run_events),
            "errors": _run_error_diagnostics(first_run_events),
        }
    finally:
        await core.close_session(session.host_session_id)


async def _run_real_agent_active_skill_smoke(tmp_path: Path) -> dict:
    skill_dir = tmp_path / ".agents" / "skills" / "say-sentinel"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: say-sentinel
description: Use when asked to verify active skill injection.
---
When this skill is active, answer exactly: PULSARA_SKILL_ACTIVE_OK
""",
        encoding="utf-8",
    )
    settings = _load_settings_for_real_llm()
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=256),
        system_prompt="Do not call tools. Follow active skill instructions if present.",
        capability_resolver=LocalSkillResolver(),
    )

    result = await agent.run_task("$say-sentinel")
    events = list(agent.runtime_session.event_log.iter(run_id=result.state.run_id))
    tool_names = [
        event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "tool_names": tool_names,
        "errors": _run_error_diagnostics(events),
    }


async def _run_real_agent_synced_bundled_skill_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=64),
        system_prompt=(
            "Do not call tools. If the active skill content is visible for pulsara-skill-creator, "
            "answer exactly: PULSARA_BUNDLED_SKILL_ACTIVE_OK"
        ),
        capability_resolver=LocalSkillResolver(),
    )

    result = await agent.run_task(
        "$pulsara-skill-creator Validation only: answer exactly PULSARA_BUNDLED_SKILL_ACTIVE_OK and nothing else."
    )
    events = list(agent.runtime_session.event_log.iter(run_id=result.state.run_id))
    tool_names = [
        event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "tool_names": tool_names,
        "errors": _run_error_diagnostics(events),
    }


async def _run_real_agent_terminal_process_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    runtime_session = RuntimeSession(tmp_path)
    agent = AgentRuntime(
        runtime_session=runtime_session,
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=256),
        system_prompt=(
            "You are validating managed terminal yielded processes. "
            "First call terminal with command exactly 'sleep 60' and yield_time_ms exactly 0. Do not pass background or timeout_seconds. "
            "Then call terminal_process with action exactly 'kill' using the process_id returned by terminal. "
            "After the kill tool result, answer exactly: PULSARA_TERMINAL_PROCESS_OK"
        ),
        permission_policy=_trusted_terminal_policy(),
    )

    try:
        result = await agent.run_task("Run the terminal yielded process validation exactly as instructed.")
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
    finally:
        runtime_session.close()


async def _run_real_host_core_terminal_continuity_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    core = HostCore(settings=settings, durable=False)
    session = await core.open_session(
        HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=tmp_path,
            memory_domain_id=f"u_real_host_{uuid4().hex[:12]}",
        ),
        host_session_id=f"host:real:{uuid4().hex[:12]}",
        conversation_id=f"conversation:real:{uuid4().hex[:12]}",
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=384),
        memory_reflection=False,
        system_prompt=(
            "You are validating Pulsara HostCore multi-turn terminal continuity. "
            "Obey the user's exact step instructions. Do not pass background, timeout_seconds, session_id, "
            "notify_on_complete, or max_lifetime_seconds to terminal."
        ),
    )
    try:
        result = await session.run_turn(
            "Call terminal exactly once with command exactly 'sleep 60' and yield_time_ms exactly 0. "
            "After the terminal tool returns a running process_id, answer exactly PULSARA_HOST_TERMINAL_TURN_OK."
        )
        events = session.replay_events()
        payloads = _json_tool_result_payloads(events)
        terminal_payload = next(
            payload for payload in payloads if payload.get("process_id") and payload.get("status") == "running"
        )
        process_id = terminal_payload["process_id"]
        terminal_status_after_run = session.wiring.runtime_wiring.runtime_session.terminal_sessions.poll_process(
            process_id,
            owner_host_session_id=session.host_session_id,
        ).status.value
        killed = session.wiring.runtime_wiring.runtime_session.terminal_sessions.kill_process(
            process_id,
            owner_host_session_id=session.host_session_id,
        )
        return {
            "status": result.status.value,
            "final_text": result.final_text.strip(),
            "terminal_status_after_run": terminal_status_after_run,
            "terminal_status_after_kill": killed.status.value,
            "replay_count": len(events),
        }
    finally:
        await core.close_session(session.host_session_id)


async def _run_real_host_core_terminal_completion_note_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    core = HostCore(settings=settings, durable=False)
    session = await core.open_session(
        HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=tmp_path,
            memory_domain_id=f"u_real_completion_{uuid4().hex[:12]}",
        ),
        host_session_id=f"host:real-completion:{uuid4().hex[:12]}",
        conversation_id=f"conversation:real-completion:{uuid4().hex[:12]}",
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=768),
        memory_reflection=False,
        system_prompt=(
            "You are validating Pulsara terminal completion notes. "
            "For the first user request, call terminal exactly once with the command the user specifies, "
            "yield_time_ms exactly 0, and no unsupported terminal arguments. After the terminal tool returns a running "
            "process_id, answer exactly PULSARA_COMPLETION_TASK_STARTED. "
            "For any later request, if the conversation context contains a Pulsara note about a completed terminal "
            "background task, first call terminal_process with action exactly 'list'. Then call terminal_process with "
            "action exactly 'log' for the completed process_id from the list result. After the log result contains "
            "PULSARA_COMPLETION_EVENT_OUTPUT, answer exactly PULSARA_TERMINAL_COMPLETION_NOTE_OK."
        ),
        permission_policy=_trusted_terminal_policy(),
    )
    try:
        first = await session.run_turn(
            "Call terminal with command exactly 'sleep 0.05 && printf PULSARA_COMPLETION_EVENT_OUTPUT' "
            "and yield_time_ms exactly 0."
        )
        terminal_sessions = session.wiring.runtime_wiring.runtime_session.terminal_sessions
        processes = terminal_sessions.list_processes(owner_host_session_id=session.host_session_id)
        process_id = processes[0].process_id
        terminal_sessions.wait_process(process_id, timeout_seconds=2, owner_host_session_id=session.host_session_id)
        deadline = asyncio.get_running_loop().time() + 2
        completion_events: list[TerminalProcessCompletedEvent] = []
        while asyncio.get_running_loop().time() < deadline:
            completion_events = [
                event for event in session.replay_events() if isinstance(event, TerminalProcessCompletedEvent)
            ]
            if completion_events:
                break
            await asyncio.sleep(0.02)
        second = await session.run_turn("Continue by inspecting the completed background terminal task.")
        second_run_events = [
            event for event in session.replay_events() if event.run_id == second.state.run_id
        ]
        second_tool_names = [
            event.tool_call_name
            for event in second_run_events
            if isinstance(event, ToolCallStartEvent)
        ]
        second_payloads = _json_tool_result_payloads(second_run_events)
        terminal_process_actions = [
            payload["terminal_process_action"]
            for payload in second_payloads
            if payload.get("terminal_process_action")
        ]
        log_payload = next(
            payload for payload in second_payloads if payload.get("terminal_process_action") == "log"
        )
        return {
            "first_status": first.status.value,
            "second_status": second.status.value,
            "second_final_text": second.final_text.strip(),
            "completion_event_count": len(completion_events),
            "completion_output_preview": completion_events[0].output_preview if completion_events else "",
            "second_tool_names": second_tool_names,
            "second_terminal_process_actions": terminal_process_actions,
            "log_output": log_payload["output"],
        }
    finally:
        await core.close_session(session.host_session_id)


async def _run_real_agent_terminal_yield_survival_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    agent = AgentRuntime(
        runtime_session=RuntimeSession(tmp_path),
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=384),
        system_prompt=(
            "You are validating terminal yield survival. "
            "First call terminal with command exactly \"python -c 'import sys; print(sys.stdin.readline().strip())'\" and yield_time_ms exactly 0. "
            "Do not pass background or timeout_seconds to terminal. "
            "Then call terminal_process with action exactly 'wait', the returned process_id, and timeout_seconds exactly 1. "
            "Then call terminal_process with action exactly 'submit', the same process_id, and data exactly 'PULSARA_YIELD_SURVIVED'. "
            "Then call terminal_process with action exactly 'wait', the same process_id, and timeout_seconds exactly 3. "
            "After the second wait tool result, answer exactly: PULSARA_TERMINAL_YIELD_SURVIVAL_OK"
        ),
        permission_policy=_trusted_terminal_policy(),
    )

    result = await agent.run_task("Run the terminal yield survival validation exactly as instructed.")
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
    wait_payloads = [
        payload for payload in tool_result_payloads if payload.get("terminal_process_action") == "wait"
    ]
    submit_payload = next(
        payload for payload in tool_result_payloads if payload.get("terminal_process_action") == "submit"
    )
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "tool_names": tool_names,
        "terminal_status": terminal_payload["status"],
        "first_wait_status": wait_payloads[0]["status"],
        "submit_action": submit_payload["terminal_process_action"],
        "second_wait_status": wait_payloads[1]["status"],
        "second_wait_output": wait_payloads[1]["output"],
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
            "and yield_time_ms exactly 0. Do not pass background or timeout_seconds. "
            "Then call terminal_process with action exactly 'submit', the returned process_id, and data exactly 'PULSARA_STDIN_OK'. "
            "Then call terminal_process with action exactly 'wait' and the same process_id. "
            "After the wait tool result, answer exactly: PULSARA_TERMINAL_STDIN_OK"
        ),
        permission_policy=_trusted_terminal_policy(),
    )

    result = await agent.run_task("Run the terminal stdin validation exactly as instructed.")
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
            "First call terminal with command exactly 'python', yield_time_ms exactly 0, and tty exactly true. Do not pass background or timeout_seconds. "
            "After the first terminal call, do not call terminal again; use terminal_process for every remaining step. "
            "Then call terminal_process with action exactly 'submit', the returned process_id, "
            "and data exactly 'print(\"PULSARA_PTY_REAL_OK\")'. "
            "Then call terminal_process with action exactly 'close_stdin' and the same process_id. "
            "Then call terminal_process with action exactly 'wait' and the same process_id. "
            "After the wait tool result, answer exactly: PULSARA_TERMINAL_PTY_OK"
        ),
        permission_policy=_trusted_terminal_policy(),
    )

    result = await agent.run_task("Run the terminal PTY validation exactly as instructed.")
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
            "\"printf 'PULSARA_STREAM_REAL_FIRST\\n'; sleep 0.5; printf PULSARA_STREAM_REAL_SECOND\" "
            "and yield_time_ms exactly 2000. "
            "Do not use background, tty, terminal_process, or any file tools. "
            "After the terminal tool result, answer exactly: PULSARA_TERMINAL_STREAMING_OK"
        ),
        permission_policy=_trusted_terminal_policy(),
    )

    result = await agent.run_task("Run the terminal streaming validation exactly as instructed.")
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
            "After the terminal tool result, inspect the artifacts[] ref and call artifact_read with that artifact_id "
            "and max_chars 60000. After artifact_read shows both PULSARA_LARGE_HEAD and PULSARA_LARGE_TAIL, "
            "answer exactly: PULSARA_TERMINAL_LARGE_OUTPUT_OK"
        ),
        permission_policy=_trusted_terminal_policy(),
    )

    result = await agent.run_task("Run the terminal large-output validation exactly as instructed.")
    events = list(agent.runtime_session.event_log.iter(run_id=result.state.run_id))
    tool_names = [
        event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]
    tool_call_names = {
        event.tool_call_id: event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    }
    deltas_by_call: dict[str, list[str]] = {}
    for event in events:
        if isinstance(event, ToolResultTextDeltaEvent):
            deltas_by_call.setdefault(event.tool_call_id, []).append(event.delta)
    errors = _run_error_diagnostics(events)
    terminal_call_id = next(call_id for call_id, name in tool_call_names.items() if name == "terminal")
    artifact_read_call_id = next(call_id for call_id, name in tool_call_names.items() if name == "artifact_read")
    terminal_payload = json.loads("".join(deltas_by_call[terminal_call_id]))
    artifact_read_payload = json.loads("".join(deltas_by_call[artifact_read_call_id]))
    terminal_end = next(event for event in events if isinstance(event, ToolResultEndEvent) and event.artifacts)
    artifact_id = terminal_end.artifacts[0].artifact_id if terminal_end.artifacts else ""
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "tool_names": tool_names,
        "terminal_status": terminal_payload["status"],
        "terminal_truncated": terminal_payload["truncated"],
        "artifact_id": artifact_id,
        "preview_chars": len(terminal_payload["output"]),
        "artifact_read_status": artifact_read_payload["status"],
        "artifact_text_sample": artifact_read_payload.get("text", ""),
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
        permission_policy=_trusted_terminal_policy(),
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


async def _run_real_host_core_active_stop_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    core = HostCore(settings=settings, durable=False, use_workspace_supervisor=False)
    session = await core.open_session(
        HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=tmp_path,
            memory_domain_id=f"u_real_stop_{uuid4().hex[:12]}",
        ),
        host_session_id=f"host:real-stop:{uuid4().hex[:12]}",
        conversation_id=f"conversation:real-stop:{uuid4().hex[:12]}",
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=2048),
        memory_reflection=False,
        system_prompt=(
            "You are validating Pulsara user stop recovery. "
            "If the conversation context contains a Pulsara note saying the previous turn was stopped by the user "
            "and the user asks to continue, answer exactly PULSARA_ACTIVE_STOP_NOTE_OK. "
            "Otherwise, follow the user's current instruction."
        ),
    )
    try:
        first_task = asyncio.create_task(
            session.run_turn(
                "Begin a long validation response. Produce many numbered lines and do not finish quickly."
            )
        )
        for _ in range(50):
            if session.active_run_id is not None:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        stop_result = await session.stop_current_turn(timeout=10)
        first_result = await first_task
        second = await session.run_turn("Please continue from the stopped turn.")
        first_events = session.replay_events()
        first_run_id = first_result.state.run_id
        run_errors = _run_error_diagnostics(event for event in first_events if event.run_id == first_run_id)
        aborted_run_end_count = sum(
            1
            for event in first_events
            if event.run_id == first_run_id and isinstance(event, RunEndEvent) and event.status == "aborted"
        )
        return {
            "stop_result_status": stop_result.status.value if stop_result is not None else None,
            "first_result_status": first_result.status.value,
            "second_status": second.status.value,
            "second_final_text": second.final_text.strip(),
            "aborted_run_end_count": aborted_run_end_count,
            "run_errors": run_errors,
        }
    finally:
        await core.close_session(session.host_session_id)


async def _run_real_host_core_pending_stop_smoke(tmp_path: Path) -> dict:
    settings = _load_settings_for_real_llm()
    core = HostCore(settings=settings, durable=False, use_workspace_supervisor=False)
    session = await core.open_session(
        HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=tmp_path,
            memory_domain_id=f"u_real_pending_stop_{uuid4().hex[:12]}",
        ),
        host_session_id=f"host:real-pending-stop:{uuid4().hex[:12]}",
        conversation_id=f"conversation:real-pending-stop:{uuid4().hex[:12]}",
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=384),
        memory_reflection=False,
        system_prompt=(
            "You are validating Pulsara pending-approval stop recovery. "
            "For the first validation request, call terminal exactly once with command exactly "
            "'rm -rf ./PULSARA_PENDING_STOP_SENTINEL_DO_NOT_CREATE' and do not call any other tools. "
            "If the conversation context contains a Pulsara note saying the previous turn was stopped by the user "
            "and the user asks to continue, answer exactly PULSARA_PENDING_STOP_NOTE_OK."
        ),
        permission_policy=_trusted_terminal_policy(),
    )
    try:
        first = await session.run_turn("Run the first validation request exactly as instructed.")
        first_events = session.replay_events()
        first_run_id = first.state.run_id
        tool_names = [
            event.tool_call_name
            for event in first_events
            if event.run_id == first_run_id and isinstance(event, ToolCallStartEvent)
        ]
        stop_result = await session.stop_current_turn()
        pending_after_stop = session.get_pending_approval()
        prior_messages = rebuild_prior_messages(session.wiring.runtime_wiring.event_log)
        interrupted_note_present = any(
            INTERRUPTED_NOTE_TEXT in getattr(block, "text", "")
            for message in prior_messages
            for block in message.content
        )
        second = await session.run_turn(
            "Do not call any tools. The prior context includes the Pulsara stop note. "
            "Answer exactly PULSARA_PENDING_STOP_NOTE_OK."
        )
        events = session.replay_events()
        run_errors = _run_error_diagnostics(event for event in events if event.run_id == first_run_id)
        aborted_run_end_count = sum(
            1
            for event in events
            if event.run_id == first_run_id and isinstance(event, RunEndEvent) and event.status == "aborted"
        )
        return {
            "first_status": first.status.value,
            "stop_result_status": stop_result.status.value if stop_result is not None else None,
            "second_status": second.status.value,
            "second_final_text": second.final_text.strip(),
            "tool_names": tool_names,
            "pending_after_stop": pending_after_stop,
            "aborted_run_end_count": aborted_run_end_count,
            "interrupted_note_present": interrupted_note_present,
            "run_errors": run_errors,
        }
    finally:
        await core.close_session(session.host_session_id)


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
    message = result["message"]
    replayed_text = "".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    ).strip()
    return {
        "event_type_names": [type(event).__name__ for event in result["events"]],
        "text": result["text"],
        "final_text": result["text"],
        "replayed_text": replayed_text,
        "thinking": result["thinking"],
        "errors": result["errors"],
        "tool_call_count": sum(1 for event in result["events"] if isinstance(event, ToolCallStartEvent)),
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


def _tool_result_payloads_by_call_id(events) -> dict[str, dict]:
    deltas_by_call: dict[str, list[str]] = {}
    for event in events:
        if isinstance(event, ToolResultTextDeltaEvent):
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


def _json_tool_result_payloads(events) -> list[dict]:
    payloads: list[dict] = []
    for event in events:
        if not isinstance(event, ToolResultTextDeltaEvent):
            continue
        try:
            payload = json.loads(event.delta)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


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
