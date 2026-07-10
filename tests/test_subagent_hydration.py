from __future__ import annotations

import asyncio

from pulsara_agent.event import EventContext, RunEndEvent, RunStartEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.runtime.subagent import (
    InMemoryEventLogLocator,
    SubagentGraphHydrator,
    SubagentRuntime,
    fold_subagent_graph,
)
from tests.conftest import run_start_permission_fields
from tests.support.runtime_session import in_memory_runtime_session


CTX = EventContext(run_id="run:parent", turn_id="turn:parent", reply_id="reply:parent")


def _runtime(tmp_path):
    parent = in_memory_runtime_session(tmp_path, runtime_session_id="runtime:parent")
    locator = InMemoryEventLogLocator()

    def child_factory(runtime_session_id: str) -> InMemoryEventLog:
        log = InMemoryEventLog()
        locator.register(runtime_session_id, log)
        return log

    runtime = SubagentRuntime(
        parent_runtime_session=parent,
        child_event_log_factory=child_factory,
        event_log_locator=locator,
    )
    return parent, locator, runtime


def test_hydrator_loads_task_objective_from_artifact(tmp_path) -> None:
    parent, locator, runtime = _runtime(tmp_path)

    async def run() -> None:
        task = await runtime.create_task(
            objective="complete objective text",
            event_context=CTX,
            task_id="task:hydrate",
        )
        fact = fold_subagent_graph(parent.event_log.iter()).tasks[task.task_id]
        view = await SubagentGraphHydrator(
            archive=parent.archive,
            parent_runtime_session_id=parent.runtime_session_id,
            event_log_locator=locator,
        ).hydrate_task(fact, max_chars=100)
        assert view.objective_text == "complete objective text"
        assert view.objective_text_complete is True
        assert view.diagnostics == ()

    asyncio.run(run())


def test_hydrator_missing_task_artifact_returns_incomplete_diagnostic(tmp_path) -> None:
    parent, locator, runtime = _runtime(tmp_path)

    async def run() -> None:
        task = await runtime.create_task(
            objective="objective",
            event_context=CTX,
            task_id="task:missing",
        )
        fact = fold_subagent_graph(parent.event_log.iter()).tasks[task.task_id]
        parent.archive.blobs.pop(fact.objective_artifact_id)  # type: ignore[attr-defined]
        view = await SubagentGraphHydrator(
            archive=parent.archive,
            parent_runtime_session_id=parent.runtime_session_id,
            event_log_locator=locator,
        ).hydrate_task(fact, max_chars=100)
        assert view.objective_text is None
        assert view.objective_text_complete is False
        assert [item.code for item in view.diagnostics] == ["subagent_artifact_unavailable"]

    asyncio.run(run())


def test_child_log_hydrates_native_run_id(tmp_path) -> None:
    parent, locator, runtime = _runtime(tmp_path)

    async def run() -> None:
        child = await runtime.spawn_fake(task="inspect", event_context=CTX)
        session = runtime.child_runtime_session(child.subagent_run_id)
        child_ctx = EventContext(
            run_id="run:child-native",
            turn_id="turn:child-native",
            reply_id="reply:child-native",
        )
        await session.emit(
            RunStartEvent(
                **child_ctx.event_fields(),
                **run_start_permission_fields(child_ctx.run_id, source="child_profile"),
                user_input_chars=7,
            )
        )
        await session.emit(
            RunEndEvent(
                **child_ctx.event_fields(),
                status="finished",
                stop_reason="final",
            )
        )
        fact = fold_subagent_graph(parent.event_log.iter()).runs[child.subagent_run_id]
        view = await SubagentGraphHydrator(
            archive=parent.archive,
            parent_runtime_session_id=parent.runtime_session_id,
            event_log_locator=locator,
        ).hydrate_run(
            fact,
            include_task_text=False,
            include_child_native=True,
            max_chars=100,
        )
        assert view.child_run_id == child_ctx.run_id
        assert view.child_terminal_status == "finished"
        assert view.diagnostics == ()

    asyncio.run(run())


def test_child_log_multiple_native_runs_is_v1_error(tmp_path) -> None:
    parent, locator, runtime = _runtime(tmp_path)

    async def run() -> None:
        child = await runtime.spawn_fake(task="inspect", event_context=CTX)
        session = runtime.child_runtime_session(child.subagent_run_id)
        for index in (1, 2):
            child_ctx = EventContext(
                run_id=f"run:child:{index}",
                turn_id=f"turn:child:{index}",
                reply_id=f"reply:child:{index}",
            )
            await session.emit(
                RunStartEvent(
                    **child_ctx.event_fields(),
                    **run_start_permission_fields(child_ctx.run_id, source="child_profile"),
                    user_input_chars=1,
                )
            )
        fact = fold_subagent_graph(parent.event_log.iter()).runs[child.subagent_run_id]
        view = await SubagentGraphHydrator(
            archive=parent.archive,
            parent_runtime_session_id=parent.runtime_session_id,
            event_log_locator=locator,
        ).hydrate_run(
            fact,
            include_task_text=False,
            include_child_native=True,
            max_chars=100,
        )
        assert view.child_run_id is None
        assert [item.code for item in view.diagnostics] == ["multiple_child_native_runs"]

    asyncio.run(run())


def test_reported_and_native_child_run_id_must_match(tmp_path) -> None:
    parent, locator, runtime = _runtime(tmp_path)

    async def run() -> None:
        child = await runtime.spawn_fake(task="inspect", event_context=CTX)
        session = runtime.child_runtime_session(child.subagent_run_id)
        child_ctx = EventContext(
            run_id="run:child-native",
            turn_id="turn:child-native",
            reply_id="reply:child-native",
        )
        await session.emit(
            RunStartEvent(
                **child_ctx.event_fields(),
                **run_start_permission_fields(child_ctx.run_id, source="child_profile"),
                user_input_chars=1,
            )
        )
        await runtime.complete_fake(
            child.subagent_run_id,
            summary="done",
            child_run_id="run:reported-different",
        )
        fact = fold_subagent_graph(parent.event_log.iter()).runs[child.subagent_run_id]
        view = await SubagentGraphHydrator(
            archive=parent.archive,
            parent_runtime_session_id=parent.runtime_session_id,
            event_log_locator=locator,
        ).hydrate_run(
            fact,
            include_task_text=False,
            include_child_native=True,
            max_chars=100,
        )
        assert view.child_run_id == child_ctx.run_id
        assert [item.code for item in view.diagnostics] == ["child_run_attribution_mismatch"]

    asyncio.run(run())


def test_wait_result_hydration_is_bounded_and_artifact_backed(tmp_path) -> None:
    parent, locator, runtime = _runtime(tmp_path)

    async def run() -> None:
        child = await runtime.spawn_fake(task="inspect", event_context=CTX)
        completed = await runtime.complete_fake(
            child.subagent_run_id,
            summary="summary",
            output_preview="0123456789",
        )
        result = fold_subagent_graph(parent.event_log.iter()).results[completed.result_id]
        view = await SubagentGraphHydrator(
            archive=parent.archive,
            parent_runtime_session_id=parent.runtime_session_id,
            event_log_locator=locator,
        ).hydrate_result(result, max_chars=4)
        assert view.result_text == "0123"
        assert view.result_text_complete is False
        assert [item.code for item in view.diagnostics] == ["subagent_artifact_clipped"]

    asyncio.run(run())
