from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable
from pathlib import Path

import psycopg
import pytest

from pulsara_agent.event import (
    EventContext,
    ModelCallStartEvent,
    SubagentEdgeRecordedEvent,
    SubagentResultDeliveredEvent,
    SubagentRunCompletedEvent,
    SubagentRunStartedEvent,
    ToolCallStartEvent,
)
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.runtime import LoopBudget
from pulsara_agent.runtime.permission import PermissionMode, preset_to_policy
from pulsara_agent.runtime.wiring import build_agent_runtime_wiring
from pulsara_agent.settings import PulsaraSettings


pytestmark = pytest.mark.real_llm


CHILD_SENTINEL = "PULSARA_REAL_SUBAGENT_CHILD_OK"
PARENT_SENTINEL = "PULSARA_REAL_SUBAGENT_PARENT_OK"
BACKGROUND_CHILD_SENTINEL = "PULSARA_REAL_SUBAGENT_BACKGROUND_CHILD_OK"
BACKGROUND_PARENT_SENTINEL = "PULSARA_REAL_SUBAGENT_BACKGROUND_PARENT_OK"


def test_real_llm_subagent_spawn_wait_dogfood(tmp_path: Path) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")
    if os.getenv("PULSARA_RUN_DOGFOOD_SUBAGENT") != "1":
        pytest.skip("Set PULSARA_RUN_DOGFOOD_SUBAGENT=1 to run subagent real LLM dogfood.")

    settings = _settings()
    result = asyncio.run(_run_subagent_spawn_wait_dogfood(settings, tmp_path))
    print("\nREAL_LLM_SUBAGENT_DOGFOOD=" + json.dumps(result, ensure_ascii=False, sort_keys=True))

    assert result["status"] == "finished", result
    assert result["spawn_tool_calls"] >= 1, result
    assert result["wait_tool_calls"] >= 1, result
    assert result["subagent_started_count"] == 1, result
    assert result["subagent_completed_count"] == 1, result
    assert result["wait_edge_count"] >= 1, result
    assert result["started_parent_context_id"], result
    assert result["started_parent_model_call_index"] is not None, result
    assert result["wait_source_context_id"], result
    assert result["wait_source_model_call_index"] is not None, result
    assert result["child_raw_event_count"] > 0, result
    assert result["child_raw_events_missing_subagent_metadata"] == 0, result
    assert CHILD_SENTINEL in result["child_summary"], result
    assert PARENT_SENTINEL in result["final_text"], result
    assert all(
        item["context_id"] and item["model_call_index"] is not None
        for item in result["delivered_events"]
    ), result


def test_real_llm_subagent_spawn_wait_durable_dogfood(tmp_path: Path) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")
    if os.getenv("PULSARA_RUN_DOGFOOD_SUBAGENT_DURABLE") != "1":
        pytest.skip(
            "Set PULSARA_RUN_DOGFOOD_SUBAGENT_DURABLE=1 to run the durable subagent real LLM dogfood."
        )

    settings = _settings()
    _connect_or_skip(settings.storage.postgres_dsn)
    result = asyncio.run(_run_subagent_spawn_wait_dogfood(settings, tmp_path, durable=True))
    print("\nREAL_LLM_SUBAGENT_DURABLE_DOGFOOD=" + json.dumps(result, ensure_ascii=False, sort_keys=True))

    assert result["status"] == "finished", result
    assert result["parent_event_log_backend"] == "PostgresEventLog", result
    assert result["child_event_log_backend"] == "PostgresEventLog", result
    assert result["spawn_tool_calls"] >= 1, result
    assert result["wait_tool_calls"] >= 1, result
    assert result["subagent_started_count"] == 1, result
    assert result["subagent_completed_count"] == 1, result
    assert result["wait_edge_count"] >= 1, result
    assert result["started_parent_context_id"], result
    assert result["started_parent_model_call_index"] is not None, result
    assert result["wait_source_context_id"], result
    assert result["wait_source_model_call_index"] is not None, result
    assert result["child_runtime_session_id"], result
    assert result["child_raw_event_count"] > 0, result
    assert result["child_raw_events_missing_subagent_metadata"] == 0, result
    assert CHILD_SENTINEL in result["child_summary"], result
    assert PARENT_SENTINEL in result["final_text"], result
    assert all(
        item["context_id"] and item["model_call_index"] is not None
        for item in result["delivered_events"]
    ), result


def test_real_llm_subagent_background_result_delivery_dogfood(tmp_path: Path) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")
    if os.getenv("PULSARA_RUN_DOGFOOD_SUBAGENT") != "1":
        pytest.skip("Set PULSARA_RUN_DOGFOOD_SUBAGENT=1 to run subagent real LLM dogfood.")

    settings = _settings()
    result = asyncio.run(_run_subagent_background_delivery_dogfood(settings, tmp_path))
    print(
        "\nREAL_LLM_SUBAGENT_BACKGROUND_DOGFOOD="
        + json.dumps(result, ensure_ascii=False, sort_keys=True)
    )

    assert result["status"] == "finished", result
    assert BACKGROUND_PARENT_SENTINEL in result["final_text"], result
    assert BACKGROUND_CHILD_SENTINEL in result["final_text"], result
    assert result["tool_names"] == [], result
    assert result["subagent_completed_count"] == 1, result
    assert len(result["delivered_events"]) == 1, result
    delivered = result["delivered_events"][0]
    assert delivered["context_id"], result
    assert delivered["model_call_index"] is not None, result
    assert delivered["section_id"] == "subagent:results", result
    assert result["child_raw_events_missing_subagent_metadata"] == 0, result


async def _run_subagent_spawn_wait_dogfood(
    settings: PulsaraSettings,
    tmp_path: Path,
    *,
    durable: bool = False,
) -> dict[str, object]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    wiring = _build_real_subagent_wiring(
        settings=settings,
        workspace_root=workspace_root,
        system_prompt=_spawn_wait_system_prompt(),
        durable=durable,
    )
    wiring.agent_runtime.budget = LoopBudget(
        max_turns=12,
        max_tool_calls=12,
        max_consecutive_model_failures=2,
        max_consecutive_tool_failures=4,
        max_subagent_results_per_parent_compile=0,
    )
    try:
        run_result = await wiring.agent_runtime.run_task(_user_prompt())
        subagent_runtime = wiring.agent_runtime.subagent_runtime
        assert subagent_runtime is not None
        parent_events = wiring.runtime_wiring.event_log.iter(run_id=run_result.state.run_id)
        started = [event for event in parent_events if isinstance(event, SubagentRunStartedEvent)]
        completed = [event for event in parent_events if isinstance(event, SubagentRunCompletedEvent)]
        wait_edges = [
            event
            for event in parent_events
            if isinstance(event, SubagentEdgeRecordedEvent) and event.edge_kind == "wait"
        ]
        delivered = [event for event in parent_events if isinstance(event, SubagentResultDeliveredEvent)]
        tool_names = [
            event.tool_call_name
            for event in parent_events
            if isinstance(event, ToolCallStartEvent)
        ]
        model_starts = [event for event in parent_events if isinstance(event, ModelCallStartEvent)]
        child_raw_events = []
        child_runtime_session_id = started[0].child_runtime_session_id if started else None
        child_event_log_backend = None
        if started:
            child_session = subagent_runtime.child_runtime_session(started[0].subagent_run_id)
            child_event_log_backend = type(child_session.event_log).__name__
            child_raw_events = child_session.event_log.iter()
        child_metadata_missing = [
            event
            for event in child_raw_events
            if not isinstance(event.metadata.get("subagent"), dict)
            or not event.metadata["subagent"].get("subagent_run_id")
            or not event.metadata["subagent"].get("parent_runtime_session_id")
        ]
        return {
            "status": run_result.status.value,
            "stop_reason": run_result.stop_reason,
            "error_message": run_result.error_message,
            "final_text": run_result.final_text,
            "parent_runtime_session_id": wiring.runtime_wiring.runtime_session.runtime_session_id,
            "child_runtime_session_id": child_runtime_session_id,
            "parent_event_log_backend": type(wiring.runtime_wiring.event_log).__name__,
            "child_event_log_backend": child_event_log_backend,
            "tool_names": tool_names,
            "spawn_tool_calls": tool_names.count("spawn_agent"),
            "wait_tool_calls": tool_names.count("wait_agent"),
            "parent_model_call_starts": [
                {
                    "context_id": event.context_id,
                    "model_call_index": event.model_call_index,
                }
                for event in model_starts
            ],
            "subagent_started_count": len(started),
            "subagent_completed_count": len(completed),
            "wait_edge_count": len(wait_edges),
            "started_parent_context_id": started[0].parent_context_id if started else None,
            "started_parent_model_call_index": started[0].parent_model_call_index if started else None,
            "wait_source_context_id": wait_edges[-1].source_context_id if wait_edges else None,
            "wait_source_model_call_index": wait_edges[-1].source_model_call_index if wait_edges else None,
            "child_summary": completed[-1].summary if completed else "",
            "child_raw_event_count": len(child_raw_events),
            "child_raw_events_missing_subagent_metadata": len(child_metadata_missing),
            "delivered_events": [
                {
                    "context_id": event.context_id,
                    "model_call_index": event.model_call_index,
                    "section_id": event.section_id,
                }
                for event in delivered
            ],
        }
    finally:
        child_session_ids = [
            run.child_runtime_session_id
            for run in (wiring.agent_runtime.subagent_runtime.runs if wiring.agent_runtime.subagent_runtime else ())
        ]
        wiring.agent_runtime.close()
        if durable:
            _delete_sessions(
                settings.storage.postgres_dsn,
                [*child_session_ids, wiring.runtime_wiring.runtime_session.runtime_session_id],
            )


async def _run_subagent_background_delivery_dogfood(
    settings: PulsaraSettings,
    tmp_path: Path,
) -> dict[str, object]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    wiring = _build_real_subagent_wiring(
        settings=settings,
        workspace_root=workspace_root,
        system_prompt=_background_delivery_system_prompt(),
    )
    subagent_runtime = wiring.agent_runtime.subagent_runtime
    assert subagent_runtime is not None
    seed_context = EventContext(run_id="run:seed", turn_id="turn:seed", reply_id="reply:seed")
    seeded = await subagent_runtime.spawn_fake(
        task="seeded background child result",
        event_context=seed_context,
    )
    await subagent_runtime.complete_fake(
        seeded.subagent_run_id,
        summary=BACKGROUND_CHILD_SENTINEL,
        output_preview=BACKGROUND_CHILD_SENTINEL,
        event_context=seed_context,
    )
    try:
        run_result = await wiring.agent_runtime.run_task(_background_delivery_user_prompt())
        parent_events = wiring.runtime_wiring.event_log.iter(run_id=run_result.state.run_id)
        completed = [
            event
            for event in wiring.runtime_wiring.event_log.iter()
            if isinstance(event, SubagentRunCompletedEvent)
        ]
        delivered = [event for event in parent_events if isinstance(event, SubagentResultDeliveredEvent)]
        tool_names = [
            event.tool_call_name
            for event in parent_events
            if isinstance(event, ToolCallStartEvent)
        ]
        child_session = subagent_runtime.child_runtime_session(seeded.subagent_run_id)
        child_raw_events = child_session.event_log.iter()
        child_metadata_missing = [
            event
            for event in child_raw_events
            if not isinstance(event.metadata.get("subagent"), dict)
            or not event.metadata["subagent"].get("subagent_run_id")
            or not event.metadata["subagent"].get("parent_runtime_session_id")
        ]
        return {
            "status": run_result.status.value,
            "stop_reason": run_result.stop_reason,
            "error_message": run_result.error_message,
            "final_text": run_result.final_text,
            "tool_names": tool_names,
            "subagent_completed_count": len(completed),
            "child_raw_event_count": len(child_raw_events),
            "child_raw_events_missing_subagent_metadata": len(child_metadata_missing),
            "delivered_events": [
                {
                    "context_id": event.context_id,
                    "model_call_index": event.model_call_index,
                    "section_id": event.section_id,
                }
                for event in delivered
            ],
        }
    finally:
        wiring.agent_runtime.close()


def _build_real_subagent_wiring(
    *,
    settings: PulsaraSettings,
    workspace_root: Path,
    system_prompt: str,
    durable: bool = False,
):
    return build_agent_runtime_wiring(
        settings,
        workspace_root,
        durable=durable,
        model_role=ModelRole.FLASH,
        options=LLMOptions(temperature=0, max_output_tokens=1024),
        system_prompt=system_prompt,
        memory_reflection=False,
        permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
    )


def _spawn_wait_system_prompt() -> str:
    return f"""
You are running a Pulsara real-LLM subagent runtime dogfood.

Strict rules:
- If the user/task starts with "CHILD_SUBAGENT_TASK", do not call tools. Reply exactly:
  {CHILD_SENTINEL}
- Otherwise, you are the parent orchestrator. You must call spawn_agent exactly once with:
  task = "CHILD_SUBAGENT_TASK: reply exactly {CHILD_SENTINEL}"
  role = "worker"
  context = "isolated"
- After spawn_agent returns, call wait_agent for the returned subagent_run_id.
- After wait_agent returns a completed result containing {CHILD_SENTINEL}, final-answer exactly:
  {PARENT_SENTINEL} {CHILD_SENTINEL}
- Do not use terminal, read_file, memory tools, or any other tools in this dogfood.
""".strip()


def _user_prompt() -> str:
    return (
        "Run the Pulsara subagent dogfood now. "
        f"Spawn the child, wait for it, and finish with {PARENT_SENTINEL} {CHILD_SENTINEL}."
    )


def _background_delivery_system_prompt() -> str:
    return f"""
You are running a Pulsara real-LLM subagent background-result dogfood.
Do not call any tools.
If the runtime-provided context contains {BACKGROUND_CHILD_SENTINEL}, final-answer exactly:
{BACKGROUND_PARENT_SENTINEL} {BACKGROUND_CHILD_SENTINEL}
If the runtime-provided context does not contain it, final-answer exactly:
MISSING_SUBAGENT_BACKGROUND_CONTEXT
""".strip()


def _background_delivery_user_prompt() -> str:
    return (
        "Read the runtime-provided subagent result context and answer with the required sentinel. "
        "Do not call tools."
    )


def _settings() -> PulsaraSettings:
    env_file = os.getenv("PULSARA_REAL_LLM_ENV_FILE")
    if env_file:
        return PulsaraSettings.from_env_file(env_file)
    path = Path(".env")
    if path.exists():
        return PulsaraSettings.from_env_file(path)
    return PulsaraSettings.from_env()


def _connect_or_skip(dsn: str) -> None:
    try:
        with psycopg.connect(dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select 1")
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def _delete_sessions(dsn: str, runtime_session_ids: Iterable[str | None]) -> None:
    session_ids = [session_id for session_id in dict.fromkeys(runtime_session_ids) if session_id]
    if not session_ids:
        return
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            for runtime_session_id in session_ids:
                cursor.execute("delete from sessions where id = %s", (runtime_session_id,))
