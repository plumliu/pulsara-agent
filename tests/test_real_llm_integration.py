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
    errors: list[str] = []

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
            errors.append(event.message)

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
    events = agent.runtime_session.event_log.iter(run_id=result.state.run_id)
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
    errors = [event.message for event in events if isinstance(event, RunErrorEvent)]
    return {
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_text": result.final_text.strip(),
        "tool_call_ids": tool_call_ids,
        "tool_result_ids": tool_result_ids,
        "errors": errors,
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
    domain = MemoryDomainContext(
        memory_domain_id="u_real_scope",
        workspace_kind="project",
        stable_project_key="repo_visible",
    )
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
            "ctx:workspace/repo_visible",
            "The domain scope sentinel says visible workspace memory.",
        ),
        (
            "preference:real-domain-hidden",
            "ctx:workspace/other_repo",
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
        errors = [event.message for event in events if isinstance(event, RunErrorEvent)]
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
        errors = [event.message for event in events if isinstance(event, RunErrorEvent)]
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
            "scope='ctx:workspace/test_project', source_authority='explicit_user_instruction', and "
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
            "scope='ctx:workspace/test_project', source_authority='explicit_user_instruction', and "
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
            "scope='ctx:workspace/test_project', applies_when='working in a git repository', "
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
            "scope='ctx:workspace/test_project', source_authority='explicit_user_instruction', and "
            "verification_status='user_confirmed'. Do not include based_on_ids. "
            "Then answer exactly: PULSARA_MEMORY_DECISION_OK"
        ),
        "user_input": "Remember this project decision: use type-specific remember tools instead of propose_memory.",
    },
)


async def _run_real_agent_remember_tool_rollout(tmp_path: Path, case: dict) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = _load_settings_for_real_llm()
    memory_domain = MemoryDomainContext(
        memory_domain_id=f"u_real_memory_tools_{uuid4().hex[:12]}",
        workspace_kind="project",
        stable_project_key="test_project",
    )
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=256),
        system_prompt=case["system_prompt"],
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
        errors = [event.message for event in events if isinstance(event, RunErrorEvent)]
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
    errors: list[str] = []

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
            errors.append(event.message)

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
        return {
            "error_type": result.error_type,
            "error_message": result.error_message,
            "decision_kinds": [decision.kind for decision in result.decisions],
            "recorded_decision_kind": result.applied[0].decision_record.decision.kind if result.applied else None,
            "applied_count": len(result.applied),
            "target_entry_id": pooled.entry_id,
            "old_status": old_doc.get(memory.STATUS.name),
            "new_status": new_doc.get(memory.STATUS.name),
            "new_id": new_id,
            "superseded_memory_ids": list(getattr(write_outcome, "superseded_memory_ids", ())),
            "supersedes_edge_present": {"@id": old_id} in new_doc.get(memory.SUPERSEDES.name, []),
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
