import asyncio
import os
import urllib.parse
from pathlib import Path
from uuid import uuid4

import pytest

from pulsara_agent.event import (
    EventContext,
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
from pulsara_agent.event_log import InMemoryEventLog, PostgresEventLog
from pulsara_agent.graph import InMemoryGraphStore, OxigraphGraphStore
from pulsara_agent.llm import LLMMessage, ModelRole, ToolSpec, build_llm_runtime
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory import (
    PostgresArtifactStore,
    RunTimelinePersistenceHook,
    load_run_timeline,
    summarize_run_timeline,
)
from pulsara_agent.message import TextBlock, ThinkingBlock, ToolCallBlock
from pulsara_agent.ontology import memory
from pulsara_agent.runtime import AgentRuntime, RuntimeSession
from pulsara_agent.settings import PulsaraSettings


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
    archive = PostgresArtifactStore(dsn=settings.storage.postgres_dsn)
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

    timeline_blob_prefix: str | None = None
    try:
        result = await agent.run_task("Read probe.txt with the tool, then answer with exactly its content.")
        timeline_blob_prefix = f"timeline:{runtime_session.runtime_session_id}:{result.state.run_id}:"
        records = graph.find_by_type(memory.RUN_TIMELINE)
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
    finally:
        if timeline_blob_prefix is not None:
            _delete_postgres_artifacts_with_prefix(settings.storage.postgres_dsn, timeline_blob_prefix)


async def _run_real_agent_postgres_event_log_timeline_smoke(tmp_path: Path) -> dict:
    probe = tmp_path / "probe.txt"
    probe.write_text("PULSARA_POSTGRES_CHAIN_OK", encoding="utf-8")
    settings = _load_settings_for_real_llm()
    runtime_session = RuntimeSession(tmp_path)
    event_log = PostgresEventLog(
        dsn=settings.storage.postgres_dsn,
        runtime_session_id=runtime_session.runtime_session_id,
        workspace_root=tmp_path,
    )
    runtime_session.event_log = event_log
    graph = OxigraphGraphStore(settings.storage.oxigraph_url)
    graph_id = f"graph:real-llm/{uuid4().hex}"
    archive = PostgresArtifactStore(dsn=settings.storage.postgres_dsn)
    runtime_session.hook_manager.register_event(
        None,
        RunTimelinePersistenceHook(
            graph=graph,
            archive=archive,
            event_store=runtime_session.event_log,
            graph_id=graph_id,
        ),
    )
    agent = AgentRuntime(
        runtime_session=runtime_session,
        llm_runtime=build_llm_runtime(settings.llm),
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=128),
        system_prompt=(
            "You are validating durable runtime event persistence. "
            "First call read_file on probe.txt. "
            "Then answer with exactly the file content and nothing else."
        ),
    )

    timeline_blob_id: str | None = None
    timeline_blob_prefix: str | None = None
    try:
        result = await agent.run_task("Read probe.txt with the tool, then answer with exactly its content.")
        timeline_blob_prefix = f"timeline:{runtime_session.runtime_session_id}:{result.state.run_id}:"
        records = graph.find_by_type(memory.RUN_TIMELINE, graph_id=graph_id)
        timeline_blob_id = _artifact_id_from_node_ref(records[0][memory.STORED_AS.name]["@id"])
        timeline = load_run_timeline(
            graph=graph,
            archive=archive,
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
            "timeline_artifact_text": archive.get_text(timeline_blob_id),
        }
    finally:
        graph.delete_graph(graph_id)
        if timeline_blob_prefix is not None:
            _delete_postgres_artifacts_with_prefix(settings.storage.postgres_dsn, timeline_blob_prefix)
        _delete_postgres_runtime_session(settings.storage.postgres_dsn, runtime_session.runtime_session_id)


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


def _artifact_id_from_node_ref(node_id: str) -> str:
    prefix = "urn:pulsara:"
    if node_id.startswith(prefix):
        return urllib.parse.unquote(node_id[len(prefix) :])
    return node_id
