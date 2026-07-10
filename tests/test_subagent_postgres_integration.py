from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from tests.conftest import run_start_permission_fields

from pulsara_agent.event import (
    EventContext,
    RunEndEvent,
    RunStartEvent,
    SubagentMessageSentEvent,
    SubagentPhaseReportedEvent,
    SubagentResultSubmittedEvent,
    SubagentRunStartedEvent,
    TextBlockDeltaEvent,
)
from pulsara_agent.event_log import InMemoryEventLog, PostgresEventLog, dump_agent_event
from pulsara_agent.inspector import InspectorService, PostgresInspectorStore
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.runtime import RuntimeSession
from pulsara_agent.runtime.subagent import (
    PostgresEventLogLocator,
    SubagentGraphHydrator,
    SubagentRuntime,
    fold_subagent_graph,
    project_subagent_graph,
)
from pulsara_agent.runtime.tool_artifacts import PostgresToolResultArtifactIndex
from pulsara_agent.settings import StorageConfig


class _FailingObserver:
    async def on_published_event(self, _published) -> None:
        raise RuntimeError("synthetic observer failure")


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def _delete_sessions(dsn: str, session_ids: list[str]) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from sessions where id = any(%s)", (session_ids,))


def _durable_runtime(tmp_path: Path):
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    parent_session_id = f"runtime:subagent-postgres-parent:{uuid4().hex}"
    parent_log = PostgresEventLog(
        dsn=dsn,
        runtime_session_id=parent_session_id,
        workspace_root=tmp_path,
    )
    parent = RuntimeSession(
        tmp_path,
        event_log=parent_log,
        archive=PostgresArtifactStore(dsn),
        tool_result_artifacts=PostgresToolResultArtifactIndex(dsn),
        runtime_session_id=parent_session_id,
    )
    locator = PostgresEventLogLocator(dsn=dsn, workspace_root=tmp_path)
    runtime = SubagentRuntime(
        parent_runtime_session=parent,
        child_event_log_factory=locator.event_log_for_runtime_session,
        event_log_locator=locator,
    )
    context = EventContext(
        run_id=f"run:subagent-postgres:{uuid4().hex}",
        turn_id=f"turn:subagent-postgres:{uuid4().hex}",
        reply_id=f"reply:subagent-postgres:{uuid4().hex}",
    )
    return dsn, parent, locator, runtime, context


async def _start_parent_run(parent: RuntimeSession, context: EventContext) -> None:
    await parent.write_event(
        RunStartEvent(
            **context.event_fields(),
            **run_start_permission_fields(context.run_id),
            user_input_chars=8,
            metadata={"user_input": "delegate"},
        )
    )


def test_postgres_parent_graph_and_child_raw_events_use_distinct_sessions(
    tmp_path: Path,
) -> None:
    dsn, parent, _locator, runtime, context = _durable_runtime(tmp_path)
    child_session_id: str | None = None
    try:
        async def run() -> None:
            nonlocal child_session_id
            await _start_parent_run(parent, context)
            child = await runtime.spawn_fake(task="durable child", event_context=context)
            child_session_id = child.child_runtime_session_id
            child_session = runtime.child_runtime_session(child.subagent_run_id)
            child_context = EventContext(
                run_id=f"run:child:{uuid4().hex}",
                turn_id=f"turn:child:{uuid4().hex}",
                reply_id=f"reply:child:{uuid4().hex}",
            )
            await child_session.write_events(
                (
                    RunStartEvent(
                        **child_context.event_fields(),
                        **run_start_permission_fields(child_context.run_id),
                        user_input_chars=5,
                    ),
                    RunEndEvent(
                        **child_context.event_fields(),
                        status="finished",
                        stop_reason="final",
                    ),
                )
            )

        asyncio.run(run())
        assert child_session_id is not None
        parent_events = parent.event_log.iter()
        child_events = PostgresEventLog(
            dsn=dsn,
            runtime_session_id=child_session_id,
            workspace_root=tmp_path,
        ).iter()
        assert any(isinstance(event, SubagentRunStartedEvent) for event in parent_events)
        assert any(isinstance(event, SubagentMessageSentEvent) for event in parent_events)
        assert not any(isinstance(event, RunStartEvent) and event.run_id.startswith("run:child:") for event in parent_events)
        assert [type(event) for event in child_events] == [RunStartEvent, RunEndEvent]
        assert not any(isinstance(event, SubagentRunStartedEvent) for event in child_events)
    finally:
        _delete_sessions(
            dsn,
            [session_id for session_id in (child_session_id, parent.runtime_session_id) if session_id],
        )


def test_postgres_child_report_events_keep_parent_spawn_context(
    tmp_path: Path,
) -> None:
    dsn, parent, _locator, runtime, context = _durable_runtime(tmp_path)
    child_session_id: str | None = None
    try:
        async def run() -> None:
            nonlocal child_session_id
            await _start_parent_run(parent, context)
            child = await runtime.spawn_fake(task="durable child report", event_context=context)
            child_session_id = child.child_runtime_session_id
            child_context = EventContext(
                run_id=f"run:child-report:{uuid4().hex}",
                turn_id=f"turn:child-report:{uuid4().hex}",
                reply_id=f"reply:child-report:{uuid4().hex}",
            )
            child_session = runtime.child_runtime_session(child.subagent_run_id)
            await child_session.write_event(
                RunStartEvent(
                    **child_context.event_fields(),
                    **run_start_permission_fields(child_context.run_id, source="child_profile"),
                    user_input_chars=5,
                )
            )
            await runtime.report_phase(
                child.subagent_run_id,
                phase="reporting",
                event_context=child_context,
                source_tool_call_id="tool:phase",
            )
            await runtime.submit_result(
                child.subagent_run_id,
                summary="durable explicit result",
                event_context=child_context,
                source_tool_call_id="tool:result",
            )

        asyncio.run(run())
        graph_events = [
            event
            for event in parent.event_log.iter()
            if isinstance(event, (SubagentPhaseReportedEvent, SubagentResultSubmittedEvent))
        ]
        assert len(graph_events) == 2
        assert all(event.run_id == context.run_id for event in graph_events)
        assert all(event.turn_id == context.turn_id for event in graph_events)
        assert all(event.reply_id == context.reply_id for event in graph_events)
    finally:
        _delete_sessions(
            dsn,
            [session_id for session_id in (child_session_id, parent.runtime_session_id) if session_id],
        )


def test_event_log_deterministic_contract_matches_in_memory_and_postgres(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:event-log-parity:{uuid4().hex}"
    postgres = PostgresEventLog(
        dsn=dsn,
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    memory = InMemoryEventLog()
    context = EventContext(
        run_id=f"run:event-log-parity:{uuid4().hex}",
        turn_id=f"turn:event-log-parity:{uuid4().hex}",
        reply_id=f"reply:event-log-parity:{uuid4().hex}",
    )
    events = tuple(
        TextBlockDeltaEvent(
            **context.event_fields(),
            block_id="text:parity",
            delta=label,
        )
        for label in ("one", "two", "three")
    )
    try:
        memory_stored = memory.extend(events, expected_last_sequence=0)
        postgres_stored = postgres.extend(events, expected_last_sequence=0)
        assert [event.sequence for event in memory_stored] == [1, 2, 3]
        assert [dump_agent_event(event) for event in memory_stored] == [
            dump_agent_event(event) for event in postgres_stored
        ]
    finally:
        _delete_sessions(dsn, [runtime_session_id])


def test_postgres_fresh_locator_hydrates_child_native_run_id(tmp_path: Path) -> None:
    dsn, parent, _locator, runtime, context = _durable_runtime(tmp_path)
    child_session_id: str | None = None
    try:
        async def seed() -> tuple[str, str]:
            nonlocal child_session_id
            await _start_parent_run(parent, context)
            child = await runtime.spawn_fake(task="durable child", event_context=context)
            child_session_id = child.child_runtime_session_id
            native_run_id = f"run:child-native:{uuid4().hex}"
            native_context = EventContext(
                run_id=native_run_id,
                turn_id=f"turn:child-native:{uuid4().hex}",
                reply_id=f"reply:child-native:{uuid4().hex}",
            )
            child_session = runtime.child_runtime_session(child.subagent_run_id)
            await child_session.write_events(
                (
                    RunStartEvent(
                        **native_context.event_fields(),
                        **run_start_permission_fields(native_run_id),
                        user_input_chars=5,
                    ),
                    RunEndEvent(
                        **native_context.event_fields(),
                        status="finished",
                        stop_reason="final",
                    ),
                )
            )
            await runtime.complete_fake(
                child.subagent_run_id,
                summary="done",
                event_context=context,
                child_run_id=native_run_id,
            )
            return child.subagent_run_id, native_run_id

        subagent_run_id, native_run_id = asyncio.run(seed())
        fresh_locator = PostgresEventLogLocator(dsn=dsn, workspace_root=tmp_path)
        state = fold_subagent_graph(parent.event_log.iter())
        hydrator = SubagentGraphHydrator(
            archive=PostgresArtifactStore(dsn),
            parent_runtime_session_id=parent.runtime_session_id,
            event_log_locator=fresh_locator,
        )
        view = asyncio.run(
            hydrator.hydrate_run(
                state.runs[subagent_run_id],
                include_task_text=False,
                include_child_native=True,
                max_chars=1_000,
            )
        )
        assert view.child_run_id == native_run_id
        assert view.child_terminal_status == "finished"
        assert view.diagnostics == ()
    finally:
        _delete_sessions(
            dsn,
            [session_id for session_id in (child_session_id, parent.runtime_session_id) if session_id],
        )


def test_postgres_subagent_graph_survives_observer_failure_and_restart(
    tmp_path: Path,
) -> None:
    dsn, parent, _locator, runtime, context = _durable_runtime(tmp_path)
    child_session_id: str | None = None
    try:
        async def seed() -> str:
            nonlocal child_session_id
            await _start_parent_run(parent, context)
            parent.publisher.subscribe(_FailingObserver())
            child = await runtime.spawn_fake(task="observer failure", event_context=context)
            child_session_id = child.child_runtime_session_id
            await runtime.complete_fake(
                child.subagent_run_id,
                summary="durable completion",
                event_context=context,
            )
            return child.subagent_run_id

        subagent_run_id = asyncio.run(seed())
        fresh_parent_log = PostgresEventLog(
            dsn=dsn,
            runtime_session_id=parent.runtime_session_id,
            workspace_root=tmp_path,
        )
        state = fold_subagent_graph(fresh_parent_log.iter())
        assert state.consistent
        assert state.runs[subagent_run_id].status == "completed"
        assert state.runs[subagent_run_id].result_id in state.results
        assert [event.sequence for event in fresh_parent_log.iter()] == list(
            range(1, fresh_parent_log.next_sequence())
        )
    finally:
        _delete_sessions(
            dsn,
            [session_id for session_id in (child_session_id, parent.runtime_session_id) if session_id],
        )


def test_postgres_subagent_three_way_projection_equality(tmp_path: Path) -> None:
    dsn, parent, _locator, runtime, context = _durable_runtime(tmp_path)
    child_session_id: str | None = None
    try:
        async def seed() -> None:
            nonlocal child_session_id
            await _start_parent_run(parent, context)
            task = await runtime.create_task(
                objective="three-way task",
                event_context=context,
                profile_id="review_worker",
                batch_id="subagent_batch:three-way",
                create_tool_call_id="tool:create-three-way",
                task_key="three-way",
            )
            child = await runtime.start_task(
                task.task_id,
                event_context=context,
                spawn_initiator_id="tool:create-three-way",
            )
            child_session_id = child.child_runtime_session_id
            await runtime.complete_fake(
                child.subagent_run_id,
                summary="three-way complete",
                event_context=context,
            )

        asyncio.run(seed())
        events = parent.event_log.iter()
        fresh_projection = project_subagent_graph(
            parent.runtime_session_id,
            fold_subagent_graph(events),
        )
        fresh_parent = RuntimeSession(
            tmp_path,
            event_log=PostgresEventLog(
                dsn=dsn,
                runtime_session_id=parent.runtime_session_id,
                workspace_root=tmp_path,
            ),
            archive=PostgresArtifactStore(dsn),
            tool_result_artifacts=PostgresToolResultArtifactIndex(dsn),
            runtime_session_id=parent.runtime_session_id,
        )
        fresh_runtime = SubagentRuntime(
            parent_runtime_session=fresh_parent,
            child_event_log_factory=PostgresEventLogLocator(
                dsn=dsn,
                workspace_root=tmp_path,
            ).event_log_for_runtime_session,
            event_log_locator=PostgresEventLogLocator(
                dsn=dsn,
                workspace_root=tmp_path,
            ),
        )
        runtime_projection = fresh_runtime.graph()
        inspect_projection = InspectorService(
            PostgresInspectorStore(dsn),
            oxigraph_url=None,
        ).inspect_session(parent.runtime_session_id)["subagent_graph"]

        expected_tasks = {
            task.task_id: (task.status, task.current_run_id, task.result_id)
            for task in fresh_projection.tasks
        }
        expected_runs = {
            node.subagent_run_id: (node.status, node.result_id)
            for node in fresh_projection.nodes
        }
        assert {
            task.task_id: (task.status, task.current_run_id, task.result_id)
            for task in runtime_projection.tasks
        } == expected_tasks
        assert {
            node.subagent_run_id: (node.status, node.result_id)
            for node in runtime_projection.nodes
        } == expected_runs
        assert {
            task["task_id"]: (
                task["status"],
                task["current_run_id"],
                task["result_id"],
            )
            for task in inspect_projection["tasks"]
        } == expected_tasks
        assert {
            node["subagent_run_id"]: (node["status"], node["result_id"])
            for node in inspect_projection["nodes"]
        } == expected_runs
    finally:
        _delete_sessions(
            dsn,
            [session_id for session_id in (child_session_id, parent.runtime_session_id) if session_id],
        )


def test_postgres_old_subagent_started_payload_without_budget_is_rejected(
    tmp_path: Path,
) -> None:
    dsn, parent, _locator, runtime, context = _durable_runtime(tmp_path)
    child_session_id: str | None = None
    try:
        async def seed() -> None:
            nonlocal child_session_id
            await _start_parent_run(parent, context)
            child = await runtime.spawn_fake(task="valid source", event_context=context)
            child_session_id = child.child_runtime_session_id

        asyncio.run(seed())
        valid = next(
            event
            for event in parent.event_log.iter()
            if isinstance(event, SubagentRunStartedEvent)
        )
        invalid_payload = dump_agent_event(valid)
        invalid_payload.pop("budget_snapshot")
        invalid_payload["id"] = f"event:invalid-old-subagent:{uuid4().hex}"
        invalid_payload["sequence"] = parent.event_log.next_sequence()
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into agent_events (
                        id, session_id, run_id, turn_id, reply_id,
                        sequence, event_type, created_at, payload
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s)
                    """,
                    (
                        invalid_payload["id"],
                        parent.runtime_session_id,
                        valid.run_id,
                        valid.turn_id,
                        valid.reply_id,
                        invalid_payload["sequence"],
                        str(valid.type),
                        valid.created_at,
                        Jsonb(invalid_payload),
                    ),
                )
        with pytest.raises(ValidationError):
            parent.event_log.iter()
    finally:
        _delete_sessions(
            dsn,
            [session_id for session_id in (child_session_id, parent.runtime_session_id) if session_id],
        )
