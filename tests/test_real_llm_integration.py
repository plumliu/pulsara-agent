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
)
from pulsara_agent.event.candidates import PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.event_log import InMemoryEventLog, PostgresEventLog
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.llm import LLMMessage, ModelRole, ToolSpec, build_llm_runtime
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory import (
    InMemoryArchiveStore,
    InMemoryCandidatePool,
    MemoryGovernanceEngine,
    MemoryGovernanceExecutor,
    MemoryGovernanceOptions,
    RunTimelinePersistenceHook,
    load_run_timeline,
    summarize_run_timeline,
)
from pulsara_agent.memory.candidate_pool import CandidateOrigin, PooledMemoryCandidate
from pulsara_agent.memory.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.reflection import MemoryReflectionEngine, MemoryReflectionHint, MemoryReflectionOptions
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.memory.write_service import MemoryWriteService
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
    assert result["tool_arguments"]["statement"] == "The user prefers compact status updates"
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
            "scope='ctx:project', source_authority='explicit_user_instruction', and "
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
            "scope='ctx:workspace', source_authority='explicit_user_instruction', and "
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
            "scope='ctx:project', applies_when='working in a git repository', "
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
            "scope='ctx:project', source_authority='explicit_user_instruction', and "
            "verification_status='user_confirmed'. Do not include based_on_ids. "
            "Then answer exactly: PULSARA_MEMORY_DECISION_OK"
        ),
        "user_input": "Remember this project decision: use type-specific remember tools instead of propose_memory.",
    },
)


async def _run_real_agent_remember_tool_rollout(tmp_path: Path, case: dict) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = _load_settings_for_real_llm()
    graph_id = f"graph:real-llm/{uuid4().hex}"
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=True,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=256),
        system_prompt=case["system_prompt"],
        graph_id=graph_id,
    )
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


def _artifact_id_from_node_ref(node_id: str) -> str:
    prefix = "urn:pulsara:"
    if node_id.startswith(prefix):
        return urllib.parse.unquote(node_id[len(prefix) :])
    return node_id
